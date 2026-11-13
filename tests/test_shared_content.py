import os
from typing import cast
from unittest.mock import Mock

import pytest
import qbittorrentapi
from structlog.testing import capture_logs

from tarr.deletion import ContentDeletionCoordinator
from tarr.main import maintain_free_space, run
from tests.deletion_helpers import (
    GIB,
    Clock,
    FileClient,
    configuration,
    measured_coordinator,
    set_manifest,
)


@pytest.fixture
def setup(tmp_path, monkeypatch):
    clock = Clock(monkeypatch)
    client = FileClient(tmp_path)
    coordinator = ContentDeletionCoordinator(client, configuration(client).content_deletion)
    return client, coordinator, clock


def shared_pair(client, *, partial=False, **peer_attributes):
    selected = client.add("release/shared", hash="selected", content_path="/downloads/release")
    if partial:
        set_manifest(client, selected, ["release/shared", "release/owned"])
    peer = client.add("release/shared", hash="retained", **peer_attributes)
    return selected, peer


@pytest.mark.parametrize("category", ["movies", "upload", "unmanaged"])
def test_all_shared_removes_only_torrent(setup, category):
    client, coordinator, _ = setup
    selected, peer = shared_pair(client, category=category)

    assert coordinator.delete(selected, lambda _: True)

    assert client.delete_requests == [(selected.hash, False)]
    assert client.torrents == [peer]
    assert (client.root / "release/shared").read_bytes() == b"data"
    assert coordinator.pending is None


def test_partial_overlap_unlinks_only_owned_files(setup):
    client, coordinator, _ = setup
    selected, peer = shared_pair(client, partial=True)

    assert coordinator.delete(selected, lambda _: True)

    assert client.delete_requests == [(selected.hash, False)]
    assert client.torrents == [peer]
    assert (client.root / "release/shared").exists()
    assert not (client.root / "release/owned").exists()
    assert coordinator.pending is None


def test_metadata_only_removal_does_not_require_writable_storage(setup, monkeypatch):
    client, coordinator, _ = setup
    selected, _ = shared_pair(client)
    monkeypatch.setattr("tarr.deletion.os.access", lambda *args: False)
    assert coordinator.delete(selected, lambda _: True)
    assert client.delete_requests == [(selected.hash, False)]
    assert (client.root / "release/shared").exists()


def test_partial_removal_requires_writable_storage_before_removing_torrent(setup, monkeypatch):
    client, coordinator, _ = setup
    selected, _ = shared_pair(client, partial=True)
    monkeypatch.setattr("tarr.deletion.os.access", lambda *args: False)
    assert not coordinator.delete(selected, lambda _: True)
    assert client.delete_requests == []
    assert len(client.torrents) == 2
    assert (client.root / "release/owned").exists()
    assert coordinator.pending is None


def test_last_reference_can_delete_content(setup):
    client, coordinator, _ = setup
    selected, peer = shared_pair(client)
    assert coordinator.delete(selected, lambda _: True)
    assert coordinator.delete(peer, lambda _: True)
    assert client.delete_requests == [(selected.hash, False), (peer.hash, True)]
    assert not (client.root / "release/shared").exists()


def test_distinct_hardlink_paths_are_owned_separately(setup):
    client, coordinator, _ = setup
    selected = client.add("one")
    peer = client.add("two")
    (client.root / "two").unlink()
    os.link(client.root / "one", client.root / "two")

    assert coordinator.delete(selected, lambda _: True)
    assert client.delete_requests == [(selected.hash, True)]
    assert (client.root / "two").read_bytes() == b"data"
    assert client.torrents == [peer]


def test_shared_directory_does_not_make_distinct_files_shared(setup):
    client, coordinator, _ = setup
    selected = client.add("release/one")
    peer = client.add("release/two")

    assert coordinator.delete(selected, lambda _: True)
    assert client.delete_requests == [(selected.hash, True)]
    assert (client.root / "release/two").exists()
    assert client.torrents == [peer]


@pytest.mark.parametrize("partial", [False, True])
def test_dry_run_reports_ownership_without_changing_anything(setup, partial):
    client, coordinator, clock = setup
    selected, _ = shared_pair(client, partial=partial)

    with capture_logs() as logs:
        assert coordinator.delete(selected, lambda _: True, dry_run=True)

    assert logs[-1]["owned_file_count"] == int(partial)
    assert logs[-1]["shared_file_count"] == 1
    assert client.delete_requests == client.stopped == []
    assert (client.root / "release/shared").exists()
    assert len(client.torrents) == 2
    assert clock.now == 0


def test_ownership_includes_skipped_peer_files(setup):
    client, coordinator, _ = setup
    selected, peer = shared_pair(client)
    client.files[peer.hash][0].priority = 0
    assert coordinator.delete(selected, lambda _: True)
    assert client.delete_requests == [(selected.hash, False)]
    assert (client.root / "release/shared").exists()


