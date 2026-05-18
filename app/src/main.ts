import { invoke } from "@tauri-apps/api/core";
import { listen } from "@tauri-apps/api/event";
import { open } from "@tauri-apps/plugin-dialog";

type State = "idle" | "working" | "done";

const stage = document.getElementById("stage") as HTMLElement;
const dropzone = document.getElementById("dropzone") as HTMLElement;
const versionEl = document.getElementById("version") as HTMLElement;
const workingLabel = document.getElementById("working-label") as HTMLElement;
const workingPath = document.getElementById("working-path") as HTMLElement;
const workingDetail = document.getElementById("working-detail") as HTMLElement;
const progressBar = document.getElementById("progress-bar") as HTMLElement;
const doneSub = document.getElementById("done-sub") as HTMLElement;
const btnAgain = document.getElementById("btn-again") as HTMLButtonElement;

function setState(s: State) {
  stage.setAttribute("data-state", s);
}

async function loadVersion() {
  try {
    const v = await invoke<string>("app_version");
    versionEl.textContent = `v${v}`;
  } catch {
    /* keep default */
  }
}

async function pickFolder() {
  const path = await open({ directory: true, multiple: false });
  if (typeof path === "string") {
    await runBatch(path);
  }
}

async function runBatch(path: string) {
  setState("working");
  workingPath.textContent = path;
  workingLabel.textContent = "Spotting";
  workingDetail.textContent = "Reading frames…";
  progressBar.style.width = "0%";

  // v0.0.1: placeholder pipeline.
  // Real face detection + clustering + tagging arrive in v0.0.2 once the
  // Python sidecar is bundled into the .app via PyInstaller.
  await invoke<{ path: string; status: string }>("start_scan", { path });

  await fakeProgress();

  setState("done");
  doneSub.textContent = "Pipeline shell wired. Face detection lands in v0.0.2.";
}

async function fakeProgress() {
  for (let i = 0; i <= 100; i += 4) {
    progressBar.style.width = `${i}%`;
    if (i === 40) workingDetail.textContent = "Detecting faces…";
    if (i === 70) workingDetail.textContent = "Clustering…";
    if (i === 92) workingDetail.textContent = "Writing keywords…";
    await new Promise((r) => setTimeout(r, 60));
  }
}

function wireDragDrop() {
  dropzone.addEventListener("click", pickFolder);

  // Tauri 2 fires file-drop events at the window. The webview's own
  // drag events are mostly suppressed by Tauri, so we listen on the
  // global event bus for actual filesystem paths.
  listen<{ paths: string[] }>("tauri://drag-enter", () => {
    dropzone.classList.add("is-hot");
  });
  listen("tauri://drag-leave", () => {
    dropzone.classList.remove("is-hot");
  });
  listen<{ paths: string[] }>("tauri://drag-drop", (e) => {
    dropzone.classList.remove("is-hot");
    const paths = e.payload?.paths ?? [];
    if (paths.length > 0) runBatch(paths[0]);
  });
}

btnAgain.addEventListener("click", () => setState("idle"));

document.getElementById("btn-reveal")?.addEventListener("click", async () => {
  // Stub for v0.0.1
});

window.addEventListener("DOMContentLoaded", () => {
  loadVersion();
  wireDragDrop();
});
