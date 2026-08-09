"""The webview must not be able to run the sidecar itself.

Through v0.0.95 the main window's capability granted `shell:allow-execute` on
`binaries/spotted-sidecar` with `"args": true`, which is a wildcard: any code
running in the webview could spawn the sidecar with any argv. The sidecar is
the whole facetag CLI, so that is arbitrary read and write over the user's
footage and library.

Nothing needed it. Every sidecar spawn happens in Rust through
`app.shell().sidecar(...)`, and that path performs no scope check at all — the
scope is only consulted by the plugin's own `execute`/`spawn` IPC commands,
which are the ones the frontend never calls. So the permission was removed
outright rather than narrowed to an argument allowlist.

That reasoning holds only while the frontend stays off the shell plugin, so
the premise is pinned here too. If someone imports it later these tests fail
and say what to do, instead of the permission quietly coming back with
`args: true` because that is what makes the import work.
"""

from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CAPABILITIES = ROOT / "app/src-tauri/capabilities"
MAIN_TS = ROOT / "app/src/main.ts"


def _permissions() -> list:
    perms = []
    for path in sorted(CAPABILITIES.glob("*.json")):
        perms.extend(json.loads(path.read_text()).get("permissions", []))
    return perms


def _identifiers() -> list[str]:
    return [p if isinstance(p, str) else p.get("identifier", "") for p in _permissions()]


def test_the_webview_cannot_execute_or_spawn_anything():
    granted = [i for i in _identifiers() if i.startswith("shell:")]
    assert granted == [], (
        f"capability grants {granted}; the frontend does not use the shell "
        "plugin, so nothing should re-open that IPC surface"
    )


def test_no_capability_grants_wildcard_arguments():
    """`"args": true` means any argv. An allowlist is a list, never `true`."""
    for perm in _permissions():
        for entry in (perm.get("allow", []) if isinstance(perm, dict) else []):
            if isinstance(entry, dict):
                assert entry.get("args") is not True, (
                    f"{perm.get('identifier')} allows {entry.get('name')} with "
                    "any arguments"
                )


def test_the_frontend_still_does_not_use_the_shell_plugin():
    """The premise of removing the permission. If this fails, do not just add
    the permission back — add it scoped to the exact commands being run."""
    source = MAIN_TS.read_text()
    assert "plugin-shell" not in source
    assert "Command(" not in source


def test_rust_still_owns_every_sidecar_spawn():
    """Sidecar spawning lives in Rust, where the scope does not apply."""
    lib = (ROOT / "app/src-tauri/src/lib.rs").read_text()
    assert '.sidecar("spotted-sidecar")' in lib
