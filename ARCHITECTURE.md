# Architecture

## Big picture

Shelf is a local FastAPI server with a buildless HTML/CSS/JavaScript client.
SQLite persists metadata and job intent, while yt-dlp owns transfer and merge
details. There is one background download worker per data directory. Runtime
files are deliberately separate from versioned source.

```text
Browser: static/index.html + app.js + style.css
  ├─ GET /api/playlists, /api/playlists/:id (poll every 2 seconds)
  ├─ POST sync, refresh, download, pause, folder, find
  ├─ POST /api/auth/start, /config, /cancel; GET /api/auth/status
  ├─ GET/POST /api/cookies; POST /api/cookies/file
  ├─ Google system-browser consent → GET /auth/callback → Shelf
  └─ GET video/file (FileResponse supports HTTP ranges)
                  │
               app.py
      ┌───────────┼───────────────┐
  account.py   store.py      downloads.py
      │        SQLite           │
 youtube-backup             runner.py subprocess
 OAuth + YouTube API            │
                            yt-dlp → ffmpeg → local files
                                │
                             finder.py → youtubevideofinder v5 HTTP API
```

## Boundaries and decisions

- `account.py` reuses the sibling `youtube-backup` package's credentials,
  pagination, retry policy, and quota tracking. `youtube-client` only supports
  API-key access and is unsuitable for enumerating private account playlists.
  The adapter normalizes Google responses into simple dictionaries. Its lock
  serializes account requests and keeps this app's quota ledger coherent. The
  sibling API's service is closed after each operation.
- `auth.py` manages the UI's Google authorization-code flow with the installed
  app library, PKCE, read-only scopes, and a five-minute state/verifier lifetime.
  It opens the system browser and returns a fallback authorization link. Token
  exchange has a 30-second network timeout; completed tokens are saved through
  `Account.save_credentials`, which shares the API-refresh lock and atomically
  replaces the existing youtube-backup token with owner-only permissions.
- `store.py` owns all SQLite transactions, sync merging, and local file
  verification. Connections are short-lived and never shared between request
  threads and the worker. WAL permits reads during writes. A completed remote
  snapshot is fetched before the sync transaction begins.
- `downloads.py` owns scheduling and process control. It launches one video at a
  time with explicit options, no shell, no external yt-dlp configuration, and no
  user plugins. Files are named by video ID, so title changes and reordering do
  not change the resume destination. Each playlist has its own physical copies.
- `cookies.py` owns live cookie preferences, upload validation, and per-video
  snapshots. `app.py` and `DownloadManager` share one instance. The manager takes
  a fresh snapshot before each subprocess, so settings changes apply without
  mutating the frozen startup Settings or interrupting an in-flight video.
- `runner.py` holds an exclusive per-video filesystem lock and watches its
  parent's PID. If the app is hard-killed, the runner terminates its process
  group, including ffmpeg. The file lock prevents a restarted app from writing
  to an old process's still-open download.
- `finder.py` integrates the upstream API instead of vendoring an unstable,
  unpackaged Python module. Lookups are serialized and cached for seven days.
  Link URLs are restricted to HTTP(S). Failed refreshes preserve earlier links.
- `app.py` serves pages, enforces IDs/playlist ownership in the local database,
  and blocks refresh/folder changes while jobs are queued or running. An
  application operation lock serializes queue preparation and snapshot/folder
  changes. The manager's lock protects state changes against the worker.
- `cli.py` loads dotenv, configures rotating logs, serves on loopback, and
  forwards login to the sibling package's read-only OAuth flow.

## Durable state and resume contract

`playlists` stores remote metadata, snapshot timestamps, job state, current
progress, queue time, and an optional absolute download-folder override.
`videos` is keyed by `(playlist_id, id)` and stores metadata, remote membership,
availability, transfer state, a relative media path, size, and cached recovery
links. Duplicate occurrences of one video in a playlist share a local file;
the first position is retained.

Normal playlist transitions are `idle → queued → running → completed` or
`completed_with_errors`. A worker exception sets `error`. `pause` persists
`paused` and stops the active process group. Enqueueing an active job is a no-op;
enqueueing a paused/completed/error job retries unfinished work. On startup,
`running` becomes `queued` and `downloading` videos become `pending`. On orderly
shutdown, active work becomes queued. A user pause is never overwritten by
worker completion or shutdown.

Transfers keep yt-dlp's `.part` and fragment state. `--abort-on-unavailable-fragments`
prevents incomplete fragmented streams from being accepted as complete.
`--print-to-file after_move:%(filepath)j` appends a receipt only after yt-dlp's
postprocessing/move stage. The worker verifies the referenced file is nonempty
and contained in the video directory before recording completion. If the app
dies between the receipt and SQLite update, the next run reconciles the receipt
without transferring again. An arbitrary `.mp4` or `.info.json` alone is not a
completion signal: it may be an unmerged stream. The GUI checks files on disk,
so a missing completed file stops counting as downloaded and can be retried.

Failures such as HTTP 429, network errors, or bot challenges remain retryable
`failed` states. Explicit private/deleted/unavailable errors trigger archive
lookup. Known deleted/private entries in an API snapshot are also looked up
when the playlist runs. Remaining videos continue. Retrying does not
automatically refresh remote playlist contents; that is a separate action.

## Folder changes and retained data

The default playlist directory is `<YTDL_DOWNLOAD_DIR>/<playlist_id>`.
Every video's recorded path is relative to that playlist directory, such as
`<video_id>/<video_id>.mp4`. The folder API changes only the playlist's absolute
root and immediately rechecks local availability. It never moves files.

