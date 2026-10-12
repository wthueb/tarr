from __future__ import annotations

import math
from numbers import Real
from typing import Any

import structlog

from tarr.config import UNLIMITED, Config, SeedLimits, TrackerConfig

log = structlog.stdlib.get_logger("tarr")


def matched_trackers(config: Config, torrent: Any) -> list[TrackerConfig]:
    try:
        trackers = config.match_trackers(tracker.url for tracker in torrent.trackers)
    except Exception as error:
        log.warning("unable to resolve torrent trackers; skipping", error_type=type(error).__name__)
        return []
    if not trackers:
        log.debug("torrent has no configured tracker match; skipping")
    return trackers


def _finite_number(value: Any) -> bool:
    return isinstance(value, Real) and not isinstance(value, bool) and math.isfinite(value)


def limits_met(
    seeding_time: Any,
    ratio: Any,
    seed_time_minutes: Any,
    ratio_limit: Any,
) -> bool:
    if not (
        _finite_number(seeding_time)
        and seeding_time >= 0
        and _finite_number(ratio)
        and (ratio >= 0 or ratio == UNLIMITED)
    ):
        return False
    time_met = (
        _finite_number(seed_time_minutes)
        and seed_time_minutes >= 0
        and seeding_time >= seed_time_minutes * 60
    )
    ratio_met = (
        _finite_number(ratio_limit)
        and ratio_limit >= 0
        and (ratio == UNLIMITED or ratio >= ratio_limit)
    )
    return time_met or ratio_met


def required_limits_met(torrent: Any, trackers: list[TrackerConfig]) -> bool:
    if not trackers:
        return False
    for tracker in trackers:
        if not limits_met(
            getattr(torrent, "seeding_time", None),
            getattr(torrent, "ratio", None),
            tracker.required.seed_time_minutes,
            tracker.required.ratio,
        ):
            log.debug(
                "torrent does not meet required tracker policy",
                tracker=tracker.name,
                required_seed_time_minutes=tracker.required.seed_time_minutes,
                required_ratio=tracker.required.ratio,
                seeding_time_seconds=getattr(torrent, "seeding_time", None),
                torrent_ratio=getattr(torrent, "ratio", None),
            )
            return False
    return True


def desired_limits(trackers: list[TrackerConfig]) -> SeedLimits:
    def maximum(values: list[Any]) -> Any:
        return UNLIMITED if UNLIMITED in values else max(values)

    return SeedLimits(
        seed_time_minutes=maximum([tracker.desired.seed_time_minutes for tracker in trackers]),
        ratio=maximum([tracker.desired.ratio for tracker in trackers]),
    )
