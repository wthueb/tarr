import datetime
from importlib import import_module
from types import SimpleNamespace
from typing import cast
from unittest.mock import Mock

import pytest
import qbittorrentapi
import yaml
from pydantic import ValidationError
from structlog.contextvars import merge_contextvars
from structlog.testing import capture_logs

from tarr.config import Config
from tarr.main import maintain_free_space, remove_stopped, remove_unregistered, set_seed_limits
from tarr.seed_limits import limits_met, required_limits_met

GIB = 1024**3


def tracker(name="first", *, required=(10, 2), desired=(20, 3), priority=0):
    return {
        "name": name,
        "hosts": [f"{name}.example"],
        "required": {"seed_time_minutes": required[0], "ratio": required[1]},
        "desired": {"seed_time_minutes": desired[0], "ratio": desired[1]},
        "delete_priority": priority,
    }


def raw_config(*trackers):
    return {
        "qbittorrent": {"host": "localhost", "username": "user", "password": "secret-password"},
        "trackers": list(trackers) or [tracker()],
        "remove_stopped": {"enabled": True, "on_delete": "RemoveWithContent"},
        "maintain_free_space": {"enabled": True, "free_space_threshold_gibi": 4},
    }


class FakeClient:
    def __init__(self, torrents):
        self.torrents = torrents

    def torrents_info(self, status_filter=None):
        return self.torrents

    def sync_maindata(self):
        return SimpleNamespace(server_state=SimpleNamespace(free_space_on_disk=0))


def torrent(name="torrent", *, hosts=("first",), seeding_time=600, ratio=2, seeders=10):
    return SimpleNamespace(
        hash=name,
        name=name,
        state="stoppedUP",
        size=GIB,
        category="cross",
        seeding_time=seeding_time,
        ratio=ratio,
        max_seeding_time=0,
        max_ratio=0,
        num_complete=seeders,
        uploaded=GIB,
        trackers=[
            SimpleNamespace(url=f"https://{host}.example/announce", status=2, msg="working")
            for host in hosts
        ],
        ratio_limit=-2,
        seeding_time_limit=-2,
        inactive_seeding_time_limit=-2,
        share_limit_action="Default",
        share_limits_mode="Default",
        set_share_limits=Mock(),
        delete=Mock(),
    )


def remove(job, client, config, first_seen=None, *, dry_run=False):
    if job == "remove_stopped":
        remove_stopped(client, config, first_seen if first_seen is not None else {}, dry_run)
    else:
        maintain_free_space(cast(qbittorrentapi.Client, client), config, dry_run)


@pytest.mark.parametrize("field", ["seed_time_minutes", "ratio"])
@pytest.mark.parametrize(
    ("required", "desired", "valid"),
    [
        (0, 0, True),
        (1, 1, True),
        (1, 2, True),
        (2, 1, False),
        (1, -1, True),
        (-1, -1, True),
        (-1, 1, False),
    ],
)
def test_desired_must_be_at_least_corresponding_required(field, required, desired, valid):
    policy = tracker(required=(0, 0), desired=(0, 0))
    policy["required"][field] = required
    policy["desired"][field] = desired
    if valid:
        Config.model_validate(raw_config(policy))
    else:
        with pytest.raises(ValidationError, match=rf"first.*desired\.{field}.*required\.{field}"):
            Config.model_validate(raw_config(policy))


@pytest.mark.parametrize("block", ["required", "desired"])
@pytest.mark.parametrize("field", ["seed_time_minutes", "ratio"])
@pytest.mark.parametrize("value", [-2, None, float("inf"), float("nan")])
def test_invalid_policy_limits_are_rejected(block, field, value):
    policy = tracker()
    policy[block][field] = value
    with pytest.raises(ValidationError):
        Config.model_validate(raw_config(policy))


@pytest.mark.parametrize("block", ["required", "desired"])
@pytest.mark.parametrize("field", [None, "seed_time_minutes", "ratio"])
def test_tracker_policy_blocks_and_fields_are_required(block, field):
    policy = tracker()
    if field is None:
        del policy[block]
    else:
        del policy[block][field]
    with pytest.raises(ValidationError):
        Config.model_validate(raw_config(policy))


