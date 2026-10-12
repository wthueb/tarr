import argparse
import datetime
import os
import pathlib
import time
from collections.abc import Iterable
from typing import Any, Protocol

import qbittorrentapi
import structlog
from pydantic import ValidationError
from qbittorrentapi.torrents import TorrentStatusesT
from structlog.contextvars import bound_contextvars

from tarr.arr import removal_allowed
from tarr.cleanup import cleanup_empty_dirs
from tarr.config import (
    UNLIMITED,
    ArrConfig,
    Config,
    QBittorrentConfig,
    RemovalCategory,
    RemoveUnregisteredConfig,
    TrackerConfig,
    category_is_included,
    load_config,
    removal_category_name,
)
from tarr.logging import setup_logging
from tarr.seed_limits import desired_limits, limits_met, matched_trackers, required_limits_met

log = structlog.stdlib.get_logger("tarr")

STOPPED_COMPLETED_STATES = {"pausedUP", "stoppedUP"}


class TorrentsClient(Protocol):
    def torrents_info(self, status_filter: TorrentStatusesT | None = None) -> Iterable[Any]: ...


def _torrent_context(torrent: Any) -> dict[str, Any]:
    return {
        "torrent": torrent.hash,
        "torrent_name": torrent.name,
        "torrent_state": torrent.state,
        "torrent_size_bytes": torrent.size,
    }


def build_client(cfg: QBittorrentConfig) -> qbittorrentapi.Client:
    client = qbittorrentapi.Client(
        host=cfg.host,
        username=cfg.username,
        password=cfg.password,
    )

    try:
        client.auth_log_in()
    except qbittorrentapi.LoginFailed as e:
        with bound_contextvars(error=str(e)):
            log.warning("failed to connect to qBittorrent")

    return client


def torrents_in_categories(
    client: TorrentsClient,
    categories: list[RemovalCategory] | None,
    ignore_categories: list[str | None] | None = None,
    status_filter: TorrentStatusesT | None = None,
) -> list[qbittorrentapi.TorrentDictionary]:
    if status_filter is None:
        torrents = client.torrents_info()
    else:
        torrents = client.torrents_info(status_filter=status_filter)
    ignored = ignore_categories or []
    names = [removal_category_name(item) for item in categories] if categories is not None else None

    return [
        torrent for torrent in torrents if category_is_included(torrent.category, names, ignored)
    ]


def warn_category_overlaps(config: Config) -> None:
    feature_configs = (
        ("remove_unregistered", config.remove_unregistered),
        ("remove_stopped", config.remove_stopped),
        ("maintain_free_space", config.maintain_free_space),
    )

    for feature_name, cfg in feature_configs:
        overlap = cfg.overlapping_categories
        if not overlap:
            continue

        with bound_contextvars(job=feature_name, overlapping_categories=overlap):
            log.warning("categories overlap with ignore_categories")


