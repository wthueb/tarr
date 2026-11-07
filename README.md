# tarr

`tarr` is a small automation service for qBittorrent. It applies tracker- and
category-specific retention rules, removes torrents that are no longer useful,
and keeps a configurable amount of disk space free. Run it once for a single
reconciliation pass or leave it running as a daemon.

The service talks to qBittorrent through its WebUI API. Jobs deleting content also
need read-only access to download/staging storage; the optional
`cleanup_empty_dirs` job needs writable access to its cleanup roots.
Configuration is a strictly validated YAML file;
unknown fields and invalid limits stop startup instead of being silently ignored.

## What it manages

Each job is independent and can be enabled or disabled:

| Job | Behavior |
| --- | --- |
| `remove_unregistered` | Deletes a torrent and its content after a tracker continuously reports it as unregistered for a configured delay. |
| `set_seed_limits` | Reconciles qBittorrent ratio, seed-time, limit action, and related share-limit settings for explicitly managed categories. |
| `remove_stopped` | Removes completed, stopped torrents after a delay only when an active torrent limit and every matched tracker's required policy are met, optionally keeping downloaded content. |
| `maintain_free_space` | When locally measured disk space is below the target, deletes eligible content one torrent at a time, waiting for verified file removal. |
| `cleanup_empty_dirs` | Prunes old, empty directories under explicitly configured roots, excluding paths referenced by any current torrent. |

Jobs run in the order listed above. `set_seed_limits` runs before `remove_stopped`,
which fetches the updated active limits even on the first pass.

The delayed jobs keep their first-seen timestamps in memory. Restarting `tarr`
resets those timers, and delays greater than zero are only useful in daemon mode.
Use `--dry-run` to inspect planned deletions and share-limit changes without
mutating qBittorrent or the filesystem.

## Quick start

Requirements are Python 3.12 or newer and a reachable qBittorrent WebUI.
Generate a minimal `config.yaml` from the schema's examples:

```nu
uv sync
uv run python -m tarr.schema --example | save config.yaml
```

Enter your WebUI credentials and tracker policies, then enable the jobs you want
using the schema's editor completions and hover documentation. The minimal starter
leaves every job disabled. Start with a dry run:

```nu
uv run tarr --config config.yaml --dry-run
```

For an expanded configuration demonstrating all sections, Arr verification, and
category overrides, use `--example full` instead. That example enables removal and
share-limit jobs; review its placeholder values and policies before using it.

Run a real pass by removing `--dry-run`, or poll continuously:

```nu
uv run tarr --config config.yaml --daemon
```

The config path defaults to `config.yaml`. It can also be supplied with `-c` /
`--config` or through the `CONFIG_FILE` environment variable. In daemon mode,
`interval_seconds` controls the polling interval and a failed pass is
logged before the next one is attempted.

## Configuration model

[`config.schema.json`](config.schema.json) is the configuration reference, generated
from the Pydantic models. Every field includes a description and examples; defaults,
units, safety rules, and interactions between jobs are documented at the relevant
field. The schema also contains complete minimal and expanded examples, so no
separate example YAML file is needed.

YAML editors supporting the YAML language server provide validation, completion,
and hover documentation when `config.yaml` starts with:

```yaml
# yaml-language-server: $schema=./config.schema.json
```

Keep the schema alongside the config file, or replace the relative path with
`https://raw.githubusercontent.com/wthueb/tarr/master/config.schema.json`.
Generated starters already include the directive. The schema checks types, required
fields, unknown keys, numeric limits, and enabled-job requirements; startup
validation also checks custom rules such as path safety, known Arr instances,
unique category names, and comparisons between tracker/category limits.

Its top-level sections are:

- `logging`: log level, output format, and optional rotating file output.
- `interval_seconds`: polling interval in daemon mode (defaults to 60 seconds).
- `qbittorrent`: WebUI connection settings only (`host`, `username`, `password`).
- `content_deletion`: shared storage mappings and confirmation timing for every job that deletes files.
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

Free-space cleanup measures available bytes directly on `free_space_path`, translated
from qBittorrent's namespace through `content_deletion.path_mappings`. It checks before
selection, immediately before deletion, and after confirmed removal; qBittorrent's
cached free-space report does not authorize deletions. Only content on the target
filesystem is eligible. Removing a hard link receives no assumed space credit.
Dry runs estimate reclaimed space from torrent sizes and label estimates explicitly.
Tracker lookup failures or missing/malformed eligibility data cannot authorize
removal. Arr verification still applies.

### Verified content deletion and migration

Every enabled job that deletes files now requires locally visible download/staging
storage and explicit mappings. Existing enabled configurations without mappings or a
free-space target path fail startup rather than falling back to API-only cleanup:

```yaml
content_deletion:
  path_mappings:
    - /downloads:/data/downloads
    - /incomplete:/data/incomplete
  poll_interval_seconds: 2
  timeout_seconds: 300
maintain_free_space:
  enabled: true
  free_space_threshold_gibi: 3072
  free_space_path: /downloads
```

A plain mapping path means identical paths in both containers; the most-specific
source mapping wins. Paths must be absolute, non-root, and contain no `..`.
Conflicting mappings fail validation. Map each separately mounted storage root,
including staging storage, and point `free_space_path` at the filesystem to protect.
The included Compose example shares writable qBittorrent volumes with tarr through
read-only mounts. Give tarr permission to list directories and inspect paths; it
never unlinks torrent data itself. `cleanup_empty_dirs` is separate and still needs
a writable mount for its configured roots; read-only mounts cannot prune directories.

