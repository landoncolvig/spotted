import { invoke, convertFileSrc } from "@tauri-apps/api/core";
import { listen, type UnlistenFn } from "@tauri-apps/api/event";
import { open, confirm } from "@tauri-apps/plugin-dialog";
import {
  isPermissionGranted,
  requestPermission,
  sendNotification,
} from "@tauri-apps/plugin-notification";

type State = "idle" | "tags" | "working" | "label" | "library" | "done";
type SidecarLine = { kind: "stdout" | "stderr"; line: string };
type SpottedEvent =
  | { event: "scan-start"; total: number }
  | { event: "video-start"; name: string; index: number; total: number; duration_sec?: number }
  | { event: "video-done"; name: string; index: number; total: number; faces: number }
  | { event: "video-skip"; name: string; index: number; total: number; reason?: string }
  | { event: "scan-complete"; total_faces: number; total_skipped: number; total_videos: number }
  | { event: "cluster-start"; faces: number }
  | { event: "cluster-complete"; clusters: number }
  | { event: "tag-start"; total: number }
  | { event: "tag-video"; name: string; names: string[]; index: number; total: number }
  | { event: "tag-error"; name: string; message: string }
  | { event: "tag-empty"; message: string }
  | { event: "tag-failed"; failed: number; total: number; first: string }
  | { event: "tag-complete"; total: number }
  | { event: "markers-start"; total: number }
  | { event: "markers-video"; name: string; count: number; index: number; total: number }
  | { event: "markers-error"; name: string; message: string }
  | { event: "markers-complete"; total: number }
  | { event: "tag-verified"; file: string; xmp: string[]; keys: string[]; comment: string }
  | { event: "tag-verify-error"; message: string }
  | { event: "markers-verified"; file: string; event_count: number; in_file_present: boolean; sidecar_present: boolean }
  | { event: "markers-verify-error"; message: string }
  | { event: "markers-sidecar-error"; name: string; message: string }
  | { event: "activity-start"; total: number }
  | { event: "activity-complete"; total: number; tagged: number; sample: { file: string; tags: string[] } | null }
  | { event: "activity-empty"; message: string }
  | { event: "activities-disabled"; reason: string }
  | { event: "library-stats"; videos: number; faces: number; clusters: number; named: number; people: { name: string; cluster_id?: number; clips: number; faces: number }[] }
  | { event: "library-person"; name: string; clips: { path: string; name: string; times: number[] }[] }
  | { event: "library-detail-complete" }
  | { event: "person-renamed"; old: string; new: string; clusters: number }
  | { event: "person-deleted"; name: string; clusters: number }
  | { event: "error"; stage: string; message: string };

const stage = document.getElementById("stage") as HTMLElement;
const dropzone = document.getElementById("dropzone") as HTMLElement;
const versionEl = document.getElementById("version") as HTMLElement;
const workingLabel = document.getElementById("working-label") as HTMLElement;
const workingPath = document.getElementById("working-path") as HTMLElement;
const workingDetail = document.getElementById("working-detail") as HTMLElement;
const progressBar = document.getElementById("progress-bar") as HTMLElement;
const doneSub = document.getElementById("done-sub") as HTMLElement;
const doneTitle = document.getElementById("done-title") as HTMLElement;
const btnAgain = document.getElementById("btn-again") as HTMLButtonElement;
const btnReveal = document.getElementById("btn-reveal") as HTMLButtonElement;
const tagsPath = document.getElementById("tags-path") as HTMLElement;
const tagsInput = document.getElementById("tags-input") as HTMLInputElement;
const tagsStart = document.getElementById("tags-start") as HTMLButtonElement;
const tagsSkip = document.getElementById("tags-skip") as HTMLButtonElement;

const LABEL_PORT = 8765;
let currentPath: string | null = null;

function setState(s: State) {
  stage.setAttribute("data-state", s);
}

async function loadVersion() {
  try {
    const v = await invoke<string>("app_version");
    versionEl.textContent = `v${v}`;
  } catch {}
}

type LibraryStatsEvent = Extract<SpottedEvent, { event: "library-stats" }>;

async function refreshFooterStatus() {
  try {
    lastStats = null;
    await invoke("fetch_status");
    await new Promise((r) => setTimeout(r, 80));
    const s = lastStats as SpottedEvent | null;
    if (!s || s.event !== "library-stats") return;
    const stats: LibraryStatsEvent = s;
    const status = document.getElementById("footer-status");
    if (status) {
      if (stats.named > 0) {
        status.textContent = `${stats.named} ${stats.named === 1 ? "person" : "people"} · ${stats.videos} clips`;
      } else if (stats.videos > 0) {
        status.textContent = `${stats.videos} clips · 0 named`;
      } else {
        status.textContent = "Library empty";
      }
    }
    // Also reflect in the window title so it shows even when minimized.
    let title = "Spotted";
    if (stats.named > 0) {
      title = `Spotted — ${stats.named} ${stats.named === 1 ? "person" : "people"} · ${stats.videos} clips`;
    } else if (stats.videos > 0) {
      title = `Spotted — ${stats.videos} clips`;
    }
    try { await invoke("set_window_title", { title }); } catch {}
  } catch {}
}

