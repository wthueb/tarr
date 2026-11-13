import math
import os
import pathlib
import stat
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import structlog

from tarr.config import ContentDeletionConfig
from tarr.storage import DirectoryMapping

log = structlog.stdlib.get_logger("tarr")
STOPPED_STATES = {"pausedDL", "stoppedDL", "pausedUP", "stoppedUP"}
UNSTABLE_STATES = {"moving", "checkingDL", "checkingUP", "checkingResumeData", "allocating"}


def absolute_path(value: Any) -> pathlib.Path:
    if not isinstance(value, str) or not value:
        raise ValueError("missing torrent storage path")
    path = pathlib.Path(value)
    if not path.is_absolute() or ".." in path.parts or path == pathlib.Path(path.anchor):
        raise ValueError("torrent storage paths must be absolute, non-root, without '..'")
    return path


def plain_stat(path: pathlib.Path) -> os.stat_result | None:
    current = pathlib.Path(path.anchor)
    for part in path.parts[1:]:
        current /= part
        try:
            info = current.lstat()
        except FileNotFoundError:
            return None
        if stat.S_ISLNK(info.st_mode):
            raise ValueError(f"symlink storage path: {current}")
    return info


class StorageView:
    def __init__(self, mappings: list[DirectoryMapping]):
        self.mappings = mappings
        self.roots: dict[pathlib.Path, tuple[int, int]] = {}

    def _check_root(self, root: pathlib.Path) -> None:
        info = plain_stat(root)
        if info is None or not stat.S_ISDIR(info.st_mode):
            raise ValueError(f"storage root is unavailable: {root}")
        identity = (info.st_dev, info.st_ino)
        if root in self.roots and self.roots[root] != identity:
            raise ValueError(f"storage root changed: {root}")
        self.roots[root] = identity
        with os.scandir(root):
            pass

    def check_roots(self) -> None:
        for root in self.roots:
            self._check_root(root)

    def local_path(self, path: pathlib.Path) -> pathlib.Path:
        matches = [item for item in self.mappings if path.is_relative_to(item.qbittorrent_path)]
        if not matches:
            raise ValueError(f"no content_deletion.path_mappings entry covers {path}")
        mapping = max(matches, key=lambda item: len(item.qbittorrent_path.parts))
        self._check_root(mapping.local_path)
        return mapping.local_path / path.relative_to(mapping.qbittorrent_path)

    def inspect(self, path: pathlib.Path) -> os.stat_result | None:
        roots = [root for root in self.roots if path.is_relative_to(root)]
        if not roots:
            raise ValueError(f"unmapped local path: {path}")
        for root in roots:
            self._check_root(root)
        return plain_stat(path)

    def available(self, path: pathlib.Path) -> tuple[int, int]:
        local = self.local_path(path)
        info = self.inspect(local)
        if info is None or not stat.S_ISDIR(info.st_mode):
            raise ValueError(f"free_space_path is not an accessible directory: {path}")
        self._check_root(local)
        fd = os.open(local, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            opened = os.fstat(fd)
            if (opened.st_dev, opened.st_ino) != (info.st_dev, info.st_ino):
                raise ValueError("free_space_path changed while measuring space")
            usage = os.fstatvfs(fd)
        finally:
            os.close(fd)
        self.inspect(local)
        return usage.f_bavail * usage.f_frsize, info.st_dev

    def require_writable(self, paths: set[pathlib.Path]) -> None:
        for path in paths:
            self.inspect(path)
            if os.statvfs(path.parent).f_flag & os.ST_RDONLY or not os.access(
                path.parent, os.W_OK | os.X_OK
            ):
                raise PermissionError(
                    f"selective content deletion requires writable storage: {path}"
                )

    def unlink(self, path: pathlib.Path, identity: tuple[int, int]) -> None:
        if self.inspect(path) is None:
            return
        root = max(
            (root for root in self.roots if path.is_relative_to(root)),
            key=lambda root: len(root.parts),
        )
        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
        fd = os.open(root, flags)
        try:
            opened = os.fstat(fd)
            if (opened.st_dev, opened.st_ino) != self.roots[root]:
                raise ValueError(f"storage root changed before selective deletion: {root}")
            for part in path.relative_to(root).parts[:-1]:
                child = os.open(part, flags, dir_fd=fd)
                os.close(fd)
                fd = child
            info = os.stat(path.name, dir_fd=fd, follow_symlinks=False)
            if not stat.S_ISREG(info.st_mode) or (info.st_dev, info.st_ino) != identity:
                raise ValueError(f"torrent file changed before selective deletion: {path}")
            os.unlink(path.name, dir_fd=fd)
        except FileNotFoundError:
            pass
        finally:
            os.close(fd)

    def files_under(self, directory: pathlib.Path) -> set[pathlib.Path]:
        info = self.inspect(directory)
        if info is None:
            return set()
        if not stat.S_ISDIR(info.st_mode):
            raise ValueError(f"torrent directory is not a directory: {directory}")
        result: set[pathlib.Path] = set()
        with os.scandir(directory) as entries:
            paths = [directory / entry.name for entry in entries]
        for path in paths:
            info = self.inspect(path)
            if info is None:
                raise ValueError("torrent files changed during inspection")
            if stat.S_ISDIR(info.st_mode):
                result.update(self.files_under(path))
            elif stat.S_ISREG(info.st_mode):
                result.add(path)
            else:
                raise ValueError(f"unsupported torrent file type: {path}")
        return result


@dataclass
class PendingDeletion:
    torrent_hash: str
    paths: set[pathlib.Path]
    started_at: float
    unlink_paths: dict[pathlib.Path, tuple[int, int]] | None = None


class ContentDeletionCoordinator:
    def __init__(self, client: Any, config: ContentDeletionConfig):
        self.client = client
        self.config = config
        self.storage = StorageView(config.directory_mappings)
        self.pending: PendingDeletion | None = None
        self.blocked = False
        self.estimated_reclaimed_bytes = 0

    @property
    def unresolved(self) -> bool:
        return self.pending is not None or self.blocked

    def begin_pass(self, dry_run: bool = False) -> None:
        if dry_run:
            return
        self.blocked = False
        if self.pending is not None:
            self._confirm()

    def free_space(self, path: pathlib.Path) -> int:
        return self.storage.available(path)[0]

    def _torrent(self, torrent_hash: str) -> Any | None:
        torrents = list(self.client.torrents_info(torrent_hashes=torrent_hash))
        if not torrents:
            return None
        if len(torrents) != 1 or torrents[0].hash != torrent_hash:
            raise ValueError("qBittorrent returned an unexpected torrent selection")
        return torrents[0]

    def _wait(self, predicate: Callable[[], bool]) -> bool:
        deadline = time.monotonic() + self.config.timeout_seconds
        while True:
            if predicate():
                return True
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            time.sleep(min(self.config.poll_interval_seconds, remaining))

    def _storage_bases(self, torrent: Any) -> set[pathlib.Path]:
        progress = torrent.progress
        if (
            isinstance(progress, bool)
            or not isinstance(progress, (int, float))
            or not math.isfinite(progress)
            or not 0 <= progress <= 1
        ):
            raise ValueError("invalid torrent progress")
        bases = {absolute_path(torrent.save_path)}
        if progress < 1:
            preferences = self.client.app_preferences()
            download_path = getattr(torrent, "download_path", "")
            if download_path:
                bases.add(absolute_path(download_path))
            if preferences.get("temp_path_enabled"):
                bases.add(absolute_path(preferences.get("temp_path")))
        return bases

    def _manifest_paths(self, torrent: Any, bases: set[pathlib.Path]) -> set[pathlib.Path]:
        paths: set[pathlib.Path] = set()
        for file in self.client.torrents_files(torrent_hash=torrent.hash):
            name = pathlib.Path(file.name)
            if not file.name or name.is_absolute() or ".." in name.parts or not name.parts:
                raise ValueError("invalid torrent file name")
            for base in bases:
                paths.add(base / name)
                paths.add(base / (str(name) + ".!qB"))
        return paths

    def _owned_paths(self, torrent_hash: str, paths: set[pathlib.Path]) -> set[pathlib.Path]:
        owned = paths.copy()
        if not paths:
            return owned
        for peer in self.client.torrents_info():
            if peer.hash == torrent_hash:
                continue
            bases = self._storage_bases(peer)
            references = self._manifest_paths(peer, bases)
            has_manifest = bool(references)
            content_path = getattr(peer, "content_path", "")
            if content_path:
                content = absolute_path(content_path)
                if not any(base.is_relative_to(content) for base in bases):
                    references.update((content, pathlib.Path(str(content) + ".!qB")))
            for reference in references:
                if not any(
                    reference.is_relative_to(mapping.qbittorrent_path)
                    for mapping in self.storage.mappings
                ):
                    continue
                local = self.storage.local_path(reference)
                if peer.state in UNSTABLE_STATES:
                    raise ValueError("another torrent's storage is moving or being checked")
                owned.discard(local)
                if not has_manifest and self.storage.inspect(local) is not None:
                    owned.difference_update(path for path in paths if path.is_relative_to(local))
        return owned

    def _snapshot(self, torrent: Any, target_device: int | None) -> set[pathlib.Path]:
        if torrent.state in UNSTABLE_STATES:
            raise ValueError("torrent storage is moving or being checked")
        bases = self._storage_bases(torrent)
        for base in bases:
            local = self.storage.local_path(base)
            info = self.storage.inspect(local)
            if info is not None:
                if not stat.S_ISDIR(info.st_mode):
                    raise ValueError(f"torrent storage path is not a directory: {base}")
                with os.scandir(local):
                    pass
        candidates = {
            self.storage.local_path(path) for path in self._manifest_paths(torrent, bases)
        }
        content_path = getattr(torrent, "content_path", "")
        if content_path:
            content = absolute_path(content_path)
            if not any(base.is_relative_to(content) for base in bases):
                local = self.storage.local_path(content)
                info = self.storage.inspect(local)
                if info is not None and stat.S_ISDIR(info.st_mode):
                    candidates.update(self.storage.files_under(local))
                else:
                    candidates.add(local)
                    candidates.add(self.storage.local_path(pathlib.Path(str(content) + ".!qB")))
        paths: set[pathlib.Path] = set()
        for path in candidates:
            info = self.storage.inspect(path)
            if info is None:
                continue
            if not stat.S_ISREG(info.st_mode):
                raise ValueError(f"torrent data is not a regular file: {path}")
            if target_device is not None and info.st_dev != target_device:
                raise ValueError("torrent content is not on the free-space target filesystem")
            paths.add(path)
        return paths

    def _restore(self, torrent_hash: str, forced: bool) -> None:
        try:
            if forced:
                self.client.torrents_set_force_start(torrent_hashes=torrent_hash, enable=True)
            else:
                self.client.torrents_start(torrent_hashes=torrent_hash)
        except Exception:
            log.exception("unable to restore torrent activity", torrent=torrent_hash)

    def delete(
        self,
        torrent: Any,
        allowed: Callable[[Any], bool],
        *,
        dry_run: bool = False,
        allowed_after_stop: Callable[[Any], bool] | None = None,
        free_space_path: pathlib.Path | None = None,
        threshold: int | None = None,
    ) -> bool:
        """Check policy and storage, then delete and confirm torrent content removal.

        allowed_after_stop replaces only the final policy check when this operation
        stopped the torrent. The initial allowed check always runs before stopping.
        """
        self.estimated_reclaimed_bytes = 0
        if self.unresolved:
            log.warning("content deletion blocked by unconfirmed operation", torrent=torrent.hash)
            return False
        stopped = False
        requested = False
        forced = False
        torrent_hash = torrent.hash
        try:
            torrent = self._torrent(torrent_hash)
            if torrent is None:
                return False
            if not allowed(torrent):
                return False
            target_device = None
            if free_space_path is not None:
                space, target_device = self.storage.available(free_space_path)
                if threshold is not None and space >= threshold:
                    return False
            paths = self._snapshot(torrent, target_device)
            if dry_run:
                owned = self._owned_paths(torrent_hash, paths)
                if paths and owned == paths:
                    self.estimated_reclaimed_bytes = torrent.size
                else:
                    self.estimated_reclaimed_bytes = sum(
                        info.st_size
                        for path in owned
                        if (info := self.storage.inspect(path)) is not None
                    )
                log.info(
                    "would verify content deletion",
                    torrent=torrent_hash,
                    estimated=True,
                    owned_file_count=len(owned),
                    shared_file_count=len(paths - owned),
                    estimated_reclaimed_bytes=self.estimated_reclaimed_bytes,
                )
                return True
            if torrent.progress < 1 and torrent.state not in STOPPED_STATES:
                forced = bool(getattr(torrent, "force_start", False))
                stopped = True
                self.client.torrents_stop(torrent_hashes=torrent_hash)

                def is_stopped() -> bool:
                    current = self._torrent(torrent_hash)
                    return current is not None and current.state in STOPPED_STATES

                if not self._wait(is_stopped):
                    raise TimeoutError("torrent did not stop before inspection")
                torrent = self._torrent(torrent_hash)
                if torrent is None:
                    return False
                paths = self._snapshot(torrent, target_device)
            current = self._torrent(torrent_hash)
            if current is None:
                return False
            location_fields = ("save_path", "content_path", "download_path", "state")
            if (
                any(
                    getattr(current, field, None) != getattr(torrent, field, None)
                    for field in location_fields
                )
                or self._snapshot(current, target_device) != paths
            ):
                raise ValueError("torrent storage changed before deletion")
            final_allowed = allowed_after_stop if stopped and allowed_after_stop else allowed
            if not final_allowed(current):
                log.info(
                    "torrent no longer eligible for content deletion",
                    torrent=torrent_hash,
                    stopped_by_tarr=stopped,
                )
                return False
            if free_space_path is not None and threshold is not None:
                if self.free_space(free_space_path) >= threshold:
                    return False
            owned = self._owned_paths(torrent_hash, paths)
            delete_files = bool(paths) and owned == paths
            unlink_paths = None
            if not delete_files:
                self.storage.require_writable(owned)
                unlink_paths = {}
                for path in owned:
                    info = self.storage.inspect(path)
                    if info is None:
                        raise ValueError("torrent files changed before selective deletion")
                    unlink_paths[path] = (info.st_dev, info.st_ino)
            requested = True
            self.pending = PendingDeletion(torrent_hash, owned, time.monotonic(), unlink_paths)
            log.info(
                "requesting serialized content deletion",
                torrent=torrent_hash,
                tracked_file_count=len(owned),
                owned_file_count=len(owned),
                shared_file_count=len(paths - owned),
                delete_files=delete_files,
            )
            self.client.torrents_delete(torrent_hashes=torrent_hash, delete_files=delete_files)
            return self._confirm()
        except Exception as error:
            if self.pending is not None:
                self.blocked = True
            log.warning(
                "content deletion could not be verified",
                torrent=torrent_hash,
                error=str(error),
                pending=self.pending is not None,
            )
            return False
        finally:
            if stopped and not requested:
                self._restore(torrent_hash, forced)

    def _confirm(self) -> bool:
        pending = self.pending
        if pending is None:
            return True

        def complete() -> bool:
            self.storage.check_roots()
            if pending.unlink_paths is not None:
                if self._torrent(pending.torrent_hash) is not None:
                    return False
                owned = self._owned_paths(pending.torrent_hash, pending.paths)
                for path in pending.paths - owned:
                    log.info(
                        "preserving newly shared torrent file",
                        torrent=pending.torrent_hash,
                        shared_path=str(path),
                    )
                    pending.unlink_paths.pop(path, None)
                pending.paths = owned
                for path, identity in pending.unlink_paths.items():
                    self.storage.unlink(path, identity)
            files_gone = all(self.storage.inspect(path) is None for path in pending.paths)
            return files_gone and self._torrent(pending.torrent_hash) is None

        try:
            confirmed = self._wait(complete)
        except Exception as error:
            log.warning(
                "content deletion confirmation failed",
                torrent=pending.torrent_hash,
                error=str(error),
            )
            confirmed = False
        if not confirmed:
            self.blocked = True
            log.warning(
                "content deletion remains unconfirmed; further content deletion blocked",
                torrent=pending.torrent_hash,
                elapsed_seconds=time.monotonic() - pending.started_at,
            )
            return False
        log.info(
            "content deletion confirmed",
            torrent=pending.torrent_hash,
            elapsed_seconds=time.monotonic() - pending.started_at,
        )
        self.pending = None
        self.blocked = False
        return True