def test_mapped_aliases_are_the_same_file_path(setup):
    client, _, _ = setup
    config = configuration(client).content_deletion
    config.path_mappings.append(f"/alias:{client.root}")
    coordinator = ContentDeletionCoordinator(client, config)
    selected, _ = shared_pair(client, save_path="/alias", content_path="/alias/release/shared")

    assert coordinator.delete(selected, lambda _: True)
    assert client.delete_requests == [(selected.hash, False)]
    assert (client.root / "release/shared").exists()


def test_unmapped_unrelated_peer_does_not_block_deletion(setup):
    client, coordinator, _ = setup
    selected = client.add("one")
    client.add("two", save_path="/elsewhere", content_path="/elsewhere/two")
    assert coordinator.delete(selected, lambda _: True)
    assert client.delete_requests == [(selected.hash, True)]


def test_peer_content_file_reference_without_manifest_is_preserved(setup):
    client, coordinator, _ = setup
    selected, peer = shared_pair(client)
    client.files[peer.hash] = []
    assert coordinator.delete(selected, lambda _: True)
    assert client.delete_requests == [(selected.hash, False)]
    assert (client.root / "release/shared").exists()


def test_incomplete_peer_staging_and_qb_suffix_are_references(setup):
    client, coordinator, _ = setup
    selected = client.add("staging/movie.!qB", hash="selected")
    peer = client.add(
        "movie",
        hash="retained",
        progress=0.5,
        state="downloading",
        download_path="/downloads/staging",
        content_path="/downloads/staging/movie",
    )
    assert coordinator.delete(selected, lambda _: True)
    assert client.delete_requests == [(selected.hash, False)]
    assert (client.root / "staging/movie.!qB").exists()
    assert client.torrents == [peer]


@pytest.mark.parametrize("failure", ["listing", "manifest", "invalid_name"])
def test_unknown_peer_ownership_fails_closed(setup, monkeypatch, failure):
    client, coordinator, _ = setup
    selected, peer = shared_pair(client)
    if failure == "listing":
        original = client.torrents_info
        monkeypatch.setattr(
            client,
            "torrents_info",
            lambda **kwargs: (
                original(**kwargs) if kwargs else (_ for _ in ()).throw(TimeoutError())
            ),
        )
    elif failure == "manifest":
        del client.files[peer.hash]
    else:
        client.files[peer.hash][0].name = "../unsafe"

    assert not coordinator.delete(selected, lambda _: True)
    assert client.delete_requests == []
    assert (client.root / "release/shared").exists()
    assert coordinator.pending is None


def test_peer_added_at_final_policy_check_is_preserved(setup):
    client, coordinator, _ = setup
    selected = client.add("movie", hash="selected")
    checks = 0

    def allowed(_):
        nonlocal checks
        checks += 1
        if checks == 2:
            client.add("movie", hash="retained")
        return True

    assert coordinator.delete(selected, allowed)
    assert client.delete_requests == [(selected.hash, False)]
    assert (client.root / "movie").exists()


def test_partial_unlink_waits_for_metadata_removal(setup, monkeypatch):
    client, coordinator, clock = setup
    selected, _ = shared_pair(client, partial=True)

    def delayed(torrent_hashes, delete_files):
        assert not delete_files
        client.delete_requests.append((torrent_hashes, delete_files))
        clock.events.append((2, lambda: client.torrents.remove(selected)))
        assert (client.root / "release/owned").exists()

    monkeypatch.setattr(client, "torrents_delete", delayed)
    assert coordinator.delete(selected, lambda _: True)
    assert clock.now == 2
    assert not (client.root / "release/owned").exists()
    assert (client.root / "release/shared").exists()


def test_unlink_failure_keeps_gate_until_retry(setup, monkeypatch):
    client, coordinator, _ = setup
    selected, _ = shared_pair(client, partial=True)
    original = coordinator.storage.unlink
    monkeypatch.setattr(coordinator.storage, "unlink", Mock(side_effect=PermissionError("denied")))

    assert not coordinator.delete(selected, lambda _: True)
    assert coordinator.pending is not None
    assert coordinator.blocked
    assert (client.root / "release/owned").exists()
    assert (client.root / "release/shared").exists()

    monkeypatch.setattr(coordinator.storage, "unlink", original)
    coordinator.begin_pass()
    assert coordinator.pending is None
    assert not (client.root / "release/owned").exists()
    assert len(client.delete_requests) == 1


def test_new_reference_while_waiting_prevents_local_unlink(setup, monkeypatch):
    client, coordinator, clock = setup
    selected, _ = shared_pair(client, partial=True)

    def delayed(torrent_hashes, delete_files):
        assert not delete_files
        client.delete_requests.append((torrent_hashes, delete_files))

        def finish():
            client.torrents.remove(selected)
            client.add("release/owned", hash="new-reference")

        clock.events.append((2, finish))

    monkeypatch.setattr(client, "torrents_delete", delayed)
    assert coordinator.delete(selected, lambda _: True)
    assert (client.root / "release/owned").exists()
    assert (client.root / "release/shared").exists()
    assert coordinator.pending is None


