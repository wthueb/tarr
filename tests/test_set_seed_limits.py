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


def make_config(set_seed_limits, *, trackers=None):
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
            },
            "set_seed_limits": set_seed_limits,
            "trackers": trackers
            if trackers is not None
            else [
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


@pytest.mark.parametrize("field", ["default_seed_time_minutes", "default_ratio"])
@pytest.mark.parametrize("value", [None, -1, 120])
def test_job_level_defaults_are_rejected(field, value):
    with pytest.raises(ValidationError) as error:
        make_config({field: value, "categories": [{"name": "cross"}]})

    assert any(
        item["loc"] == ("set_seed_limits", field) and item["type"] == "extra_forbidden"
        for item in error.value.errors()
    )


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


def test_tracker_limits_precede_category_defaults():
    torrent = make_torrent()
    config = make_config(
        {
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


def test_category_defaults_apply_without_matching_tracker():
    torrent = make_torrent(tracker_url="https://unknown.example/announce")
    config = make_config(
        {
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


@pytest.mark.parametrize("seed_time_default", [{}, {"default_seed_time_minutes": None}])
def test_category_defaults_apply_independently(seed_time_default):
    torrent = make_torrent(
        tracker_url="https://unknown.example/announce",
        seeding_time_limit=45,
    )
    config = make_config(
        {"categories": [{"name": "cross", "default_ratio": 0.5, **seed_time_default}]}
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
            "categories": [
                {"name": "cross", "ratio": None, "default_ratio": 4.0, "action": "Stop"}
            ],
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


def test_explicit_null_category_default_preserves_current_value():
    torrent = make_torrent(
        tracker_url="https://unknown.example/announce",
        ratio_limit=1.25,
    )
    config = make_config({"categories": [{"name": "cross", "default_ratio": None}]})

    set_seed_limits(FakeClient([torrent]), config)

    assert torrent.set_share_limits.call_args.kwargs["ratio_limit"] == "1.25"


@pytest.mark.parametrize("use_tracker_limits", [False, True])
def test_missing_limits_preserve_current_values(use_tracker_limits):
    torrent = make_torrent(
        tracker_url="https://unknown.example/announce"
        if use_tracker_limits
        else "https://tracker.example.com/announce",
        ratio_limit=1.25,
        seeding_time_limit=45,
    )
    config = make_config(
        {"categories": [{"name": "cross", "use_tracker_limits": use_tracker_limits}]}
    )

    set_seed_limits(FakeClient([torrent]), config)

    torrent.set_share_limits.assert_called_once_with(
        ratio_limit="1.25",
        seeding_time_limit=45,
        inactive_seeding_time_limit=-1,
        share_limit_action="Default",
        share_limits_mode="MatchAny",
    )
    category = config.set_seed_limits.categories[0]
    assert category.resolve_seed_time_minutes(None) == (None, "current")
    assert category.resolve_ratio(None) == (None, "current")


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


@pytest.mark.parametrize("limit", [-1, None, 500])
@pytest.mark.parametrize("use_tracker_limits", [False, True])
def test_forced_tracker_overrides_explicit_limits_and_tracker_toggle(limit, use_tracker_limits):
    torrent = make_torrent()
    config = make_config(
        {
            "categories": [
                {
                    "name": "cross",
                    "seed_time_minutes": limit,
                    "ratio": limit,
                    "use_tracker_limits": use_tracker_limits,
                    "force_use_tracker_limits": ["example"],
                    "action": "Stop",
                }
            ]
        }
    )

    set_seed_limits(FakeClient([torrent]), config)

    torrent.set_share_limits.assert_called_once_with(
        ratio_limit="2.0",
        seeding_time_limit=100,
        inactive_seeding_time_limit=-1,
        share_limit_action="Stop",
        share_limits_mode="MatchAny",
    )
    category = config.set_seed_limits.categories[0]
    assert category.needs_tracker_limits
    assert category.resolve_seed_time_minutes(config.trackers[0]) == (
        100,
        "forced_tracker:example",
    )
    assert category.resolve_ratio(config.trackers[0]) == (2.0, "forced_tracker:example")


@pytest.mark.parametrize(
    "tracker_url",
    [
        "https://tracker.example.com/announce",
        "https://unknown.example/announce",
    ],
)
def test_nonforced_or_unmatched_tracker_preserves_category_limits(tracker_url):
    torrent = make_torrent(tracker_url=tracker_url)
    config = make_config(
        {
            "categories": [
                {
                    "name": "cross",
                    "seed_time_minutes": -1,
                    "ratio": None,
                    "force_use_tracker_limits": ["other"],
                }
            ]
        },
        trackers=[
            {
                "name": "example",
                "hosts": ["tracker.example.com"],
                "seed_time_minutes": 100,
                "ratio": 2,
            },
            {"name": "other", "hosts": ["other.example.com"], "seed_time_minutes": 200, "ratio": 3},
        ],
    )

    set_seed_limits(FakeClient([torrent]), config)

    torrent.set_share_limits.assert_called_once_with(
        ratio_limit="-2",
        seeding_time_limit=-1,
        inactive_seeding_time_limit=-1,
        share_limit_action="Default",
        share_limits_mode="MatchAny",
    )


@pytest.mark.parametrize("use_tracker_limits", [False, True])
def test_unmatched_forced_tracker_keeps_normal_fallbacks(use_tracker_limits):
    torrent = make_torrent(tracker_url="https://unknown.example/announce")
    config = make_config(
        {
            "categories": [
                {
                    "name": "cross",
                    "force_use_tracker_limits": ["example"],
                    "use_tracker_limits": use_tracker_limits,
                    "default_seed_time_minutes": 200,
                },
            ],
        }
    )

    set_seed_limits(FakeClient([torrent]), config)

    assert torrent.set_share_limits.call_args.kwargs["seeding_time_limit"] == 200
    assert torrent.set_share_limits.call_args.kwargs["ratio_limit"] == "-2"


def test_empty_force_list_does_not_read_trackers_with_complete_category_limits():
    torrent = make_torrent()
    del torrent.trackers
    config = make_config(
        {
            "categories": [
                {
                    "name": "cross",
                    "seed_time_minutes": -1,
                    "ratio": -1,
                    "force_use_tracker_limits": [],
                }
            ]
        }
    )

    set_seed_limits(FakeClient([torrent]), config)

    assert not config.set_seed_limits.categories[0].needs_tracker_limits
    torrent.set_share_limits.assert_called_once()


def test_forced_tracker_keeps_first_matching_tracker_selection():
    torrent = make_torrent()
    torrent.trackers.append(SimpleNamespace(url="https://forced.example.com/announce"))
    config = make_config(
        {
            "categories": [
                {
                    "name": "cross",
                    "seed_time_minutes": -1,
                    "ratio": -1,
                    "force_use_tracker_limits": ["forced"],
                }
            ]
        },
        trackers=[
            {
                "name": "example",
                "hosts": ["tracker.example.com"],
                "seed_time_minutes": 100,
                "ratio": 2,
            },
            {
                "name": "forced",
                "hosts": ["forced.example.com"],
                "seed_time_minutes": 200,
                "ratio": 3,
            },
        ],
    )

    set_seed_limits(FakeClient([torrent]), config)

    assert torrent.set_share_limits.call_args.kwargs["seeding_time_limit"] == -1
    assert torrent.set_share_limits.call_args.kwargs["ratio_limit"] == "-1.0"


def test_forced_tracker_only_applies_to_the_configured_category():
    managed = make_torrent(category="cross")
    other = make_torrent(category="upload")
    unlisted = make_torrent(category="unlisted")
    config = make_config(
        {
            "categories": [
                {
                    "name": "cross",
                    "seed_time_minutes": -1,
                    "ratio": -1,
                    "force_use_tracker_limits": ["example"],
                },
                {"name": "upload", "seed_time_minutes": -1, "ratio": -1},
            ]
        }
    )

    set_seed_limits(FakeClient([managed, other, unlisted]), config)

    assert managed.set_share_limits.call_args.kwargs["seeding_time_limit"] == 100
    assert other.set_share_limits.call_args.kwargs["seeding_time_limit"] == -1
    unlisted.set_share_limits.assert_not_called()


def test_unknown_forced_tracker_rejected():
    with pytest.raises(ValidationError, match="unknown tracker"):
        make_config({"categories": [{"name": "cross", "force_use_tracker_limits": ["typo"]}]})


@pytest.mark.parametrize("names", [None, True, "example", [1]])
def test_invalid_force_tracker_list_rejected(names):
    with pytest.raises(ValidationError):
        make_config({"categories": [{"name": "cross", "force_use_tracker_limits": names}]})


@pytest.mark.parametrize("dry_run", [False, True])
def test_forced_tracker_preserves_dry_run_and_noop_reconciliation(dry_run):
    torrent = make_torrent(
        ratio_limit=2,
        seeding_time_limit=100,
        inactive_seeding_time_limit=-1,
        share_limit_action="Stop",
        share_limits_mode="MatchAny",
    )
    config = make_config(
        {
            "categories": [
                {
                    "name": "cross",
                    "seed_time_minutes": -1,
                    "ratio": -1,
                    "force_use_tracker_limits": ["example"],
                    "action": "Stop",
                }
            ]
        }
    )

    set_seed_limits(FakeClient([torrent]), config, dry_run=dry_run)
    torrent.set_share_limits.assert_not_called()
    torrent.seeding_time_limit = -1
    set_seed_limits(FakeClient([torrent]), config, dry_run=dry_run)
    if dry_run:
        torrent.set_share_limits.assert_not_called()
    else:
        torrent.set_share_limits.assert_called_once()
