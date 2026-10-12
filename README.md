# tarr

`tarr` is a small automation service for qBittorrent. It applies tracker- and
category-specific retention rules, removes torrents that are no longer useful,
and keeps a configurable amount of disk space free. Run it once for a single
reconciliation pass or leave it running as a daemon.

The service talks to qBittorrent through its WebUI API. The optional
`cleanup_empty_dirs` job also needs direct access to the download filesystem.
Configuration is a strictly validated YAML file;
unknown fields and invalid limits stop startup instead of being silently ignored.

## What it manages

Each job is independent and can be enabled or disabled:

| Job | Behavior |
| --- | --- |
| `remove_unregistered` | Deletes a torrent and its content after a tracker continuously reports it as unregistered for a configured delay. |
| `set_seed_limits` | Reconciles qBittorrent ratio, seed-time, limit action, and related share-limit settings for explicitly managed categories. |
| `remove_stopped` | Removes completed, stopped torrents after a delay only when an active torrent limit and every matched tracker's required policy are met, optionally keeping downloaded content. |
| `maintain_free_space` | When disk space is below the target, deletes eligible torrents and their content, rechecking qBittorrent's reported free space after each deletion. |
| `cleanup_empty_dirs` | Prunes old, empty directories under explicitly configured roots, excluding paths referenced by any current torrent. |

Jobs run in the order listed above. `set_seed_limits` runs before `remove_stopped`,
which fetches the updated active limits even on the first pass.

The delayed jobs keep their first-seen timestamps in memory. Restarting `tarr`
resets those timers, and delays greater than zero are only useful in daemon mode.
Use `--dry-run` to inspect planned deletions and share-limit changes without
mutating qBittorrent or the filesystem.

## Quick start

Requirements are Python 3.12 or newer and a reachable qBittorrent WebUI. Copy
the annotated example, enter the WebUI credentials, and start with a dry run:

```nu
cp config.example.yaml config.yaml
uv sync
uv run tarr --config config.yaml --dry-run
```

Run a real pass by removing `--dry-run`, or poll continuously:

```nu
uv run tarr --config config.yaml --daemon
```

The config path defaults to `config.yaml`. It can also be supplied with `-c` /
`--config` or through the `CONFIG_FILE` environment variable. In daemon mode,
`interval_seconds` controls the polling interval and a failed pass is
logged before the next one is attempted.

## Configuration model

See [`config.example.yaml`](config.example.yaml) for the complete, documented
schema. Its top-level sections are:

- `logging`: log level, output format, and optional rotating file output.
- `interval_seconds`: polling interval in daemon mode (defaults to 60 seconds).
- `qbittorrent`: WebUI connection settings only (`host`, `username`, `password`).
- `remove_unregistered`, `remove_stopped`, `set_seed_limits`, `maintain_free_space`,
  and `cleanup_empty_dirs`: independent job settings.
- `trackers`: tracker hostnames, required and desired seed limits, and deletion priorities.
- `arrs`: optional named Radarr/Sonarr connections for removal verification.

Polling and job settings must be at the root; nesting them under `qbittorrent`
is rejected. The previous nested layout is not supported.

### Categories

The three removal jobs share include/exclude filters. `categories: null` includes
all categories; otherwise only exact names in the list are included.
`ignore_categories` always wins over `categories`. Use YAML `null` in either list
for torrents that have no category. Category entries can also be objects with
`name` and `arr_verify_removal`:

```yaml
remove_stopped:
  enabled: true
  categories:
    - name: radarr
      arr_verify_removal: [radarr, radarr4k]
    - name: sonarr
      arr_verify_removal: [sonarr]
  on_delete: RemoveWithContent
arrs:
  radarr:
    host: http://radarr:7878
    api_key: replace-me
  radarr4k:
    host: http://radarr4k:7878
    api_key: replace-me
  sonarr:
    host: http://sonarr:8989
    api_key: replace-me
```

This example is a fragment to merge into the full configuration. All three removal
jobs accept this shape. Verification is scoped to the job/category where it is
configured, not inherited by other jobs. Immediately before removal, each named
instance's `/api/v3/queue` is checked, including every page and unknown items.
Torrent hashes are matched case-insensitively against `downloadId`; any matching
queue entry blocks removal regardless of its status. An unreachable instance,
failed authentication, or invalid response also blocks removal. Unknown instance
names are rejected at startup. Dry runs perform the same checks without deleting.
Skipped torrents retain their delay tracking and can be reconsidered on later runs;
free-space cleanup continues to other candidates without counting skipped space.

