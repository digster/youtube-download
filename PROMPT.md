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
