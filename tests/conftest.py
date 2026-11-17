from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from youtube_download.app import create_app
from youtube_download.config import Settings
from youtube_download.store import Store

PID = "PLtest123"
VID = "dQw4w9WgXcQ"
VID2 = "jNQXAC9IVRw"


def playlist(id=PID, **kwargs):
    return {
        "id": id,
        "title": "My playlist",
        "description": "Saved favourites",
        "thumbnail": "",
        "channel": "My account",
        "privacy": "private",
        "item_count": 2,
        **kwargs,
    }


def video(id=VID, **kwargs):
    return {
        "id": id,
        "title": "A video",
        "position": 0,
        "channel": "A channel",
        "thumbnail": "",
        "availability": "unknown",
        **kwargs,
    }


class FakeAccount:
    def status(self):
        return {"authenticated": True, "provider": "youtube-backup", "expired": False}

    def playlists(self):
        return [playlist()]

    def videos(self, playlist_id):
        return [
            video(),
            video(VID2, title="Deleted video", availability="unavailable", position=1),
        ]


@pytest.fixture
def settings(tmp_path):
    return Settings(data_dir=tmp_path / "data", download_dir=tmp_path / "downloads")


@pytest.fixture
def store(settings):
    store = Store(settings.data_dir / "library.sqlite3", settings.download_dir)
    store.sync_playlists([playlist()])
    store.sync_videos(PID, [video(), video(VID2, position=1)])
    return store


@pytest.fixture
def client(settings):
    app = create_app(settings, FakeAccount(), start_worker=False)
    with TestClient(
        app, base_url="http://127.0.0.1", headers={"X-Local-App": "1"}
    ) as client:
        client.post("/api/sync")
        client.post(f"/api/playlists/{PID}/refresh")
        yield client


def save_video(store, video_id=VID, content=b"local video") -> Path:
    path = store.playlist_directory(PID) / video_id / f"{video_id}.mp4"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    store.update_video(
        PID,
        video_id,
        status="downloaded",
        file_path=f"{video_id}/{video_id}.mp4",
        size=len(content),
    )
    return path
