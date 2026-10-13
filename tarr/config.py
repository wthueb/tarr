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
SeedTimeMinutes = Annotated[
    int,
    AfterValidator(_check_limit),
    Field(
        description=(
            "Nonnegative integer seed time in minutes (10080 = 7 days, 20160 = 14 days). "
            "-1 means unlimited: this dimension never qualifies a torrent for removal. "
            "Unlimited is greater than any finite value when comparing policies."
        ),
        examples=[10080, 20160, -1],
        json_schema_extra={"anyOf": [{"minimum": 0}, {"const": UNLIMITED}]},
    ),
]
# a share ratio, or -1 for unlimited
Ratio = Annotated[
    float,
    AfterValidator(_check_limit),
    Field(
        description=(
            "Finite, nonnegative uploaded/downloaded ratio. -1 means unlimited: this "
            "dimension never qualifies a torrent for removal. Unlimited is greater "
            "than any finite value when comparing policies."
        ),
        examples=[1.0, 2.0, -1],
        json_schema_extra={"anyOf": [{"minimum": 0}, {"const": UNLIMITED}]},
    ),
]
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
    """A complete seed policy; both seed_time_minutes and ratio are required.

    Removal requires reaching the seed time OR ratio, not both. If both are -1,
    this policy prevents removal. Limits are validated even for disabled jobs.
    """

    model_config = ConfigDict(extra="forbid")

    seed_time_minutes: SeedTimeMinutes
    ratio: Ratio = Field(allow_inf_nan=False)


def limit_at_least(value: float, minimum: float) -> bool:
    return value == UNLIMITED or (minimum != UNLIMITED and value >= minimum)


class TrackerConfig(BaseModel):
    """Retention policy for a tracker. Every matching tracker applies to a torrent.

    Each desired limit must be at least its corresponding required limit; equality
    is allowed and -1 (unlimited) is greater than finite values. Startup enforces
    these comparisons. Torrents without any matching tracker are left alone by
    set_seed_limits, remove_stopped, and maintain_free_space.
    """

    model_config = ConfigDict(extra="forbid")

    name: str = Field(description="Tracker display name used in logs.", examples=["Example"])
    hosts: list[str] = Field(
        min_length=1,
        description=(
            "Announce hostnames, without URL schemes or paths. Matching is case-insensitive "
            "and includes subdomains. All matching configured trackers apply once each."
        ),
        examples=[["tracker.example.org"]],
    )
    required: SeedLimits = Field(
        description=(
            "Minimum retention policy for remove_stopped and maintain_free_space. Every "
            "matched tracker must pass its required seed-time OR ratio threshold. "
            "remove_unregistered bypasses this policy."
        ),
        examples=[{"seed_time_minutes": 10080, "ratio": 1.0}],
    )
    desired: SeedLimits = Field(
        description=(
            "Share limits applied by set_seed_limits. Each field must be at least the "
            "corresponding required field. Across matching trackers, the maximum in "
            "each dimension wins; unlimited dominates. A category override replaces both."
        ),
        examples=[{"seed_time_minutes": 20160, "ratio": 2.0}],
    )
    delete_priority: int = Field(
        default=0,
        description=(
            "Free-space deletion priority; higher values are deleted first, then torrents "
            "with more swarm seeders. The effective priority is the MINIMUM across all "
            "matched trackers. Negative values are allowed. Priority never bypasses retention."
        ),
        examples=[0, 100, -100],
    )

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
    """Structured logs always go to stdout; optionally also write rotating files."""

    model_config = ConfigDict(extra="forbid")

    level: str = Field(
        default="INFO",
        description=(
            "Python log level, case-insensitive: DEBUG, INFO, WARNING, ERROR, CRITICAL, "
            "or NOTSET (WARN and FATAL aliases are also accepted)."
        ),
        examples=["INFO", "DEBUG"],
    )
    format: Literal["logfmt", "json"] = Field(
        default="logfmt",
        description="Structured output format for both stdout and optional file logs.",
        examples=["logfmt", "json"],
    )
    file: pathlib.Path | None = Field(
        default=None,
        description=(
            "Optional log file path; omit or use null for stdout only. Files rotate at "
            "10 MiB. The process needs permission to create and write the file."
        ),
        examples=["/var/log/tarr/tarr.log", None],
    )
    file_count: int = Field(
        default=20,
        ge=1,
        description="Number of rotated backup log files to retain when file logging is enabled.",
        examples=[20],
    )

    @field_validator("level")
    @classmethod
    def validate_level(cls, value: str) -> str:
        value = value.upper()
        if value not in logging.getLevelNamesMapping():
            raise ValueError(f"unknown log level {value!r}")
        return value


