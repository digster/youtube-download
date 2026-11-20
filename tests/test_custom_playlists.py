import json
import re
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import Mock

import pytest
from conftest import PID, VID, VID2, FakeAccount, playlist

from youtube_download.custom_playlists import parse_video_urls, video_id_from_url
from youtube_download.downloads import DownloadManager, build_command
from youtube_download.store import Store


@pytest.mark.parametrize(
    "url",
    [
        f"https://www.youtube.com/watch?v={VID}",
        f"http://youtube.com/watch?v={VID}&list=PLother&t=20",
        f"https://youtu.be/{VID}?si=tracking",
        f"youtu.be/{VID}",
        f"https://m.youtube.com/watch?v={VID}",
        f"https://music.youtube.com/watch?v={VID}",
        f"https://www.youtube.com/shorts/{VID}",
        f"https://youtube.com/live/{VID}",
        f"https://www.youtube.com/embed/{VID}",
        f"https://www.youtube-nocookie.com/embed/{VID}",
    ],
)
def test_common_youtube_urls_normalize_to_video_ids(url):
    assert video_id_from_url(url) == VID


@pytest.mark.parametrize(
    "url",
    [
        "https://youtube.com/playlist?list=PLtest",
        "https://youtube.com/@channel",
        VID,
        f"https://youtube.com.evil.test/watch?v={VID}",
        f"https://youtube.com@evil.test/watch?v={VID}",
        f"https://user:pass@youtube.com/watch?v={VID}",
        f"https://youtube.com:123/watch?v={VID}",
        f"file://youtube.com/watch?v={VID}",
        f"https://localhost/watch?v={VID}",
        f"https://youtube.com/watch?v={VID}&v={VID2}",
        "https://youtu.be/short",
        f"https://youtu.be/{VID}/extra",
        f"https://youtube.com/redirect?q=https://youtu.be/{VID}",
        f"https://youtube.com\\@evil.test/watch?v={VID}",
        "https://[malformed",
    ],
)
def test_non_video_or_untrusted_urls_are_rejected(url):
    with pytest.raises(ValueError):
        video_id_from_url(url)


def test_pasted_list_preserves_order_and_deduplicates():
    assert parse_video_urls(
        f"\n https://youtu.be/{VID2} \nhttps://youtube.com/watch?v={VID}\nhttps://youtube.com/shorts/{VID2}\n"
    ) == [VID2, VID]
    with pytest.raises(ValueError, match=r"line\(s\) 3"):
        parse_video_urls(f"https://youtu.be/{VID}\n\ninvalid")
    with pytest.raises(ValueError, match="at least one"):
        parse_video_urls("\n  ")


def test_large_custom_playlist_import_has_no_video_count_cap(client):
    # A 5,001-video Watch Later export exceeds both former limits: 500 entries
    # and 200,000 characters. Import it atomically without truncation or reordering.
    ids = [f"{index:011d}" for index in range(5001)]
    response = client.post(
        "/api/playlists/custom",
        json={
            "title": "Large Watch Later",
            "urls": "\n".join(
                f"https://www.youtube.com/watch?v={value}" for value in ids
            ),
        },
    )
    assert response.status_code == 201
    assert [video["id"] for video in response.json()["videos"]] == ids
    assert response.json()["playlist"]["total"] == 5001


def test_large_duplicate_input_still_deduplicates():
    assert parse_video_urls((f"https://youtu.be/{VID}\n") * 501) == [VID]