All three removal jobs share one in-memory gate: a new content deletion cannot start
until qBittorrent no longer lists the previous torrent **and** its tracked files have
disappeared. Empty directories do not block confirmation. tarr records only files
that currently exist at the torrent's manifest paths in download/staging storage,
including `.!qB` variants and files beneath a reported content directory. Shared
save/staging directories are not recursively scanned for unrelated files. No specific
layout or match between `content_path` and the manifest is required.

If no files exist, confirmation requires only that qBittorrent no longer lists the
torrent. Downloaded-byte counters and file progress do not need to be zero. Missing
download subdirectories are allowed, but mapped storage roots must remain accessible
and unchanged, even when no files were recorded. This also applies to free-space
cleanup, which always remeasures actual available space rather than crediting sizes.
Symlinks, inaccessible storage, unsafe paths, and moving torrents still fail closed.

Active incomplete torrents are stopped before final inspection; tarr restores their
previous activity if it abandons deletion before sending the request. For unregistered
removal, tarr verifies the tracker error before stopping and preserves that evidence
through its own stop operation, which can clear qBittorrent's tracker response.
Category and Arr queue checks still run again immediately before deletion.

Timeout defaults to 300 seconds, with checks every two seconds. Timeout or an uncertain
API response retains the pending guard across daemon passes; content-removal jobs
remain blocked until confirmation succeeds. Other jobs and torrent-only removal can
continue. One-shot runs exit nonzero with an unconfirmed deletion. Dry runs never stop
torrents, request deletion, or wait for completion.

**Restarting tarr loses this guard.** Before restarting or resuming cleanup, check that
qBittorrent has finished outstanding deletions. Run only one tarr process against this
storage. No persistent state volume is used. Confirmation trusts unlink completion,
not an expected byte increase: open handles, delayed block release, external activity,
and one torrent larger than the deficit can still cause overshoot. This is not an
exact-target guarantee. qBittorrent's autonomous removal actions and other tools are
not serialized; use `Stop` plus tarr-managed removal where this protection is needed.

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
content_deletion:
  path_mappings:
    - "/downloads:/data/torrents"
    - /incomplete
cleanup_empty_dirs:
  enabled: true
  directories:
    - "/downloads/cross-seed:/data/torrents/cross-seed"
```

The included Compose file mounts download and staging volumes read-only in tarr.
Directory cleanup requires a writable mount for its cleanup roots, as in the example
above; otherwise pruning attempts log permission errors.

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

### End-to-end tests

The E2E suite requires only Docker with Compose, not a local Python environment.
From the repository root:

```nu
docker compose -f compose.e2e.yaml run --build --rm --use-aliases tests
```

Compose builds the Dockerfile's `test` target, seeds disposable volumes, waits
for qBittorrent v5.2.3 to be healthy, and runs pytest against `e2e/` as the normal
non-root application user. The test image extends the same runtime as production,
adding only the locked `test` dependency group and E2E suite. The default
`production` target contains neither pytest nor test code.

Compose manages the services; pytest fixtures manage the client, tracker, and
per-test torrent cleanup. There is no runtime dependency installation, Docker
socket mount, source bind mount, or host port exposure. The suite covers the CLI,
read-only storage, hard links, multi-file and nested torrents, partial downloads,
and zero-data removal against a local fake tracker. The network is internal;
production is never contacted. `uv run pytest` runs only the unit tests and does
not need Docker.

The command returns the test runner's exit code. After each run, including a
failed or interrupted run, remove the isolated containers, network, and volumes:

```nu
docker compose -f compose.e2e.yaml down --volumes --remove-orphans
```

The `tarr-e2e` Compose project is separate from the production `compose.yaml`.
Images are retained for reuse. To test an already-built image without rebuilding:

```nu
$env.TARR_E2E_IMAGE = "tarr:test"
docker compose -f compose.e2e.yaml run --rm --use-aliases tests
```

Use `docker compose -f compose.e2e.yaml logs` for service logs. To run concurrent
suites, add the same `--project-name <unique-name>` to both the run and cleanup
commands so they use separate resources.

GitHub Actions runs the same Compose suite on every pull request, push to `master`,
and manual CI run. It builds and loads the `test` target with BuildKit caching,
then runs Compose without rebuilding or installing Python on the runner. It does
not publish or transfer test images between jobs. Cleanup always runs, Docker
diagnostics are uploaded on failure, and a failed E2E run blocks image publication.

After changing configuration models, regenerate the checked-in editor schema:

```nu
uv run python -m tarr.schema | save --force config.schema.json
```

Edit field descriptions and examples in `tarr/config.py`, not in the generated JSON.
The YAML starter generator uses those same schema examples. Tests verify schema/model
synchronization, documentation coverage, field examples, and both generated starters
against the schema and startup validation.

Source lives in [`tarr/`](tarr/), with configuration models in `config.py`, job
orchestration in `main.py`, shared content verification in `deletion.py`, path mapping
in `storage.py`, filesystem cleanup in `cleanup.py`, and structured logging in `logging.py`. Tests live in
[`tests/`](tests/). Releases are packaged from `pyproject.toml` and the container
build is defined in [`Dockerfile`](Dockerfile).

## License

`tarr` is available under the [MIT License](LICENSE).
