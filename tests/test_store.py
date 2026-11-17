import shutil

from conftest import PID, VID, VID2, playlist, save_video, video
from youtube_download.config import contained_file


def test_sync_preserves_downloads_and_removed_videos(store):
    path = save_video(store)
    store.sync_videos(PID, [video(VID2, title="New title")])
    old = store.video(PID, VID)
    assert old["local_available"] and not old["in_playlist"]
    assert path.is_file()
    assert store.playlists()[0]["local_count"] == 1
    assert store.playlists()[0]["downloaded"] == 0


def test_duplicate_ids_download_once_preserving_first_position(store):
    store.sync_videos(PID, [video(position=1), video(position=9)])
    current = [v for v in store.videos(PID) if v["in_playlist"]]
    assert len(current) == 1 and current[0]["position"] == 1
    assert store.playlists()[0]["total"] == 1


def test_deleted_file_reports_missing_and_is_not_counted(store):
    save_video(store).unlink()
    assert store.video(PID, VID)["status"] == "missing"
    assert store.playlists()[0]["downloaded"] == 0


def test_moved_folder_reconnects_without_changing_files(store, tmp_path):
    save_video(store)
    old = store.playlist_directory(PID)
    new = tmp_path / "New drive" / "My playlist"
    new.parent.mkdir()
    shutil.move(old, new)
    assert store.video(PID, VID)["status"] == "missing"
    store.update_playlist(PID, download_dir=str(new))
    assert store.video(PID, VID)["local_available"]
    assert store.playlists()[0]["downloaded"] == 1


def test_restart_recovers_running_but_keeps_user_pause(store):
    store.sync_playlists([playlist("PLpaused")])
    store.update_playlist(PID, state="running")
    store.update_playlist("PLpaused", state="paused")
    store.update_video(PID, VID, status="downloading")
    store.recover_jobs()
    assert store.next_job() == PID
    assert store.playlist("PLpaused")["state"] == "paused"
    assert store.video(PID, VID)["status"] == "pending"


def test_resync_keeps_local_file_for_newly_unavailable_video(store):
    save_video(store)
    store.sync_videos(PID, [video(title="Deleted video", availability="unavailable")])
    assert store.video(PID, VID)["status"] == "downloaded"
    assert store.video(PID, VID)["local_available"]


def test_private_becomes_available_on_refresh(store):
    store.sync_videos(PID, [video(availability="unavailable")])
    assert store.video(PID, VID)["status"] == "unavailable"
    store.sync_videos(PID, [video()])
    assert store.video(PID, VID)["status"] == "pending"


def test_media_paths_cannot_escape_through_symlinks(tmp_path):
    root = tmp_path / "library"
    root.mkdir()
    secret = tmp_path / "secret.txt"
    secret.write_text("not media")
    (root / "link.mp4").symlink_to(secret)
    assert contained_file(root, "link.mp4") is None
    assert contained_file(root, "../secret.txt") is None


def test_empty_playlist_snapshot_is_valid(store):
    store.sync_videos(PID, [])
    assert store.playlists()[0]["total"] == 0


def test_unavailable_snapshot_keeps_last_known_metadata(store):
    store.sync_videos(
        PID, [video(title="An irreplaceable title", channel="Original channel")]
    )
    store.sync_videos(
        PID, [video(title="Deleted video", channel="", availability="unavailable")]
    )
    saved = store.video(PID, VID)
    assert saved["title"] == "An irreplaceable title"
    assert saved["channel"] == "Original channel"
