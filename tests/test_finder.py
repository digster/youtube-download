import json
import threading
from types import SimpleNamespace
from unittest.mock import Mock

import httpx
import pytest
from conftest import PID, VID, VID2, save_video
from test_downloads import wait_for

from youtube_download.downloads import DownloadManager
from youtube_download.finder import Finder, parse_response, retry_delay
from youtube_download.store import Store


def response():
    return {
        "status": "ok",
        "keys": [
            {
                "name": "Internet Archive",
                "available": [
                    {
                        "url": "https://web.archive.org/example",
                        "title": "Saved page",
                        "contains": {"metadata": True},
                    },
                    {"url": "javascript:alert(1)", "title": "Unsafe"},
                    {"url": "https://web.archive.org/example", "title": "Duplicate"},
                ],
            }
        ],
        "verdict": {"human_friendly": "Archived with metadata only."},
    }


def test_v5_links_are_normalized_and_unsafe_urls_removed():
    result = parse_response(response())
    assert len(result["links"]) == 1
    assert result["links"][0]["kind"] == "metadata"


def test_original_youtube_and_audio_links_have_distinct_labels():
    payload = response()
    payload["keys"].extend(
        [
            {
                "name": "YouTube",
                "classname": "YouTube",
                "available": [
                    {"url": f"https://youtu.be/{VID}", "contains": {"video": True}}
                ],
            },
            {
                "name": "Wayback",
                "available": [
                    {
                        "url": "https://archive.org/audio",
                        "contains": {"standalone_audio": True},
                    },
                    {"url": "https://archive.org/video", "contains": {"video": True}},
                ],
            },
        ]
    )
    assert [link["kind"] for link in parse_response(payload)["links"]] == [
        "metadata",
        "original",
        "audio",
        "video",
    ]


def test_creation_and_refresh_schedule_shared_checks_without_changing_selection(
    client, monkeypatch
):
    monkeypatch.setattr("youtube_download.finder.REQUEST_DELAY", 0)
    store = client.app.state.store
    row = client.post(
        "/api/playlists/custom",
        json={
            "title": "Review archives",
            "urls": f"https://youtu.be/{VID}\nhttps://youtu.be/{VID2}",
        },
    ).json()
    pid = row["playlist"]["id"]
    assert row["playlist"]["archive_pending"] == 2
    store.set_download_skipped(pid, VID2, True)
    calls = []

    def lookup(request):
        calls.append(request.url.path)
        return httpx.Response(200, json=response())

    finder = Finder(
        store,
        "https://finder.test",
        httpx.Client(transport=httpx.MockTransport(lookup)),
    )
    while finder.check_next():
        pass
    assert sorted(calls) == sorted([f"/api/v5/{VID}", f"/api/v5/{VID2}"])
    assert store.video(pid, VID)["recovery"] == store.video(PID, VID)["recovery"]
    assert not store.video(pid, VID)["download_skipped"]
    assert store.video(pid, VID2)["download_skipped"]
    compact = client.get(
        f"/api/playlists/{pid}", params={"compact_archives": True, "review_video": VID}
    ).json()
    first, second = compact["videos"]
    assert first["recovery"]["links"][0]["service"] == "Internet Archive"
    assert second["recovery"] == {"link_count": 1, "partial": False, "error": None}
    client.post(f"/api/playlists/{pid}/metadata").raise_for_status()
    assert (
        client.get(f"/api/playlists/{pid}").json()["playlist"]["archive_pending"] == 2
    )
    assert finder.check_next() and finder.check_next()
    assert len(calls) == 4
    assert not finder.check_next()
    # A newly created list reuses fresh results for an ID already reviewed.
    another = store.create_custom_playlist("Same video", [VID])
    assert not store.video(another, VID)["archive_pending"]
    client.post(f"/api/playlists/{PID}/refresh").raise_for_status()
    assert store.pending_archives(PID)


def test_manual_and_background_requests_share_cooldown_after_restart(
    store, monkeypatch
):
    clock = [1000.0]
    starts = []

    def wait(seconds):
        clock[0] += seconds
        return False

    def handle(request):
        starts.append(clock[0])
        wait(2)  # Simulate time spent receiving the response.
        if len(starts) == 1:
            return httpx.Response(429, headers={"Retry-After": "17"})
        if len(starts) == 3:
            return httpx.Response(302, headers={"Location": "/redirect-target"})
        return httpx.Response(200, json=response())

    monkeypatch.setattr(
        "youtube_download.finder.time", SimpleNamespace(time=lambda: clock[0])
    )
    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        first = Finder(store, "https://finder.test", client)
        assert first.check_next()
        assert store.video(PID, VID)["recovery"]["error"]
        reopened = Store(store.path, store.download_dir)
        second = Finder(reopened, "https://finder.test", client)
        monkeypatch.setattr(second.stop, "wait", wait)
        second.lookup(PID, VID, force=True)
        assert second.check_next()
        assert not second.check_next()
    assert starts == [1000, 1019, 1026, 1033]


