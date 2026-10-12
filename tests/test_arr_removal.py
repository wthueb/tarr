import datetime
import io
import json
from email.message import Message
from types import SimpleNamespace
from typing import cast
from unittest.mock import Mock
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlparse

import pytest
import qbittorrentapi
from pydantic import ValidationError
from structlog.testing import capture_logs

from tarr.arr import removal_allowed, torrent_in_queue
from tarr.config import ArrConfig, Config, RemovalCategoryConfig
from tarr.main import run

ARRS = {
    "movies": {"host": "http://arr.example/radarr/", "api_key": "secret"},
    "movies4k": {"host": "http://arr.example/radarr4k", "api_key": "secret4k"},
}
HASH = "abcdef0123456789"
GIB = 1024**3


def queue_page(records, *, page=1, page_size=100, total=None):
    return {
        "page": page,
        "pageSize": page_size,
        "totalRecords": len(records) if total is None else total,
        "records": records,
    }


def mock_http(monkeypatch, *pages):
    request = Mock(side_effect=[io.BytesIO(json.dumps(page).encode()) for page in pages])
    monkeypatch.setattr("tarr.arr.urlopen", request)
    return request


def test_queue_checks_all_pages_and_preserves_base_path(monkeypatch):
    request = mock_http(
        monkeypatch,
        queue_page([{"downloadId": "other"}], page_size=1, total=2),
        queue_page([{"downloadId": HASH.upper()}], page=2, page_size=1, total=2),
    )

    assert torrent_in_queue(ArrConfig.model_validate(ARRS["movies"]), HASH)

    assert request.call_count == 2
    for index, call in enumerate(request.call_args_list, start=1):
        req = call.args[0]
        assert req.get_header("X-api-key") == "secret"
        assert call.kwargs == {"timeout": 10}
        url = urlparse(req.full_url)
        assert url.path == "/radarr/api/v3/queue"
        assert parse_qs(url.query) == {
            "page": [str(index)],
            "pageSize": ["100"],
            "sortKey": ["id"],
            "sortDirection": ["ascending"],
            "includeUnknownMovieItems": ["true"],
            "includeUnknownSeriesItems": ["true"],
        }
        assert "secret" not in req.full_url


@pytest.mark.parametrize("records", [[], [{"downloadId": None}], [{}], [{"downloadId": "other"}]])
def test_queue_without_matching_download_id_allows_removal(monkeypatch, records):
    mock_http(monkeypatch, queue_page(records))

    assert not torrent_in_queue(ArrConfig.model_validate(ARRS["movies"]), HASH)


def test_absence_requires_reading_every_page(monkeypatch):
    request = mock_http(
        monkeypatch,
        queue_page([{"downloadId": "one"}], page_size=1, total=2),
        queue_page([{"downloadId": "two"}], page=2, page_size=1, total=2),
    )

    assert not torrent_in_queue(ArrConfig.model_validate(ARRS["movies"]), HASH)
    assert request.call_count == 2


@pytest.mark.parametrize(
    "page",
    [
        {},
        [],
        queue_page([], total=1),
        queue_page([], page=2),
        queue_page([], total="0"),
        queue_page([{"downloadId": 123}]),
        queue_page([{"downloadId": "other"}], total=2),
        queue_page([{"downloadId": "one"}, {"downloadId": "two"}], page_size=1),
        {**queue_page([]), "records": None},
    ],
)
def test_invalid_or_incomplete_queue_blocks_removal(monkeypatch, page):
    mock_http(monkeypatch, page)
    arrs = {"movies": ArrConfig.model_validate(ARRS["movies"])}

    assert not removal_allowed(HASH, ["movies"], arrs)


@pytest.mark.parametrize(
    "error",
    [
        URLError("cannot connect"),
        TimeoutError("timed out"),
        HTTPError("http://arr.example", 401, "unauthorized", Message(), None),
        json.JSONDecodeError("invalid json", "secret", 0),
    ],
)
def test_api_errors_block_removal_without_logging_secrets(monkeypatch, error):
    monkeypatch.setattr("tarr.arr.urlopen", Mock(side_effect=error))
    arrs = {"movies": ArrConfig.model_validate(ARRS["movies"])}

    with capture_logs() as logs:
        assert not removal_allowed(HASH, ["movies"], arrs)

    assert logs[0]["arr"] == "movies"
    assert logs[0]["error_type"] == type(error).__name__
    assert "secret" not in str(logs)


