import datetime
from types import SimpleNamespace
from typing import cast
from unittest.mock import Mock

import pytest
import qbittorrentapi
from pydantic import ValidationError
from structlog.contextvars import get_contextvars

from tarr.config import Config, RemoveStoppedConfig, SetSeedLimitsCategoryConfig
from tarr.main import remove_stopped, run


class FakeClient:
    def __init__(self, torrents):
        self.torrents = torrents
        self.status_filters = []

    def torrents_info(self, status_filter=None):
        self.status_filters.append(status_filter)
        return self.torrents


def make_config(**remove_stopped):
    return RemoveStoppedConfig.model_validate(remove_stopped)


def make_run_config(*, seed_time_minutes=1, ratio=1, **remove_stopped):
    return Config.model_validate(
        {
            "qbittorrent": {
                "host": "localhost",
                "username": "user",
                "password": "pass",
                "remove_stopped": remove_stopped,
            },
            "trackers": [
                {
                    "name": "example",
                    "hosts": ["tracker.example.com"],
                    "seed_time_minutes": seed_time_minutes,
                    "ratio": ratio,
                }
            ],
        }
    )


def make_torrent(
    category="movies",
    state="stoppedUP",
    *,
    seeding_time=60,
    ratio=1,
    max_seeding_time=1,
    max_ratio=1,
    tracker_url="https://tracker.example.com/announce",
):
    return SimpleNamespace(
        hash="1234567890abcdef",
        name="completed torrent",
        state=state,
        size=1024**3,
        category=category,
        seeding_time=seeding_time,
        ratio=ratio,
        max_seeding_time=max_seeding_time,
        max_ratio=max_ratio,
        trackers=[SimpleNamespace(url=tracker_url)],
        delete=Mock(),
    )


def test_first_observation_starts_delay_without_removing():
    torrent = make_torrent()
    client = FakeClient([torrent])
    first_seen = {}

    remove_stopped(client, make_config(delay_minutes=10), first_seen)

    assert client.status_filters == ["completed"]
    assert torrent.hash in first_seen
    torrent.delete.assert_not_called()


def test_zero_delay_removes_on_first_observation():
    torrent = make_torrent()
    client = FakeClient([torrent])
    first_seen = {}

    remove_stopped(client, make_config(), first_seen)

    torrent.delete.assert_called_once_with(delete_files=False)
    assert torrent.hash not in first_seen


@pytest.mark.parametrize(
    "state", ["uploading", "stalledUP", "forcedUP", "queuedUP", "pausedDL", "stoppedDL"]
)
def test_seeding_torrent_is_not_tracked_or_removed(state):
    torrent = make_torrent(state=state)
    client = FakeClient([torrent])
    first_seen = {}

    remove_stopped(client, make_config(), first_seen)

    assert first_seen == {}
    torrent.delete.assert_not_called()


def test_resumed_seeding_torrent_resets_stopped_delay():
    torrent = make_torrent(state="uploading")
    client = FakeClient([torrent])
    first_seen = {torrent.hash: datetime.datetime.now() - datetime.timedelta(minutes=11)}

    remove_stopped(client, make_config(delay_minutes=10), first_seen)

    assert first_seen == {}
    torrent.delete.assert_not_called()


@pytest.mark.parametrize("state", ["pausedUP", "stoppedUP"])
def test_qbittorrent_stopped_states_are_removed(state):
    torrent = make_torrent(state=state)
    client = FakeClient([torrent])
    first_seen = {}

    remove_stopped(client, make_config(), first_seen)

    torrent.delete.assert_called_once_with(delete_files=False)


