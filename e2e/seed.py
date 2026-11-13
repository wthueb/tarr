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


def seed():
    root = Path("/downloads")
    root.chmod(0o777)
    payload = b"verified-content" * 65536
    piece_length = 16384
    pieces = b"".join(
        hashlib.sha1(payload[i : i + piece_length]).digest()
        for i in range(0, len(payload), piece_length)
    )

    def torrent(name, attributes):
        info = {b"piece length": piece_length, b"pieces": pieces, b"private": 1, **attributes}
        (root / name).write_bytes(encode({b"info": info}))

    def directory(name):
        path = root / name
        path.mkdir()
        path.chmod(0o777)
        return path

    def write(path, content):
        path.write_bytes(content)
        path.chmod(0o666)

    for name in ("one.bin", "two.bin"):
        if name == "one.bin":
            write(root / name, payload)
        else:
            os.link(root / "one.bin", root / name)
        torrent(name + ".torrent", {b"name": name.encode(), b"length": len(payload)})

    release = directory("release")
    for name in ("first", "second"):
        write(release / name, payload[: len(payload) // 2])
    torrent(
        "release.torrent",
        {
            b"name": b"release",
            b"files": [
                {b"length": len(payload) // 2, b"path": [name]} for name in (b"first", b"second")
            ],
        },
    )

    for name in ("shared-only.bin", "readonly-shared.bin"):
        write(root / name, payload)
        for source in (b"one", b"two"):
            torrent(
                name + "." + source.decode() + ".torrent",
                {b"name": name.encode(), b"length": len(payload), b"source": source},
            )

    for name in ("shared-part", "selected-part", "retained-part"):
        write(root / name, payload[: len(payload) // 2])
    for name, unique in (("selected", b"selected-part"), ("retained", b"retained-part")):
        torrent(
            name + ".torrent",
            {
                b"name": name.encode(),
                b"files": [
                    {b"length": len(payload) // 2, b"path": [part]}
                    for part in (b"shared-part", unique)
                ],
            },
        )

    nested = directory("nested")
    os.link(root / "one.bin", nested / "movie.bin")
    torrent(
        "nested.torrent",
        {b"name": b"nested", b"files": [{b"length": len(payload), b"path": [b"movie.bin"]}]},
    )

    write(root / "flat-first", payload[:65536])
    (root / "unrelated").write_bytes(b"keep")
    torrent(
        "flat.torrent",
        {
            b"name": b"flat",
            b"files": [
                {b"length": len(payload) // 2, b"path": [name]}
                for name in (b"flat-first", b"flat-second")
            ],
        },
    )

    staging = directory("staging")
    write(staging / "partial.bin.!qB", payload[:65536])
    torrent("partial.torrent", {b"name": b"partial.bin", b"length": len(payload)})
    (root / "empty.torrent").write_bytes(
        encode(
            {
                b"info": {
                    b"name": b"empty.bin",
                    b"length": len(payload),
                    b"piece length": piece_length,
                    b"pieces": pieces,
                    b"private": 1,
                },
                b"announce": b"http://tests:8000/announce",
            }
        )
    )

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


if __name__ == "__main__":
    seed()