def test_edit_urls_preserves_order_saved_files_and_shared_work(client):
    store = client.app.state.store
    third, fourth = "abcdefghijk", "00000000001"
    pid = store.create_custom_playlist("Edit me", [VID, VID2])
    url = f"/api/playlists/{pid}/urls"

    def edit(action, ids):
        urls = "\n".join(f"https://youtu.be/{video_id}" for video_id in ids)
        return client.post(url, json={"action": action, "urls": urls}).json()

    folder = store.playlist_directory(pid)
    media = folder / VID / f"{VID}.mp4"
    media.parent.mkdir(parents=True)
    media.write_bytes(b"saved")
    store.update_video(
        pid, VID, file_path=str(media.relative_to(folder)), title="Saved title",
        status="downloaded", metadata_status="updated",
    )
    store.set_download_skipped(pid, VID, True)
    with store.connect() as db:
        db.execute("UPDATE archive_checks SET checked_at='2020-01-01',requested_at=NULL WHERE video_id=?", (VID2,))
    assert edit("add", [third, VID, fourth, third]) == {"changed": 2, "unchanged": 1}
    assert store.archive_check(third)["requested_at"]
    assert not store.archive_check(VID2)["requested_at"]  # No stale-cache rescan for untouched members.
    assert edit("add", [fourth]) == {"changed": 0, "unchanged": 1}
    assert [v["id"] for v in store.videos(pid)] == [VID, VID2, third, fourth]
    assert edit("remove", [VID, third, "00000000002"]) == {"changed": 2, "unchanged": 1}
    assert edit("remove", [VID]) == {"changed": 0, "unchanged": 1}
    assert store.archive_check(VID)["requested_at"]  # Still needed by the synced playlist.
    assert not store.archive_check(third)["requested_at"]
    assert client.get(f"/api/playlists/{pid}/videos/{VID}/file").status_code == 404
    assert media.read_bytes() == b"saved"
    assert edit("add", [VID])["changed"] == 1
    reopened = Store(store.path, store.download_dir)
    assert [v["id"] for v in reopened.videos(pid)] == [VID2, fourth, VID]
    saved = reopened.video(pid, VID)
    assert saved["title"] == "Saved title" and saved["local_available"] and saved["download_skipped"]
    assert saved["metadata_status"] == "updated"
    assert reopened.playlist(pid)["item_count"] == 3


def test_concurrent_url_appends_merge_batches_once(store):
    pid = store.create_custom_playlist("Two tabs", [VID])
    shared = "00000000000"
    first, second = ["00000000001", shared], ["00000000002", shared]
    with ThreadPoolExecutor(max_workers=2) as pool:
        edits = [pool.submit(store.edit_custom_urls, pid, batch, remove=False) for batch in (first, second)]
        assert sorted(edit.result() for edit in edits) == [1, 2]
    ids = [row["id"] for row in store.videos(pid)]
    assert ids in ([VID, *first, second[0]], [VID, *second, first[0]])


def test_url_edit_upgrade_keeps_preexisting_retained_rows_visible(store):
    pid = store.create_custom_playlist("Older library", [VID])
    with store.connect() as db:
        db.execute("ALTER TABLE videos DROP COLUMN user_removed")
        db.execute("UPDATE videos SET in_playlist=0 WHERE playlist_id=?", (pid,))
    reopened = Store(store.path, store.download_dir)
    assert reopened.video(pid, VID)["in_playlist"] == 0
    assert reopened.edit_custom_urls(pid, [VID], remove=True) == 1
    assert reopened.video(pid, VID) is None


def test_edit_urls_validates_whole_batch_and_source(client):
    store = client.app.state.store
    pid = store.create_custom_playlist("Editable", [VID])
    for action in ("add", "remove"):
        body = {"action": action, "urls": f"https://youtu.be/{VID2}\ninvalid"}
        response = client.post(f"/api/playlists/{pid}/urls", json=body)
        assert response.status_code == 400 and "line(s) 2" in response.json()["detail"]
        body["urls"] = f"https://youtu.be/{VID}"
        assert client.post(f"/api/playlists/{PID}/urls", json=body).status_code == 400
        assert client.post(f"/api/playlists/{pid}/urls", json=body, headers={"X-Local-App": ""}).status_code == 403
    assert [v["id"] for v in store.videos(pid)] == [VID]


def test_bulk_edits_can_empty_and_refill_a_large_playlist(client):
    store = client.app.state.store
    ids = [f"{n:011d}" for n in range(5001)]
    pid = store.create_custom_playlist("Large", ids)
    url = f"/api/playlists/{pid}/urls"
    urls = "\n".join(f"https://youtu.be/{i}" for i in reversed(ids))
    assert client.post(url, json={"action": "remove", "urls": urls}).json()["changed"] == len(ids)
    assert store.playlist(pid)["item_count"] == 0 and store.videos(pid) == []
    assert client.post(url, json={"action": "add", "urls": urls}).json()["changed"] == len(ids)
    assert [v["id"] for v in store.videos(pid)] == ids[::-1]