@pytest.mark.parametrize("field", ["seed_time_minutes", "ratio", "action"])
def test_legacy_tracker_fields_are_rejected(field):
    policy = tracker()
    policy[field] = "Stop" if field == "action" else 1
    with pytest.raises(ValidationError, match="Extra inputs"):
        Config.model_validate(raw_config(policy))


@pytest.mark.parametrize("field", ["seed_time_minutes", "ratio"])
@pytest.mark.parametrize("enabled", [False, True])
@pytest.mark.parametrize("action", ["Stop", "RemoveWithContent", "Default"])
def test_override_validated_against_every_tracker_even_if_disabled(field, enabled, action):
    raw = raw_config(tracker(), tracker("second", required=(100, 10), desired=(200, 20)))
    override = {"seed_time_minutes": 100, "ratio": 10}
    override[field] = 10 if field == "seed_time_minutes" else 2
    raw["set_seed_limits"] = {
        "enabled": enabled,
        "categories": [{"name": "cross", "action": action, "override": override}],
    }
    with pytest.raises(
        ValidationError, match=rf"cross.*override\.{field}.*second.*required\.{field}"
    ):
        Config.model_validate(raw)


@pytest.mark.parametrize(
    "override", [{"seed_time_minutes": 100, "ratio": 10}, {"seed_time_minutes": -1, "ratio": -1}]
)
def test_override_equal_to_or_above_all_required_policies_is_valid(override):
    raw = raw_config(tracker(), tracker("second", required=(100, 10), desired=(200, 20)))
    raw["set_seed_limits"] = {
        "categories": [{"name": "cross", "action": "Stop", "override": override}]
    }
    Config.model_validate(raw)


@pytest.mark.parametrize("invalid", ["desired", "override"])
@pytest.mark.parametrize("daemon", [False, True])
def test_startup_fails_before_connection_without_exposing_credentials(
    tmp_path, monkeypatch, invalid, daemon
):
    main_module = import_module("tarr.main")
    raw = raw_config()
    if invalid == "desired":
        raw["trackers"][0]["desired"]["ratio"] = 1
    else:
        raw["set_seed_limits"] = {
            "categories": [
                {
                    "name": "cross",
                    "action": "Stop",
                    "override": {"seed_time_minutes": 1, "ratio": 1},
                }
            ]
        }
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(raw))
    build = Mock()
    run = Mock()
    monkeypatch.setattr(main_module, "build_client", build)
    monkeypatch.setattr(main_module, "run", run)
    monkeypatch.setattr(
        "sys.argv", ["tarr", "--config", str(path), *(["--daemon"] if daemon else [])]
    )

    with pytest.raises(SystemExit) as error:
        main_module.main()

    assert "invalid config" in str(error.value)
    assert "ratio" in str(error.value) or "seed_time_minutes" in str(error.value)
    assert "secret-password" not in str(error.value)
    build.assert_not_called()
    run.assert_not_called()


def test_matching_is_case_insensitive_deduplicated_and_covers_subdomains():
    config = Config.model_validate(raw_config(tracker(), tracker("second")))
    matches = config.match_trackers(
        [
            "https://FIRST.EXAMPLE/a",
            "https://sub.first.example/b",
            "https://second.example/a",
            "https://unknown.example/a",
            "DHT",
        ]
    )
    assert [match.name for match in matches] == ["first", "second"]
    assert config.match_trackers(["https://notfirst.example/a"]) == []


@pytest.mark.parametrize("job", ["remove_stopped", "maintain_free_space"])
@pytest.mark.parametrize("dry_run", [False, True])
def test_every_matched_tracker_required_policy_blocks_removal(job, dry_run):
    raw = raw_config(
        tracker(priority=10000),
        tracker("second", required=(100, 10), desired=(200, 20), priority=10000),
    )
    raw["remove_stopped"]["delay_minutes"] = 15
    raw["set_seed_limits"] = {
        "categories": [
            {
                "name": "cross",
                "action": "RemoveWithContent",
                "override": {"seed_time_minutes": 100, "ratio": 10},
            }
        ]
    }
    config = Config.model_validate(raw)
    candidate = torrent(hosts=("first", "second"))
    observed = datetime.datetime.now() - datetime.timedelta(minutes=20)
    first_seen = {candidate.hash: observed}

    with capture_logs(processors=[merge_contextvars]) as logs:
        remove(job, FakeClient([candidate]), config, first_seen, dry_run=dry_run)

    candidate.delete.assert_not_called()
    assert first_seen == {candidate.hash: observed}
    assert not any(
        "eligible for removal" in event["event"] or "selected to reclaim" in event["event"]
        for event in logs
    )
    assert any(event.get("tracker") == "second" for event in logs)


