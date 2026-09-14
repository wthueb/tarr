import importlib
from types import SimpleNamespace
from typing import cast
from unittest.mock import Mock

import qbittorrentapi

from tarr.config import RemoveUnregisteredConfig

main = importlib.import_module("tarr.main")


class FakeClient:
    def __init__(self, torrents):
        self.torrents = torrents

    def torrents_info(self, status_filter=None):
        return self.torrents


def test_logs_tracker_msg_when_torrent_is_unregistered(monkeypatch):
    tracker_msg = "Unregistered torrent: passkey is invalid"
    torrent = SimpleNamespace(
        hash="1234567890abcdef",
        name="unregistered torrent",
        state="stalledUP",
        size=1024,
        category="movies",
        trackers=[SimpleNamespace(status=2, msg=tracker_msg)],
        delete=Mock(),
    )
    logger = Mock()
    monkeypatch.setattr(main, "log", logger)

    main.remove_unregistered(
        cast(qbittorrentapi.Client, FakeClient([torrent])),
        RemoveUnregisteredConfig(),
        {},
    )

    logger.debug.assert_any_call(
        "tracker reported unregistered torrent",
        tracker_msg=tracker_msg,
    )
