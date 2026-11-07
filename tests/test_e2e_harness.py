from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from e2e import support
from e2e.seed import encode


def test_recheck_waits_for_registered_torrents(monkeypatch):
    client = Mock()
    clock = Mock()
    clock.monotonic.return_value = 0
    monkeypatch.setattr(support, "time", clock)
    pending = SimpleNamespace(name="nested", hash="hash", state="checkingResumeData", progress=0)
    ready = SimpleNamespace(name="nested", hash="hash", state="stoppedDL", progress=0)
    complete = SimpleNamespace(name="nested", hash="hash", state="stoppedUP", progress=1)
    client.torrents_info.side_effect = [[], [pending], [ready], [ready], [complete]]

    def recheck(**_kwargs):
        assert client.torrents_info.call_count == 3

    client.torrents_recheck.side_effect = recheck

    assert support.recheck_complete_torrents(client, 1) == [complete]
    client.torrents_recheck.assert_called_once_with(torrent_hashes=["hash"])
    assert clock.sleep.call_count == 3


def test_wait_for_torrents_times_out_with_state(monkeypatch):
    client = Mock()
    torrent = SimpleNamespace(name="stuck", state="checkingResumeData", progress=0)
    client.torrents_info.return_value = [torrent]
    clock = Mock()
    clock.monotonic.side_effect = [0, 0, 30]
    monkeypatch.setattr(support, "time", clock)

    with pytest.raises(AssertionError, match="stuck.*checkingResumeData"):
        support.wait_for_torrents(client, lambda torrents: False)

    assert client.torrents_info.call_count == 2
    clock.sleep.assert_called_once_with(1)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (42, b"i42e"),
        (b"test", b"4:test"),
        ([b"a", 1], b"l1:ai1ee"),
        ({b"z": 1, b"a": b"x"}, b"d1:a1:x1:zi1ee"),
    ],
)
def test_torrent_encoding(value, expected):
    assert encode(value) == expected