class RemovalCategoryConfig(BaseModel):
    """A removal category with optional per-job Radarr/Sonarr queue verification."""

    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(
        description=(
            "Exact, case-sensitive qBittorrent category name. YAML null (or an empty string) "
            "means torrents without a category. ignore_categories always takes precedence."
        ),
        examples=["radarr", None],
    )
    arr_verify_removal: list[str] = Field(
        default_factory=list,
        description=(
            "Names from the top-level arrs mapping; defaults to []. Immediately before "
            "removal, every named instance's /api/v3/queue is checked, including all pages "
            "and unknown items. Any matching downloadId blocks removal regardless of status. "
            "Unreachable instances, authentication failures, or invalid responses also block "
            "removal. Unknown names fail startup. Checks apply only to this job/category "
            "and run during dry runs too. Use action: Stop in set_seed_limits so qBittorrent "
            "cannot automatically delete before verification."
        ),
        examples=[[], ["radarr"], ["radarr", "radarr4k"]],
    )

    @field_validator("name")
    @classmethod
    def _normalize_uncategorized_name(cls, value: str | None) -> str | None:
        return None if value == "" else value


RemovalCategory = RemovalCategoryConfig | str | None


def removal_category_name(category: RemovalCategory) -> str | None:
    return category.name if isinstance(category, RemovalCategoryConfig) else category or None


class ArrConfig(BaseModel):
    """Connection to a named Radarr or Sonarr instance for guarded torrent removal."""

    model_config = ConfigDict(extra="forbid")

    host: str = Field(
        description=(
            "HTTP(S) base URL, optionally including a reverse-proxy base path. Credentials, "
            "query strings, and fragments are forbidden. Trailing slashes are stripped."
        ),
        examples=["http://radarr:7878", "https://media.example.org/sonarr"],
    )
    api_key: str = Field(
        min_length=1,
        repr=False,
        description="Nonempty Radarr/Sonarr API key from Settings > General. Treat as a secret.",
        examples=["replace-me"],
    )

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
    """Shared opt-in job settings and exact, case-sensitive include/exclude filters."""

    model_config = ConfigDict(extra="forbid")

    enabled: bool = Field(
        default=False,
        description="Enable this removal job. Disabled by default; review settings with --dry-run.",
        examples=[False, True],
    )
    categories: list[RemovalCategory] | None = Field(
        default=None,
        description=(
            "Include only these exact categories; omit or use null to include all. An empty "
            "list includes none. Entries may be strings, YAML null for uncategorized torrents, "
            "or objects with name and arr_verify_removal. Verification is not inherited by "
            "other jobs. ignore_categories always wins."
        ),
        examples=[None, ["autobrr", None], [{"name": "radarr", "arr_verify_removal": ["radarr"]}]],
    )
    ignore_categories: list[str | None] = Field(
        default_factory=list,
        description=(
            "Never consider torrents in these exact categories, even if also included in "
            "categories. YAML null represents uncategorized torrents. Defaults to []."
        ),
        examples=[[], [None, "upload"]],
    )

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
    """Delete torrents and content after a tracker continuously reports unregistered.

    Tracker retention requirements do not apply to this job. Category exclusions
    and any configured Arr verification still apply. Runs before all other jobs.
    """

    delay_minutes: int = Field(
        default=0,
        ge=0,
        description=(
            "Wait this many minutes after first observing unregistered status, which some "
            "trackers report transiently. Zero removes immediately. Timers are in memory "
            "and reset on restart; positive delays are useful only with --daemon."
        ),
        examples=[15, 0],
    )


