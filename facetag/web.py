"""Single-page web labeler. One scrollable list of clusters with name inputs and a Save All button."""
from __future__ import annotations

import io
import os
import threading
import webbrowser
from pathlib import Path

import cv2
from flask import Flask, jsonify, request, send_file

from . import db
from .label import _crop_face, _make_grid


def _render_seen_in(video_paths: list[str], max_shown: int = 3) -> str:
    """Render the "Seen in: a.mov, b.mov, +N more" caption for a cluster card.

    Shows up to `max_shown` basenames so the user can tell at a glance which
    clip(s) the cluster came from before naming it. Empty list → empty string
    (caller still gets a stable DOM structure to attach to).
    """
    if not video_paths:
        return ""
    names = [os.path.basename(p) for p in video_paths]
    shown = names[:max_shown]
    extra = len(names) - len(shown)
    label = ", ".join(shown)
    if extra > 0:
        label += f", +{extra} more"
    title = "&#10;".join(names)  # newline-separated for the hover tooltip
    return f'<span class="seen-in" title="{title}">seen in: {label}</span>'


def create_app(
    db_path: Path,
    thumb_dir: Path,
    *,
    scope_paths: list[str] | None = None,
) -> Flask:
    thumb_dir.mkdir(parents=True, exist_ok=True)
    app = Flask(__name__)
    app.config["DB_PATH"] = db_path
    app.config["THUMB_DIR"] = thumb_dir
    app.config["SCOPE_PATHS"] = list(scope_paths) if scope_paths else []

    def _conn():
        return db.connect(db_path)

    def _scope_label() -> str:
        """Short human label for the active scope filter, used in the chip."""
        sp = app.config["SCOPE_PATHS"]
        if not sp:
            return ""
        # If a single file: show its basename. If a dir: show its basename + "/".
        # Multiple: "N paths".
        import os
        if len(sp) == 1:
            p = sp[0].rstrip("/")
            base = os.path.basename(p) or p
            return base + ("/" if os.path.isdir(sp[0]) else "")
        return f"{len(sp)} paths"

    @app.route("/")
    def index() -> str:
        # Two orthogonal filters drive what cards render:
        #   ?show=all     → bypass scope (see clusters from other batches too)
        #   ?view=needs   → DEFAULT: only unnamed clusters with size >= MIN_FACES
        #   ?view=labeled → only clusters with a name (for renaming)
        #   ?view=all     → everyone, even tiny noise clusters
        show_all = request.args.get("show") == "all"
        view = request.args.get("view") or "needs"
        scope = None if show_all else (app.config["SCOPE_PATHS"] or None)
        MIN_FACES = 3  # below this, almost always a false-positive cluster

        with _conn() as conn:
            summary = db.cluster_summary_with_videos(conn, scope_paths=scope)

        # Apply the view filter and count what we're hiding so the header
        # can offer a "show N hidden" affordance — users without context
        # would otherwise wonder where their named people went.
        total = len(summary)
        labeled_total = sum(1 for _, _, n, _ in summary if n)
        noise_total = sum(1 for _, c, n, _ in summary if not n and c < MIN_FACES)
        if view == "needs":
            summary = [r for r in summary if not r[2] and r[1] >= MIN_FACES]
        elif view == "labeled":
            summary = [r for r in summary if r[2]]
        # view == "all" → keep everything

        # The thumb route uses the same ?show=all flag to pick between the
        # scoped and unscoped cache files. Propagate the query string so a
        # show-all view doesn't accidentally serve scoped thumbs.
        thumb_qs = "?show=all" if show_all else ""

        cards = []
        for cid, count, name, video_paths in summary:
            existing = (name or "").replace('"', "&quot;")
            badge = f'<span class="hint">already: {existing}</span>' if name else ""
            initial_class = " is-saved" if name else ""
            cards.append(f"""
            <div class="card{initial_class}" data-cluster="{cid}">
              <span class="card-status" aria-live="polite"></span>
              <div class="head">
                <span class="cid">cluster {cid}</span>
                <span class="cnt">{count} faces</span>
                <button type="button" class="hide-btn" data-cluster="{cid}" title="Hide this cluster (not a person)">×</button>
              </div>
              <img loading="lazy" src="/thumb/{cid}.jpg{thumb_qs}" alt="cluster {cid}">
              <input type="text" name="name-{cid}" placeholder="name…" value="{existing}" autocomplete="off">
              {badge}
              {_render_seen_in(video_paths)}
            </div>
            """)

        # View-toggle chips: tell the user what's currently visible and
        # offer one-click swaps to see labeled or every cluster. Eliminates
        # the "where did my named people go" surprise.
        def _chip(target_view: str, label: str, active: bool) -> str:
            qs = []
            if show_all:
                qs.append("show=all")
            if target_view != "needs":
                qs.append(f"view={target_view}")
            href = "/?" + "&".join(qs) if qs else "/"
            cls = "view-chip is-active" if active else "view-chip"
            return f'<a class="{cls}" href="{href}">{label}</a>'

        view_chips = (
            _chip("needs", f"Needs labeling ({total - labeled_total - noise_total})", view == "needs")
            + _chip("labeled", f"Labeled ({labeled_total})", view == "labeled")
            + _chip("all", f"All ({total})", view == "all")
        )

        scope_chip = ""
        if app.config["SCOPE_PATHS"]:
            label = _scope_label()
            tail = "&view=" + view if view != "needs" else ""
            if show_all:
                scope_chip = (
                    f'<a class="scope-chip scope-chip--all" href="/?{("view=" + view) if view != "needs" else ""}">'
                    f'<span class="dot"></span>Showing all clusters · back to {label}</a>'
                )
            else:
                scope_chip = (
                    f'<a class="scope-chip" href="/?show=all{tail}">'
                    f'<span class="dot"></span>Filtered to {label} · show all</a>'
                )

        return (
            _PAGE
            .replace("__CARDS__", "\n".join(cards))
            .replace("__COUNT__", str(len(summary)))
            .replace("__SCOPE_CHIP__", scope_chip)
            .replace("__VIEW_CHIPS__", view_chips)
        )

    @app.route("/thumb/<int:cluster_id>.jpg")
    def thumb(cluster_id: int):
        # When the labeler is scoped, the thumb sampling has to follow the
        # scope — otherwise a cluster that absorbed faces from the freshly-
        # dropped clip via centroid match would still display its old crops
        # from the original batch, which is exactly the "wrong faces"
        # confusion users hit. Cache key bakes in a hash of the scope so the
        # scoped and unscoped thumbs don't clobber each other on disk.
        import hashlib
        show_all = request.args.get("show") == "all"
        scope = None if show_all else (app.config["SCOPE_PATHS"] or None)
        if scope:
            digest = hashlib.sha1("|".join(scope).encode()).hexdigest()[:8]
            out = thumb_dir / f"cluster_{cluster_id:04d}_scope_{digest}.jpg"
        else:
            out = thumb_dir / f"cluster_{cluster_id:04d}.jpg"

        if not out.exists():
            with _conn() as conn:
                samples = db.representative_faces(
                    conn, cluster_id, n=9, scope_paths=scope
                )
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
        """Bulk save + final merge. Used by the "Done" button.

        Per-card saves use /save-one — by the time the user hits Done,
        most names are already persisted; this path just catches anything
        in-flight and runs the auto-merge.
        """
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

    @app.route("/save-one", methods=["POST"])
    def save_one():
        """Save a single cluster's name. Used by per-card auto-save.

        Doesn't run the merge step — that's expensive and only matters
        once the user is done with all clusters. Empty name clears any
        previously-saved name for that cluster.
        """
        data = request.get_json(force=True) or {}
        try:
            cid = int(data.get("cluster_id"))
        except (TypeError, ValueError):
            return jsonify({"error": "cluster_id required"}), 400
        name = (data.get("name") or "").strip()
        with _conn() as conn:
            if not name:
                conn.execute("DELETE FROM people WHERE cluster_id = ?", (cid,))
                conn.commit()
            else:
                db.name_cluster(conn, cid, name)
        return jsonify({"cluster_id": cid, "name": name or None})

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