@pytest.mark.parametrize(
    ("seed_time_minutes", "target_ratio", "seeding_time", "ratio", "removed"),
    [
        (10, 2, 599, 1.99, False),
        (10, 2, 600, 0, True),
        (10, 2, 0, 2, True),
        (10, 2, 601, 2.01, True),
        (-1, 2, 100000, 1.99, False),
        (-1, 2, 0, 2, True),
        (10, -1, 599, 100, False),
        (10, -1, 600, 0, True),
        (-1, -1, 100000, 100, False),
        (0, -1, 0, 0, True),
        (-1, 0, 0, 0, True),
        (-2, -2, 100000, 100, False),
        (10, 2, 0, -1, True),
        (-1, -1, 0, -1, False),
    ],
)
def test_stopped_removal_requires_either_active_torrent_target(
    seed_time_minutes, target_ratio, seeding_time, ratio, removed
):
    torrent = make_torrent(
        seeding_time=seeding_time,
        ratio=ratio,
        max_seeding_time=seed_time_minutes,
        max_ratio=target_ratio,
    )
    client = FakeClient([torrent])
    observed_at = datetime.datetime.now() - datetime.timedelta(minutes=11)
    first_seen = {torrent.hash: observed_at}
    config = make_config(delay_minutes=10)

    remove_stopped(client, config, first_seen)

    if removed:
        torrent.delete.assert_called_once_with(delete_files=False)
        assert first_seen == {}
    else:
        torrent.delete.assert_not_called()
        assert first_seen == {torrent.hash: observed_at}


def test_stopped_torrent_without_matching_tracker_is_removed_when_active_target_is_met():
    torrent = make_torrent(tracker_url="https://unknown.example/announce")

    remove_stopped(FakeClient([torrent]), make_config(), {})

    torrent.delete.assert_called_once_with(delete_files=False)


def test_stopped_removal_does_not_access_trackers():
    torrent = make_torrent()
    del torrent.trackers

    remove_stopped(FakeClient([torrent]), make_config(), {})

    torrent.delete.assert_called_once_with(delete_files=False)


@pytest.mark.parametrize(
    ("active_time_limit", "active_ratio_limit", "removed"),
    [(10, 2, False), (1, 1, True)],
)
def test_run_uses_active_limits_instead_of_tracker_targets(
    active_time_limit, active_ratio_limit, removed
):
    torrent = make_torrent(max_seeding_time=active_time_limit, max_ratio=active_ratio_limit)
    config = make_run_config(
        enabled=True,
        seed_time_minutes=1 if not removed else 10,
        ratio=1 if not removed else 2,
    )

    run(cast(qbittorrentapi.Client, FakeClient([torrent])), config, {})

    if removed:
        torrent.delete.assert_called_once_with(delete_files=False)
    else:
        torrent.delete.assert_not_called()


@pytest.mark.parametrize(
    ("effective_time_limit", "effective_ratio_limit", "removed"),
    [(10, 2, False), (1, 1, True), (-1, -1, False)],
)
def test_stopped_removal_uses_resolved_limits_when_torrent_inherits_defaults(
    effective_time_limit, effective_ratio_limit, removed
):
    torrent = make_torrent(max_seeding_time=effective_time_limit, max_ratio=effective_ratio_limit)
    torrent.seeding_time_limit = -2
    torrent.ratio_limit = -2

    remove_stopped(FakeClient([torrent]), make_config(), {})

    if removed:
        torrent.delete.assert_called_once_with(delete_files=False)
    else:
        torrent.delete.assert_not_called()


def test_stopped_torrent_becomes_eligible_when_target_is_reached():
    torrent = make_torrent(seeding_time=59, ratio=0)
    client = FakeClient([torrent])
    first_seen = {torrent.hash: datetime.datetime.now() - datetime.timedelta(minutes=11)}
    config = make_config(delay_minutes=10)

    remove_stopped(client, config, first_seen)
    torrent.delete.assert_not_called()

    torrent.ratio = 1
    remove_stopped(client, config, first_seen)

    torrent.delete.assert_called_once_with(delete_files=False)
    assert first_seen == {}


@pytest.mark.parametrize(
    ("on_delete", "delete_files"),
    [("Remove", False), ("RemoveWithContent", True)],
)
def test_removes_after_delay_with_configured_delete_action(on_delete, delete_files):
    torrent = make_torrent()
    client = FakeClient([torrent])
    first_seen = {torrent.hash: datetime.datetime.now() - datetime.timedelta(minutes=11)}
    config = make_config(delay_minutes=10, on_delete=on_delete)

    remove_stopped(client, config, first_seen)

    torrent.delete.assert_called_once_with(delete_files=delete_files)
    assert torrent.hash not in first_seen


