# Shelf · YouTube Download

A local HTML GUI for selecting your YouTube playlists, downloading their videos
with [yt-dlp](https://github.com/yt-dlp/yt-dlp), and browsing your offline library.
The Python server runs on your computer; no hosting service or frontend build is
required. Supported platforms: macOS and Linux.

## Features

- Connect or reconnect Google directly from **Account & setup**, without a terminal command.
- Choose Firefox, Safari, Chrome, or another supported browser for download
  cookies, or upload cookies.txt directly from **Account & setup**.
- List your account's owned playlists, including private playlists and Liked videos.
- Create custom local playlists by pasting YouTube video URLs, without Google login.
- Open the original YouTube playlist from its card or library page, and the
  original video from every video row, including downloaded or unavailable videos.
- Give new playlists readable folders such as `Weekend watchlist - a1b2c3`.
- Select several playlists to download in a persistent, sequential queue.
- Pause and resume; unfinished jobs resume after restarting the server. Closing
  the browser does not interrupt an active download.
- Browse each playlist on its own library page, filter local or unavailable
  videos, play compatible formats, and save files for an external player.
- Change a playlist's folder after moving it, recheck existing files, and use the
  new location for future downloads.
- Find metadata and archive URLs for unavailable videos through
  [TheTechRobo/youtubevideofinder](https://github.com/TheTechRobo/youtubevideofinder/).
- Keep local videos and previously known metadata when videos disappear from the
  remote playlist. Syncing never deletes media.

## Setup and launch

Install Python 3.12+, [uv](https://docs.astral.sh/uv/), ffmpeg, and a supported
JavaScript runtime for yt-dlp (Node 20+ or Deno). For macOS with Homebrew:

```sh
brew install ffmpeg node
```

Keep the existing `youtube-backup` checkout alongside this repository:

```text
tools/
├── youtube-backup/
└── youtube-download/
```

Then, from `youtube-download`:

```sh
uv sync --locked
cp .env.example .env
uv run youtube-download serve
```

Open [the local app](http://127.0.0.1:8765). Use `--port 8767` to change the port.
The server intentionally binds only to loopback. Run one server per data
directory, without Uvicorn's multiple-worker mode. Stop with Ctrl+C.

### Connect your YouTube account

Shelf reuses `youtube-backup`'s existing OAuth token. An API key is insufficient
for private account data. If no token exists, or Google rejects an expired login:

1. Open **Account & setup** in the sidebar.
2. Click **Connect with Google** or **Reconnect with Google**.
3. Complete Google's consent screen in the browser that opens. Google returns
   you to Shelf, and the original tab also updates its connection status.
4. Click **Sync playlists** in the connected account panel.

Shelf automatically finds `./client_secrets.json` or the existing
`../youtube-backup/client_secrets.json`. You can also set `YTDL_CLIENT_SECRETS`.
If no file is available, use **Google OAuth client file → Choose Desktop app
client JSON** in the same dialog. The selected configuration is saved locally
with owner-only permissions under `data/oauth-client.json`; it takes precedence
over environment/discovered files. Files must be valid Google Desktop app
client JSON and smaller than 64 KB.

Use your Google Cloud **Desktop app** OAuth client JSON with the YouTube Data API
v3 enabled. The login requests read-only YouTube access and stores its token using
`youtube-backup`'s platformdirs location (on macOS:
`~/Library/Application Support/youtube-backup/token.json`). The token is never
returned to the HTML client or copied into this repository. Its replacement is
atomic, so denial, cancellation, or a failed exchange keeps the previous login.

The button opens your default system browser. If it does not open, use the
**Open Google sign-in** link. Waiting attempts expire after five minutes;
**Cancel sign-in** lets you start again immediately. Restarting the server
invalidates an unfinished login. You can close/reopen the setup dialog without
cancelling an attempt. The CLI login remains available for terminal workflows:

```sh
uv run youtube-download auth login --client-secrets ../youtube-backup/client_secrets.json
uv run youtube-download auth status
```

Why `youtube-backup` rather than `youtube-client`: the former already supports
account OAuth, `mine=True` playlist pagination, retries, and Liked videos.
`youtube-client` uses an API key and is designed for public channel data.
The sibling package is a uv path dependency; neither sibling's source is changed.

**Account API limitations:** this lists playlists you own, not every playlist you
saved from another channel. YouTube's account API does not expose Watch Later.
The initial sync fetches playlist headers only; **Load/Refresh videos** or the
first download fetches a playlist's contents. Refresh videos explicitly to pick
up later additions. Resuming uses the saved snapshot, including when offline.

### Cookies for downloads

OAuth lists playlists; yt-dlp uses browser cookies to download videos that need
sign-in. Open **Account & setup → Private or restricted videos**:

1. Choose **Use a browser**, then select **Firefox**, **Safari**, **Chrome**, Brave,
   Chromium, Edge, Opera, Vivaldi, or Whale. Sign in to YouTube in that browser.
   Leave advanced options blank for automatic profile selection. You can specify
   a profile name/path and, for Firefox, a container name (`none` excludes
   containers). Safari's optional path points to its cookies database instead.
2. Alternatively, choose **Upload cookies.txt** and select a Netscape-format
   export, up to 5 MB. JSON exports and browser database files are not accepted by
   this file picker. The browser option reads native databases through yt-dlp.
3. Click **Save cookie source**. Changes persist and apply to the next video
   without restarting Shelf. A running video keeps its original source. Pause
   and resume the playlist if you want to apply a change to that video now.

Safari extraction is supported on macOS. If access is denied, grant Full Disk
Access to the app/terminal running Shelf in **System Settings → Privacy &
Security**. Chromium browsers may require OS keychain access. Browser access is
checked when downloading, not when saving preferences. Saving a browser choice
does not verify that it is installed or signed in. Errors appear on the video's
library row. The UI uses yt-dlp's supported
[`--cookies-from-browser` option](https://github.com/yt-dlp/yt-dlp#filesystem-options).

If every video fails with `Operation not permitted` for Safari's
`Cookies.binarycookies`, macOS is blocking cookie extraction before the video
download starts. Grant access to the terminal/app that launches Shelf (for
example Terminal or iTerm), quit and reopen that app, restart Shelf, and choose
**Resume / retry**. Changing Google OAuth login will not fix this filesystem
permission. Alternatively, sign in to YouTube in Firefox or Chrome and select
that browser in setup. For public videos, **No cookies** also avoids Safari
extraction, though some videos may still require authentication.

Firefox's `could not find firefox cookies database` can also mean access is
blocked: yt-dlp's directory search suppresses directory-read errors. Check
**Full Disk Access** for the app that starts Shelf, not only the browser. If
Shelf starts in iTerm2, grant iTerm2 access and fully quit/reopen it before
restarting Shelf. If permissions are already correct, open `about:support` in
Firefox and use **Profile Folder → Show in Finder** to locate the active profile.
Enter that full folder path in Shelf's **Profile & advanced options**, save,
and retry. The path should be a profile folder containing `cookies.sqlite`,
not the database file itself. See [Mozilla's profile guide](https://support.mozilla.org/en-US/kb/profiles-where-firefox-stores-user-data).

Entering a profile path does not grant macOS permission to read it. Shelf's web
form cannot authorize the Python server to access a protected profile folder.
To avoid Full Disk Access when that folder remains blocked, use **Upload
cookies.txt** with a Netscape-format export. This passes only the selected file
contents to Shelf; do not select `cookies.sqlite` in the cookies.txt picker.

#### Export YouTube cookies from Firefox without Full Disk Access

1. In Firefox, install [cookies.txt by Lennon Hill](https://addons.mozilla.org/en-US/firefox/addon/cookies-txt/),
   an exporter linked from [yt-dlp's FAQ](https://github.com/yt-dlp/yt-dlp/wiki/FAQ#how-do-i-pass-cookies-to-yt-dlp).
   It requests access to website data; you can remove it after exporting.
2. Open `about:addons`, select **Extensions → cookies.txt**, and set **Run in
   Private Windows → Allow**. See [Mozilla's instructions](https://support.mozilla.org/en-US/kb/extensions-private-browsing).
3. Close any existing private windows. Open a fresh private window with
   **Command-Shift-P**, visit YouTube, and sign in to the account with access to
   your videos. In that same tab, open <https://www.youtube.com/robots.txt>.
4. Open the cookies.txt extension from Firefox's extensions/toolbar menu. Click
   **Download** beside **Current Container and Site**. This selects the current
   tab's cookie store and site, rather than every site's cookies. Save the `.txt`
   file to Downloads; a name such as `cookies.firefox-private.txt` is valid.
5. After the file has saved, close the private window without signing out first.
   This follows [yt-dlp's YouTube cookie export guidance](https://github.com/yt-dlp/yt-dlp/wiki/Extractors#exporting-youtube-cookies)
   to reduce cookie rotation from continued browser use of the exported session.
6. In Shelf, open **Account & setup → Private or restricted videos**, choose
   **Upload cookies.txt**, select the exported file, and click **Save cookie source**.
   Return to the playlist and click **Resume / retry**. No server restart is needed.

The export contains sign-in credentials. Keep it local and upload it only to your
local Shelf app, not to chat or a public site. Shelf keeps its own private copy,
so the exported download can be deleted after a successful save. If YouTube later
rejects the saved login, export a fresh session and replace the uploaded file.

Choose **No cookies** to disable cookies, including any startup default. **Use
startup defaults** removes the UI override and restores the following optional
`.env` settings (only one may be set):

```dotenv
YTDL_COOKIES_BROWSER=chrome
# OR a Netscape-format cookie file:
# YTDL_COOKIES_FILE=/absolute/path/to/cookies.txt
```

Restart the server only if changing `.env`; UI selections take precedence.
Preferences and the current uploaded cookie text are stored atomically in
`<YTDL_DATA_DIR>/cookies.json` with owner-only permissions. Status endpoints never
return cookie contents. Each download uses a temporary private working copy,
because yt-dlp writes its cookie jar back to disk. This preserves both your
original export and any replacement uploaded while a download is running.
Working copies are removed after use; startup removes leftovers from crashes.
Switching sources replaces the saved upload. Cookies, OAuth files, media, and
runtime data are excluded from Git.

## Using the library

1. **Sync playlists**, then select cards or open an individual playlist.
2. Click **Download selected** or **Download**. Only one video downloads at a
   time to keep resource use predictable. An unavailable video does not block
   the rest of its playlist.
3. Use **Pause** and **Resume / retry** as needed. Retry skips verified existing
   files and retries unfinished or failed videos. User-paused playlists stay
   paused after restart; queued/running work resumes automatically.
4. Open **View library** to inspect video statuses and play local files. Browsers
   do not support every yt-dlp codec/container; use **Save video file** and an
   external player when necessary.
5. For missing videos, open **Archive links / Find archives**. Links are also
   fetched automatically when a queued video is identified as unavailable.

Synced playlists have a **YouTube playlist ↗** link on both their card and
library page. Every video has a **YouTube ↗** link alongside its local playback
or archive actions. These links open YouTube in a new tab; unavailable videos
may still be unavailable there.

### Create a custom playlist from video URLs

Click **Create playlist**, enter a name, and paste one YouTube video URL per
line (up to 500). Standard watch links, youtu.be links, Shorts, live, and embed
links are supported. Duplicate videos are saved once, in first-seen order;
timestamps and tracking parameters are discarded. Invalid URLs are reported
by line number, and nothing is created until every line is valid.

Creation opens the new local library; click **Download** when ready. Custom
playlists use the same queue, pause/resume, folder changes, and archive lookup
as synced playlists. They require no Google account connection and are not
published to YouTube. Private videos still need suitable download cookies.
Video titles initially show their IDs and are updated from yt-dlp metadata
after a successful download. Custom playlists have video links but no original
YouTube playlist link or Google refresh action.

### Default download folders

New synced and custom playlists use `<playlist name> - <6-character random ID>`
inside the configured download directory, for example
`Weekend watchlist - a1b2c3`. Unsafe filename characters are removed and long
names shortened. The folder is saved when the playlist is created; later title
changes keep that location so completed and partial files remain usable.

Existing playlist folders and manually selected locations stay unchanged.
Older playlists can therefore still use their original playlist-ID folder.

### Move a playlist to another folder or drive

Pause the playlist and wait for its download to stop. Move its entire folder,
including hidden files and video ID subfolders, with your file manager. Then
open **View library → Change folder**, enter the new absolute folder path (or
`~/...`), and click **Save & recheck files**.

Choose the folder containing the video ID folders, not its parent. For example:

```text
/Volumes/Media/My favourite playlist/     ← choose this folder
└── dQw4w9WgXcQ/
    ├── dQw4w9WgXcQ.mp4
    ├── dQw4w9WgXcQ.info.json
    ├── .completed.jsonl
    └── .download.lock
```

This changes the location mapping; the app does not move or rename files.
An existing empty folder can also be chosen as a new destination. Missing files
are shown as **File missing**, not counted as saved, and can be downloaded again.
Keep filenames and subfolder structure unchanged. Partially downloaded files
resume in the new folder if moved with the rest of the playlist. Selecting the
same folder for two playlists is rejected. Retained videos no longer in the
remote playlist are never automatically downloaded again.

Keep the `data/library.sqlite3` database when migrating the app itself. Changing
`YTDL_DATA_DIR` to an empty directory starts a new library; changing only the
global download directory does not relocate existing media.

### Archive lookup

The adapter calls the upstream project's documented
[`GET /api/v5/:videoid`](https://findyoutubevideo.thetechrobo.ca/api). It stores
safe HTTP(S) URLs, source names, and metadata/video labels in SQLite. Successful
lookups are cached for seven days; failed requests can be retried. Some archive
services may fail independently. A result can contain metadata without a
recoverable video; archive links are not downloaded automatically. These
requests send the video ID to the configured finder service. Use
`YTDL_FINDER_URL` for your own deployment of the upstream repository.

## Configuration

Copy `.env.example` to `.env`; all settings are optional.

| Variable | Default | Purpose |
| --- | --- | --- |
| `YTDL_DATA_DIR` | `./data` | SQLite database, advisory API quota, logs, server lock |
| `YTDL_DOWNLOAD_DIR` | `<data dir>/downloads` | Default parent folder for playlists |
| `YTDL_PORT` | `8765` | Loopback HTTP port; CLI `--port` takes precedence |
| `YTDL_LOG_LEVEL` | `INFO` | Python console and rotating file log level |
| `YTDL_CLIENT_SECRETS` | empty | Optional Google Desktop OAuth client JSON; otherwise auto-detected |
| `YTDL_COOKIES_FILE` | empty | Default Netscape-format cookie file; UI selection overrides it |
| `YTDL_COOKIES_BROWSER` | empty | Default browser/profile; UI selection overrides it |
| `YTDL_FORMAT` | `bv*+ba/b` | yt-dlp format selector; best video and audio by default |
| `YTDL_FINDER_URL` | `https://findyoutubevideo.thetechrobo.ca` | youtubevideofinder API base URL |

Relative paths resolve from the directory where the server starts. Prefer
absolute paths if launching from different directories. Per-playlist folder
overrides are stored in SQLite and take precedence over the default parent.
Jobs use the current format/cookie configuration on resume; keep the format
unchanged to maximize partial-file reuse. This app ignores global yt-dlp config
and plugins to preserve its output/receipt contract.

## Development and verification

```sh
uv sync --locked
uv run pytest -q
uv run pytest --cov=youtube_download --cov-report=term-missing
node --check src/youtube_download/static/app.js
```

Tests use temporary directories and mocked account/finder responses. The real
yt-dlp integration test generates a tiny video with ffmpeg and downloads it from
a loopback HTTP server; it needs localhost access and never contacts YouTube.
It is skipped when ffmpeg is not installed. Browser QA uses a separate fixture
database, not the user's account or downloads.

Covered edge cases include duplicate queue requests and playlist entries,
pause/resume of an actual subprocess, restart recovery, a completed receipt
without a database update, moved folders, deleted local files, removed playlist
entries, unavailable videos, metadata preservation, expired OAuth, finder
failures, byte-range playback, and path/symlink containment.
Custom playlist tests cover URL validation and deduplication, atomic creation,
operation without Google, original source links, custom pause/resume, downloaded
metadata, folder collisions, long Unicode names, and legacy directory migration.
Authentication tests cover PKCE, read-only scopes, success/denial, cancelled and
expired callbacks, duplicate clicks, bad callback state, failed exchanges,
invalid client files, and atomic token replacement. Browser QA uses a mock
Google consent page and never alters your real credentials.
Cookie tests cover every supported browser's actual yt-dlp argument parsing,
Firefox containers, empty/malformed/oversized files, HttpOnly/session cookies,
saved-source precedence, disk-write failure, private permissions, and changing
sources while an earlier snapshot remains active. The localhost integration
test also verifies that yt-dlp sends an uploaded fixture cookie. Browser QA
checks file selection, source/profile persistence, polling during edits, and
mobile layout without reading any real browser cookies.

Additional integration cases to test with your setup:

- Disconnect and reconnect the network during a large fragmented download.
- Fill or disconnect an external drive, reconnect it, and retry the playlist.
- Download a private video with browser cookies and an expired browser login.
- Check Firefox, Safari, and Chrome extraction with your own signed-in profiles;
  test missing profiles and denied Safari disk/Chrome keychain permissions.
- Interrupt the server during ffmpeg merging, restart it, and verify playback.
- Try a large real playlist containing repeated IDs and deleted/private entries.
- Move a paused playlist across volumes while retaining `.part`, `.ytdl`, and
  hidden receipt files, then resume from its new folder.
- Complete a real Google reconnect, decline consent, and retry from the UI;
  also verify the fallback link if your default browser cannot be opened.

Logs are in `data/app.log` (2 MB rotation, three backups) and the terminal.
Account errors are deliberately summarized without logging OAuth response
contents. Failed downloads expose yt-dlp's error text in the affected video row.

Update yt-dlp when YouTube changes extraction behavior:

```sh
uv lock --upgrade-package yt-dlp --upgrade-package yt-dlp-ejs
uv sync --locked
```

See [ARCHITECTURE.md](ARCHITECTURE.md) for queue invariants and component
boundaries. This project is MIT licensed; external dependencies retain their
own licenses. youtubevideofinder is consumed over HTTP, not vendored.
