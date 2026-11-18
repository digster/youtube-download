// Run with playwright-cli run-code --filename tests/frontend/rendering.browser.js
// from the repo root. Serve only shipped assets and fixture responses in this tab.
async function testLibraryRendering(page) {
  const origin = "http://shelf.test";
  const playlist = {
    id: "active",
    title: "Active playlist",
    source: "custom",
    state: "running",
    total: 2,
    downloaded: 0,
    local_count: 0,
    bytes: 0,
    items_synced_at: 1,
    current_video: "video-1",
    progress: 0,
    thumbnail: origin + "/art.svg",
    metadata: { state: "running", total: 2, updated: 0, failed: 0 },
  };
  const other = {
    ...playlist,
    id: "idle",
    title: "Idle playlist",
    state: "idle",
    metadata: { state: "idle", total: 2, updated: 0, failed: 0 },
  };
  let playlists = [
    playlist,
    other,
    ...Array.from({ length: 68 }, (_, i) => ({
      ...other,
      id: "extra-" + i,
      title: "Shelf " + i,
    })),
  ];
  const videos = ["video-1", "video-2"].map((id) => ({
    id,
    title: id,
    status: "failed",
    error: "Download interrupted",
    in_playlist: true,
    thumbnail: playlist.thumbnail,
  }));
  let failedImages = 0;
  await page.route(origin + "/**", async (route) => {
    const path = new URL(route.request().url()).pathname;
    if (path === "/api/status") return route.fulfill({ json: { login: {} } });
    if (path === "/api/playlists")
      return route.fulfill({ json: { playlists } });
    if (path === "/api/playlists/active")
      return route.fulfill({ json: { playlist, videos } });
    if (
      path === "/api/playlists/active/pause" &&
      route.request().method() === "POST"
    ) {
      playlist.state = "paused";
      return route.fulfill({ json: {} });
    }
    if (path === "/missing.svg") {
      failedImages++;
      return route.fulfill({ status: 404, body: "" });
    }
    const asset = path.startsWith("/static/")
      ? path.slice(8)
      : path === "/art.svg"
        ? "favicon.svg"
        : "index.html";
    return route.fulfill({ path: "src/youtube_download/static/" + asset });
  });
  function check(condition, message) {
    if (!condition) throw new Error(message);
  }
  await page.clock.install();
  await page.goto(origin);
  await page.locator(".card").last().waitFor();
  await page
    .getByRole("checkbox", { name: "Select Idle playlist", exact: true })
    .check();
  await page.evaluate(() => {
    window.cards = [...document.querySelectorAll(".card")];
    window.images = cards.map((card) => card.querySelector("img"));
    window.focused = cards[1].querySelector(".library-link");
    focused.focus();
    window.scrollTo(0, 600);
    window.savedScroll = window.scrollY;
    window.mutations = [];
    const observer = new MutationObserver((records) =>
      mutations.push(...records),
    );
    for (const node of [
      cards[1],
      document.querySelector("#stats"),
      document.querySelector("#connection"),
    ]) {
      observer.observe(node, {
        childList: true,
        subtree: true,
        attributes: true,
        characterData: true,
      });
    }
  });
  playlist.progress = 50;
  await page.clock.runFor(2000);
  await page.waitForFunction(
    () => document.querySelector(".card progress").value === 25,
  );
  check(
    await page.evaluate(() =>
      cards.every(
        (card, i) =>
          card === document.querySelectorAll(".card")[i] &&
          images[i] === card.querySelector("img"),
      ),
    ),
    "A progress tick replaced a playlist card or thumbnail",
  );
  check(
    await page.evaluate(
      () =>
        document.activeElement === focused &&
        cards[1].querySelector("input").checked,
    ),
    "A progress tick lost focus or selection",
  );
  check(
    await page.evaluate(
      () =>
        savedScroll > 0 &&
        window.scrollY === savedScroll &&
        mutations.length === 0,
    ),
    "A progress tick scrolled the page or rewrote an unchanged region",
  );

  playlists = [other, playlist];
  await page.clock.runFor(2000);
  await page.waitForFunction(
    () => document.querySelector(".card-title").textContent === "Idle playlist",
  );
  check(
    await page.evaluate(() => cards[1] === document.querySelector(".card")),
    "Reordering playlists lost the card identity",
  );
  const search = page.getByRole("searchbox");
  await search.fill("no matches");
  await page.getByText("Nothing here just yet.").waitFor();
  await search.fill("");
  check(
    await page
      .getByRole("checkbox", { name: "Select Idle playlist", exact: true })
      .isChecked(),
    "A selection was lost across an empty search",
  );

  // A broken image should fall back once. A new URL must still display.
  playlist.thumbnail = origin + "/missing.svg";
  await page.clock.runFor(2000);
  await page.waitForFunction(
    () => !document.querySelectorAll(".card")[1].querySelector("img"),
  );
  playlist.downloaded = 1;
  playlist.local_count = 1;
  await page.clock.runFor(2000);
  await page.getByText("1 of 2 saved", { exact: true }).waitFor();
  check(
    (await page
      .locator("#stats .stat")
      .nth(1)
      .locator("strong")
      .textContent()) === "1",
    "The library summary missed a newly saved video",
  );
  check(failedImages === 1, "Progress retried an unchanged broken thumbnail");
  playlist.thumbnail = origin + "/art.svg";
  await page.clock.runFor(2000);
  await page.waitForFunction(
    () =>
      document.querySelectorAll(".card")[1].querySelector("img")?.naturalWidth >
      0,
  );

  await page.getByRole("button", { name: "Pause" }).click();
  await page.getByRole("button", { name: "Resume", exact: false }).waitFor();
  check(
    playlist.state === "paused",
    "The updated card did not pause its playlist",
  );

  await page.goto(origin + "/playlists/active");
  await page.locator(".video-row").last().waitFor();
  // A decoded image can still be clipped below the full-height placeholder.
  // Check what the browser displays inside the artwork slot.
  const thumbnail = page.locator(".video-thumb").first();
  await thumbnail.scrollIntoViewIfNeeded();
  check(
    await thumbnail.evaluate(async (slot) => {
      const image = slot.querySelector("img");
      await image.decode();
      const { x, y, width, height } = slot.getBoundingClientRect();
      return document.elementFromPoint(x + width / 2, y + height / 2) === image;
    }),
    "The video thumbnail loaded but the placeholder covers its slot",
  );
  await page.locator("summary").first().click();
  await page.evaluate(() => {
    window.rows = [...document.querySelectorAll(".video-row")];
    window.images = rows.map((row) => row.querySelector("img"));
    window.focused = rows[0].querySelector("summary");
    window.metadataProgress = document.querySelector(
      ".metadata-panel progress",
    );
    focused.focus();
  });
  playlist.progress = 75;
  playlist.metadata.updated = 1;
  videos[0].title = "Updated title";
  await page.clock.runFor(2000);
  await page.getByRole("heading", { name: "Updated title" }).waitFor();
  check(
    await page.evaluate(() =>
      rows.every(
        (row, i) =>
          row === document.querySelectorAll(".video-row")[i] &&
          images[i] === row.querySelector("img"),
      ),
    ),
    "A metadata update replaced a video row or thumbnail",
  );
  check(
    await page.evaluate(
      () =>
        rows[0].querySelector("details").open &&
        document.activeElement === focused,
    ),
    "A metadata update closed or blurred download details",
  );
  check(
    await page.evaluate(
      () =>
        metadataProgress ===
          document.querySelector(".metadata-panel progress") &&
        metadataProgress.value === 1,
    ),
    "Metadata progress did not update in place",
  );
  // Only the current page mounts rows. Shrinking the result set clamps a
  // now-empty second page and drops the obsolete rows from the DOM.
  videos.push(
    ...Array.from({ length: 99 }, (_, i) => ({
      ...videos[1],
      id: "extra-video-" + i,
      title: "Extra video " + i,
    })),
  );
  playlist.total = videos.length;
  await page.clock.runFor(2000);
  await page.getByRole("button", { name: "Next" }).click();
  await page.getByRole("heading", { name: "Extra video 98" }).waitFor();
  check(
    (await page.locator(".video-row").count()) === 1,
    "Pagination retained off-page rows",
  );
  videos.splice(2);
  playlist.total = videos.length;
  await page.clock.runFor(2000);
  await page.getByRole("heading", { name: "Updated title" }).waitFor();
  check(
    (await page.locator(".video-row").count()) === 2,
    "Polling failed to clamp the video page",
  );
  return "Playlist and video updates preserve thumbnails, selection, focus and open details.";
}