Use `action: Stop` for these categories in `set_seed_limits` so qBittorrent cannot
automatically delete them before verification; enable `remove_stopped` to handle
subsequent removal. Checks only guard deletions performed by `tarr`, not other tools
or qBittorrent's own automatic removal actions.

`set_seed_limits` is intentionally opt-in: only exact category names listed under
that job are changed. A category named `null` manages uncategorized torrents.
Category names are case-sensitive and must be unique.

### Trackers and retention

Every configured tracker whose `hosts` entry matches an announce hostname or its
subdomain applies to the torrent. Duplicate announce URLs do not duplicate policies.
Unknown announces are ignored when at least one configured tracker matches; torrents
with no configured matches are never removed except by `remove_unregistered`.

Each tracker requires `required` and `desired` blocks containing both
`seed_time_minutes` and `ratio`. Required limits guard removal; desired limits
control qBittorrent share limits. For **each** matched tracker, a torrent must reach
its required seed time **OR** required ratio. **Every** matched tracker must pass,
though different trackers can pass via different dimensions. Thresholds are
inclusive. `-1` means unlimited and never qualifies that dimension for removal;
both required limits unlimited prevent removal. qBittorrent's achieved ratio of
`-1` represents infinite ratio and satisfies finite ratio requirements.

At startup, each desired field must be **greater than or equal to** its corresponding
required field, treating unlimited as greater than finite values. Equality is allowed,
including `0/0` and unlimited/unlimited. Limits must be nonnegative or `-1`, seed times
must be integer minutes, and configured ratios must be finite. Unsafe values stop
startup before connecting to qBittorrent, even if the relevant job is disabled.
The previous flat tracker limits and category precedence fields are not supported.

`remove_stopped` additionally requires a completed download in `pausedUP` or
`stoppedUP`, a continuously elapsed stopped delay, and at least one satisfied active
qBittorrent seed-time or ratio limit. Its active check uses resolved `max_seeding_time`
and `max_ratio`, including inherited defaults and manually defined limits. Both
active limits unlimited keep the torrent even after required policies pass. Active
limits, manual stops, and delays never bypass required tracker policies. The delay
resets if the torrent resumes or is no longer completed; actively seeding torrents
are untouched.

Free-space cleanup uses required policies, not desired limits or category overrides.
Its effective `delete_priority` is the **minimum across all matched trackers**.
Eligible torrents are ordered by effective priority descending, then total swarm
seeder count descending. Each tracker defaults to priority zero; positive and negative
integers are supported. A torrent matching priorities `1000` and `-500` has effective
priority `-500`, so a priority-zero torrent is removed first. Priority never bypasses
eligibility. Logs include all matched tracker names and the effective priority.

Torrents are deleted with content until qBittorrent reports enough free space or no
eligible torrents remain. Free space is re-read after each deletion, so removing a
hard link is not assumed to reclaim space while other links remain. Dry runs estimate
reclaimed space from torrent sizes. Tracker lookup failures or missing/malformed
eligibility data cannot authorize removal. Arr verification still applies.

### Share limits

For managed categories with at least one configured tracker match, `set_seed_limits`
uses the maximum desired seed time and maximum desired ratio across all matches.
Unlimited dominates finite values. A complete category `override` replaces both:

```yaml
set_seed_limits:
  enabled: true
  categories:
    - name: upload
      action: Stop
      override:
        seed_time_minutes: -1
        ratio: -1
```

Both override fields are mandatory and cannot be null. Each must be at least every
configured tracker's corresponding required value, even for `Stop` actions or disabled
jobs; unsafe overrides stop startup. Omit `override` (or set it to null) to use desired
tracker limits. Unlisted categories and torrents without any configured tracker match
are skipped entirely, even when an override exists. Their existing settings, including
any autonomous qBittorrent deletion behavior, remain untouched.

Each category requires `action`: `Default`, `Stop`, `Remove`, `RemoveWithContent`, or
`EnableSuperSeeding`. There is no global action or inheritance. Flat category limits,
`use_tracker_limits`, `force_use_tracker_limits`, and `default_*` fields are rejected.

The job also sets inactive-seeding time to unlimited and uses `MatchAny` semantics;
qBittorrent versions before 5.3 use these implicitly. Validated effective limits cannot
trigger deletion below matched tracker requirements. No API update is sent when the
resolved state already matches. These safeguards govern tarr's deletions and settings
it applies, not autonomous actions on untouched torrents or deletions by other tools.

