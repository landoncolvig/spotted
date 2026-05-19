import { invoke, convertFileSrc } from "@tauri-apps/api/core";
import { listen, type UnlistenFn } from "@tauri-apps/api/event";
import { open } from "@tauri-apps/plugin-dialog";
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
  | { event: "tag-complete"; total: number }
  | { event: "markers-start"; total: number }
  | { event: "markers-video"; name: string; count: number; index: number; total: number }
  | { event: "markers-error"; name: string; message: string }
  | { event: "markers-complete"; total: number }
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

function showWelcome() {
  const w = document.getElementById("welcome");
  if (w) w.hidden = false;
}

function dismissWelcome(persist = true) {
  const w = document.getElementById("welcome");
  if (w) w.hidden = true;
  if (persist) {
    try { localStorage.setItem(WELCOME_KEY, "1"); } catch {}
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
      handleLibraryEvent(evt);
      break;
    case "library-person":
      handleLibraryEvent(evt);
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
  wireLibrary();
  wireWelcome();
  maybeShowWelcome();
  refreshFooterStatus();
});