def test_dry_run_does_not_remove_torrent_or_tracking_state():
    torrent = make_torrent()
    client = FakeClient([torrent])
    observed_at = datetime.datetime.now() - datetime.timedelta(minutes=11)
    first_seen = {torrent.hash: observed_at}

    remove_stopped(
        client,
        make_config(delay_minutes=10, on_delete="RemoveWithContent"),
        first_seen,
        dry_run=True,
    )

    torrent.delete.assert_not_called()
    assert first_seen == {torrent.hash: observed_at}


def test_category_filters_apply_before_tracking():
    torrent = make_torrent(category="upload")
    client = FakeClient([torrent])
    first_seen = {}
    config = make_config(categories=["movies"], ignore_categories=["upload"])

    remove_stopped(client, config, first_seen)

    assert first_seen == {}
    torrent.delete.assert_not_called()


def test_torrent_no_longer_stopped_is_removed_from_tracking():
    client = FakeClient([])
    first_seen = {"1234567890abcdef": datetime.datetime.now()}

    remove_stopped(client, make_config(delay_minutes=10), first_seen)

    assert first_seen == {}


@pytest.mark.parametrize("on_delete", ["Default", "Stop", "EnableSuperSeeding"])
def test_on_delete_rejects_unsupported_actions(on_delete):
    with pytest.raises(ValidationError):
        RemoveStoppedConfig(on_delete=on_delete)


@pytest.mark.parametrize(
    ("initial_limit", "applied_limit", "removed"),
    [(1, 10, False), (10, 1, True)],
)
def test_run_applies_seed_limits_before_checking_stopped_removal(
    initial_limit, applied_limit, removed
):
    torrent = make_torrent(max_seeding_time=initial_limit, max_ratio=initial_limit)
    torrent.seeding_time_limit = initial_limit
    torrent.ratio_limit = initial_limit
    torrent.inactive_seeding_time_limit = -1
    torrent.share_limit_action = "Stop"
    torrent.share_limits_mode = "MatchAny"
    operations = []

    def apply_limits(**limits):
        operations.append("set_seed_limits")
        torrent.max_seeding_time = limits["seeding_time_limit"]
        torrent.max_ratio = float(limits["ratio_limit"])
        torrent.seeding_time_limit = torrent.max_seeding_time
        torrent.ratio_limit = torrent.max_ratio

    torrent.set_share_limits = Mock(side_effect=apply_limits)
    torrent.delete.side_effect = lambda **_kwargs: operations.append("remove_stopped")
    client = FakeClient([torrent])
    config = make_run_config(enabled=True, seed_time_minutes=applied_limit, ratio=applied_limit)
    config.qbittorrent.set_seed_limits.enabled = True
    config.qbittorrent.set_seed_limits.categories = [
        SetSeedLimitsCategoryConfig(name="movies", action="Stop")
    ]

    run(cast(qbittorrentapi.Client, client), config, {})

    assert client.status_filters == [None, "completed"]
    torrent.set_share_limits.assert_called_once()
    if removed:
        torrent.delete.assert_called_once_with(delete_files=False)
        assert operations == ["set_seed_limits", "remove_stopped"]
    else:
        torrent.delete.assert_not_called()
        assert operations == ["set_seed_limits"]


def test_run_binds_job_and_torrent_context_during_removal():
    torrent = make_torrent()
    observed_context = {}
    torrent.delete.side_effect = lambda **_kwargs: observed_context.update(get_contextvars())
    client = FakeClient([torrent])
    config = make_run_config(enabled=True)

    run(cast(qbittorrentapi.Client, client), config, {})

    assert observed_context == {
        "delete_files": False,
        "dry_run": False,
        "job": "remove_stopped",
        "on_delete": "Remove",
        "stopped_for_seconds": 0.0,
        "torrent": torrent.hash,
        "torrent_name": torrent.name,
        "torrent_size_bytes": torrent.size,
        "torrent_state": torrent.state,
    }
    assert get_contextvars() == {}
