# Prompt history

## 2026-11-16 — Repository instructions supplied by the user

### General

- If a folder does not contain an AGENTS.md file but has a CLAUDE.md file, consider it as an AGENTS.md file and proceed accordingly.
- Everything should be clean, performant and adhering to modern coding standards.
- UI and UX should be based on modern standards.
- Always ask before removing or changing any major existing functionality that is out of the scope of the current request before working on it.
- After every fix generate a git commit message that can be used by the user (never add codex as the author or co-author). Also, do not commit for me, only generate the message.
- When not having any context and trying to understand a new project, start with the readme and architecture files, if they exist, and then move on to the other files (the readme and architecture files could also be stale, so keep that in mind).
- Always add detailed comments for important sections of the code and follow general good commenting etiquette.
- Ask for clarifications where necessary.

### Tech Choices

- Always use uv for any python or pip related projects.

### Architecture

- Understand the architecture of the project using the ARCHITECTURE.md file (if present).
- Create the ARCHITECTURE.md file if not present (update it in case of any architectural change).
- While making the file consider:
  - The big picture architecture that requires reading multiple files to understand: major components, service boundaries, data flows, and the why behind structural decisions.
  - Critical developer workflows (builds, tests, debugging), especially commands that aren't obvious from file inspection alone.
  - Project-specific conventions and patterns that differ from common practices.
  - Integration points, external dependencies, and cross-component communication patterns.
  - Avoid generic advice; focus on the project's specific approaches.
  - Document only discoverable patterns, not aspirational practices.
  - Reference key files/directories that exemplify important patterns.

### Files

- Make sure any changes that would warrant an update to the readme are documented accordingly (make a README.md file if it does not exist).
- Generate an AGENTS.md file if it does not exist (keep it empty).
- Append all the prompts to a PROMPT.md file (create if not present).
- Make sure to update the gitignore file if necessary (create one if it does not exist).
- Create/append a summary of the work done after each change in memory/YYYY-MM-DD.md.
- When a new codex code session is started, read the last 2 files (date wise) from this memory folder to get recent context.
- Any new or updated env vars should be appropriately updated in .env.example.
- If the codebase has a LEARNINGS.md file, read and remember its learnings. Create or add to it as necessary.
- Always add an MIT license by default for greenfield projects. Use digster as the right holder name.

### Testing

- Always set up tests for a new project.
- Always add tests for new features and run after changes to test integrity.
- For UI changes of web projects, test using an actual browser instance.
- Remove screenshot files after testing if created by the testing library/MCP.
- Set up proper logging with appropriate log levels.
- After writing code, list the edge cases and suggest test cases to cover them.
- When there's a bug, start by writing a test that reproduces it, then fix it until the test passes.

### Git Branches

- When creating feature branches, give them a task-relevant name.
- Personal email is not allowed for GitHub pushes; use 3542726+digster@users.noreply.github.com for commits.

## 2026-11-16 — Initial implementation request

- We want to use this tool to download videos from a playlist.
- It will be an HTML GUI which will use https://github.com/yt-dlp/yt-dlp to download the videos.
- I should be presented with a list of the playlists for which I want to download. Out of ~/lab/tools/youtube-client and ~/lab/tools/youtube-backup see which utility will be ideal to fetch the account data.
- If the downloads are interrupted, they should resume for that playlist.
- The local videos downloaded for a playlist should be displayed against the list (ideally in a separate page).
- Use https://github.com/TheTechRobo/youtubevideofinder/ repo to get meta URLs for videos which are not available.
- Do the commits on my behalf.

The explicit commit instruction above supersedes the earlier message-only instruction for this task.

## 2026-11-16 — Folder relocation request

Also allow changing the download folder for a list if the local videos are moved.

## 2026-11-16 — Continuation

continue

## 2026-11-16 — Google reconnect in the UI

Instead of typing the command, can you implement the Google login reconnecting through the ui?

## 2026-11-16 — Cookie source setup through the UI

