"""Single-page web labeler. One scrollable list of clusters with name inputs and a Save All button."""
from __future__ import annotations

import io
import threading
import webbrowser
from pathlib import Path

import cv2
from flask import Flask, jsonify, request, send_file

from . import db
from .label import _crop_face, _make_grid


def create_app(db_path: Path, thumb_dir: Path) -> Flask:
    thumb_dir.mkdir(parents=True, exist_ok=True)
    app = Flask(__name__)
    app.config["DB_PATH"] = db_path
    app.config["THUMB_DIR"] = thumb_dir

    def _conn():
        return db.connect(db_path)

    @app.route("/")
    def index() -> str:
        with _conn() as conn:
            summary = db.cluster_summary(conn)
        cards = []
        for cid, count, name in summary:
            existing = (name or "").replace('"', "&quot;")
            badge = f'<span class="hint">already: {existing}</span>' if name else ""
            cards.append(f"""
            <div class="card" data-cluster="{cid}">
              <div class="head">
                <span class="cid">cluster {cid}</span>
                <span class="cnt">{count} faces</span>
                <button type="button" class="hide-btn" data-cluster="{cid}" title="Hide this cluster (not a person)">×</button>
              </div>
              <img loading="lazy" src="/thumb/{cid}.jpg" alt="cluster {cid}">
              <input type="text" name="name-{cid}" placeholder="name…" value="{existing}" autocomplete="off">
              {badge}
            </div>
            """)
        return _PAGE.replace("__CARDS__", "\n".join(cards)).replace("__COUNT__", str(len(summary)))

    @app.route("/thumb/<int:cluster_id>.jpg")
    def thumb(cluster_id: int):
        out = thumb_dir / f"cluster_{cluster_id:04d}.jpg"
        if not out.exists():
            with _conn() as conn:
                samples = db.representative_faces(conn, cluster_id, n=9)
            crops = []
            for video_path, t, bbox in samples:
                c = _crop_face(Path(video_path), t, bbox)
                if c is not None:
                    crops.append(c)
            if not crops:
                return ("no preview", 404)
            grid = _make_grid(crops)
            cv2.imwrite(str(out), grid, [cv2.IMWRITE_JPEG_QUALITY, 85])
        return send_file(out, mimetype="image/jpeg")

    @app.route("/save", methods=["POST"])
    def save():
        data = request.get_json(force=True) or {}
        names: dict[str, str] = data.get("names", {})
        with _conn() as conn:
            saved = 0
            for cid_str, name in names.items():
                name = (name or "").strip()
                if not name:
                    continue
                db.name_cluster(conn, int(cid_str), name)
                saved += 1
            # Auto-merge clusters sharing a name. If the user typed "Ellie"
            # on 30 cards, this consolidates them into one cluster_id so
            # future label sessions show ONE Ellie card with all faces.
            merged = db.merge_clusters_by_name(conn)
        merged_count = sum(max(0, len(v) - 1) for v in merged.values())
        return jsonify({"saved": saved, "merged": merged_count, "merged_by": merged})

    @app.route("/hide/<int:cluster_id>", methods=["POST"])
    def hide(cluster_id: int):
        """Mark a cluster as noise so it doesn't show up in future labeler runs."""
        with _conn() as conn:
            db.hide_cluster(conn, cluster_id)
        return jsonify({"hidden": cluster_id})

    @app.route("/unhide/<int:cluster_id>", methods=["POST"])
    def unhide(cluster_id: int):
        with _conn() as conn:
            db.unhide_cluster(conn, cluster_id)
        return jsonify({"unhidden": cluster_id})

    return app


def serve(db_path: Path, thumb_dir: Path, port: int = 8765, open_browser: bool = True) -> None:
    app = create_app(db_path, thumb_dir)
    url = f"http://127.0.0.1:{port}/"
    if open_browser:
        threading.Timer(0.7, lambda: webbrowser.open(url)).start()
    print(f"\nfacetag web labeler running at {url}")
    print("Type names in each card, then click Save All. Ctrl+C to stop.\n")
    # Use Flask's built-in server; this is single-user local-only.
    app.run(host="127.0.0.1", port=port, debug=False, use_reloader=False)