def test_missing_instance_fails_closed():
    assert not removal_allowed(HASH, ["missing"], {})


def test_unprotected_category_does_not_query_arr(monkeypatch):
    request = Mock()
    monkeypatch.setattr("tarr.arr.urlopen", request)

    assert removal_allowed(HASH, [], {})
    request.assert_not_called()


class FakeClient:
    def __init__(self, torrents):
        self.torrents = torrents

    def torrents_info(self, status_filter=None):
        return self.torrents

    def sync_maindata(self):
        return SimpleNamespace(server_state=SimpleNamespace(free_space_on_disk=0))


def make_torrent(*, category="radarr", torrent_hash=HASH, seeders=10):
    return SimpleNamespace(
        hash=torrent_hash,
        name="completed torrent",
        state="stoppedUP",
        size=GIB,
        category=category,
        seeding_time=60,
        ratio=1,
        max_seeding_time=0,
        max_ratio=0,
        num_complete=seeders,
        uploaded=GIB,
        trackers=[
            SimpleNamespace(
                url="http://tracker.example/announce", status=2, msg="unregistered torrent"
            )
        ],
        delete=Mock(),
    )


def make_config(job, *, categories=None):
    return Config.model_validate(
        {
            "qbittorrent": {
                "host": "localhost",
                "username": "user",
                "password": "pass",
                job: {
                    "enabled": True,
                    "categories": categories
                    if categories is not None
                    else [
                        {
                            "name": "radarr",
                            "arr_verify_removal": ["movies", "movies4k"],
                        }
                    ],
                    **({"free_space_threshold_gibi": 2} if job == "maintain_free_space" else {}),
                },
            },
            "trackers": [
                {
                    "name": "example",
                    "hosts": ["tracker.example"],
                    "seed_time_minutes": 0,
                    "ratio": 0,
                }
            ],
            "arrs": ARRS,
        }
    )


@pytest.mark.parametrize("job", ["remove_stopped", "remove_unregistered", "maintain_free_space"])
@pytest.mark.parametrize("queued", [False, True])
@pytest.mark.parametrize("dry_run", [False, True])
def test_every_removal_job_checks_all_instances(monkeypatch, job, queued, dry_run):
    torrent = make_torrent()
    check = Mock(side_effect=[False, queued])
    monkeypatch.setattr("tarr.arr.torrent_in_queue", check)
    config = make_config(job)
    unregistered = {}
    stopped = {}

    run(
        cast(qbittorrentapi.Client, FakeClient([torrent])),
        config,
        unregistered,
        dry_run=dry_run,
        stopped_first_seen=stopped,
    )

    assert check.call_args_list == [
        ((config.arrs["movies"], HASH),),
        ((config.arrs["movies4k"], HASH),),
    ]
    if queued or dry_run:
        torrent.delete.assert_not_called()
        if job == "remove_stopped":
            assert HASH in stopped
        if job == "remove_unregistered":
            assert HASH in unregistered
    else:
        torrent.delete.assert_called_once_with(delete_files=job != "remove_stopped")
        assert stopped == unregistered == {}


@pytest.mark.parametrize("job", ["remove_stopped", "remove_unregistered", "maintain_free_space"])
def test_removal_resumes_after_queue_clears(monkeypatch, job):
    torrent = make_torrent()
    check = Mock(return_value=True)
    monkeypatch.setattr("tarr.arr.torrent_in_queue", check)
    config = make_config(job)
    client = cast(qbittorrentapi.Client, FakeClient([torrent]))
    observed = datetime.datetime.now() - datetime.timedelta(minutes=20)
    unregistered = {HASH: observed}
    stopped = {HASH: observed}

    run(client, config, unregistered, stopped_first_seen=stopped)
    torrent.delete.assert_not_called()
    assert unregistered == stopped == {HASH: observed}

    check.return_value = False
    run(client, config, unregistered, stopped_first_seen=stopped)
    torrent.delete.assert_called_once()