def serve(
    db_path: Path,
    thumb_dir: Path,
    port: int = 8765,
    open_browser: bool = True,
    scope_paths: list[str] | None = None,
) -> None:
    app = create_app(db_path, thumb_dir, scope_paths=scope_paths)
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
    position: relative;
  }
  .card:focus-within {
    border-color: var(--primary);
    background: var(--surface-2);
  }
  /* Saved-state indicator on each card */
  .card-status {
    position: absolute; top: 8px; right: 32px;
    font-size: 10px; font-weight: 500;
    color: var(--text-faint);
    opacity: 0; transition: opacity .15s ease, color .15s ease;
    user-select: none;
    pointer-events: none;
  }
  .card.is-saving .card-status { opacity: 1; color: var(--text-faint); }
  .card.is-saving .card-status::before { content: "saving…"; }
  .card.is-saved .card-status { opacity: 1; color: var(--success); }
  .card.is-saved .card-status::before { content: "✓ saved"; }
  .card.is-saveerror .card-status { opacity: 1; color: var(--danger); }
  .card.is-saveerror .card-status::before { content: "× retry"; }
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
  .seen-in {
    display: block;
    font-size: 10px;
    color: var(--text-faint);
    margin-top: 4px;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }
  .scope-chip {
    display: inline-flex; align-items: center; gap: 6px;
    background: rgba(91, 182, 224, 0.10);
    border: 1px solid rgba(91, 182, 224, 0.35);
    color: var(--accent);
    padding: 4px 10px; border-radius: 999px;
    font-size: 11px; text-decoration: none;
    transition: background .15s ease, border-color .15s ease;
  }
  .scope-chip:hover { background: rgba(91, 182, 224, 0.18); border-color: rgba(91, 182, 224, 0.55); }
  .scope-chip .dot {
    width: 6px; height: 6px; border-radius: 50%;
    background: var(--accent);
  }
  .scope-chip--all { color: var(--text-dim); border-color: var(--border-strong); background: var(--surface-2); }
  .scope-chip--all .dot { background: var(--text-faint); }

  /* View toggle chips — switch between Needs labeling / Labeled / All
     so a user who's labeled people can still find them again. */
  .view-chips { display: inline-flex; gap: 4px; margin-left: 12px; }
  .view-chip {
    display: inline-flex;
    align-items: center;
    padding: 4px 10px;
    border: 1px solid var(--border);
    border-radius: 14px;
    font-size: 12px;
    color: var(--text-dim);
    background: var(--surface);
    text-decoration: none;
    transition: background 120ms, color 120ms, border-color 120ms;
  }
  .view-chip:hover { color: var(--text); border-color: var(--border-strong); }
  .view-chip.is-active {
    color: var(--text);
    border-color: var(--primary);
    background: rgba(240, 130, 32, 0.12);
  }