class RemoveStoppedConfig(CategoryFilterConfig):
    """Remove completed torrents that remain stopped (pausedUP or stoppedUP).

    Requires reaching an active qBittorrent seed-time OR ratio limit AND passing
    every matched tracker's required policy. No tracker match means no removal.
    Inherited, manual, and set_seed_limits limits are respected. Both active limits
    unlimited prevent removal. Runs after set_seed_limits, fetching updated limits.
    Actively seeding torrents are left alone; resuming resets the stopped timer.
    """

    delay_minutes: int = Field(
        default=0,
        ge=0,
        description=(
            "Minutes continuously observed as completed and stopped before removal. "
            "Zero allows immediate removal once eligibility checks pass. Timers reset on "
            "restart or if the torrent resumes/is incomplete; positive delays need --daemon."
        ),
        examples=[15, 0],
    )
    on_delete: Literal["Remove", "RemoveWithContent"] = Field(
        default="Remove",
        description="Remove keeps downloaded files; RemoveWithContent deletes them too.",
        examples=["Remove", "RemoveWithContent"],
    )


class MaintainFreeSpaceConfig(CategoryFilterConfig):
    """Delete eligible torrents with content until qBittorrent reports enough free space.

    Every matched tracker must pass its required seed-time OR ratio policy; no match
    means no removal. Highest effective delete_priority goes first (the minimum
    across matched trackers), then most swarm seeders. Desired limits and category
    overrides do not govern eligibility. Space is re-read after each removal so
    hard links are not assumed to reclaim space. Dry runs estimate from sizes.
    Arr verification failures skip a candidate and continue to other torrents.
    """

    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={
            "allOf": [
                {
                    "if": {"properties": {"enabled": {"const": True}}, "required": ["enabled"]},
                    "then": {
                        "properties": {"free_space_threshold_gibi": {"type": "integer"}},
                        "required": ["free_space_threshold_gibi"],
                    },
                }
            ]
        },
    )

    free_space_threshold_gibi: int | None = Field(
        default=None,
        ge=0,
        description=(
            "Target free space in GiB (1 GiB = 1024^3 bytes), measured by qBittorrent. "
            "Required and non-null when enabled. Below this threshold, remove eligible "
            "torrents until the target is met or no eligible candidates remain."
        ),
        examples=[1024, 100],
    )

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
    """Explicitly managed category with a required action and optional complete override."""

    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(
        description=(
            "Exact, case-sensitive category to manage. YAML null (or an empty string) "
            "means uncategorized torrents. Names must be unique in set_seed_limits."
        ),
        examples=["cross", "upload", None],
    )
    override: SeedLimits | None = Field(
        default=None,
        description=(
            "Replace both tracker-derived desired limits. Both fields are mandatory and "
            "each must be at least EVERY configured tracker's corresponding required "
            "limit, even for Stop actions or disabled jobs. -1 is unlimited. Startup "
            "checks these comparisons. Omit or use null to use tracker desired limits. "
            "Torrents with no matching tracker are still skipped."
        ),
        examples=[None, {"seed_time_minutes": -1, "ratio": -1}],
    )
    action: ShareLimitAction = Field(
        description=(
            "Required qBittorrent action when a share limit is reached: Default uses the "
            "global action; Stop stops seeding; Remove removes the torrent but keeps files; "
            "RemoveWithContent also deletes files; EnableSuperSeeding enables super-seeding. "
            "There is no global tarr action or inheritance. Use Stop for Arr-verified "
            "categories and let remove_stopped perform guarded removal."
        ),
        examples=["Stop", "RemoveWithContent"],
    )

    @field_validator("name")
    @classmethod
    def _normalize_uncategorized_name(cls, value: str | None) -> str | None:
        return None if value == "" else value