// ---------- WELCOME ----------
const WELCOME_KEY = "spotted.hasSeenWelcome.v1";

function maybeShowWelcome() {
  try {
    if (!localStorage.getItem(WELCOME_KEY)) {
      showWelcome();
    }
  } catch {
    /* localStorage might be blocked — skip welcome rather than crash */
  }
}

function dismissWelcome(persist = true) {
  const w = document.getElementById("welcome");
  if (w) {
    w.hidden = true;
    // Belt and suspenders: even if a CSS rule wins over [hidden],
    // inline display:none always wins.
    w.style.display = "none";
  }
  if (persist) {
    try { localStorage.setItem(WELCOME_KEY, "1"); } catch {}
  }
}

function showWelcome() {
  const w = document.getElementById("welcome");
  if (w) {
    w.hidden = false;
    w.style.display = "";  // clear the inline override
  }
}

function wireWelcome() {
  document.getElementById("welcome-cta")?.addEventListener("click", () => dismissWelcome(true));
  document.getElementById("welcome-skip")?.addEventListener("click", () => dismissWelcome(true));
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape") {
      const w = document.getElementById("welcome");
      if (w && !w.hidden) dismissWelcome(true);
    }
  });
}

async function pickFolder() {
  const path = await open({ directory: true, multiple: false });
  if (typeof path === "string") {
    askForTags(path);
  }
}

// Reasonable stop words to drop from auto-suggested tags.
const STOP = new Set([
  "the", "and", "a", "an", "of", "in", "on", "at", "for", "to",
  "test", "tests", "video", "videos", "footage", "clip", "clips",
  "mov", "mp4", "avi", "m4v",
  "raw", "edit", "final", "draft", "v1", "v2", "v3",
]);

function suggestTagsFromPath(path: string): string {
  const last = path.split("/").filter(Boolean).pop() || "";
  const parts = last
    .split(/[-_.\s]+/)
    .map((s) => s.trim().toLowerCase())
    .filter((s) => s && s.length > 1 && !STOP.has(s) && !/^\d+$/.test(s));
  // Dedupe preserving order
  const seen = new Set<string>();
  const out: string[] = [];
  for (const p of parts) {
    if (!seen.has(p)) {
      seen.add(p);
      out.push(p);
    }
  }
  return out.slice(0, 5).join(", ");
}

function askForTags(path: string) {
  currentPath = path;
  tagsPath.textContent = path;
  tagsInput.value = suggestTagsFromPath(path);
  setState("tags");
  // Focus + select-all so the user can immediately edit or hit Enter
  setTimeout(() => {
    tagsInput.focus();
    tagsInput.select();
  }, 50);
}

function readTagsInput(): string[] {
  return tagsInput.value
    .split(",")
    .map((t) => t.trim().toLowerCase())
    .filter(Boolean);
}

function setProgress(pct: number) {
  // Switch out of indeterminate mode the moment we get a real value
  const wrap = document.getElementById("progress");
  if (wrap) wrap.classList.remove("is-indeterminate");
  progressBar.style.width = `${Math.max(0, Math.min(100, pct))}%`;
}

function setProgressIndeterminate() {
  const wrap = document.getElementById("progress");
  if (wrap) wrap.classList.add("is-indeterminate");
}

function parseSpotted(line: string): SpottedEvent | null {
  if (!line.startsWith("__SPOTTED__ ")) return null;
  try {
    return JSON.parse(line.slice("__SPOTTED__ ".length)) as SpottedEvent;
  } catch {
    return null;
  }
}

let lastStats: SpottedEvent | null = null;
let lastVerification: Extract<SpottedEvent, { event: "tag-verified" }> | null = null;
let lastMarkersVerification: Extract<SpottedEvent, { event: "markers-verified" }> | null = null;
let lastActivityResult: Extract<SpottedEvent, { event: "activity-complete" }> | null = null;

async function fetchLibraryStats(): Promise<SpottedEvent | null> {
  // Calls `facetag status`; the structured library-stats event is captured
  // by the global sidecar://line listener and stored in lastStats.
  lastStats = null;
  try {
    await invoke<string>("fetch_status");
  } catch {}
  // Give the event a tick to land.
  await new Promise((r) => setTimeout(r, 100));
  return lastStats;
}

// Cross-phase state for the current batch. Reset at runBatch entry, read by
// the global sidecar event handler.
const batch = {
  scanTotal: 0,
  scanDone: 0,
  scanFaces: 0,
  tagTotal: 0,
};

