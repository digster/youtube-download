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
  polling: false,
  login: null,
  loginPolling: false,
  loginBusy: false,
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
const needsAttention = (p) =>
  p.failed ||
  p.unavailable ||
  ["error", "completed_with_errors", "paused"].includes(p.state) ||
  (p.items_synced_at && p.state === "completed" && p.downloaded < p.total);
const apiPath = (id = playlistId) => `/api/playlists/${encodeURIComponent(id)}`;

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

function stats(items) {
  $("#stats").innerHTML = items
    .map(
      ([label, value, suffix]) =>
        `<div class="stat"><label>${escapeHTML(label)}</label><strong>${escapeHTML(value)}</strong>${suffix ? `<small>${escapeHTML(suffix)}</small>` : ""}</div>`,
    )
    .join("");
}

function downloadButton(p, small = false) {
  const action = active(p) ? "pause" : "download";
  const text = active(p)
    ? "Ⅱ Pause"
    : p.state === "paused"
      ? "↓ Resume"
      : p.state === "completed" && p.downloaded === p.total
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
      (!p.items_synced_at || p.downloaded !== p.total)
    )
      return false;
    return `${p.title} ${p.channel}`
      .toLowerCase()
      .includes(state.search.toLowerCase());
  });
}

function renderPlaylists() {
  const playlists = state.playlists;
  $("#nav-count").textContent = playlists.length;
  stats([
    ["Playlists", playlists.length],
    ["Videos on disk", playlists.reduce((s, p) => s + p.local_count, 0)],
    ["In progress", playlists.filter(active).length],
    ["Library size", bytes(playlists.reduce((s, p) => s + p.bytes, 0))],
  ]);
  const shown = visiblePlaylists();
  if (!playlists.length) {
    $("#content").innerHTML =
      `<div class="empty"><div class="empty-icon">▤</div><h3>A home for your playlists.</h3><p>Sync your YouTube account to choose the playlists you want to keep. Downloads and progress will appear here.</p><button class="button primary" data-action="sync">↻ Sync playlists</button> <button class="button quiet" data-action="setup">Account setup ↗</button></div>`;
  } else if (!shown.length) {
    $("#content").innerHTML =
      '<div class="empty"><div class="empty-icon">⌕</div><h3>Nothing here just yet.</h3><p>No playlists match this view. Try a different filter or search.</p></div>';
  } else {
    $("#content").innerHTML = `<div class="grid">${shown
      .map((p) => {
        const percent = p.total
          ? Math.min(
              100,
              Math.round(
                ((p.downloaded + (p.current_video ? p.progress / 100 : 0)) /
                  p.total) *
                  100,
              ),
            )
          : 0;
        return `<article class="card ${state.selected.has(p.id) ? "selected" : ""}">
        <div class="card-top"><div class="art-fallback" aria-hidden="true">▤</div>${safeURL(p.thumbnail) ? `<img src="${safeURL(p.thumbnail)}" alt="" loading="lazy" referrerpolicy="no-referrer">` : ""}<label class="card-check"><input type="checkbox" data-select="${escapeHTML(p.id)}" data-focus="select-${escapeHTML(p.id)}" aria-label="Select ${escapeHTML(p.title)}" ${state.selected.has(p.id) ? "checked" : ""}></label><span class="video-count">▤ &nbsp;${p.total} videos</span></div>
        <div class="card-body"><a class="card-title" href="/playlists/${encodeURIComponent(p.id)}" title="${escapeHTML(p.title)}">${escapeHTML(p.title)}</a><div class="card-meta"><span>${escapeHTML(p.channel || "Your YouTube account")}</span><span>${escapeHTML(p.privacy || "Playlist")}</span></div><div class="status-line">${badge(p.state)}<span>${p.downloaded} of ${p.total} saved</span></div><progress class="progress" max="100" value="${percent}" aria-label="${escapeHTML(p.title)} download progress">${percent}%</progress><div class="card-footer"><a class="library-link" href="/playlists/${encodeURIComponent(p.id)}">View library <span>↗</span></a>${downloadButton(p, true)}</div></div>
      </article>`;
      })
      .join("")}</div>`;
  }
  updateSelection();
}