class SetSeedLimitsConfig(BaseModel):
    """Reconcile share limits only for explicitly listed categories with matching trackers.

    Uses the maximum desired limit in each dimension across matching trackers;
    unlimited dominates. A complete category override replaces both limits.
    Unlisted categories and torrents without a matching tracker remain untouched.
    Sets inactive-seeding time to unlimited and MatchAny semantics. Runs before
    remove_stopped so guarded removal sees updated active limits.
    """

    model_config = ConfigDict(extra="forbid")

    enabled: bool = Field(
        default=False,
        description="Enable share-limit reconciliation. Disabled by default.",
        examples=[False, True],
    )
    categories: list[SetSeedLimitsCategoryConfig] = Field(
        default_factory=list,
        description=(
            "Opt-in list of exact categories. Each entry requires name and action; names "
            "must be unique (null and empty string both mean uncategorized). Defaults to "
            "[]: no torrents are managed. No tracker match means no change, even with override."
        ),
        examples=[
            [{"name": "radarr", "action": "Stop"}],
            [
                {
                    "name": "upload",
                    "action": "Stop",
                    "override": {"seed_time_minutes": -1, "ratio": -1},
                }
            ],
        ],
    )

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
    """Prune old empty directories left by qBittorrent or other tools; runs last.

    Only immediate children of configured roots are scanned. Never removes roots,
    symlinks, nonempty directories, or paths referenced by any current torrent,
    regardless of category/state. No cleanup if the torrent listing fails or paths
    are missing/relative. Uses rmdir only, never recursive deletion. Requires local
    storage mounts and write permission; a remote qBittorrent host alone is insufficient.
    """

    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={
            "allOf": [
                {
                    "if": {"properties": {"enabled": {"const": True}}, "required": ["enabled"]},
                    "then": {
                        "properties": {"directories": {"minItems": 1}},
                        "required": ["directories"],
                    },
                }
            ]
        },
    )

    enabled: bool = Field(
        default=False,
        description="Enable local empty-directory cleanup. Disabled by default.",
        examples=[False, True],
    )
    directories: list[str] = Field(
        default_factory=list,
        description=(
            "Cleanup roots; at least one is required when enabled. A plain absolute path "
            "means the same path in qBittorrent and tarr. For different mounts use "
            "'qBittorrent-path:tarr-local-path' (qBittorrent first); only the local side "
            "is scanned. Both sides must be absolute, non-root paths without '..'. "
            "Plain and mapped roots can be mixed; conflicting mappings for the same "
            "source fail startup. Most-specific source mappings protect torrent paths. "
            "Defaults to []."
        ),
        examples=[["/downloads/cross-seed"], ["/downloads/cross-seed:/data/torrents/cross-seed"]],
    )
    name_pattern: str = Field(
        default="*--????????",
        description=(
            "Glob matched against directory basenames, not paths. The default matches "
            "qui's name--<8-character hash prefix> directories. Use '*' for every immediate "
            "child. Must be nonempty, contain no slash/backslash, and not be '.' or '..'. "
            "Quote patterns in YAML so '*' is not parsed as an alias."
        ),
        examples=["*--????????", "*"],
    )
    min_age_minutes: int = Field(
        default=60,
        ge=0,
        description=(
            "Minimum directory age in minutes, measured from its modification time. "
            "Zero disables the age guard; emptiness and torrent-path protection still apply."
        ),
        examples=[60, 0],
    )

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
    """qBittorrent WebUI connection settings only; polling and jobs belong at the root."""

    model_config = ConfigDict(extra="forbid")

    host: str = Field(
        description=(
            "qBittorrent WebUI base URL reachable from tarr. With the included Docker "
            "Compose setup, use http://qbittorrent:8080 rather than localhost."
        ),
        examples=["http://localhost:8080", "http://qbittorrent:8080"],
    )
    username: str = Field(description="WebUI login username.", examples=["admin"])
    password: str = Field(
        description="WebUI login password. Replace the placeholder and treat as a secret.",
        examples=["replace-me"],
    )


