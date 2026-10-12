from types import SimpleNamespace

import pytest
from structlog.contextvars import merge_contextvars
from structlog.testing import capture_logs

from tarr.config import (
    Config,
    MaintainFreeSpaceConfig,
    RemoveStoppedConfig,
    RemoveUnregisteredConfig,
)
from tarr.main import torrents_in_categories, warn_category_overlaps


class FakeClient:
    def __init__(self, *categories: str):
        self.torrents = [SimpleNamespace(category=category) for category in categories]

    def torrents_info(self, status_filter=None):
        return self.torrents


@pytest.fixture
def client():
    return FakeClient("movies", "music", "upload", "")


def filtered_categories(client, included=None, ignored=None):
    torrents = torrents_in_categories(client, included, ignored)
    return [torrent.category for torrent in torrents]


@pytest.mark.parametrize(
    ("included", "ignored", "expected"),
    [
        (None, None, ["movies", "music", "upload", ""]),
        (None, ["upload", None], ["movies", "music"]),
        (["movies", "music"], None, ["movies", "music"]),
        (["movies", "music"], ["music"], ["movies"]),
        ([None], None, [""]),
    ],
    ids=[
        "no-filters",
        "ignore-only",
        "categories-only",
        "ignore-takes-precedence",
        "uncategorized",
    ],
)
def test_category_filtering(client, included, ignored, expected):
    assert filtered_categories(client, included, ignored) == expected


@pytest.mark.parametrize(
    "config_type",
    [
        RemoveUnregisteredConfig,
        RemoveStoppedConfig,
        MaintainFreeSpaceConfig,
    ],
)
def test_all_feature_configs_accept_both_filters(config_type):
    config = config_type(categories=["movies"], ignore_categories=["music"])

    assert config.categories == ["movies"]
    assert config.ignore_categories == ["music"]


def test_startup_warning_names_overlapping_categories():
    config = Config.model_validate(
        {
            "qbittorrent": {
                "host": "localhost",
                "username": "user",
                "password": "pass",
            },
            "remove_unregistered": {
                "categories": ["music", None],
                "ignore_categories": ["music", None],
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
    )

    with capture_logs(processors=[merge_contextvars]) as logs:
        warn_category_overlaps(config)

    assert logs == [
        {
            "event": "categories overlap with ignore_categories",
            "job": "remove_unregistered",
            "log_level": "warning",
            "overlapping_categories": ["music", None],
        }
    ]
