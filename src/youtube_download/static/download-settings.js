/* Settings are loaded only when this dialog opens. Library/account polling
   cannot overwrite a draft, and each save replaces one validated preference set. */
const downloadForm = document.querySelector("#download-settings-form");
const downloadPresets = { best: "bv*+ba/b" };
for (const height of [2160, 1440, 1080, 720, 480]) {
  downloadPresets[height] = `bv*[height<=${height}]+ba/b[height<=${height}]`;
}
let downloadSettingsBusy = false;

function updateDownloadFields() {
  const custom = $("#download-quality").value === "custom";
  $("#download-custom-format").hidden = !custom;
  $("#download-format").required = custom;
  const subtitles =
    downloadForm.elements.write_subs.checked ||
    downloadForm.elements.write_auto_subs.checked;
  $("#download-subtitle-options").hidden = !subtitles;
  if (!subtitles) downloadForm.elements.embed_subs.checked = false;
}

function fillDownloadSettings(data) {
  for (const [key, value] of Object.entries(data.options)) {
    const control = downloadForm.elements.namedItem(key);
    if (!control) continue;
    if (control.type === "checkbox") control.checked = value;
    else control.value = value;
  }
  $("#download-quality").value =
    Object.keys(downloadPresets).find(
      (key) => downloadPresets[key] === data.options.format,
    ) || "custom";
  $("#download-settings-status").textContent =
    data.warning ||
    (data.overridden
      ? "Using your saved download settings."
      : "Using startup defaults.");
  updateDownloadFields();
}

function downloadFeedback(message, error = false) {
  const feedback = $("#download-settings-feedback");
  feedback.textContent = message;
  feedback.className = error ? "auth-error" : "auth-success";
  feedback.hidden = !message;
}

async function openDownloadSettings() {
  const dialog = $("#download-settings-dialog");
  if (dialog.open) return;
  dialog.showModal();
  if (downloadSettingsBusy) return;
  downloadSettingsBusy = true;
  $("#download-settings-fields").disabled = true;
  $("#download-settings-status").textContent = "Loading download settings…";
  downloadFeedback("");
  try {
    fillDownloadSettings(await api("/api/download-settings"));
    $("#download-settings-fields").disabled = false;
  } catch (error) {
    $("#download-settings-status").textContent =
      "Settings could not be loaded. Close and reopen to retry.";
    downloadFeedback(error.message, true);
  } finally {
    downloadSettingsBusy = false;
  }
}

async function saveDownloadSettings(reset = false) {
  if (downloadSettingsBusy) return;
  const values = {};
  for (const control of downloadForm.elements) {
    if (!control.name) continue;
    values[control.name] =
      control.type === "checkbox"
        ? control.checked
        : control.type === "number"
          ? Number(control.value)
          : control.value.trim();
  }
  values.format =
    downloadPresets[$("#download-quality").value] || values.format;
  downloadSettingsBusy = true;
  $("#download-settings-fields").disabled = true;
  $("#download-settings-save").textContent = "Saving…";
  downloadFeedback("");
  try {
    const result = await api(
      reset ? "/api/download-settings/reset" : "/api/download-settings",
      "POST",
      reset ? undefined : values,
    );
    fillDownloadSettings(result);
    downloadFeedback(
      `${reset ? "Startup defaults restored" : "Settings saved"}. Applies when the next video starts.`,
    );
  } catch (error) {
    // Keep every field intact on failure so a typo or disk error is easy to fix.
    downloadFeedback(error.message, true);
  } finally {
    downloadSettingsBusy = false;
    $("#download-settings-fields").disabled = false;
    $("#download-settings-save").textContent = "Save download settings";
  }
}

downloadForm.addEventListener("change", updateDownloadFields);
downloadForm.addEventListener("input", () => downloadFeedback(""));
downloadForm.addEventListener("submit", (event) => {
  event.preventDefault();
  saveDownloadSettings();
});
$("#download-settings-reset").addEventListener("click", () =>
  saveDownloadSettings(true),
);
