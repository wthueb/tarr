from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from pydantic import ValidationError
from structlog.contextvars import merge_contextvars
from structlog.testing import capture_logs

from tarr.config import Config, SetSeedLimitsConfig
from tarr.main import set_seed_limits


class FakeClient:
    def __init__(self, torrents):
        self.torrents = torrents

    def torrents_info(self, status_filter=None):
        return self.torrents


def make_tracker(name="example", *, required=(100, 2), desired=(200, 3)):
    return {
        "name": name,
        "hosts": [f"tracker.{name}.com"],
        "required": {"seed_time_minutes": required[0], "ratio": required[1]},
        "desired": {"seed_time_minutes": desired[0], "ratio": desired[1]},
    }


def make_config(set_seed_limits, *, trackers=None):
    return Config.model_validate(
        {
            "qbittorrent": {"host": "localhost", "username": "user", "password": "pass"},
            "set_seed_limits": {
                **set_seed_limits,
                "categories": [
                    {"action": "Default", **category}
                    for category in set_seed_limits.get("categories", [])
                ],
            },
            "trackers": trackers if trackers is not None else [make_tracker()],
        }
    )


def make_torrent(
    *,
    category="cross",
    tracker_url="https://tracker.example.com/announce",
    ratio_limit=-2,
    seeding_time_limit=-2,
    inactive_seeding_time_limit=-2,
    share_limit_action="Default",
    share_limits_mode="Default",
):
    return SimpleNamespace(
        hash="1234567890abcdef",
        name="test torrent",
        state="uploading",
        size=1024,
        category=category,
        trackers=[SimpleNamespace(url=tracker_url)],
        ratio_limit=ratio_limit,
        seeding_time_limit=seeding_time_limit,
        inactive_seeding_time_limit=inactive_seeding_time_limit,
        share_limit_action=share_limit_action,
        share_limits_mode=share_limits_mode,
        set_share_limits=Mock(),
    )


@pytest.mark.parametrize(
    "action", ["Default", "Stop", "Remove", "RemoveWithContent", "EnableSuperSeeding"]
)
def test_category_action_and_tracker_desired_limits_are_applied(action):
    torrent = make_torrent()
    config = make_config({"categories": [{"name": "cross", "action": action}]})

    set_seed_limits(FakeClient([torrent]), config)

    torrent.set_share_limits.assert_called_once_with(
        ratio_limit="3.0",
        seeding_time_limit=200,
        inactive_seeding_time_limit=-1,
        share_limit_action=action,
        share_limits_mode="MatchAny",
    )


@pytest.mark.parametrize(
    "raw",
    [
        {"categories": ["cross"]},
        {"ignore_categories": []},
        {"on_delete": "Remove"},
        {"categories": [{"name": "cross", "action": None}]},
        {"categories": [{"name": "cross"}]},
        {"action": "Stop"},
        {"categories": [{"name": "cross", "action": "Stop"}] * 2},
        {"categories": [{"name": None, "action": "Stop"}, {"name": "", "action": "Stop"}]},
    ],
)
def test_invalid_set_seed_limits_configuration_is_rejected(raw):
    with pytest.raises(ValidationError):
        SetSeedLimitsConfig.model_validate(raw)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("seed_time_minutes", 100),
        ("ratio", 2),
        ("use_tracker_limits", False),
        ("force_use_tracker_limits", ["example"]),
        ("default_seed_time_minutes", 100),
        ("default_ratio", 2),
    ],
)
def test_legacy_category_fields_are_rejected(field, value):
    with pytest.raises(ValidationError, match="Extra inputs"):
        make_config({"categories": [{"name": "cross", field: value}]})


@pytest.mark.parametrize("field", ["default_seed_time_minutes", "default_ratio"])
@pytest.mark.parametrize("value", [None, -1, 120])
def test_job_level_defaults_are_rejected(field, value):
    with pytest.raises(ValidationError):
        make_config({field: value, "categories": [{"name": "cross"}]})


@pytest.mark.parametrize(
    "override",
    [
        {},
        {"seed_time_minutes": 200},
        {"ratio": 3},
        {"seed_time_minutes": None, "ratio": 3},
        {"seed_time_minutes": -2, "ratio": 3},
        {"seed_time_minutes": 200, "ratio": -2},
        {"seed_time_minutes": 200, "ratio": None},
        {"seed_time_minutes": 200, "ratio": float("inf")},
    ],
)
def test_override_requires_two_valid_limits(override):
    with pytest.raises(ValidationError):
        make_config({"categories": [{"name": "cross", "override": override}]})


@pytest.mark.parametrize("override", [(100, 2), (400, 5), (-1, -1)])
def test_complete_category_override_precedes_desired_limits(override):
    torrent = make_torrent()
    config = make_config(
        {
            "categories": [
                {
                    "name": "cross",
                    "action": "RemoveWithContent",
                    "override": {"seed_time_minutes": override[0], "ratio": override[1]},
                }
            ]
        }
    )

    set_seed_limits(FakeClient([torrent]), config)

    torrent.set_share_limits.assert_called_once_with(
        ratio_limit=str(float(override[1])),
        seeding_time_limit=override[0],
        inactive_seeding_time_limit=-1,
        share_limit_action="RemoveWithContent",
        share_limits_mode="MatchAny",
    )


