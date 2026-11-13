# tarr

`tarr` automates torrent retention and cleanup in qBittorrent. Set tracker and
category policies, keep disk space free, and run a single pass or a polling daemon.
It connects through the qBittorrent WebUI API.

## Features

Each job can be enabled independently:

| Job | What it does |
| --- | --- |
| `remove_unregistered` | Removes torrents and their content after a tracker reports them as unregistered for a configurable delay. |
| `set_seed_limits` | Applies tracker-based ratio and seed-time limits, category overrides, and per-category actions in qBittorrent. |
| `remove_stopped` | Removes completed, stopped torrents after a delay once their active limits and required tracker policies are met; optionally keeps the downloaded files. |
| `maintain_free_space` | Deletes eligible torrents by tracker priority and swarm seeder count until the configured free-space target is met. |
| `cleanup_empty_dirs` | Prunes old, empty directories under configured roots, while protecting paths used by current torrents. Supports qui cross-seed directory names. |

Other capabilities include:

- **Tracker-aware retention:** required policies protect torrents from premature
  removal; desired policies control share limits. Torrents matching multiple
  trackers must satisfy every tracker's required policy.
- **Category filtering:** include or exclude categories for each removal job, and
  explicitly opt categories into share-limit management.
- **Radarr/Sonarr verification:** optionally block removal while a torrent appears
  in any configured instance's queue. Failed checks also block removal.
- **Ownership-aware content deletion:** preserve file paths referenced by any other
  torrent, including unmanaged categories. Delete only uniquely owned files; if
  none are owned, remove only the torrent. Wait for torrent/file removal before
  starting another deletion, and measure actual free space rather than crediting sizes.
- **Dry runs:** preview deletions, directory cleanup, and share-limit changes
  without changing qBittorrent or the filesystem.
- **Validated YAML configuration:** catch invalid settings at startup, with an
  editor schema for completion and inline help.
- **Structured logging:** logfmt or JSON output, with optional rotating log files.

## Quick start

From a checkout, use Python 3.12+ and `uv` to install dependencies and generate a
starter configuration:

```sh
uv sync
uv run python -m tarr.schema --example > config.yaml
```

Set your qBittorrent WebUI address and credentials, add tracker policies, and enable
the jobs you want. The starter leaves all jobs disabled. Preview your changes:

```sh
uv run tarr --config config.yaml --dry-run
```

Remove `--dry-run` for a real pass, or run continuously:

```sh
uv run tarr --config config.yaml --daemon
```

The config path defaults to `config.yaml` and can also be set with `CONFIG_FILE`.
`interval_seconds` controls daemon polling.

## Configuration

[`config.schema.json`](config.schema.json) is the field-level reference, including
defaults, examples, and safety rules. Generated configurations include a schema
hint for YAML editors. To generate an expanded example, use
`uv run python -m tarr.schema --example full` instead; it enables jobs, so review
its placeholder values and policies before running it.

The main sections are `qbittorrent` for the connection, `trackers` for required and
desired seed policies, and one section per job. Optional `arrs` connections enable
queue verification. `content_deletion` maps qBittorrent's storage paths to locally
mounted paths, including when the two processes use different paths.

### Before enabling deletion

- Content-deleting jobs require local access to download and staging storage, plus
  explicit path mappings. Partially shared torrents require writable mounts and
  permission to unlink their uniquely owned files. Read-only mounts still support
  wholly owned content deletion through qBittorrent and shared-only metadata removal;
  partially shared deletions are skipped before removing the torrent if storage is
  read-only. Empty-directory cleanup also needs writable access.
- Ownership checks compare mapped file paths, not inode identities: separate
  hardlinks can be deleted independently. Manifest, download/staging, and `.!qB`
  paths are considered; failures to determine ownership skip removal. Mixed-content
  deletions remove torrent metadata first, then unlink only owned files, preserving
  newly detected references during confirmation. Empty directories are left alone.
- Ownership checks are refreshed before deletion but are not atomic with external
  clients adding, moving, or renaming torrents; avoid concurrent storage changes.
- Required tracker policies guard stopped-torrent and free-space removal.
  Unregistered-torrent removal is a separate policy and can delete incomplete
  downloads.
- Arr checks and deletion coordination only protect actions performed by `tarr`.
  Use qBittorrent's `Stop` action with `remove_stopped` if you want `tarr` to handle
  removal rather than qBittorrent deleting automatically.
- Run only one `tarr` process per storage setup. Delay timers and pending-deletion
  tracking are in memory and reset on restart; confirm outstanding deletions have
  finished before restarting cleanup.

## Docker

[`compose.yaml`](compose.yaml) runs qBittorrent and `tarr` together. Generate and
edit `config.yaml`, set its WebUI host to `http://qbittorrent:8080`, and review the
credentials and volume mounts before starting:

```sh
docker compose up -d
```

Compose builds the local source by default. Remove `build` to use the published
`ghcr.io/wthueb/tarr:latest` image instead. The image reads `/config/config.yaml`
and runs as UID/GID 999.

The supplied storage mounts are writable in `tarr` for selective file deletion.
Ensure its user can traverse storage and unlink files in their parent directories.
The configuration mount remains read-only.

## Development

Use `nix develop` for the reproducible development shell, or `uv sync` with a local
Python installation. Run the checks with:

```sh
uv run pytest
uv run ruff check .
uv run ruff format --check .
uv run ty check
```

The isolated end-to-end suite requires Docker Compose. Clean up its resources
after each run, including failures:

```sh
docker compose -f compose.e2e.yaml run --build --rm --use-aliases tests
docker compose -f compose.e2e.yaml down --volumes --remove-orphans
```

After editing configuration models in [`tarr/config.py`](tarr/config.py), regenerate
the schema:

```sh
uv run python -m tarr.schema > config.schema.json
```

## License

[MIT](LICENSE).
