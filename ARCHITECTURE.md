# Architecture

## Big picture

Shelf is a local FastAPI server with a buildless HTML/CSS/JavaScript client.
SQLite persists metadata and job intent, while yt-dlp owns transfer and merge
details. Each data directory has independent download, metadata and archive
workers. Runtime files are deliberately separate from
versioned source.

```text
Browser: static/index.html + polling.js + app.js + style.css
  ├─ GET /api/playlists, /api/playlists/:id (2 seconds active / 30 seconds idle)
  ├─ POST sync, refresh, download, pause, folder, find
  ├─ POST /api/playlists/custom (local URL list → saved membership)
  ├─ POST /api/playlists/:id/metadata, /metadata/resume, /metadata/cancel
  ├─ POST /api/auth/start, /config, /cancel; GET /api/auth/status
  ├─ GET/POST /api/cookies; POST /api/cookies/file
  ├─ GET/POST /api/download-settings; POST /api/download-settings/reset
  ├─ GET/POST /api/download-directory; POST /api/download-directory/reset
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

  metadata.py → runner.py → yt-dlp simulation → metadata fields in SQLite
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
- `custom_playlists.py` validates pasted video links against known YouTube
  hosts and URL forms, keeps only canonical video IDs, and deduplicates in input
  order. It never fetches user-supplied URLs. `/api/playlists/custom` validates
  the whole list before atomically creating its header and membership in SQLite.
  The `source` column distinguishes `custom` from `youtube`; custom membership
  is immediately ready, bypasses Google during queueing, and rejects remote refresh.
- `downloads.py` owns scheduling and process control. It launches one video at a
  time with explicit options, no shell, no external yt-dlp configuration, and no
  user plugins. Files are named by video ID, so title changes and reordering do
  not change the resume destination. Each playlist has its own physical copies.
- `metadata.py` owns a separate persistent queue for custom video inspection.
  `MetadataManager` shares cookie/download settings with the download worker,
  but runs its own simulation-only `runner.py` subprocess and writes only
  title/channel/thumbnail and metadata progress fields. No Google API is used.
- `download_settings.py` owns immutable, strictly validated `DownloadOptions`
  and atomic saved preferences. `DownloadManager` snapshots these immediately
  before building each subprocess command. The same service instance is shared
  with the settings API; a save affects subsequent launches, including retries,
  without mutating a running command or the startup `Settings` object.
- `cookies.py` owns live cookie preferences, upload validation, and per-video
  snapshots. `app.py` and both workers share one instance. Each manager takes
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

Each membership has a durable `download_skipped` flag, independent of its
transfer status and metadata job. The video `/skip` POST sets an explicit
boolean. The downloader checks it between videos and under the subprocess
launch lock. Skipping the active transfer signals its process group and keeps
the playlist running. Receipt recovery still records a transfer that finished
just before a skip. Progress and completion count saved and skipped entries
separately, and a restored entry becomes eligible on its next visit.

The library renders a checkbox for each video and a three-state master
checkbox across all current members. The playlist-level `/videos/skip` POST
updates every member in one SQLite transaction under the same launch lock.
Both endpoints can change future download eligibility for saved media.
Removed rows are retained for browsing with their download controls disabled.

Transfers keep yt-dlp's `.part` and fragment state. `--abort-on-unavailable-fragments`
prevents incomplete fragmented streams from being accepted as complete.
`--print-to-file after_move:%(filepath)j` appends a receipt only after yt-dlp's
postprocessing/move stage. The worker verifies the referenced file is nonempty
and contained in the video directory before recording completion. If the app
dies between the receipt and SQLite update, the next run reconciles the receipt
without transferring again. An arbitrary `.mp4` or `.info.json` alone is not a
completion signal: it may be an unmerged stream. The GUI checks files on disk,
so a missing completed file stops counting as downloaded and can be retried.

Custom videos start with ID-based labels. After verifying completion,
`DownloadManager._save_file` reads a contained, size-limited yt-dlp `.info.json`
sidecar with a matching video ID to enrich title/channel. Missing or malformed
metadata never invalidates completed media. Receipt reconciliation uses the
same path, so metadata can also be recovered after interruption.

After recording media, the worker adds an empty title-named text file beside
it. `filenames.py` shares the playlist filename sanitizer and verifies the
recorded media relative to an open video directory before exclusive creation.
At startup and on a folder recheck, `Store.saved_media` supplies recorded paths,
including retained videos. Backfill uses the library title or matching local
yt-dlp metadata for an ID placeholder. It performs no network requests and
missing drives or unwritable title files leave transfer state intact.

Failures such as HTTP 429, network errors, or bot challenges remain retryable
`failed` states. Each playlist waits for its archive scan before transferring
media. If a metadata refresh starts during a download, the next video's
transfer waits for that video's new check. Only the user's checkbox determines
whether an otherwise eligible video is skipped.

## Archive checks

`archive_checks` stores one pending request and cached result per YouTube ID.
The store queues checks in the same transaction as custom creation, membership
sync or metadata refresh. Concurrent playlists share requests and results.
Existing libraries retain saved links and acquire missing checks on download.
The Finder worker drains outstanding IDs in request order. Each result includes
individual URLs, source, content flags, notes and partial service errors.
The compact library view reads a stored count and fetches full links for the
open dialog. Both views come from SQLite.

The background scan and manual lookups share a lock and a persisted
`finder_throttle` deadline. Finder reserves a request slot before HTTP and
extends the deadline after each response. The minimum gap is five seconds.
Redirect hops pass through the same gate. HTTP 429/503 use the server's
`Retry-After` or a sixty-second fallback. The UI queues an individual recheck
through the existing `/find?queue=true` POST.

## Custom playlist metadata updates

The protected metadata POST routes accept only custom playlists and return the
usual library snapshot. `metadata_jobs` stores queued/running/paused/completed/
completed_with_errors/error states independently of the media queue. Each video
has `metadata_status`, `metadata_error`, and `metadata_updated_at`; successful
lookups commit immediately. Resume retries pending/failed rows; a full update
resets lookup progress without deleting known fields. Repeated starts while
queued/running do not reset progress. Startup requeues running metadata work,
while explicit cancellations stay paused. Shutdown terminates the process group
and requeues unfinished work; the existing runner watches for parent death.

The worker builds canonical watch URLs from validated saved video IDs. Explicit
`--simulate --skip-download --no-check-formats --ignore-no-formats-error` options
allow inspection without downloading or probing media formats. A compact JSON
print template returns only ID/title/channel/uploader/thumbnail. Preferences
that request subtitles, thumbnail files, or postprocessing are never passed to
this command. Cookie snapshots apply per lookup; network timeout/retry preferences
are capped at 30 seconds/one retry, with a 90-second overall lookup deadline.

Output is bounded to 1 MiB in a private `metadata-sessions/` scratch directory;
the matching ID, useful title, and optional HTTPS thumbnail are validated before
writing. Invalid optional images preserve useful titles. Scratch directories and
cookie snapshots are cleaned after each lookup and at startup under the server
lock. Raw extractor output is not logged or exposed. Per-video failures retain
known metadata and continue; cookie configuration or unexpected worker failures
stop the job with a resumable error. Download status, files, partial transfers,
playlist order, and membership remain independent of metadata writes.

The custom library heading offers Update/Resume/Retry metadata; a separate panel
shows counts and Cancel/Update all controls. Active-job polling displays progress
and refreshed rows without making the open tab responsible for work.
Metadata jobs cover all current membership, independent of pagination/filtering.

## Folder changes and retained data

The setup form reads and saves the parent for new playlists with
`/api/download-directory`. Saving validates the absolute path and tests file
creation. The SQLite `download_directory` table holds one optional override
per library. A reset removes the row and uses the startup `YTDL_DOWNLOAD_DIR`.
Save, reset and playlist creation share the application operation lock.
Folder status reports available bytes on the effective drive, or null if
the drive is inaccessible.

New playlist directories are allocated once as
`<effective parent>/<sanitized title> - <6 hex characters>` and persisted in
`playlists.download_dir`. `Store._new_playlist_directory` caps both title length
and UTF-8 bytes, retries filesystem/database collisions, and defers directory
creation until downloading. Rows without a saved directory use the legacy
`<YTDL_DOWNLOAD_DIR>/<playlist_id>` fallback. Saving or resetting the parent
pins those paths in the same transaction, so later configuration changes
preserve access to existing files and partial transfers.
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

### yt-dlp preferences

`GET/POST /api/download-settings` reads/replaces global download preferences;
`POST /api/download-settings/reset` removes the override. These routes use the
local header/origin protections. Strict types, bounds, known containers, and
the installed yt-dlp format parser validate values before any write. Selectors
that produce multiple outputs are rejected to preserve the single-media receipt
contract. User input never adds arbitrary CLI flags, hooks, or output templates.

Settings are stored as mode-0600 `data/download-settings.json`, using a temporary
file plus fsync/replace. In-memory options are published only after successful
persistence. Failed save/reset leaves prior options active. Invalid saved JSON
falls back to startup defaults with a visible warning; tokens/cookies are stored
separately. Defaults retain the existing format, merge, timeout, and retry policy.

`static/download-settings.js` manages a dedicated native dialog loaded after
`app.js`; it shares the API helper and never participates in library/account
polling. Drafts survive validation errors and polling. Presets resolve to ordinary
format selectors, and conditional subtitle/custom-format controls keep common
settings compact. Settings controls are disabled during load/save/reset.

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
Playlist summaries expose a canonical YouTube playlist URL for synced sources
only; cards and library headings link to it. All video rows link to their
original YouTube URL independently of download status or local playback.
The custom playlist dialog preserves input on validation failure and disables
submission while creating. It makes no account request and does not auto-start
downloads. Successful creation navigates to the resulting local library.
Imports have no video-count cap; a 2-million-character input bound limits request
size. Detail views filter/search the full snapshot, then render 100 rows at a
time. Changing search/filter resets the page; polling clamps pages to the current
result count. API snapshots remain complete so queue and metadata semantics are
unchanged.
`static/polling.js` loads before `app.js` and owns `AdaptivePoller`, a completion-
scheduled timeout with one in-flight read per instance. Library snapshots use a
2-second delay when any displayed playlist has pending archive checks or an active media or metadata
job, otherwise 30 seconds (including paused/failed jobs). Overview checks all
saved playlists before filtering; detail pages check their own playlist. Failed
reads use a 30-second delay even if the saved state is active. User actions
request a fresh snapshot immediately. A forced refresh during an older GET is
coalesced into one trailing read so a stale response cannot hide a newly queued
job until the idle timer fires.

Visibility/page-hide events suspend timers; visibility/page-cache restoration
refreshes immediately and handles duplicate restore events once. An already
started read can finish while hidden but schedules no further polling. There
are no hidden-tab polling timers, cross-tab leader election, or server push.
Changes made in another tab appear on the next idle read or return-to-tab read.
`renderItems` in `static/app.js` matches visible cards and video rows by ID.
Its cache holds only mounted articles and their last rendered signatures.
Playlist signatures include the browser's selection and busy state. Changed
records update individual fields. Artwork is a separate region so progress
updates preserve decoded images, and a failed URL keeps its fallback until
the URL changes or the item leaves the view. Headers, counters, and the
metadata panel update independently of the list. Workers keep running
independently of the tab.

A separate poller checks sign-in every 2 seconds while waiting/exchanging, every
30 seconds with an idle setup dialog open, and stops when setup is closed and no
attempt is pending. It never forces a library read. A login revision counter
discards status responses superseded by start/cancel/client-upload actions.
Login polling updates only its controls, preserving client-file and cookie
drafts. An account API 401 opens setup and directs the user to reconnect.

## Developer workflows

- `uv sync --locked` installs this package and `../youtube-backup` from the
  checked-in lockfile. A missing sibling checkout is an installation error.
- `uv run youtube-download serve` is the supported entry point; dotenv is loaded
  there. `create_app(Settings(...), account=..., start_worker=False)` enables
  isolated browser/test fixtures without real account access or downloads.
- `uv run pytest -q` runs API/store/finder/worker tests. Metadata tests cover
  migrations, failure preservation, cancellation, shutdown/restart recovery,
  and a real yt-dlp simulation with a localhost media trap that must receive
  zero requests and produce no media or sidecars. The real download test uses
  a generated ffmpeg clip and a temporary localhost HTTP server. It requires
  local network binding but no YouTube credentials or external network.
- There is no frontend bundling step. Static assets are included in the wheel.
- `node --check src/youtube_download/static/app.js` validates JavaScript syntax.
- `node --test tests/frontend/polling.test.cjs` exercises the shipped polling/app
  scripts with a deterministic clock and local API fixtures, including idle/
  active timing, hidden tabs, request races, failed reads, and sign-in isolation.
  `tests/test_frontend.py` includes this suite in pytest; Node.js is required for
  developer tests, with no npm dependency installation or frontend build needed.
- Browser QA uses a separate data/download root and fake account, checks desktop
  and mobile layouts, queue/pause/search, local playback, archive links, and a
  moved folder. Screenshots are temporary and removed after verification.
- To run the rendering regression from the repo root, open a browser with
  `playwright-cli open about:blank`, then run
  `playwright-cli run-code --filename tests/frontend/rendering.browser.js`
  and `playwright-cli close`. This test serves the shipped assets and fixture
  API responses directly in Chromium. It checks DOM identity, scroll, focus,
  and thumbnail recovery across real polling and user interactions.
- Runtime logs are rotated at 2 MB with three backups. SQLite, downloads,
  cookies, credentials, `.env`, logs, and browser artifacts are ignored by Git.

The schema is created idempotently. The folder-override, source, and metadata
columns have additive compatibility migrations in `Store.__init__`; there is no
external migration framework. The advisory API quota file is app-local; Google's real project
quota is shared with other tools using the same OAuth client.
