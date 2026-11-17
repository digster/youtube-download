# Learnings

- A yt-dlp filename existing on disk does not prove completion: separate format
  streams and interrupted merges can look like finished videos. Use an
  `after_move` receipt plus a nonempty file check.
- Keep IDs, not titles or playlist positions, in output paths. Otherwise renames
  and reorders break partial-file resume.
- Store media paths relative to the playlist root. Completion receipts from
  yt-dlp contain absolute paths, so resolve the recorded basename under the
  current video directory when reconnecting a moved folder.
- An expired token file is not a connected account. RefreshError needs explicit
  reconnect guidance, and raw OAuth exception content must not reach the UI/log.
- A deleted/private placeholder from an API refresh should not overwrite the
  last useful title or thumbnail already saved in the library.
- Loopback integration tests need permission to bind a port in sandboxed
  environments. Keep them real; do not silently replace the yt-dlp integration
  test with a mocked success when localhost is blocked.
- The public youtubevideofinder v5 API returns `keys[].available[]` link objects
  with `contains.video`/`contains.metadata`, rather than a single URL per service.
