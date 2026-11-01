import os
from types import SimpleNamespace

import pytest
from structlog.contextvars import get_contextvars, merge_contextvars
from structlog.testing import capture_logs

from tarr.main import maintain_free_space
from tests.deletion_helpers import GIB, Clock, FileClient, configuration, measured_coordinator


def setup_cleanup(tmp_path, monkeypatch, threshold=3, **filters):
    client = FileClient(tmp_path)
    config = configuration(
        client,
        maintain_free_space={
            "enabled": True,
            "free_space_threshold_gibi": threshold,
            "free_space_path": "/downloads",
            **filters,
        },
    )
    coordinator = measured_coordinator(client, config, monkeypatch)
    return client, config, coordinator


def test_removes_most_seeded_first_regardless_of_upload_rate_or_connected_seeders(
    tmp_path, monkeypatch
):
    client, config, coordinator = setup_cleanup(tmp_path, monkeypatch)
    client.add("low", 1, seeding_time=1200, num_seeds=50)
    client.add("high", 100, seeding_time=0, ratio=10)
    client.add("middle", 10)

    maintain_free_space(client, config, deletion_coordinator=coordinator)

    assert client.deleted == ["high", "middle", "low"]
    assert get_contextvars() == {}


@pytest.mark.parametrize("dry_run", [False, True])
def test_orders_by_tracker_priority_then_swarm_seeders(tmp_path, monkeypatch, dry_run):
    client, config, coordinator = setup_cleanup(tmp_path, monkeypatch, threshold=5)
    config.trackers = [
        config.trackers[0].model_copy(
            update={
                "name": name,
                "hosts": [f"{name}.example"],
                "delete_priority": priority,
            }
        )
        for name, priority in [("high", 10), ("also_high", 10), ("default", 0), ("low", -10)]
    ]
    for name, count, tracker in [
        ("low", 1000, "low"),
        ("default", 100, "default"),
        ("high_few", 1, "high"),
        ("high_many", 10, "also_high"),
        ("high_middle", 5, "high"),
    ]:
        client.add(
            name, count, trackers=[SimpleNamespace(url=f"https://{tracker}.example/announce")]
        )

    with capture_logs(processors=[merge_contextvars]) as logs:
        maintain_free_space(client, config, dry_run, coordinator)

    selections = [
        line for line in logs if line["event"] == "torrent selected to reclaim free space"
    ]
    expected = ["high_many", "high_middle", "high_few", "default", "low"]
    assert [line["torrent"] for line in selections] == expected
    assert [line["delete_priority"] for line in selections] == [10, 10, 10, 0, -10]
    assert client.deleted == ([] if dry_run else expected)
    assert get_contextvars() == {}


def test_stops_at_fresh_filesystem_threshold_despite_stale_api(tmp_path, monkeypatch):
    client, config, coordinator = setup_cleanup(tmp_path, monkeypatch)
    client.add("high", 100)
    client.add("middle", 10)
    client.free_space = GIB

    def finish(torrent_hash):
        client.finish(torrent_hash)
        client.free_space = 3 * GIB

    client.on_delete = finish
    maintain_free_space(client, config, deletion_coordinator=coordinator)

    assert client.deleted == ["high"]


def test_hundreds_of_candidates_cannot_queue_behind_async_deletion(tmp_path, monkeypatch):
    clock = Clock(monkeypatch)
    client, config, coordinator = setup_cleanup(tmp_path, monkeypatch)
    for index in range(300):
        client.add(str(index), seeders=300 - index)
    client.on_delete = lambda torrent_hash: setattr(
        client, "torrents", [torrent for torrent in client.torrents if torrent.hash != torrent_hash]
    )

    maintain_free_space(client, config, deletion_coordinator=coordinator)

    assert client.deleted == ["0"]
    assert coordinator.pending.torrent_hash == "0"
    assert clock.now == 4