function handleSpottedEvent(evt: SpottedEvent) {
  switch (evt.event) {
    case "scan-start":
      batch.scanTotal = evt.total;
      batch.scanDone = 0;
      batch.scanFaces = 0;
      workingLabel.textContent = "Spotting";
      workingDetail.textContent = `Reading ${evt.total} clips`;
      setProgress(2);
      break;
    case "video-start":
      workingDetail.textContent = `${evt.name} — clip ${evt.index} of ${evt.total}`;
      break;
    case "video-done":
      batch.scanDone = evt.index;
      batch.scanFaces += evt.faces;
      if (batch.scanTotal > 0) {
        setProgress((batch.scanDone / batch.scanTotal) * 80);
      }
      workingDetail.textContent = `${batch.scanDone}/${batch.scanTotal} clips · ${batch.scanFaces} faces · ${evt.name}`;
      break;
    case "scan-complete":
      batch.scanFaces = evt.total_faces;
      workingDetail.textContent = `${evt.total_videos} clips · ${evt.total_faces} faces`;
      setProgress(82);
      break;
    case "cluster-start":
      workingLabel.textContent = "Grouping faces";
      workingDetail.textContent = `Clustering ${evt.faces} faces…`;
      setProgress(85);
      break;
    case "cluster-complete":
      workingDetail.textContent = `${evt.clusters} people candidates`;
      setProgress(92);
      break;
    case "tag-start":
      batch.tagTotal = evt.total;
      workingLabel.textContent = "Writing keywords";
      workingDetail.textContent = `Tagging ${evt.total} clips…`;
      setProgress(0);
      break;
    case "tag-video":
      workingDetail.textContent = `${evt.name} — ${evt.names.join(", ")}`;
      if (batch.tagTotal > 0) setProgress((evt.index / batch.tagTotal) * 100);
      break;
    case "tag-complete":
      // Tag-write done; markers come next and finish at 100.
      setProgress(60);
      break;
    case "markers-start":
      workingLabel.textContent = "Writing timeline markers";
      workingDetail.textContent = `Adding markers to ${evt.total} clips…`;
      setProgress(65);
      break;
    case "markers-video":
      workingDetail.textContent = `${evt.name} — ${evt.count} marker(s)`;
      if (evt.total > 0) setProgress(60 + (evt.index / evt.total) * 40);
      break;
    case "markers-error":
      console.warn("marker error:", evt.name, evt.message);
      break;
    case "markers-complete":
      setProgress(100);
      break;
    case "library-stats":
      lastStats = evt;
      handleLibraryEvent(evt);
      break;
    case "tag-verified":
      lastVerification = evt;
      break;
    case "markers-verified":
      lastMarkersVerification = evt;
      break;
    case "activity-start":
      workingLabel.textContent = "Spotting activities";
      workingDetail.textContent = `Scoring ${evt.total} clips against curated prompts…`;
      break;
    case "activity-complete":
      lastActivityResult = evt;
      break;
    case "activity-empty":
    case "activities-disabled":
      // Non-fatal — face tagging still proceeds. Logged for devtools.
      console.info("activity step skipped:", evt);
      break;
    case "library-person":
      handleLibraryEvent(evt);
      break;
    case "error":
    case "tag-error":
    case "tag-empty":
    case "tag-failed":
      // These are also surfaced through the sidecar exit-code path in
      // runTagWrite()'s catch — log here for the devtools breadcrumb.
      console.warn(evt);
      break;
  }
}

let sidecarUnlisten: UnlistenFn | null = null;
async function ensureSidecarListener() {
  if (sidecarUnlisten) return;
  sidecarUnlisten = await listen<SidecarLine>("sidecar://line", (e) => {
    const evt = parseSpotted(e.payload.line);
    if (evt) handleSpottedEvent(evt);
  });
}

async function runBatch(path: string, tags: string[] = []) {
  currentPath = path;
  setState("working");
  workingPath.textContent = path;
  setProgressIndeterminate();
  workingLabel.textContent = "Spotting";
  workingDetail.textContent = "Looking for footage…";

  batch.scanTotal = 0;
  batch.scanDone = 0;
  batch.scanFaces = 0;
  batch.tagTotal = 0;

  await ensureSidecarListener();

  try {
    await invoke<number>("scan_folder", { path, tags });
    await invoke<number>("cluster_faces");
    workingLabel.textContent = "Naming people";
    workingDetail.textContent = "Opening labeler…";
    await invoke<number>("start_label_server", {
      port: LABEL_PORT,
      scopePaths: currentPath ? [currentPath] : null,
    });
    mountLabelScreen();
    setState("label");
  } catch (err) {
    showError(String(err));
  }
}

function showError(message: string) {
  setState("done");
  doneTitle.textContent = "Couldn't finish";
  doneSub.textContent = friendlyError(message);
  doneSub.classList.add("done__sub--error");
}

function clearError() {
  doneTitle.textContent = "Done.";
  doneSub.classList.remove("done__sub--error");
}

