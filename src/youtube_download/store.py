"""SQLite owns job intent; yt-dlp owns partial files. Every update is transactional."""

from contextlib import contextmanager
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3

from .config import contained_file


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


class Store:
    def __init__(self, path: Path, download_dir: Path):
        self.path = path
        self.download_dir = download_dir
        path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.executescript("""
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS playlists (
                    id TEXT PRIMARY KEY, title TEXT NOT NULL, description TEXT DEFAULT '',
                    thumbnail TEXT DEFAULT '', channel TEXT DEFAULT '', privacy TEXT DEFAULT '',
                    item_count INTEGER DEFAULT 0, synced_at TEXT, items_synced_at TEXT,
                    state TEXT DEFAULT 'idle', error TEXT DEFAULT '', queued_at TEXT,
                    current_video TEXT, progress REAL DEFAULT 0, download_dir TEXT DEFAULT ''
                );
                CREATE TABLE IF NOT EXISTS videos (
                    playlist_id TEXT NOT NULL REFERENCES playlists(id), id TEXT NOT NULL,
                    title TEXT NOT NULL, channel TEXT DEFAULT '', thumbnail TEXT DEFAULT '',
                    position INTEGER DEFAULT 0, in_playlist INTEGER DEFAULT 1,
                    availability TEXT DEFAULT 'unknown', status TEXT DEFAULT 'pending',
                    error TEXT DEFAULT '', file_path TEXT, size INTEGER DEFAULT 0,
                    downloaded_at TEXT, recovery TEXT, recovery_checked_at TEXT,
                    PRIMARY KEY (playlist_id, id)
                );
                CREATE INDEX IF NOT EXISTS video_status ON videos(playlist_id, status);
            """)
            columns = {row[1] for row in db.execute("PRAGMA table_info(playlists)")}
            if "download_dir" not in columns:
                db.execute(
                    "ALTER TABLE playlists ADD COLUMN download_dir TEXT DEFAULT ''"
                )

    def playlist_directory(self, playlist_id: str) -> Path:
        playlist = self.playlist(playlist_id)
        return (
            Path(playlist["download_dir"]).resolve()
            if playlist and playlist["download_dir"]
            else self.download_dir / playlist_id
        )

    @contextmanager
    def connect(self):
        # One short-lived connection per transaction avoids sharing connections between
        # FastAPI request threads and the background downloader. WAL keeps reads available.
        db = sqlite3.connect(self.path, timeout=15)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        try:
            with db:
                yield db
        finally:
            db.close()

    def sync_playlists(self, playlists: list[dict]):
        with self.connect() as db:
            for playlist in playlists:
                db.execute(
                    """
                    INSERT INTO playlists(id,title,description,thumbnail,channel,privacy,item_count,synced_at)
                    VALUES(:id,:title,:description,:thumbnail,:channel,:privacy,:item_count,:synced_at)
                    ON CONFLICT(id) DO UPDATE SET title=excluded.title, description=excluded.description,
                    thumbnail=excluded.thumbnail, channel=excluded.channel, privacy=excluded.privacy,
                    item_count=excluded.item_count, synced_at=excluded.synced_at
                """,
                    {**playlist, "synced_at": now()},
                )

    def sync_videos(self, playlist_id: str, videos: list[dict]):
        # Fetch the entire remote snapshot before this transaction: a failed API page
        # must never mark the remaining local videos as removed.
        with self.connect() as db:
            db.execute(
                "UPDATE videos SET in_playlist=0 WHERE playlist_id=?", (playlist_id,)
            )
            seen = set()
            for video in videos:
                if video["id"] in seen:
                    continue  # Duplicate playlist entries share one local file.
                seen.add(video["id"])
                db.execute(
                    """
                    INSERT INTO videos(playlist_id,id,title,channel,thumbnail,position,availability,status)
                    VALUES(:playlist_id,:id,:title,:channel,:thumbnail,:position,:availability,:status)
                    ON CONFLICT(playlist_id,id) DO UPDATE SET
                    title=CASE WHEN excluded.availability='unavailable' THEN videos.title ELSE excluded.title END,
                    channel=CASE WHEN excluded.channel='' THEN videos.channel ELSE excluded.channel END,
                    thumbnail=CASE WHEN excluded.thumbnail='' THEN videos.thumbnail ELSE excluded.thumbnail END,
                    position=excluded.position,
                    availability=excluded.availability, in_playlist=1,
                    status=CASE WHEN videos.status='downloaded' THEN 'downloaded'
                        WHEN excluded.availability='unavailable' THEN 'unavailable'
                        WHEN videos.status='unavailable' THEN 'pending' ELSE videos.status END
                """,
                    {
                        **video,
                        "playlist_id": playlist_id,
                        "status": "unavailable"
                        if video["availability"] == "unavailable"
                        else "pending",
                    },
                )
            db.execute(
                "UPDATE playlists SET items_synced_at=?, item_count=? WHERE id=?",
                (now(), len(videos), playlist_id),
            )

    def playlist(self, playlist_id: str) -> dict | None:
        with self.connect() as db:
            row = db.execute(
                "SELECT * FROM playlists WHERE id=?", (playlist_id,)
            ).fetchone()
        return dict(row) if row else None

    def playlists(self) -> list[dict]:
        with self.connect() as db:
            rows = db.execute(
                "SELECT * FROM playlists ORDER BY title COLLATE NOCASE"
            ).fetchall()
        return [self.playlist_summary(dict(row)) for row in rows]

    def playlist_summary(self, row: dict, videos: list[dict] | None = None) -> dict:
        videos = self.videos(row["id"]) if videos is None else videos
        current = [v for v in videos if v["in_playlist"]]
        return {
            **dict(row),
            "download_dir": str(self.playlist_directory(row["id"])),
            "downloaded": sum(v["local_available"] for v in current),
            "local_count": sum(v["local_available"] for v in videos),
            "total": len(current) if row["items_synced_at"] else row["item_count"],
            "unavailable": sum(
                v["status"] == "unavailable" and not v["local_available"]
                for v in current
            ),
            "failed": sum(v["status"] == "failed" for v in current),
            "bytes": sum(v["size"] for v in videos if v["local_available"]),
        }

    def videos(self, playlist_id: str) -> list[dict]:
        with self.connect() as db:
            rows = db.execute(
                "SELECT * FROM videos WHERE playlist_id=? ORDER BY in_playlist DESC, position, id",
                (playlist_id,),
            ).fetchall()
        directory = self.playlist_directory(playlist_id)
        return [self._video_view(row, directory) for row in rows]

    @staticmethod
    def _video_view(row, directory: Path) -> dict:
        item = dict(row)
        item["local_available"] = (
            contained_file(directory, item["file_path"]) is not None
        )
        if item["status"] == "downloaded" and not item["local_available"]:
            item["status"] = "missing"
        item["recovery"] = json.loads(item["recovery"]) if item["recovery"] else None
        return item

    def video(self, playlist_id: str, video_id: str) -> dict | None:
        with self.connect() as db:
            row = db.execute(
                "SELECT * FROM videos WHERE playlist_id=? AND id=?",
                (playlist_id, video_id),
            ).fetchone()
        return (
            self._video_view(row, self.playlist_directory(playlist_id)) if row else None
        )

    def update_video(self, playlist_id: str, video_id: str, **fields):
        allowed = {
            "status",
            "error",
            "file_path",
            "size",
            "downloaded_at",
            "recovery",
            "recovery_checked_at",
        }
        if not fields or not fields.keys() <= allowed:
            raise ValueError("Invalid video update")
        with self.connect() as db:
            db.execute(
                f"UPDATE videos SET {','.join(f'{key}=?' for key in fields)} WHERE playlist_id=? AND id=?",
                (*fields.values(), playlist_id, video_id),
            )

    def update_playlist(self, playlist_id: str, **fields):
        allowed = {
            "state",
            "error",
            "queued_at",
            "current_video",
            "progress",
            "download_dir",
        }
        if not fields or not fields.keys() <= allowed:
            raise ValueError("Invalid playlist update")
        with self.connect() as db:
            db.execute(
                f"UPDATE playlists SET {','.join(f'{key}=?' for key in fields)} WHERE id=?",
                (*fields.values(), playlist_id),
            )

    def recover_jobs(self):
        with self.connect() as db:
            db.execute(
                "UPDATE playlists SET state='queued', current_video=NULL, progress=0 WHERE state='running'"
            )
            db.execute("UPDATE videos SET status='pending' WHERE status='downloading'")

    def reset_inflight_videos(self, playlist_id: str):
        with self.connect() as db:
            db.execute(
                "UPDATE videos SET status='pending' WHERE playlist_id=? AND status='downloading'",
                (playlist_id,),
            )

    def next_job(self) -> str | None:
        with self.connect() as db:
            row = db.execute(
                "SELECT id FROM playlists WHERE state='queued' ORDER BY queued_at LIMIT 1"
            ).fetchone()
        return row["id"] if row else None
