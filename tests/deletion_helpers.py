from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

from tarr.config import Config
from tarr.deletion import ContentDeletionCoordinator

GIB = 1024**3


class Clock:
    def __init__(self, monkeypatch):
        self.now = 0
        self.events = []
        monkeypatch.setattr("tarr.deletion.time.monotonic", lambda: self.now)
        monkeypatch.setattr("tarr.deletion.time.sleep", self.sleep)

    def sleep(self, seconds):
        self.now += seconds
        ready = [event for event in self.events if event[0] <= self.now]
        self.events = [event for event in self.events if event[0] > self.now]
        for _, callback in ready:
            callback()


class FileClient:
    def __init__(self, root):
        self.root = root
        self.torrents = []
        self.files = {}
        self.deleted = []
        self.stopped = []
        self.started = []
        self.forced = []
        self.free_space = 0
        self.on_delete = self.finish
        self.on_stop = None
        self.preferences = {}

    def add(self, name="movie.mkv", seeders=10, **attributes):
        torrent = SimpleNamespace(
            hash=name,
            name=name,
            state="uploading",
            size=GIB,
            category="movies",
            progress=1.0,
            save_path="/downloads",
            content_path=f"/downloads/{name}",
            download_path="",
            force_start=False,
            seeding_time=120,
            ratio=1,
            uploaded=GIB,
            num_complete=seeders,
            max_seeding_time=0,
            max_ratio=0,
            trackers=[
                SimpleNamespace(url="https://tracker.example/announce", status=2, msg="working")
            ],
            delete=Mock(),
        )
        for key, value in attributes.items():
            setattr(torrent, key, value)
        self.torrents.append(torrent)
        self.files[torrent.hash] = [SimpleNamespace(name=name, size=4, progress=1, priority=1)]
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"data")
        return torrent

    def torrents_info(self, status_filter=None, torrent_hashes=None):
        return [
            torrent
            for torrent in self.torrents
            if (torrent_hashes is None or torrent.hash == torrent_hashes)
            and (status_filter != "completed" or torrent.progress == 1)
        ]

    def torrents_files(self, torrent_hash):
        return self.files[torrent_hash]

    def app_preferences(self):
        return self.preferences

    def sync_maindata(self):
        raise AssertionError("cleanup must not use qBittorrent's cached free-space report")

    def torrents_delete(self, torrent_hashes, delete_files):
        assert delete_files is True
        self.deleted.append(torrent_hashes)
        self.on_delete(torrent_hashes)

    def finish(self, torrent_hash):
        for file in self.files[torrent_hash]:
            (self.root / file.name).unlink(missing_ok=True)
        self.torrents = [torrent for torrent in self.torrents if torrent.hash != torrent_hash]

    def torrents_stop(self, torrent_hashes):
        self.stopped.append(torrent_hashes)
        if self.on_stop:
            self.on_stop(torrent_hashes)
        else:
            for torrent in self.torrents:
                if torrent.hash == torrent_hashes:
                    torrent.state = "stoppedDL"

    def torrents_start(self, torrent_hashes):
        self.started.append(torrent_hashes)

    def torrents_set_force_start(self, torrent_hashes, enable):
        assert enable
        self.forced.append(torrent_hashes)


def configuration(client, **jobs):
    return Config.model_validate(
        {
            "qbittorrent": {"host": "localhost", "username": "user", "password": "pass"},
            "content_deletion": {
                "path_mappings": [f"/downloads:{client.root}"],
                "poll_interval_seconds": 1,
                "timeout_seconds": 4,
            },
            "trackers": [
                {
                    "name": "example",
                    "hosts": ["tracker.example"],
                    "required": {"seed_time_minutes": 1, "ratio": 1},
                    "desired": {"seed_time_minutes": 1, "ratio": 1},
                }
            ],
            **jobs,
        }
    )


def measured_coordinator(client, config, monkeypatch):
    coordinator = ContentDeletionCoordinator(client, config.content_deletion)
    available = coordinator.storage.available
    monkeypatch.setattr(
        coordinator.storage, "available", lambda path: (client.free_space, available(path)[1])
    )
    return coordinator


def set_manifest(client, torrent, names):
    client.files[torrent.hash] = [
        SimpleNamespace(name=name, size=4, progress=torrent.progress, priority=1) for name in names
    ]
    for name in names:
        path = client.root / Path(name)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"data")