/** Map common sidecar failure modes to plain-English explanations. */
function friendlyError(raw: string): string {
  const r = raw.toLowerCase();
  if (r.includes("no videos found")) {
    return "That folder doesn't have any videos in it (or none in a format I recognize: .mov, .mp4, .m4v, .mkv, .avi).";
  }
  if (r.includes("no faces in index")) {
    return "I scanned the folder but couldn't detect any faces. The clips might be too dark, faces too small in frame, or no people on camera.";
  }
  if (r.includes("ffprobe not on path") || r.includes("ffmpeg")) {
    return "Video decoding failed — ffmpeg isn't available. This shouldn't happen in a packaged Spotted build; please report it.";
  }
  if (r.includes("exiftool not found")) {
    return "Metadata writing failed — exiftool isn't available. This shouldn't happen in a packaged Spotted build; please report it.";
  }
  if (r.includes("nothing to tag")) {
    return "Nothing was tagged — you need to name at least one face cluster (or add a batch tag) before hitting Tag & finish.";
  }
  if (r.includes("can't locate image/exiftool.pm") || r.includes("image::exiftool")) {
    return "The bundled metadata writer (exiftool) couldn't find its support files. This is a Spotted packaging bug; please report it and include the version number.";
  }
  if (r.match(/exiftool failed on \d+\/\d+ clip/)) {
    // First line carries the per-clip error from exiftool's stderr.
    return `Couldn't write metadata to your clips. ${raw} — common causes: the folder is in iCloud Drive / Dropbox (file is a placeholder, not downloaded), the volume is read-only, or the clips have a quarantine flag from being downloaded.`;
  }
  if (r.includes("address already in use")) {
    return "The labeling page port is busy. Quit and reopen Spotted, then try again.";
  }
  // Fallback: show the raw error but trimmed to last reasonable chunk
  return raw;
}

function makeEl<K extends keyof HTMLElementTagNameMap>(
  tag: K,
  className?: string,
): HTMLElementTagNameMap[K] {
  const el = document.createElement(tag);
  if (className) el.className = className;
  return el;
}

function mountLabelScreen() {
  if (document.querySelector(".screen--label")) return;

  const screen = makeEl("div", "screen screen--label");
  const wrap = makeEl("div", "label-wrap");

  const frame = makeEl("iframe", "label-frame");
  frame.src = `http://127.0.0.1:${LABEL_PORT}/`;
  frame.title = "Spotted labeler";

  const bar = makeEl("div", "label-bar");
  const hint = makeEl("span", "label-hint");
  hint.textContent = "Name each cluster, then:";
  const tagBtn = makeEl("button", "btn");
  tagBtn.id = "btn-tag";
  tagBtn.textContent = "Tag & finish";
  bar.append(hint, tagBtn);

  wrap.append(frame, bar);
  screen.appendChild(wrap);
  stage.appendChild(screen);

  tagBtn.addEventListener("click", runTagWrite);
}

async function runTagWrite() {
  setState("working");
  setProgressIndeterminate();
  workingLabel.textContent = "Spotting activities";
  workingDetail.textContent = "Scoring curated prompts against frame embeddings…";
  // Activity suggestion runs before tag-write so any matched prompts
  // (kids, beach, wedding…) merge into the keyword field with the
  // person names. Non-fatal — if the .mlpackage isn't bundled or the
  // user disabled --activities on scan, this returns quickly with no
  // auto-tags applied.
  try {
    await invoke<number>("suggest_activities");
  } catch (e) {
    console.warn("activity suggest failed (non-fatal):", e);
  }
  workingLabel.textContent = "Writing keywords";
  workingDetail.textContent = "Running exiftool, per clip…";
  try {
    await invoke<number>("tag_videos");

    // Markers are a bonus — Premiere/DaVinci-only feature. Failures here
    // should not break the flow because keywords already succeeded.
    workingLabel.textContent = "Writing timeline markers";
    workingDetail.textContent = "For Premiere & DaVinci scrubber…";
    try {
      await invoke<number>("write_markers");
    } catch (e) {
      console.warn("marker write failed (non-fatal):", e);
    }

    setProgress(100);
    const stats = await fetchLibraryStats();
    setState("done");
    renderDone(stats);
    notifyIfBackground("Spotted finished", summarizeDone(stats));
  } catch (err) {
    showError(String(err));
  }
}

function summarizeDone(stats: SpottedEvent | null): string {
  if (!stats || stats.event !== "library-stats" || stats.people.length === 0) {
    return "Tags written. Open Spotted to see what changed.";
  }
  const top = stats.people.slice(0, 3).map((p) => p.name).join(", ");
  const extra = stats.people.length > 3 ? ` and ${stats.people.length - 3} more` : "";
  return `${top}${extra} — tagged across ${stats.videos} clips.`;
}