Receipts contain yt-dlp's original absolute path. On recovery, only the basename
is used inside the current video directory, allowing receipts to survive a
folder move. User-provided paths must identify existing absolute directories;
`~/` is expanded. A paused process must have finished stopping before rebinding.
The same directory cannot be assigned to another playlist. All served files
are resolved against the current root, including symlink containment checks.

Sync marks removed membership instead of deleting rows or files. Last-known
titles and channel/thumbnail details survive unavailable API placeholders.
Removed local videos are still listed and playable. Playlists absent from later
account syncs are retained too. Changing accounts therefore does not erase a
previous local library; account separation is not implemented.

## Local security and frontend

### Download cookies

`GET /api/cookies` returns source metadata only. `POST /api/cookies` selects a
browser/profile/container, explicitly disables cookies, or restores startup
defaults. `POST /api/cookies/file` accepts a raw Netscape cookie file with a
streamed 5 MB cap. Its filename query is a display label, never a disk path.
These mutations use the existing local-app header and origin protections.

UI preferences override cookie environment variables. The active selection and
any uploaded contents are one atomically replaced, mode-0600 `data/cookies.json`;
failed validation or writes preserve the prior source. Switching to another
source discards the saved upload. Browser extraction is deferred to yt-dlp at
download time, using an allowlist of supported browsers and separate optional
profile/container fields. Settings APIs do not open browser databases.

yt-dlp reads **and writes** `--cookies`, so a context manager creates a private
working file under `data/cookie-sessions/` for each video, including when using
an environment-provided export. Changing the selection cannot alter that file;
yt-dlp cannot overwrite a newer upload or the original export. Cleanup includes
launch failures and interrupted jobs. Startup removes stale sessions only after
acquiring the exclusive server lock. Cookie records are validated without using
yt-dlp's permissive loader, whose malformed-record warnings include raw values.

The setup form keeps cookie drafts independent of OAuth status polling. It shows
the saved source separately from the selection being edited. Changing browsers
clears profile/container drafts to avoid applying one browser's path to another.

### Google reconnect

`POST /api/auth/start` creates one in-memory OAuth attempt and returns its status.
Repeated clicks reuse that attempt. A configured client file comes from the
local UI upload first, then `YTDL_CLIENT_SECRETS`, or automatic discovery in the
working directory/sibling youtube-backup checkout. The upload endpoint enforces
a 64 KB limit, Google Desktop app structure, and Google's known OAuth endpoints.
The config and tokens are never returned to the browser.

Google redirects to `http://127.0.0.1:<server port>/auth/callback`. This GET route
is the intentional exception to mutation-header checks: it verifies the
one-time OAuth state before exchanging the code with PKCE. A callback with a bad,
replayed, cancelled, or expired state cannot change credentials. A generation
counter prevents a callback finishing after cancellation from saving a token.
The flow is ephemeral; restarting the server requires a new sign-in attempt.

The callback query is removed from the ASGI scope before access logging, and a
303 redirect clears the authorization code from the browser address. OAuth
library debug logging is suppressed to avoid logging request bodies/tokens.
The original page polls the login status and offers **Sync playlists** when
connected. A denied/failed login keeps the old token; the existing CLI workflow
is retained. The implementation follows Google's
[Desktop app OAuth flow](https://developers.google.com/identity/protocols/oauth2/native-app).

### Other local routes

This app is a local tool, not a public multiuser server. It binds to 127.0.0.1,
validates the Host header, requires a custom header plus matching Origin for
mutations, and enables no CORS access. A CSP blocks inline scripts, framing,
plugins, and external connections other than HTTPS thumbnails. Dynamic content
is escaped before HTML insertion. Media routes take IDs, not user-supplied
filesystem paths. OAuth/token contents are never sent to the client.

The frontend uses ordinary links for playlist pages, native dialogs, responsive
cards, accessible controls, local video playback, and a selection toolbar.
Two-second polling stops when the document is hidden and avoids overlapping
requests. Polls preserve focused card controls; dialogs/search remain outside
the rerendered list. Download work continues independently of the tab.
Login polling updates only its controls; it does not replace the client-file
picker or other settings while someone is using them. An account API 401 opens
the setup dialog and directs the user to the reconnect button.

## Developer workflows

- `uv sync --locked` installs this package and `../youtube-backup` from the
  checked-in lockfile. A missing sibling checkout is an installation error.
- `uv run youtube-download serve` is the supported entry point; dotenv is loaded
  there. `create_app(Settings(...), account=..., start_worker=False)` enables
  isolated browser/test fixtures without real account access or downloads.
- `uv run pytest -q` runs API/store/finder/worker tests. The real yt-dlp test uses
  a generated ffmpeg clip and a temporary localhost HTTP server. It requires
  local network binding but no YouTube credentials or external network.
- There is no frontend bundling step. Static assets are included in the wheel.
- `node --check src/youtube_download/static/app.js` validates JavaScript syntax.
- Browser QA uses a separate data/download root and fake account, checks desktop
  and mobile layouts, queue/pause/search, local playback, archive links, and a
  moved folder. Screenshots are temporary and removed after verification.
- Runtime logs are rotated at 2 MB with three backups. SQLite, downloads,
  cookies, credentials, `.env`, logs, and browser artifacts are ignored by Git.

The schema is created idempotently. The folder-override column has an additive
compatibility migration in `Store.__init__`; there is no external migration
framework. The advisory API quota file is app-local; Google's real project
quota is shared with other tools using the same OAuth client.