Also enable ui flow for setting this file for yt-dlp. For example, in my case it
could be Firefox or Safari(could also be others).

Reference: screenshot of the existing Private or restricted videos setup text
requiring YTDL_COOKIES_BROWSER or YTDL_COOKIES_FILE in .env.

make sure to add support for atleast firefox/safari/chrome

The assistant initially left these changes uncommitted after applying the
general AGENTS.md guidance; the correction below confirms the explicit request
to commit on the user's behalf.

## 2026-11-16 — Commit authorization reminder

I had asked you to do the commits for me.

## 2026-11-16 — Safari cookie permission failure

Getting this error for all videos. What could be the issue?

Attached screenshot shows yt-dlp reporting `ERROR: [Errno 1] Operation not
permitted` when accessing Safari's `Cookies.binarycookies` file.

## 2026-11-16 — Firefox cookie discovery failure

Tried Firefox and got this error.

Attached screenshot shows `could not find firefox cookies database` in the
default macOS Firefox Profiles directory.

## 2026-11-16 — Prefer avoiding Full Disk Access

Ideally I don't want to give the full disk access. I'll try the about:support method.

## 2026-11-16 — Continue the pending documentation commit

continue

## 2026-11-16 — Explicit Firefox profile and limited folder access

this is the path - /Users/ishan/Library/Application Support/Firefox/Profiles/ldbl1cym.default-release
What should I do now?

Can I give access to that specific profile path?

## 2026-11-17 — Help exporting and uploading cookies

Help me with this step.

Attached screenshot highlights the previous instructions to upload a
Netscape-format cookies.txt export when direct browser access remains blocked,
then save the cookie source and resume/retry the playlist.

## 2026-11-17 — Original video links and custom playlists

Few things -

- The videos in the playlist should also have a link to the original YouTube video link.
- Also allow creation of custom playlists by accepting a list of video URLs.

## 2026-11-17 — Readable default playlist folders

Also, by default use the playlist name for the folder name and a small random ID.
playlist name - random id(not too big)

## 2026-11-17 — Original playlist links

Also want to confirm that the YouTube playlists also link their original URL.

## 2026-11-17 — Commit the completed changes

I had told you commit for me.

## 2026-11-17 — yt-dlp settings in the UI

Allow setting the settings for yt-dlp through the ui

## 2026-11-17 — Custom playlist limit

Also, why is there a limit for the custom playlists?

Attached screenshot shows a Watch Later import rejected by the 500-video limit.

## 2026-11-17 — Commit reminder and continuation

Also remember to do the commits for me.

continue

## 2026-11-17 — Custom playlist metadata updates

For custom playlists, also add an option to update the video metadata for the
videos in the list based on the video URLs.

Attached screenshot shows a custom playlist with generic video-ID labels.

continue

## 2026-11-17 — Commit authorization reaffirmed

I had asked you to commit for me.

## 2026-11-18 — Reduce continuous API polling

Why are there continuous api hits on the server?

Attached screenshot shows repeated local playlist GET requests returning 200.

Okay, then work on these improvements.

Accepted improvements: frequent polling during active jobs, slower checks when
idle, and immediate refresh when returning to a tab.

## 2026-11-18 — Visible library refresh

There is a very visible repaint/refresh happening in the UI. See how it can be fixed.

Attached screenshot shows the playlist overview with 70 playlists and one active job.

Also do the commits for me.

## 2026-11-18 — Video thumbnails inside playlists

Why are there no thumbnails for the videos in a list?

Attached screenshot shows two video rows with play-icon placeholders.

continue

## 2026-11-18 — Skip individual downloads and search by video title

- add a skip option to allow skipping individual videos from download in a list.
- Also it's difficult to find the video by name on disk since the video's name is not visible. Add an empty text file with the video's name so that we can at least search(or suggest a better alternative).

Attached screenshot shows video-ID folders containing media and metadata named by ID.

Also do the commits for me.