/** Fire a macOS notification only if the user isn't actively looking at
 *  Spotted. If the window is focused, the in-app done screen is enough. */
async function notifyIfBackground(title: string, body: string) {
  if (document.visibilityState === "visible" && document.hasFocus()) return;
  try {
    let granted = await isPermissionGranted();
    if (!granted) {
      const r = await requestPermission();
      granted = r === "granted";
    }
    if (!granted) return;
    sendNotification({ title, body });
  } catch (e) {
    // Silently fail — notification is a nice-to-have, not a critical path
    console.warn("notification failed:", e);
  }
}

function renderDone(stats: SpottedEvent | null) {
  clearError();
  if (!stats || stats.event !== "library-stats" || stats.people.length === 0) {
    doneSub.textContent = "Open the folder in Premiere or DaVinci — search by name.";
  } else {
    const top = stats.people.slice(0, 6);
    const breakdown = top.map((p) => `${p.name} (${p.clips})`).join(" · ");
    const more = stats.people.length > top.length ? ` · +${stats.people.length - top.length} more` : "";
    doneSub.textContent = `Tagged ${stats.people.length} people across ${stats.videos} clips — ${breakdown}${more}`;
  }
  renderVerification();
}

function renderVerification() {
  let panel = document.getElementById("done-verify");
  if (!panel) {
    panel = document.createElement("div");
    panel.id = "done-verify";
    panel.className = "done-verify";
    // Insert after doneSub, before the actions
    doneSub.parentElement?.insertBefore(panel, doneSub.nextSibling);
  }
  panel.replaceChildren();

  if (!lastVerification) return;
  const v = lastVerification;

  const header = document.createElement("div");
  header.className = "done-verify__header";
  header.textContent = `Verified on ${v.file}`;
  panel.appendChild(header);

  const markers = lastMarkersVerification;
  const markerCells: string[] = [];
  if (markers) {
    if (markers.in_file_present) markerCells.push(`in-file (${markers.event_count})`);
    if (markers.sidecar_present) markerCells.push(`sidecar .xmp`);
  }

  const rows: Array<{ label: string; values: string[]; help: string }> = [
    {
      label: "Keys (DaVinci, Finder)",
      values: v.keys,
      help: "Read by DaVinci Resolve's Keywords column and macOS Spotlight search.",
    },
    {
      label: "XMP (Premiere)",
      values: v.xmp,
      help: "Read by Adobe Premiere Pro's Keywords column.",
    },
    {
      label: "Spotlight Comment",
      values: v.comment ? [v.comment] : [],
      help: "Shown in Get Info → Comments. iCloud-synced files may strip this; Keys above keeps search working anyway.",
    },
    {
      label: "Markers (timeline)",
      values: markerCells,
      help: "Per-face timeline markers. In-file XMP for Premiere; sidecar .xmp next to the clip for DaVinci (enable 'Use Sidecar Files' in project settings).",
    },
    {
      label: "Activities (MobileCLIP)",
      values: lastActivityResult?.sample?.tags ?? [],
      help: "Zero-shot scene/object tags auto-applied via Apple's MobileCLIP. Adds discoverability beyond face names (kids, beach, wedding…). Disable per-scan with the --activities/--no-activities flag.",
    },
  ];

  for (const row of rows) {
    const div = document.createElement("div");
    div.className = "done-verify__row " + (row.values.length > 0 ? "is-ok" : "is-empty");
    const dot = document.createElement("span");
    dot.className = "done-verify__dot";
    dot.textContent = row.values.length > 0 ? "✓" : "⚠";
    const label = document.createElement("span");
    label.className = "done-verify__label";
    label.textContent = row.label;
    const value = document.createElement("span");
    value.className = "done-verify__value";
    value.textContent = row.values.length > 0 ? row.values.join(", ") : "empty";
    value.title = row.help;
    div.append(dot, label, value);
    panel.appendChild(div);
  }
}

btnAgain.addEventListener("click", () => {
  setProgress(0);
  clearError();
  setState("idle");
});

btnReveal.addEventListener("click", async () => {
  if (currentPath) {
    try {
      await invoke("reveal_in_finder", { path: currentPath });
    } catch (e) {
      console.warn("reveal failed:", e);
    }
  }
});

const btnCancel = document.getElementById("btn-cancel") as HTMLButtonElement | null;
btnCancel?.addEventListener("click", async () => {
  const ok = confirm("Stop the current batch? Anything already detected stays in the index.");
  if (!ok) return;
  btnCancel.disabled = true;
  btnCancel.textContent = "Cancelling…";
  try {
    await invoke<number>("cancel_work");
    flashToast("Cancelled.");
  } catch (e) {
    flashToast("Cancel failed: " + String(e), true);
  }
  // Send the user back to idle. The sidecar might still emit a few
  // events as it winds down — that's fine.
  setState("idle");
  setProgress(0);
  btnCancel.disabled = false;
  btnCancel.textContent = "Cancel";
});

