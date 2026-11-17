"""Loopback-only HTTP API and static HTML application."""

from contextlib import asynccontextmanager
import fcntl
import logging
from pathlib import Path
import shutil
from threading import Lock
from urllib.parse import urlparse

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, ValidationError
from starlette.middleware.trustedhost import TrustedHostMiddleware
from starlette.concurrency import run_in_threadpool
from google.auth.exceptions import RefreshError
from youtube_backup.auth import ScopeUpgradeRequired

from .account import Account
from .auth import GoogleLogin, LoginError, MAX_CONFIG_BYTES
from .config import PLAYLIST_ID, VIDEO_ID, Settings, contained_file
from .cookies import CookieError, CookieSettings, MAX_COOKIE_BYTES
from .custom_playlists import parse_video_urls
from .download_settings import DownloadSettings
from .downloads import DownloadManager
from .finder import Finder
from .store import Store

logger = logging.getLogger(__name__)
STATIC = Path(__file__).parent / "static"


class FolderUpdate(BaseModel):
    directory: str = Field(min_length=1, max_length=4096)


class CookieUpdate(BaseModel):
    mode: str = Field(max_length=20)
    browser: str = Field(default="", max_length=30)
    profile: str = Field(default="", max_length=4096)
    container: str = Field(default="", max_length=200)


class CustomPlaylistCreate(BaseModel):
    title: str = Field(max_length=200)
    urls: str = Field(max_length=2_000_000)


