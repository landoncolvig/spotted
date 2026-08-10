"""What the consent dialog promises has to be what the code sends.

Telemetry itself was already built and is sound: off unless explicitly opted
in, a no-op if no app id is compiled in, Sentry gated on the same flag with
send_default_pii off and the hostname pinned so a crash report cannot ship
"Ellies-MacBook-Pro". TELEMETRY.md describes all of that accurately.

The dialog the user actually reads did not. It named "macOS version" when only
the OS family is ever sent, and left out the hashed install id, the session id
and the per-run counts. Someone agreeing to it was agreeing to less than was
collected, in an app whose front door says "Local. No cloud."

So these tests pin the two things that keep consent honest: the payload cannot
grow a field that the dialog does not cover, and the dialog cannot claim
something the code does not send.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LIB_RS = (ROOT / "app/src-tauri/src/lib.rs").read_text()
TELEMETRY_RS = (ROOT / "app/src-tauri/src/telemetry.rs").read_text()
MAIN_TS = (ROOT / "app/src/main.ts").read_text()

# Every key any telemetry payload may carry. Counts, flags and outcomes only.
# Adding to this list means the consent text needs to cover it.
ALLOWED_PAYLOAD_KEYS = {
    "status", "paths", "had_batch_tags", "excluded", "overwrite",
}


def _consent_dialogs() -> list[str]:
    """The first-run prompt and the settings toggle. Both are consent.

    Comment lines are stripped. Without that, a test asserting a phrase is
    absent fails on the comment explaining why the phrase was removed — which
    has now happened four times in this codebase in one day. Assert against
    what ships, not against the source around it.
    """
    out = []
    for anchor in ("async function maybeAskTelemetry", 'listen("menu://telemetry-settings"'):
        start = MAIN_TS.index(anchor)
        block = MAIN_TS[start:MAIN_TS.index("await confirm(", start) + 1400]
        out.append("\n".join(
            ln for ln in block.splitlines() if not ln.strip().startswith("//")
        ))
    return out


def test_no_call_site_sends_anything_but_counts_and_outcomes():
    """The guarantee rests on nobody writing payload.insert("path", ...).
    Nothing else checks that."""
    keys = set(re.findall(r'payload\.insert\(\s*"([a-z_]+)"', LIB_RS))
    assert keys, "found no telemetry payload inserts; this check would be vacuous"
    extra = keys - ALLOWED_PAYLOAD_KEYS
    assert not extra, (
        f"new telemetry fields {sorted(extra)} — add them to the consent text "
        "and to ALLOWED_PAYLOAD_KEYS, or do not send them"
    )


def test_the_multiline_inserts_are_covered_too():
    """payload.insert("status".into(), ...) spans lines in places; make sure
    the scan above is not silently missing call sites."""
    assert LIB_RS.count("telemetry::track(") == 4
    assert "app-launch" in LIB_RS


def test_telemetry_is_off_unless_explicitly_turned_on():
    """None (never asked) and Some(false) must both mean silence."""
    assert "if cfg.telemetry_enabled != Some(true)" in TELEMETRY_RS
    assert TELEMETRY_RS.count("cfg.telemetry_enabled != Some(true)") == 2, (
        "both track() and init_sentry() must carry the gate"
    )


def test_crash_reporting_uses_the_same_gate_as_events():
    """One switch, one consent. A crash report that shipped without opt-in
    would be the same promise broken."""
    fn = TELEMETRY_RS[TELEMETRY_RS.index("pub fn init_sentry"):]
    assert "telemetry_enabled != Some(true)" in fn
    assert "send_default_pii: false" in fn
    assert 'server_name: Some("anonymous"' in fn


def test_both_dialogs_disclose_the_install_and_session_ids():
    """These are what make installs countable across sessions, so they are
    exactly the part a person consenting would want named."""
    for dialog in _consent_dialogs():
        assert "random ID" in dialog, "install/session id not disclosed"
        assert "each launch" in dialog


def test_both_dialogs_disclose_the_per_run_counts():
    for dialog in _consent_dialogs():
        assert "how many clips" in dialog


def test_neither_dialog_claims_a_macos_version_is_sent():
    """Only the OS family ever goes out. Claiming more than is collected is
    its own kind of wrong."""
    assert "macos" in TELEMETRY_RS
    assert '("os", os_label())' in TELEMETRY_RS
    for dialog in _consent_dialogs():
        assert "macOS version" not in dialog


def test_both_dialogs_still_name_what_never_leaves():
    for dialog in _consent_dialogs():
        for promise in ("clip names", "folder paths", "face data", "people's names"):
            assert promise in dialog, f"dialog no longer promises {promise}"


def test_the_shipped_doc_matches_the_dialogs_on_what_is_never_sent():
    doc = (ROOT / "TELEMETRY.md").read_text()
    for promise in ("File paths", "person names", "face embeddings"):
        assert promise.lower() in doc.lower()