def test_retry_after_http_date_and_missing_value(monkeypatch):
    monkeypatch.setattr("youtube_download.finder.time", SimpleNamespace(time=lambda: 0))
    assert (
        retry_delay(
            httpx.Response(
                503, headers={"Retry-After": "Thu, 01 Jan 1970 00:02:00 GMT"}
            )
        )
        == 120
    )
    assert retry_delay(httpx.Response(429)) == 60
    assert retry_delay(httpx.Response(429, headers={"Retry-After": "1"})) == 5


def test_legacy_cache_and_manual_recheck_survive_restart(store):
    previous = parse_response(response())
    with store.connect() as db:
        db.execute("DROP TABLE archive_checks")
        db.execute(
            "UPDATE videos SET recovery=?, recovery_checked_at=? WHERE id=?",
            (json.dumps(previous), "2026-11-01T00:00:00+00:00", VID),
        )
    reopened = Store(store.path, store.download_dir)
    reopened.queue_missing_archives(PID)
    assert reopened.next_archive_id() == VID2
    assert reopened.video(PID, VID)["recovery"] == previous
    reopened.queue_archive_check(VID)
    assert reopened.video(PID, VID)["archive_pending"]
    reopened.sync_videos(PID, [])
    assert reopened.next_archive_id() is None
    assert reopened.video(PID, VID)["recovery"] == previous


def test_media_waits_for_whole_scan_and_honors_selection_during_checks(
    store, settings, monkeypatch
):
    monkeypatch.setattr("youtube_download.finder.REQUEST_DELAY", 0)
    release = threading.Event()
    checking_second = threading.Event()

    def handle(request):
        if request.url.path.endswith(VID2):
            checking_second.set()
            assert release.wait(8)
        data = response()
        data["keys"][0]["available"][0]["contains"] = {"video": True}
        return httpx.Response(200, json=data)

    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        finder = Finder(store, "https://finder.test", client)
        manager = DownloadManager(store, settings, finder)
        downloads = Mock(
            side_effect=lambda playlist_id, video_id: save_video(store, video_id)
        )
        monkeypatch.setattr(manager, "_download_video", downloads)
        manager.enqueue(PID)
        finder.start()
        manager.start()
        try:
            assert checking_second.wait(5)
            wait_for(lambda: store.playlist(PID)["state"] == "running")
            assert not store.video(PID, VID)["download_skipped"]
            manager.set_skipped(PID, VID, True)
            downloads.assert_not_called()
            release.set()
            wait_for(lambda: store.playlist(PID)["state"] == "completed")
            downloads.assert_called_once_with(PID, VID2)
            assert not store.video(PID, VID2)["download_skipped"]
        finally:
            release.set()
            manager.close()
            finder.close()


def test_lookup_is_cached_and_errors_are_retryable(store):
    calls = []

    def handle(request):
        calls.append(request.url)
        return httpx.Response(200, json=response())

    finder = Finder(
        store,
        "https://finder.test",
        httpx.Client(transport=httpx.MockTransport(handle)),
    )
    finder.lookup(PID, VID)
    finder.lookup(PID, VID)
    assert len(calls) == 1
    assert str(calls[0]).endswith(f"/api/v5/{VID}")


def test_finder_failure_keeps_previous_links(store):
    ok = httpx.Client(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json=response())
        )
    )
    finder = Finder(store, "https://finder.test", ok)
    finder.lookup(PID, VID)
    finder.client = httpx.Client(
        transport=httpx.MockTransport(lambda request: httpx.Response(503))
    )
    result = finder.lookup(PID, VID, force=True)
    assert result["error"] and len(result["links"]) == 1


@pytest.mark.parametrize(
    "payload", [{}, {"status": "bad.id"}, {"status": "ok", "keys": None}]
)
def test_malformed_finder_response_is_rejected(payload):
    with pytest.raises(ValueError):
        parse_response(payload)