@pytest.mark.parametrize("job", ["remove_stopped", "maintain_free_space"])
@pytest.mark.parametrize("dry_run", [False, True])
def test_different_trackers_can_pass_different_required_dimensions(job, dry_run):
    config = Config.model_validate(
        raw_config(
            tracker(required=(10, 100), desired=(20, 200)),
            tracker("second", required=(100, 2), desired=(200, 3)),
        )
    )
    candidate = torrent(hosts=("first", "second", "unknown", "first"))

    remove(job, FakeClient([candidate]), config, dry_run=dry_run)

    if dry_run:
        candidate.delete.assert_not_called()
    else:
        candidate.delete.assert_called_once_with(delete_files=True)


@pytest.mark.parametrize("job", ["remove_stopped", "maintain_free_space"])
@pytest.mark.parametrize(
    ("required", "seeding_time", "ratio", "eligible"),
    [
        ((10, 2), 599, 1.99, False),
        ((10, 2), 600, 0, True),
        ((10, 2), 0, 2, True),
        ((10, 2), 0, -1, True),
        ((-1, 2), 100000, 1.99, False),
        ((-1, 2), 0, 2, True),
        ((10, -1), 599, 100, False),
        ((10, -1), 600, 0, True),
        ((-1, -1), 100000, -1, False),
        ((0, 0), 0, 0, True),
    ],
)
def test_required_thresholds_are_inclusive_and_unlimited_never_qualifies(
    job, required, seeding_time, ratio, eligible
):
    config = Config.model_validate(raw_config(tracker(required=required, desired=(-1, -1))))
    candidate = torrent(seeding_time=seeding_time, ratio=ratio)

    remove(job, FakeClient([candidate]), config)

    assert candidate.delete.called is eligible


@pytest.mark.parametrize("job", ["remove_stopped", "maintain_free_space"])
@pytest.mark.parametrize("field", ["seeding_time", "ratio"])
@pytest.mark.parametrize("value", [None, "missing", float("nan"), float("inf"), -2, True])
def test_invalid_achieved_metrics_never_authorize_removal(job, field, value):
    config = Config.model_validate(raw_config(tracker(required=(0, 0), desired=(0, 0))))
    candidate = torrent()
    if value == "missing":
        delattr(candidate, field)
    else:
        setattr(candidate, field, value)

    remove(job, FakeClient([candidate]), config)

    candidate.delete.assert_not_called()


@pytest.mark.parametrize("job", ["remove_stopped", "maintain_free_space"])
@pytest.mark.parametrize("failure", ["unknown", "missing", "raises", "invalid_url"])
def test_no_match_or_tracker_failure_never_authorizes_removal(job, failure):
    candidate = torrent(hosts=("unknown",))
    if failure == "missing":
        del candidate.trackers
    elif failure == "raises":
        candidate.trackers = Mock()
    elif failure == "invalid_url":
        candidate.trackers[0].url = "http://[invalid"
    config = Config.model_validate(raw_config())

    remove(job, FakeClient([candidate]), config)

    candidate.delete.assert_not_called()


def test_required_policies_do_not_replace_active_stopped_limits():
    config = Config.model_validate(raw_config())
    candidate = torrent()
    candidate.max_seeding_time = -1
    candidate.max_ratio = -1

    remove_stopped(FakeClient([candidate]), config, {})

    candidate.delete.assert_not_called()


def test_free_space_uses_required_not_desired_or_category_override():
    config = Config.model_validate(raw_config())
    candidate = torrent()
    config.set_seed_limits = config.set_seed_limits.model_validate(
        {
            "categories": [
                {
                    "name": "cross",
                    "action": "Stop",
                    "override": {"seed_time_minutes": -1, "ratio": -1},
                }
            ]
        }
    )

    maintain_free_space(cast(qbittorrentapi.Client, FakeClient([candidate])), config)

    candidate.delete.assert_called_once_with(delete_files=True)