def test_waits_for_all_files_then_uses_recovered_space(tmp_path, monkeypatch):
    clock = Clock(monkeypatch)
    client, config, coordinator = setup_cleanup(tmp_path, monkeypatch, threshold=1)
    client.add("high", 100)
    client.add("low", 1)

    def delayed(torrent_hash):
        client.torrents = [torrent for torrent in client.torrents if torrent.hash != torrent_hash]

        def release():
            client.finish(torrent_hash)
            client.free_space = GIB

        clock.events.append((3, release))

    client.on_delete = delayed
    maintain_free_space(client, config, deletion_coordinator=coordinator)

    assert client.deleted == ["high"]
    assert coordinator.pending is None
    assert clock.now == 3


@pytest.mark.parametrize("recover", [False, True])
def test_hard_links_do_not_receive_size_credit(tmp_path, monkeypatch, recover):
    client, config, coordinator = setup_cleanup(tmp_path, monkeypatch, threshold=1)
    client.add("first", 100)
    client.add("last", 10)
    client.add("other", 1)
    (tmp_path / "last").unlink()
    os.link(tmp_path / "first", tmp_path / "last")

    def finish(torrent_hash):
        client.finish(torrent_hash)
        if recover and torrent_hash == "last":
            client.free_space = GIB

    client.on_delete = finish
    maintain_free_space(client, config, deletion_coordinator=coordinator)

    assert client.deleted == (["first", "last"] if recover else ["first", "last", "other"])


def test_absent_files_do_not_receive_size_credit(tmp_path, monkeypatch):
    client, config, coordinator = setup_cleanup(tmp_path, monkeypatch, threshold=1)
    missing = client.add("missing", 100)
    (tmp_path / missing.name).unlink()
    client.add("present", 10)
    client.add("other", 1)

    def finish(torrent_hash):
        client.finish(torrent_hash)
        if torrent_hash == "present":
            client.free_space = GIB

    client.on_delete = finish
    maintain_free_space(client, config, deletion_coordinator=coordinator)

    assert client.deleted == ["missing", "present"]
    assert coordinator.pending is None
    assert (tmp_path / "other").exists()


@pytest.mark.parametrize("space", [GIB, 2 * GIB])
def test_no_deletion_at_or_above_threshold(tmp_path, monkeypatch, space):
    client, config, coordinator = setup_cleanup(tmp_path, monkeypatch, threshold=1)
    client.add()
    client.free_space = space

    maintain_free_space(client, config, deletion_coordinator=coordinator)

    assert client.deleted == []


def test_preserves_eligibility_and_category_filters(tmp_path, monkeypatch):
    client, config, coordinator = setup_cleanup(
        tmp_path, monkeypatch, categories=["movies", "upload"], ignore_categories=["upload"]
    )
    client.add("below", 100, seeding_time=0, ratio=0)
    client.add("unknown", 100, trackers=[SimpleNamespace(url="https://unknown/announce")])
    client.add("excluded", 100, category="upload")
    client.add("not_included", 100, category="tv")
    client.add("eligible", 1)

    maintain_free_space(client, config, deletion_coordinator=coordinator)

    assert client.deleted == ["eligible"]


def test_dry_run_estimates_without_deleting_or_waiting(tmp_path, monkeypatch):
    clock = Clock(monkeypatch)
    client, config, coordinator = setup_cleanup(tmp_path, monkeypatch, threshold=1)
    client.add("low", 1)
    client.add("high", 100)

    with capture_logs(processors=[merge_contextvars]) as logs:
        maintain_free_space(client, config, True, coordinator)

    selections = [
        line for line in logs if line["event"] == "torrent selected to reclaim free space"
    ]
    assert [line["torrent"] for line in selections] == ["high"]
    assert selections[0]["dry_run"] is True
    assert clock.now == 0
    assert client.deleted == client.stopped == []
    assert coordinator.pending is None


def test_refreshes_candidates_and_rechecks_threshold_before_request(tmp_path, monkeypatch):
    client, config, coordinator = setup_cleanup(tmp_path, monkeypatch, threshold=1)
    client.add("high", 100)
    client.add("middle", 10)
    low = client.add("low", 1)

    def finish(torrent_hash):
        client.finish(torrent_hash)
        low.num_complete = 1000
        if torrent_hash == "low":
            client.free_space = GIB

    client.on_delete = finish
    maintain_free_space(client, config, deletion_coordinator=coordinator)

    assert client.deleted == ["high", "low"]
