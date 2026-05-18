import { invoke } from "@tauri-apps/api/core";
import { listen, type UnlistenFn } from "@tauri-apps/api/event";
import { open } from "@tauri-apps/plugin-dialog";

type State = "idle" | "tags" | "working" | "label" | "done";
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
  | { event: "tag-complete"; total: number }
  | { event: "markers-start"; total: number }
  | { event: "markers-video"; name: string; count: number; index: number; total: number }
  | { event: "markers-error"; name: string; message: string }
  | { event: "markers-complete"; total: number }
  | { event: "library-stats"; videos: number; faces: number; clusters: number; named: number; people: { name: string; clips: number; faces: number }[] }
  | { event: "error"; stage: string; message: string };

const stage = document.getElementById("stage") as HTMLElement;
const dropzone = document.getElementById("dropzone") as HTMLElement;
const versionEl = document.getElementById("version") as HTMLElement;
const workingLabel = document.getElementById("working-label") as HTMLElement;
const workingPath = document.getElementById("working-path") as HTMLElement;
const workingDetail = document.getElementById("working-detail") as HTMLElement;
const progressBar = document.getElementById("progress-bar") as HTMLElement;
const doneSub = document.getElementById("done-sub") as HTMLElement;
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
  progressBar.style.width = `${Math.max(0, Math.min(100, pct))}%`;
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
      break;
    case "error":
    case "tag-error":
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
  setProgress(0);
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
    await invoke<number>("start_label_server", { port: LABEL_PORT });
    mountLabelScreen();
    setState("label");
  } catch (err) {
    showError(String(err));
  }
}

function showError(message: string) {
  setState("done");
  doneSub.textContent = message;
  doneSub.classList.add("done__sub--error");
}

function clearError() {
  doneSub.classList.remove("done__sub--error");
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
  setProgress(0);
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
  } catch (err) {
    showError(String(err));
  }
}

function renderDone(stats: SpottedEvent | null) {
  clearError();
  if (!stats || stats.event !== "library-stats" || stats.people.length === 0) {
    doneSub.textContent = "Open the folder in Premiere or DaVinci — search by name.";
    return;
  }
  const top = stats.people.slice(0, 6);
  const breakdown = top.map((p) => `${p.name} (${p.clips})`).join(" · ");
  const more = stats.people.length > top.length ? ` · +${stats.people.length - top.length} more` : "";
  doneSub.textContent = `Tagged ${stats.people.length} people across ${stats.videos} clips — ${breakdown}${more}`;
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

function isBusy(): boolean {
  const s = stage.getAttribute("data-state");
  return s === "working" || s === "label";
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
  listen("menu://check-updates", async () => {
    try {
      const newVersion = await invoke<string | null>("check_for_updates");
      if (!newVersion) {
        flashToast(`You're on the latest version.`);
      } else {
        flashToast(`Update to v${newVersion} is available — restart to install.`);
      }
    } catch (e) {
      flashToast(`Update check failed: ${e}`, true);
    }
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
  wireTagsScreen();
});