def create_app(settings=None, account=None, start_worker=True) -> FastAPI:
    settings = settings or Settings.from_env()
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    settings.download_dir.mkdir(parents=True, exist_ok=True)
    store = Store(settings.data_dir / "library.sqlite3", settings.download_dir)
    account = account or Account(settings.data_dir)
    login = GoogleLogin(settings, account.save_credentials)
    finder = Finder(store, settings.finder_url)
    cookies = CookieSettings(settings)
    download_settings = DownloadSettings(settings)
    manager = DownloadManager(store, settings, finder, cookies, download_settings)
    operations = Lock()

    @asynccontextmanager
    async def lifespan(app):
        # The SQLite queue has one owner. Reject a second server/uvicorn worker
        # instead of letting two processes download the same playlist concurrently.
        with (settings.data_dir / ".server.lock").open("a") as lock_file:
            try:
                fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise RuntimeError(
                    "Another downloader is already using this data directory."
                ) from exc
            cookies.cleanup_sessions()
            if start_worker:
                manager.start()
            try:
                yield
            finally:
                login.cancel()
                manager.close()
                fcntl.flock(lock_file, fcntl.LOCK_UN)

    app = FastAPI(
        title="YouTube Download", lifespan=lifespan, docs_url=None, redoc_url=None
    )
    app.state.store, app.state.manager, app.state.finder = store, manager, finder
    app.state.login = login
    app.state.cookies = cookies
    app.state.download_settings = download_settings
    app.add_middleware(
        TrustedHostMiddleware, allowed_hosts=["127.0.0.1", "localhost", "[::1]"]
    )

    @app.middleware("http")
    async def local_browser_only(request: Request, call_next):
        if request.url.path == "/auth/callback":
            # Uvicorn logs the scope's query string after sending the response.
            # Keep authorization codes/state out of access logs and other middleware.
            request.state.oauth_callback = dict(request.query_params)
            request.scope["query_string"] = b""
        # Cross-origin pages cannot add this header without a CORS preflight. Host
        # validation additionally prevents DNS rebinding against the local server.
        if request.method not in {"GET", "HEAD", "OPTIONS"}:
            origin = request.headers.get("origin")
            if request.headers.get("x-local-app") != "1" or (
                origin and urlparse(origin).netloc != request.headers.get("host")
            ):
                return JSONResponse(
                    {"detail": "Use the local application to perform this action."},
                    status_code=403,
                )
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self'; "
            "img-src 'self' https:; media-src 'self'; connect-src 'self'; "
            "object-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'"
        )
        if request.url.path.startswith(("/api/", "/auth/")):
            response.headers["Cache-Control"] = "no-store"
        return response

    def require_playlist(playlist_id):
        if not PLAYLIST_ID.fullmatch(playlist_id):
            raise HTTPException(400, "Invalid playlist ID")
        playlist = store.playlist(playlist_id)
        if not playlist:
            raise HTTPException(404, "Playlist not found. Sync your account first.")
        return playlist

    def require_video(playlist_id, video_id):
        require_playlist(playlist_id)
        if not VIDEO_ID.fullmatch(video_id):
            raise HTTPException(400, "Invalid video ID")
        video = store.video(playlist_id, video_id)
        if not video:
            raise HTTPException(404, "Video not found")
        return video

    def account_call(function, *args):
        try:
            return function(*args)
        except FileNotFoundError as exc:
            raise HTTPException(
                401,
                "Connect your YouTube account first. Open Account & setup and choose Connect with Google.",
            ) from exc
        except (RefreshError, ScopeUpgradeRequired) as exc:
            logger.warning("Account login needs renewal (%s)", type(exc).__name__)
            raise HTTPException(
                401,
                "Reconnect your YouTube account: the saved login can no longer be refreshed. Open Account & setup and choose Reconnect with Google.",
            ) from exc
        except Exception as exc:
            # Exceptions from OAuth transports may include credentials or request
            # URLs. Return actionable guidance without reflecting their raw text.
            logger.warning("Account sync failed (%s)", type(exc).__name__)
            raise HTTPException(
                502,
                "YouTube sync failed. Check your connection, API quota, and youtube-backup login. Cached playlists are still available.",
            ) from exc

    @app.get("/api/status")
    def status():
        cookie_status = cookies.status()
        return {
            "account": account.status(),
            "login": login.status(),
            "download_dir": str(settings.download_dir),
            "ffmpeg": bool(shutil.which("ffmpeg")),
            "javascript": bool(shutil.which("node") or shutil.which("deno")),
            "cookies": cookie_status["active"],
            "cookie_settings": cookie_status,
            "free_bytes": shutil.disk_usage(settings.download_dir).free,
        }

    @app.get("/api/download-settings")
    def get_download_settings():
        return download_settings.status()

    def download_settings_call(function, *args):
        try:
            return function(*args)
        except ValidationError as exc:
            error = exc.errors()[0]
            field = ".".join(str(part) for part in error["loc"])
            message = error["msg"].removeprefix("Value error, ")
            raise HTTPException(
                400, f"{field}: {message}" if field else message
            ) from exc
        except OSError as exc:
            logger.warning("Could not save download settings (%s)", type(exc).__name__)
            raise HTTPException(
                500,
                "Could not save download settings. Check the data folder's permissions.",
            ) from exc

    @app.post("/api/download-settings")
    def configure_download_settings(update: dict):
        return download_settings_call(download_settings.configure, update)

    @app.post("/api/download-settings/reset")
    def reset_download_settings():
        return download_settings_call(download_settings.reset)

    @app.get("/api/cookies")
    def cookie_status():
        return cookies.status()

    def cookie_call(function, *args):
        try:
            return function(*args)
        except CookieError as exc:
            raise HTTPException(400, str(exc)) from exc
        except OSError as exc:
            logger.warning("Could not save cookie settings (%s)", type(exc).__name__)
            raise HTTPException(
                500, "Could not save cookies. Check the data folder's permissions."
            ) from exc

    @app.post("/api/cookies")
    def configure_cookies(update: CookieUpdate):
        return cookie_call(
            cookies.configure,
            update.mode,
            update.browser,
            update.profile,
            update.container,
        )

    @app.post("/api/cookies/file")
    async def upload_cookies(request: Request):
        content = bytearray()
        async for chunk in request.stream():
            if len(content) + len(chunk) > MAX_COOKIE_BYTES:
                raise HTTPException(413, "Choose a cookies.txt file smaller than 5 MB.")
            content.extend(chunk)
        # Validation and durable disk writes must not stall polling while a large
        # export is uploaded; the cookie service serializes its own state changes.
        return await run_in_threadpool(
            cookie_call,
            cookies.upload,
            bytes(content),
            request.query_params.get("filename", "cookies.txt"),
        )

    @app.get("/api/auth/status")
    def login_status():
        return login.status()

    @app.post("/api/auth/start")
    def start_login(request: Request):
        try:
            # Use a literal loopback IP even when the app was opened as localhost.
            # Desktop OAuth clients support the local server's chosen port.
            return login.start(
                f"http://127.0.0.1:{request.url.port or 80}/auth/callback"
            )
        except LoginError as exc:
            raise HTTPException(400, str(exc)) from exc

    @app.post("/api/auth/cancel")
    def cancel_login():
        return login.cancel()

    @app.post("/api/auth/config")
    async def configure_login(request: Request):
        content = bytearray()
        async for chunk in request.stream():
            content.extend(chunk)
            if len(content) > MAX_CONFIG_BYTES:
                raise HTTPException(
                    413, "Choose an OAuth client JSON smaller than 64 KB."
                )
        try:
            return login.configure(bytes(content))
        except LoginError as exc:
            raise HTTPException(400, str(exc)) from exc
        except OSError as exc:
            raise HTTPException(
                500,
                "Could not save the Google client file. Check the data folder's permissions.",
            ) from exc

    @app.get("/auth/callback")
    def google_callback(request: Request):
        query = request.state.oauth_callback
        result = login.complete(
            query.get("state", ""), query.get("code", ""), query.get("error", "")
        )
        # Drop the authorization code from the address bar before loading any app
        # assets. The originating tab also observes completion through status polling.
        return RedirectResponse(f"/?auth={result}", status_code=303)

    @app.get("/api/playlists")
    def playlists():
        return {"playlists": store.playlists()}

    @app.post("/api/playlists/custom", status_code=201)
    def create_custom_playlist(request: CustomPlaylistCreate):
        try:
            ids = parse_video_urls(request.urls)
            with operations:
                playlist_id = store.create_custom_playlist(request.title, ids)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        logger.info("Created custom playlist %s with %d videos", playlist_id, len(ids))
        return playlist_detail(playlist_id)

    @app.post("/api/sync")
    def sync():
        with operations:
            store.sync_playlists(account_call(account.playlists))
        return {"playlists": store.playlists()}

    @app.get("/api/playlists/{playlist_id}")
    def playlist_detail(playlist_id: str):
        row = require_playlist(playlist_id)
        videos = store.videos(playlist_id)
        return {"playlist": store.playlist_summary(row, videos), "videos": videos}

    @app.post("/api/playlists/{playlist_id}/refresh")
    def refresh(playlist_id: str):
        with operations:
            playlist = require_playlist(playlist_id)
            if playlist["source"] == "custom":
                raise HTTPException(
                    400,
                    "Custom playlists are saved locally and cannot be refreshed from YouTube.",
                )
            if playlist["state"] in {"running", "queued"}:
                raise HTTPException(
                    409, "Pause this playlist before refreshing its videos."
                )
            videos = account_call(account.videos, playlist_id)
            store.sync_videos(playlist_id, videos)
        return playlist_detail(playlist_id)

    @app.post("/api/playlists/{playlist_id}/download")
    def download(playlist_id: str):
        with operations:
            playlist = require_playlist(playlist_id)
            if not shutil.which("ffmpeg"):
                raise HTTPException(
                    503, "Install ffmpeg before starting downloads. See Setup."
                )
            if not playlist["items_synced_at"] and playlist["source"] != "custom":
                store.sync_videos(
                    playlist_id, account_call(account.videos, playlist_id)
                )
            try:
                manager.enqueue(playlist_id)
            except ValueError as exc:
                raise HTTPException(409, str(exc)) from exc
        return {"playlist": store.playlist(playlist_id)}

    @app.post("/api/playlists/{playlist_id}/pause")
    def pause(playlist_id: str):
        require_playlist(playlist_id)
        manager.pause(playlist_id)
        return {"playlist": store.playlist(playlist_id)}

    @app.post("/api/playlists/{playlist_id}/folder")
    def change_folder(playlist_id: str, update: FolderUpdate):
        with operations, manager.lock:
            playlist = require_playlist(playlist_id)
            if (
                playlist["state"] in {"running", "queued"}
                or manager.active_playlist == playlist_id
            ):
                raise HTTPException(
                    409,
                    "Pause this playlist and wait for its download to stop before changing folders.",
                )
            directory = Path(update.directory).expanduser()
            if not directory.is_absolute():
                raise HTTPException(
                    400, "Enter an absolute folder path, or a path beginning with ~/."
                )
            directory = directory.resolve()
            if not directory.is_dir():
                raise HTTPException(
                    400,
                    "That folder does not exist. Create it or move your playlist folder there first.",
                )
            for other in store.playlists():
                if (
                    other["id"] != playlist_id
                    and Path(other["download_dir"]).resolve() == directory
                ):
                    raise HTTPException(
                        409, "That folder is already assigned to another playlist."
                    )
            store.update_playlist(playlist_id, download_dir=str(directory))
        return playlist_detail(playlist_id)

    @app.post("/api/playlists/{playlist_id}/videos/{video_id}/find")
    def find(playlist_id: str, video_id: str):
        require_video(playlist_id, video_id)
        return finder.lookup(playlist_id, video_id)

    @app.get("/api/playlists/{playlist_id}/videos/{video_id}/file")
    def media(playlist_id: str, video_id: str):
        video = require_video(playlist_id, video_id)
        path = contained_file(store.playlist_directory(playlist_id), video["file_path"])
        if not path:
            raise HTTPException(
                404, "Local file is missing. Resume this playlist to download it again."
            )
        return FileResponse(path, filename=path.name, content_disposition_type="inline")

    app.mount("/static", StaticFiles(directory=STATIC), name="static")

    @app.get("/")
    @app.get("/playlists/{playlist_id}")
    def index():
        return FileResponse(STATIC / "index.html")

    return app