function isBusy(): boolean {
  const s = stage.getAttribute("data-state");
  return s === "working" || s === "label";
}

// ---------- LIBRARY ----------

type LibraryClip = { path: string; name: string; times: number[] };
type LibraryPerson = {
  name: string;
  clusterId?: number;
  clips: number;
  faces: number;
  clipsDetail: LibraryClip[];
};

const library: {
  people: LibraryPerson[];
  selected: string | null;
  loading: boolean;
} = {
  people: [],
  selected: null,
  loading: false,
};

function libraryEl<T extends HTMLElement>(id: string): T {
  return document.getElementById(id) as T;
}

async function openLibrary() {
  setState("library");
  await loadLibrary();
}

async function loadLibrary() {
  if (library.loading) return;
  library.loading = true;
  library.people = [];
  renderLibrarySkeleton();
  await ensureSidecarListener();
  try {
    await invoke("fetch_library_detail");
    // Generate per-person thumbnails in the background. Sidebar will
    // re-render as they appear on disk; missing ones gracefully fall
    // back to text-only rows.
    invoke("generate_person_thumbs")
      .then(() => renderLibrarySidebar())
      .catch(() => {});
  } catch (e) {
    flashToast("Couldn't load library: " + String(e), true);
  } finally {
    library.loading = false;
    refreshFooterStatus();
  }
}

function renderLibrarySkeleton() {
  const list = libraryEl<HTMLUListElement>("library-people");
  list.replaceChildren();
  list.className = "library-skeleton";
  for (let i = 0; i < 6; i++) {
    const li = document.createElement("li");
    const name = document.createElement("span");
    name.className = "bar bar--name";
    const count = document.createElement("span");
    count.className = "bar bar--count";
    li.append(name, count);
    list.appendChild(li);
  }
}

function handleLibraryEvent(evt: SpottedEvent) {
  if (evt.event === "library-stats") {
    library.people = evt.people.map((p) => ({
      name: p.name,
      clusterId: p.cluster_id,
      clips: p.clips,
      faces: p.faces,
      clipsDetail: [],
    }));
    libraryEl("library-stats").textContent =
      `${evt.named} ${evt.named === 1 ? "person" : "people"} · ${evt.videos} clips`;
    renderLibrarySidebar();
  } else if (evt.event === "library-person") {
    const p = library.people.find((x) => x.name === evt.name);
    if (p) p.clipsDetail = evt.clips;
    if (library.selected === evt.name) renderLibraryDetail();
  }
}

function personThumbUrl(p: LibraryPerson): string | null {
  if (p.clusterId == null) return null;
  // ~/.facetag/person_thumbs/<cluster_id>.jpg via the Tauri asset protocol.
  // We resolve $HOME via the standard convertFileSrc helper.
  const home = (window as any).__TAURI_INTERNALS__?.metadata?.os?.platform
    ? "/Users/" + ((window as any).__TAURI_INTERNALS__?.metadata?.os?.user || "qb")
    : "/Users/qb";
  return convertFileSrc(`${home}/.facetag/person_thumbs/${p.clusterId}.jpg`);
}

function renderLibrarySidebar() {
  const list = libraryEl<HTMLUListElement>("library-people");
  list.className = "library-people";
  list.replaceChildren();
  const filter = (libraryEl<HTMLInputElement>("library-search").value || "")
    .toLowerCase()
    .trim();
  const filtered = filter
    ? library.people.filter((p) => p.name.toLowerCase().includes(filter))
    : library.people;
  if (filtered.length === 0) {
    const li = document.createElement("li");
    li.className = "library-person-row";
    li.style.opacity = "0.5";
    li.style.cursor = "default";
    li.textContent = library.people.length === 0 ? "No tagged people yet." : "No matches.";
    list.appendChild(li);
    return;
  }
  for (const p of filtered) {
    const li = document.createElement("li");
    li.className = "library-person-row";
    if (p.name === library.selected) li.classList.add("active");

    const left = document.createElement("span");
    left.className = "library-person-left";
    const thumbUrl = personThumbUrl(p);
    if (thumbUrl) {
      const img = document.createElement("img");
      img.className = "library-thumb";
      img.alt = "";
      img.loading = "lazy";
      img.src = thumbUrl;
      img.onerror = () => img.classList.add("library-thumb--missing");
      left.appendChild(img);
    } else {
      const placeholder = document.createElement("span");
      placeholder.className = "library-thumb library-thumb--missing";
      left.appendChild(placeholder);
    }
    const name = document.createElement("span");
    name.className = "name";
    name.textContent = p.name;
    left.appendChild(name);

    const count = document.createElement("span");
    count.className = "count";
    count.textContent = String(p.clips);
    li.append(left, count);
    li.addEventListener("click", () => selectPerson(p.name));
    list.appendChild(li);
  }
}