### Empty directory cleanup

qBittorrent's `RemoveWithContent` action can leave the parent directories that
qui creates for cross-seeds. Enable this independent job to clean them up,
including leftovers from deletions outside `tarr`:

```yaml
cleanup_empty_dirs:
  enabled: true
  directories:
    - /downloads/cross-seed
  name_pattern: "*--????????"
  min_age_minutes: 60
```

The job runs each polling pass, after the other jobs. It only considers immediate
children of the configured roots; it never recursively deletes content or removes
the roots themselves. The default glob matches qui's `name--<8-character hash
prefix>` layout. Customize `name_pattern` for other naming schemes, or use `"*"`
to consider every immediate child.

A candidate must be empty and its modification time must be at least
`min_age_minutes` old. Symlink roots and children are skipped. Every current
torrent's save and content paths are protected, including stopped or downloading
torrents and torrents excluded from other jobs. If the torrent listing fails or
contains missing/relative save paths, no cleanup is performed. Removal uses only
`rmdir`, so newly added content prevents deletion. `--dry-run` logs candidates
without removing them; missing mounts and permission failures are logged.

Each entry in `directories` supports either:

- `/downloads/cross-seed`: the same path in qBittorrent and `tarr`.
- `/downloads/cross-seed:/data/torrents/cross-seed`: **qBittorrent path first,
  tarr-local path second**. Only the local side is scanned and modified.

Mappings translate torrent save and content paths before checking protection.
Matches use complete path components (not similar string prefixes), and the most
specific source prefix wins when mappings overlap. A torrent saved above a mapped
root conservatively protects that entire local root. Plain and mapped entries
can be mixed. Both sides must be absolute, non-root paths without `..`; malformed
mappings and conflicting destinations for the same source are rejected.

`tarr` must have the storage mounted locally and permission to remove directories,
but the mount paths may differ from qBittorrent's when a mapping is provided.
This job is disabled by default and requires at least one directory when enabled.
It is not suitable for a remote qBittorrent instance whose storage is not mounted
locally.

## Docker

The included [`compose.yaml`](compose.yaml) runs both qBittorrent and `tarr`.
Create `config.yaml`, change its qBittorrent host to
`http://qbittorrent:8080`, review the credentials and volume settings, then run:

```nu
docker compose up -d
```

The `tarr` image runs as UID/GID 999 and reads the mounted configuration from
`/config/config.yaml`. The compose file builds the local source by default;
remove `build` if you only want to use `ghcr.io/wthueb/tarr:latest`.

For directory cleanup, mount the same storage in **both** containers, and run
`tarr` with a UID/GID that can remove the folders. Different container paths are
supported. For example, add these settings to the respective services (retaining
their existing mounts):

```yaml
services:
  qbittorrent:
    volumes:
      - /path/to/downloads:/downloads
  tarr:
    user: "1000:1000" # match the download directory's ownership/permissions
    volumes:
      - /path/to/downloads:/data/torrents
```

For these mounts, configure `tarr` with:

```yaml
cleanup_empty_dirs:
  enabled: true
  directories:
    - "/downloads/cross-seed:/data/torrents/cross-seed"
```

The included compose file does not mount downloads by default; enabling cleanup
without adding this shared mount will only log a missing-directory warning.

## Logging

Logs go to stdout in logfmt format. Set `logging.format: json` for JSON, and set
`logging.file` to additionally write 10 MiB rotating files; `file_count` controls
how many are retained. Events include stable `ts`, `level`, `logger`, `src`, and
`msg` fields plus job, torrent, limits, and deletion context where relevant.

## Development

The repository uses `uv` for dependency locking and Nix for a reproducible
development shell. `nix develop` syncs and activates the virtual environment.
The usual checks are:

```nu
uv run pytest
uv run ruff check .
uv run ruff format --check .
uv run ty check
```

Source lives in [`tarr/`](tarr/), with configuration models in `config.py`, job
orchestration in `main.py`, filesystem cleanup in `cleanup.py`, and structured
logging in `logging.py`. Tests live in
[`tests/`](tests/). Releases are packaged from `pyproject.toml` and the container
build is defined in [`Dockerfile`](Dockerfile).

## License

`tarr` is available under the [MIT License](LICENSE).
