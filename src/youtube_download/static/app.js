/* The server owns all job state. Polling only refreshes the view; closing the tab
   never cancels a download. User selection and filters remain browser-local. */
const $ = (selector) => document.querySelector(selector);
const escapeHTML = (value) =>
  String(value ?? "").replace(
    /[&<>"']/g,
    (char) =>
      ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[
        char
      ],
  );
const safeURL = (value) => {
  try {
    const url = new URL(value);
    return ["http:", "https:"].includes(url.protocol)
      ? escapeHTML(url.href)
      : "";
  } catch {
    return "";
  }
};
const playlistId = location.pathname.match(/^\/playlists\/([\w-]+)$/)?.[1];
const view = new URLSearchParams(location.search).get("view") || "all";
const state = {
  playlists: [],
  playlist: null,
  videos: [],
  status: null,
  filter: "all",
  search: "",
  selected: new Set(),
  busy: new Set(),
  signature: "",
  login: null,
  loginBusy: false,
  loginRevision: 0,
  cookies: null,
  cookieBusy: false,
  creatingPlaylist: false,
  videoPage: 0,
  pendingInclusion: null,
  archiveVideo: null,
};
const labels = {
  idle: "Not started",
  queued: "Queued",
  running: "Downloading",
  paused: "Paused",
  completed: "Complete",
  completed_with_errors: "Needs attention",
  error: "Needs attention",
  pending: "Ready to download",
  skipped: "Skipped",
  downloading: "Downloading",
  downloaded: "On disk",
  missing: "File missing",
  failed: "Download failed",
  unavailable: "Unavailable",
};
const badge = (status) =>
  `<span class="badge ${escapeHTML(status)}">${escapeHTML(labels[status] || status)}</span>`;
const bytes = (value) => {
  if (!value) return "0 B";
  const units = ["B", "KB", "MB", "GB", "TB"];
  const i = Math.min(4, Math.floor(Math.log(value) / Math.log(1024)));
  return `${(value / 1024 ** i).toFixed(i > 1 ? 1 : 0)} ${units[i]}`;
};
const active = (p) => ["queued", "running"].includes(p.state);
const waitingForArchives = (p) => active(p) && p.archive_pending > 0 && !p.current_video;
const processedVideos = (p) => p.downloaded + (p.skipped || 0);
const videoNeedsAttention = (v) =>
  !v.local_available &&
  !v.download_skipped &&
  ["failed", "unavailable", "missing"].includes(v.status);
const needsAttention = (p) =>
  p.failed ||
  p.unavailable ||
  ["error", "completed_with_errors", "paused"].includes(p.state) ||
  (p.items_synced_at && p.state === "completed" && processedVideos(p) < p.total);
const apiPath = (id = playlistId) => `/api/playlists/${encodeURIComponent(id)}`;

const libraryPoller = new AdaptivePoller(fetchLibrary, () => {
  const playlists = playlistId ? [state.playlist] : state.playlists;
  return (playlistId && state.videos.some((video) => video.archive_pending)) || playlists.some(
    (p) =>
      p && (p.archive_pending || active(p) || ["queued", "running"].includes(p.metadata?.state)),
  )
    ? ACTIVE_POLL_MS
    : IDLE_POLL_MS;
});
const loginPoller = new AdaptivePoller(fetchLogin, () => {
  if (["waiting", "exchanging"].includes(state.login?.phase))
    return ACTIVE_POLL_MS;
  return $("#setup-dialog").open ? IDLE_POLL_MS : null;
});

const reload = (force = false) => libraryPoller.request(force);
const refreshLogin = () => loginPoller.request();

// Hidden pages have no polling timers. An already-started read may finish,
// but returning to the tab fetches fresh state without waiting for an idle tick.
document.addEventListener("visibilitychange", () => {
  for (const poller of [libraryPoller, loginPoller]) {
    if (document.hidden) poller.suspend();
    else poller.resume();
  }
});
window.addEventListener("pagehide", () => {
  libraryPoller.suspend();
  loginPoller.suspend();
});
window.addEventListener("pageshow", (event) => {
  if (event.persisted && !document.hidden) {
    libraryPoller.resume();
    loginPoller.resume();
  }
});
$("#setup-dialog").addEventListener("close", () => loginPoller.schedule());

// Custom lists exist only in Shelf; only synced lists have a YouTube source.
const playlistSourceLink = (playlist, className) =>
  playlist.source === "youtube" && safeURL(playlist.youtube_url)
    ? `<a class="${className}" href="${safeURL(playlist.youtube_url)}" target="_blank" rel="noopener noreferrer" aria-label="Open ${escapeHTML(playlist.title)} playlist on YouTube (new tab)">YouTube playlist ↗</a>`
    : "";

async function api(path, method = "GET", body) {
  const response = await fetch(path, {
    method,
    headers: {
      "X-Local-App": "1",
      ...(body ? { "Content-Type": "application/json" } : {}),
    },
    ...(body ? { body: JSON.stringify(body) } : {}),
  });
  const data = await response.json();
  if (!response.ok) {
    const error = new Error(
      typeof data.detail === "string"
        ? data.detail
        : "The request could not be completed.",
    );
    error.status = response.status;
    throw error;
  }
  return data;
}

function notice(message, error = false) {
  $("#notice").textContent = message;
  $("#notice").className = error ? "error" : "";
  $("#notice").hidden = false;
}

// Cache the last requested markup, rather than serializing the live DOM. A
// failed thumbnail can then stay on its fallback until its URL actually changes.
const lastMarkup = new WeakMap();
function setMarkup(element, html) {
  if (lastMarkup.get(element) === html) return;
  element.innerHTML = html;
  lastMarkup.set(element, html);
}

function setText(element, value) {
  const text = String(value ?? "");
  if (element.textContent !== text) element.textContent = text;
}

function setHidden(element, hidden) {
  if (element.hidden !== hidden) element.hidden = hidden;
}

function renderArtwork(element, thumbnail, fallback) {
  const url = safeURL(thumbnail);
  setMarkup(
    element,
    fallback +
      (url
        ? `<img src="${url}" alt="" loading="lazy" referrerpolicy="no-referrer">`
        : ""),
  );
}

// Keep each visible record's article mounted. Remove filtered/deleted items
// first so removing an early result never moves all its surviving siblings.
const renderedItems = new Map();
function renderItems(
  items,
  className,
  updateItem,
  signatureOf = JSON.stringify,
) {
  const list = $("#library-items");
  if (list.className !== className) list.className = className;
  const visible = new Set(items.map((item) => item.id));
  for (const [id, entry] of renderedItems) {
    if (!visible.has(id)) {
      entry.node.remove();
      renderedItems.delete(id);
    }
  }
  let next = list.firstElementChild;
  for (const item of items) {
    const previous = renderedItems.get(item.id);
    const node = previous?.node ?? document.createElement("article");
    const signature = signatureOf(item);
    if (signature !== previous?.signature) {
      updateItem(node, item);
      renderedItems.set(item.id, { node, signature });
    }
    if (node !== next) list.insertBefore(node, next);
    next = node.nextElementSibling;
  }
}

function stats(items) {
  const container = $("#stats");
  setMarkup(
    container,
    items
      .map(
        ([label]) =>
          `<div class="stat"><label>${escapeHTML(label)}</label><strong></strong></div>`,
      )
      .join(""),
  );
  items.forEach(([, value], i) =>
    setText(container.children[i].lastElementChild, value),
  );
}

function downloadButton(p, small = false) {
  const action = active(p) ? "pause" : "download";
  const text = active(p)
    ? "Ⅱ Pause"
    : p.state === "paused"
      ? "↓ Resume"
      : p.state === "completed" && processedVideos(p) === p.total
        ? "↻ Check downloads"
        : p.state === "idle"
          ? "↓ Download"
          : "↓ Resume / retry";
  return `<button class="button ${active(p) ? "secondary" : "primary"} ${small ? "small" : ""}" data-action="${action}" data-id="${escapeHTML(p.id)}" data-focus="${action}-${escapeHTML(p.id)}" ${state.busy.has(p.id) ? "disabled" : ""}>${state.busy.has(p.id) ? "Working…" : text}</button>`;
}

function renderFilters() {
  const options = playlistId
    ? [
        ["all", "All videos"],
        ["downloaded", "On disk"],
        ["skipped", "Skipped"],
        ["archives", "Archive links"],
        ["attention", "Needs attention"],
      ]
    : [
        ["all", "All playlists"],
        ["active", "In progress"],
        ["complete", "Complete"],
      ];
  $("#filters").innerHTML =
    options
      .map(
        ([key, label]) =>
          `<button class="filter ${state.filter === key ? "active" : ""}" data-action="filter" data-filter="${key}" aria-pressed="${state.filter === key}">${label}</button>`,
      )
      .join("") +
    (!playlistId
      ? '<label class="select-all"><input type="checkbox" id="select-all" aria-label="Select all visible playlists"> Select visible</label>'
      : "");
}

function visiblePlaylists() {
  return state.playlists.filter((p) => {
    if (view === "downloaded" && !p.local_count) return false;
    if (view === "attention" && !needsAttention(p)) return false;
    if (state.filter === "active" && !active(p) && p.state !== "paused")
      return false;
    if (
      state.filter === "complete" &&
      (!p.items_synced_at || processedVideos(p) !== p.total)
    )
      return false;
    return `${p.title} ${p.channel}`
      .toLowerCase()
      .includes(state.search.toLowerCase());
  });
}

function renderPlaylistCard(card, p) {
  const selected = state.selected.has(p.id);
  if (!card.firstElementChild) {
    const id = escapeHTML(p.id);
    card.className = "card";
    card.innerHTML = `
      <div class="card-top"><div class="art-fallback" aria-hidden="true"></div>
        <label class="card-check"><input type="checkbox" data-select="${id}" data-focus="select-${id}"></label><span class="video-count"></span></div>
      <div class="card-body"><div class="card-summary"></div><div class="status-line"></div>
        <progress class="progress" max="100" value="0"></progress>
        <div class="card-footer"><a class="library-link" href="/playlists/${encodeURIComponent(p.id)}">View library <span>↗</span></a><span class="download-action"></span></div></div>`;
  }
  card.classList.toggle("selected", selected);
  const checkbox = card.querySelector("input");
  if (checkbox.checked !== selected) checkbox.checked = selected;
  const selectLabel = `Select ${p.title}`;
  if (checkbox.getAttribute("aria-label") !== selectLabel)
    checkbox.setAttribute("aria-label", selectLabel);

  renderArtwork(card.querySelector(".art-fallback"), p.thumbnail, "▤");
  setText(card.querySelector(".video-count"), `▤ \u00a0${p.total} videos`);
  setMarkup(
    card.querySelector(".card-summary"),
    `<a class="card-title" href="/playlists/${encodeURIComponent(p.id)}" title="${escapeHTML(p.title)}">${escapeHTML(p.title)}</a><div class="card-meta"><span>${escapeHTML(p.channel || "Your YouTube account")}</span><span>${escapeHTML(p.privacy || "Playlist")}</span></div>${playlistSourceLink(p, "playlist-source")}`,
  );
  setMarkup(
    card.querySelector(".status-line"),
    `${waitingForArchives(p) ? '<span class="badge">Checking archives</span>' : badge(p.state)}<span>${p.downloaded} of ${p.total} saved${p.skipped ? ` · ${p.skipped} skipped` : ""}</span>`,
  );
  const progress = card.querySelector("progress");
  const percent = p.total
    ? Math.min(
        100,
        Math.round(
          (100 * (processedVideos(p) + (p.current_video ? p.progress / 100 : 0))) /
            p.total,
        ),
      )
    : 0;
  if (progress.value !== percent) progress.value = percent;
  setText(progress, `${percent}%`);
  const progressLabel = `${p.title} download progress`;
  if (progress.getAttribute("aria-label") !== progressLabel)
    progress.setAttribute("aria-label", progressLabel);
  setMarkup(card.querySelector(".download-action"), downloadButton(p, true));
}

function renderPlaylists() {
  const playlists = state.playlists;
  setText($("#nav-count"), playlists.length);
  stats([
    ["Playlists", playlists.length],
    ["Videos on disk", playlists.reduce((s, p) => s + p.local_count, 0)],
    ["In progress", playlists.filter(active).length],
    ["Library size", bytes(playlists.reduce((s, p) => s + p.bytes, 0))],
  ]);
  const shown = visiblePlaylists();
  let info = "";
  if (!playlists.length) {
    info = `<div class="empty"><div class="empty-icon">▤</div><h3>A home for your playlists.</h3><p>Create a playlist from video URLs, or sync your YouTube account. Downloads and progress will appear here.</p><button class="button primary" data-action="create-playlist">+ Create playlist</button> <button class="button secondary" data-action="sync">↻ Sync playlists</button> <button class="button quiet" data-action="setup">Account setup ↗</button></div>`;
  } else if (!shown.length) {
    info =
      '<div class="empty"><div class="empty-icon">⌕</div><h3>Nothing here just yet.</h3><p>No playlists match this view. Try a different filter or search.</p></div>';
  }
  setMarkup($("#library-info"), info);
  renderItems(shown, "grid", renderPlaylistCard, (p) =>
    JSON.stringify([p, state.selected.has(p.id), state.busy.has(p.id)]),
  );
  updateSelection(shown);
}

function metadataButton(p) {
  const job = p.metadata || { state: "idle" };
  const running = ["queued", "running"].includes(job.state);
  const resume = ["paused", "error", "completed_with_errors"].includes(
    job.state,
  );
  const label = running
    ? job.state === "queued"
      ? "Metadata queued"
      : "Updating metadata…"
    : job.state === "paused"
      ? "Resume metadata"
      : resume
        ? "Retry metadata"
        : "↻ Update metadata";
  return `<button class="button secondary" data-action="${resume ? "metadata-resume" : "metadata"}" data-focus="metadata-start" ${running || state.busy.has(p.id) ? "disabled" : ""}>${label}</button>`;
}

function renderMetadataPanel(p) {
  const container = $("#metadata-info");
  if (p.source !== "custom") {
    setMarkup(container, "");
    return;
  }
  const job = p.metadata || {
    state: "idle",
    total: p.total,
    updated: 0,
    failed: 0,
  };
  const running = ["queued", "running"].includes(job.state);
  const checked = job.updated + job.failed;
  const titles = {
    idle: "Video metadata",
    queued: "Metadata update queued",
    running: "Updating video metadata",
    paused: "Metadata update cancelled",
    error: "Metadata update stopped",
    completed: "Metadata updated",
    completed_with_errors: "Metadata update finished with errors",
  };
  const description =
    job.state === "idle"
      ? "Update video titles, channels, thumbnails and archive checks. YouTube lookups use your saved cookie source for videos that need sign-in."
      : `${checked} of ${job.total} checked · ${job.updated} updated${job.failed ? ` · ${job.failed} failed` : ""}. ${running ? "You can leave this page; the update continues." : "Existing details are kept when a lookup fails."}`;
  setMarkup(
    container,
    `<section class="metadata-panel" aria-label="Video metadata">
    <div class="metadata-panel-heading"></div><p role="status"></p>
    <progress class="progress" value="0" aria-label="Metadata lookup progress"></progress><p class="auth-error" hidden></p></section>`,
  );
  const disabled = state.busy.has(p.id) ? "disabled" : "";
  const action = running
    ? `<button class="button secondary small" data-action="metadata-cancel" data-focus="metadata-cancel" ${disabled}>Cancel update</button>`
    : ["paused", "error", "completed_with_errors"].includes(job.state)
      ? `<button class="button quiet small" data-action="metadata" data-focus="metadata-restart" ${disabled}>Update all metadata</button>`
      : "";
  setMarkup(
    container.querySelector(".metadata-panel-heading"),
    `<h3>${escapeHTML(titles[job.state] || "Video metadata")}</h3>${action}`,
  );
  setText(container.querySelector('[role="status"]'), description);
  const progress = container.querySelector("progress");
  setHidden(progress, job.state === "idle");
  if (!progress.hidden) {
    const max = Math.max(1, job.total);
    if (progress.max !== max) progress.max = max;
    if (progress.value !== checked) progress.value = checked;
    setText(progress, `${checked} of ${job.total}`);
  }
  const error = container.querySelector(".auth-error");
  setHidden(error, !job.error);
  setText(error, job.error || "");
}

function originalSource(link) {
  return link.kind === "original" || link.service?.toLowerCase() === "youtube";
}

function archiveLinkCount(v) {
  return v.recovery?.link_count ?? v.recovery?.links?.filter((link) => !originalSource(link)).length ?? 0;
}

function finderButton(v) {
  const count = archiveLinkCount(v);
  let label = "Find archives";
  if (v.archive_pending && !v.recovery) label = "Archive check queued";
  else if (v.recovery?.error) label = "Archive check failed";
  else if (count) label = `Archive links (${count})`;
  else if (v.recovery?.partial || v.recovery?.service_errors?.length) label = "Partial archive results";
  else if (v.recovery) label = "No archive links";
  return `<button class="button secondary small" data-action="find" data-video="${escapeHTML(v.id)}">${escapeHTML(label)}</button>`;
}

function renderVideoRow(row, v) {
  if (!row.firstElementChild) {
    row.className = "video-row";
    row.innerHTML = `<label class="video-choice"><input type="checkbox" data-include-video="${escapeHTML(v.id)}" data-focus="include-${escapeHTML(v.id)}"></label>
      <div class="video-thumb"><div class="art-fallback" aria-hidden="true"></div></div>
      <div class="video-info"><h3></h3><p></p>
        <details hidden><summary>Download details</summary><p></p></details>
        <details class="metadata-error" hidden><summary>Metadata unavailable</summary><p></p></details>
      </div><div class="video-actions"></div>`;
  }
  const checkbox = row.querySelector("[data-include-video]");
  checkbox.setAttribute("aria-label", `Include ${v.title} in downloads`);
  renderArtwork(row.querySelector(".art-fallback"), v.thumbnail, "▷");
  setText(row.querySelector("h3"), v.title);
  setMarkup(
    row.querySelector(".video-info > p"),
    `${escapeHTML(v.channel || v.id)}${v.local_available ? ` · ${bytes(v.size)}` : ""}${!v.in_playlist ? '<span class="removed-label">Removed from playlist · kept locally</span>' : ""}`,
  );
  // Keep expanded errors open as metadata or download progress arrives.
  const details = row.querySelectorAll("details");
  [v.error, v.metadata_error].forEach((message, i) => {
    const disclosure = details[i];
    setHidden(disclosure, !message);
    if (message) setText(disclosure.querySelector("p"), message);
    else if (disclosure.open) disclosure.open = false;
  });
  const status = v.local_available
    ? "downloaded"
    : v.download_skipped ? "skipped" : v.status;
  const id = escapeHTML(v.id);
  setMarkup(
    row.querySelector(".video-actions"),
    `${badge(status)}${v.local_available ? `<button class="button secondary small" data-action="play" data-video="${id}">▷ Play</button>` : ""}${finderButton(v)}<a class="button quiet small" target="_blank" rel="noopener noreferrer" href="https://www.youtube.com/watch?v=${encodeURIComponent(v.id)}" aria-label="Open ${escapeHTML(v.title)} on YouTube (new tab)">YouTube ↗</a>`,
  );
}

function renderDownloadSelection(pageVideos) {
  // The master checkbox represents the full membership even while searching
  // or paging. Only the mounted rows need their native controls updated.
  const pending = state.pendingInclusion;
  const included = (v) =>
    !!v.in_playlist && (pending && (!pending.videoId || pending.videoId === v.id)
      ? pending.checked
      : !v.download_skipped);
  const members = state.videos.filter((v) => v.in_playlist);
  const checked = members.filter(included).length;
  const all = $("#include-all");
  setHidden($("#download-selection"), !members.length);
  all.checked = members.length > 0 && checked === members.length;
  all.indeterminate = checked > 0 && checked < members.length;
  all.disabled = !members.length || !!pending;
  setText($("#included-count"), `${checked} of ${members.length} included`);
  for (const video of pageVideos) {
    const input = renderedItems.get(video.id).node.querySelector("[data-include-video]");
    const selected = included(video);
    const disabled = !video.in_playlist || !!pending;
    if (input.checked !== selected) input.checked = selected;
    if (input.disabled !== disabled) input.disabled = disabled;
  }
}

function renderDetail() {
  const p = state.playlist;
  if (!p) return;
  setHidden($("#nav-count"), true);
  const title = `${p.title} · Shelf`;
  if (document.title !== title) document.title = title;
  setMarkup(
    $("#breadcrumb"),
    '<a href="/">Playlists</a><span>/</span> Local library',
  );
  setText($("#page-title"), p.title);
  setText(
    $("#page-description"),
    p.description ||
      "Your playlist and its local videos, together in one place.",
  );
  setMarkup(
    $("#page-actions"),
    `<div class="heading-actions">${playlistSourceLink(p, "button secondary")}${p.source === "custom" ? metadataButton(p) : `<button class="button secondary" data-action="refresh" data-id="${escapeHTML(p.id)}" ${active(p) || state.busy.has(p.id) ? "disabled" : ""}>↻ Refresh videos</button>`}${downloadButton(p)}</div>`,
  );
  stats([
    ["Playlist videos", p.total],
    ["Videos on disk", p.local_count],
    ["Needs attention", state.videos.filter(videoNeedsAttention).length],
    ["Saved locally", bytes(p.bytes)],
  ]);
  setText($("#collection-title"), "Playlist library");
  setText(
    $("#collection-description"),
    `${waitingForArchives(p) ? "Waiting for archive checks" : labels[p.state] || p.state}${p.current_video ? ` · Current video ${Math.round(p.progress)}%` : ""}${p.skipped ? ` · ${p.skipped} skipped` : ""}${p.archive_pending ? ` · ${p.archive_pending} archive checks remaining` : ""}`,
  );
  const shown = state.videos.filter(
    (v) =>
      `${v.title} ${v.channel} ${v.id}`
        .toLowerCase()
        .includes(state.search.toLowerCase()) &&
      (state.filter === "all" ||
        (state.filter === "downloaded" && v.local_available) ||
        (state.filter === "skipped" && v.in_playlist && v.download_skipped) ||
        (state.filter === "archives" && archiveLinkCount(v) > 0) ||
        (state.filter === "attention" && videoNeedsAttention(v))),
  );
  setMarkup(
    $("#library-info"),
    '<div id="folder-info"></div><div id="metadata-info"></div><div id="page-info"></div><div class="download-selection" id="download-selection" hidden><label><input id="include-all" type="checkbox"> Include all videos for download</label><span id="included-count"></span></div><div id="result-info"></div>',
  );
  setMarkup(
    $("#folder-info"),
    `<div class="folder-location"><span>Folder</span><code>${escapeHTML(p.download_dir)}</code><button class="button secondary small" data-action="folder" ${active(p) ? "disabled" : ""}>Change folder</button></div>`,
  );
  renderMetadataPanel(p);
  // Limit DOM work for large imports while search/filters still cover every video.
  // Clamp the page after polling changes membership or local availability.
  const pageSize = 100;
  state.videoPage = Math.min(
    state.videoPage,
    Math.max(0, Math.ceil(shown.length / pageSize) - 1),
  );
  const start = state.videoPage * pageSize;
  const pageVideos = shown.slice(start, start + pageSize);
  let pagination = "";
  if (shown.length > pageSize) {
    pagination = `<nav class="video-pagination" aria-label="Video pages"><span>Videos ${start + 1}–${Math.min(start + pageSize, shown.length)} of ${shown.length}</span><div class="heading-actions"><button class="button secondary small" data-action="video-page" data-page="${state.videoPage - 1}" ${state.videoPage === 0 ? "disabled" : ""}>← Previous</button><button class="button secondary small" data-action="video-page" data-page="${state.videoPage + 1}" ${start + pageSize >= shown.length ? "disabled" : ""}>Next →</button></div></nav>`;
  }
  setMarkup($("#page-info"), pagination);
  let content = p.error ? `<p class="muted">${escapeHTML(p.error)}</p>` : "";
  if (!p.items_synced_at)
    content +=
      '<div class="empty"><div class="empty-icon">▤</div><h3>Bring the playlist into view.</h3><p>Load its video list to see what is available, then start a download when you are ready.</p><button class="button primary" data-action="refresh">↻ Load videos</button></div>';
  else if (!shown.length)
    content += `<div class="empty"><div class="empty-icon">▤</div><h3>No videos in this view.</h3><p>${p.source === "custom" ? "Try another filter or search." : "Try another filter, or refresh the playlist to check for new videos."}</p></div>`;
  setMarkup($("#result-info"), content);
  const visibleVideos = p.items_synced_at ? pageVideos : [];
  renderItems(visibleVideos, "video-list", renderVideoRow);
  renderDownloadSelection(visibleVideos);
  if ($("#recovery-dialog").open) renderArchiveResults();
}

function render() {
  // Polls may update cards while someone is using the keyboard. Restore the same
  // control's focus when it still exists; search inputs and dialogs are untouched.
  const focused = document.activeElement;
  const focusKey = focused?.dataset.focus;
  if (playlistId) renderDetail();
  else renderPlaylists();
  if (focused && focused !== document.activeElement) {
    const control = focused.isConnected
      ? focused
      : focusKey &&
        document.querySelector(`[data-focus="${CSS.escape(focusKey)}"]`);
    if (control) control.focus({ preventScroll: true });
  }
  const content = $("#content");
  if (content.getAttribute("aria-busy") !== "false")
    content.setAttribute("aria-busy", "false");
}

function updateSelection(visible) {
  setHidden($("#selection-bar"), !state.selected.size);
  setText(
    $("#selection-count"),
    `${state.selected.size} playlist${state.selected.size === 1 ? "" : "s"} selected`,
  );
  const all = $("#select-all");
  if (all) {
    const checked =
      visible.length > 0 && visible.every((p) => state.selected.has(p.id));
    const partial = !checked && visible.some((p) => state.selected.has(p.id));
    if (all.checked !== checked) all.checked = checked;
    if (all.indeterminate !== partial) all.indeterminate = partial;
  }
}

async function fetchLibrary(force = false) {
  try {
    const review = state.archiveVideo ? `&review_video=${encodeURIComponent(state.archiveVideo)}` : "";
    const data = await api(playlistId ? `${apiPath()}?compact_archives=true${review}` : "/api/playlists");
    const signature = JSON.stringify(data);
    if (force || signature !== state.signature) {
      if (playlistId) {
        state.playlist = data.playlist;
        state.videos = data.videos;
      } else state.playlists = data.playlists;
      render();
      state.signature = signature;
    }
    setMarkup($("#connection"), '<span class="dot"></span> Local library');
  } catch (error) {
    setMarkup($("#connection"), "Connection interrupted");
    if (!state.signature) {
      notice(error.message, true);
      setMarkup(
        $("#library-info"),
        '<div class="empty"><h3>Library is unavailable.</h3><p>Check that the local server is running, or return to your playlists.</p><a class="button secondary" href="/">Back to playlists</a></div>',
      );
    }
    return false;
  }
}

function setup() {
  const s = state.status;
  const check = (name, ok, yes, no) =>
    `<div class="setup-check"><span>${name}</span><strong class="${ok ? "" : "warning"}">${ok ? yes : no}</strong></div>`;
  $("#setup-content").innerHTML =
    `<section class="auth-card" aria-label="Google connection"><h3>Your YouTube account</h3><p>Connect with Google to see your playlists. Sign-in opens in your browser and requests read-only YouTube access.</p><div id="auth-controls" aria-live="polite"></div><p id="auth-error" class="auth-error" role="alert" hidden></p><details id="auth-file-options" ${state.login?.configured ? "" : "open"}><summary>Google OAuth client file</summary><p id="auth-config-status"></p><label class="auth-file-label" for="auth-client-file">Choose Desktop app client JSON</label><input id="auth-client-file" type="file" accept=".json,application/json"><p class="muted">Use the OAuth client JSON from your Google Cloud project. The file stays on this computer.</p></details></section><section class="cookie-card" aria-label="Download cookies"><h3>Private or restricted videos</h3><p>Google supplies your playlist list. For videos that need sign-in, choose where yt-dlp gets your YouTube cookies.</p><div id="cookie-controls"></div></section>${s ? check("ffmpeg", s.ffmpeg, "Ready", "Install required") + check("JavaScript runtime", s.javascript, "Ready", "Install Node or Deno") : "<p>Server status is unavailable.</p>"}`;
  renderLogin();
  // Cookie controls are loaded once on opening. OAuth polling must not reset an
  // unsaved browser choice, optional profile, or selected local file.
  refreshCookies();
  updateDefaultFolder();
  if (!$("#setup-dialog").open) $("#setup-dialog").showModal();
  refreshLogin();
}

// Load the folder on each open so changes made in another tab are visible.
// The static form survives sign-in polling and closing/reopening during a save.
let defaultFolderBusy = false;
async function updateDefaultFolder(action = "load") {
  if (defaultFolderBusy) return;
  defaultFolderBusy = true;
  const fields = $("#default-folder-fields");
  const path = $("#default-folder-path");
  const feedback = $("#default-folder-feedback");
  fields.disabled = true;
  feedback.className = "";
  feedback.textContent = action === "load" ? "Loading folder…" : "Saving folder…";
  const endpoint = "/api/download-directory";
  try {
    let result;
    if (action === "save") {
      result = await api(endpoint, "POST", { directory: path.value.trim() });
    } else if (action === "reset") {
      result = await api(endpoint + "/reset", "POST");
    } else {
      result = await api(endpoint);
    }
    path.value = result.download_dir;
    $("#default-folder-space").textContent = result.free_bytes == null
      ? "Free space unavailable. Check that the drive is connected."
      : `${bytes(result.free_bytes)} available in the saved folder.`;
    $("#default-folder-reset").hidden = !result.download_dir_overridden;
    feedback.className = "auth-success";
    feedback.textContent = "";
    if (action !== "load") {
      const count = result.updated_playlists;
      feedback.textContent = count
        ? `Default download folder saved. Updated ${count} playlist${count === 1 ? "" : "s"}.`
        : "Default download folder saved.";
      await reload(true);
    }
  } catch (error) {
    feedback.className = "auth-error";
    feedback.textContent = error.message;
  } finally {
    fields.disabled = false;
    defaultFolderBusy = false;
  }
}

$("#default-folder-form").addEventListener("submit", (event) => {
  event.preventDefault();
  updateDefaultFolder("save");
});
$("#default-folder-reset").addEventListener("click", () => updateDefaultFolder("reset"));
$("#default-folder-path").addEventListener("input", () => {
  $("#default-folder-feedback").textContent = "";
});

const browserNames = {
  firefox: "Firefox",
  safari: "Safari",
  chrome: "Chrome",
  brave: "Brave",
  chromium: "Chromium",
  edge: "Edge",
  opera: "Opera",
  vivaldi: "Vivaldi",
  whale: "Whale",
};

function cookieLabel(settings) {
  if (settings.mode === "browser")
    return `${browserNames[settings.browser] || settings.browser}${settings.profile ? ` · ${settings.profile}` : ""}${settings.container ? ` · Container: ${settings.container}` : ""}`;
  if (settings.mode === "file") return `File: ${settings.filename}`;
  if (settings.mode === "environment") return settings.environment_label;
  return "No cookies";
}

async function refreshCookies() {
  const container = $("#cookie-controls");
  container.textContent = "Loading cookie settings…";
  try {
    state.cookies = await api("/api/cookies");
    renderCookies();
  } catch {
    container.innerHTML =
      '<p role="alert">Could not load cookie settings. Close and reopen Account & setup to retry.</p>';
  }
}

function renderCookies() {
  const c = state.cookies;
  $("#cookie-controls").innerHTML = `
    <p id="cookie-current" class="cookie-current">Current source: ${escapeHTML(cookieLabel(c))}</p>
    <form id="cookie-form">
      <fieldset id="cookie-fields">
        <label for="cookie-source">Cookie source</label>
        <select id="cookie-source">
          <option value="browser">Use a browser</option>
          <option value="file">Upload cookies.txt</option>
          <option value="none">No cookies</option>
          <option value="environment">Use startup defaults</option>
        </select>
        <div id="cookie-browser-fields">
          <label for="cookie-browser">Browser signed in to YouTube</label>
          <select id="cookie-browser">${c.browsers.map((b) => `<option value="${escapeHTML(b)}">${escapeHTML(browserNames[b] || b)}</option>`).join("")}</select>
          <p id="cookie-browser-help" class="muted"></p>
          <details id="cookie-advanced"><summary>Profile & advanced options</summary>
            <label id="cookie-profile-label" for="cookie-profile">Profile name or path (optional)</label>
            <input id="cookie-profile" type="text" maxlength="4096" autocomplete="off" spellcheck="false" placeholder="Automatic" value="${escapeHTML(c.profile)}">
            <div id="cookie-container-field"><label for="cookie-container">Firefox container (optional)</label>
              <input id="cookie-container" type="text" maxlength="200" autocomplete="off" placeholder="All containers" value="${escapeHTML(c.container)}">
              <p class="muted">Enter a container name, or “none” for cookies outside containers.</p>
            </div>
          </details>
        </div>
        <div id="cookie-file-fields" hidden>
          <label for="cookie-file">Choose Netscape cookies.txt</label>
          <input id="cookie-file" type="file" accept=".txt,text/plain">
          <p class="muted">Choose an export from a browser signed in to YouTube, up to 5 MB. Shelf stores a private copy on this computer; your original file stays unchanged.</p>
        </div>
        <p id="cookie-none-help" class="muted" hidden>Downloads will run without cookies. Videos that require sign-in may fail.</p>
        <p id="cookie-environment-help" class="muted" hidden>Startup defaults: ${escapeHTML(c.environment_label)}. This removes your saved override.</p>
        <button class="button primary" id="cookie-save" type="submit">Save cookie source</button>
      </fieldset>
      <p id="cookie-feedback" role="status" aria-live="polite" hidden></p>
    </form>`;
  $("#cookie-source").value = c.mode;
  $("#cookie-browser").value = c.browser;
  $("#cookie-advanced").open = Boolean(c.profile || c.container);
  updateCookieFields();
}

function updateCookieFields() {
  const mode = $("#cookie-source").value;
  const browser = $("#cookie-browser").value;
  $("#cookie-browser-fields").hidden = mode !== "browser";
  $("#cookie-file-fields").hidden = mode !== "file";
  $("#cookie-none-help").hidden = mode !== "none";
  $("#cookie-environment-help").hidden = mode !== "environment";
  $("#cookie-container-field").hidden = browser !== "firefox";
  $("#cookie-profile-label").textContent =
    browser === "safari"
      ? "Cookie database path (optional)"
      : "Profile name or path (optional)";
  $("#cookie-browser-help").textContent =
    browser === "safari"
      ? "Safari works on macOS. If access is denied, allow Full Disk Access for the app or terminal running Shelf in System Settings → Privacy & Security. Leave the database path blank for automatic detection."
      : browser === "firefox"
        ? "Uses your most recently used Firefox profile by default. You can choose a profile or container below."
        : "Uses your most recently used profile by default. Your operating system may ask for keychain access when a download starts.";
  $("#cookie-feedback").hidden = true;
}

async function saveCookies() {
  if (state.cookieBusy) return;
  state.cookieBusy = true;
  $("#cookie-fields").disabled = true;
  $("#cookie-save").textContent = "Saving…";
  const feedback = $("#cookie-feedback");
  feedback.hidden = true;
  try {
    const mode = $("#cookie-source").value;
    let result;
    if (mode === "file") {
      const file = $("#cookie-file").files[0];
      if (!file && state.cookies.mode !== "file")
        throw new Error("Choose a cookies.txt file first.");
      if (file) {
        if (file.size > 5 * 1024 * 1024)
          throw new Error("Choose a cookies.txt file smaller than 5 MB.");
        // Send the file as a bounded raw body, never in a URL, localStorage, or log.
        const response = await fetch(
          `/api/cookies/file?filename=${encodeURIComponent(file.name)}`,
          {
            method: "POST",
            headers: { "X-Local-App": "1", "Content-Type": "text/plain" },
            body: file,
          },
        );
        result = await response.json();
        if (!response.ok)
          throw new Error(result.detail || "Could not save the cookies file.");
      } else result = state.cookies;
    } else {
      const browser = $("#cookie-browser").value;
      result = await api("/api/cookies", "POST", {
        mode,
        browser,
        profile: $("#cookie-profile").value.trim(),
        container:
          browser === "firefox" ? $("#cookie-container").value.trim() : "",
      });
    }
    state.cookies = result;
    if (state.status) {
      state.status.cookies = result.active;
      state.status.cookie_settings = result;
    }
    $("#cookie-current").textContent = `Current source: ${cookieLabel(result)}`;
    $("#cookie-file").value = "";
    feedback.className = "auth-success";
    feedback.textContent =
      "Saved. Applies to the next video, without restarting." +
      (result.mode === "browser"
        ? " Browser access is checked when a download starts."
        : "");
  } catch (error) {
    feedback.className = "auth-error";
    feedback.textContent = error.message;
  } finally {
    state.cookieBusy = false;
    $("#cookie-fields").disabled = false;
    $("#cookie-save").textContent = "Save cookie source";
    feedback.hidden = false;
  }
}

function renderLogin() {
  const container = $("#auth-controls");
  if (!container) return;
  const login = state.login;
  const pending = ["waiting", "exchanging"].includes(login?.phase);
  const connected = login?.phase === "connected";
  const configured = login?.configured;
  const label = state.status?.account.authenticated
    ? "Reconnect with Google"
    : "Connect with Google";
  const focusedAction = document.activeElement?.dataset.action;
  container.innerHTML = `<p class="auth-message ${connected ? "auth-success" : ""}">${escapeHTML(login?.message || "Checking your Google connection…")}</p><div class="auth-actions">${pending ? `<button class="button primary" disabled>${login.phase === "exchanging" ? "Finishing connection…" : "Waiting for Google…"}</button><button class="button secondary" data-action="auth-cancel" ${state.loginBusy ? "disabled" : ""}>Cancel sign-in</button>` : `${connected ? '<button class="button primary" data-action="sync">↻ Sync playlists</button>' : ""}<button class="button ${connected ? "secondary" : "primary"}" data-action="auth-start" ${!configured || state.loginBusy ? "disabled" : ""}>${state.loginBusy ? "Opening Google…" : label}</button>`}</div>${pending && safeURL(login.authorization_url) ? `<p class="muted">Browser didn't open? <a class="external-link" href="${safeURL(login.authorization_url)}" target="_blank" rel="noopener noreferrer">Open Google sign-in ↗</a></p>` : ""}`;
  $("#auth-config-status").textContent = configured
    ? "A Google client file is available. You can use it or choose another."
    : "Choose your Google Desktop app client JSON to enable sign-in.";
  $("#auth-client-file").disabled = pending || state.loginBusy;
  if (focusedAction)
    container
      .querySelector(`[data-action="${CSS.escape(focusedAction)}"]`)
      ?.focus({ preventScroll: true });
}

function loginError(message) {
  if (!$("#auth-error")) return;
  $("#auth-error").textContent = message;
  $("#auth-error").hidden = false;
}

async function fetchLogin() {
  if (state.loginBusy) return;
  const revision = state.loginRevision;
  try {
    const previous = state.login?.phase;
    const login = await api("/api/auth/status");
    // A slow status read must not replace a newer start/cancel/config result,
    // which also decides whether the sign-in polling timer should keep running.
    if (revision !== state.loginRevision) return;
    const changed =
      !state.login ||
      ["phase", "message", "configured", "authorization_url"].some(
        (key) => state.login[key] !== login[key],
      );
    state.login = login;
    if (previous !== "connected" && login.phase === "connected") {
      const status = await api("/api/status");
      if (revision !== state.loginRevision) return;
      state.status = status;
      notice("Google connected. Sync your playlists to continue.");
    }
    if (changed) renderLogin();
  } catch (error) {
    if (revision !== state.loginRevision) return;
    loginError(
      "Could not check sign-in progress. Check that Shelf is still running.",
    );
    return false;
  }
}

async function loginAction(action) {
  if (state.loginBusy) return;
  state.loginBusy = true;
  state.loginRevision += 1;
  $("#auth-error").hidden = true;
  renderLogin();
  try {
    state.login = await api(
      `/api/auth/${action === "auth-start" ? "start" : "cancel"}`,
      "POST",
    );
  } catch (error) {
    loginError(error.message);
  } finally {
    state.loginBusy = false;
    renderLogin();
    loginPoller.schedule();
  }
}

async function chooseGoogleClient(input) {
  const file = input.files[0];
  if (!file) return;
  state.loginBusy = true;
  state.loginRevision += 1;
  $("#auth-error").hidden = true;
  renderLogin();
  try {
    if (file.size > 64 * 1024)
      throw new Error("Choose an OAuth client JSON smaller than 64 KB.");
    let config;
    try {
      config = JSON.parse(await file.text());
    } catch {
      throw new Error("The selected file is not valid JSON.");
    }
    state.login = await api("/api/auth/config", "POST", config);
    $("#auth-file-options").open = false;
  } catch (error) {
    loginError(error.message);
  } finally {
    state.loginBusy = false;
    input.value = "";
    renderLogin();
    loginPoller.schedule();
  }
}

function archiveLink(link) {
  const url = safeURL(link.url);
  if (!url) return "";
  const kinds = { video: "Video", audio: "Audio", metadata: "Metadata / other" };
  const kind = originalSource(link) ? "Original YouTube" : kinds[link.kind] || "Archive";
  const contents = (link.contents || []).map((key) => key.replaceAll("_", " ")).join(", ");
  return `<a class="recovery-link" href="${url}" target="_blank" rel="noopener noreferrer">
    <span class="badge">${escapeHTML(kind)}</span>
    <strong>${escapeHTML(link.service)} · ${escapeHTML(link.title)} ↗</strong>
    <small>${escapeHTML(link.url)}</small>
    ${contents ? `<small>Reported contents · ${escapeHTML(contents)}</small>` : ""}
    ${link.note ? `<small>${escapeHTML(link.note)}</small>` : ""}
  </a>`;
}

function renderArchiveResults() {
  const video = state.videos.find((v) => v.id === state.archiveVideo);
  if (!video) return;
  setText($("#recovery-title"), video.title);
  const result = video.recovery;
  if (result && !result.links) {
    setMarkup($("#recovery-content"), '<p role="status">Loading saved links…</p>');
    return;
  }
  const errors = result?.service_errors || [];
  const links = result?.links || [];
  const pending = video.archive_pending;
  const checked = video.recovery_checked_at
    ? `Last checked ${new Date(video.recovery_checked_at).toLocaleString()}` : "";
  setMarkup($("#recovery-content"), `
    ${pending ? '<p role="status">Archive check queued. Results update here.</p>' : ""}
    ${checked ? `<p class="muted">${escapeHTML(checked)}</p>` : ""}
    ${result?.verdict && !result.error ? `<p>${escapeHTML(result.verdict)}</p>` : ""}
    ${result?.error ? `<p role="alert">${escapeHTML(result.error)}${links.length ? " Showing previously saved links." : ""}</p>` : ""}
    ${result && !result.error && !links.length ? "<p>Finder returned no links.</p>" : ""}
    ${links.map(archiveLink).join("")}
    ${errors.length ? `<p class="muted">Services that could not be checked · ${escapeHTML(errors.join(", "))}</p>` : ""}
    ${!pending ? `<button class="button secondary small" data-action="recheck-archive" data-video="${escapeHTML(video.id)}">${result ? "Recheck archives" : "Check archives"}</button>` : ""}
    <a class="external-link" href="https://findyoutubevideo.thetechrobo.ca/?q=${encodeURIComponent(video.id)}" target="_blank" rel="noopener noreferrer">Open YouTube Video Finder ↗</a>`);
}

async function queueArchiveCheck(button) {
  button.disabled = true;
  try {
    await api(`${apiPath()}/videos/${encodeURIComponent(button.dataset.video)}/find?queue=true`, "POST");
    await reload(true);
  } catch (error) {
    notice(error.message, true);
  } finally {
    button.disabled = false;
  }
}

document.addEventListener("click", async (event) => {
  const close = event.target.closest("[data-close]");
  if (close) {
    close.closest("dialog").close();
    return;
  }
  const button = event.target.closest("[data-action]");
  if (!button || button.disabled) return;
  const action = button.dataset.action;
  const id = button.dataset.id || playlistId;
  if (action === "download-settings") {
    openDownloadSettings();
    return;
  }
  if (action === "download-cookies") {
    $("#download-settings-dialog").close();
    setup();
    return;
  }
  if (action === "video-page") {
    state.videoPage = Math.max(0, Number(button.dataset.page));
    render();
    return;
  }
  if (action === "create-playlist") {
    $("#create-error").hidden = true;
    $("#create-dialog").showModal();
    $("#create-name").focus();
    return;
  }
  if (action === "setup") {
    setup();
    return;
  }
  if (action === "auth-start" || action === "auth-cancel") {
    await loginAction(action);
    return;
  }
  if (action === "filter") {
    state.filter = button.dataset.filter;
    state.videoPage = 0;
    renderFilters();
    render();
    return;
  }
  if (action === "clear-selection") {
    state.selected.clear();
    render();
    return;
  }
  if (action === "folder") {
    $("#folder-path").value = state.playlist.download_dir;
    $("#folder-error").hidden = true;
    $("#folder-dialog").showModal();
    return;
  }
  if (action === "play") {
    const video = state.videos.find((v) => v.id === button.dataset.video);
    $("#player-title").textContent = video.title;
    $("#player").src = `${apiPath()}/videos/${video.id}/file`;
    $("#player-download").href = $("#player").src;
    $("#player-dialog").showModal();
    return;
  }
  if (action === "find") {
    state.archiveVideo = button.dataset.video;
    renderArchiveResults();
    $("#recovery-dialog").showModal();
    const video = state.videos.find((v) => v.id === state.archiveVideo);
    if (!video.recovery && !video.archive_pending) await queueArchiveCheck(button);
    else if (video.recovery && !video.recovery.links) {
      await reload(true);
      if (state.archiveVideo === video.id && libraryPoller.failed)
        setMarkup($("#recovery-content"), "<p>Could not load saved links. Check the connection to Shelf and reopen the results.</p>");
    }
    return;
  }
  if (action === "recheck-archive") {
    await queueArchiveCheck(button);
    return;
  }
  button.disabled = true;
  if (id) state.busy.add(id);
  try {
    if (action === "sync") {
      if ($("#setup-dialog").open) $("#setup-dialog").close();
      await api("/api/sync", "POST");
      notice(
        "Playlists synced. Open a library to see its videos, or select playlists to download.",
      );
    }
    if (action === "refresh") {
      await api(`${apiPath(id)}/refresh`, "POST");
      notice("Video list refreshed. Existing local files have been kept.");
    }
    if (["metadata", "metadata-resume", "metadata-cancel"].includes(action)) {
      const suffix =
        action === "metadata" ? "" : `/${action.slice("metadata-".length)}`;
      await api(`${apiPath(id)}/metadata${suffix}`, "POST");
      notice(
        action === "metadata-cancel"
          ? "Metadata update cancelled. Saved progress is kept for resume."
          : "Metadata update queued. Progress appears in the library; no media download is started.",
      );
    }
    if (action === "download" || action === "pause") {
      await api(`${apiPath(id)}/${action}`, "POST");
      notice(
        action === "pause"
          ? "Playlist paused. Partial downloads are kept for resume."
          : "Playlist queued. Downloads continue while the local app is running.",
      );
    }
    if (action === "download-selected") {
      const ids = [...state.selected];
      const failures = [];
      for (const selected of ids) {
        try {
          await api(`${apiPath(selected)}/download`, "POST");
          state.selected.delete(selected);
        } catch (error) {
          failures.push(
            `${state.playlists.find((p) => p.id === selected)?.title}: ${error.message}`,
          );
        }
      }
      notice(
        failures.length
          ? `${ids.length - failures.length} queued. ${failures.join(" ")}`
          : `${ids.length} playlists queued.`,
        failures.length > 0,
      );
    }
  } catch (error) {
    notice(error.message, true);
    if (error.status === 401) setup();
  } finally {
    if (id) state.busy.delete(id);
    button.disabled = false;
    await reload(true);
  }
});

async function saveDownloadSelection(input) {
  const { checked } = input;
  const videoId = input.dataset.includeVideo;
  const focused = document.activeElement === input;
  state.pendingInclusion = { videoId, checked };
  render();
  try {
    const suffix = videoId ? `/${encodeURIComponent(videoId)}` : "";
    await api(`${apiPath()}/videos${suffix}/skip`, "POST", { skipped: !checked });
  } catch (error) {
    notice(error.message, true);
  } finally {
    await reload(true);
    state.pendingInclusion = null;
    render();
    if (focused && input.isConnected && document.activeElement === document.body)
      input.focus({ preventScroll: true });
  }
}

$("#recovery-dialog").addEventListener("close", () => { state.archiveVideo = null; });

document.addEventListener("change", (event) => {
  const input = event.target;
  if (input.id === "include-all" || input.matches("[data-include-video]")) {
    saveDownloadSelection(input);
    return;
  }
  if (input.id === "cookie-source" || input.id === "cookie-browser") {
    // Profiles belong to a browser; a Firefox profile is not a Safari database.
    if (input.id === "cookie-browser") {
      $("#cookie-profile").value = "";
      $("#cookie-container").value = "";
    }
    updateCookieFields();
    return;
  }
  if (input.id === "auth-client-file") {
    chooseGoogleClient(input);
    return;
  }
  if (input.matches("[data-select]")) {
    if (input.checked) state.selected.add(input.dataset.select);
    else state.selected.delete(input.dataset.select);
    render();
  }
  if (input.id === "select-all") {
    for (const p of visiblePlaylists()) {
      if (input.checked) state.selected.add(p.id);
      else state.selected.delete(p.id);
    }
    render();
  }
});
document.addEventListener("submit", (event) => {
  if (event.target.id === "cookie-form") {
    event.preventDefault();
    saveCookies();
  }
});
$("#search").addEventListener("input", (event) => {
  state.search = event.target.value;
  state.videoPage = 0;
  render();
});
$("#player-dialog").addEventListener("close", () => {
  $("#player").pause();
  $("#player").removeAttribute("src");
  $("#player").load();
});
$("#folder-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const button = event.submitter;
  button.disabled = true;
  try {
    const data = await api(`${apiPath()}/folder`, "POST", {
      directory: $("#folder-path").value.trim(),
    });
    $("#folder-dialog").close();
    notice(
      `Folder updated. Found ${data.playlist.local_count} local videos. Missing videos can be downloaded again with Resume / retry.`,
    );
    await reload(true);
  } catch (error) {
    $("#folder-error").textContent = error.message;
    $("#folder-error").hidden = false;
  } finally {
    button.disabled = false;
  }
});
$("#create-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  if (state.creatingPlaylist) return;
  state.creatingPlaylist = true;
  $("#create-fields").disabled = true;
  $("#create-submit").textContent = "Creating…";
  $("#create-error").hidden = true;
  try {
    // Persist the whole validated list before navigating. Validation failures
    // leave the draft intact, including line breaks used by server error messages.
    if ($("#create-urls").value.length > 2_000_000) {
      throw new Error(
        "This paste exceeds 2 million characters. Shorten tracking parameters or split it into smaller playlists.",
      );
    }
    const data = await api("/api/playlists/custom", "POST", {
      title: $("#create-name").value.trim(),
      urls: $("#create-urls").value,
    });
    location.assign(`/playlists/${encodeURIComponent(data.playlist.id)}`);
  } catch (error) {
    $("#create-error").textContent = error.message;
    $("#create-error").hidden = false;
  } finally {
    state.creatingPlaylist = false;
    $("#create-fields").disabled = false;
    $("#create-submit").textContent = "Create playlist";
  }
});
// Broken remote thumbnails should fall back to the local artwork, never leave a
// browser broken-image icon over the library. No inline event handlers are needed.
document.addEventListener(
  "error",
  (event) => {
    if (event.target.tagName === "IMG") event.target.remove();
  },
  true,
);

async function initialize() {
  document
    .querySelector(
      `[data-nav="${["downloaded", "attention"].includes(view) && !playlistId ? view : "all"}"]`,
    )
    .classList.add("active");
  if (playlistId) {
    $("#page-description").classList.add("detail-description");
    $("#search").placeholder = "Search videos…";
    $("#search").setAttribute("aria-label", "Search videos");
  } else if (view !== "all") {
    $("#collection-title").textContent =
      view === "downloaded"
        ? "Downloaded playlists"
        : "Playlists needing attention";
  }
  renderFilters();
  await Promise.all([
    reload(),
    api("/api/status")
      .then((s) => {
        state.status = s;
        state.login = s.login;
      })
      .catch(() => {}),
  ]);
  const authResult = new URLSearchParams(location.search).get("auth");
  if (authResult) {
    history.replaceState(null, "", location.pathname);
    setup();
    if (authResult === "invalid")
      loginError(
        "This sign-in link is invalid or expired. Start a new connection here.",
      );
  }
  loginPoller.schedule();
}
initialize();
