import os
import pathlib
import subprocess
import textwrap
import uuid

import pytest

pytestmark = pytest.mark.skipif(
    os.environ.get("TARR_RUN_E2E") != "1",
    reason="set TARR_RUN_E2E=1 to build a temporary image and test isolated Docker volumes",
)

SEED_STORAGE = r"""
import hashlib
import os
from pathlib import Path


def encode(value):
    if isinstance(value, int):
        return b"i" + str(value).encode() + b"e"
    if isinstance(value, bytes):
        return str(len(value)).encode() + b":" + value
    if isinstance(value, list):
        return b"l" + b"".join(map(encode, value)) + b"e"
    return b"d" + b"".join(encode(k) + encode(v) for k, v in sorted(value.items())) + b"e"


root = Path("/downloads")
os.chmod(root, 0o777)
payload = b"verified-content" * 65536
piece_length = 16384
pieces = b"".join(hashlib.sha1(payload[i:i+piece_length]).digest()
                  for i in range(0, len(payload), piece_length))
for name in ("one.bin", "two.bin"):
    path = root / name
    if name == "one.bin":
        path.write_bytes(payload)
        path.chmod(0o666)
    else:
        os.link(root / "one.bin", path)
    info = {b"name": name.encode(), b"length": len(payload),
            b"piece length": piece_length, b"pieces": pieces, b"private": 1}
    (root / (name + ".torrent")).write_bytes(encode({b"info": info}))
release = root / "release"
release.mkdir(mode=0o777)
release.chmod(0o777)
for name in ("first", "second"):
    (release / name).write_bytes(payload[:len(payload)//2])
    (release / name).chmod(0o666)
info = {b"name": b"release", b"piece length": piece_length, b"pieces": pieces,
        b"private": 1, b"files": [{b"length": len(payload)//2, b"path": [name]}
                                 for name in (b"first", b"second")]}
(root / "release.torrent").write_bytes(encode({b"info": info}))
nested = root / "nested"
nested.mkdir(mode=0o777)
nested.chmod(0o777)
os.link(root / "one.bin", nested / "movie.bin")
info = {b"name": b"nested", b"piece length": piece_length, b"pieces": pieces,
        b"private": 1, b"files": [{b"length": len(payload), b"path": [b"movie.bin"]}]}
(root / "nested.torrent").write_bytes(encode({b"info": info}))
(root / "flat-first").write_bytes(payload[:65536])
(root / "flat-first").chmod(0o666)
(root / "unrelated").write_bytes(b"keep")
info = {b"name": b"flat", b"piece length": piece_length, b"pieces": pieces,
        b"private": 1, b"files": [{b"length": len(payload)//2, b"path": [name]}
                                 for name in (b"flat-first", b"flat-second")]}
(root / "flat.torrent").write_bytes(encode({b"info": info}))
staging = root / "staging"
staging.mkdir(mode=0o777)
staging.chmod(0o777)
(staging / "partial.bin.!qB").write_bytes(payload[:65536])
(staging / "partial.bin.!qB").chmod(0o666)
info = {b"name": b"partial.bin", b"length": len(payload),
        b"piece length": piece_length, b"pieces": pieces, b"private": 1}
(root / "partial.torrent").write_bytes(encode({b"info": info}))
info = {b"name": b"empty.bin", b"length": len(payload),
        b"piece length": piece_length, b"pieces": pieces, b"private": 1}
(root / "empty.torrent").write_bytes(encode({
    b"info": info, b"announce": b"http://verifier:8000/announce",
}))
config = Path("/config/qBittorrent")
config.mkdir(parents=True, exist_ok=True)
(config / "qBittorrent.conf").write_text(
    "[Preferences]\n"
    "WebUI\\AuthSubnetWhitelistEnabled=true\n"
    "WebUI\\AuthSubnetWhitelist=0.0.0.0/0\n"
    "WebUI\\CSRFProtection=false\n"
    "WebUI\\HostHeaderValidation=false\n"
    "WebUI\\LocalHostAuth=false\n"
    "Connection\\UPnP=false\n"
    "Bittorrent\\DHT=false\n"
    "Bittorrent\\PeX=false\n"
    "Bittorrent\\LSD=false\n"
)
"""

