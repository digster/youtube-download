"""SQLite owns job intent; yt-dlp owns partial files. Every update is transactional."""

from contextlib import contextmanager
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import secrets
import sqlite3
from urllib.parse import quote
from uuid import uuid4

from .config import VIDEO_ID, contained_file


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
                    current_video TEXT, progress REAL DEFAULT 0, download_dir TEXT DEFAULT '',
                    source TEXT NOT NULL DEFAULT 'youtube'
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
                CREATE TABLE IF NOT EXISTS metadata_jobs (
                    playlist_id TEXT PRIMARY KEY REFERENCES playlists(id),
                    state TEXT NOT NULL, queued_at TEXT, current_video TEXT,
                    error TEXT DEFAULT '', finished_at TEXT
                );
            """)
            columns = {row[1] for row in db.execute("PRAGMA table_info(playlists)")}
            if "download_dir" not in columns:
                db.execute(
                    "ALTER TABLE playlists ADD COLUMN download_dir TEXT DEFAULT ''"
                )
            if "source" not in columns:
                db.execute(
                    "ALTER TABLE playlists ADD COLUMN source TEXT NOT NULL DEFAULT 'youtube'"
                )
            video_columns = {row[1] for row in db.execute("PRAGMA table_info(videos)")}
            for name, definition in {
                "download_skipped": "INTEGER NOT NULL DEFAULT 0",
                "metadata_status": "TEXT NOT NULL DEFAULT 'pending'",
                "metadata_error": "TEXT DEFAULT ''",
                "metadata_updated_at": "TEXT",
            }.items():
                if name not in video_columns:
                    db.execute(f"ALTER TABLE videos ADD COLUMN {name} {definition}")

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
                existing = db.execute(
                    "SELECT id FROM playlists WHERE id=?", (playlist["id"],)
                ).fetchone()
                directory = (
                    ""
                    if existing
                    else self._new_playlist_directory(db, playlist["title"])
                )
                db.execute(
                    """
                    INSERT INTO playlists(id,title,description,thumbnail,channel,privacy,item_count,synced_at,download_dir)
                    VALUES(:id,:title,:description,:thumbnail,:channel,:privacy,:item_count,:synced_at,:download_dir)
                    ON CONFLICT(id) DO UPDATE SET title=excluded.title, description=excluded.description,
                    thumbnail=excluded.thumbnail, channel=excluded.channel, privacy=excluded.privacy,
                    item_count=excluded.item_count, synced_at=excluded.synced_at
                    WHERE playlists.source='youtube'
                """,
                    {**playlist, "synced_at": now(), "download_dir": directory},
                )

    def _new_playlist_directory(self, db, title: str) -> str:
        # Only allocate once. Renames and syncs must not detach .part files or
        # downloaded media. Old rows with no override keep their legacy ID folder.
        name = re.sub(r'[<>:"/\\|?*\x00-\x1f\x7f]', " ", title)
        name = " ".join(name.split()).strip(" .")[:100]
        # Filesystem component limits count bytes on Linux; emoji and other
        # multibyte titles need a byte cap as well as a readable character cap.
        name = (
            name.encode("utf-8")[:180].decode("utf-8", errors="ignore").rstrip(" .")
            or "Playlist"
        )
        for _ in range(20):
            path = self.download_dir.resolve() / f"{name} - {secrets.token_hex(3)}"
            if (
                not path.exists()
                and not path.is_symlink()
                and not db.execute(
                    "SELECT 1 FROM playlists WHERE download_dir=?", (str(path),)
                ).fetchone()
            ):
                return str(path)
        raise RuntimeError("Could not allocate a unique playlist folder")

    def create_custom_playlist(self, title: str, video_ids: list[str]) -> str:
        title = title.strip()
        if not title or len(title) > 200:
            raise ValueError("Enter a playlist name between 1 and 200 characters.")
        ids = list(dict.fromkeys(video_ids))
        if not ids or any(not VIDEO_ID.fullmatch(value) for value in ids):
            raise ValueError("A custom playlist requires valid YouTube video IDs.")
        playlist_id = f"custom_{uuid4().hex}"
        timestamp = now()
        # The header and full membership become visible together. Mark the local
        # snapshot ready now so downloads never ask Google for this synthetic ID.
        with self.connect() as db:
            directory = self._new_playlist_directory(db, title)
            db.execute(
                """INSERT INTO playlists(id,title,description,channel,privacy,item_count,
                   synced_at,items_synced_at,source,download_dir) VALUES(?,?,?,?,?,?,?,?,?,?)""",
                (
                    playlist_id,
                    title,
                    "A custom playlist saved on this computer.",
                    "Custom playlist",
                    "Local",
                    len(ids),
                    timestamp,
                    timestamp,
                    "custom",
                    directory,
                ),
            )
            db.executemany(
                "INSERT INTO videos(playlist_id,id,title,position,thumbnail) VALUES(?,?,?,?,?)",
                [
                    (
                        playlist_id,
                        value,
                        f"YouTube video · {value}",
                        position,
                        f"https://i.ytimg.com/vi/{value}/hqdefault.jpg",
                    )
                    for position, value in enumerate(ids)
                ],
            )
        return playlist_id

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
        remaining = [v for v in current if not v["local_available"]]
        return {
            **dict(row),
            "youtube_url": f"https://www.youtube.com/playlist?list={quote(row['id'], safe='')}"
            if row["source"] == "youtube"
            else None,
            "download_dir": str(self.playlist_directory(row["id"])),
            "downloaded": sum(v["local_available"] for v in current),
            "local_count": sum(v["local_available"] for v in videos),
            "total": len(current) if row["items_synced_at"] else row["item_count"],
            "skipped": sum(v["download_skipped"] for v in remaining),
            "unavailable": sum(
                v["status"] == "unavailable" and not v["download_skipped"]
                for v in remaining
            ),
            "failed": sum(
                v["status"] == "failed" and not v["download_skipped"] for v in remaining
            ),
            "bytes": sum(v["size"] for v in videos if v["local_available"]),
            "metadata": {
                **self.metadata_job(row["id"]),
                "total": len(current),
                "updated": sum(v["metadata_status"] == "updated" for v in current),
                "failed": sum(v["metadata_status"] == "failed" for v in current),
            },
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
        item["download_skipped"] = bool(item["download_skipped"])
        item["youtube_url"] = (
            f"https://www.youtube.com/watch?v={quote(item['id'], safe='')}"
        )
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

    def download_skipped(self, playlist_id: str, video_id: str) -> bool:
        # The worker needs the live preference between transfers and at launch.
        # Avoid resolving paths and loading recovery metadata for these checks.
        with self.connect() as db:
            row = db.execute(
                "SELECT download_skipped FROM videos WHERE playlist_id=? AND id=?",
                (playlist_id, video_id),
            ).fetchone()
        return bool(row["download_skipped"])

    def update_video(self, playlist_id: str, video_id: str, **fields):
        allowed = {
            "download_skipped",
            "title",
            "channel",
            "thumbnail",
            "status",
            "error",
            "file_path",
            "size",
            "downloaded_at",
            "recovery",
            "recovery_checked_at",
            "metadata_status",
            "metadata_error",
            "metadata_updated_at",
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

    def metadata_job(self, playlist_id: str) -> dict:
        with self.connect() as db:
            row = db.execute(
                "SELECT * FROM metadata_jobs WHERE playlist_id=?", (playlist_id,)
            ).fetchone()
        return (
            dict(row) if row else {"state": "idle", "error": "", "current_video": None}
        )

    def queue_metadata(self, playlist_id: str, resume: bool = False):
        # The metadata ledger is independent of download status. Restarting a
        # lookup clears only its progress/errors, never known titles or media.
        with self.connect() as db:
            db.execute(
                "UPDATE videos SET metadata_status='pending', metadata_error='' "
                "WHERE playlist_id=? AND in_playlist=1 "
                + ("AND metadata_status!='updated'" if resume else ""),
                (playlist_id,),
            )
            db.execute(
                """INSERT INTO metadata_jobs(playlist_id,state,queued_at)
                VALUES(?,'queued',?) ON CONFLICT(playlist_id) DO UPDATE SET
                state='queued',queued_at=excluded.queued_at,current_video=NULL,
                error='',finished_at=NULL""",
                (playlist_id, now()),
            )

    def update_metadata_job(self, playlist_id: str, **fields):
        if not fields or not fields.keys() <= {
            "state",
            "error",
            "current_video",
            "finished_at",
        }:
            raise ValueError("Invalid metadata job update")
        with self.connect() as db:
            db.execute(
                f"UPDATE metadata_jobs SET {','.join(f'{key}=?' for key in fields)} WHERE playlist_id=?",
                (*fields.values(), playlist_id),
            )

    def recover_metadata_jobs(self):
        with self.connect() as db:
            db.execute(
                "UPDATE metadata_jobs SET state='queued',current_video=NULL WHERE state='running'"
            )

    def next_metadata_job(self) -> str | None:
        with self.connect() as db:
            row = db.execute(
                "SELECT playlist_id FROM metadata_jobs WHERE state='queued' ORDER BY queued_at LIMIT 1"
            ).fetchone()
        return row["playlist_id"] if row else None

    def pending_metadata_ids(self, playlist_id: str) -> list[str]:
        with self.connect() as db:
            return [
                row["id"]
                for row in db.execute(
                    "SELECT id FROM videos WHERE playlist_id=? AND in_playlist=1 "
                    "AND metadata_status='pending' ORDER BY position,id",
                    (playlist_id,),
                )
            ]

    def metadata_has_failures(self, playlist_id: str) -> bool:
        with self.connect() as db:
            return (
                db.execute(
                    "SELECT 1 FROM videos WHERE playlist_id=? AND in_playlist=1 "
                    "AND metadata_status='failed' LIMIT 1",
                    (playlist_id,),
                ).fetchone()
                is not None
            )

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