function selectPerson(name: string) {
  library.selected = name;
  renderLibrarySidebar();
  renderLibraryDetail();
}

function renderLibraryDetail() {
  const emptyEl = libraryEl("library-empty");
  const personEl = libraryEl("library-person");
  if (!library.selected) {
    emptyEl.hidden = false;
    personEl.hidden = true;
    return;
  }
  const p = library.people.find((x) => x.name === library.selected);
  if (!p) {
    emptyEl.hidden = false;
    personEl.hidden = true;
    return;
  }
  emptyEl.hidden = true;
  personEl.hidden = false;

  const nameInput = libraryEl<HTMLInputElement>("person-name");
  nameInput.value = p.name;
  libraryEl("person-meta").textContent =
    `${p.clips} ${p.clips === 1 ? "clip" : "clips"} · ${p.faces} face detections`;

  const clipsEl = libraryEl<HTMLUListElement>("person-clips");
  clipsEl.replaceChildren();
  if (p.clipsDetail.length === 0) {
    const li = document.createElement("li");
    li.className = "person-clip";
    li.textContent = "Loading clip list…";
    clipsEl.appendChild(li);
    return;
  }
  for (const c of p.clipsDetail) {
    const li = document.createElement("li");
    li.className = "person-clip";
    const nm = document.createElement("span");
    nm.className = "person-clip-name";
    nm.textContent = c.name;
    nm.title = c.path;
    const tms = document.createElement("span");
    tms.className = "person-clip-times";
    const summary = c.times.length === 1
      ? `${c.times[0].toFixed(1)}s`
      : `${c.times.length} appearances`;
    tms.textContent = summary;
    const reveal = document.createElement("button");
    reveal.className = "person-clip-reveal";
    reveal.textContent = "Reveal";
    reveal.addEventListener("click", () => {
      invoke("reveal_in_finder", { path: c.path }).catch(() => {});
    });
    li.append(nm, tms, reveal);
    clipsEl.appendChild(li);
  }
}

async function commitRename(newName: string) {
  const old = library.selected;
  if (!old) return;
  newName = newName.trim();
  if (!newName || newName === old) return;
  try {
    await invoke("rename_person", { old, new: newName });
    // Update in-memory state
    const p = library.people.find((x) => x.name === old);
    if (p) p.name = newName;
    library.selected = newName;
    flashToast(`Renamed ${old} → ${newName}`);
    renderLibrarySidebar();
    renderLibraryDetail();
  } catch (e) {
    flashToast("Rename failed: " + String(e), true);
    libraryEl<HTMLInputElement>("person-name").value = old;
  }
}

async function deletePerson() {
  const name = library.selected;
  if (!name) return;
  if (!confirm(`Delete ${name} from the library?\n\nClips on disk keep their existing Keywords until you re-tag.`)) {
    return;
  }
  try {
    await invoke("delete_person", { name });
    library.people = library.people.filter((x) => x.name !== name);
    library.selected = null;
    flashToast(`Deleted ${name}`);
    renderLibrarySidebar();
    renderLibraryDetail();
  } catch (e) {
    flashToast("Delete failed: " + String(e), true);
  }
}

function wireLibrary() {
  libraryEl("library-back").addEventListener("click", () => setState("idle"));
  libraryEl<HTMLInputElement>("library-search").addEventListener("input", renderLibrarySidebar);
  const nameInput = libraryEl<HTMLInputElement>("person-name");
  nameInput.addEventListener("blur", () => commitRename(nameInput.value));
  nameInput.addEventListener("keydown", (e) => {
    if (e.key === "Enter") {
      e.preventDefault();
      nameInput.blur();
    } else if (e.key === "Escape") {
      e.preventDefault();
      if (library.selected) nameInput.value = library.selected;
      nameInput.blur();
    }
  });
  libraryEl("person-delete").addEventListener("click", deletePerson);
}

function wireDragDrop() {
  dropzone.addEventListener("click", () => {
    if (isBusy()) {
      flashToast("Finish or cancel the current batch first.");
      return;
    }
    pickFolder();
  });
  listen("tauri://drag-enter", () => {
    if (!isBusy()) dropzone.classList.add("is-hot");
  });
  listen("tauri://drag-leave", () => dropzone.classList.remove("is-hot"));
  listen<{ paths: string[] }>("tauri://drag-drop", (e) => {
    dropzone.classList.remove("is-hot");
    if (isBusy()) {
      flashToast("Finish or cancel the current batch first.");
      return;
    }
    const paths = e.payload?.paths ?? [];
    if (paths.length > 0) askForTags(paths[0]);
  });
}