@pytest.mark.parametrize("dry_run", [False, True])
def test_free_space_skips_queued_torrent_and_continues_without_counting_its_space(
    monkeypatch, dry_run
):
    blocked = make_torrent(seeders=100)
    eligible = make_torrent(category="cross", torrent_hash="other", seeders=1)
    check = Mock(return_value=True)
    monkeypatch.setattr("tarr.arr.torrent_in_queue", check)
    config = make_config("maintain_free_space")
    config.qbittorrent.maintain_free_space.free_space_threshold_gibi = 1
    config.qbittorrent.maintain_free_space.categories.append("cross")

    with capture_logs() as logs:
        run(
            cast(qbittorrentapi.Client, FakeClient([blocked, eligible])),
            config,
            {},
            dry_run=dry_run,
        )

    blocked.delete.assert_not_called()
    assert [entry["event"] for entry in logs].count("torrent selected to reclaim free space") == 1
    if dry_run:
        eligible.delete.assert_not_called()
    else:
        eligible.delete.assert_called_once_with(delete_files=True)


@pytest.mark.parametrize("job", ["remove_stopped", "remove_unregistered", "maintain_free_space"])
def test_ignored_torrents_are_not_queried(monkeypatch, job):
    torrent = make_torrent()
    check = Mock()
    monkeypatch.setattr("tarr.arr.torrent_in_queue", check)
    config = make_config(job)
    getattr(config.qbittorrent, job).ignore_categories = ["radarr"]

    run(cast(qbittorrentapi.Client, FakeClient([torrent])), config, {})

    check.assert_not_called()
    torrent.delete.assert_not_called()


@pytest.mark.parametrize("job", ["remove_stopped", "remove_unregistered", "maintain_free_space"])
def test_ineligible_torrents_are_not_queried(monkeypatch, job):
    torrent = make_torrent()
    torrent.ratio = 0
    torrent.seeding_time = 0
    torrent.max_ratio = 1
    torrent.max_seeding_time = 1
    torrent.trackers[0].msg = "working"
    check = Mock()
    monkeypatch.setattr("tarr.arr.torrent_in_queue", check)
    config = make_config(job)
    config.trackers[0].ratio = 1
    config.trackers[0].seed_time_minutes = 1

    run(cast(qbittorrentapi.Client, FakeClient([torrent])), config, {})

    check.assert_not_called()
    torrent.delete.assert_not_called()


@pytest.mark.parametrize("job", ["remove_stopped", "remove_unregistered", "maintain_free_space"])
def test_api_failure_blocks_each_removal_job(monkeypatch, job):
    torrent = make_torrent()
    request = Mock(side_effect=TimeoutError("timed out"))
    monkeypatch.setattr("tarr.arr.urlopen", request)

    run(cast(qbittorrentapi.Client, FakeClient([torrent])), make_config(job), {})

    request.assert_called_once()
    torrent.delete.assert_not_called()


@pytest.mark.parametrize("job", ["remove_stopped", "remove_unregistered", "maintain_free_space"])
def test_unknown_arr_references_rejected(job):
    with pytest.raises(ValidationError, match="unknown Arr instance"):
        make_config(job, categories=[{"name": "radarr", "arr_verify_removal": ["missing"]}])


@pytest.mark.parametrize("name", [None, ""])
def test_uncategorized_removal_object_is_normalized(name):
    config = make_config(
        "remove_stopped", categories=[{"name": name, "arr_verify_removal": ["movies"]}]
    )
    cfg = config.qbittorrent.remove_stopped

    assert cfg.includes_category("")
    assert cfg.arr_instances("") == ["movies"]
    assert cfg.overlapping_categories == []
    cfg.ignore_categories = [None]
    assert not cfg.includes_category("")
    assert cfg.overlapping_categories == [None]


def test_duplicate_category_entries_combine_verification_instances():
    config = make_config(
        "remove_stopped",
        categories=[
            {"name": "radarr", "arr_verify_removal": ["movies"]},
            {"name": "radarr", "arr_verify_removal": ["movies", "movies4k"]},
        ],
    )

    assert config.qbittorrent.remove_stopped.arr_instances("radarr") == ["movies", "movies4k"]


def test_verification_does_not_apply_to_other_jobs():
    config = make_config("remove_stopped")

    assert config.qbittorrent.remove_unregistered.arr_instances("radarr") == []
    assert config.qbittorrent.maintain_free_space.arr_instances("radarr") == []


@pytest.mark.parametrize(
    "host", ["arr:7878", "ftp://arr", "http://user:pass@arr", "http://arr?key=x"]
)
def test_invalid_arr_hosts_rejected(host):
    with pytest.raises(ValidationError):
        ArrConfig(host=host, api_key="secret")


def test_removal_category_does_not_accept_action():
    with pytest.raises(ValidationError):
        RemovalCategoryConfig.model_validate({"name": "radarr", "action": "Stop"})
