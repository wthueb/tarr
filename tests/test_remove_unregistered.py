import importlib
from types import SimpleNamespace
from typing import cast
from unittest.mock import Mock

import pytest
import qbittorrentapi

from tarr.config import RemoveUnregisteredConfig

main = importlib.import_module("tarr.main")


class FakeClient:
    def __init__(self, torrents):
        self.torrents = torrents

    def torrents_info(self, status_filter=None):
        return self.torrents


@pytest.mark.parametrize("status", [0, 1, 2, 3, 4, 5])
@pytest.mark.parametrize(
    "msg",
    [
        "Unregistered torrent: passkey is invalid",
        "Torrent does not exist on this tracker",
        "Torrent has been deleted",
        "TORRENT NOT REGISTERED WITH THIS TRACKER.",
    ],
)
def test_tracker_is_unregistered_requires_message_and_active_status(status, msg):
    tracker = SimpleNamespace(status=status, msg=msg)

    assert main._tracker_is_unregistered(tracker) is (status not in {0, 1, 3})


@pytest.mark.parametrize("msg", ["", "working", "timed out", "invalid passkey", "not registered"])
def test_tracker_is_unregistered_rejects_unrelated_messages(msg):
    tracker = SimpleNamespace(status=5, msg=msg)

    assert not main._tracker_is_unregistered(tracker)


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
