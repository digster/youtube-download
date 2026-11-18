# Learnings

- A yt-dlp filename existing on disk does not prove completion: separate format
  streams and interrupted merges can look like finished videos. Use an
  `after_move` receipt plus a nonempty file check.
- Keep video IDs, not titles or positions, in file/subfolder paths. Readable
  playlist roots must be allocated once and persisted; recomputing them from
  titles on sync breaks partial-file resume after a rename.
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
- yt-dlp's cookie-file discovery uses os.walk without an error callback. An
  unreadable Firefox profile directory can therefore report a missing database.
  Use explicit directory enumeration when diagnosing; pathlib glob and
  ConfigParser.read can also hide permission errors.
- Filename limits can count UTF-8 bytes rather than characters. Cap both when
  allocating readable playlist folders so long emoji titles remain usable.
- Metadata sidecars enrich a verified download but do not establish completion.
  Keep optional metadata access inside error handling, including containment
  checks, so missing or unreadable metadata cannot fail completed media.
- Validate speed limits with yt-dlp's current `utils.parse_bytes`, not only a
  positive-number check: tiny fractional byte values round down to zero and
  fail its CLI validation. The older FileDownloader helper is deprecated.
- Bound custom-import request size rather than imposing a low video count.
  Large playlists need bounded DOM rendering; keep search/filter over the full
  saved snapshot before selecting the current page.
- Keep download preferences immutable per subprocess. Persist the new file
  before publishing an updated in-memory snapshot, so failed writes cannot
  silently change the next download's behavior.
- Metadata-only lookups need their own explicit simulation command: reusing
  download arguments can enable subtitle/thumbnail writes or format checks.
  Test against the installed yt-dlp with a media endpoint that must receive no
  requests. Keep lookup progress separate from media completion and preserve
  known fields when a lookup fails or an optional thumbnail URL is malformed.
- Schedule polling from request completion, with independent library/login
  timers. Do not drop a post-mutation refresh just because an older GET is in
  flight: coalesce it into a trailing read. Otherwise stale idle state can
  delay progress updates. Likewise, ignore sign-in status reads superseded by
  a newer start/cancel/config action, and deduplicate visibility/page-cache
  resume events so returning to a tab does not produce duplicate requests.