@pytest.mark.parametrize("override", [None, {"seed_time_minutes": -1, "ratio": -1}])
def test_unknown_tracker_skips_reconciliation_even_with_override(override):
    torrent = make_torrent(tracker_url="https://unknown.example/announce")
    config = make_config({"categories": [{"name": "cross", "override": override}]})

    set_seed_limits(FakeClient([torrent]), config)

    torrent.set_share_limits.assert_not_called()


@pytest.mark.parametrize("failure", ["missing", "raises", "invalid_url"])
def test_tracker_resolution_failure_skips_reconciliation(failure):
    torrent = make_torrent()
    if failure == "missing":
        del torrent.trackers
    elif failure == "raises":
        torrent.trackers = Mock()
    else:
        torrent.trackers[0].url = "http://[invalid"
    config = make_config(
        {"categories": [{"name": "cross", "override": {"seed_time_minutes": -1, "ratio": -1}}]}
    )

    set_seed_limits(FakeClient([torrent]), config)

    torrent.set_share_limits.assert_not_called()


@pytest.mark.parametrize(
    ("first", "second", "expected"),
    [((300, 2), (100, 4), (300, 4)), ((-1, 2), (100, -1), (-1, -1))],
)
@pytest.mark.parametrize("reverse", [False, True])
def test_multiple_trackers_use_most_protective_desired_limits(first, second, expected, reverse):
    torrent = make_torrent()
    torrent.trackers.extend(
        [
            SimpleNamespace(url="https://tracker.second.com/announce"),
            SimpleNamespace(url="https://unknown.example/announce"),
            SimpleNamespace(url="https://tracker.example.com/another"),
            SimpleNamespace(url="DHT"),
        ]
    )
    trackers = [
        make_tracker(required=(0, 0), desired=first),
        make_tracker("second", required=(0, 0), desired=second),
    ]
    if reverse:
        trackers.reverse()
        torrent.trackers.reverse()
    config = make_config({"categories": [{"name": "cross"}]}, trackers=trackers)

    with capture_logs(processors=[merge_contextvars]) as logs:
        set_seed_limits(FakeClient([torrent]), config)

    assert torrent.set_share_limits.call_args.kwargs["seeding_time_limit"] == expected[0]
    assert torrent.set_share_limits.call_args.kwargs["ratio_limit"] == str(float(expected[1]))
    reconciliation = next(
        log for log in logs if log["event"] == "torrent share limits need reconciliation"
    )
    assert reconciliation["trackers"] == [tracker["name"] for tracker in trackers]


@pytest.mark.parametrize("categories", [[], [{"name": "other"}]])
def test_empty_or_unlisted_categories_are_not_managed(categories):
    torrent = make_torrent()
    config = make_config({"categories": categories})

    set_seed_limits(FakeClient([torrent]), config)

    torrent.set_share_limits.assert_not_called()


@pytest.mark.parametrize("name", [None, ""])
def test_uncategorized_category_is_normalized(name):
    torrent = make_torrent(category="")
    config = make_config({"categories": [{"name": name}]})

    set_seed_limits(FakeClient([torrent]), config)

    torrent.set_share_limits.assert_called_once()


@pytest.mark.parametrize("supports_mode", [False, True])
@pytest.mark.parametrize("dry_run", [False, True])
def test_matching_state_is_noop_and_drift_respects_dry_run(supports_mode, dry_run):
    torrent = make_torrent(
        ratio_limit=3,
        seeding_time_limit=200,
        inactive_seeding_time_limit=-1,
        share_limit_action="Default",
        share_limits_mode="MatchAny",
    )
    if not supports_mode:
        del torrent.share_limits_mode
    config = make_config({"categories": [{"name": "cross"}]})

    set_seed_limits(FakeClient([torrent]), config, dry_run=dry_run)
    torrent.set_share_limits.assert_not_called()
    torrent.seeding_time_limit = 100
    set_seed_limits(FakeClient([torrent]), config, dry_run=dry_run)

    if dry_run:
        torrent.set_share_limits.assert_not_called()
    else:
        kwargs = dict(
            ratio_limit="3.0",
            seeding_time_limit=200,
            inactive_seeding_time_limit=-1,
            share_limit_action="Default",
        )
        if supports_mode:
            kwargs["share_limits_mode"] = "MatchAny"
        torrent.set_share_limits.assert_called_once_with(**kwargs)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("inactive_seeding_time_limit", -2),
        ("share_limit_action", "Stop"),
        ("share_limits_mode", "MatchAll"),
    ],
)
def test_non_limit_drift_triggers_reconciliation(field, value):
    torrent = make_torrent(
        ratio_limit=3,
        seeding_time_limit=200,
        inactive_seeding_time_limit=-1,
        share_limit_action="Default",
        share_limits_mode="MatchAny",
    )
    setattr(torrent, field, value)
    config = make_config({"categories": [{"name": "cross"}]})

    set_seed_limits(FakeClient([torrent]), config)

    torrent.set_share_limits.assert_called_once()
