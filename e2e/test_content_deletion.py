import errno
import subprocess

import pytest

from e2e.support import recheck_complete_torrents, wait_for_torrents
from tarr.config import RemoveUnregisteredConfig
from tarr.main import remove_unregistered


def test_cli():
    result = subprocess.run(["tarr", "--help"], text=True, capture_output=True, timeout=10)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "usage: tarr" in result.stdout


def test_downloads_are_read_only(storage):
    with pytest.raises(OSError) as error:
        (storage / "must-not-write").write_bytes(b"no")
    assert error.value.errno == errno.EROFS


def test_hard_links(client, coordinator, storage, add_torrent):
    for name in ("one.bin", "two.bin"):
        add_torrent(name + ".torrent")
    recheck_complete_torrents(client, 2)
    assert (storage / "one.bin").stat().st_ino == (storage / "two.bin").stat().st_ino
    for name in ("one.bin", "two.bin"):
        torrent = next(t for t in client.torrents_info() if t.name == name)
        assert coordinator.delete(torrent, lambda current: True)
        assert coordinator.pending is None
        assert not (storage / name).exists()
        if name == "one.bin":
            assert (storage / "two.bin").exists()
        assert coordinator.free_space(storage) > 0
    assert not client.torrents_info()


def test_multi_file_release(client, coordinator, storage, add_torrent):
    add_torrent("release.torrent")
    torrent = recheck_complete_torrents(client, 1)[0]
    assert coordinator.delete(torrent, lambda current: True)
    assert coordinator.pending is None
    assert not client.torrents_info()
    assert not (storage / "release/first").exists()
    assert not (storage / "release/second").exists()


def test_nested_single_file(client, coordinator, storage, add_torrent):
    add_torrent("nested.torrent")
    torrent = recheck_complete_torrents(client, 1)[0]
    assert torrent.content_path == "/downloads/nested/movie.bin"
    assert [file.name for file in torrent.files] == ["nested/movie.bin"]
    assert coordinator.delete(torrent, lambda current: True)
    assert coordinator.pending is None
    assert not client.torrents_info()
    assert not (storage / "nested/movie.bin").exists()


def test_incomplete_shared_directory(client, coordinator, storage, add_torrent):
    add_torrent("flat.torrent", content_layout="NoSubfolder")
    torrent = wait_for_torrents(
        client, lambda torrents: len(torrents) == 1 and torrents[0].state == "stoppedDL"
    )[0]
    client.torrents_file_priority(torrent_hash=torrent.hash, file_ids=1, priority=0)
    torrent = client.torrents_info(torrent_hashes=torrent.hash)[0]
    assert torrent.progress < 1
    assert {file.name for file in torrent.files} == {"flat-first", "flat-second"}
    assert (storage / "flat-first").exists()
    assert coordinator.delete(torrent, lambda current: True)
    assert coordinator.pending is None
    assert not client.torrents_info()
    assert not (storage / "flat-first").exists()
    assert (storage / "unrelated").exists()
    assert (storage / "empty.torrent").exists()


def test_incomplete_staging_download(client, coordinator, staging, add_torrent):
    add_torrent("partial.torrent", download_path=str(staging), is_stopped=False)
    torrent = wait_for_torrents(
        client,
        lambda torrents: len(torrents) == 1 and torrents[0].state in {"stalledDL", "downloading"},
    )[0]
    assert torrent.progress < 1
    assert (staging / "partial.bin.!qB").exists()
    assert coordinator.delete(torrent, lambda current: True)
    assert coordinator.pending is None
    assert not client.torrents_info()
    assert not (staging / "partial.bin.!qB").exists()


@pytest.mark.usefixtures("unregistered_tracker")
def test_zero_data_unregistered_torrent(client, coordinator, storage, staging, add_torrent):
    add_torrent("empty.torrent", download_path=str(staging), is_stopped=False)
    torrent = wait_for_torrents(
        client,
        lambda torrents: (
            len(torrents) == 1
            and torrents[0].state == "stalledDL"
            and any(
                t.status not in {0, 1, 3} and t.msg == "Unregistered torrent"
                for t in torrents[0].trackers
            )
        ),
    )[0]
    assert torrent.progress == torrent.completed == torrent.downloaded == 0
    assert all(file.progress == 0 for file in torrent.files)
    assert not (storage / "empty.bin").exists()
    assert not (staging / "empty.bin.!qB").exists()
    assert coordinator._snapshot(torrent, None) == set()
    first_seen = {}
    remove_unregistered(
        client,
        RemoveUnregisteredConfig(delay_minutes=0),
        first_seen,
        deletion_coordinator=coordinator,
    )
    assert coordinator.pending is None
    assert not client.torrents_info()
    assert not first_seen
