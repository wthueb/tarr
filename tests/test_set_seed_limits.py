from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from pydantic import ValidationError

from tarr.config import Config, SetSeedLimitsConfig
from tarr.main import set_seed_limits


class FakeClient:
    def __init__(self, torrents):
        self.torrents = torrents

    def torrents_info(self, status_filter=None):
        return self.torrents


def make_config(set_seed_limits):
    set_seed_limits = {
        **set_seed_limits,
        "categories": [
            {"action": "Default", **category} for category in set_seed_limits.get("categories", [])
        ],
    }
    return Config.model_validate(
        {
            "qbittorrent": {
                "host": "localhost",
                "username": "user",
                "password": "pass",
                "set_seed_limits": set_seed_limits,
            },
            "trackers": [
                {
                    "name": "example",
                    "hosts": ["tracker.example.com"],
                    "seed_time_minutes": 100,
                    "ratio": 2.0,
                }
            ],
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
    "action",
    ["Default", "Stop", "Remove", "RemoveWithContent", "EnableSuperSeeding"],
)
def test_all_share_limit_actions_are_valid_per_category(action):
    config = SetSeedLimitsConfig.model_validate(
        {"categories": [{"name": "cross", "action": action}]}
    )
    assert config.categories[0].action == action


@pytest.mark.parametrize(
    "set_seed_limits",
    [
        {"categories": ["cross"]},
        {"ignore_categories": []},
        {"on_delete": "Remove"},
        {"categories": [{"name": "cross", "action": None}]},
        {"categories": [{"name": "cross", "action": "Stop", "ratio": -2}]},
        {"categories": [{"name": "cross", "action": "Stop", "seed_time_minutes": -2}]},
        {"categories": [{"name": "cross", "action": "Stop"}] * 2},
        {"categories": [{"name": None, "action": "Stop"}, {"name": "", "action": "Stop"}]},
        {"categories": [{"name": "cross"}]},
        {"action": "Stop", "categories": [{"name": "cross", "action": "Stop"}]},
    ],
    ids=[
        "legacy-category-list",
        "legacy-ignore-categories",
        "legacy-on-delete",
        "null-action",
        "global-ratio-sentinel",
        "global-time-sentinel",
        "duplicate-category",
        "duplicate-uncategorized-category",
        "missing-category-action",
        "global-action",
    ],
)
def test_invalid_set_seed_limits_configuration_is_rejected(set_seed_limits):
    with pytest.raises(ValidationError):
        SetSeedLimitsConfig.model_validate(set_seed_limits)


def test_trackers_cannot_configure_share_limit_actions():
    with pytest.raises(ValidationError):
        Config.model_validate(
            {
                "qbittorrent": {
                    "host": "localhost",
                    "username": "user",
                    "password": "pass",
                },
                "trackers": [
                    {
                        "name": "invalid",
                        "hosts": ["tracker.example.com"],
                        "seed_time_minutes": 100,
                        "ratio": 2.0,
                        "action": "Stop",
                    }
                ],
            }
        )


def test_category_limits_override_matching_tracker_limits():
    torrent = make_torrent()
    config = make_config(
        {
            "categories": [
                {
                    "name": "cross",
                    "seed_time_minutes": -1,
                    "ratio": -1,
                    "action": "RemoveWithContent",
                }
            ],
        }
    )

    set_seed_limits(FakeClient([torrent]), config)

    torrent.set_share_limits.assert_called_once_with(
        ratio_limit="-1.0",
        seeding_time_limit=-1,
        inactive_seeding_time_limit=-1,
        share_limit_action="RemoveWithContent",
        share_limits_mode="MatchAny",
    )


def test_complete_category_limits_do_not_access_torrent_trackers():
    torrent = make_torrent()
    del torrent.trackers
    config = make_config({"categories": [{"name": "cross", "seed_time_minutes": -1, "ratio": -1}]})

    set_seed_limits(FakeClient([torrent]), config)

    torrent.set_share_limits.assert_called_once()


def test_tracker_limits_precede_category_and_global_defaults():
    torrent = make_torrent()
    config = make_config(
        {
            "default_seed_time_minutes": 300,
            "default_ratio": 4.0,
            "categories": [
                {
                    "name": "cross",
                    "default_seed_time_minutes": 200,
                    "default_ratio": 3.0,
                }
            ],
        }
    )

    set_seed_limits(FakeClient([torrent]), config)

    torrent.set_share_limits.assert_called_once_with(
        ratio_limit="2.0",
        seeding_time_limit=100,
        inactive_seeding_time_limit=-1,
        share_limit_action="Default",
        share_limits_mode="MatchAny",
    )


def test_category_defaults_precede_global_defaults_without_matching_tracker():
    torrent = make_torrent(tracker_url="https://unknown.example/announce")
    config = make_config(
        {
            "default_seed_time_minutes": 300,
            "default_ratio": 4.0,
            "categories": [
                {
                    "name": "cross",
                    "default_seed_time_minutes": 200,
                    "default_ratio": 3.0,
                }
            ],
        }
    )

    set_seed_limits(FakeClient([torrent]), config)

    torrent.set_share_limits.assert_called_once_with(
        ratio_limit="3.0",
        seeding_time_limit=200,
        inactive_seeding_time_limit=-1,
        share_limit_action="Default",
        share_limits_mode="MatchAny",
    )


def test_global_defaults_apply_independently():
    torrent = make_torrent(
        tracker_url="https://unknown.example/announce",
        seeding_time_limit=45,
    )
    config = make_config(
        {
            "default_seed_time_minutes": None,
            "default_ratio": 0.5,
            "categories": [{"name": "cross"}],
        }
    )

    set_seed_limits(FakeClient([torrent]), config)

    torrent.set_share_limits.assert_called_once_with(
        ratio_limit="0.5",
        seeding_time_limit=45,
        inactive_seeding_time_limit=-1,
        share_limit_action="Default",
        share_limits_mode="MatchAny",
    )


def test_explicit_null_stops_resolution_and_preserves_current_value():
    torrent = make_torrent(ratio_limit=1.25)
    config = make_config(
        {
            "default_ratio": 4.0,
            "categories": [{"name": "cross", "ratio": None, "action": "Stop"}],
        }
    )

    set_seed_limits(FakeClient([torrent]), config)

    torrent.set_share_limits.assert_called_once_with(
        ratio_limit="1.25",
        seeding_time_limit=100,
        inactive_seeding_time_limit=-1,
        share_limit_action="Stop",
        share_limits_mode="MatchAny",
    )


def test_explicit_null_category_default_stops_before_global_default():
    torrent = make_torrent(
        tracker_url="https://unknown.example/announce",
        ratio_limit=1.25,
    )
    config = make_config(
        {
            "default_ratio": 4.0,
            "categories": [{"name": "cross", "default_ratio": None}],
        }
    )

    set_seed_limits(FakeClient([torrent]), config)

    assert torrent.set_share_limits.call_args.kwargs["ratio_limit"] == "1.25"


def test_disabling_tracker_limits_uses_category_defaults():
    torrent = make_torrent()
    config = make_config(
        {
            "categories": [
                {
                    "name": "cross",
                    "use_tracker_limits": False,
                    "default_seed_time_minutes": 200,
                    "default_ratio": 3.0,
                }
            ]
        }
    )

    set_seed_limits(FakeClient([torrent]), config)

    assert torrent.set_share_limits.call_args.kwargs["ratio_limit"] == "3.0"
    assert torrent.set_share_limits.call_args.kwargs["seeding_time_limit"] == 200


def test_category_action_is_applied():
    torrent = make_torrent()
    config = make_config(
        {
            "categories": [{"name": "cross", "action": "EnableSuperSeeding"}],
        }
    )

    set_seed_limits(FakeClient([torrent]), config)

    assert torrent.set_share_limits.call_args.kwargs["share_limit_action"] == "EnableSuperSeeding"


@pytest.mark.parametrize("categories", [[], [{"name": "other"}]])
def test_empty_or_unlisted_categories_are_not_managed(categories):
    torrent = make_torrent()
    config = make_config({"categories": categories})

    set_seed_limits(FakeClient([torrent]), config)

    torrent.set_share_limits.assert_not_called()


def test_null_category_name_manages_uncategorized_torrents():
    torrent = make_torrent(category="")
    config = make_config({"categories": [{"name": None, "seed_time_minutes": -1, "ratio": -1}]})

    set_seed_limits(FakeClient([torrent]), config)

    torrent.set_share_limits.assert_called_once()


def test_matching_complete_state_does_not_make_redundant_call():
    torrent = make_torrent(
        ratio_limit=2.0,
        seeding_time_limit=100,
        inactive_seeding_time_limit=-1,
        share_limit_action="Default",
        share_limits_mode="MatchAny",
    )
    config = make_config({"categories": [{"name": "cross"}]})

    set_seed_limits(FakeClient([torrent]), config)

    torrent.set_share_limits.assert_not_called()


def test_qbittorrent_before_5_3_uses_implicit_match_any_mode():
    torrent = make_torrent(
        ratio_limit=2.0,
        seeding_time_limit=100,
        inactive_seeding_time_limit=-1,
        share_limit_action="Stop",
    )
    del torrent.share_limits_mode
    config = make_config({"categories": [{"name": "cross"}]})

    set_seed_limits(FakeClient([torrent]), config)

    torrent.set_share_limits.assert_called_once_with(
        ratio_limit="2.0",
        seeding_time_limit=100,
        inactive_seeding_time_limit=-1,
        share_limit_action="Default",
    )


def test_qbittorrent_before_5_3_matching_state_does_not_make_redundant_call():
    torrent = make_torrent(
        ratio_limit=2.0,
        seeding_time_limit=100,
        inactive_seeding_time_limit=-1,
        share_limit_action="Default",
    )
    del torrent.share_limits_mode
    config = make_config({"categories": [{"name": "cross"}]})

    set_seed_limits(FakeClient([torrent]), config)

    torrent.set_share_limits.assert_not_called()


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
        ratio_limit=2.0,
        seeding_time_limit=100,
        inactive_seeding_time_limit=-1,
        share_limit_action="Default",
        share_limits_mode="MatchAny",
    )
    setattr(torrent, field, value)
    config = make_config({"categories": [{"name": "cross"}]})

    set_seed_limits(FakeClient([torrent]), config)

    torrent.set_share_limits.assert_called_once()


def test_dry_run_reports_drift_without_mutating():
    torrent = make_torrent()
    config = make_config({"categories": [{"name": "cross"}]})

    set_seed_limits(FakeClient([torrent]), config, dry_run=True)

    torrent.set_share_limits.assert_not_called()
