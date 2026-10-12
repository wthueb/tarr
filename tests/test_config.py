import pathlib
from importlib import import_module
from unittest.mock import Mock

import pytest
from pydantic import ValidationError

from tarr.config import Config, load_config

ROOT_FIELDS = [
    "interval_seconds",
    "remove_unregistered",
    "remove_stopped",
    "maintain_free_space",
    "set_seed_limits",
    "cleanup_empty_dirs",
]


def minimal_config() -> dict:
    return {
        "qbittorrent": {
            "host": "localhost",
            "username": "user",
            "password": "pass",
        },
        "trackers": [
            {
                "name": "example",
                "hosts": ["tracker.example.com"],
                "required": {"seed_time_minutes": 1, "ratio": 1},
                "desired": {"seed_time_minutes": 1, "ratio": 1},
            }
        ],
    }


def test_example_config_uses_supported_structure():
    config = load_config(pathlib.Path(__file__).parents[1] / "config.example.yaml")

    assert config.interval_seconds == 60
    assert config.remove_unregistered.enabled is True
    assert config.remove_stopped.enabled is False
    assert config.maintain_free_space.enabled is True
    assert config.set_seed_limits.enabled is True
    assert config.cleanup_empty_dirs.enabled is False
    assert config.cleanup_empty_dirs.directories == ["/downloads/cross-seed"]
    mapping = config.cleanup_empty_dirs.directory_mappings[0]
    assert mapping.qbittorrent_path == mapping.local_path == pathlib.Path("/downloads/cross-seed")
    assert set(config.qbittorrent.model_dump()) == {"host", "username", "password"}


def test_root_fields_keep_defaults_when_omitted():
    config = Config.model_validate(minimal_config())

    assert config.interval_seconds == 60
    for field in ROOT_FIELDS[1:]:
        assert getattr(config, field).enabled is False


@pytest.mark.parametrize("field", ROOT_FIELDS)
def test_polling_and_job_fields_are_accepted_at_config_root(field):
    raw = minimal_config()
    raw[field] = 120 if field == "interval_seconds" else {}

    config = Config.model_validate(raw)

    if field == "interval_seconds":
        assert config.interval_seconds == 120
    else:
        assert getattr(config, field).enabled is False


@pytest.mark.parametrize("field", ROOT_FIELDS)
@pytest.mark.parametrize("also_at_root", [False, True])
def test_polling_and_job_fields_are_rejected_under_qbittorrent(field, also_at_root):
    raw = minimal_config()
    raw["qbittorrent"][field] = 60 if field == "interval_seconds" else {}
    if also_at_root:
        raw[field] = raw["qbittorrent"][field]

    with pytest.raises(ValidationError) as error:
        Config.model_validate(raw)

    assert any(
        item["loc"] == ("qbittorrent", field) and item["type"] == "extra_forbidden"
        for item in error.value.errors()
    )


@pytest.mark.parametrize("interval", [0, -1, 1.5, None])
def test_root_polling_interval_rejects_invalid_values(interval):
    raw = minimal_config()
    raw["interval_seconds"] = interval

    with pytest.raises(ValidationError):
        Config.model_validate(raw)


def test_daemon_uses_root_polling_interval(tmp_path, monkeypatch):
    main_module = import_module("tarr.main")
    path = tmp_path / "config.yaml"
    path.write_text(
        "qbittorrent:\n"
        "  host: localhost\n"
        "  username: user\n"
        "  password: pass\n"
        "interval_seconds: 120\n"
        "trackers:\n"
        "  - name: example\n"
        "    hosts: [tracker.example.com]\n"
        "    required: {seed_time_minutes: 1, ratio: 1}\n"
        "    desired: {seed_time_minutes: 1, ratio: 1}\n"
    )
    client = Mock()
    run = Mock()
    sleep = Mock(side_effect=KeyboardInterrupt)
    monkeypatch.setattr("sys.argv", ["tarr", "--config", str(path), "--daemon"])
    monkeypatch.setattr(main_module, "build_client", Mock(return_value=client))
    monkeypatch.setattr(main_module, "setup_logging", Mock())
    monkeypatch.setattr(main_module, "run", run)
    monkeypatch.setattr(main_module.time, "sleep", sleep)

    with pytest.raises(KeyboardInterrupt):
        main_module.main()

    run.assert_called_once()
    assert run.call_args.args[0] is client
    sleep.assert_called_once_with(120)


def test_tracker_delete_priority_defaults_to_zero():
    config = Config.model_validate(minimal_config())

    assert config.trackers[0].delete_priority == 0


@pytest.mark.parametrize("priority", [-10, 0, 10])
def test_tracker_delete_priority_accepts_integers(priority):
    raw = minimal_config()
    raw["trackers"][0]["delete_priority"] = priority

    config = Config.model_validate(raw)

    assert config.trackers[0].delete_priority == priority


@pytest.mark.parametrize("priority", [1.5, "high", None])
def test_tracker_delete_priority_rejects_invalid_values(priority):
    raw = minimal_config()
    raw["trackers"][0]["delete_priority"] = priority

    with pytest.raises(ValidationError):
        Config.model_validate(raw)