VERIFY_DELETION = r"""
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread

import qbittorrentapi

from tarr.config import ContentDeletionConfig, RemoveUnregisteredConfig
from tarr.deletion import ContentDeletionCoordinator
from tarr.main import remove_unregistered


class UnregisteredTracker(BaseHTTPRequestHandler):
    def do_GET(self):
        message = b"Unregistered torrent"
        response = b"d14:failure reason" + str(len(message)).encode() + b":" + message + b"e"
        self.send_response(200)
        self.send_header("Content-Length", str(len(response)))
        self.end_headers()
        self.wfile.write(response)

    def log_message(self, *args):
        pass


tracker = ThreadingHTTPServer(("0.0.0.0", 8000), UnregisteredTracker)
Thread(target=tracker.serve_forever, daemon=True).start()
client = qbittorrentapi.Client(host="http://qbittorrent:8080", REQUESTS_ARGS={"timeout": 3})
deadline = time.monotonic() + 60
while True:
    try:
        version = client.app_version()
        break
    except Exception:
        if time.monotonic() >= deadline:
            raise
        time.sleep(1)
print("qBittorrent", version, flush=True)
try:
    Path("/downloads/must-not-write").write_bytes(b"no")
except OSError:
    pass
else:
    raise AssertionError("tarr's download mount must be read-only")

for name in ("one.bin", "two.bin"):
    result = client.torrents_add(
        torrent_files=Path("/downloads/" + name + ".torrent"),
        save_path="/downloads", is_stopped=True,
    )
    assert result == "Ok." or result.get("success_count") == 1, result

client.torrents_recheck(torrent_hashes="all")
while True:
    torrents = client.torrents_info()
    if len(torrents) == 2 and all(t.progress == 1 for t in torrents):
        break
    if time.monotonic() >= deadline:
        raise AssertionError([(t.name, t.state, t.progress) for t in torrents])
    time.sleep(1)

coordinator = ContentDeletionCoordinator(client, ContentDeletionConfig(
    path_mappings=["/downloads"], poll_interval_seconds=1, timeout_seconds=15,
))
assert Path("/downloads/one.bin").stat().st_ino == Path("/downloads/two.bin").stat().st_ino
for name in ("one.bin", "two.bin"):
    torrent = next(t for t in client.torrents_info() if t.name == name)
    print("verifying", torrent.name, torrent.save_path, torrent.content_path,
          [f.name for f in torrent.files], flush=True)
    assert coordinator.delete(torrent, lambda current: True)
    assert coordinator.pending is None
    assert not Path("/downloads/" + name).exists()
    if name == "one.bin":
        assert Path("/downloads/two.bin").exists()
    assert coordinator.free_space(Path("/downloads")) > 0
assert not client.torrents_info()
client.torrents_add(torrent_files=Path("/downloads/release.torrent"),
                   save_path="/downloads", is_stopped=True)
client.torrents_recheck(torrent_hashes="all")
while True:
    torrents = client.torrents_info()
    if len(torrents) == 1 and torrents[0].progress == 1:
        break
    assert time.monotonic() < deadline, [(t.name, t.state, t.progress) for t in torrents]
    time.sleep(1)
torrent = torrents[0]
print("verifying multi-file", torrent.content_path, [f.name for f in torrent.files], flush=True)
assert coordinator.delete(torrent, lambda current: True)
assert not Path("/downloads/release/first").exists()
assert not Path("/downloads/release/second").exists()

client.torrents_add(torrent_files=Path("/downloads/nested.torrent"),
                   save_path="/downloads", is_stopped=True)
client.torrents_recheck(torrent_hashes="all")
while True:
    torrents = client.torrents_info()
    if len(torrents) == 1 and torrents[0].progress == 1:
        break
    assert time.monotonic() < deadline, [(t.name, t.state, t.progress) for t in torrents]
    time.sleep(1)
torrent = torrents[0]
assert torrent.content_path == "/downloads/nested/movie.bin", torrent.content_path
assert [file.name for file in torrent.files] == ["nested/movie.bin"]
print("verifying nested single-file", torrent.content_path, flush=True)
assert coordinator.delete(torrent, lambda current: True)
assert not Path("/downloads/nested/movie.bin").exists()
assert coordinator.pending is None

client.torrents_add(torrent_files=Path("/downloads/flat.torrent"),
                   save_path="/downloads", content_layout="NoSubfolder", is_stopped=True)
while True:
    torrents = client.torrents_info()
    if len(torrents) == 1 and torrents[0].state == "stoppedDL":
        break
    assert time.monotonic() < deadline, [(t.name, t.state, t.progress) for t in torrents]
    time.sleep(1)
torrent = torrents[0]
client.torrents_file_priority(torrent_hash=torrent.hash, file_ids=1, priority=0)
torrent = client.torrents_info(torrent_hashes=torrent.hash)[0]
assert torrent.progress < 1
assert {file.name for file in torrent.files} == {"flat-first", "flat-second"}
assert Path("/downloads/flat-first").exists()
print("verifying incomplete shared directory", torrent.content_path, flush=True)
assert coordinator.delete(torrent, lambda current: True)
assert not Path("/downloads/flat-first").exists()
assert Path("/downloads/unrelated").exists()
assert Path("/downloads/empty.torrent").exists()
assert coordinator.pending is None

client.app_set_preferences({"temp_path_enabled": True, "temp_path": "/downloads/staging",
                            "incomplete_files_ext": True})
client.torrents_add(torrent_files=Path("/downloads/partial.torrent"),
                   save_path="/downloads", download_path="/downloads/staging", is_stopped=False)
while True:
    torrents = client.torrents_info()
    if len(torrents) == 1 and torrents[0].state in {"stalledDL", "downloading"}:
        break
    assert time.monotonic() < deadline, [(t.name, t.state, t.progress) for t in torrents]
    time.sleep(1)
torrent = torrents[0]
print("verifying incomplete", torrent.state, torrent.save_path, torrent.content_path,
      torrent.download_path, [f.name for f in torrent.files], flush=True)
assert coordinator.delete(torrent, lambda current: True)
assert not Path("/downloads/staging/partial.bin.!qB").exists()
assert coordinator.pending is None
assert not client.torrents_info()

client.torrents_add(torrent_files=Path("/downloads/empty.torrent"),
                   save_path="/downloads", download_path="/downloads/staging", is_stopped=False)
while True:
    torrents = client.torrents_info()
    if (len(torrents) == 1 and torrents[0].state == "stalledDL"
            and any(t.status not in {0, 1, 3} and t.msg == "Unregistered torrent"
                    for t in torrents[0].trackers)):
        break
    assert time.monotonic() < deadline, [(t.name, t.state, t.progress) for t in torrents]
    time.sleep(1)
torrent = torrents[0]
print("verifying zero-data", torrent.state, torrent.content_path,
      torrent.completed, torrent.downloaded, flush=True)
assert torrent.progress == torrent.completed == torrent.downloaded == 0
assert all(file.progress == 0 for file in torrent.files)
assert not Path("/downloads/empty.bin").exists()
assert not Path("/downloads/staging/empty.bin.!qB").exists()
assert coordinator._snapshot(torrent, None) == set()
first_seen = {}
remove_unregistered(client, RemoveUnregisteredConfig(delay_minutes=0), first_seen,
                    deletion_coordinator=coordinator)
assert coordinator.pending is None
assert not client.torrents_info()
assert not first_seen
tracker.shutdown()
print("READ_ONLY_HARD_LINK_DELETION_VERIFIED", flush=True)
"""