def test_unregistered_removal_is_the_only_required_policy_exception():
    config = Config.model_validate(raw_config(tracker(required=(-1, -1), desired=(-1, -1))))
    candidate = torrent(seeding_time=0, ratio=0)
    candidate.trackers[0].msg = "unregistered torrent"

    remove_unregistered(
        cast(qbittorrentapi.Client, FakeClient([candidate])), config.remove_unregistered, {}
    )

    candidate.delete.assert_called_once_with(delete_files=True)


@pytest.mark.parametrize("dry_run", [False, True])
@pytest.mark.parametrize("reverse_config", [False, True])
@pytest.mark.parametrize("reverse_announces", [False, True])
def test_free_space_priority_is_minimum_of_all_matches(dry_run, reverse_config, reverse_announces):
    policies = [
        tracker("high", priority=1000),
        tracker("protected", priority=-500),
        tracker("normal"),
        tracker("low", priority=-1000),
    ]
    if reverse_config:
        policies.reverse()
    config = Config.model_validate(raw_config(*policies))
    candidates = [
        torrent("protected_few", hosts=("high", "protected", "high", "unknown"), seeders=10),
        torrent("low", hosts=("low",), seeders=10000),
        torrent("protected_many", hosts=("protected", "high"), seeders=100),
        torrent("normal", hosts=("normal",), seeders=1),
    ]
    if reverse_announces:
        for candidate in candidates:
            candidate.trackers.reverse()
    deleted = []
    for candidate in candidates:
        candidate.delete.side_effect = lambda *, t=candidate, **kwargs: deleted.append(t.name)

    with capture_logs(processors=[merge_contextvars]) as logs:
        maintain_free_space(cast(qbittorrentapi.Client, FakeClient(candidates)), config, dry_run)

    selections = [
        entry for entry in logs if entry["event"] == "torrent selected to reclaim free space"
    ]
    expected = ["normal", "protected_many", "protected_few", "low"]
    assert [entry["torrent"] for entry in selections] == expected
    assert [entry["delete_priority"] for entry in selections] == [0, -500, -500, -1000]
    assert set(selections[1]["trackers"]) == set(selections[2]["trackers"]) == {"high", "protected"}
    assert deleted == ([] if dry_run else expected)


def test_override_precedes_aggregated_desired_limits():
    raw = raw_config(tracker(), tracker("second", required=(100, 10), desired=(-1, -1)))
    raw["set_seed_limits"] = {
        "categories": [
            {
                "name": "cross",
                "action": "Remove",
                "override": {"seed_time_minutes": 100, "ratio": 10},
            }
        ]
    }
    config = Config.model_validate(raw)
    candidate = torrent(hosts=("first", "second"))

    set_seed_limits(FakeClient([candidate]), config)

    candidate.set_share_limits.assert_called_once_with(
        ratio_limit="10.0",
        seeding_time_limit=100,
        inactive_seeding_time_limit=-1,
        share_limit_action="Remove",
        share_limits_mode="MatchAny",
    )


@pytest.mark.parametrize("action", ["Default", "Remove", "RemoveWithContent"])
@pytest.mark.parametrize("override", [None, {"seed_time_minutes": -1, "ratio": -1}])
@pytest.mark.parametrize(
    "policies",
    [
        [tracker(), tracker("second", required=(100, 10), desired=(200, 20))],
        [tracker(required=(-1, 2), desired=(-1, 3)), tracker("second")],
        [tracker(required=(10, -1), desired=(20, -1)), tracker("second")],
        [
            tracker(required=(0, 0), desired=(0, 0)),
            tracker("second", required=(0, 0), desired=(0, 0)),
        ],
    ],
)
def test_applied_match_any_limits_cannot_trigger_before_required_policies(
    action, override, policies
):
    raw = raw_config(*policies)
    raw["set_seed_limits"] = {
        "categories": [{"name": "cross", "action": action, "override": override}]
    }
    config = Config.model_validate(raw)
    candidate = torrent(hosts=("first", "second"))
    set_seed_limits(FakeClient([candidate]), config)
    applied = candidate.set_share_limits.call_args.kwargs

    for seeding_time in (0, 599, 600, 1200, 5999, 6000, 12000):
        for ratio in (0, 1, 1.99, 2, 3, 10, 20, -1):
            candidate.seeding_time = seeding_time
            candidate.ratio = ratio
            if limits_met(
                seeding_time, ratio, applied["seeding_time_limit"], float(applied["ratio_limit"])
            ):
                assert required_limits_met(candidate, config.trackers)
