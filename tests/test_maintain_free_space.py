from types import SimpleNamespace
from typing import cast
from unittest.mock import Mock

import pytest
import qbittorrentapi
from structlog.contextvars import get_contextvars

from tarr.config import Config
from tarr.main import log, maintain_free_space

GIB = 1024**3


class FakeClient:
    def __init__(self, torrents, free_space=0):
        self.torrents = torrents
        self.free_space = free_space
        self.space_reads = 0

    def torrents_info(self):
        return self.torrents

    def sync_maindata(self):
        self.space_reads += 1
        return SimpleNamespace(server_state=SimpleNamespace(free_space_on_disk=self.free_space))


def make_config(threshold=3, *, trackers=None, **filters):
    return Config.model_validate(
        {
            "qbittorrent": {
                "host": "localhost",
                "username": "user",
                "password": "pass",
            },
            "maintain_free_space": {
                "enabled": True,
                "free_space_threshold_gibi": threshold,
                **filters,
            },
            "trackers": trackers
            if trackers is not None
            else [
                {
                    "name": "example",
                    "hosts": ["tracker.example.com"],
                    "seed_time_minutes": 1,
                    "ratio": 1,
                }
            ],
        }
    )


def make_torrent(
    name,
    seeders,
    *,
    seeding_time=120,
    ratio=1,
    category="movies",
    tracker_host="tracker.example.com",
):
    return SimpleNamespace(
        hash=name,
        name=name,
        state="uploading",
        size=GIB,
        category=category,
        seeding_time=seeding_time,
        ratio=ratio,
        uploaded=ratio * GIB,
        num_complete=seeders,
        num_seeds=0,
        trackers=[SimpleNamespace(url=f"https://{tracker_host}/announce")],
        delete=Mock(),
    )


def test_removes_most_seeded_first_regardless_of_upload_rate_or_connected_seeders():
    low = make_torrent("low", 1, seeding_time=1200)
    high = make_torrent("high", 100, seeding_time=0, ratio=10)
    middle = make_torrent("middle", 10)
    low.num_seeds = 50
    torrents = [low, high, middle]
    deleted = []
    for torrent in torrents:
        torrent.delete.side_effect = lambda *, t=torrent, **_kwargs: deleted.append(t.name)

    maintain_free_space(cast(qbittorrentapi.Client, FakeClient(torrents)), make_config())

    assert deleted == ["high", "middle", "low"]
    for torrent in torrents:
        torrent.delete.assert_called_once_with(delete_files=True)


@pytest.mark.parametrize("dry_run", [False, True])
def test_orders_by_tracker_priority_then_swarm_seeders(monkeypatch, dry_run):
    config = make_config(
        threshold=5,
        trackers=[
            {
                "name": name,
                "hosts": [f"{name}.example.com"],
                "seed_time_minutes": 1,
                "ratio": 1,
                **({"delete_priority": priority} if priority is not None else {}),
            }
            for name, priority in [("high", 10), ("also_high", 10), ("default", None), ("low", -10)]
        ],
    )
    torrents = [
        make_torrent("low", 1000, tracker_host="low.example.com"),
        make_torrent("default", 100, tracker_host="default.example.com"),
        make_torrent("high_few_seeders", 1, tracker_host="high.example.com"),
        make_torrent("high_many_seeders", 10, tracker_host="also_high.example.com"),
        make_torrent("high_middle_seeders", 5, tracker_host="high.example.com"),
    ]
    selected = []
    deleted = []

    def record_selection(event, **_kwargs):
        if event == "torrent selected to reclaim free space":
            selected.append(get_contextvars())

    monkeypatch.setattr(log, "info", record_selection)
    for torrent in torrents:
        torrent.delete.side_effect = lambda *, t=torrent, **_kwargs: deleted.append(t.name)
    client = FakeClient(torrents)

    maintain_free_space(cast(qbittorrentapi.Client, client), config, dry_run=dry_run)

    expected = [
        "high_many_seeders",
        "high_middle_seeders",
        "high_few_seeders",
        "default",
        "low",
    ]
    assert [context["torrent"] for context in selected] == expected
    assert [context["delete_priority"] for context in selected] == [10, 10, 10, 0, -10]
    assert [context["tracker"] for context in selected] == [
        "also_high",
        "high",
        "high",
        "default",
        "low",
    ]
    assert deleted == ([] if dry_run else expected)
    assert client.space_reads == (1 if dry_run else 6)
    assert get_contextvars() == {}
    for torrent in torrents:
        if dry_run:
            torrent.delete.assert_not_called()
        else:
            torrent.delete.assert_called_once_with(delete_files=True)