def test_edit_urls_updates_finished_jobs_and_respects_worker_ownership(client):
    store = client.app.state.store
    pid = store.create_custom_playlist("Jobs", [VID])
    store.update_playlist(pid, state="completed")
    store.set_download_skipped(pid, VID, True)
    store.queue_metadata(pid)
    store.update_video(pid, VID, metadata_status="updated")
    store.update_metadata_job(pid, state="completed")
    url = f"/api/playlists/{pid}/urls"
    change = {"action": "add", "urls": f"https://youtu.be/{VID2}"}
    for worker in (client.app.state.manager, client.app.state.metadata):
        worker.active_playlist = pid
        assert client.post(url, json=change).status_code == 409
        assert store.video(pid, VID2) is None
        worker.active_playlist = None
    assert client.post(url, json=change).json()["changed"] == 1
    assert store.playlist(pid)["state"] == "idle"
    assert store.metadata_job(pid)["state"] == "paused"
    assert store.pending_metadata_ids(pid) == [VID2]
    client.app.state.manager.enqueue(pid)
    client.app.state.metadata.enqueue(pid, resume=True)
    assert client.post(url, json={**change, "action": "remove"}).json()["changed"] == 1
    assert store.next_job() == pid and store.next_metadata_job() == pid
    assert store.pending_metadata_ids(pid) == []


def test_deleting_last_visible_copy_drops_archive_work(client):
    store = client.app.state.store
    unique = "abcdefghijk"
    a = store.create_custom_playlist("First", [unique])
    b = store.create_custom_playlist("Second", [unique])
    client.post(f"/api/playlists/{a}/urls", json={"action": "remove", "urls": f"https://youtu.be/{unique}"}).raise_for_status()
    assert store.archive_check(unique)["requested_at"]
    client.delete(f"/api/playlists/{b}").raise_for_status()
    row = store.archive_check(unique)
    assert row is None or not row["requested_at"]


def test_edit_urls_keeps_membership_and_archive_queue_atomic(client):
    store = client.app.state.store
    pid = store.create_custom_playlist("Atomic", [VID])
    unique = "abcdefghijk"
    with store.connect() as db:
        db.execute("""CREATE TRIGGER fail_count BEFORE UPDATE OF item_count ON playlists
                      BEGIN SELECT RAISE(ABORT, 'simulated failure'); END""")
    response = client.post(f"/api/playlists/{pid}/urls", json={"action": "add", "urls": f"https://youtu.be/{unique}"})
    assert response.status_code == 500
    assert store.playlist(pid)["item_count"] == 1
    assert store.video(pid, unique) is None and store.archive_check(unique) is None


def test_custom_creation_needs_no_account_and_queues_normally(client, monkeypatch):
    no_account = Mock(side_effect=AssertionError("Custom lists must not call Google"))
    monkeypatch.setattr(FakeAccount, "videos", no_account)
    response = client.post(
        "/api/playlists/custom",
        json={
            "title": "  Weekend watchlist  ",
            "urls": f"https://youtu.be/{VID2}\nhttps://youtube.com/watch?v={VID}\nhttps://youtu.be/{VID2}",
        },
    )
    assert response.status_code == 201
    result = response.json()
    row = result["playlist"]
    assert row["source"] == "custom" and row["title"] == "Weekend watchlist"
    assert row["total"] == 2 and row["items_synced_at"]
    assert [v["id"] for v in result["videos"]] == [VID2, VID]
    assert (
        result["videos"][0]["youtube_url"] == f"https://www.youtube.com/watch?v={VID2}"
    )
    assert re.fullmatch(
        r"Weekend watchlist - [a-f0-9]{6}", Path(row["download_dir"]).name
    )
    url = f"/api/playlists/{row['id']}"
    assert client.post(f"{url}/download").json()["playlist"]["state"] == "queued"
    assert client.post(f"{url}/pause").json()["playlist"]["state"] == "paused"
    assert client.post(f"{url}/refresh").status_code == 400
    assert [v["id"] for v in client.get(url).json()["videos"]] == [VID2, VID]
    no_account.assert_not_called()
    reopened = Store(client.app.state.store.path, client.app.state.store.download_dir)
    assert reopened.playlist(row["id"])["source"] == "custom"
    assert reopened.playlist_directory(row["id"]) == Path(row["download_dir"])


@pytest.mark.parametrize(
    "title,urls",
    [
        ("  ", f"https://youtu.be/{VID}"),
        ("Bad list", ""),
        ("Bad list", f"https://youtu.be/{VID}\nhttps://example.com/video"),
    ],
)
def test_invalid_custom_playlist_does_not_leave_partial_rows(client, title, urls):
    before = client.get("/api/playlists").json()
    assert (
        client.post(
            "/api/playlists/custom", json={"title": title, "urls": urls}
        ).status_code
        == 400
    )
    assert client.get("/api/playlists").json() == before


def test_custom_creation_keeps_local_request_guards(client):
    body = {"title": "My list", "urls": f"https://youtu.be/{VID}"}
    assert (
        client.post(
            "/api/playlists/custom", json=body, headers={"X-Local-App": ""}
        ).status_code
        == 403
    )
    assert (
        client.post(
            "/api/playlists/custom", json=body, headers={"Origin": "https://evil.test"}
        ).status_code
        == 403
    )