def remove_unregistered(
    client: qbittorrentapi.Client,
    cfg: RemoveUnregisteredConfig,
    unregistered_first_seen: dict[str, datetime.datetime],
    dry_run: bool = False,
    arrs: dict[str, ArrConfig] | None = None,
) -> None:
    log.info("checking for unregistered torrents...")

    torrents = torrents_in_categories(client, cfg.categories, cfg.ignore_categories)
    now = datetime.datetime.now()

    delay = datetime.timedelta(minutes=cfg.delay_minutes)

    currently_unregistered: set[str] = set()

    for torrent in torrents:
        with bound_contextvars(**_torrent_context(torrent)):
            log.debug("checking torrent")

            trackers = torrent.trackers

            for tracker in trackers:
                # https://github.com/qbittorrent/qBittorrent/wiki/WebUI-API-(qBittorrent-5.0)#get-torrent-trackers
                if tracker.status in [0, 1, 3]:
                    continue

                msg = tracker.msg.lower()

                if (
                    "unregistered torrent" in msg
                    or "torrent does not exist on this tracker" in msg
                    or "torrent has been deleted" in msg
                ):
                    log.debug(
                        "tracker reported unregistered torrent",
                        tracker_msg=tracker.msg,
                    )

                    # TL reports unregistered sometimes but then it goes away,
                    # so we want to wait a bit before removing
                    currently_unregistered.add(torrent.hash)

                    if torrent.hash not in unregistered_first_seen:
                        unregistered_first_seen[torrent.hash] = now
                        with bound_contextvars(delay_minutes=cfg.delay_minutes):
                            log.debug("first observed unregistered torrent")

                    first_seen = unregistered_first_seen[torrent.hash]
                    time_unregistered = now - first_seen

                    if time_unregistered >= delay:
                        if not removal_allowed(
                            torrent.hash, cfg.arr_instances(torrent.category), arrs or {}
                        ):
                            break
                        with bound_contextvars(
                            dry_run=dry_run,
                            delete_files=True,
                            unregistered_for_seconds=time_unregistered.total_seconds(),
                        ):
                            log.info("unregistered torrent eligible for removal")
                            if not dry_run:
                                torrent.delete(delete_files=True)
                                unregistered_first_seen.pop(torrent.hash, None)
                    else:
                        remaining = delay - time_unregistered
                        with bound_contextvars(
                            unregistered_for_seconds=time_unregistered.total_seconds(),
                            remaining_seconds=remaining.total_seconds(),
                        ):
                            log.debug("unregistered torrent removal delayed")
                    break

    stale_hashes = set(unregistered_first_seen.keys()) - currently_unregistered
    for torrent_hash in stale_hashes:
        with bound_contextvars(torrent=torrent_hash):
            log.debug("torrent no longer unregistered; clearing tracking state")
            unregistered_first_seen.pop(torrent_hash, None)


def remove_stopped(
    client: TorrentsClient,
    config: Config,
    stopped_first_seen: dict[str, datetime.datetime],
    dry_run: bool = False,
) -> None:
    log.info("checking for stopped torrents...")
    cfg = config.remove_stopped

    completed_torrents = torrents_in_categories(
        client,
        cfg.categories,
        cfg.ignore_categories,
        status_filter="completed",
    )
    torrents = [
        torrent for torrent in completed_torrents if torrent.state in STOPPED_COMPLETED_STATES
    ]
    now = datetime.datetime.now()
    delay = datetime.timedelta(minutes=cfg.delay_minutes)
    currently_stopped = {torrent.hash for torrent in torrents}

    for torrent in torrents:
        with bound_contextvars(**_torrent_context(torrent)):
            log.debug("checking torrent")

            if torrent.hash not in stopped_first_seen:
                stopped_first_seen[torrent.hash] = now
                with bound_contextvars(delay_minutes=cfg.delay_minutes):
                    log.debug("first observed completed and stopped torrent")

            first_seen = stopped_first_seen[torrent.hash]
            time_stopped = now - first_seen

            if time_stopped < delay:
                remaining = delay - time_stopped
                with bound_contextvars(
                    stopped_for_seconds=time_stopped.total_seconds(),
                    remaining_seconds=remaining.total_seconds(),
                ):
                    log.debug("stopped torrent removal delayed")
                continue

            trackers = matched_trackers(config, torrent)
            if not required_limits_met(torrent, trackers):
                continue

            active_limits_met = limits_met(
                getattr(torrent, "seeding_time", None),
                getattr(torrent, "ratio", None),
                getattr(torrent, "max_seeding_time", None),
                getattr(torrent, "max_ratio", None),
            )
            with bound_contextvars(
                seeding_time_seconds=torrent.seeding_time,
                torrent_ratio=torrent.ratio,
                minimum_seed_time_minutes=getattr(torrent, "max_seeding_time", None),
                minimum_ratio=getattr(torrent, "max_ratio", None),
            ):
                if not active_limits_met:
                    log.debug("torrent does not meet removal criteria")
                    continue

            if not removal_allowed(torrent.hash, cfg.arr_instances(torrent.category), config.arrs):
                continue

            delete_files = cfg.on_delete == "RemoveWithContent"
            with bound_contextvars(
                dry_run=dry_run,
                delete_files=delete_files,
                on_delete=cfg.on_delete,
                stopped_for_seconds=time_stopped.total_seconds(),
            ):
                log.info("stopped torrent eligible for removal")

                if not dry_run:
                    torrent.delete(delete_files=delete_files)
                    stopped_first_seen.pop(torrent.hash, None)

    stale_hashes = set(stopped_first_seen) - currently_stopped
    for torrent_hash in stale_hashes:
        with bound_contextvars(torrent=torrent_hash):
            log.debug("torrent no longer completed and stopped; clearing tracking state")
            stopped_first_seen.pop(torrent_hash, None)


