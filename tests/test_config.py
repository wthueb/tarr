import pathlib

import pytest
from pydantic import ValidationError

from tarr.config import Config, load_config


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
                "seed_time_minutes": 1,
                "ratio": 1,
            }
        ],
    }


def test_example_config_uses_supported_structure():
    config = load_config(pathlib.Path(__file__).parents[1] / "config.example.yaml")

    assert config.qbittorrent.interval_seconds == 60
    assert config.qbittorrent.remove_unregistered.enabled is True
    assert config.qbittorrent.remove_stopped.enabled is False
    assert config.qbittorrent.maintain_free_space.enabled is True
    assert config.qbittorrent.set_seed_limits.enabled is True
    assert config.qbittorrent.cleanup_empty_dirs.enabled is False
    assert config.qbittorrent.cleanup_empty_dirs.directories == ["/downloads/cross-seed"]
    mapping = config.qbittorrent.cleanup_empty_dirs.directory_mappings[0]
    assert mapping.qbittorrent_path == mapping.local_path == pathlib.Path("/downloads/cross-seed")


@pytest.mark.parametrize(
    "field",
    [
        "interval_seconds",
        "remove_unregistered",
        "remove_stopped",
        "maintain_free_space",
        "set_seed_limits",
        "cleanup_empty_dirs",
    ],
)
def test_qbittorrent_fields_are_rejected_at_config_root(field):
    raw = minimal_config()
    raw[field] = 60 if field == "interval_seconds" else {}

    with pytest.raises(ValidationError):
        Config.model_validate(raw)
