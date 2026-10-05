import errno
import os
import time
from types import SimpleNamespace
from typing import cast
from unittest.mock import Mock

import pytest
import qbittorrentapi
from pydantic import ValidationError
from structlog.contextvars import get_contextvars

from tarr.cleanup import cleanup_empty_dirs
from tarr.config import CleanupEmptyDirsConfig, Config
from tarr.main import run


def make_client(torrents=()):
    return Mock(spec=qbittorrentapi.Client, torrents_info=Mock(return_value=list(torrents)))


def make_directory(root, name="Movie.mkv--1234abcd", age_minutes=120):
    path = root / name
    path.mkdir()
    observed_at = time.time() - age_minutes * 60
    os.utime(path, (observed_at, observed_at))
    return path


def config(root, **kwargs):
    return CleanupEmptyDirsConfig(directories=[root], **kwargs)


def test_removes_only_old_matching_empty_children(tmp_path):
    old = make_directory(tmp_path)
    recent = make_directory(tmp_path, "Recent--abcdef12", age_minutes=0)
    unmatched = make_directory(tmp_path, "Other")
    nonempty = make_directory(tmp_path, "Nonempty--abcdef12")
    file = nonempty / "movie.mkv"
    file.write_text("content")
    nested = make_directory(nonempty, "Nested--abcdef12")
    old_time = time.time() - 7200
    os.utime(nonempty, (old_time, old_time))

    cleanup_empty_dirs(make_client(), config(tmp_path))

    assert not old.exists()
    assert tmp_path.is_dir()
    assert recent.is_dir()
    assert unmatched.is_dir()
    assert nonempty.is_dir()
    assert nested.is_dir()
    assert file.read_text() == "content"


def test_dry_run_logs_without_removing(tmp_path, monkeypatch):
    path = make_directory(tmp_path)
    logger = Mock()
    monkeypatch.setattr("tarr.cleanup.log", logger)

    cleanup_empty_dirs(make_client(), config(tmp_path), dry_run=True)

    assert path.is_dir()
    logger.info.assert_any_call("would remove empty directory", directory=str(path), dry_run=True)


@pytest.mark.parametrize("state", ["downloading", "stoppedUP", "stalledUP", "checkingUP"])
@pytest.mark.parametrize("reference", ["save", "content", "parent", "descendant"])
def test_protects_all_live_torrents_regardless_of_state(tmp_path, state, reference):
    path = make_directory(tmp_path)
    torrent = SimpleNamespace(save_path=str(tmp_path / "elsewhere"), content_path="", state=state)
    if reference == "save":
        torrent.save_path = str(path)
    elif reference == "content":
        torrent.content_path = str(path / "movie.mkv")
    elif reference == "parent":
        torrent.save_path = str(tmp_path)
    else:
        torrent.save_path = str(path / "nested")
    client = make_client([torrent])

    cleanup_empty_dirs(client, config(tmp_path))

    assert path.is_dir()
    client.torrents_info.assert_called_once_with()


