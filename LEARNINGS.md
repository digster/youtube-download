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
- Google Desktop OAuth can return to the app's existing loopback port. Keep
  state and PKCE in memory, validate state before a code-only token exchange,
  and do not disable OAuthlib's HTTPS protection globally.
- Callback URLs carry authorization codes. Remove the query from the ASGI
  scope before access logging and redirect to a clean app URL immediately.
- `secrets.compare_digest` rejects non-ASCII strings; compare encoded bytes so
  a malformed callback returns a normal rejection instead of HTTP 500.
- Validate endpoint types before set membership: malformed JSON can contain
  arrays/objects instead of strings, which are unhashable.
- yt-dlp reads and writes its `--cookies` file. Use a private per-video copy so
  an in-flight job cannot overwrite a replacement upload or the user's export.
- yt-dlp's cookie loader prints malformed records, including values. Validate
  Netscape exports before passing them to yt-dlp and return generic errors.
- Browser profile paths are not interchangeable: Safari expects a binarycookies
  file where Firefox/Chromium accept profiles. Clear drafts when browsers change.
