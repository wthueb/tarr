import errno
import fnmatch
import os
import pathlib
import stat
import time
from collections.abc import Iterator

import qbittorrentapi
import structlog

from tarr.config import CleanupDirectoryMapping, CleanupEmptyDirsConfig

log = structlog.stdlib.get_logger("tarr")


def _local_torrent_paths(
    path: pathlib.Path,
    mappings: list[CleanupDirectoryMapping],
) -> Iterator[pathlib.Path]:
    matches = [mapping for mapping in mappings if path.is_relative_to(mapping.qbittorrent_path)]
    if matches:
        mapping = max(matches, key=lambda item: len(item.qbittorrent_path.parts))
        yield mapping.local_path / path.relative_to(mapping.qbittorrent_path)
    else:
        yield path
    for mapping in mappings:
        if mapping.qbittorrent_path.is_relative_to(path):
            yield mapping.local_path


def cleanup_empty_dirs(
    client: qbittorrentapi.Client,
    cfg: CleanupEmptyDirsConfig,
    dry_run: bool = False,
) -> None:
    log.info("checking for empty directories...")

    mappings = cfg.directory_mappings
    configured_roots = {mapping.local_path.resolve() for mapping in mappings}
    protected_paths: set[pathlib.Path] = set()
    for torrent in client.torrents_info():
        save_path = torrent.save_path
        if not save_path:
            raise ValueError("cannot clean directories without every torrent's save path")
        for value in (save_path, getattr(torrent, "content_path", "")):
            if not value:
                continue
            path = pathlib.Path(value)
            if not path.is_absolute() or ".." in path.parts:
                raise ValueError(
                    "torrent paths must be absolute without '..' for directory cleanup"
                )
            protected_paths.update(
                local.resolve() for local in _local_torrent_paths(path, mappings)
            )

    cutoff = time.time() - cfg.min_age_minutes * 60
    for mapping in mappings:
        directory = mapping.local_path
        try:
            if directory.is_symlink():
                log.warning("skipping symlink cleanup directory", directory=str(directory))
                continue
            root = directory.resolve(strict=True)
            if root == pathlib.Path(root.anchor):
                log.warning("skipping root cleanup directory", directory=str(directory))
                continue
            root_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        except OSError as error:
            log.warning("cannot open cleanup directory", directory=str(directory), error=str(error))
            continue

        try:
            with os.scandir(root_fd) as entries:
                names = [entry.name for entry in entries]
            for name in names:
                if not fnmatch.fnmatchcase(name, cfg.name_pattern):
                    continue
                path = root / name
                if any(configured.is_relative_to(path) for configured in configured_roots):
                    continue
                if any(
                    protected.is_relative_to(path) or path.is_relative_to(protected)
                    for protected in protected_paths
                ):
                    log.debug("directory is referenced by a torrent; skipping", directory=str(path))
                    continue
                try:
                    info = os.stat(name, dir_fd=root_fd, follow_symlinks=False)
                    if not stat.S_ISDIR(info.st_mode) or info.st_mtime > cutoff:
                        continue
                    child_fd = os.open(
                        name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=root_fd
                    )
                    try:
                        with os.scandir(child_fd) as contents:
                            if next(contents, None) is not None:
                                continue
                        current = os.stat(name, dir_fd=root_fd, follow_symlinks=False)
                        opened = os.fstat(child_fd)
                        if (current.st_dev, current.st_ino) != (opened.st_dev, opened.st_ino):
                            continue
                        if current.st_mtime > cutoff:
                            continue
                        if dry_run:
                            log.info(
                                "would remove empty directory", directory=str(path), dry_run=True
                            )
                        else:
                            os.rmdir(name, dir_fd=root_fd)
                            log.info("removed empty directory", directory=str(path), dry_run=False)
                    finally:
                        os.close(child_fd)
                except FileNotFoundError:
                    continue
                except OSError as error:
                    if error.errno in {errno.ENOTEMPTY, errno.EEXIST, errno.ENOTDIR, errno.ELOOP}:
                        log.debug("directory changed during cleanup; skipping", directory=str(path))
                    else:
                        log.warning(
                            "failed to remove empty directory",
                            directory=str(path),
                            error=str(error),
                        )
        except OSError as error:
            log.warning("cannot scan cleanup directory", directory=str(root), error=str(error))
        finally:
            os.close(root_fd)