function renderDetail() {
  const p = state.playlist;
  if (!p) return;
  $("#nav-count").hidden = true;
  document.title = `${p.title} · Shelf`;
  $("#breadcrumb").innerHTML =
    `<a href="/">Playlists</a><span>/</span> Local library`;
  $("#page-title").textContent = p.title;
  $("#page-description").textContent =
    p.description ||
    "Your playlist and its local videos, together in one place.";
  $("#page-description").className = "detail-description";
  $("#page-actions").innerHTML =
    `<div class="heading-actions"><button class="button secondary" data-action="refresh" data-id="${escapeHTML(p.id)}" ${active(p) || state.busy.has(p.id) ? "disabled" : ""}>↻ Refresh videos</button>${downloadButton(p)}</div>`;
  stats([
    ["Playlist videos", p.total],
    ["Videos on disk", p.local_count],
    [
      "Needs attention",
      state.videos.filter(
        (v) =>
          !v.local_available &&
          ["failed", "unavailable", "missing"].includes(v.status),
      ).length,
    ],
    ["Saved locally", bytes(p.bytes)],
  ]);
  $("#collection-title").textContent = "Playlist library";
  $("#collection-description").textContent =
    `${labels[p.state] || p.state}${p.current_video ? ` · Current video ${Math.round(p.progress)}%` : ""}`;
  const shown = state.videos.filter(
    (v) =>
      `${v.title} ${v.channel} ${v.id}`
        .toLowerCase()
        .includes(state.search.toLowerCase()) &&
      (state.filter === "all" ||
        (state.filter === "downloaded"
          ? v.local_available
          : ["failed", "unavailable", "missing"].includes(v.status) &&
            !v.local_available)),
  );
  let content = `<div class="folder-location"><span>Folder</span><code>${escapeHTML(p.download_dir)}</code><button class="button secondary small" data-action="folder" ${active(p) ? "disabled" : ""}>Change folder</button></div>`;
  if (p.error) content += `<p class="muted">${escapeHTML(p.error)}</p>`;
  if (!p.items_synced_at)
    content +=
      '<div class="empty"><div class="empty-icon">▤</div><h3>Bring the playlist into view.</h3><p>Load its video list to see what is available, then start a download when you are ready.</p><button class="button primary" data-action="refresh">↻ Load videos</button></div>';
  else if (!shown.length)
    content +=
      '<div class="empty"><div class="empty-icon">▤</div><h3>No videos in this view.</h3><p>Try another filter, or refresh the playlist to check for new videos.</p></div>';
  else
    content += `<div class="video-list">${shown.map((v) => `<article class="video-row"><div class="video-thumb"><div class="art-fallback" aria-hidden="true">▷</div>${safeURL(v.thumbnail) ? `<img src="${safeURL(v.thumbnail)}" alt="" loading="lazy" referrerpolicy="no-referrer">` : ""}</div><div class="video-info"><h3>${escapeHTML(v.title)}</h3><p>${escapeHTML(v.channel || v.id)}${v.local_available ? ` · ${bytes(v.size)}` : ""}${!v.in_playlist ? '<span class="removed-label">Removed from playlist · kept locally</span>' : ""}</p>${v.error ? `<details><summary>Download details</summary><p>${escapeHTML(v.error)}</p></details>` : ""}</div><div class="video-actions">${badge(v.local_available ? "downloaded" : v.status)}${v.local_available ? `<button class="button secondary small" data-action="play" data-video="${escapeHTML(v.id)}">▷ Play</button>` : ["unavailable", "failed"].includes(v.status) ? `<button class="button secondary small" data-action="find" data-video="${escapeHTML(v.id)}">${v.recovery ? "Archive links ↗" : "Find archives ↗"}</button>` : `<a class="button quiet small" target="_blank" rel="noopener noreferrer" href="https://www.youtube.com/watch?v=${encodeURIComponent(v.id)}">YouTube ↗</a>`}</div></article>`).join("")}</div>`;
  $("#content").innerHTML = content;
}

function render() {
  // Polls may update cards while someone is using the keyboard. Restore the same
  // control's focus when it still exists; search inputs and dialogs are untouched.
  const focused = document.activeElement?.dataset.focus;
  if (playlistId) renderDetail();
  else renderPlaylists();
  if (focused)
    document
      .querySelector(`[data-focus="${CSS.escape(focused)}"]`)
      ?.focus({ preventScroll: true });
  $("#content").setAttribute("aria-busy", "false");
}

function updateSelection() {
  $("#selection-bar").hidden = !state.selected.size;
  $("#selection-count").textContent =
    `${state.selected.size} playlist${state.selected.size === 1 ? "" : "s"} selected`;
  const visible = visiblePlaylists();
  const all = $("#select-all");
  if (all) {
    all.checked =
      visible.length > 0 && visible.every((p) => state.selected.has(p.id));
    all.indeterminate =
      !all.checked && visible.some((p) => state.selected.has(p.id));
  }
}

async function reload(force = false) {
  if (state.polling) return;
  state.polling = true;
  try {
    const data = await api(playlistId ? apiPath() : "/api/playlists");
    const signature = JSON.stringify(data);
    if (force || signature !== state.signature) {
      state.signature = signature;
      if (playlistId) {
        state.playlist = data.playlist;
        state.videos = data.videos;
      } else state.playlists = data.playlists;
      render();
    }
    $("#connection").innerHTML = '<span class="dot"></span> Local library';
  } catch (error) {
    $("#connection").textContent = "Connection interrupted";
    if (!state.signature) {
      notice(error.message, true);
      $("#content").innerHTML =
        '<div class="empty"><h3>Library is unavailable.</h3><p>Check that the local server is running, or return to your playlists.</p><a class="button secondary" href="/">Back to playlists</a></div>';
    }
  } finally {
    state.polling = false;
  }
}

