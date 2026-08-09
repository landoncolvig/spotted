"""The main window's Content-Security-Policy has to match what the UI loads.

The webview shipped with `"csp": null` through v0.0.94, which means no policy
at all: any script the page could be made to load would run, and nothing
stopped it talking to the network. Turning a policy on is only safe if it
keeps allowing the four things the real UI depends on, and there is no local
way to run the Tauri webview and look. So the policy is pinned here against
the frontend source that needs it.

The drift this is really guarding against: `LABEL_PORT` in main.ts and the
`frame-src` port in the CSP are the same number written in two files. If they
ever disagree the labeler iframe renders blank, which is the naming step, which
is the whole product.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TAURI_CONF = ROOT / "app/src-tauri/tauri.conf.json"
MAIN_TS = ROOT / "app/src/main.ts"


def _csp() -> dict[str, list[str]]:
    """Parse the configured CSP into {directive: [sources]}."""
    raw = json.loads(TAURI_CONF.read_text())["app"]["security"]["csp"]
    assert isinstance(raw, str) and raw.strip(), (
        "app.security.csp is null or empty — the webview would run with no "
        "policy at all. See the header of this file."
    )
    parsed: dict[str, list[str]] = {}
    for chunk in raw.split(";"):
        parts = chunk.split()
        if parts:
            parsed[parts[0]] = parts[1:]
    return parsed


def _sources(directive: str) -> list[str]:
    """Sources for a directive, falling back to default-src like a browser."""
    csp = _csp()
    if directive in csp:
        return csp[directive]
    return csp.get("default-src", [])


def test_person_thumbnails_can_load_over_the_asset_protocol():
    """main.ts builds thumbnail URLs with convertFileSrc, which on macOS
    returns `asset://localhost/...`. Without `asset:` the Library sidebar
    silently loses every face."""
    assert "convertFileSrc" in MAIN_TS.read_text()
    assert "asset:" in _sources("img-src")


def test_activity_review_thumbnails_can_load_as_data_uris():
    """activity-suggest attaches base64 JPEG data URIs so the review screen can
    show what each tag matched. `data:` in img-src is what renders them."""
    assert "thumbs" in MAIN_TS.read_text()
    assert "data:" in _sources("img-src")


def test_the_labeler_iframe_port_matches_the_frontend_constant():
    port = re.search(r"const LABEL_PORT\s*=\s*(\d+)", MAIN_TS.read_text())
    assert port, "LABEL_PORT disappeared from main.ts"
    origin = f"http://127.0.0.1:{port.group(1)}"
    assert origin in _sources("frame-src"), (
        f"main.ts serves the labeler on {origin} but frame-src does not allow "
        "it, so the naming step would render an empty iframe"
    )


def test_tauri_ipc_is_reachable():
    """Every invoke() goes through the IPC origin. Blocking it bricks the app."""
    connect = _sources("connect-src")
    assert "ipc:" in connect and "http://ipc.localhost" in connect


def test_scripts_are_restricted_to_the_bundle():
    """The point of the policy. No remote scripts, no eval, no inline."""
    script = _sources("script-src")
    assert script == ["'self'"], f"script-src widened to {script}"


def test_no_directive_is_a_wildcard():
    for directive, sources in _csp().items():
        assert "*" not in sources, f"{directive} allows any origin"
        assert "'unsafe-eval'" not in sources, f"{directive} allows eval"
