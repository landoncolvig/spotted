import { invoke } from "@tauri-apps/api/core";
import { listen, type UnlistenFn } from "@tauri-apps/api/event";
import { open } from "@tauri-apps/plugin-dialog";

type State = "idle" | "working" | "label" | "done";
type SidecarEvent = { kind: "stdout" | "stderr"; line: string };

const stage = document.getElementById("stage") as HTMLElement;
const dropzone = document.getElementById("dropzone") as HTMLElement;
const versionEl = document.getElementById("version") as HTMLElement;
const workingLabel = document.getElementById("working-label") as HTMLElement;
const workingPath = document.getElementById("working-path") as HTMLElement;
const workingDetail = document.getElementById("working-detail") as HTMLElement;
const progressBar = document.getElementById("progress-bar") as HTMLElement;
const doneSub = document.getElementById("done-sub") as HTMLElement;
const btnAgain = document.getElementById("btn-again") as HTMLButtonElement;

const LABEL_PORT = 8765;

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
    await runBatch(path);
  }
}

function setProgress(pct: number) {
  progressBar.style.width = `${Math.max(0, Math.min(100, pct))}%`;
}

async function runBatch(path: string) {
  setState("working");
  workingPath.textContent = path;
  let totalVideos = 0;
  let scannedVideos = 0;
  let totalFaces = 0;

  const unlisten: UnlistenFn = await listen<SidecarEvent>("sidecar://line", (e) => {
    const line = e.payload.line;

    let m = line.match(/Found\s+(\d+)\s+video/);
    if (m) {
      totalVideos = parseInt(m[1], 10);
      workingDetail.textContent = `Reading ${totalVideos} clips`;
    }

    m = line.match(/\.mov.*?(\d+)\/(\d+)\s+frames\s+(\d+)\s+faces/);
    if (m) {
      scannedVideos = Math.min(totalVideos, scannedVideos + 1);
      totalFaces += parseInt(m[3], 10);
      if (totalVideos > 0) setProgress((scannedVideos / totalVideos) * 100);
      workingDetail.textContent = `${scannedVideos}/${totalVideos} clips, ${totalFaces} faces`;
    }

    m = line.match(/Done\.\s+(\d+)\s+faces indexed/);
    if (m) {
      totalFaces = parseInt(m[1], 10);
      setProgress(100);
    }
  });

  try {
    workingLabel.textContent = "Spotting";
    workingDetail.textContent = "Reading frames…";
    setProgress(2);
    await invoke<number>("scan_folder", { path });

    workingLabel.textContent = "Grouping faces";
    workingDetail.textContent = "Clustering…";
    setProgress(0);
    await invoke<number>("cluster_faces");

    workingLabel.textContent = "Naming people";
    workingDetail.textContent = "Opening labeler…";
    await invoke<number>("start_label_server", { port: LABEL_PORT });
    mountLabelScreen();
    setState("label");
  } catch (err) {
    workingLabel.textContent = "Something went wrong";
    workingDetail.textContent = String(err);
  } finally {
    unlisten();
  }
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
  workingLabel.textContent = "Writing keywords";
  workingDetail.textContent = "exiftool, per clip…";
  setProgress(20);
  try {
    await invoke<number>("tag_videos");
    setProgress(100);
    setState("done");
    doneSub.textContent = "Open the folder in Premiere or DaVinci — search by name.";
  } catch (err) {
    workingDetail.textContent = String(err);
  }
}

btnAgain.addEventListener("click", () => {
  setProgress(0);
  setState("idle");
});

function wireDragDrop() {
  dropzone.addEventListener("click", pickFolder);
  listen("tauri://drag-enter", () => dropzone.classList.add("is-hot"));
  listen("tauri://drag-leave", () => dropzone.classList.remove("is-hot"));
  listen<{ paths: string[] }>("tauri://drag-drop", (e) => {
    dropzone.classList.remove("is-hot");
    const paths = e.payload?.paths ?? [];
    if (paths.length > 0) runBatch(paths[0]);
  });
}

window.addEventListener("DOMContentLoaded", () => {
  loadVersion();
  wireDragDrop();
});