</style>
</head><body>
<header>
  <h1>Name the people</h1>
  <span class="meta">__COUNT__ clusters &middot; saves as you type &middot; <kbd>Tab</kbd> to advance</span>
  <span class="view-chips">__VIEW_CHIPS__</span>
  __SCOPE_CHIP__
  <input class="filter" id="filter" placeholder="filter…" autocomplete="off">
  <div class="actions">
    <button id="save">Done</button>
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
  btn.disabled = true; btn.textContent = "Finishing…";
  // Catch any still-in-flight names (rare if per-card autosave works,
  // but covers the edge where user clicks Done immediately after typing
  // before the debounce fires).
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
    btn.disabled = false; btn.textContent = "Done";
  }
}

async function hideCard(cid, cardEl) {
  cardEl.classList.add("is-hiding");
  try {
    await fetch(`/hide/${cid}`, { method: "POST" });
    setTimeout(() => cardEl.remove(), 200);
  } catch (e) {
    cardEl.classList.remove("is-hiding");
    toast("Hide failed: " + e.message, true);
  }
}

// Per-card auto-save: writes each name to disk as the user types.
// Debounced 350ms after the last keystroke; immediate on blur.
const saveTimers = new Map();
function setCardState(card, state) {
  card.classList.remove("is-saving", "is-saved", "is-saveerror");
  if (state) card.classList.add(state);
}
async function saveOne(card) {
  const cid = card.dataset.cluster;
  const input = card.querySelector("input");
  const name = input.value.trim();
  setCardState(card, "is-saving");
  try {
    const r = await fetch("/save-one", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({ cluster_id: Number(cid), name }),
    });
    if (!r.ok) throw new Error("HTTP " + r.status);
    setCardState(card, name ? "is-saved" : null);
  } catch (e) {
    setCardState(card, "is-saveerror");
  }
}
function scheduleSave(card) {
  const cid = card.dataset.cluster;
  clearTimeout(saveTimers.get(cid));
  saveTimers.set(cid, setTimeout(() => saveOne(card), 350));
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

// Per-card auto-save listeners
document.addEventListener("input", (e) => {
  const input = e.target;
  if (input.tagName !== "INPUT" || input.classList.contains("filter")) return;
  const card = input.closest(".card");
  if (!card) return;
  scheduleSave(card);
});
document.addEventListener("blur", (e) => {
  const input = e.target;
  if (input.tagName !== "INPUT" || input.classList.contains("filter")) return;
  const card = input.closest(".card");
  if (!card) return;
  // Flush any pending debounced save immediately on blur
  clearTimeout(saveTimers.get(card.dataset.cluster));
  saveOne(card);
}, true);

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
