from __future__ import annotations

import pathlib
from typing import NamedTuple


class DirectoryMapping(NamedTuple):
    qbittorrent_path: pathlib.Path
    local_path: pathlib.Path

    @classmethod
    def parse(cls, value: str) -> DirectoryMapping:
        parts = value.split(":")
        if len(parts) not in {1, 2}:
            raise ValueError("directories must use 'qBittorrent-path:tarr-path' or one path")
        paths = [pathlib.Path(part) for part in parts]
        for path in paths:
            if not path.is_absolute() or path == pathlib.Path(path.anchor) or ".." in path.parts:
                raise ValueError("directories must be absolute, non-root paths without '..'")
        return cls(paths[0], paths[-1])


def mapped_path(path: pathlib.Path, mappings: list[DirectoryMapping]) -> pathlib.Path:
    matches = [mapping for mapping in mappings if path.is_relative_to(mapping.qbittorrent_path)]
    if not matches:
        raise ValueError(f"no content_deletion.path_mappings entry covers {path}")
    mapping = max(matches, key=lambda item: len(item.qbittorrent_path.parts))
    return mapping.local_path / path.relative_to(mapping.qbittorrent_path)


def validate_mappings(values: list[str]) -> list[str]:
    destinations: dict[pathlib.Path, pathlib.Path] = {}
    for value in values:
        mapping = DirectoryMapping.parse(value)
        previous = destinations.get(mapping.qbittorrent_path)
        if previous is not None and previous != mapping.local_path:
            raise ValueError("a qBittorrent directory cannot map to multiple tarr paths")
        destinations[mapping.qbittorrent_path] = mapping.local_path
    return values
