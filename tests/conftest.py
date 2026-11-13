from importlib import import_module

import pytest


@pytest.fixture
def policy_deletion(monkeypatch):
    """Isolate retention/Arr unit tests from the separately tested filesystem coordinator."""

    class Coordinator:
        def __init__(self, client, config):
            self.client = client
            self.pending = None
            self.blocked = False
            self.estimated_reclaimed_bytes = 0

        @property
        def unresolved(self):
            return self.pending is not None or self.blocked

        def begin_pass(self, dry_run=False):
            pass

        def free_space(self, path):
            return getattr(self.client, "free_space", 0)

        def delete(self, torrent, allowed, *, dry_run=False, **kwargs):
            if not allowed(torrent):
                return False
            self.estimated_reclaimed_bytes = torrent.size
            if not dry_run:
                torrent.delete(delete_files=True)
            return True

    monkeypatch.setattr(import_module("tarr.main"), "ContentDeletionCoordinator", Coordinator)