def docker(*args, source=None, timeout=120):
    result = subprocess.run(
        ["docker", *args],
        input=source,
        text=True,
        capture_output=True,
        timeout=timeout,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    return result.stdout


def test_real_qbittorrent_with_read_only_tarr_storage():
    prefix = f"tarr-deletion-test-{uuid.uuid4().hex[:10]}"
    network = prefix + "-network"
    downloads = prefix + "-downloads"
    config = prefix + "-config"
    image = prefix + ":test"
    container = prefix + "-qbittorrent"
    root = pathlib.Path(__file__).parents[1]
    try:
        docker("build", "--tag", image, str(root), timeout=300)
        docker("network", "create", "--internal", network)
        docker("volume", "create", downloads)
        docker("volume", "create", config)
        docker(
            "run",
            "--rm",
            "-i",
            "--network",
            "none",
            "--mount",
            f"type=volume,src={downloads},dst=/downloads",
            "--mount",
            f"type=volume,src={config},dst=/config",
            "python:3.12-slim",
            "python",
            "-",
            source=textwrap.dedent(SEED_STORAGE),
        )
        docker(
            "run",
            "--detach",
            "--name",
            container,
            "--network",
            network,
            "--network-alias",
            "qbittorrent",
            "--env",
            "PUID=1000",
            "--env",
            "PGID=1000",
            "--mount",
            f"type=volume,src={downloads},dst=/downloads",
            "--mount",
            f"type=volume,src={config},dst=/config",
            "linuxserver/qbittorrent:5.2.3",
        )
        output = docker(
            "run",
            "--rm",
            "-i",
            "--network",
            network,
            "--network-alias",
            "verifier",
            "--mount",
            f"type=volume,src={downloads},dst=/downloads,readonly",
            "--entrypoint",
            "python",
            image,
            "-",
            source=textwrap.dedent(VERIFY_DELETION),
        )
        assert "READ_ONLY_HARD_LINK_DELETION_VERIFIED" in output
        print(output)
    finally:
        subprocess.run(["docker", "rm", "--force", container], capture_output=True)
        subprocess.run(["docker", "volume", "rm", downloads, config], capture_output=True)
        subprocess.run(["docker", "network", "rm", network], capture_output=True)
        subprocess.run(["docker", "image", "rm", image], capture_output=True)
