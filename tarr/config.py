from __future__ import annotations

import logging
import pathlib
from collections.abc import Iterable
from typing import Annotated, Literal, NamedTuple
from urllib.parse import urlparse

import yaml
from pydantic import AfterValidator, BaseModel, ConfigDict, Field, field_validator, model_validator

# a seed-time/ratio limit of -1 means "unlimited" (qBittorrent's own convention: -1 is no limit)
UNLIMITED = -1


def _check_limit(value: float) -> float:
    if value != UNLIMITED and value < 0:
        raise ValueError(f"must be a non-negative number, or {UNLIMITED} for unlimited")
    return value


# a number of minutes, or -1 for unlimited
SeedTimeMinutes = Annotated[int, AfterValidator(_check_limit)]
# a share ratio, or -1 for unlimited
Ratio = Annotated[float, AfterValidator(_check_limit)]
ShareLimitAction = Literal[
    "Default",
    "Stop",
    "Remove",
    "RemoveWithContent",
    "EnableSuperSeeding",
]


def category_is_included(
    category: str | None,
    categories: list[str | None] | None,
    ignore_categories: list[str | None],
) -> bool:
    """Return whether a qBittorrent category passes include/exclude filters."""
    # qBittorrent reports "" for torrents without a category, configured here as null.
    category = category or None
    return (categories is None or category in categories) and category not in ignore_categories


class SeedLimits(BaseModel):
    model_config = ConfigDict(extra="forbid")

    seed_time_minutes: SeedTimeMinutes
    ratio: Ratio = Field(allow_inf_nan=False)


def limit_at_least(value: float, minimum: float) -> bool:
    return value == UNLIMITED or (minimum != UNLIMITED and value >= minimum)


class TrackerConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    hosts: list[str] = Field(min_length=1)
    required: SeedLimits
    desired: SeedLimits
    delete_priority: int = 0

    @model_validator(mode="after")
    def _validate_desired_limits(self) -> TrackerConfig:
        for field in ("seed_time_minutes", "ratio"):
            if not limit_at_least(getattr(self.desired, field), getattr(self.required, field)):
                raise ValueError(
                    f"tracker {self.name!r}: desired.{field} must be at least required.{field}"
                )
        return self

    def matches(self, hostname: str) -> bool:
        hostname = hostname.lower()
        for host in self.hosts:
            host = host.lower()
            if hostname == host or hostname.endswith("." + host):
                return True
        return False


class LoggingConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    level: str = "INFO"
    format: Literal["logfmt", "json"] = "logfmt"
    file: pathlib.Path | None = None
    file_count: int = Field(default=20, ge=1)

    @field_validator("level")
    @classmethod
    def validate_level(cls, value: str) -> str:
        value = value.upper()
        if value not in logging.getLevelNamesMapping():
            raise ValueError(f"unknown log level {value!r}")
        return value


class RemovalCategoryConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str | None
    arr_verify_removal: list[str] = Field(default_factory=list)

    @field_validator("name")
    @classmethod
    def _normalize_uncategorized_name(cls, value: str | None) -> str | None:
        return None if value == "" else value


RemovalCategory = RemovalCategoryConfig | str | None


def removal_category_name(category: RemovalCategory) -> str | None:
    return category.name if isinstance(category, RemovalCategoryConfig) else category or None


class ArrConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    host: str
    api_key: str = Field(min_length=1, repr=False)

    @field_validator("host")
    @classmethod
    def _validate_host(cls, value: str) -> str:
        parsed = urlparse(value)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError(
                "Arr host must be an HTTP(S) URL without credentials, query or fragment"
            )
        return value.rstrip("/")


class CategoryFilterConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    # if set, only torrents in these categories are considered; a null entry
    # represents torrents without a category
    categories: list[RemovalCategory] | None = None
    # torrents in these categories are never considered; a null entry represents
    # torrents without a category
    ignore_categories: list[str | None] = Field(default_factory=list)

    def includes_category(self, category: str | None) -> bool:
        names = (
            [removal_category_name(item) for item in self.categories]
            if self.categories is not None
            else None
        )
        return category_is_included(category, names, self.ignore_categories)

    def arr_instances(self, category: str | None) -> list[str]:
        category = category or None
        return list(
            dict.fromkeys(
                instance
                for item in self.categories or []
                if isinstance(item, RemovalCategoryConfig) and item.name == category
                for instance in item.arr_verify_removal
            )
        )

    @property
    def overlapping_categories(self) -> list[str | None]:
        if self.categories is None:
            return []

        # Preserve the configured order while suppressing duplicate warnings.
        return list(
            dict.fromkeys(
                name
                for item in self.categories
                if (name := removal_category_name(item)) in self.ignore_categories
            )
        )


class RemoveUnregisteredConfig(CategoryFilterConfig):
    # wait this long after a tracker first reports a torrent as "unregistered"
    # before deleting it (some trackers report it transiently)
    delay_minutes: int = Field(default=0, ge=0)


class RemoveStoppedConfig(CategoryFilterConfig):
    # wait this long after first observing a torrent as completed and stopped before deleting it
    delay_minutes: int = Field(default=0, ge=0)
    # Remove keeps downloaded content; RemoveWithContent deletes it
    on_delete: Literal["Remove", "RemoveWithContent"] = "Remove"


class MaintainFreeSpaceConfig(CategoryFilterConfig):
    free_space_threshold_gibi: int | None = Field(default=None, ge=0)

    @property
    def free_space_threshold_bytes(self) -> int:
        return (self.free_space_threshold_gibi or 0) * 1024**3

    @model_validator(mode="after")
    def _require_threshold(self) -> MaintainFreeSpaceConfig:
        if self.enabled and self.free_space_threshold_gibi is None:
            raise ValueError(
                "free_space_threshold_gibi is required when maintain_free_space is enabled"
            )
        return self


class SetSeedLimitsCategoryConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # qBittorrent reports "" for torrents without a category, configured here as null.
    name: str | None
    override: SeedLimits | None = None
    action: ShareLimitAction

    @field_validator("name")
    @classmethod
    def _normalize_uncategorized_name(cls, value: str | None) -> str | None:
        return None if value == "" else value


class SetSeedLimitsConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    # Only torrents whose exact category appears here are managed.
    categories: list[SetSeedLimitsCategoryConfig] = Field(default_factory=list)

    @model_validator(mode="after")
    def _require_unique_categories(self) -> SetSeedLimitsConfig:
        names = [category.name for category in self.categories]
        if len(names) != len(set(names)):
            raise ValueError("set_seed_limits category names must be unique")
        return self

    def category_config(self, category: str | None) -> SetSeedLimitsCategoryConfig | None:
        category = category or None
        return next((item for item in self.categories if item.name == category), None)


class CleanupDirectoryMapping(NamedTuple):
    qbittorrent_path: pathlib.Path
    local_path: pathlib.Path

    @classmethod
    def parse(cls, value: str) -> CleanupDirectoryMapping:
        parts = value.split(":")
        if len(parts) not in {1, 2}:
            raise ValueError(
                "cleanup directories must use 'qBittorrent-path:tarr-path' or one path"
            )
        paths = [pathlib.Path(part) for part in parts]
        for path in paths:
            if not path.is_absolute() or path == pathlib.Path(path.anchor) or ".." in path.parts:
                raise ValueError(
                    "cleanup directories must be absolute, non-root paths without '..'"
                )
        return cls(paths[0], paths[-1])


class CleanupEmptyDirsConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    directories: list[str] = Field(default_factory=list)
    name_pattern: str = "*--????????"
    min_age_minutes: int = Field(default=60, ge=0)

    @field_validator("directories", mode="before")
    @classmethod
    def _stringify_paths(cls, value: object) -> object:
        if isinstance(value, (list, tuple)):
            return [str(item) if isinstance(item, pathlib.Path) else item for item in value]
        return value

    @field_validator("directories")
    @classmethod
    def _validate_mappings(cls, value: list[str]) -> list[str]:
        for directory in value:
            CleanupDirectoryMapping.parse(directory)
        return value

    @property
    def directory_mappings(self) -> list[CleanupDirectoryMapping]:
        return [CleanupDirectoryMapping.parse(directory) for directory in self.directories]

    @field_validator("name_pattern")
    @classmethod
    def _require_basename_pattern(cls, value: str) -> str:
        if not value or "/" in value or "\\" in value or value in {".", ".."}:
            raise ValueError("name_pattern must match directory names, not paths")
        return value

    @model_validator(mode="after")
    def _require_directories(self) -> CleanupEmptyDirsConfig:
        if self.enabled and not self.directories:
            raise ValueError("directories are required when cleanup_empty_dirs is enabled")
        destinations: dict[pathlib.Path, pathlib.Path] = {}
        for mapping in self.directory_mappings:
            previous = destinations.get(mapping.qbittorrent_path)
            if previous is not None and previous != mapping.local_path:
                raise ValueError(
                    "a qBittorrent cleanup directory cannot map to multiple tarr paths"
                )
            destinations[mapping.qbittorrent_path] = mapping.local_path
        return self


class QBittorrentConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    host: str
    username: str
    password: str


class Config(BaseModel):
    model_config = ConfigDict(extra="forbid")

    qbittorrent: QBittorrentConfig
    interval_seconds: int = Field(default=60, ge=1)
    remove_unregistered: RemoveUnregisteredConfig = Field(default_factory=RemoveUnregisteredConfig)
    remove_stopped: RemoveStoppedConfig = Field(default_factory=RemoveStoppedConfig)
    maintain_free_space: MaintainFreeSpaceConfig = Field(default_factory=MaintainFreeSpaceConfig)
    set_seed_limits: SetSeedLimitsConfig = Field(default_factory=SetSeedLimitsConfig)
    cleanup_empty_dirs: CleanupEmptyDirsConfig = Field(default_factory=CleanupEmptyDirsConfig)
    trackers: list[TrackerConfig] = Field(min_length=1)
    logging: LoggingConfig = Field(default_factory=LoggingConfig)
    arrs: dict[str, ArrConfig] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _require_known_arrs(self) -> Config:
        for job in ("remove_unregistered", "remove_stopped", "maintain_free_space"):
            cfg = getattr(self, job)
            for category in cfg.categories or []:
                if not isinstance(category, RemovalCategoryConfig):
                    continue
                for instance in category.arr_verify_removal:
                    if instance not in self.arrs:
                        raise ValueError(f"{job} references unknown Arr instance {instance!r}")
        return self

    @model_validator(mode="after")
    def _validate_category_overrides(self) -> Config:
        for category in self.set_seed_limits.categories:
            if category.override is None:
                continue
            for tracker in self.trackers:
                for field in ("seed_time_minutes", "ratio"):
                    if not limit_at_least(
                        getattr(category.override, field), getattr(tracker.required, field)
                    ):
                        raise ValueError(
                            f"category {category.name!r}: override.{field} must be at least "
                            f"tracker {tracker.name!r} required.{field}"
                        )
        return self

    def match_trackers(self, tracker_urls: Iterable[str]) -> list[TrackerConfig]:
        """Return every matching configured tracker once, in configuration order."""
        hostnames = [h for url in tracker_urls if (h := urlparse(url).hostname)]
        return [
            tracker
            for tracker in self.trackers
            if any(tracker.matches(hostname) for hostname in hostnames)
        ]


def load_config(path: pathlib.Path) -> Config:
    with path.open() as f:
        raw = yaml.safe_load(f)

    if not isinstance(raw, dict):
        raise ValueError(f"config file {path} is empty or not a mapping")

    return Config.model_validate(raw)