class Config(BaseModel):
    """tarr configuration. Only qbittorrent and a nonempty trackers list are required.

    All jobs are disabled by default. Start with --dry-run before enabling removal
    or share-limit changes. Run once, or poll with --daemon. Job order is
    remove_unregistered, set_seed_limits, remove_stopped, maintain_free_space,
    cleanup_empty_dirs. Jobs and interval_seconds belong at the root, never under
    qbittorrent. Unknown keys and invalid policies stop startup, even for disabled
    jobs. The schema validates structure; startup also checks path safety, known
    Arr references, category uniqueness, and comparisons between retention limits.
    """

    model_config = ConfigDict(extra="forbid")

    qbittorrent: QBittorrentConfig = Field(
        description="Required qBittorrent WebUI connection; contains no job or polling settings.",
        examples=[{"host": "http://localhost:8080", "username": "admin", "password": "replace-me"}],
    )
    interval_seconds: int = Field(
        default=60,
        ge=1,
        description=(
            "Seconds between reconciliation passes with --daemon. Ignored for a single "
            "pass. A failed daemon pass is logged before the next attempt."
        ),
        examples=[60, 300],
    )
    remove_unregistered: RemoveUnregisteredConfig = Field(
        default_factory=RemoveUnregisteredConfig,
        description=(
            "Delete unregistered torrents with content after a continuous delay. Bypasses "
            "tracker retention policies but respects category filters and Arr verification. "
            "Omit to leave disabled."
        ),
        examples=[{"enabled": True, "delay_minutes": 15, "ignore_categories": [None, "upload"]}],
    )
    remove_stopped: RemoveStoppedConfig = Field(
        default_factory=RemoveStoppedConfig,
        description=(
            "Remove completed, stopped torrents after a delay when an active share limit "
            "and every matched tracker's required policy pass. Arr verification applies. "
            "Omit to leave disabled; runs after set_seed_limits."
        ),
        examples=[
            {
                "enabled": True,
                "delay_minutes": 15,
                "categories": [{"name": "radarr", "arr_verify_removal": ["radarr"]}],
                "on_delete": "RemoveWithContent",
            }
        ],
    )
    maintain_free_space: MaintainFreeSpaceConfig = Field(
        default_factory=MaintainFreeSpaceConfig,
        description=(
            "Delete eligible torrents with content to reach a free-space target, ordered "
            "by effective tracker priority then swarm seeders. Tracker required policies "
            "and Arr verification apply. Omit to leave disabled."
        ),
        examples=[{"enabled": True, "categories": ["autobrr"], "free_space_threshold_gibi": 1024}],
    )
    set_seed_limits: SetSeedLimitsConfig = Field(
        default_factory=SetSeedLimitsConfig,
        description=(
            "Reconcile share limits for explicitly listed categories using matched tracker "
            "desired limits or complete overrides. Each category requires an action. "
            "Omit to leave disabled."
        ),
        examples=[
            {
                "enabled": True,
                "categories": [
                    {"name": "cross", "action": "RemoveWithContent"},
                    {
                        "name": "upload",
                        "action": "Stop",
                        "override": {"seed_time_minutes": -1, "ratio": -1},
                    },
                    {"name": "radarr", "action": "Stop"},
                ],
            }
        ],
    )
    cleanup_empty_dirs: CleanupEmptyDirsConfig = Field(
        default_factory=CleanupEmptyDirsConfig,
        description=(
            "Prune old, empty immediate child directories on locally mounted storage, "
            "protecting symlinks and paths used by every current torrent. Runs last. "
            "Omit to leave disabled."
        ),
        examples=[
            {
                "enabled": False,
                "directories": ["/downloads/cross-seed"],
                "name_pattern": "*--????????",
                "min_age_minutes": 60,
            }
        ],
    )
    trackers: list[TrackerConfig] = Field(
        min_length=1,
        description=(
            "Required nonempty tracker policies. All matching announce hostnames (including "
            "subdomains) apply. Required limits guard removal; desired limits control share "
            "settings. No matching tracker means no action except remove_unregistered."
        ),
        examples=[
            [
                {
                    "name": "Example",
                    "hosts": ["tracker.example.org"],
                    "required": {"seed_time_minutes": 10080, "ratio": 1.0},
                    "desired": {"seed_time_minutes": 20160, "ratio": 2.0},
                    "delete_priority": 0,
                }
            ]
        ],
    )
    logging: LoggingConfig = Field(
        default_factory=LoggingConfig,
        description="Structured stdout and optional rotating file logs; defaults to INFO/logfmt.",
        examples=[{"level": "INFO", "format": "logfmt"}],
    )
    arrs: dict[str, ArrConfig] = Field(
        default_factory=dict,
        description=(
            "Optional named Radarr/Sonarr connections referenced by removal categories' "
            "arr_verify_removal lists. Names are case-sensitive; unknown references fail "
            "startup. Defaults to {}. Checks guard only tarr's deletions, not other tools "
            "or qBittorrent's autonomous removal."
        ),
        examples=[{"radarr": {"host": "http://radarr:7878", "api_key": "replace-me"}}],
    )

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