function setup() {
  const s = state.status;
  const check = (name, ok, yes, no) =>
    `<div class="setup-check"><span>${name}</span><strong class="${ok ? "" : "warning"}">${ok ? yes : no}</strong></div>`;
  $("#setup-content").innerHTML =
    `<section class="auth-card" aria-label="Google connection"><h3>Your YouTube account</h3><p>Connect with Google to see your playlists. Sign-in opens in your browser and requests read-only YouTube access.</p><div id="auth-controls" aria-live="polite"></div><p id="auth-error" class="auth-error" role="alert" hidden></p><details id="auth-file-options" ${state.login?.configured ? "" : "open"}><summary>Google OAuth client file</summary><p id="auth-config-status"></p><label class="auth-file-label" for="auth-client-file">Choose Desktop app client JSON</label><input id="auth-client-file" type="file" accept=".json,application/json"><p class="muted">Use the OAuth client JSON from your Google Cloud project. The file stays on this computer.</p></details></section>${s ? check("ffmpeg", s.ffmpeg, "Ready", "Install required") + check("JavaScript runtime", s.javascript, "Ready", "Install Node or Deno") + check("Download cookies", s.cookies, "Configured", "Optional · for restricted videos") : "<p>Server status is unavailable.</p>"}<h3>Where your videos live</h3><code>${escapeHTML(s?.download_dir || "Configured in .env")}</code><p>${s ? `${bytes(s.free_bytes)} available. ` : ""}Change a playlist's folder on its library page after moving its contents. Downloads use video ID subfolders so titles can change safely.</p><h3>Private or restricted videos</h3><p>Google supplies the playlist list. Set <strong>YTDL_COOKIES_BROWSER=chrome</strong> or <strong>YTDL_COOKIES_FILE</strong> in .env for yt-dlp, then restart the app. Use only one cookie source.</p><h3>Dependencies</h3><p>Install ffmpeg and Node or Deno. On macOS with Homebrew:</p><code>brew install ffmpeg node</code><p class="muted">Active playlists resume when the app restarts. Paused playlists stay paused. Watch Later and saved playlists from other channels are not exposed by the account API.</p>`;
  renderLogin();
  if (!$("#setup-dialog").open) $("#setup-dialog").showModal();
  refreshLogin();
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

async function refreshLogin() {
  if (state.loginPolling || state.loginBusy) return;
  state.loginPolling = true;
  try {
    const previous = state.login?.phase;
    const login = await api("/api/auth/status");
    const changed =
      !state.login ||
      ["phase", "message", "configured", "authorization_url"].some(
        (key) => state.login[key] !== login[key],
      );
    state.login = login;
    if (previous !== "connected" && login.phase === "connected") {
      state.status = await api("/api/status");
      notice("Google connected. Sync your playlists to continue.");
    }
    if (changed) renderLogin();
  } catch (error) {
    loginError(
      "Could not check sign-in progress. Check that Shelf is still running.",
    );
  } finally {
    state.loginPolling = false;
  }
}

async function loginAction(action) {
  if (state.loginBusy) return;
  state.loginBusy = true;
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
  }
}

async function chooseGoogleClient(input) {
  const file = input.files[0];
  if (!file) return;
  state.loginBusy = true;
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
  }
}

async function findArchives(videoId) {
  const video = state.videos.find((v) => v.id === videoId);
  $("#recovery-title").textContent = video.title;
  $("#recovery-content").innerHTML =
    '<p>Searching archive services with YouTube Video Finder…</p><p class="muted">This may take up to 30 seconds.</p>';
  $("#recovery-dialog").showModal();
  try {
    const result = await api(`${apiPath()}/videos/${videoId}/find`, "POST");
    $("#recovery-content").innerHTML =
      `<p>${escapeHTML(result.error || result.verdict || "Archive search complete.")}</p>${result.links.map((link) => `<a class="recovery-link" href="${safeURL(link.url)}" target="_blank" rel="noopener noreferrer"><span class="badge">${link.kind === "video" ? "Video archive" : "Metadata / archive"}</span><strong>${escapeHTML(link.title)} ↗</strong><small>${escapeHTML(link.service)}${link.note ? ` · ${escapeHTML(link.note)}` : ""}</small></a>`).join("")}${!result.links.length ? "<p>No archive URLs were returned. Unavailable videos may not have an archived copy.</p>" : ""}${result.service_errors?.length ? `<p class="muted">Some services could not be checked: ${escapeHTML(result.service_errors.join(", "))}.</p>` : ""}<p class="muted">Results are cached for seven days. Archive links may contain metadata only and are not downloaded automatically.</p><a class="external-link" href="https://findyoutubevideo.thetechrobo.ca/?q=${encodeURIComponent(videoId)}" target="_blank" rel="noopener noreferrer">Open YouTube Video Finder ↗</a>`;
    await reload(true);
  } catch (error) {
    $("#recovery-content").textContent = error.message;
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
    await findArchives(button.dataset.video);
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

document.addEventListener("change", (event) => {
  const input = event.target;
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
$("#search").addEventListener("input", (event) => {
  state.search = event.target.value;
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
  setInterval(() => {
    if (!document.hidden) reload();
    if (
      !document.hidden &&
      ($("#setup-dialog").open ||
        ["waiting", "exchanging"].includes(state.login?.phase))
    )
      refreshLogin();
  }, 2000);
}
initialize();