def test_high_tracker_priority_does_not_bypass_eligibility_or_category_filters():
    config = make_config(
        ignore_categories=["upload"],
        trackers=[
            {
                "name": name,
                "hosts": [f"{name}.example.com"],
                "seed_time_minutes": 1,
                "ratio": 1,
                "delete_priority": priority,
            }
            for name, priority in [("high", 100), ("low", 0)]
        ],
    )
    below_limits = make_torrent(
        "below_limits",
        100,
        seeding_time=0,
        ratio=0,
        tracker_host="high.example.com",
    )
    excluded = make_torrent(
        "excluded",
        100,
        category="upload",
        tracker_host="high.example.com",
    )
    eligible = make_torrent("eligible", 1, tracker_host="low.example.com")

    maintain_free_space(
        cast(qbittorrentapi.Client, FakeClient([below_limits, excluded, eligible])),
        config,
    )

    below_limits.delete.assert_not_called()
    excluded.delete.assert_not_called()
    eligible.delete.assert_called_once_with(delete_files=True)


def test_stops_deleting_when_reported_free_space_reaches_threshold():
    low = make_torrent("low", 1)
    high = make_torrent("high", 100)
    middle = make_torrent("middle", 10)
    client = FakeClient([low, high, middle], free_space=GIB)
    high.delete.side_effect = lambda **_kwargs: setattr(client, "free_space", 3 * GIB)

    maintain_free_space(cast(qbittorrentapi.Client, client), make_config(threshold=3))

    high.delete.assert_called_once_with(delete_files=True)
    middle.delete.assert_not_called()
    low.delete.assert_not_called()
    assert client.space_reads == 2


@pytest.mark.parametrize("space_after_last_link", [0, GIB])
def test_continues_deleting_when_hard_link_removal_reclaims_no_space(space_after_last_link):
    first_link = make_torrent("first_link", 100)
    last_link = make_torrent("last_link", 10)
    other = make_torrent("other", 1)
    client = FakeClient([other, last_link, first_link])
    last_link.delete.side_effect = lambda **_kwargs: setattr(
        client, "free_space", space_after_last_link
    )

    maintain_free_space(cast(qbittorrentapi.Client, client), make_config(threshold=1))

    first_link.delete.assert_called_once_with(delete_files=True)
    last_link.delete.assert_called_once_with(delete_files=True)
    if space_after_last_link == 0:
        other.delete.assert_called_once_with(delete_files=True)
        assert client.space_reads == 4
    else:
        other.delete.assert_not_called()
        assert client.space_reads == 3


@pytest.mark.parametrize("free_space", [GIB, 2 * GIB])
def test_does_not_delete_when_free_space_meets_or_exceeds_threshold(free_space):
    torrent = make_torrent("high", 100)
    client = FakeClient([torrent], free_space=free_space)

    maintain_free_space(cast(qbittorrentapi.Client, client), make_config(threshold=1))

    torrent.delete.assert_not_called()


def test_preserves_eligibility_and_category_filters():
    eligible = make_torrent("eligible", 1)
    below_limits = make_torrent("below_limits", 100, seeding_time=0, ratio=0)
    unmanaged = make_torrent("unmanaged", 100)
    unmanaged.trackers = [SimpleNamespace(url="https://unknown.example/announce")]
    excluded = make_torrent("excluded", 100, category="upload")
    not_included = make_torrent("not_included", 100, category="tv")
    torrents = [below_limits, unmanaged, excluded, not_included, eligible]
    config = make_config(categories=["movies", "upload"], ignore_categories=["upload"])

    maintain_free_space(cast(qbittorrentapi.Client, FakeClient(torrents)), config)

    eligible.delete.assert_called_once_with(delete_files=True)
    for torrent in torrents[:-1]:
        torrent.delete.assert_not_called()


def test_dry_run_selects_most_seeded_without_deleting(monkeypatch):
    low = make_torrent("low", 0)
    high = make_torrent("high", 100)
    selected = []

    def record_selection(event, **_kwargs):
        if event == "torrent selected to reclaim free space":
            selected.append(get_contextvars())

    monkeypatch.setattr(log, "info", record_selection)
    client = FakeClient([low, high])

    maintain_free_space(
        cast(qbittorrentapi.Client, client),
        make_config(threshold=1),
        dry_run=True,
    )

    assert [context["torrent"] for context in selected] == ["high"]
    assert selected[0]["torrent_seeders"] == 100
    assert selected[0]["dry_run"] is True
    assert get_contextvars() == {}
    assert client.space_reads == 1
    low.delete.assert_not_called()
    high.delete.assert_not_called()