function wireMenuEvents() {
  listen("menu://open-folder", () => {
    if (isBusy()) {
      flashToast("Finish or cancel the current batch first.");
      return;
    }
    pickFolder();
  });
  listen("menu://open-library", () => {
    if (isBusy()) {
      flashToast("Finish or cancel the current batch first.");
      return;
    }
    openLibrary();
  });
  listen("menu://retag-library", async () => {
    if (isBusy()) {
      flashToast("Finish or cancel the current batch first.");
      return;
    }
    await ensureSidecarListener();
    await runTagWrite();
  });
  listen("menu://show-welcome", () => {
    try { localStorage.removeItem(WELCOME_KEY); } catch {}
    showWelcome();
  });
  listen("menu://reset-library", async () => {
    if (isBusy()) {
      flashToast("Finish or cancel the current batch first.");
      return;
    }
    const ok = await confirm(
      "Reset everything Spotted has indexed?\n\n" +
      "This deletes the face index, frame embeddings, generated thumbnails, " +
      "and per-person crops in ~/.facetag/. Tag data already written into " +
      "your .mov files stays put — only Spotted's internal library is wiped.",
      { title: "Reset Library", okLabel: "Reset", cancelLabel: "Cancel" }
    );
    if (!ok) return;
    try {
      await invoke("reset_library");
      flashToast("Library reset. Drop a folder to start fresh.");
      setState("idle");
    } catch (e) {
      flashToast(`Reset failed: ${e}`, true);
    }
  });
  listen("menu://check-updates", async () => {
    try {
      const newVersion = await invoke<string | null>("check_for_updates");
      if (!newVersion) {
        flashToast(`You're on the latest version.`);
      } else {
        await promptForUpdate(newVersion);
      }
    } catch (e) {
      flashToast(`Update check failed: ${e}`, true);
    }
  });
}

/** Confirm dialog → install → restart, with visible feedback at every step.
 *  Reused by the on-launch auto-check and the manual "Check for Updates…"
 *  menu item so users get one consistent flow. */
async function promptForUpdate(version: string, currentVersion?: string): Promise<void> {
  const fromTo = currentVersion
    ? `Spotted ${version} is available — you're on ${currentVersion}.`
    : `Spotted ${version} is available.`;
  const ok = await confirm(
    `${fromTo}\n\nInstall and restart now?`,
    { title: "Update available", okLabel: "Install & Restart", cancelLabel: "Later" }
  );
  if (!ok) return;
  flashToast("Downloading update…");
  try {
    await invoke("install_update");
    flashToast("Restarting Spotted…");
    // Give the toast a moment to render before the process is replaced.
    setTimeout(() => { invoke("restart_app").catch(() => {}); }, 400);
  } catch (e) {
    flashToast(`Update failed: ${e}`, true);
  }
}

/** Wire updater events from Rust. The auto-check task in setup() emits
 *  `updater://available` when a newer version is published; the user sees
 *  a native dialog and decides whether to install. */
function wireUpdaterEvents() {
  listen<{ version: string; current_version: string }>(
    "updater://available",
    (e) => {
      promptForUpdate(e.payload.version, e.payload.current_version).catch((err) => {
        flashToast(`Update prompt failed: ${err}`, true);
      });
    },
  );
  listen<{ downloaded: number; total: number | null }>(
    "updater://progress",
    (e) => {
      if (e.payload.total) {
        const pct = Math.round((e.payload.downloaded / e.payload.total) * 100);
        flashToast(`Downloading update… ${pct}%`);
      }
    },
  );
  listen<{ message: string }>("updater://error", (e) => {
    // Auto-check errors are usually transient network issues — log them
    // for the devtools but don't pop a toast on every launch. The user
    // can still trigger a manual check from the menu.
    console.warn("updater:", e.payload.message);
  });
}

let toastTimer: number | null = null;
function flashToast(msg: string, isError = false) {
  let toast = document.getElementById("toast");
  if (!toast) {
    toast = makeEl("div");
    toast.id = "toast";
    document.body.appendChild(toast);
  }
  toast.textContent = msg;
  toast.className = "toast" + (isError ? " toast--err" : "");
  toast.classList.add("toast--show");
  if (toastTimer !== null) window.clearTimeout(toastTimer);
  toastTimer = window.setTimeout(() => {
    toast!.classList.remove("toast--show");
  }, 2400);
}

function wireTagsScreen() {
  tagsStart.addEventListener("click", () => {
    if (!currentPath) return;
    runBatch(currentPath, readTagsInput());
  });
  tagsSkip.addEventListener("click", () => {
    if (!currentPath) return;
    runBatch(currentPath, []);
  });
  tagsInput.addEventListener("keydown", (e) => {
    if (e.key === "Enter") {
      e.preventDefault();
      tagsStart.click();
    } else if (e.key === "Escape") {
      e.preventDefault();
      setState("idle");
    }
  });
}

window.addEventListener("DOMContentLoaded", () => {
  loadVersion();
  wireDragDrop();
  wireMenuEvents();
  wireUpdaterEvents();
  wireTagsScreen();
  wireLibrary();
  wireWelcome();
  maybeShowWelcome();
  refreshFooterStatus();
});
