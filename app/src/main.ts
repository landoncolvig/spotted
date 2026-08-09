import { invoke, convertFileSrc } from "@tauri-apps/api/core";
import { listen, type UnlistenFn } from "@tauri-apps/api/event";
import { open, save, confirm } from "@tauri-apps/plugin-dialog";
import { homeDir } from "@tauri-apps/api/path";
import {
  isPermissionGranted,
  requestPermission,
  sendNotification,
} from "@tauri-apps/plugin-notification";

type State = "idle" | "tags" | "working" | "label" | "review" | "library" | "done";
/** One clip a tag matched, individually rejectable on the review screen. */
type ShownClip = { path: string; name: string; score: number; thumb: string | null };
type MatchedTag = { tag: string; clips: number; peak: number; sample: string; shown?: ShownClip[] };
/** One energy bucket as the review screen shows it. Same shape as MatchedTag
 *  minus the score, so both render through the same row builder. */
type EnergyBucket = { tag: string; bucket: string; clips: number; sample: string; thumbs?: string[] };
type SidecarLine = { kind: "stdout" | "stderr"; line: string };
type SpottedEvent =
  | { event: "scan-start"; total: number }
  | { event: "video-start"; name: string; index: number; total: number; duration_sec?: number }
  | { event: "video-done"; name: string; index: number; total: number; faces: number }
  | { event: "video-skip"; name: string; index: number; total: number; reason?: string }
  | { event: "video-backfill"; name: string; index: number; total: number }
  | { event: "scan-complete"; total_faces: number; total_skipped: number; total_videos: number }
  | { event: "cluster-start"; faces: number }
  | { event: "cluster-complete"; clusters: number }
  | { event: "tag-start"; total: number }
  | { event: "tag-video"; name: string; names: string[]; index: number; total: number }
  | { event: "tag-error"; name: string; message: string }
  | { event: "tag-skip"; name: string; reason: string; index?: number; total?: number }
  | { event: "scan-not-downloaded"; clips: number; names: string[]; fatal: boolean; message: string }
  | { event: "report-complete"; path: string; clips: number }
  // Failures the user needs to hear about. Each one used to fall through the
  // event switch, so the step reported success it had not achieved.
  | { event: "finder-error"; name: string; message: string }
  | { event: "markers-skip"; name: string; reason: string }
  | { event: "energy-skip"; name: string; reason: string }
  | { event: "index-prune-error"; message: string }
  // Progress for the embedding backfill, which runs on any library first
  // scanned before activity tagging existed and showed nothing at all.
  | { event: "activity-backfill-start"; total: number }
  | { event: "activity-backfill"; name: string; index: number; total: number; embeddings: number }
  // Declared so they cannot be mistaken for an oversight, but deliberately
  // console-only: each reports something the UI already conveys elsewhere or
  // that the user has no decision to make about.
  | { event: "cluster-empty" }
  | { event: "cluster-skipped"; clusters: number }
  | { event: "index-pruned"; count: number }
  | { event: "person-thumbs-complete"; count: number; dir: string }
  | { event: "resolve-stale-removed"; path: string }
  | { event: "timeline-duplicate-skipped"; path: string; kept: string }
  | { event: "video-energy"; name: string; bucket: string; score: number; peaks: number }
  | { event: "tag-pruned"; pairs: number }
  | { event: "tag-prune-error"; message: string }
  | { event: "tag-empty"; message: string }
  | { event: "tag-failed"; failed: number; total: number; first: string }
  | { event: "tag-complete"; total: number }
  | { event: "markers-start"; total: number }
  | { event: "markers-video"; name: string; count: number; index: number; total: number }
  | { event: "markers-error"; name: string; message: string }
  | { event: "markers-complete"; total: number }
  | { event: "tag-verified"; file: string; xmp: string[]; keys: string[]; comment: string }
  | { event: "tag-verify-error"; message: string }
  | { event: "markers-verified"; file: string; event_count: number; in_file_present: boolean; sidecar_present: boolean; failed?: number; written?: number }
  | { event: "markers-verify-error"; message: string }
  | { event: "resolve-script"; path: string; clips: number }
  | { event: "markers-summary"; scanned: number; timeline_clips: number; marked_clips: number; skipped_missing: number; skipped_no_markers: number; timeline_fps?: number | null; source_rates?: Record<string, number> }
  // Containers exiftool can't write into (.mkv, .mts, .avi...). Not a failure:
  // the clip is on the timeline and its markers ride in the EDL, which is how
  // DaVinci reads them anyway. Only in-file embedding is unavailable.
  | { event: "markers-unwritable"; count: number; names: string[] }
  // Camera raw (.r3d, .braw...). Only the manufacturer's SDK can decode the
  // picture, so there is no frame to find a face in. Carries its own
  // explanation because the generic "no videos found" reads as a bug.
  | { event: "scan-camera-raw"; formats: string[]; message: string }
  | { event: "resolve-timeline"; path: string; clips: number }
  | { event: "resolve-edl"; path: string; clips: number }
  | { event: "resolve-script-error"; message: string }
  | { event: "markers-sidecar-error"; name: string; message: string }
  | { event: "activity-start"; total: number }
  | { event: "activity-complete"; total: number; tagged: number; sample: { file: string; tags: string[] } | null; matched?: MatchedTag[] }
  | { event: "energy-summary"; buckets: EnergyBucket[] }
  | { event: "activity-empty"; message: string }
  | { event: "activity-fallback"; reason: string; tags: string[]; clips: number }
  | { event: "activities-disabled"; reason: string }
  | { event: "library-stats"; videos: number; faces: number; clusters: number; named: number; people: { name: string; cluster_id?: number; clips: number; faces: number }[] }
  // Same payload as library-stats, but counting only the clips this run was
  // handed. Kept as a separate event so it can never reach the Library view.
  // `known: false` means the database could not say what the batch was, so
  // these are library totals and must not be presented as the batch.
  | { event: "batch-stats"; known?: boolean; videos: number; faces: number; clusters: number; named: number; people: { name: string; cluster_id?: number; clips: number; faces: number }[] }
  | { event: "library-person"; name: string; clips: { path: string; name: string; times: number[] }[] }
  | { event: "library-clip-index"; clips: { path: string; name: string; keywords: string[] }[] }
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
const btnReport = document.getElementById("btn-report") as HTMLButtonElement | null;
const tagsPath = document.getElementById("tags-path") as HTMLElement;
const tagsInput = document.getElementById("tags-input") as HTMLInputElement;
const tagsStart = document.getElementById("tags-start") as HTMLButtonElement;
const tagsSkip = document.getElementById("tags-skip") as HTMLButtonElement;
const tagsOverwrite = document.getElementById("tags-overwrite") as HTMLInputElement;
const tagsEnergy = document.getElementById("tags-energy") as HTMLInputElement;

const LABEL_PORT = 8765;
/** Tokenised labeler URL returned by start_label_server. */
let labelUrl: string | null = null;

/** Cache-bust the labeler without dropping its auth token.
 *  Defensive about what came back over IPC: this function runs at the very end
 *  of a batch, so anything it throws surfaces as "Couldn't finish" after the
 *  user has already waited through the whole scan. */
function labelFrameSrc(): string {
  const base =
    typeof labelUrl === "string" && labelUrl
      ? labelUrl
      : `http://127.0.0.1:${LABEL_PORT}/`;
  return base + (base.includes("?") ? "&" : "?") + `t=${Date.now()}`;
}
// Everything the user handed over this batch. A Finder multi-selection is a
// list, so this is a list; `currentPath` is the single-root scope hint the
// downstream --scope flag takes, and is null for a multi-path batch so those
// steps fall back to the full root list the scan persisted in the DB.
let currentPaths: string[] = [];
let currentPath: string | null = null;
// Whether the final tag-write replaces each clip's whole keyword set (start
// fresh) vs merging. Captured from the tags-screen checkbox when a run begins,
// because that screen is gone by the time keywords are written.
let overwriteKeywords = false;
/** Whether this batch scores energy at all. Captured from the tags screen the
 *  same way, and for the same reason: that screen is gone by write time.
 *
 *  Turning it off has to act at three stages, because a single one leaks. The
 *  scan stops computing it, which only covers clips scanned from here on. The
 *  keyword and the peak markers are ALSO suppressed at write time, because a
 *  clip scored on an earlier drop keeps its `energy_bucket` and its peaks in
 *  the index forever — so an opt-out that only passed `--no-energy` would
 *  still stamp energy on every clip the user had scanned before. */