def test_only_youtube_playlists_expose_original_playlist_links(client):
    custom = client.post(
        "/api/playlists/custom",
        json={"title": "Local", "urls": f"https://youtu.be/{VID}"},
    ).json()["playlist"]
    assert custom["youtube_url"] is None
    original = client.get(f"/api/playlists/{PID}").json()["playlist"]
    assert original["youtube_url"] == f"https://www.youtube.com/playlist?list={PID}"
    summaries = {p["id"]: p for p in client.get("/api/playlists").json()["playlists"]}
    assert summaries[PID]["youtube_url"] == original["youtube_url"]
    assert summaries[custom["id"]]["youtube_url"] is None


def test_remove_custom_playlist_keeps_files_and_shared_video_records(client):
    store = client.app.state.store
    unique = "abcdefghijk"
    custom = store.create_custom_playlist("Remove me", [VID, unique])
    url = f"/api/playlists/{custom}"
    folder = store.playlist_directory(custom)
    media = folder / VID / f"{VID}.mp4"
    media.parent.mkdir(parents=True)
    media.write_bytes(b"saved media")
    partial = folder / "unfinished.part"
    partial.write_bytes(b"partial download")
    store.update_video(custom, VID, file_path=str(media.relative_to(folder)))
    client.app.state.manager.enqueue(custom)
    client.app.state.metadata.enqueue(custom)
    shared_check = dict(store.archive_check(VID))

    assert client.delete(url).status_code == 200
    assert client.get(url).status_code == client.delete(url).status_code == 404
    reopened = Store(store.path, store.download_dir)
    assert [p["id"] for p in reopened.playlists()] == [PID]
    assert reopened.videos(custom) == []
    with reopened.connect() as db:
        assert not db.execute(
            "SELECT 1 FROM metadata_jobs WHERE playlist_id=?", (custom,)
        ).fetchone()
    assert reopened.archive_check(unique) is None
    assert dict(reopened.archive_check(VID)) == shared_check
    assert reopened.video(PID, VID)
    assert media.read_bytes() == b"saved media"
    assert partial.read_bytes() == b"partial download"


def test_removal_requires_local_request_and_custom_source(client):
    store = client.app.state.store
    custom = store.create_custom_playlist("Local", [VID])
    url = f"/api/playlists/{custom}"
    for headers in ({"X-Local-App": ""}, {"Origin": "https://other.test"}):
        assert client.delete(url, headers=headers).status_code == 403
    assert store.playlist(custom)
    assert client.delete(f"/api/playlists/{PID}").status_code == 400
    assert store.video(PID, VID)


def test_removal_rolls_back_on_database_failure(client):
    store = client.app.state.store
    unique = "abcdefghijk"
    custom = store.create_custom_playlist("Keep on failure", [unique])
    client.app.state.metadata.enqueue(custom)
    with store.connect() as db:
        db.execute("""CREATE TRIGGER reject_removal BEFORE DELETE ON playlists
                      BEGIN SELECT RAISE(ABORT, 'simulated failure'); END""")
    response = client.delete(f"/api/playlists/{custom}")
    assert response.status_code == 500
    assert "library database" in response.json()["detail"]
    assert store.playlist(custom) and store.video(custom, unique)
    assert store.metadata_job(custom)["state"] == "queued"
    assert store.archive_check(unique)["requested_at"]


@pytest.mark.parametrize("worker", ["manager", "metadata"])
def test_removal_waits_for_worker_to_release_playlist(client, worker):
    store = client.app.state.store
    custom = store.create_custom_playlist("Busy", [VID])
    url = f"/api/playlists/{custom}"
    manager = getattr(client.app.state, worker)
    manager.enqueue(custom)
    manager.active_playlist = custom
    if worker == "manager":
        store.update_playlist(custom, state="running")
    else:
        store.update_metadata_job(custom, state="running")
    assert client.delete(url).status_code == 409
    # Pause/cancel changes the stored state before the worker finishes cleanup.
    action = "pause" if worker == "manager" else "metadata/cancel"
    assert client.post(f"{url}/{action}").status_code == 200
    assert client.delete(url).status_code == 409
    assert store.video(custom, VID)
    manager.active_playlist = None
    assert client.delete(url).status_code == 200


def test_account_sync_cannot_overwrite_custom_list(store):
    custom = store.create_custom_playlist("Mine", [VID, VID2])
    original = store.playlist(custom)
    store.sync_playlists([playlist(custom, title="Remote collision")])
    assert store.playlist(custom) == original
    assert [v["id"] for v in store.videos(custom)] == [VID, VID2]