@pytest.mark.parametrize("replacement", ["file", "symlink", "directory"])
def test_replaced_owned_path_is_not_unlinked(setup, monkeypatch, replacement):
    client, coordinator, _ = setup
    selected, _ = shared_pair(client, partial=True)
    path = client.root / "release/owned"
    original = client.torrents_delete

    def replace(torrent_hashes, delete_files):
        original(torrent_hashes, delete_files)
        path.rename(path.with_name("original"))
        if replacement == "file":
            path.write_bytes(b"new file")
        elif replacement == "directory":
            path.mkdir()
        else:
            path.symlink_to(path.with_name("shared"))

    monkeypatch.setattr(client, "torrents_delete", replace)
    assert not coordinator.delete(selected, lambda _: True)
    assert path.exists()
    assert (client.root / "release/shared").read_bytes() == b"data"
    assert coordinator.pending is not None


def test_parent_replaced_after_inspection_cannot_redirect_unlink(setup, monkeypatch):
    client, coordinator, _ = setup
    selected, _ = shared_pair(client, partial=True)
    path = client.root / "release/owned"
    outside = client.root / "outside"
    outside.mkdir()
    (outside / "owned").write_bytes(b"keep")
    original = coordinator.storage.inspect

    def replace_parent(current):
        info = original(current)
        if current == path and selected not in client.torrents:
            path.parent.rename(client.root / "detached")
            path.parent.symlink_to(outside, target_is_directory=True)
        return info

    monkeypatch.setattr(coordinator.storage, "inspect", replace_parent)
    assert not coordinator.delete(selected, lambda _: True)
    assert (outside / "owned").read_bytes() == b"keep"
    assert (client.root / "detached/owned").exists()
    assert coordinator.pending is not None


def test_ambiguous_metadata_request_can_be_confirmed_without_repeating_it(setup, monkeypatch):
    client, coordinator, _ = setup
    selected, _ = shared_pair(client, partial=True)
    original = client.torrents_delete

    def lost_response(torrent_hashes, delete_files):
        original(torrent_hashes, delete_files)
        raise TimeoutError("response lost")

    monkeypatch.setattr(client, "torrents_delete", lost_response)
    assert not coordinator.delete(selected, lambda _: True)
    assert (client.root / "release/owned").exists()
    coordinator.begin_pass()
    assert coordinator.pending is None
    assert not (client.root / "release/owned").exists()
    assert (client.root / "release/shared").exists()
    assert client.delete_requests == [(selected.hash, False)]


def test_shared_content_dry_run_does_not_credit_shared_torrent_size(tmp_path, monkeypatch):
    Clock(monkeypatch)
    client = FileClient(tmp_path)
    selected, _ = shared_pair(client, category="protected")
    selected.num_complete = 100
    unique = client.add("unique")
    config = configuration(
        client,
        maintain_free_space={
            "enabled": True,
            "categories": ["movies"],
            "free_space_threshold_gibi": 1,
            "free_space_path": "/downloads",
        },
    )
    coordinator = measured_coordinator(client, config, monkeypatch)

    with capture_logs() as logs:
        maintain_free_space(
            cast(qbittorrentapi.Client, client),
            config,
            dry_run=True,
            deletion_coordinator=coordinator,
        )

    estimates = [
        event["estimated_reclaimed_bytes"]
        for event in logs
        if event["event"] == "would verify content deletion"
    ]
    assert estimates == [0, GIB]
    assert client.delete_requests == []
    assert unique in client.torrents


@pytest.mark.parametrize("job", ["remove_unregistered", "remove_stopped", "maintain_free_space"])
def test_all_content_removal_jobs_preserve_shared_content(tmp_path, monkeypatch, job):
    Clock(monkeypatch)
    client = FileClient(tmp_path)
    selected, peer = shared_pair(client, category="protected")
    selected.state = "stoppedUP"
    selected.trackers[0].msg = "Unregistered torrent"
    options = {"enabled": True, "categories": ["movies"]}
    if job == "maintain_free_space":
        options.update(free_space_threshold_gibi=1, free_space_path="/downloads")
    elif job == "remove_stopped":
        options["on_delete"] = "RemoveWithContent"
    config = configuration(client, **{job: options})
    coordinator = measured_coordinator(client, config, monkeypatch)

    run(cast(qbittorrentapi.Client, client), config, {}, deletion_coordinator=coordinator)

    assert client.delete_requests == [(selected.hash, False)]
    assert client.torrents == [peer]
    assert (client.root / "release/shared").exists()


def test_shared_metadata_removal_does_not_satisfy_free_space_target(tmp_path, monkeypatch):
    Clock(monkeypatch)
    client = FileClient(tmp_path)
    selected, _ = shared_pair(client, category="protected")
    selected.num_complete = 100
    unique = client.add("unique")
    config = configuration(
        client,
        maintain_free_space={
            "enabled": True,
            "categories": ["movies"],
            "free_space_threshold_gibi": 1,
            "free_space_path": "/downloads",
        },
    )
    coordinator = measured_coordinator(client, config, monkeypatch)

    maintain_free_space(
        cast(qbittorrentapi.Client, client), config, deletion_coordinator=coordinator
    )

    assert client.delete_requests == [(selected.hash, False), (unique.hash, True)]
    assert (client.root / "release/shared").exists()