let energyEnabled = true;
/** Energy buckets the user unchecked on the review screen. Distinct from
 *  energyEnabled: that is "never score this batch", this is "you scored it,
 *  but do not put low energy on my footage". */
let excludedEnergyBuckets: string[] = [];
/** [clip path, tag] pairs the user rejected for individual clips. Distinct
 *  from unchecking a tag, which drops it everywhere. */
let droppedClips: [string, string][] = [];

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
  const picked = await open({ directory: true, multiple: true });
  if (typeof picked === "string") askForTags([picked]);
  else if (Array.isArray(picked) && picked.length > 0) askForTags(picked);
}

/** What to show when a batch is a pile of files rather than one folder. */
function describeBatch(paths: string[]): string {
  if (paths.length === 1) return paths[0];
  const dir = paths[0].slice(0, paths[0].lastIndexOf("/")) || "/";
  return `${paths.length} items in ${dir}`;
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

function askForTags(paths: string[]) {
  currentPaths = paths;
  currentPath = paths.length === 1 ? paths[0] : null;
  tagsPath.textContent = describeBatch(paths);
  tagsInput.value = suggestTagsFromPath(paths[0]);
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
let lastBatchStats: SpottedEvent | null = null;
let lastVerification: Extract<SpottedEvent, { event: "tag-verified" }> | null = null;
let lastMarkersVerification: Extract<SpottedEvent, { event: "markers-verified" }> | null = null;
/** Why the marker step failed, if it did. Shown on the done screen so a broken
 *  marker run is reportable instead of silently rendering as "empty". */
let lastMarkerError: string | null = null;
/** Where the DaVinci marker script was written, if it was. */
let lastResolveScript: string | null = null;
/** Where the DaVinci FCPXML timeline was written. This is the path that
 *  actually works for users: File > Import > Timeline, markers included. */
let lastResolveTimeline: string | null = null;
/** Companion EDL carrying the markers themselves. */
let lastResolveEdl: string | null = null;
/** How many scanned clips actually reached the DaVinci timeline, and why the
 *  rest did not. A tester received a timeline holding 1 of her 170 clips with
 *  nothing on screen to explain it, and assumed DaVinci had dropped them. */
let lastMarkersSummary: Extract<SpottedEvent, { event: "markers-summary" }> | null = null;
/** Which clips exiftool could not write, and why. The sidecar reports these
 *  per clip and then exits non-zero if any of them failed, so a batch where
 *  106 of 107 clips tagged perfectly arrives at the UI as one error string.
 *  Keeping the individual failures means a run that half-worked can be
 *  reported as half-worked, and the user can go look at the clips named. */
let lastTagFailures: { name: string; message: string }[] = [];
/** The sidecar's own count of the damage, which is authoritative: the per-clip
 *  events can be truncated by a crash, this cannot. */
let lastTagFailed: Extract<SpottedEvent, { event: "tag-failed" }> | null = null;
/** Clips deliberately passed over — a container that cannot hold keywords is
 *  not a failure, and must not be counted as one. */
let lastTagSkips: { name: string; reason: string }[] = [];
/** Clips a cloud provider had evicted, skipped before the scan started.
 *  Kept so the Done screen can name them: someone who dropped 200 clips and
 *  got 140 needs to know the other 60 were not downloaded, not lost. */
let lastNotDownloaded: { count: number; names: string[] } | null = null;

/** Clips whose Finder tag or Spotlight comment could not be written. The
 *  keyword write can succeed while this fails, so the run looks clean and
 *  Finder search quietly does not find those clips. */
let lastFinderErrors: { name: string; message: string }[] = [];
/** Clips whose container cannot carry in-file markers. Not a failure — the
 *  same fact the tag side already reports as "in EDL only". */
let lastMarkerSkips: { name: string; reason: string }[] = [];
/** Clips that could not be scored for energy. */
let lastEnergySkips: { name: string; reason: string }[] = [];
/** Set when the index prune failed, which leaves stale rows behind. */
let lastIndexPruneError: string | null = null;

/** Set when the sidecar could not apply the per-clip rejections. Worth saying
 *  out loud: the user unchecked clips, the write went ahead anyway, and those
 *  tags are now in their files. Silence here looks like success. */
let lastPruneError: string | null = null;
// How many clips are in a container that can't hold in-file markers.
let lastUnwritable = 0;
// Explanation the sidecar sent for camera-raw footage, preferred over its exit text.
let lastCameraRaw: string | null = null;
/** Resolved once at startup; person thumbnails live under it. */
let homePath: string | null = null;
/** Set when the user cancels, so the resulting sidecar rejection isn't
 *  reported to them as a crash. */
let cancelled = false;
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

/** Counts for the clips this run was handed, for the finish line. The library
 *  totals are a different question and answering it there read as a bug: a
 *  user who dropped one clip was told Spotted had tagged 169. */
async function fetchBatchStats(): Promise<SpottedEvent | null> {
  lastBatchStats = null;
  try {
    await invoke<string>("fetch_status", { batch: true });
  } catch {}
  await new Promise((r) => setTimeout(r, 100));
  return lastBatchStats;
}

// Cross-phase state for the current batch. Reset at runBatch entry, read by
// the global sidecar event handler.
const batch = {
  scanTotal: 0,
  scanDone: 0,
  scanFaces: 0,
  tagTotal: 0,
  tagDone: 0,
  startedAt: 0,
  // The write phase gets its own clock. Sharing the scan's would date from
  // before the labeler, which the user spent an unbounded amount of time in,
  // and every estimate would be nonsense.
  tagStartedAt: 0,
};

/** " · about 12 min left" once there's enough history to mean anything.
 *  A long run with no time estimate reads as a hang. */
function eta(done: number, total: number, startedAt: number): string {
  if (!startedAt || done < 2 || total <= done) return "";
  const elapsed = Date.now() - startedAt;
  const remaining = (elapsed / done) * (total - done);
  const mins = Math.round(remaining / 60000);
  if (mins < 1) return " · under a minute left";
  if (mins < 60) return ` · about ${mins} min left`;
  const h = Math.floor(mins / 60);
  return ` · about ${h}h ${mins % 60}m left`;
}

function etaSuffix(): string {
  return eta(batch.scanDone, batch.scanTotal, batch.startedAt);
}

/** Writing runs exiftool twice and xattr twice per clip, so on a few hundred
 *  clips it is minutes of a moving bar over a filename with no position in it.
 *  The scan has said "12 of 107 · about 3 min left" for a while; this phase
 *  was still the one where someone would wonder whether it had stopped. */
function tagEtaSuffix(): string {
  return eta(batch.tagDone, batch.tagTotal, batch.tagStartedAt);
}

function handleSpottedEvent(evt: SpottedEvent) {
  switch (evt.event) {
    case "scan-start":
      batch.scanTotal = evt.total;
      batch.scanDone = 0;
      batch.startedAt = Date.now();
      batch.scanFaces = 0;
      workingLabel.textContent = "Spotting";
      workingDetail.textContent = `Reading ${evt.total} clips`;
      setProgress(2);
      break;
    case "video-start":
      if (batch.startedAt === 0) batch.startedAt = Date.now();
      workingDetail.textContent =
        `${evt.name} — clip ${evt.index} of ${evt.total}` + etaSuffix();
      break;
    case "video-done":
      batch.scanDone = evt.index;
      batch.scanFaces += evt.faces;
      if (batch.scanTotal > 0) {
        setProgress((batch.scanDone / batch.scanTotal) * 80);
      }
      workingDetail.textContent =
        `${batch.scanDone}/${batch.scanTotal} clips · ${batch.scanFaces} faces` +
        etaSuffix();
      break;
    case "video-skip":
    case "video-backfill":
      // Already-scanned clips emit these instead of video-done. Without
      // advancing here the bar froze at 2% for the whole pass.
      batch.scanDone = evt.index;
      if (batch.scanTotal > 0) {
        setProgress((batch.scanDone / batch.scanTotal) * 80);
      }
      workingDetail.textContent =
        `${batch.scanDone}/${batch.scanTotal} clips · already scanned` + etaSuffix();
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
      batch.tagDone = 0;
      batch.tagStartedAt = Date.now();
      workingLabel.textContent = "Writing keywords";
      workingDetail.textContent = `Tagging ${evt.total} clips…`;
      setProgress(0);
      break;
    case "tag-video":
      batch.tagDone = evt.index;
      workingDetail.textContent =
        `${evt.name} · ${evt.index}/${evt.total}` + tagEtaSuffix();
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
    case "batch-stats":
      // Deliberately not forwarded to the Library view. These are one drop's
      // numbers, and the sidebar would render them as the whole library.
      lastBatchStats = evt;
      break;
    case "tag-verified":
      lastVerification = evt;
      break;
    case "resolve-script":
      lastResolveScript = evt.path;
      break;
    case "markers-summary":
      lastMarkersSummary = evt;
      break;
    case "markers-unwritable":
      lastUnwritable = evt.count;
      break;
    case "scan-camera-raw":
      // The sidecar exits non-zero right after this, and the raw exit text is
      // not something to show anyone. Keep the explanation it sent instead.
      lastCameraRaw = evt.message;
      break;
    case "resolve-timeline":
      lastResolveTimeline = evt.path;
      break;
    case "resolve-edl":
      lastResolveEdl = evt.path;
      break;
    case "resolve-script-error":
      lastResolveScript = `failed: ${evt.message}`;
      break;
    case "markers-verified":
      lastMarkersVerification = evt;
      break;
    case "activity-start":
      workingLabel.textContent = "Spotting your tags";
      workingDetail.textContent = `Looking for your tags across ${evt.total} clips…`;
      break;
    case "activity-complete":
      lastActivityResult = evt;
      break;
    case "activity-empty":
    case "activities-disabled":
      // Non-fatal — face tagging still proceeds. Logged for devtools.
      console.info("activity step skipped:", evt);
      break;
    case "activity-fallback":
      // Couldn't match per clip (no embeddings / model), so every typed tag was
      // applied to every clip instead of being silently dropped. The review
      // screen surfaces them all; the working detail line notes why.
      workingDetail.textContent = `Couldn't match per clip (${evt.reason}); applied your ${evt.tags.length} tag(s) to every clip — prune next.`;
      break;
    case "library-person":
      handleLibraryEvent(evt);
      break;
    case "tag-error":
      // Kept, not just logged. The exit-code path gives one message for the
      // whole batch; these are what let the done screen name the clips.
      lastTagFailures.push({ name: evt.name, message: evt.message });
      console.warn(evt);
      break;
    case "tag-skip":
      lastTagSkips.push({ name: evt.name, reason: evt.reason });
      // A run of unwritable containers used to leave the bar parked wherever
      // the last writable clip left it.
      if (evt.index) {
        batch.tagDone = evt.index;
        workingDetail.textContent =
          `${evt.name} · ${evt.index}/${evt.total ?? batch.tagTotal}` + tagEtaSuffix();
        if (batch.tagTotal > 0) setProgress((evt.index / batch.tagTotal) * 100);
      }
      break;
    case "tag-failed":
      lastTagFailed = evt;
      console.warn(evt);
      break;
    case "report-complete":
      console.info(`report: ${evt.clips} clip(s) → ${evt.path}`);
      break;
    case "scan-not-downloaded":
      lastNotDownloaded = { count: evt.clips, names: evt.names };
      // The fatal case is surfaced by the sidecar's exit; this line covers the
      // partial one, where the scan carries on without them.
      if (!evt.fatal) workingDetail.textContent = evt.message;
      console.warn(evt);
      break;
    case "finder-error":
      lastFinderErrors.push({ name: evt.name, message: evt.message });
      console.warn(evt);
      break;
    case "markers-skip":
      lastMarkerSkips.push({ name: evt.name, reason: evt.reason });
      break;
    case "energy-skip":
      lastEnergySkips.push({ name: evt.name, reason: evt.reason });
      console.warn(evt);
      break;
    case "index-prune-error":
      lastIndexPruneError = evt.message;
      console.warn(evt);
      break;
    case "activity-backfill-start":
      workingLabel.textContent = "Catching up on older clips";
      workingDetail.textContent =
        `Indexing ${evt.total} clip${evt.total === 1 ? "" : "s"} scanned before tag matching existed…`;
      setProgressIndeterminate();
      break;
    case "activity-backfill":
      workingDetail.textContent = `${evt.name} · ${evt.index}/${evt.total}`;
      if (evt.total > 0) setProgress((evt.index / evt.total) * 100);
      break;
    // Deliberately console-only. See the union above for why each is here
    // rather than on screen.
    case "cluster-empty":
    case "cluster-skipped":
    case "index-pruned":
    case "person-thumbs-complete":
    case "resolve-stale-removed":
    case "timeline-duplicate-skipped":
    case "video-energy":
      console.info(evt);
      break;
    case "tag-pruned":
      console.info(`pruned ${evt.pairs} clip/tag pair(s)`);
      break;
    case "tag-prune-error":
      lastPruneError = evt.message;
      console.warn(evt);
      break;
    case "error":
    case "tag-empty":
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

async function runBatch(paths: string[], tags: string[] = []) {
  currentPaths = paths;
  currentPath = paths.length === 1 ? paths[0] : null;
  // Clear the camera-raw explanation from any previous drop. friendlyError
  // prefers it over the real error, so one folder of .r3d originals used to
  // make every later failure in the session — empty folder, busy port,
  // permissions — report itself as a RED problem.
  lastCameraRaw = null;
  setState("working");
  workingPath.textContent = describeBatch(paths);
  setProgressIndeterminate();
  workingLabel.textContent = "Spotting";
  workingDetail.textContent = "Looking for footage…";

  batch.scanTotal = 0;
  batch.scanDone = 0;
  batch.scanFaces = 0;
  batch.tagTotal = 0;
  batch.tagDone = 0;
  batch.tagStartedAt = 0;

  await ensureSidecarListener();

  try {
    await invoke<number>("scan_folder", { paths, tags, energy: energyEnabled });
    await invoke<number>("cluster_faces");
    workingLabel.textContent = "Naming people";
    workingDetail.textContent = "Opening labeler…";
    // Rust returns the URL including the per-session token; the labeler
    // rejects any request without it, so the iframe must use this exact URL.
    // Returns the labeler URL including its per-session token.
    labelUrl = await invoke<string>("start_label_server", {
      port: LABEL_PORT,
      scopePaths: currentPaths.length > 0 ? currentPaths : null,
    });
    mountLabelScreen();
    setState("label");
  } catch (err) {
    if (cancelled) {
      cancelled = false;
      setState("idle");
      flashToast("Cancelled.");
    } else {
      showError(String(err));
    }
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
  // Camera raw explains itself. The sidecar sends the reason and the way
  // through before it exits, and that beats anything derived from exit text.
  if (lastCameraRaw) return lastCameraRaw;
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
    return "Nothing was tagged — name at least one face cluster, or enter tags that actually appear in the clips, before hitting Tag & finish.";
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
  if (
    r.includes("could not create temporary directory") ||
    r.includes("local working folder") ||
    r.includes("local cache")
  ) {
    return "Spotted couldn't prepare its local working folder. Quit and reopen Spotted, then try again. If it still happens, check that your Mac has free disk space and send this screen.";
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
  const existing = document.querySelector(".screen--label");
  if (existing) {
    // Reuse the screen, but force the iframe to reload. A new batch serves
    // fresh clusters on the same port; setting the same URL wouldn't reload,
    // so without the cache-buster we'd show the previous batch's named faces.
    const frame = existing.querySelector(".label-frame") as HTMLIFrameElement | null;
    if (frame) frame.src = labelFrameSrc();
    return;
  }

  const screen = makeEl("div", "screen screen--label");
  const wrap = makeEl("div", "label-wrap");

  const frame = makeEl("iframe", "label-frame");
  frame.src = labelFrameSrc();
  frame.title = "Spotted labeler";

  const bar = makeEl("div", "label-bar");
  const hint = makeEl("span", "label-hint");
  hint.textContent = "Name each cluster, then:";
  const tagBtn = makeEl("button", "btn");
  tagBtn.id = "btn-tag";
  tagBtn.textContent = "Tag & finish";
  // The labeling screen used to be a dead end: its only control was
  // "Tag & finish" and every other action was blocked as busy, so a user who
  // dropped the wrong folder or wanted to stop naming 95 people had to quit
  // the app. Names autosave, so leaving is safe.
  const backBtn = makeEl("button", "btn btn--ghost");
  backBtn.id = "btn-label-back";
  backBtn.textContent = "Finish later";
  backBtn.addEventListener("click", async () => {
    const ok = await confirm(
      "Leave naming for now?\n\nThe names you've typed are saved. You can pick up where you left off by dropping the same folder again.",
    );
    if (!ok) return;
    setState("idle");
    refreshFooterStatus();
  });
  bar.append(hint, backBtn, tagBtn);

  wrap.append(frame, bar);
  screen.appendChild(wrap);
  stage.appendChild(screen);

  // Wrapped deliberately: passing startTagFlow directly hands the MouseEvent
  // in as allClips, which is truthy, so every click would re-tag the whole
  // library instead of this batch.
  tagBtn.addEventListener("click", () => { void startTagFlow(false); });
}

// After faces are named: first look for each of the user's own tags in every
// clip, then hand them a review screen to drop any bad matches BEFORE anything
// is written. This is the tag-side mirror of naming faces — the user supplied
// the words, the app found where they apply, and the user confirms.
//
// If nothing matched (no tags entered, or none crossed the threshold) we skip
// the review and write face names straight away.
async function startTagFlow(allClips = false) {
  setState("working");
  setProgressIndeterminate();
  workingLabel.textContent = "Spotting your tags";
  workingDetail.textContent = "Looking for each of your tags in every clip…";
  // Read matches from the invoke RESULT (the sidecar's captured stdout), not
  // from the activity-complete event global. The event can be delivered after
  // the invoke promise resolves, and reading the global too early would see
  // no matches, silently skip the review, and write tags unconfirmed — the
  // exact failure the review screen exists to prevent.
  let matched: MatchedTag[] = [];
  let energy: EnergyBucket[] = [];
  excludedEnergyBuckets = [];
  droppedClips = [];
  try {
    const out = await invoke<string>("suggest_activities", { scope: currentPath ?? null, allClips: false });
    matched = parseMatchedTags(out);
    energy = energyEnabled ? parseEnergyBuckets(out) : [];
  } catch (e) {
    console.warn("activity suggest failed (non-fatal):", e);
  }
  // Energy alone is reason enough to stop and ask. Someone who typed no tags
  // still had their footage scored, and used to reach the write with nothing
  // to confirm.
  if (matched.length === 0 && energy.length === 0) {
    await runWrite([], allClips);
    return;
  }
  renderReview(matched, energy);
  setState("review");
}

/** Pull the energy buckets out of the energy-summary line, read from the same
 *  captured stdout and for the same reason as parseMatchedTags: the event
 *  global can land after the invoke promise resolves, and reading it early
 *  would skip the review and write unconfirmed. */
function parseEnergyBuckets(stdout: string): EnergyBucket[] {
  for (const line of stdout.split("\n")) {
    const evt = parseSpotted(line.trim());
    if (evt && evt.event === "energy-summary" && Array.isArray(evt.buckets)) {
      return evt.buckets;
    }
  }
  return [];
}

/** Pull the `matched` array out of the activity-complete line in the sidecar's
 *  captured stdout. Returns [] if the step emitted no matches. */
function parseMatchedTags(stdout: string): MatchedTag[] {
  for (const line of stdout.split("\n")) {
    const evt = parseSpotted(line.trim());
    if (evt && evt.event === "activity-complete" && Array.isArray(evt.matched)) {
      return evt.matched;
    }
  }
  return [];
}

/** Confirm screen: the tags Spotted found, each checked by default. Unchecking
 *  one drops it from every clip; only checked tags get written. */
function renderReview(matched: MatchedTag[], energy: EnergyBucket[] = []) {
  document.querySelector(".screen--review")?.remove();
  // Re-rendering the screen rebuilds every control, so any rejection recorded
  // against the old DOM is stale.
  droppedClips = [];

  const screen = makeEl("div", "screen screen--review");
  const wrap = makeEl("div", "review-wrap");

  const title = makeEl("h2", "review-title");
  title.textContent = "Review your tags";
  const sub = makeEl("p", "review-sub");
  sub.textContent =
    "Spotted looked for your tags in each clip. Uncheck any that look wrong — only the checked ones get written into your files.";
  wrap.append(title, sub);

  const tools = makeEl("div", "review-tools");
  const count = makeEl("span", "review-count");
  const toggle = makeEl("button", "review-toggle");
  tools.append(count, toggle);
  wrap.appendChild(tools);

  const list = makeEl("div", "review-list");
  const checks: HTMLInputElement[] = [];
  const energyChecks: HTMLInputElement[] = [];
  for (const m of matched) {
    const row = makeEl("label", "review-row");
    const input = document.createElement("input");
    input.type = "checkbox";
    input.checked = true;
    input.className = "review-check";
    input.dataset.tag = m.tag;
    const main = makeEl("span", "review-row__main");
    const name = makeEl("span", "review-row__tag");
    name.textContent = m.tag;
    const meta = makeEl("span", "review-row__meta");
    meta.textContent = `${m.clips} clip${m.clips === 1 ? "" : "s"} · e.g. ${m.sample}`;
    main.append(name, meta);
    row.append(input, main);
    // The clips this tag matched, each one rejectable on its own. Dropping a
    // whole tag says "beach was never right"; this says "beach is right, just
    // not in this clip", which previously could not be said at all.
    //
    // These are the WEAKEST matches, not the strongest: a user opening this
    // row is looking for the ones to remove, and the top scorers are the ones
    // most likely correct.
    if (m.shown && m.shown.length) {
      const thumbs = makeEl("div", "review-row__thumbs");
      for (const clip of m.shown) {
        const cell = makeEl("button", "review-clip");
        cell.type = "button";
        cell.title = `${clip.name} · ${clip.score}\nClick to keep this tag off this clip`;
        if (clip.thumb) {
          const img = document.createElement("img");
          img.className = "review-thumb";
          img.src = clip.thumb;
          img.alt = `${m.tag} in ${clip.name}`;
          img.loading = "lazy";
          cell.appendChild(img);
        } else {
          // No frame could be extracted. Still rejectable — losing the preview
          // must not also lose the control.
          const ph = makeEl("span", "review-thumb review-thumb--missing");
          ph.textContent = clip.name.slice(0, 12);
          cell.appendChild(ph);
        }
        cell.addEventListener("click", () => {
          const on = cell.classList.toggle("is-dropped");
          if (on) droppedClips.push([clip.path, m.tag]);
          else {
            const i = droppedClips.findIndex(
              ([p, t]) => p === clip.path && t === m.tag,
            );
            if (i >= 0) droppedClips.splice(i, 1);
          }
          updateCount();
        });
        thumbs.appendChild(cell);
      }
      row.appendChild(thumbs);
      // Never let a cap masquerade as the whole set. Someone who prunes the
      // six shown and sees the tag still land on 40 clips would reasonably
      // conclude the pruning does not work.
      if (m.clips > m.shown.length) {
        const more = makeEl("span", "review-row__more");
        more.textContent =
          `showing the ${m.shown.length} weakest of ${m.clips} · uncheck the tag to drop it from all`;
        main.appendChild(more);
      }
    }
    list.appendChild(row);
    checks.push(input);
  }
  wrap.appendChild(list);

  // Energy gets its own section rather than sitting among the tags. It is not
  // something the user asked for by name — it was scored for them — so it
  // needs the sentence explaining what it is.
  if (energy.length) {
    const head = makeEl("div", "review-section");
    head.textContent = "Energy";
    const note = makeEl("p", "review-sub");
    note.textContent =
      "Spotted scored how lively each clip is and dropped a marker on its peak moments. Uncheck a level to keep it off your footage.";
    wrap.append(head, note);

    const elist = makeEl("div", "review-list");
    for (const b of energy) {
      const row = makeEl("label", "review-row");
      const input = document.createElement("input");
      input.type = "checkbox";
      input.checked = true;
      input.className = "review-check";
      input.dataset.bucket = b.bucket;
      const main = makeEl("span", "review-row__main");
      const name = makeEl("span", "review-row__tag");
      name.textContent = b.tag;
      const meta = makeEl("span", "review-row__meta");
      meta.textContent = `${b.clips} clip${b.clips === 1 ? "" : "s"} · e.g. ${b.sample}`;
      main.append(name, meta);
      row.append(input, main);
      if (b.thumbs && b.thumbs.length) {
        const thumbs = makeEl("div", "review-row__thumbs");
        for (const src of b.thumbs) {
          const img = document.createElement("img");
          img.className = "review-thumb";
          img.src = src;
          img.alt = b.tag;
          img.loading = "lazy";
          thumbs.appendChild(img);
        }
        row.appendChild(thumbs);
      }
      elist.appendChild(row);
      energyChecks.push(input);
    }
    wrap.appendChild(elist);
  }

  const updateCount = () => {
    const all = checks.concat(energyChecks);
    const kept = all.filter((c) => c.checked).length;
    const pruned = droppedClips.length
      ? ` · ${droppedClips.length} clip${droppedClips.length === 1 ? "" : "s"} dropped`
      : "";
    count.textContent =
      `${kept} of ${all.length} tag${all.length === 1 ? "" : "s"} will be written${pruned}`;
    toggle.textContent = kept > 0 ? "Uncheck all" : "Check all";
  };
  checks.forEach((c) => c.addEventListener("change", updateCount));
  energyChecks.forEach((c) => c.addEventListener("change", updateCount));
  toggle.addEventListener("click", () => {
    const all = checks.concat(energyChecks);
    const anyChecked = all.some((c) => c.checked);
    all.forEach((c) => (c.checked = !anyChecked));
    updateCount();
  });
  updateCount();

  const bar = makeEl("div", "review-bar");
  const hint = makeEl("span", "review-hint");
  hint.textContent = "The faces you named are always written.";
  const writeBtn = makeEl("button", "btn");
  writeBtn.textContent = "Write tags";
  bar.append(hint, writeBtn);
  wrap.appendChild(bar);

  writeBtn.addEventListener("click", () => {
    const exclude = checks.filter((c) => !c.checked).map((c) => c.dataset.tag!);
    excludedEnergyBuckets = energyChecks
      .filter((c) => !c.checked)
      .map((c) => c.dataset.bucket!);
    runWrite(exclude);
  });

  screen.appendChild(wrap);
  stage.appendChild(screen);
}

async function runWrite(excludeTags: string[], allClips = false) {
  cancelled = false;
  lastRunWasLibraryWide = allClips;
  // Clear both the state and the rendered panel. Resetting only the globals
  // left the previous run's green paths visible when this run failed before
  // renderVerification() was called.
  lastVerification = null;
  lastMarkersVerification = null;
  lastResolveScript = null;
  lastResolveTimeline = null;
  lastResolveEdl = null;
  lastMarkerError = null;
  lastMarkersSummary = null;
  lastTagFailures = [];
  lastTagFailed = null;
  lastTagSkips = [];
  lastFinderErrors = [];
  lastNotDownloaded = null;
  lastMarkerSkips = [];
  lastEnergySkips = [];
  lastIndexPruneError = null;
  lastPruneError = null;
  lastUnwritable = 0;
  lastCameraRaw = null;
  document.getElementById("done-verify")?.replaceChildren();
  setState("working");
  setProgressIndeterminate();
  workingLabel.textContent = "Writing keywords";
  workingDetail.textContent = "Running exiftool, per clip…";

  // A source clip can reject in-file metadata while the batch-level DaVinci
  // exports remain perfectly writable. Keep the phases independent so one
  // locked/iCloud clip cannot suppress the FCPXML and EDL for the whole batch.
  let tagWriteError: string | null = null;
  try {
    try {
      await invoke<number>("tag_videos", {
        excludeTags,
        // A clip scored on an earlier drop keeps its bucket in the index, so
        // opting out has to suppress the keyword here as well as skip the
        // scoring pass. Without this the checkbox would look like it worked on
        // a fresh folder and do nothing on a re-drop.
        energy: energyEnabled,
        excludeEnergy: excludedEnergyBuckets,
        // Rejections for individual clips. A tag unchecked entirely is already
        // in excludeTags; these are the ones kept but wrong in one place.
        dropPairs: droppedClips,
        overwrite: overwriteKeywords,
        scope: currentPath ?? null,
        allClips,
      });
    } catch (e) {
      if (cancelled) {
        cancelled = false;
        setState("idle");
        flashToast("Cancelled.");
        return;
      }
      tagWriteError = String(e);
    }

    workingLabel.textContent = "Writing timeline markers";
    workingDetail.textContent = "For Premiere & DaVinci scrubber…";
    try {
      lastMarkerError = null;
      await invoke<number>("write_markers", {
        scope: currentPath ?? null,
        allClips,
        energy: energyEnabled,
        excludeEnergy: excludedEnergyBuckets,
      });
    } catch (e) {
      if (cancelled) {
        cancelled = false;
        setState("idle");
        flashToast("Cancelled.");
        return;
      }
      // Marker failures stay visible even when keyword writing succeeded.
      lastMarkerError = String(e);
      console.warn("marker write failed (non-fatal):", e);
    }

    setProgress(100);
    // The batch, not the library. Re-tag Library is the one run that really is
    // about the whole index; scoping it to the last drop would headline "1
    // clip" over a coverage row reading "169 of 169".
    const stats = allClips
      ? await fetchLibraryStats()
      : ((await fetchBatchStats()) ?? (await fetchLibraryStats()));
    setState("done");
    if (tagWriteError) {
      showError(tagWriteError);
      // "Couldn't finish" is the right headline only when nothing landed. One
      // unwritable clip in a batch of 107 used to read exactly the same as all
      // 107 failing, which sends someone to re-run a batch that was fine.
      const partial = tagWriteWasPartial();
      if (partial || lastResolveTimeline || lastResolveEdl) {
        doneTitle.textContent = "Finished with issues";
        doneSub.textContent = [
          partial,
          partial ? "" : friendlyError(tagWriteError),
          lastResolveTimeline || lastResolveEdl
            ? "The DaVinci files were still created below."
            : "",
        ].filter(Boolean).join(" ");
      }
      renderVerification();
      notifyIfBackground(
        "Spotted finished with issues",
        partial ||
          (lastResolveTimeline || lastResolveEdl
            ? "DaVinci files were created, but some clip metadata could not be written."
            : "Some clip metadata and timeline files could not be written."),
      );
    } else {
      renderDone(stats);
      notifyIfBackground("Spotted finished", summarizeDone(stats));
    }
  } catch (err) {
    if (cancelled) {
      cancelled = false;
      setState("idle");
      flashToast("Cancelled.");
    } else {
      showError(String(err));
    }
  }
}

/** "Tagged 106 of 107 clips." when some of the batch survived, else "".
 *
 *  Only the sidecar's own tally is trusted for this. Counting the per-clip
 *  tag-error events instead would understate the damage whenever the sidecar
 *  died partway, and overstating success is the one direction that matters
 *  here: it sends someone away believing footage is tagged when it is not. */
function tagWriteWasPartial(): string {
  const f = lastTagFailed;
  if (!f || f.total <= 0 || f.failed <= 0 || f.failed >= f.total) return "";
  const ok = f.total - f.failed;
  return `Tagged ${ok} of ${f.total} clips. ${f.failed} could not be written.`;
}

/** library-stats and batch-stats carry the same payload; the finish line takes
 *  whichever it was given and does not care which query produced it. */
function isStats(
  s: SpottedEvent | null,
): s is Extract<SpottedEvent, { event: "library-stats" | "batch-stats" }> {
  if (!s) return false;
  if (s.event === "library-stats") return true;
  // Batch counts the engine could not confine to this run are library totals
  // wearing the batch's name. Say nothing rather than say them.
  return s.event === "batch-stats" && s.known !== false;
}

function summarizeDone(stats: SpottedEvent | null): string {
  if (!isStats(stats) || stats.people.length === 0) {
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
  if (isStats(stats) && stats.people.length === 0) {
    // Nobody Spotted knows turned up in this batch. Say so, with the clip
    // count, rather than falling through to advice that reads like a result.
    const n = stats.videos;
    doneSub.textContent =
      `${n} clip${n === 1 ? "" : "s"} processed. No named faces in ${n === 1 ? "it" : "them"}. ` +
      `Open the folder in Premiere or DaVinci to see the keywords and timeline.`;
  } else if (!isStats(stats)) {
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

  const v = lastVerification;
  const markers = lastMarkersVerification;
  const markerCells: string[] = [];
  if (markers) {
    if (markers.in_file_present) markerCells.push(`in-file (${markers.event_count})`);
    if (markers.sidecar_present) markerCells.push(`sidecar .xmp`);
    // A partial failure is not the same as "nothing landed" — say how many
    // clips were skipped rather than letting the row read as empty.
    if (markers.failed) markerCells.push(`${markers.failed} clip(s) failed`);
  }
  // Formats exiftool can't write into. Named plainly and kept out of the
  // failure count, because those clips ARE on the timeline with their markers
  // in the EDL. Calling it a failure made a working run look broken.
  if (lastUnwritable > 0) {
    markerCells.push(`${lastUnwritable} in EDL only (format can't embed)`);
  }
  if (!markerCells.length && lastMarkerError) {
    markerCells.push(`failed: ${lastMarkerError.slice(0, 120)}`);
  }

  // Where the DaVinci script landed. Users cannot find this on their own —
  // it goes into ~/Library, which Spotlight does not index — so show the path.
  const resolveCells: string[] = [];
  if (lastResolveTimeline) resolveCells.push(lastResolveTimeline);
  else if (lastResolveScript) resolveCells.push(lastResolveScript);
  if (lastResolveEdl) resolveCells.push(lastResolveEdl);

  // How much of the batch the timeline actually covers. Silence here reads as
  // "all of it", so a short timeline has to announce itself and say why.
  const coverageCells: string[] = [];
  const ms = lastMarkersSummary;
  if (ms) {
    coverageCells.push(`${ms.timeline_clips} of ${ms.scanned} clip(s)`);
    coverageCells.push(`${ms.marked_clips} carrying markers`);
    if (ms.skipped_missing > 0) {
      coverageCells.push(`${ms.skipped_missing} no longer on disk`);
    }
    // A batch shot at more than one frame rate is worth naming. It is the
    // only thing that can leave a clip padded, and saying so here beats
    // asking someone to go read frame rates off their own footage.
    const rates = Object.keys(ms.source_rates ?? {});
    if (rates.length > 1 && ms.timeline_fps) {
      coverageCells.push(`${rates.length} frame rates, timeline at ${ms.timeline_fps}fps`);
    }
  }

  const hasEvidence = Boolean(
    v ||
    markers ||
    lastMarkerError ||
    resolveCells.length ||
    coverageCells.length ||
    lastTagFailures.length ||
    lastTagSkips.length ||
    lastNotDownloaded ||
    lastFinderErrors.length ||
    lastMarkerSkips.length ||
    lastEnergySkips.length ||
    lastIndexPruneError ||
    lastPruneError
  );
  if (!hasEvidence) return;

  const header = document.createElement("div");
  header.className = "done-verify__header";
  header.textContent = v
    ? `Verified on ${v.file}`
    : (lastResolveTimeline || lastResolveEdl)
      ? "DaVinci files created"
      : "Current marker results";
  panel.appendChild(header);

  // "empty" next to a warning sign reads as "this step failed". Most of the
  // time it means the step ran and found nothing to do, which is a different
  // sentence — a tester read a clean run as the app doing nothing at all.
  // `note` says which one it was; rows carrying one render as a plain fact.
  // `bad` inverts the row's reading. Every other row here is evidence that a
  // step worked, so empty means something is wrong and gets a warning. A row
  // listing failures means the opposite: empty is the good case, and it is
  // dropped rather than rendered as "⚠ Clips that failed: empty".
  const rows: Array<{
    label: string;
    values: string[];
    help: string;
    note?: string;
    bad?: boolean;
  }> = [];
  if (v) {
    rows.push({
      label: "Keys (DaVinci, Finder)",
      values: v.keys,
      help: "Read by DaVinci Resolve's Keywords column and macOS Spotlight search.",
    });
    rows.push({
      label: "XMP (Premiere)",
      values: v.xmp,
      help: "Read by Adobe Premiere Pro's Keywords column.",
    });
    rows.push({
      label: "Spotlight Comment",
      values: v.comment ? [v.comment] : [],
      help: "Shown in Get Info → Comments. iCloud-synced files may strip this; Keys above keeps search working anyway.",
    });
  }
  rows.push(
    {
      label: "Markers (timeline)",
      values: markerCells,
      // Only when nothing was there to mark. A batch whose named clips have
      // moved off disk also lands on marked_clips === 0, and calling that
      // "no named face in this batch" contradicts the coverage row below it,
      // which is busy saying those clips are missing.
      note: ms && ms.marked_clips === 0 && ms.skipped_missing === 0 && !lastMarkerError
        ? "no named face or energy peak in this batch"
        : undefined,
      help: "Per-face timeline markers. In-file XMP for Premiere; sidecar .xmp next to the clip for DaVinci (enable 'Use Sidecar Files' in project settings).",
    },
    {
      label: "Timeline covers",
      values: coverageCells,
      help: "Every clip in this batch goes on the DaVinci timeline, in filename order. Markers land on the ones with a named face or an energy peak. Clips Spotted has indexed but can no longer find on disk are left off, because Resolve would show them as Media Offline.",
    },
    {
      label: "DaVinci files",
      values: resolveCells,
      help: "Import the FCPXML with File > Import > Timeline, then import the EDL with Timelines > Import > Timeline Markers.",
    },
    // Named, not counted. "3 clips failed" leaves someone scrolling a folder
    // guessing which three; the filenames are what makes it actionable. Capped
    // because a systemic failure names every clip in the batch, and the row
    // says how many it is holding back rather than quietly truncating.
    {
      // Someone who dropped 200 clips and saw 140 tagged needs to know the
      // other 60 are on a server, not missing.
      label: "Not downloaded",
      values: lastNotDownloaded
        ? lastNotDownloaded.names.slice(0, 8).concat(
            lastNotDownloaded.count > lastNotDownloaded.names.length
              ? [`+${lastNotDownloaded.count - lastNotDownloaded.names.length} more`]
              : [],
          )
        : [],
      help: "iCloud Drive, Dropbox and OneDrive keep a placeholder until something opens the file. Download these in Finder and drop the folder again.",
      bad: true,
    },
    {
      // The keyword write can succeed while this fails, so without a row here
      // the run reports clean and Finder search silently misses those clips.
      label: "Finder tags that failed",
      values: lastFinderErrors.slice(0, 8).map((t) => t.name).concat(
        lastFinderErrors.length > 8 ? [`+${lastFinderErrors.length - 8} more`] : [],
      ),
      help: lastFinderErrors.length
        ? `Spotlight and Finder search will not find these by name. First reason: ${lastFinderErrors[0].message}`
        : "",
      bad: true,
    },
    {
      label: "Skipped (can't hold markers)",
      values: lastMarkerSkips.slice(0, 8).map((t) => t.name).concat(
        lastMarkerSkips.length > 8 ? [`+${lastMarkerSkips.length - 8} more`] : [],
      ),
      help: "These containers cannot carry in-file markers. They are still in the DaVinci timeline and EDL.",
      bad: true,
    },
    {
      label: "Not scored for energy",
      values: lastEnergySkips.slice(0, 8).map((t) => t.name).concat(
        lastEnergySkips.length > 8 ? [`+${lastEnergySkips.length - 8} more`] : [],
      ),
      help: lastEnergySkips.length
        ? `No energy keyword or peak markers on these. First reason: ${lastEnergySkips[0].reason}`
        : "",
      bad: true,
    },
    {
      label: "Index cleanup",
      values: lastIndexPruneError ? ["failed — stale clips may still be listed"] : [],
      help: lastIndexPruneError
        ? `Spotted could not forget clips that left the disk: ${lastIndexPruneError}`
        : "",
      bad: true,
    },
    {
      label: "Clips you unchecked",
      values: lastPruneError ? ["not applied — the tags were still written"] : [],
      help: lastPruneError
        ? `Spotted could not read the list of clips you unchecked: ${lastPruneError}`
        : "",
      bad: true,
    },
    {
      label: "Clips that failed",
      values: lastTagFailures.slice(0, 8).map((t) => t.name).concat(
        lastTagFailures.length > 8 ? [`+${lastTagFailures.length - 8} more`] : [],
      ),
      help: lastTagFailures.length
        ? `Keywords could not be written into these. First reason: ${lastTagFailures[0].message}`
        : "",
      bad: true,
    },
    {
      label: "Skipped (can't hold keywords)",
      values: lastTagSkips.slice(0, 8).map((t) => t.name).concat(
        lastTagSkips.length > 8 ? [`+${lastTagSkips.length - 8} more`] : [],
      ),
      help: "These containers cannot carry in-file keywords. They are still scanned, named, and placed in the DaVinci timeline.",
      bad: true,
    },
    {
      label: "Your tags (matched)",
      values: lastActivityResult?.sample?.tags ?? [],
      note: lastActivityResult && !lastActivityResult.sample?.tags?.length
        ? "none of your tags matched this batch"
        : undefined,
      help: "The tags you entered, matched per clip with Apple's MobileCLIP and confirmed by you on the review screen. Each lands only on the clips it was found in, the same way a face name does.",
    },
  );

  for (const row of rows) {
    const filled = row.values.length > 0;
    if (row.bad && !filled) continue;  // nothing failed: say nothing
    const explained = !filled && Boolean(row.note);
    const div = document.createElement("div");
    div.className =
      "done-verify__row " +
      (row.bad ? "is-empty" : filled ? "is-ok" : explained ? "is-none" : "is-empty");
    const dot = document.createElement("span");
    dot.className = "done-verify__dot";
    dot.textContent = row.bad ? "⚠" : filled ? "✓" : explained ? "–" : "⚠";
    const label = document.createElement("span");
    label.className = "done-verify__label";
    label.textContent = row.label;
    const value = document.createElement("span");
    value.className = "done-verify__value";
    value.textContent = filled ? row.values.join(", ") : (row.note ?? "empty");
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
  if (currentPaths.length > 0) {
    try {
      await invoke("reveal_in_finder", { path: currentPaths[0] });
    } catch (e) {
      console.warn("reveal failed:", e);
    }
  }
});

/** Whether the last run was a library-wide re-tag, so the report covers what
 *  the Done screen just described rather than silently widening to everything. */
let lastRunWasLibraryWide = false;

btnReport?.addEventListener("click", async () => {
  const target = await save({
    title: "Save report",
    defaultPath: "spotted-report.csv",
    filters: [{ name: "CSV", extensions: ["csv"] }],
  });
  if (!target) return;  // cancelled
  const label = btnReport.textContent;
  btnReport.disabled = true;
  btnReport.textContent = "Saving…";
  try {
    await invoke<number>("write_report", {
      out: target,
      scope: lastRunWasLibraryWide ? null : (currentPath ?? null),
      allClips: lastRunWasLibraryWide,
    });
    flashToast("Report saved.");
  } catch (e) {
    // A failed export must say so. The user asked for a file and would
    // otherwise go looking for one that is not there.
    showError(String(e));
  } finally {
    btnReport.disabled = false;
    btnReport.textContent = label;
  }
});

const btnCancel = document.getElementById("btn-cancel") as HTMLButtonElement | null;
btnCancel?.addEventListener("click", async () => {
  const ok = await confirm("Stop the current batch? Anything already detected stays in the index.");
  if (!ok) return;
  // The killed sidecar rejects with its stderr tail; without this the user's
  // deliberate cancel lands on a red "Couldn't finish" crash screen.
  cancelled = true;
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

type LibraryClipIndexEntry = { path: string; name: string; keywords: string[] };

const library: {
  people: LibraryPerson[];
  selected: string | null;
  loading: boolean;
  clipIndex: LibraryClipIndexEntry[];
} = {
  people: [],
  selected: null,
  loading: false,
  clipIndex: [],
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
  } else if (evt.event === "library-clip-index") {
    library.clipIndex = evt.clips;
    // Re-render in case there's an active search query awaiting matches.
    renderLibraryDetail();
  }
}

function personThumbUrl(p: LibraryPerson): string | null {
  if (p.clusterId == null || !homePath) return null;
  // ~/.facetag/person_thumbs/<cluster_id>.jpg via the Tauri asset protocol.
  // This used to build "/Users/" + a metadata field that does not exist in
  // Tauri v2, so it always fell through to a hardcoded "/Users/qb" and every
  // thumbnail 404'd on every machine except the developer's.
  return convertFileSrc(`${homePath}/.facetag/person_thumbs/${p.clusterId}.jpg`);
}

function librarySearchQuery(): string {
  return (libraryEl<HTMLInputElement>("library-search").value || "")
    .toLowerCase()
    .trim();
}

function matchingClipsForQuery(query: string): LibraryClipIndexEntry[] {
  if (!query) return [];
  return library.clipIndex.filter((c) =>
    c.keywords.some((kw) => kw.toLowerCase().includes(query))
  );
}

function renderLibrarySidebar() {
  const list = libraryEl<HTMLUListElement>("library-people");
  list.className = "library-people";
  list.replaceChildren();
  const filter = librarySearchQuery();
  // People filter: name match OR appears in any matching clip's keywords.
  // The second clause means typing "wedding" surfaces every person who
  // appears in wedding-tagged clips, not just people literally named
  // "Wedding".
  const matchingClipPaths = new Set(
    matchingClipsForQuery(filter).map((c) => c.path)
  );
  const filtered = filter
    ? library.people.filter((p) => {
        if (p.name.toLowerCase().includes(filter)) return true;
        // Cross-reference: does this person appear in any matching clip?
        return p.clipsDetail.some((c) => matchingClipPaths.has(c.path));
      })
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
  const query = librarySearchQuery();
  if (!library.selected) {
    // No person picked, but if a search query is active, show clip
    // matches instead of the generic empty state.
    if (query) {
      renderLibraryClipSearchResults(query);
      personEl.hidden = true;
      emptyEl.hidden = true;
      return;
    }
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
  // Hide the search-results panel when a person is selected (their
  // detail view takes over the main pane).
  const searchPanel = document.getElementById("library-clip-search");
  if (searchPanel) searchPanel.hidden = true;

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

/** Render the cross-clip search results pane. Shows when the user has a
 *  query active but no person selected — bridges the gap that used to
 *  force them out to Finder/DaVinci to find footage by keyword. */
function renderLibraryClipSearchResults(query: string): void {
  let panel = document.getElementById("library-clip-search");
  if (!panel) {
    panel = document.createElement("div");
    panel.id = "library-clip-search";
    panel.className = "library-clip-search";
    const detail = document.querySelector(".library-detail");
    detail?.appendChild(panel);
  }
  panel.hidden = false;
  panel.replaceChildren();

  const matches = matchingClipsForQuery(query);
  const header = document.createElement("div");
  header.className = "library-clip-search__header";
  header.textContent = matches.length === 0
    ? `No clips tagged with "${query}".`
    : `${matches.length} clip${matches.length === 1 ? "" : "s"} tagged with "${query}"`;
  panel.appendChild(header);

  if (matches.length === 0) {
    const hint = document.createElement("div");
    hint.className = "library-clip-search__hint";
    hint.textContent = "Try a person's name, an activity (wedding, beach), or a batch tag.";
    panel.appendChild(hint);
    return;
  }

  const list = document.createElement("ul");
  list.className = "library-clip-search__list";
  for (const c of matches.slice(0, 200)) {
    const li = document.createElement("li");
    li.className = "library-clip-search__row";

    const name = document.createElement("span");
    name.className = "library-clip-search__name";
    name.textContent = c.name;
    name.title = c.path;

    const kws = document.createElement("span");
    kws.className = "library-clip-search__kws";
    // Highlight the matched keyword(s) so the user sees WHY this clip
    // showed up — important when a clip has 8 tags and only "wedding"
    // matches the query.
    for (const kw of c.keywords) {
      const chip = document.createElement("span");
      chip.className = "library-clip-search__kw" +
        (kw.toLowerCase().includes(query) ? " is-match" : "");
      chip.textContent = kw;
      kws.appendChild(chip);
    }

    const reveal = document.createElement("button");
    reveal.className = "btn btn--ghost library-clip-search__reveal";
    reveal.textContent = "Reveal";
    reveal.addEventListener("click", () => {
      invoke("reveal_in_finder", { path: c.path }).catch(() => {});
    });

    li.append(name, kws, reveal);
    list.appendChild(li);
  }
  panel.appendChild(list);

  if (matches.length > 200) {
    const overflow = document.createElement("div");
    overflow.className = "library-clip-search__overflow";
    overflow.textContent = `+ ${matches.length - 200} more — refine your search to see them.`;
    panel.appendChild(overflow);
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
  if (!(await confirm(`Delete ${name} from the library?\n\nClips on disk keep their existing Keywords until you re-tag.`))) {
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
  libraryEl<HTMLInputElement>("library-search").addEventListener("input", () => {
    renderLibrarySidebar();
    renderLibraryDetail();  // also refresh the main pane for clip-search results
  });
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
    if (paths.length > 0) askForTags(paths);
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
    // The menu item says "Library", so this one really is every clip.
    await startTagFlow(true);
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
      "This moves the current library (face index, frame embeddings, " +
      "thumbnails, person crops in ~/.facetag/) to a timestamped backup. " +
      "You can restore via File > Restore Last Backup if you change your " +
      "mind. Tag data already written into your .mov files isn't touched.",
      { title: "Reset Library", okLabel: "Reset", cancelLabel: "Cancel" }
    );
    if (!ok) return;
    try {
      const result = await invoke<{ backup_dir: string | null }>("reset_library");
      if (result.backup_dir) {
        flashToast("Library reset. Backup saved — undo via File > Restore Last Backup.");
      } else {
        flashToast("Library was already empty.");
      }
      setState("idle");
    } catch (e) {
      flashToast(`Reset failed: ${e}`, true);
    }
  });
  listen("menu://telemetry-settings", async () => {
    let cfg: TelemetryConfig;
    try {
      cfg = await invoke<TelemetryConfig>("telemetry_state");
    } catch (e) {
      flashToast(`Couldn't read telemetry state: ${e}`, true);
      return;
    }
    const currently = cfg.telemetry_enabled === true ? "currently ON" :
                      cfg.telemetry_enabled === false ? "currently OFF" :
                      "not set";
    const wantOn = await confirm(
      `Anonymous usage telemetry is ${currently}.\n\n` +
      "What gets sent: event names (scan/tag/activity complete), app " +
      "version, macOS version.\n\n" +
      "What never leaves your Mac: clip names, folder paths, face data.\n\n" +
      "Turn it ON to help improve Spotted, or OFF to disable.",
      { title: "Telemetry", okLabel: "Turn ON", cancelLabel: "Turn OFF" }
    );
    try {
      await invoke("set_telemetry_enabled", { enabled: wantOn });
      flashToast(`Telemetry ${wantOn ? "enabled" : "disabled"}.`);
    } catch (e) {
      flashToast(`Couldn't save telemetry setting: ${e}`, true);
    }
  });
  listen("menu://restore-backup", async () => {
    if (isBusy()) {
      flashToast("Finish or cancel the current batch first.");
      return;
    }
    let backups: string[] = [];
    try {
      backups = await invoke<string[]>("list_library_backups");
    } catch (e) {
      flashToast(`Couldn't list backups: ${e}`, true);
      return;
    }
    if (backups.length === 0) {
      flashToast("No backups available.");
      return;
    }
    const latest = backups[0].split("/").pop() ?? backups[0];
    const ok = await confirm(
      `Restore from ${latest}?\n\n` +
      "Your current library (if any) will be moved aside as a safety " +
      "backup before the restore, so this is itself reversible.",
      { title: "Restore Last Backup", okLabel: "Restore", cancelLabel: "Cancel" }
    );
    if (!ok) return;
    try {
      await invoke("restore_last_backup");
      flashToast("Library restored from backup.");
      setState("idle");
    } catch (e) {
      flashToast(`Restore failed: ${e}`, true);
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
    if (currentPaths.length === 0) return;
    overwriteKeywords = tagsOverwrite.checked;
    energyEnabled = tagsEnergy.checked;
    runBatch(currentPaths, readTagsInput());
  });
  tagsSkip.addEventListener("click", () => {
    if (currentPaths.length === 0) return;
    overwriteKeywords = tagsOverwrite.checked;
    energyEnabled = tagsEnergy.checked;
    runBatch(currentPaths, []);
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
  // Person thumbnails need the real home directory. Resolve it before the
  // library can render so the sidebar isn't a column of blank circles.
  homeDir()
    .then((h) => { homePath = h.replace(/\/$/, ""); })
    .catch(() => { homePath = null; });
  loadVersion();
  wireDragDrop();
  wireMenuEvents();
  wireUpdaterEvents();
  wireTagsScreen();
  wireLibrary();
  wireWelcome();
  maybeShowWelcome();
  maybeAskTelemetry();
  refreshFooterStatus();
});

type TelemetryConfig = {
  install_id: string | null;
  telemetry_enabled: boolean | null;
};

/** On first launch (no telemetry choice yet), ask the user. Off by default
 *  to align with Spotted's privacy positioning — we only collect anonymous
 *  usage events if they explicitly opt in. */
async function maybeAskTelemetry(): Promise<void> {
  let cfg: TelemetryConfig;
  try {
    cfg = await invoke<TelemetryConfig>("telemetry_state");
  } catch {
    return;
  }
  if (cfg.telemetry_enabled !== null) return; // already decided
  const ok = await confirm(
    "Help improve Spotted by sharing anonymous usage data?\n\n" +
    "What gets sent: event names (scan complete, tag write complete), app " +
    "version, and macOS version.\n\n" +
    "What never leaves your Mac: clip names, folder paths, face data, " +
    "people's names, anything from inside your files.\n\n" +
    "You can change this anytime under Spotted → Telemetry…",
    {
      title: "Help improve Spotted",
      okLabel: "Yes, share anonymously",
      cancelLabel: "No thanks",
    }
  );
  try {
    await invoke("set_telemetry_enabled", { enabled: ok });
  } catch (e) {
    console.warn("failed to persist telemetry choice:", e);
  }
}
