import datetime
import os
from importlib import import_module
from pathlib import Path
from unittest.mock import Mock

import pytest
from pydantic import ValidationError

from tarr.config import Config, ContentDeletionConfig, MaintainFreeSpaceConfig
from tarr.deletion import ContentDeletionCoordinator, StorageView
from tarr.main import run
from tests.deletion_helpers import (
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
    config = configuration(client)
    coordinator = ContentDeletionCoordinator(client, config.content_deletion)
    return client, config, coordinator, clock


def test_requires_both_file_and_torrent_disappearance(setup):
    client, _, coordinator, clock = setup
    torrent = client.add()
    client.on_delete = lambda torrent_hash: (client.root / torrent_hash).unlink()

    assert not coordinator.delete(torrent, lambda _: True)
    assert coordinator.pending is not None
    assert clock.now == 4

    client.torrents.clear()
    coordinator.begin_pass()
    assert coordinator.pending is None


def test_partial_multifile_removal_waits_but_empty_directory_is_allowed(setup):
    client, _, coordinator, clock = setup
    torrent = client.add("release/one", content_path="/downloads/release")
    set_manifest(client, torrent, ["release/one", "release/two"])

    def partial(_torrent_hash):
        client.torrents.clear()
        (client.root / "release/one").unlink()
        clock.events.append((2, lambda: (client.root / "release/two").unlink()))

    client.on_delete = partial
    assert coordinator.delete(torrent, lambda _: True)
    assert clock.now == 2
    assert (client.root / "release").is_dir()


def test_files_in_reported_content_directory_are_tracked(setup):
    client, _, coordinator, _ = setup
    torrent = client.add("release/one", content_path="/downloads/release")
    auxiliary = client.root / "release/.unwanted/parts"
    auxiliary.parent.mkdir()
    auxiliary.write_bytes(b"parts")

    assert not coordinator.delete(torrent, lambda _: True)
    assert auxiliary in coordinator.pending.paths
    auxiliary.unlink()
    coordinator.begin_pass()
    assert coordinator.pending is None


@pytest.mark.parametrize("progress", [0, 0.5, 1])
@pytest.mark.parametrize("content_path", ["/downloads/release", "/downloads/release/movie.mkv"])
def test_nested_single_file_content_path_is_supported(setup, progress, content_path):
    client, _, coordinator, _ = setup
    torrent = client.add(
        "release/movie.mkv",
        content_path=content_path,
        progress=progress,
        state="stalledDL" if progress < 1 else "stalledUP",
    )

    assert coordinator.delete(torrent, lambda _: True)
    assert client.deleted == [torrent.hash]
    assert client.stopped == ([torrent.hash] if progress < 1 else [])
    assert coordinator.pending is None


@pytest.mark.parametrize(
    "content_path",
    ["/downloads/release/other.mkv", "/downloads/other/movie.mkv", "/downloads/release/movie.mkv"],
)
@pytest.mark.parametrize("multifile", [False, True])
def test_content_path_layout_does_not_block_manifest_file_removal(setup, content_path, multifile):
    client, _, coordinator, _ = setup
    torrent = client.add("release/one.mkv", content_path=content_path)
    if multifile:
        set_manifest(client, torrent, ["release/movie.mkv", "release/two.mkv"])

    assert coordinator.delete(torrent, lambda _: True)
    assert client.deleted == [torrent.hash]


def test_snapshot_contains_only_files_that_exist(setup):
    client, _, coordinator, _ = setup
    torrent = client.add("one", content_path="/downloads")
    set_manifest(client, torrent, ["one", "missing"])
    (client.root / "missing").unlink()
    partial = client.root / "missing.!qB"
    partial.write_bytes(b"partial")
    (client.root / "unrelated").write_bytes(b"keep")

    assert coordinator._snapshot(torrent, None) == {client.root / "one", partial}


@pytest.mark.parametrize("progress", [0, 0.5, 1])
@pytest.mark.parametrize("manifest", [False, True])
def test_absent_content_only_requires_torrent_disappearance(setup, progress, manifest):
    client, _, coordinator, clock = setup
    torrent = client.add(progress=progress, downloaded=100, completed=100)
    (client.root / torrent.name).unlink()
    if not manifest:
        client.files[torrent.hash] = []
    client.on_delete = lambda _: None
    clock.events.append((2, client.torrents.clear))

    assert coordinator.delete(torrent, lambda _: True)
    assert client.deleted == [torrent.hash]
    assert clock.now == 2
    assert coordinator.pending is None


def test_empty_snapshot_still_checks_mount_during_confirmation(setup, empty_torrent):
    client, _, coordinator, _ = setup

    def remove_mount(_torrent_hash):
        client.torrents.clear()
        client.root.rename(client.root.with_name("detached"))

    client.on_delete = remove_mount
    assert not coordinator.delete(empty_torrent, lambda _: True)
    assert coordinator.pending is not None
    assert coordinator.pending.paths == set()
    assert coordinator.blocked


def test_reported_content_is_tracked_without_a_manifest(setup):
    client, _, coordinator, clock = setup
    torrent = client.add("renamed/file", content_path="/downloads/renamed")
    client.files[torrent.hash] = []
    path = client.root / "renamed/file"
    client.on_delete = lambda _: client.torrents.clear()
    clock.events.append((2, path.unlink))

    assert coordinator.delete(torrent, lambda _: True)
    assert clock.now == 2


def test_content_file_does_not_imply_ownership_of_siblings(setup):
    client, _, coordinator, _ = setup
    torrent = client.add("shared/movie", content_path="/downloads/shared/movie")
    unrelated = client.root / "shared/other"
    unrelated.write_bytes(b"keep")

    assert coordinator.delete(torrent, lambda _: True)
    assert unrelated.exists()


def test_ambiguous_api_failure_retains_guard_and_is_never_retried(setup):
    client, _, coordinator, _ = setup
    torrent = client.add()
    other = client.add("other")
    client.on_delete = Mock(side_effect=TimeoutError("response lost"))

    assert not coordinator.delete(torrent, lambda _: True)
    assert not coordinator.delete(other, lambda _: True)
    coordinator.begin_pass()
    assert not coordinator.delete(other, lambda _: True)
    assert client.deleted == [torrent.hash]
    assert coordinator.pending is not None


def test_pending_state_survives_passes_but_not_a_new_coordinator(setup):
    client, config, coordinator, _ = setup
    torrent = client.add()
    client.on_delete = lambda _: None
    coordinator.delete(torrent, lambda _: True)
    coordinator.begin_pass()
    assert coordinator.pending is not None
    assert ContentDeletionCoordinator(client, config.content_deletion).pending is None


def test_recreated_path_keeps_gate_closed(setup):
    client, _, coordinator, _ = setup
    torrent = client.add()

    def recreate(torrent_hash):
        client.finish(torrent_hash)
        (client.root / torrent_hash).write_bytes(b"new")

    client.on_delete = recreate
    assert not coordinator.delete(torrent, lambda _: True)
    assert coordinator.pending is not None


@pytest.mark.parametrize("failure", ["missing_root", "replaced_root", "permission", "symlink"])
def test_filesystem_failure_does_not_look_like_confirmation(setup, monkeypatch, failure):
    client, _, _, _ = setup
    root = client.root / "mounted"
    root.mkdir()
    client.root = root
    config = configuration(client)
    coordinator = ContentDeletionCoordinator(client, config.content_deletion)
    torrent = client.add()
    moved = root.with_name("detached")

    def remove_mount(torrent_hash):
        client.torrents.clear()
        if failure == "permission":
            original = coordinator.storage.inspect

            def denied(_path):
                raise PermissionError("cannot traverse mount")

            monkeypatch.setattr(coordinator.storage, "inspect", denied)
            assert original(root / torrent_hash) is not None
        else:
            root.rename(moved)
            if failure == "replaced_root":
                root.mkdir()
            elif failure == "symlink":
                root.symlink_to(moved, target_is_directory=True)

    client.on_delete = remove_mount
    assert not coordinator.delete(torrent, lambda _: True)
    assert coordinator.pending is not None
    assert coordinator.blocked


@pytest.mark.parametrize("failure", ["symlink_file", "symlink_parent", "unsafe_name"])
def test_unverifiable_content_is_not_deleted(setup, failure):
    client, _, coordinator, _ = setup
    torrent = client.add()
    path = client.root / torrent.name
    if failure == "symlink_file":
        path.unlink()
        path.symlink_to(client.root / "target")
    elif failure == "symlink_parent":
        target = client.root / "target"
        target.mkdir()
        (target / "file").write_bytes(b"data")
        (client.root / "link").symlink_to(target, target_is_directory=True)
        client.files[torrent.hash][0].name = "link/file"
        torrent.content_path = "/downloads/link"
    else:
        client.files[torrent.hash][0].name = "../outside"

    assert not coordinator.delete(torrent, lambda _: True)
    assert client.deleted == []


@pytest.fixture
def empty_torrent(setup):
    client, _, _, _ = setup
    torrent = client.add(state="stalledDL", progress=0, completed=0, downloaded=0)
    client.files[torrent.hash][0].progress = 0
    (client.root / torrent.name).unlink()
    return torrent


@pytest.mark.parametrize("tracker_status, tracker_msg", [(0, ""), (1, ""), (1, "timed out")])
def test_unregistered_evidence_survives_our_stop(setup, empty_torrent, tracker_status, tracker_msg):
    client, _, coordinator, _ = setup
    empty_torrent.trackers[0].status = 5
    empty_torrent.trackers[0].msg = "Unregistered torrent"
    config = configuration(client, remove_unregistered={"enabled": True})

    def stop(_torrent_hash):
        empty_torrent.state = "stoppedDL"
        empty_torrent.trackers[0].status = tracker_status
        empty_torrent.trackers[0].msg = tracker_msg

    client.on_stop = stop
    first_seen = {}
    run(client, config, first_seen, deletion_coordinator=coordinator)

    assert client.deleted == client.stopped == [empty_torrent.hash]
    assert client.started == client.forced == []
    assert not first_seen
    assert coordinator.pending is None


@pytest.mark.parametrize("guard", ["category", "queued", "arr_error"])
def test_unregistered_stop_preserves_category_and_arr_checks(
    setup, empty_torrent, monkeypatch, guard
):
    client, _, coordinator, _ = setup
    empty_torrent.trackers[0].msg = "Unregistered torrent"
    config = configuration(
        client,
        arrs={"movies": {"host": "http://radarr:7878", "api_key": "test"}},
        remove_unregistered={
            "enabled": True,
            "categories": [{"name": "movies", "arr_verify_removal": ["movies"]}],
        },
    )
    check = Mock(return_value=False)
    monkeypatch.setattr("tarr.arr.torrent_in_queue", check)

    def stop(_torrent_hash):
        empty_torrent.state = "stoppedDL"
        empty_torrent.trackers[0].status = 0
        empty_torrent.trackers[0].msg = ""
        if guard == "category":
            empty_torrent.category = "protected"
        elif guard == "queued":
            check.return_value = True
        else:
            check.side_effect = TimeoutError("Arr unavailable")

    client.on_stop = stop
    first_seen = {}
    run(client, config, first_seen, deletion_coordinator=coordinator)

    assert client.deleted == []
    assert client.stopped == client.started == [empty_torrent.hash]
    assert empty_torrent.hash in first_seen
    assert check.call_count == (1 if guard == "category" else 2)


@pytest.mark.parametrize("state", ["stoppedDL", "stalledUP"])
def test_unregistered_rechecks_tracker_when_we_did_not_stop(setup, monkeypatch, state):
    client, _, coordinator, _ = setup
    torrent = client.add(state=state, progress=0.5 if state == "stoppedDL" else 1)
    torrent.trackers[0].msg = "Unregistered torrent"
    config = configuration(client, remove_unregistered={"enabled": True})
    snapshot = coordinator._snapshot

    def recover(current, target_device):
        paths = snapshot(current, target_device)
        torrent.trackers[0].status = 2
        torrent.trackers[0].msg = "working"
        return paths

    monkeypatch.setattr(coordinator, "_snapshot", recover)
    run(client, config, {}, deletion_coordinator=coordinator)

    assert client.stopped == client.deleted == []


def test_unregistered_rechecks_tracker_before_stopping(setup, empty_torrent, monkeypatch):
    client, _, coordinator, _ = setup
    empty_torrent.trackers[0].msg = "Unregistered torrent"
    config = configuration(client, remove_unregistered={"enabled": True})
    refresh = coordinator._torrent

    def recover(torrent_hash):
        empty_torrent.trackers[0].msg = "working"
        return refresh(torrent_hash)

    monkeypatch.setattr(coordinator, "_torrent", recover)
    run(client, config, {}, deletion_coordinator=coordinator)

    assert client.stopped == client.deleted == []


def test_unregistered_torrent_without_downloaded_data_is_removed(setup, empty_torrent):
    client, _, coordinator, clock = setup
    empty_torrent.trackers[0].msg = "Unregistered torrent"
    config = configuration(client, remove_unregistered={"enabled": True, "delay_minutes": 15})
    first_seen = {empty_torrent.hash: datetime.datetime.now() - datetime.timedelta(minutes=16)}

    run(client, config, first_seen, deletion_coordinator=coordinator)

    assert client.deleted == [empty_torrent.hash]
    assert client.stopped == [empty_torrent.hash]
    assert client.started == client.forced == []
    assert not client.torrents
    assert not first_seen
    assert coordinator.pending is None
    assert clock.now == 0


@pytest.mark.parametrize(
    "tracker_msg",
    [
        "Unregistered torrent",
        "Torrent does not exist on this tracker",
        "Torrent has been deleted",
        "Torrent not registered with this tracker.",
    ],
)
@pytest.mark.parametrize("state", ["stalledUP", "stoppedDL", "stalledDL"])
def test_unregistered_messages_allow_removal_after_delay(setup, tracker_msg, state):
    client, _, coordinator, _ = setup
    torrent = client.add(state=state, progress=1 if state == "stalledUP" else 0.5)
    torrent.trackers[0].status = 5
    torrent.trackers[0].msg = tracker_msg
    config = configuration(client, remove_unregistered={"enabled": True, "delay_minutes": 15})
    first_seen = {torrent.hash: datetime.datetime.now() - datetime.timedelta(minutes=16)}

    run(client, config, first_seen, deletion_coordinator=coordinator)

    assert client.deleted == [torrent.hash]
    assert client.stopped == ([torrent.hash] if state == "stalledDL" else [])
    assert not (client.root / torrent.name).exists()
    assert not client.torrents
    assert not first_seen
    assert coordinator.pending is None


@pytest.mark.parametrize("state", ["stalledDL", "stoppedDL"])
@pytest.mark.parametrize("isolated", [False, True])
def test_zero_data_removal_checks_staging_and_isolated_directories(
    setup, empty_torrent, state, isolated
):
    client, _, coordinator, _ = setup
    staging = client.root / "incomplete"
    staging.mkdir()
    empty_torrent.state = state
    empty_torrent.download_path = "/downloads/incomplete"
    client.preferences = {"temp_path_enabled": True, "temp_path": "/downloads/incomplete"}
    if isolated:
        set_manifest(client, empty_torrent, ["release/one", "release/two"])
        for file in client.files[empty_torrent.hash]:
            (client.root / file.name).unlink()
        empty_torrent.content_path = "/downloads/incomplete/release"
    else:
        empty_torrent.content_path = f"/downloads/incomplete/{empty_torrent.name}"

    assert coordinator.delete(empty_torrent, lambda _: True)
    assert client.deleted == [empty_torrent.hash]
    assert client.stopped == ([empty_torrent.hash] if state == "stalledDL" else [])
    assert coordinator.pending is None


@pytest.mark.parametrize("field", ["completed", "downloaded"])
@pytest.mark.parametrize("value", [1, None, False, -1, float("nan")])
def test_absent_data_does_not_require_zero_byte_counters(setup, empty_torrent, field, value):
    client, _, coordinator, _ = setup
    setattr(empty_torrent, field, value)

    assert coordinator.delete(empty_torrent, lambda _: True)
    assert client.stopped == client.deleted == [empty_torrent.hash]


@pytest.mark.parametrize("field", ["completed", "downloaded"])
def test_absent_data_does_not_require_byte_counters(setup, empty_torrent, field):
    client, _, coordinator, _ = setup
    delattr(empty_torrent, field)

    assert coordinator.delete(empty_torrent, lambda _: True)
    assert client.deleted == [empty_torrent.hash]


@pytest.mark.parametrize("progress", [0.5, 1, None, False, float("nan")])
def test_absent_data_does_not_require_zero_file_progress(setup, empty_torrent, progress):
    client, _, coordinator, _ = setup
    client.files[empty_torrent.hash][0].progress = progress

    assert coordinator.delete(empty_torrent, lambda _: True)
    assert client.deleted == [empty_torrent.hash]


@pytest.mark.parametrize("failure", ["missing_root", "symlink"])
def test_absent_data_still_requires_accessible_storage(setup, empty_torrent, failure):
    client, _, coordinator, _ = setup
    if failure == "missing_root":
        client.root.rmdir()
    else:
        (client.root / empty_torrent.name).symlink_to(client.root / "missing")

    assert not coordinator.delete(empty_torrent, lambda _: True)
    assert client.stopped == client.deleted == []


@pytest.mark.parametrize("missing", ["save", "staging"])
def test_missing_download_subdirectory_is_not_a_missing_mount(setup, empty_torrent, missing):
    client, _, coordinator, _ = setup
    if missing == "save":
        empty_torrent.save_path = "/downloads/missing"
        empty_torrent.content_path = f"/downloads/missing/{empty_torrent.name}"
    else:
        client.preferences = {"temp_path_enabled": True, "temp_path": "/downloads/missing"}

    assert coordinator.delete(empty_torrent, lambda _: True)
    assert client.deleted == [empty_torrent.hash]


def test_absent_data_can_be_removed_during_free_space_cleanup(setup, empty_torrent):
    client, _, coordinator, _ = setup
    assert coordinator.delete(empty_torrent, lambda _: True, free_space_path=Path("/downloads"))
    assert client.deleted == [empty_torrent.hash]


def test_zero_data_deletion_waits_for_torrent_disappearance(setup, empty_torrent):
    client, _, coordinator, clock = setup
    client.on_delete = lambda _: None

    assert not coordinator.delete(empty_torrent, lambda _: True)
    assert coordinator.pending is not None
    assert coordinator.blocked
    assert clock.now == 4
    client.torrents.clear()
    coordinator.begin_pass()
    assert not coordinator.unresolved


def test_data_appearing_while_stopping_is_tracked(setup, empty_torrent):
    client, _, coordinator, _ = setup
    path = client.root / empty_torrent.name

    def stop(_torrent_hash):
        empty_torrent.state = "stoppedDL"
        empty_torrent.progress = 0.5
        empty_torrent.completed = empty_torrent.downloaded = 2
        client.files[empty_torrent.hash][0].progress = 0.5
        path.write_bytes(b"partial")

    client.on_stop = stop
    client.on_delete = lambda _: client.torrents.clear()

    assert not coordinator.delete(empty_torrent, lambda _: True)
    assert coordinator.pending is not None
    assert path in coordinator.pending.paths
    path.unlink()
    coordinator.begin_pass()
    assert coordinator.pending is None


def test_zero_data_dry_run_does_not_stop_or_delete(setup, empty_torrent):
    client, _, coordinator, clock = setup
    assert coordinator.delete(empty_torrent, lambda _: True, dry_run=True)
    assert client.stopped == client.deleted == []
    assert coordinator.pending is None
    assert clock.now == 0


def test_different_filesystem_is_not_deleted(setup, monkeypatch):
    client, _, coordinator, _ = setup
    torrent = client.add()
    actual = coordinator.storage.available
    monkeypatch.setattr(coordinator.storage, "available", lambda path: (0, actual(path)[1] + 1))
    assert not coordinator.delete(torrent, lambda _: True, free_space_path=Path("/downloads"))
    assert client.deleted == []


def test_stops_active_incomplete_torrent_and_refreshes_manifest(setup):
    client, _, coordinator, clock = setup
    torrent = client.add(
        "release/file", state="downloading", progress=0.5, content_path="/downloads/release"
    )
    partial = client.root / "release/file.!qB"
    (client.root / "release/file").rename(partial)

    def stop(_torrent_hash):
        clock.events.append((1, lambda: setattr(torrent, "state", "stoppedDL")))

    def remove(_torrent_hash):
        assert torrent.state == "stoppedDL"
        partial.unlink()
        client.torrents.clear()

    client.on_stop = stop
    client.on_delete = remove
    assert coordinator.delete(torrent, lambda _: True)
    assert client.stopped == [torrent.hash]
    assert client.started == client.forced == []
    assert clock.now == 1


@pytest.mark.parametrize("forced", [False, True])
def test_abandoned_deletion_restores_only_activity_stopped_by_tarr(setup, forced):
    client, _, coordinator, _ = setup
    torrent = client.add(state="downloading", progress=0.5, force_start=forced)
    allowed = Mock(side_effect=[True, False])

    assert not coordinator.delete(torrent, allowed)
    assert client.stopped == [torrent.hash]
    assert client.deleted == []
    assert client.forced == ([torrent.hash] if forced else [])
    assert client.started == ([] if forced else [torrent.hash])


def test_stop_timeout_authorizes_no_deletion_and_restores_activity(setup):
    client, _, coordinator, clock = setup
    torrent = client.add(state="downloading", progress=0.5)
    client.on_stop = lambda _: None
    assert not coordinator.delete(torrent, lambda _: True)
    assert client.deleted == []
    assert client.started == [torrent.hash]
    assert coordinator.pending is None
    assert clock.now == 4


def test_incomplete_staging_path_is_verified(setup):
    client, _, _, _ = setup
    staging = client.root / "staging"
    staging.mkdir()
    config = configuration(client)
    config.content_deletion.path_mappings.append(f"/incomplete:{staging}")
    coordinator = ContentDeletionCoordinator(client, config.content_deletion)
    torrent = client.add(state="stoppedDL", progress=0.5, download_path="/incomplete")
    (client.root / torrent.name).rename(staging / (torrent.name + ".!qB"))

    def remove(_torrent_hash):
        (staging / (torrent.name + ".!qB")).unlink()
        client.torrents.clear()

    client.on_delete = remove
    assert coordinator.delete(torrent, lambda _: True)


@pytest.mark.parametrize("priority", [0, 1])
@pytest.mark.parametrize("content_path", ["/downloads", "/downloads/shared"])
def test_incomplete_shared_directory_tracks_only_torrent_files(setup, priority, content_path):
    client, _, coordinator, clock = setup
    shared = client.root / "shared"
    shared.mkdir()
    torrent = client.add(
        "shared/one",
        progress=0.5,
        state="downloading",
        save_path="/downloads/shared",
        content_path=content_path,
    )
    set_manifest(client, torrent, ["shared/one", "shared/two"])
    for file in client.files[torrent.hash]:
        file.name = Path(file.name).name
    client.files[torrent.hash][1].priority = priority
    unrelated = shared / "unrelated"
    unrelated.write_bytes(b"keep")
    second = shared / "two"

    def remove(_torrent_hash):
        client.torrents.clear()
        (shared / "one").unlink()
        clock.events.append((2, second.unlink))

    client.on_delete = remove
    assert coordinator.delete(torrent, lambda _: True)
    assert client.stopped == client.deleted == [torrent.hash]
    assert unrelated.exists()
    assert clock.now == 2


def test_dry_run_does_not_stop_wait_delete_or_change_pending_state(setup):
    client, _, coordinator, clock = setup
    torrent = client.add(state="downloading", progress=0.5)
    assert coordinator.delete(torrent, lambda _: True, dry_run=True)
    assert coordinator.pending is None
    assert client.stopped == client.deleted == []
    assert clock.now == 0


def test_shared_gate_blocks_all_jobs_then_recovers(setup, monkeypatch):
    client, _, _, clock = setup
    unregistered = client.add("unregistered", category="unregistered")
    unregistered.trackers[0].msg = "unregistered torrent"
    stopped = client.add("stopped", category="stopped", state="stoppedUP")
    client.add("free_space", category="movies")
    config = configuration(
        client,
        remove_unregistered={"enabled": True, "categories": ["unregistered"]},
        remove_stopped={
            "enabled": True,
            "categories": ["stopped"],
            "on_delete": "RemoveWithContent",
        },
        maintain_free_space={
            "enabled": True,
            "free_space_threshold_gibi": 1,
            "free_space_path": "/downloads",
        },
        set_seed_limits={"enabled": True},
    )
    coordinator = measured_coordinator(client, config, monkeypatch)
    client.on_delete = lambda _: None
    seed_limits = Mock()
    monkeypatch.setattr(import_module("tarr.main"), "set_seed_limits", seed_limits)
    first_seen = {}
    stopped_seen = {}

    for _ in range(2):
        run(
            client,
            config,
            first_seen,
            stopped_first_seen=stopped_seen,
            deletion_coordinator=coordinator,
        )
    assert client.deleted == [unregistered.hash]
    assert coordinator.pending is not None
    assert seed_limits.call_count == 2
    assert clock.now == 8

    client.finish(unregistered.hash)
    client.on_delete = client.finish
    run(
        client,
        config,
        first_seen,
        stopped_first_seen=stopped_seen,
        deletion_coordinator=coordinator,
    )
    assert client.deleted == [unregistered.hash, stopped.hash, "free_space"]
    assert coordinator.pending is None


def test_torrent_only_removal_continues_while_content_deletion_is_pending(setup):
    client, _, coordinator, _ = setup
    pending = client.add("pending")
    client.on_delete = lambda _: None
    coordinator.delete(pending, lambda _: True)
    stopped = client.add("stopped", state="stoppedUP")
    config = configuration(client, remove_stopped={"enabled": True, "on_delete": "Remove"})

    run(client, config, {}, deletion_coordinator=coordinator)

    stopped.delete.assert_called_once_with(delete_files=False)
    assert client.deleted == [pending.hash]


def test_direct_filesystem_measurement_uses_available_blocks(setup):
    client, _, coordinator, _ = setup
    available = coordinator.free_space(Path("/downloads"))
    usage = os.statvfs(client.root)
    assert abs(available - usage.f_bavail * usage.f_frsize) < 1024**2


@pytest.mark.parametrize("mapping", ["relative", "/", "/downloads:relative", "/downloads/../other"])
def test_config_rejects_unsafe_storage_mapping(mapping):
    with pytest.raises(ValidationError):
        ContentDeletionConfig(path_mappings=[mapping])


def test_conflicting_mappings_and_invalid_timing_are_rejected():
    with pytest.raises(ValidationError, match="multiple tarr paths"):
        ContentDeletionConfig(path_mappings=["/downloads:/data", "/downloads:/other"])
    for fields in (
        {"timeout_seconds": 0},
        {"poll_interval_seconds": 0},
        {"timeout_seconds": 1, "poll_interval_seconds": 2},
    ):
        with pytest.raises(ValidationError):
            ContentDeletionConfig.model_validate(fields)


def test_enabled_deletion_requires_mappings_but_torrent_only_removal_does_not(setup):
    client, _, _, _ = setup
    raw = configuration(client).model_dump()
    raw.pop("content_deletion")
    raw["remove_stopped"] = {"enabled": True, "on_delete": "Remove"}
    Config.model_validate(raw)
    raw["remove_unregistered"] = {"enabled": True}
    with pytest.raises(ValidationError, match="content_deletion.path_mappings"):
        Config.model_validate(raw)


def test_free_space_target_is_required_and_must_be_mapped(setup):
    with pytest.raises(ValidationError, match="free_space_path"):
        MaintainFreeSpaceConfig(enabled=True, free_space_threshold_gibi=1)
    client, _, _, _ = setup
    with pytest.raises(ValidationError, match="no content_deletion.path_mappings"):
        configuration(
            client,
            maintain_free_space={
                "enabled": True,
                "free_space_threshold_gibi": 1,
                "free_space_path": "/elsewhere",
            },
        )


def test_most_specific_mapping_wins_and_prefixes_match_components(tmp_path):
    config = ContentDeletionConfig(
        path_mappings=[
            f"/downloads:{tmp_path}",
            f"/downloads/special:{tmp_path / 'other'}",
        ]
    )
    (tmp_path / "other").mkdir()
    storage = StorageView(config.directory_mappings)
    assert storage.local_path(Path("/downloads/special/file")) == tmp_path / "other/file"
    with pytest.raises(ValueError, match="no content_deletion.path_mappings"):
        storage.local_path(Path("/downloads-other/file"))


@pytest.mark.parametrize("content_path", ["/incomplete/release", "/incomplete/release/file"])
def test_nested_incomplete_content_can_be_in_staging(setup, content_path):
    client, _, _, _ = setup
    staging = client.root / "staging"
    staging.mkdir()
    config = configuration(client)
    config.content_deletion.path_mappings.append(f"/incomplete:{staging}")
    coordinator = ContentDeletionCoordinator(client, config.content_deletion)
    torrent = client.add(
        "release/file",
        state="stoppedDL",
        progress=0.5,
        download_path="/incomplete",
        content_path=content_path,
    )
    (client.root / "release").rename(staging / "release")

    def remove(_torrent_hash):
        (staging / "release/file").unlink()
        client.torrents.clear()

    client.on_delete = remove
    assert coordinator.delete(torrent, lambda _: True)


@pytest.mark.parametrize("progress", [float("nan"), float("inf"), -1, 2, True, None])
def test_invalid_progress_cannot_authorize_removal(setup, progress):
    client, _, coordinator, _ = setup
    torrent = client.add(progress=progress)
    assert not coordinator.delete(torrent, lambda _: True)
    assert client.deleted == []


def test_changed_metadata_before_request_authorizes_no_deletion(setup, monkeypatch):
    client, _, coordinator, _ = setup
    torrent = client.add()
    original = coordinator._snapshot

    def inspect_and_move(current, target_device):
        paths = original(current, target_device)
        client.torrents = [
            type(current)(
                **{
                    **vars(current),
                    "content_path": "/downloads/other-location",
                }
            )
        ]
        return paths

    monkeypatch.setattr(coordinator, "_snapshot", inspect_and_move)
    assert not coordinator.delete(torrent, lambda _: True)
    assert client.deleted == []


def test_single_pass_cli_exits_nonzero_when_confirmation_times_out(setup, monkeypatch):
    client, _, coordinator, _ = setup
    torrent = client.add()
    torrent.trackers[0].msg = "unregistered torrent"
    config = configuration(client, remove_unregistered={"enabled": True})
    client.on_delete = lambda _: None
    main_module = import_module("tarr.main")
    monkeypatch.setattr(main_module, "load_config", lambda _: config)
    monkeypatch.setattr(main_module, "build_client", lambda _: client)
    monkeypatch.setattr(main_module, "ContentDeletionCoordinator", lambda *_: coordinator)
    monkeypatch.setattr("sys.argv", ["tarr"])

    with pytest.raises(SystemExit, match="content deletion remains unconfirmed"):
        main_module.main()
    assert coordinator.pending is not None
    assert client.deleted == [torrent.hash]


def test_daemon_retains_same_coordinator_after_failed_pass(setup, monkeypatch):
    client, config, coordinator, _ = setup
    torrent = client.add()
    client.on_delete = lambda _: None
    coordinator.delete(torrent, lambda _: True)
    main_module = import_module("tarr.main")
    monkeypatch.setattr(main_module, "load_config", lambda _: config)
    monkeypatch.setattr(main_module, "build_client", lambda _: client)
    monkeypatch.setattr(main_module, "ContentDeletionCoordinator", lambda *_: coordinator)
    monkeypatch.setattr("sys.argv", ["tarr", "--daemon"])
    observed = []

    def reconcile(*_args, deletion_coordinator, **_kwargs):
        observed.append(deletion_coordinator)
        if len(observed) == 1:
            raise RuntimeError("later job failed")
        raise KeyboardInterrupt

    monkeypatch.setattr(main_module, "run", reconcile)
    with pytest.raises(KeyboardInterrupt):
        main_module.main()
    assert observed == [coordinator, coordinator]
    assert coordinator.pending is not None