def test_new_folder_name_is_stable_after_rename_and_restart(store):
    original = store.playlist_directory(PID)
    assert re.fullmatch(r"My playlist - [a-f0-9]{6}", original.name)
    store.sync_playlists([playlist(title="Renamed playlist")])
    assert store.playlist_directory(PID) == original
    assert Store(store.path, store.download_dir).playlist_directory(PID) == original
    command = build_command(
        Mock(
            download_dir=store.download_dir,
            format="best",
            cookies_file="",
            cookies_browser="",
        ),
        PID,
        VID,
        original,
    )
    assert command[command.index("--output") + 1].startswith(str(original / VID))


def test_custom_names_are_sanitized_and_duplicates_have_different_folders(store):
    title = ' ../Music / live: "favourites"? 🎵 '
    one = store.create_custom_playlist(title, [VID])
    two = store.create_custom_playlist(title, [VID])
    a, b = store.playlist_directory(one), store.playlist_directory(two)
    assert a != b and a.parent == b.parent == store.download_dir
    assert not any(c in a.name for c in '/\\:*?"<>|')
    assert "🎵" in a.name and len(a.name) <= 109


def test_random_folder_suffix_retries_existing_path_and_db_collision(
    store, monkeypatch
):
    existing = store.download_dir / "Name - abcdef"
    existing.mkdir(parents=True)
    monkeypatch.setattr(
        "youtube_download.store.secrets.token_hex",
        Mock(side_effect=["abcdef", "123456", "123456", "654321"]),
    )
    one = store.create_custom_playlist("Name", [VID])
    two = store.create_custom_playlist("Name", [VID])
    assert store.playlist_directory(one).name == "Name - 123456"
    assert store.playlist_directory(two).name == "Name - 654321"


def test_long_unicode_titles_fit_filesystem_component_limits(store):
    custom = store.create_custom_playlist("🎵" * 150, [VID])
    folder = store.playlist_directory(custom)
    assert len(folder.name.encode("utf-8")) <= 240
    folder.mkdir(parents=True)
    assert folder.is_dir()


def test_legacy_schema_and_id_folders_remain_usable(store):
    old = store.download_dir / PID
    (old / VID).mkdir(parents=True)
    partial = old / VID / f"{VID}.mp4.part"
    partial.write_bytes(b"partial download")
    with store.connect() as db:
        db.execute("UPDATE playlists SET download_dir='' WHERE id=?", (PID,))
        db.execute("ALTER TABLE playlists DROP COLUMN source")
    reopened = Store(store.path, store.download_dir)
    reopened.sync_playlists([playlist(title="Changed title")])
    assert reopened.playlist(PID)["source"] == "youtube"
    assert reopened.playlist_directory(PID) == old
    assert partial.read_bytes() == b"partial download"


@pytest.mark.parametrize(
    "info",
    [
        {"id": VID, "title": "Actual title", "channel": "Actual channel"},
        {"id": VID2, "title": "Wrong video"},
        [],
        "invalid-json",
    ],
)
def test_completed_custom_download_enriches_only_matching_metadata(
    store, settings, info
):
    custom = store.create_custom_playlist("Clips", [VID])
    folder = store.playlist_directory(custom) / VID
    folder.mkdir(parents=True)
    path = folder / f"{VID}.mp4"
    path.write_bytes(b"completed")
    (folder / f"{VID}.info.json").write_text(
        json.dumps(info) if info != "invalid-json" else "not json"
    )
    DownloadManager(store, settings, Mock())._save_file(custom, VID, path)
    saved = store.video(custom, VID)
    assert saved["local_available"]
    if isinstance(info, dict) and info.get("id") == VID:
        assert saved["title"] == "Actual title" and saved["channel"] == "Actual channel"
        assert (folder / "Actual title.txt").read_bytes() == b""
    else:
        assert saved["title"] == f"YouTube video · {VID}"


def test_unreadable_optional_metadata_does_not_fail_completed_video(
    store, settings, monkeypatch
):
    custom = store.create_custom_playlist("Clips", [VID])
    path = store.playlist_directory(custom) / VID / f"{VID}.mp4"
    path.parent.mkdir(parents=True)
    path.write_bytes(b"completed")
    monkeypatch.setattr(
        "youtube_download.downloads.contained_file",
        Mock(side_effect=PermissionError("metadata denied")),
    )
    DownloadManager(store, settings, Mock())._save_file(custom, VID, path)
    assert store.video(custom, VID)["local_available"]