def test_protects_paths_referenced_through_a_symlink(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    alias = tmp_path / "alias"
    alias.symlink_to(root, target_is_directory=True)
    path = make_directory(root)
    torrent = SimpleNamespace(save_path=str(alias / path.name), content_path="")

    cleanup_empty_dirs(make_client([torrent]), config(root))

    assert path.is_dir()


def test_skips_symlink_children_and_roots(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    target = make_directory(outside)
    root = tmp_path / "root"
    root.mkdir()
    child_link = root / "Link--abcdef12"
    child_link.symlink_to(target, target_is_directory=True)
    root_link = tmp_path / "root_link"
    root_link.symlink_to(outside, target_is_directory=True)

    cleanup_empty_dirs(make_client(), config(root))
    cleanup_empty_dirs(make_client(), config(root_link))

    assert child_link.is_symlink()
    assert root_link.is_symlink()
    assert target.is_dir()


def test_custom_name_pattern(tmp_path):
    path = make_directory(tmp_path, "custom")

    cleanup_empty_dirs(make_client(), config(tmp_path, name_pattern="custom"))

    assert not path.exists()


def test_zero_age_allows_immediate_cleanup(tmp_path):
    path = make_directory(tmp_path, age_minutes=0)

    cleanup_empty_dirs(make_client(), config(tmp_path, min_age_minutes=0))

    assert not path.exists()


def test_future_mtime_is_not_removed(tmp_path):
    path = make_directory(tmp_path, age_minutes=-10)

    cleanup_empty_dirs(make_client(), config(tmp_path, min_age_minutes=0))

    assert path.is_dir()


def test_api_failure_does_not_remove_any_directories(tmp_path):
    path = make_directory(tmp_path)
    client = make_client()
    client.torrents_info.side_effect = RuntimeError("qBittorrent unavailable")

    with pytest.raises(RuntimeError, match="unavailable"):
        cleanup_empty_dirs(client, config(tmp_path))

    assert path.is_dir()


@pytest.mark.parametrize("save_path", ["", "relative/path", "/downloads/../other"])
def test_missing_or_relative_torrent_path_fails_closed(tmp_path, save_path):
    path = make_directory(tmp_path)
    torrent = SimpleNamespace(save_path=save_path, content_path="")

    with pytest.raises(ValueError):
        cleanup_empty_dirs(make_client([torrent]), config(tmp_path))

    assert path.is_dir()


def test_missing_root_does_not_prevent_other_roots(tmp_path):
    path = make_directory(tmp_path)
    cfg = CleanupEmptyDirsConfig(directories=[tmp_path / "missing", tmp_path])

    cleanup_empty_dirs(make_client(), cfg)

    assert not path.exists()


def test_new_content_during_removal_is_preserved(tmp_path, monkeypatch):
    path = make_directory(tmp_path)
    original_rmdir = os.rmdir

    def add_content(name, *, dir_fd):
        (path / "new.mkv").write_text("new content")
        original_rmdir(name, dir_fd=dir_fd)

    monkeypatch.setattr("tarr.cleanup.os.rmdir", add_content)

    cleanup_empty_dirs(make_client(), config(tmp_path))

    assert (path / "new.mkv").read_text() == "new content"


def test_symlink_swap_during_removal_does_not_touch_target(tmp_path, monkeypatch):
    root = tmp_path / "root"
    root.mkdir()
    path = make_directory(root)
    outside = tmp_path / "outside"
    outside.mkdir()
    original_rmdir = os.rmdir

    def replace_with_symlink(name, *, dir_fd):
        original_rmdir(name, dir_fd=dir_fd)
        path.symlink_to(outside, target_is_directory=True)
        original_rmdir(name, dir_fd=dir_fd)

    monkeypatch.setattr("tarr.cleanup.os.rmdir", replace_with_symlink)

    cleanup_empty_dirs(make_client(), config(root))

    assert path.is_symlink()
    assert outside.is_dir()


def test_removal_failure_is_logged_and_other_entries_are_processed(tmp_path, monkeypatch):
    paths = [make_directory(tmp_path, name) for name in ("First--1234abcd", "Second--1234abcd")]
    original_rmdir = os.rmdir
    logger = Mock()
    monkeypatch.setattr("tarr.cleanup.log", logger)

    def deny_first(name, *, dir_fd):
        if name == paths[0].name:
            raise PermissionError(errno.EACCES, "permission denied")
        original_rmdir(name, dir_fd=dir_fd)

    monkeypatch.setattr("tarr.cleanup.os.rmdir", deny_first)

    cleanup_empty_dirs(make_client(), config(tmp_path))

    assert paths[0].is_dir()
    assert not paths[1].exists()
    assert logger.warning.call_args.args[0] == "failed to remove empty directory"


def run_config(**job):
    return Config.model_validate(
        {
            "qbittorrent": {
                "host": "localhost",
                "username": "user",
                "password": "pass",
                "cleanup_empty_dirs": job,
            },
            "trackers": [
                {"name": "example", "hosts": ["example.org"], "seed_time_minutes": 1, "ratio": 1}
            ],
        }
    )


def test_job_defaults_to_disabled():
    client = make_client()

    run(cast(qbittorrentapi.Client, client), run_config(), {})

    client.torrents_info.assert_not_called()


def test_run_integrates_cleanup_with_job_context_and_dry_run(tmp_path, monkeypatch):
    path = make_directory(tmp_path)
    logger = Mock()
    contexts = []
    logger.info.side_effect = lambda *_args, **_kwargs: contexts.append(get_contextvars())
    monkeypatch.setattr("tarr.cleanup.log", logger)
    cfg = run_config(enabled=True, directories=[str(tmp_path)])

    run(cast(qbittorrentapi.Client, make_client()), cfg, {}, dry_run=True)

    assert path.is_dir()
    assert contexts == [{"job": "cleanup_empty_dirs"}, {"job": "cleanup_empty_dirs"}]
    assert get_contextvars() == {}


def test_run_removes_empty_directory_when_enabled(tmp_path):
    path = make_directory(tmp_path)

    run(
        cast(qbittorrentapi.Client, make_client()),
        run_config(enabled=True, directories=[str(tmp_path)]),
        {},
    )

    assert not path.exists()


@pytest.mark.parametrize("directories", [["relative"], ["/"], ["/downloads/../other"]])
def test_config_rejects_unsafe_directories(directories):
    with pytest.raises(ValidationError):
        CleanupEmptyDirsConfig(directories=directories)


def test_enabled_job_requires_directories():
    with pytest.raises(ValidationError, match="directories are required"):
        CleanupEmptyDirsConfig(enabled=True)


@pytest.mark.parametrize("pattern", ["", ".", "..", "../*", "subdir/*", "subdir\\*"])
def test_config_rejects_path_patterns(pattern):
    with pytest.raises(ValidationError):
        CleanupEmptyDirsConfig(name_pattern=pattern)


def test_config_rejects_negative_age():
    with pytest.raises(ValidationError):
        CleanupEmptyDirsConfig(min_age_minutes=-1)


@pytest.mark.parametrize("directory", ["/downloads/cross-seed", "/downloads/cross-seed/"])
def test_plain_directory_uses_identical_paths(directory):
    cfg = CleanupEmptyDirsConfig(directories=[directory])
    mapping = cfg.directory_mappings[0]

    assert str(mapping.qbittorrent_path) == "/downloads/cross-seed"
    assert mapping.qbittorrent_path == mapping.local_path
    assert cfg.model_dump()["directories"] == [directory]


@pytest.mark.parametrize(
    "directory",
    [
        ":/local",
        "/remote:",
        ":",
        "/remote:/local:rw",
        "/remote:relative",
        "relative:/local",
        "/:/local",
        "/remote:/",
        "/remote/../other:/local",
        "/remote:/local/../other",
    ],
)
def test_config_rejects_invalid_mappings(directory):
    with pytest.raises(ValidationError):
        CleanupEmptyDirsConfig(directories=[directory])


def test_config_rejects_conflicting_mappings():
    with pytest.raises(ValidationError, match="multiple tarr paths"):
        CleanupEmptyDirsConfig(directories=["/remote:/local", "/remote/:/other"])


@pytest.mark.parametrize("dry_run", [False, True])
@pytest.mark.parametrize("reference", ["save", "content", "parent", "descendant"])
def test_mapped_cleanup_scans_local_side_and_protects_torrents(tmp_path, reference, dry_run):
    remote = tmp_path / "qbittorrent"
    remote.mkdir()
    local = tmp_path / "tarr"
    local.mkdir()
    active = make_directory(local)
    orphan = make_directory(local, "Orphan--abcd1234")
    remote_orphan = make_directory(remote, orphan.name)
    torrent = SimpleNamespace(save_path=str(tmp_path / "elsewhere"), content_path="")
    if reference == "save":
        torrent.save_path = str(remote / active.name)
    elif reference == "content":
        torrent.content_path = str(remote / active.name / "movie.mkv")
    elif reference == "parent":
        torrent.save_path = str(remote)
    else:
        torrent.save_path = str(remote / active.name / "nested")
    cfg = CleanupEmptyDirsConfig(directories=[f"{remote}:{local}"])

    cleanup_empty_dirs(make_client([torrent]), cfg, dry_run=dry_run)

    assert active.is_dir()
    assert orphan.exists() == (dry_run or reference == "parent")
    assert remote_orphan.is_dir()


def test_mapping_matches_components_not_similar_prefixes(tmp_path):
    path = make_directory(tmp_path)
    torrent = SimpleNamespace(save_path=f"/remote-other/{path.name}", content_path="")
    cfg = CleanupEmptyDirsConfig(directories=[f"/remote:{tmp_path}"])

    cleanup_empty_dirs(make_client([torrent]), cfg)

    assert not path.exists()


def test_mapped_source_need_not_exist_locally(tmp_path):
    active = make_directory(tmp_path)
    orphan = make_directory(tmp_path, "Orphan--abcd1234")
    remote = tmp_path / "missing" / "downloads"
    torrent = SimpleNamespace(save_path=str(remote / active.name), content_path="")
    cfg = CleanupEmptyDirsConfig(directories=[f"{remote}:{tmp_path}"])

    cleanup_empty_dirs(make_client([torrent]), cfg)

    assert active.is_dir()
    assert not orphan.exists()
    assert not remote.exists()


def test_mapped_paths_are_translated_before_resolving_symlinks(tmp_path):
    local = tmp_path / "local"
    local.mkdir()
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    remote = tmp_path / "remote"
    remote.symlink_to(elsewhere, target_is_directory=True)
    active = make_directory(local)
    torrent = SimpleNamespace(save_path=str(remote / active.name), content_path="")
    cfg = CleanupEmptyDirsConfig(directories=[f"{remote}:{local}"])

    cleanup_empty_dirs(make_client([torrent]), cfg)

    assert active.is_dir()


@pytest.mark.parametrize("reverse", [False, True])
def test_nested_mapping_uses_longest_source_prefix(tmp_path, reverse):
    local = tmp_path / "local"
    local.mkdir()
    general = local / "nested"
    general.mkdir()
    specific = tmp_path / "specific"
    specific.mkdir()
    general_candidate = make_directory(general)
    specific_candidate = make_directory(specific)
    directories = [f"/remote:{local}", f"/remote/nested:{specific}", str(general)]
    cfg = CleanupEmptyDirsConfig(directories=directories[::-1] if reverse else directories)
    torrent = SimpleNamespace(
        save_path=f"/remote/nested/{specific_candidate.name}", content_path=""
    )

    cleanup_empty_dirs(make_client([torrent]), cfg)

    assert specific_candidate.is_dir()
    assert not general_candidate.exists()


def test_parent_torrent_path_protects_nested_mapped_roots(tmp_path):
    general = tmp_path / "general"
    general.mkdir()
    specific = tmp_path / "specific"
    specific.mkdir()
    candidate = make_directory(specific)
    cfg = CleanupEmptyDirsConfig(directories=[f"/remote:{general}", f"/remote/nested:{specific}"])
    torrent = SimpleNamespace(save_path="/remote", content_path="")

    cleanup_empty_dirs(make_client([torrent]), cfg)

    assert candidate.is_dir()


def test_mixed_plain_and_mapped_directories(tmp_path):
    plain = tmp_path / "plain"
    plain.mkdir()
    mapped = tmp_path / "mapped"
    mapped.mkdir()
    plain_active = make_directory(plain)
    mapped_active = make_directory(mapped)
    plain_orphan = make_directory(plain, "Orphan--abcd1234")
    mapped_orphan = make_directory(mapped, "Orphan--abcd1234")
    cfg = CleanupEmptyDirsConfig(directories=[str(plain), f"/remote:{mapped}"])
    torrents = [
        SimpleNamespace(save_path=str(plain_active), content_path=""),
        SimpleNamespace(save_path=f"/remote/{mapped_active.name}", content_path=""),
    ]

    cleanup_empty_dirs(make_client(torrents), cfg)

    assert plain_active.is_dir()
    assert mapped_active.is_dir()
    assert not plain_orphan.exists()
    assert not mapped_orphan.exists()


def test_configured_nested_root_is_never_removed(tmp_path):
    nested = make_directory(tmp_path)
    cfg = CleanupEmptyDirsConfig(directories=[str(tmp_path), f"/remote:{nested}"])

    cleanup_empty_dirs(make_client(), cfg)

    assert nested.is_dir()