def set_seed_limits(
    client: TorrentsClient,
    config: Config,
    dry_run: bool = False,
) -> None:
    cfg = config.set_seed_limits

    log.info("setting seed limits...")

    for torrent in client.torrents_info():
        with bound_contextvars(**_torrent_context(torrent)):
            category_cfg = cfg.category_config(torrent.category)
            if category_cfg is None:
                log.debug("torrent category is not managed; skipping")
                continue

            trackers = matched_trackers(config, torrent)
            if not trackers:
                continue
            limits = category_cfg.override or desired_limits(trackers)
            limit_source = "category_override" if category_cfg.override is not None else "trackers"
            target_seed_time = limits.seed_time_minutes
            target_ratio = limits.ratio
            target_action = category_cfg.action
            target_inactive_seed_time = UNLIMITED
            target_mode = "MatchAny"
            supports_share_limits_mode = hasattr(torrent, "share_limits_mode")
            # qBittorrent before 5.3 only supports MatchAny semantics and does not
            # expose share_limits_mode. Treat that implicit behavior as the target.
            current_mode = getattr(torrent, "share_limits_mode", target_mode)

            current_state = (
                torrent.ratio_limit,
                torrent.seeding_time_limit,
                torrent.inactive_seeding_time_limit,
                torrent.share_limit_action,
                current_mode,
            )
            target_state = (
                target_ratio,
                target_seed_time,
                target_inactive_seed_time,
                target_action,
                target_mode,
            )

            with bound_contextvars(
                dry_run=dry_run,
                ratio_limit_current=torrent.ratio_limit,
                ratio_limit_target=target_ratio,
                ratio_limit_source=limit_source,
                trackers=[tracker.name for tracker in trackers],
                seeding_time_limit_current=torrent.seeding_time_limit,
                seeding_time_limit_target=target_seed_time,
                seeding_time_limit_source=limit_source,
                inactive_seeding_time_limit_current=torrent.inactive_seeding_time_limit,
                inactive_seeding_time_limit_target=target_inactive_seed_time,
                share_limit_action_current=torrent.share_limit_action,
                share_limit_action_target=target_action,
                share_limits_mode_current=current_mode,
                share_limits_mode_target=target_mode,
            ):
                if current_state == target_state:
                    log.debug("torrent share limits already match")
                    continue

                log.info("torrent share limits need reconciliation")

                if not dry_run:
                    share_limits = dict(
                        ratio_limit=str(target_ratio),
                        seeding_time_limit=target_seed_time,
                        inactive_seeding_time_limit=target_inactive_seed_time,
                        share_limit_action=target_action,
                    )
                    if supports_share_limits_mode:
                        share_limits["share_limits_mode"] = target_mode
                    torrent.set_share_limits(**share_limits)


def maintain_free_space(
    client: qbittorrentapi.Client,
    config: Config,
    dry_run: bool = False,
) -> None:
    log.info("maintaining free space...")

    cfg = config.maintain_free_space
    torrents = torrents_in_categories(client, cfg.categories, cfg.ignore_categories)
    free_space = client.sync_maindata().server_state.free_space_on_disk
    threshold = cfg.free_space_threshold_bytes

    if free_space >= threshold:
        with bound_contextvars(free_space_bytes=free_space, threshold_bytes=threshold):
            log.info("free space above threshold; nothing to do")
        return

    possible_to_remove: list[tuple[qbittorrentapi.TorrentDictionary, list[TrackerConfig], int]] = []

    for torrent in torrents:
        with bound_contextvars(**_torrent_context(torrent)):
            trackers = matched_trackers(config, torrent)
            if not trackers:
                continue
            delete_priority = min(tracker.delete_priority for tracker in trackers)
            with bound_contextvars(
                trackers=[tracker.name for tracker in trackers],
                delete_priority=delete_priority,
            ):
                log.debug("checking torrent removal eligibility")
                if not required_limits_met(torrent, trackers):
                    continue
            possible_to_remove.append((torrent, trackers, delete_priority))

    possible_to_remove.sort(key=lambda item: (item[2], item[0].num_complete), reverse=True)

    with bound_contextvars(eligible_torrent_count=len(possible_to_remove)):
        log.debug("identified torrents meeting removal criteria")

    while possible_to_remove and free_space < threshold:
        torrent, trackers, delete_priority = possible_to_remove.pop(0)

        with bound_contextvars(**_torrent_context(torrent)):
            if not removal_allowed(torrent.hash, cfg.arr_instances(torrent.category), config.arrs):
                continue

            with bound_contextvars(
                dry_run=dry_run,
                delete_files=True,
                trackers=[tracker.name for tracker in trackers],
                delete_priority=delete_priority,
                torrent_ratio=torrent.ratio,
                uploaded_bytes=torrent.uploaded,
                seeding_time_seconds=torrent.seeding_time,
                torrent_seeders=torrent.num_complete,
            ):
                log.info("torrent selected to reclaim free space")

                if not dry_run:
                    torrent.delete(delete_files=True)

            if dry_run:
                free_space += torrent.size
            else:
                free_space = client.sync_maindata().server_state.free_space_on_disk