_PAGE = r"""<!doctype html>
<html lang="en"><head>
<meta charset="utf-8">
<title>Spotted — name the people</title>
<style>
  :root {
    --bg: #0E0F12;
    --surface: #1C1D22;
    --surface-2: #24262C;
    --border: #2B2C32;
    --border-strong: #3A3C44;
    --primary: #E0833B;
    --primary-hover: #EA9450;
    --primary-press: #C46327;
    --accent: #5BB6E0;
    --text: #F5F5F0;
    --text-dim: #9B9890;
    --text-faint: #6E6B66;
    --success: #6FCF97;
    --danger: #E06A6A;
    color-scheme: dark;
  }
  * { box-sizing: border-box; }
  body {
    font-family: -apple-system, "SF Pro Text", system-ui, sans-serif;
    margin: 0; background: var(--bg); color: var(--text);
    -webkit-font-smoothing: antialiased;
  }
  header {
    position: sticky; top: 0; z-index: 10;
    background: var(--bg); padding: 14px 20px;
    border-bottom: 1px solid var(--border);
    display: flex; align-items: center; gap: 16px;
  }
  header h1 {
    font-family: -apple-system, "SF Pro Display", system-ui, sans-serif;
    font-size: 15px; margin: 0; font-weight: 600;
    letter-spacing: -0.01em; color: var(--text);
  }
  header .meta { color: var(--text-faint); font-size: 12px; }
  header .actions { margin-left: auto; }
  button {
    font-family: inherit;
    background: var(--primary); color: #1A0F06;
    border: 1px solid transparent;
    padding: 8px 16px; border-radius: 10px;
    font-size: 13px; font-weight: 500; cursor: pointer;
    transition: background .15s ease, transform .1s ease;
  }
  button:hover { background: var(--primary-hover); }
  button:active { background: var(--primary-press); transform: translateY(1px); }
  button:disabled { background: var(--surface-2); color: var(--text-faint); cursor: not-allowed; }
  #toast {
    position: fixed; bottom: 20px; right: 20px;
    background: var(--success); color: #0E1A12;
    padding: 9px 14px; border-radius: 10px;
    font-size: 13px; font-weight: 500;
    opacity: 0; transition: opacity .2s;
    pointer-events: none;
  }
  #toast.show { opacity: 1; }
  #toast.err { background: var(--danger); color: #1A0808; }
  .grid {
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(220px, 1fr));
    gap: 14px; padding: 18px;
  }
  .card {
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: 12px; padding: 10px;
    display: flex; flex-direction: column; gap: 8px;
    transition: border-color .15s ease, background .15s ease;
  }
  .card:focus-within {
    border-color: var(--primary);
    background: var(--surface-2);
  }
  .card .head {
    display: flex; justify-content: space-between;
    font-size: 11px; color: var(--text-faint);
    font-variant-numeric: tabular-nums;
  }
  .card .cid { font-weight: 500; color: var(--text-dim); }
  .card img {
    width: 100%; aspect-ratio: 1;
    object-fit: cover; border-radius: 8px;
    background: #000;
  }
  .card input {
    font-family: inherit;
    background: var(--bg);
    border: 1px solid var(--border-strong);
    color: var(--text);
    padding: 8px 10px; border-radius: 8px;
    font-size: 13px;
    transition: border-color .15s ease;
  }
  .card input::placeholder { color: var(--text-faint); }
  .card input:focus { outline: none; border-color: var(--primary); }
  .hint { color: var(--text-faint); font-size: 11px; }
  .hide-btn {
    font-family: inherit;
    background: transparent; color: var(--text-faint);
    border: 1px solid transparent;
    width: 20px; height: 20px;
    border-radius: 6px;
    padding: 0; line-height: 1;
    font-size: 14px; cursor: pointer;
    transition: background .15s ease, color .15s ease;
  }
  .hide-btn:hover {
    background: rgba(224, 106, 106, 0.15);
    color: var(--danger);
  }
  .card.is-hiding { opacity: 0.3; pointer-events: none; transition: opacity .2s ease; }
  .filter {
    font-family: inherit;
    background: var(--surface);
    border: 1px solid var(--border);
    color: var(--text);
    padding: 7px 10px; border-radius: 8px;
    font-size: 12px; width: 220px;
    transition: border-color .15s ease;
  }
  .filter::placeholder { color: var(--text-faint); }
  .filter:focus { outline: none; border-color: var(--primary); }
  kbd {
    font-family: ui-monospace, "SF Mono", Menlo, monospace;
    background: var(--surface-2); border: 1px solid var(--border-strong);
    border-radius: 4px; padding: 1px 5px;
    font-size: 10px; color: var(--text-dim);
  }
</style>
</head><body>
<header>
  <h1>Name the people</h1>
  <span class="meta">__COUNT__ clusters &middot; <kbd>Tab</kbd> to advance, <kbd>⌘S</kbd> to save</span>
  <input class="filter" id="filter" placeholder="filter…" autocomplete="off">
  <div class="actions">
    <button id="save">Save All</button>
  </div>
</header>
<div class="grid" id="grid">__CARDS__</div>
<div id="toast"></div>
<script>
const $ = (s) => document.querySelector(s);
const $$ = (s) => document.querySelectorAll(s);

function toast(msg, err) {
  const t = $("#toast");
  t.textContent = msg;
  t.className = "show" + (err ? " err" : "");
  setTimeout(() => t.className = "", 1800);
}

async function save() {
  const btn = $("#save");
  btn.disabled = true; btn.textContent = "Saving…";
  const names = {};
  $$(".card").forEach(c => {
    if (c.classList.contains("is-hiding")) return;
    const cid = c.dataset.cluster;
    const v = c.querySelector("input").value.trim();
    if (v) names[cid] = v;
  });
  try {
    const r = await fetch("/save", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({names}),
    });
    const j = await r.json();
    let msg = `Saved ${j.saved} name(s)`;
    if (j.merged > 0) msg += ` · merged ${j.merged} duplicate(s)`;
    toast(msg);
  } catch (e) {
    toast("Save failed: " + e.message, true);
  } finally {
    btn.disabled = false; btn.textContent = "Save All";
  }
}

async function hideCard(cid, cardEl) {
  cardEl.classList.add("is-hiding");
  try {
    await fetch(`/hide/${cid}`, { method: "POST" });
    // Remove from DOM after the fade
    setTimeout(() => cardEl.remove(), 200);
  } catch (e) {
    cardEl.classList.remove("is-hiding");
    toast("Hide failed: " + e.message, true);
  }
}

$("#save").addEventListener("click", save);
window.addEventListener("keydown", (e) => {
  if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "s") {
    e.preventDefault(); save();
  }
});

// Hide-cluster buttons: dismiss noise clusters without naming them
document.addEventListener("click", (e) => {
  const btn = e.target.closest(".hide-btn");
  if (!btn) return;
  const card = btn.closest(".card");
  if (!card) return;
  const cid = btn.dataset.cluster;
  hideCard(cid, card);
});

const filter = $("#filter");
filter.addEventListener("input", () => {
  const q = filter.value.toLowerCase().trim();
  $$(".card").forEach(c => {
    if (!q) { c.style.display = ""; return; }
    const cid = c.dataset.cluster;
    const v = c.querySelector("input").value.toLowerCase();
    c.style.display = (cid.includes(q) || v.includes(q)) ? "" : "none";
  });
});
</script>
</body></html>
"""
