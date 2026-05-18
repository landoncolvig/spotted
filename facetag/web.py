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
        return jsonify({"saved": saved})

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
<title>facetag — label clusters</title>
<style>
  :root { color-scheme: light dark; }
  * { box-sizing: border-box; }
  body { font-family: -apple-system, system-ui, sans-serif; margin: 0; background: #111; color: #eee; }
  header { position: sticky; top: 0; z-index: 10; background: #111; padding: 14px 20px; border-bottom: 1px solid #333; display: flex; align-items: center; gap: 16px; }
  header h1 { font-size: 16px; margin: 0; font-weight: 600; }
  header .meta { color: #888; font-size: 13px; }
  header .actions { margin-left: auto; }
  button { background: #2563eb; color: white; border: 0; padding: 9px 18px; border-radius: 6px; font-size: 14px; cursor: pointer; font-weight: 600; }
  button:hover { background: #1d4ed8; }
  button:disabled { background: #444; cursor: not-allowed; }
  #toast { position: fixed; bottom: 20px; right: 20px; background: #16a34a; color: white; padding: 10px 16px; border-radius: 6px; opacity: 0; transition: opacity .2s; pointer-events: none; }
  #toast.show { opacity: 1; }
  #toast.err { background: #dc2626; }
  .grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(240px, 1fr)); gap: 14px; padding: 18px; }
  .card { background: #1c1c1c; border: 1px solid #2a2a2a; border-radius: 8px; padding: 10px; display: flex; flex-direction: column; gap: 8px; }
  .card .head { display: flex; justify-content: space-between; font-size: 12px; color: #888; }
  .card .cid { font-weight: 600; color: #ccc; }
  .card img { width: 100%; aspect-ratio: 1; object-fit: cover; border-radius: 4px; background: #000; }
  .card input { background: #0c0c0c; border: 1px solid #333; color: #eee; padding: 8px 10px; border-radius: 4px; font-size: 14px; }
  .card input:focus { outline: none; border-color: #2563eb; }
  .hint { color: #888; font-size: 11px; }
  .filter { background: #1c1c1c; border: 1px solid #333; color: #eee; padding: 7px 10px; border-radius: 6px; font-size: 13px; width: 200px; }
  .filter:focus { outline: none; border-color: #2563eb; }
  kbd { background: #2a2a2a; border: 1px solid #444; border-radius: 3px; padding: 1px 5px; font-size: 11px; }
</style>
</head><body>
<header>
  <h1>facetag</h1>
  <span class="meta">__COUNT__ clusters &middot; <kbd>Tab</kbd> to next, <kbd>⌘S</kbd> to save</span>
  <input class="filter" id="filter" placeholder="filter by cluster id or name…" autocomplete="off">
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
    toast(`Saved ${j.saved} cluster name(s)`);
  } catch (e) {
    toast("Save failed: " + e.message, true);
  } finally {
    btn.disabled = false; btn.textContent = "Save All";
  }
}

$("#save").addEventListener("click", save);
window.addEventListener("keydown", (e) => {
  if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "s") {
    e.preventDefault(); save();
  }
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
