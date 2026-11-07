import time


def wait_for_torrents(client, predicate, timeout=30):
    deadline = time.monotonic() + timeout
    while True:
        torrents = client.torrents_info()
        if predicate(torrents):
            return torrents
        if time.monotonic() >= deadline:
            raise AssertionError([(t.name, t.state, t.progress) for t in torrents])
        time.sleep(1)


def recheck_complete_torrents(client, count):
    torrents = wait_for_torrents(
        client,
        lambda torrents: (
            len(torrents) == count and all(t.state in {"stoppedDL", "stoppedUP"} for t in torrents)
        ),
    )
    client.torrents_recheck(torrent_hashes=[t.hash for t in torrents])
    return wait_for_torrents(
        client, lambda torrents: len(torrents) == count and all(t.progress == 1 for t in torrents)
    )