def run(
    client: qbittorrentapi.Client,
    config: Config,
    unregistered_first_seen: dict[str, datetime.datetime],
    dry_run: bool = False,
    stopped_first_seen: dict[str, datetime.datetime] | None = None,
) -> None:
    log.info("starting run...")

    if stopped_first_seen is None:
        stopped_first_seen = {}

    if config.remove_unregistered.enabled:
        with bound_contextvars(job="remove_unregistered"):
            remove_unregistered(
                client,
                config.remove_unregistered,
                unregistered_first_seen,
                dry_run,
                arrs=config.arrs,
            )

    if config.set_seed_limits.enabled:
        with bound_contextvars(job="set_seed_limits"):
            set_seed_limits(client, config, dry_run)

    if config.remove_stopped.enabled:
        with bound_contextvars(job="remove_stopped"):
            remove_stopped(client, config, stopped_first_seen, dry_run)

    if config.maintain_free_space.enabled:
        with bound_contextvars(job="maintain_free_space"):
            maintain_free_space(client, config, dry_run)

    if config.cleanup_empty_dirs.enabled:
        with bound_contextvars(job="cleanup_empty_dirs"):
            cleanup_empty_dirs(client, config.cleanup_empty_dirs, dry_run)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "-c",
        "--config",
        type=pathlib.Path,
        default=pathlib.Path(os.environ.get("CONFIG_FILE", "config.yaml")),
        help="path to the YAML config file (default: config.yaml, or $CONFIG_FILE)",
    )
    parser.add_argument("-d", "--daemon", action="store_true")
    parser.add_argument(
        "-n",
        "--dry-run",
        action="store_true",
        help="log planned changes without modifying torrents or directories",
    )

    args = parser.parse_args()

    try:
        config = load_config(args.config)
    except FileNotFoundError:
        raise SystemExit(f"config file not found: {args.config}")
    except ValidationError as e:
        errors = "\n".join(
            f"{'.'.join(str(part) for part in error['loc']) or 'config'}: {error['msg']}"
            for error in e.errors(include_input=False, include_url=False)
        )
        raise SystemExit(f"invalid config file {args.config}:\n{errors}") from None

    setup_logging(config.logging)
    warn_category_overlaps(config)

    client = build_client(config.qbittorrent)

    if args.dry_run:
        log.info("dry-run mode: no torrents or directories will be modified")

    unregistered_first_seen: dict[str, datetime.datetime] = {}
    stopped_first_seen: dict[str, datetime.datetime] = {}

    if not args.daemon:
        run(
            client,
            config,
            unregistered_first_seen,
            dry_run=args.dry_run,
            stopped_first_seen=stopped_first_seen,
        )
        return

    with bound_contextvars(interval_seconds=config.interval_seconds):
        log.info("running in daemon mode")

    while True:
        try:
            run(
                client,
                config,
                unregistered_first_seen,
                dry_run=args.dry_run,
                stopped_first_seen=stopped_first_seen,
            )
        except Exception:
            log.exception("run failed")

        time.sleep(config.interval_seconds)


if __name__ == "__main__":
    main()
