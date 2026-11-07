from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread

import pytest
import qbittorrentapi

from e2e.support import wait_for_torrents
from tarr.config import ContentDeletionConfig
from tarr.deletion import ContentDeletionCoordinator


@pytest.fixture
def storage():
    return Path("/downloads")


@pytest.fixture(scope="session")
def qbittorrent():
    client = qbittorrentapi.Client(host="http://qbittorrent:8080", REQUESTS_ARGS={"timeout": 3})
    print("qBittorrent", client.app_version(), flush=True)
    return client


@pytest.fixture
def client(qbittorrent):
    qbittorrent.app_set_preferences({"temp_path_enabled": False})
    try:
        yield qbittorrent
    finally:
        qbittorrent.torrents_delete(torrent_hashes="all", delete_files=True)
        wait_for_torrents(qbittorrent, lambda torrents: not torrents)


@pytest.fixture
def coordinator(client):
    return ContentDeletionCoordinator(
        client,
        ContentDeletionConfig(
            path_mappings=["/downloads"], poll_interval_seconds=1, timeout_seconds=15
        ),
    )


@pytest.fixture
def add_torrent(client, storage):
    def add(name, *, is_stopped=True, **options):
        result = client.torrents_add(
            torrent_files=str(storage / name),
            save_path="/downloads",
            is_stopped=is_stopped,
            **options,
        )
        assert result == "Ok." or (
            not isinstance(result, str) and result.get("success_count") == 1
        ), result

    return add


@pytest.fixture
def staging(client):
    client.app_set_preferences(
        {
            "temp_path_enabled": True,
            "temp_path": "/downloads/staging",
            "incomplete_files_ext": True,
        }
    )
    return Path("/downloads/staging")


class UnregisteredTracker(BaseHTTPRequestHandler):
    def do_GET(self):
        message = b"Unregistered torrent"
        response = b"d14:failure reason" + str(len(message)).encode() + b":" + message + b"e"
        self.send_response(200)
        self.send_header("Content-Length", str(len(response)))
        self.end_headers()
        self.wfile.write(response)

    def log_message(self, format, *args):
        pass


@pytest.fixture
def unregistered_tracker():
    with ThreadingHTTPServer(("0.0.0.0", 8000), UnregisteredTracker) as tracker:
        thread = Thread(target=tracker.serve_forever, daemon=True)
        thread.start()
        try:
            yield tracker
        finally:
            tracker.shutdown()
            thread.join(timeout=3)
