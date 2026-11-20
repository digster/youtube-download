"""SQLite owns job intent; yt-dlp owns partial files. Every update is transactional."""

import json
import os
import re
import secrets
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote
from uuid import uuid4

from .config import VIDEO_ID, contained_file
from .filenames import safe_stem

# Archive responses belong to a YouTube ID, while download selections belong
# to a playlist membership. Share results across all copies of the same video.
VIDEO_QUERY = """SELECT v.*, CASE WHEN :compact AND v.id != :review
                        THEN COALESCE(a.summary, a.result) ELSE a.result END AS archive_result,
                        a.checked_at AS archive_checked,
                        a.requested_at IS NOT NULL AS archive_pending
                 FROM videos v LEFT JOIN archive_checks a ON a.video_id=v.id"""
SAVE_FINDER_DEADLINE = """INSERT INTO finder_throttle(id,next_request) VALUES(1,?)
    ON CONFLICT(id) DO UPDATE SET next_request=MAX(next_request,excluded.next_request)"""


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


class Store:
    def __init__(self, path: Path, download_dir: Path):
        self.path = path
        self.download_dir = download_dir.resolve()
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
                    source TEXT NOT NULL DEFAULT 'youtube',
                    folder_is_custom INTEGER NOT NULL DEFAULT 0
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
                CREATE INDEX IF NOT EXISTS video_membership ON videos(id, in_playlist);
                CREATE TABLE IF NOT EXISTS archive_checks (
                    video_id TEXT PRIMARY KEY, requested_at TEXT, checked_at TEXT, result TEXT,
                    summary TEXT
                );
                CREATE INDEX IF NOT EXISTS pending_archives ON archive_checks(requested_at)
                    WHERE requested_at IS NOT NULL;
                CREATE TABLE IF NOT EXISTS finder_throttle (
                    id INTEGER PRIMARY KEY CHECK(id=1), next_request REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS download_directory (
                    id INTEGER PRIMARY KEY CHECK(id=1), directory TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS metadata_jobs (
                    playlist_id TEXT PRIMARY KEY REFERENCES playlists(id),
                    state TEXT NOT NULL, queued_at TEXT, current_video TEXT,
                    error TEXT DEFAULT '', finished_at TEXT
                );
            """)
            # Commit each schema change together with the data it classifies.
            db.execute("BEGIN IMMEDIATE")
            columns = {row[1] for row in db.execute("PRAGMA table_info(playlists)")}
            if "download_dir" not in columns:
                db.execute(
                    "ALTER TABLE playlists ADD COLUMN download_dir TEXT DEFAULT ''"
                )
            if "source" not in columns:
                db.execute(
                    "ALTER TABLE playlists ADD COLUMN source TEXT NOT NULL DEFAULT 'youtube'"
                )
            if "folder_is_custom" not in columns:
                db.execute(
                    "ALTER TABLE playlists ADD COLUMN folder_is_custom INTEGER NOT NULL DEFAULT 0"
                )
                self._classify_legacy_folders(db)
            video_columns = {row[1] for row in db.execute("PRAGMA table_info(videos)")}
            for name, definition in {
                "download_skipped": "INTEGER NOT NULL DEFAULT 0",
                "metadata_status": "TEXT NOT NULL DEFAULT 'pending'",
                "metadata_error": "TEXT DEFAULT ''",
                "metadata_updated_at": "TEXT",
            }.items():
                if name not in video_columns:
                    db.execute(f"ALTER TABLE videos ADD COLUMN {name} {definition}")
            archive_columns = {
                row[1] for row in db.execute("PRAGMA table_info(archive_checks)")
            }
            if "summary" not in archive_columns:
                db.execute("ALTER TABLE archive_checks ADD COLUMN summary TEXT")
            # Older libraries stored Finder results per membership. Prefer the
            # most recent copy when seeding the shared cache on upgrade.
            db.execute("""INSERT OR IGNORE INTO archive_checks(video_id, checked_at, result)
                          SELECT id, recovery_checked_at, recovery FROM videos
                          WHERE recovery IS NOT NULL ORDER BY recovery_checked_at DESC""")

    @staticmethod
    def _directory_override(db) -> Path | None:
        row = db.execute(
            "SELECT directory FROM download_directory WHERE id=1"
        ).fetchone()
        return Path(row["directory"]) if row else None

    def directory_override(self) -> Path | None:
        with self.connect() as db:
            return self._directory_override(db)

    def _classify_legacy_folders(self, db):
        # Previous versions stored both manual choices and allocations as absolute
        # paths. Only infer an allocation for a missing ID/generated folder under
        # a known default. Existing directories could have been chosen by hand.
        roots = {self.download_dir, self._directory_override(db)}
        rows = db.execute(
            "SELECT id,download_dir FROM playlists WHERE download_dir != ''"
        ).fetchall()
        for row in rows:
            path = Path(row["download_dir"])
            if path.parent in roots and (
                path.name == row["id"] or re.fullmatch(r".+ - [0-9a-f]{6}", path.name)
            ):
                try:
                    path.lstat()
                except FileNotFoundError:
                    continue
                except OSError:
                    pass  # Preserve paths we cannot inspect.
            db.execute(
                "UPDATE playlists SET folder_is_custom=1 WHERE id=?", (row["id"],)
            )

    def set_directory_override(
        self, directory: Path | None, active_playlist: str | None = None
    ) -> int:
        with self.connect() as db:
            if directory is None:
                db.execute("DELETE FROM download_directory WHERE id=1")
            else:
                db.execute(
                    """INSERT INTO download_directory(id,directory) VALUES(1,?)
                       ON CONFLICT(id) DO UPDATE SET directory=excluded.directory""",
                    (str(directory),),
                )
            return self._apply_default_folder(
                db, directory or self.download_dir, active_playlist
            )

    def apply_default_folder(self) -> int:
        with self.connect() as db:
            return self._apply_default_folder(
                db, self._directory_override(db) or self.download_dir
            )

    def _apply_default_folder(
        self, db, root: Path, active_playlist: str | None = None
    ) -> int:
        # Materialize the original ID path even for protected legacy rows. Later
        # startup settings must not change where a partial or completed file lives.
        db.execute(
            """UPDATE playlists SET download_dir=rtrim(?, '/') || '/' || id
               WHERE download_dir='' OR download_dir IS NULL""",
            (str(self.download_dir),),
        )
        try:
            if not root.is_dir():
                return 0
        except OSError:
            return 0
        rows = db.execute(
            "SELECT id,download_dir,folder_is_custom,state FROM playlists"
        ).fetchall()
        reserved = {Path(row["download_dir"]) for row in rows}
        updated = 0
        for row in rows:
            if (
                row["folder_is_custom"]
                or row["id"] == active_playlist
                or row["state"] in {"queued", "running"}
            ):
                continue
            old = Path(row["download_dir"])
            target = root / old.name
            if target in reserved or not self._can_rebind_folder(old, target):
                continue
            db.execute(
                "UPDATE playlists SET download_dir=? WHERE id=?",
                (str(target), row["id"]),
            )
            reserved.add(target)
            updated += 1
        return updated

    @staticmethod
    def _can_rebind_folder(old: Path, target: Path) -> bool:
        # Database counters miss .part files, receipts and user files. Inspect
        # the directory itself; a disconnected source or occupied target cannot
        # be safely reassigned. lstat also detects dangling destination symlinks.
        try:
            if not old.parent.is_dir() or old.is_symlink():
                return False
            try:
                with os.scandir(old) as entries:
                    if next(entries, None) is not None:
                        return False
            except FileNotFoundError:
                pass
            try:
                target.lstat()
            except FileNotFoundError:
                return True
        except OSError:
            pass
        return False

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
        name = safe_stem(title, max_chars=100, max_bytes=180) or "Playlist"
        root = self._directory_override(db) or self.download_dir
        for _ in range(20):
            path = root / f"{name} - {secrets.token_hex(3)}"
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
            self._queue_archives(db, playlist_id)
        return playlist_id

    def sync_videos(self, playlist_id: str, videos: list[dict]):
        # Fetch the entire remote snapshot before this transaction: a failed API page
        # must never mark the remaining local videos as removed.
        with self.connect() as db:
            previous = db.execute(
                "SELECT items_synced_at FROM playlists WHERE id=?", (playlist_id,)
            ).fetchone()
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
            self._queue_archives(
                db, playlist_id, refresh=bool(previous["items_synced_at"])
            )
            # Drop obsolete work when a refresh removes the last current
            # membership. Keep cached links available on the retained row.
            db.execute(
                """UPDATE archive_checks SET requested_at=NULL WHERE requested_at IS NOT NULL
                AND video_id IN (SELECT id FROM videos WHERE playlist_id=? AND in_playlist=0)
                AND NOT EXISTS (SELECT 1 FROM videos WHERE id=video_id AND in_playlist=1)""",
                (playlist_id,),
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
        videos = (
            self.videos(row["id"], compact_archives=True) if videos is None else videos
        )
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
            "archive_pending": sum(v["archive_pending"] for v in current),
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

    def videos(
        self, playlist_id: str, *, compact_archives=False, review_video=""
    ) -> list[dict]:
        with self.connect() as db:
            rows = db.execute(
                VIDEO_QUERY
                + " WHERE v.playlist_id=:playlist ORDER BY v.in_playlist DESC, v.position, v.id",
                {
                    "compact": compact_archives,
                    "review": review_video,
                    "playlist": playlist_id,
                },
            ).fetchall()
        directory = self.playlist_directory(playlist_id)
        return [self._video_view(row, directory) for row in rows]

    def saved_media(self, playlist_id: str | None = None):
        # Backfill includes retained media outside current membership. Release
        # the read transaction before checking potentially slow external drives.
        query = """SELECT v.playlist_id,v.id,v.title,v.file_path,p.download_dir
                   FROM videos v JOIN playlists p ON v.playlist_id=p.id
                   WHERE v.file_path IS NOT NULL"""
        with self.connect() as db:
            if playlist_id is None:
                return db.execute(query).fetchall()
            return db.execute(query + " AND v.playlist_id=?", (playlist_id,)).fetchall()

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
        recovery = item.pop("archive_result") or item["recovery"]
        item["recovery"] = json.loads(recovery) if recovery else None
        item["recovery_checked_at"] = (
            item.pop("archive_checked") or item["recovery_checked_at"]
        )
        item["archive_pending"] = bool(item["archive_pending"])
        return item

    def video(self, playlist_id: str, video_id: str) -> dict | None:
        with self.connect() as db:
            row = db.execute(
                VIDEO_QUERY + " WHERE v.playlist_id=:playlist AND v.id=:review",
                {"compact": False, "review": video_id, "playlist": playlist_id},
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

    def set_download_skipped(
        self, playlist_id: str, video_id: str | None, skipped: bool
    ):
        # One transaction for the entire selection, including off-page items.
        # Retained rows do not participate in the download queue.
        query = (
            "UPDATE videos SET download_skipped=? WHERE playlist_id=? AND in_playlist=1"
        )
        with self.connect() as db:
            if video_id is None:
                db.execute(query, (skipped, playlist_id))
            elif not db.execute(
                query + " AND id=?", (skipped, playlist_id, video_id)
            ).rowcount:
                raise ValueError("This video is no longer in the playlist.")

    def update_video(self, playlist_id: str, video_id: str, **fields):
        allowed = {
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
            "folder_is_custom",
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

    @staticmethod
    def _queue_archives(db, playlist_id, *, refresh=False, missing_only=False):
        # Membership and lookup intent are committed together. Multiple lists
        # requesting the same ID share one outstanding request, even on refresh.
        query = """INSERT INTO archive_checks(video_id,requested_at)
                   SELECT id,:requested FROM videos WHERE playlist_id=:playlist AND in_playlist=1
                   ORDER BY position,id ON CONFLICT(video_id) """
        conflict = (
            "DO NOTHING"
            if missing_only
            else """
            DO UPDATE SET requested_at=excluded.requested_at WHERE requested_at IS NULL AND
            (:refresh OR checked_at IS NULL OR checked_at < :cutoff OR json_extract(result,'$.error') IS NOT NULL)"""
        )
        db.execute(
            query + conflict,
            {
                "playlist": playlist_id,
                "requested": now(),
                "refresh": refresh,
                "cutoff": (datetime.now(timezone.utc) - timedelta(days=7)).isoformat(),
            },
        )

    def queue_missing_archives(self, playlist_id):
        # Also covers a download resumed from a pre-archive-queue database.
        with self.connect() as db:
            self._queue_archives(db, playlist_id, missing_only=True)

    def next_archive_id(self):
        with self.connect() as db:
            row = db.execute("""SELECT video_id FROM archive_checks WHERE requested_at IS NOT NULL
                ORDER BY requested_at, rowid LIMIT 1""").fetchone()
        return row["video_id"] if row else None

    def queue_archive_check(self, video_id):
        with self.connect() as db:
            db.execute(
                """INSERT INTO archive_checks(video_id, requested_at) VALUES(?,?)
                ON CONFLICT(video_id) DO UPDATE SET requested_at=excluded.requested_at
                WHERE requested_at IS NULL""",
                (video_id, now()),
            )

    def pending_archives(self, playlist_id, video_id=None):
        query = """SELECT 1 FROM videos v JOIN archive_checks a ON a.video_id=v.id
                   WHERE v.playlist_id=? AND v.in_playlist=1 AND a.requested_at IS NOT NULL"""
        with self.connect() as db:
            if video_id is None:
                return (
                    db.execute(query + " LIMIT 1", (playlist_id,)).fetchone()
                    is not None
                )
            return (
                db.execute(query + " AND v.id=?", (playlist_id, video_id)).fetchone()
                is not None
            )

    def archive_check(self, video_id):
        with self.connect() as db:
            return db.execute(
                "SELECT * FROM archive_checks WHERE video_id=?", (video_id,)
            ).fetchone()

    def finder_next_request(self):
        with self.connect() as db:
            row = db.execute(
                "SELECT next_request FROM finder_throttle WHERE id=1"
            ).fetchone()
        return row[0] if row else 0

    def reserve_finder_request(self, deadline):
        # A hard shutdown during HTTP still leaves a minimum gap before a
        # restarted worker may issue another request.
        with self.connect() as db:
            db.execute(SAVE_FINDER_DEADLINE, (deadline,))

    def finish_archive_check(self, video_id, result, next_request):
        # Persist the cooldown with the response, including after a 429. A
        # server restart can resume the queue without resetting Retry-After.
        # Large lists poll for progress. Send the link count with each row and
        # the full payload only for the video whose results are being reviewed.
        summary = {
            "link_count": sum(
                link["kind"] != "original" and link["service"].casefold() != "youtube"
                for link in result["links"]
            ),
            "partial": bool(result["service_errors"]),
            "error": result["error"],
        }
        with self.connect() as db:
            db.execute(
                """INSERT INTO archive_checks(video_id,result,summary,checked_at) VALUES(?,?,?,?)
                ON CONFLICT(video_id) DO UPDATE SET result=excluded.result,
                summary=excluded.summary,checked_at=excluded.checked_at,requested_at=NULL""",
                (video_id, json.dumps(result), json.dumps(summary), now()),
            )
            db.execute(SAVE_FINDER_DEADLINE, (next_request,))

    def queue_metadata(self, playlist_id: str, resume: bool = False):
        # The metadata ledger is independent of download status. Restarting a
        # lookup clears only its progress/errors, never known titles or media.
        with self.connect() as db:
            self._queue_archives(db, playlist_id, refresh=not resume)
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
