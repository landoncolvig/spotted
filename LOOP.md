# Spotted — autonomous work queue

The 30-minute loop reads this file each tick, does ONE item, ships it, and
updates the queue. Ordered: tester feedback beats everything, then whatever is
at the top of the backlog.

Shipped through **v0.0.94** (2026-08-06). Tester: Ellie. Silent since v0.0.94.

---

## Every tick, in order

1. **Check for tester feedback.** `~/.claude/pm/inbox/imessage/*.jsonl` (today
   and yesterday) for anything about Spotted. If there is any, that is the
   tick's work. Nothing else.
2. Otherwise take the **top unchecked item** below.
3. Implement it. Small enough to finish and ship in one tick; if it isn't,
   split it here and do the first slice.
4. `bash scripts/preflight.sh` — must print ALL GREEN. Never tag otherwise.
5. Bump `app/package.json`, `app/src-tauri/tauri.conf.json`,
   `app/src-tauri/Cargo.toml` together (preflight checks they agree).
6. Commit, push, `git tag vX.Y.Z && git push origin vX.Y.Z`.
7. `bash scripts/watch_release.sh X.Y.Z` — must print SHIPPED.
8. Tick the item here, commit the queue update.
9. If the change is something Ellie would notice, text her (sign `-AI`).
   Silent internal work does not earn a text.

**Do not** touch the two-frame Resolve offset without a Resolve-proven source
mapping from Ellie's real artifact. **Do not** guess at R3D decoding; that is
blocked on her sample + REDCINE-X install.

---

## Security / hardening

- [x] **Lock the main window's CSP.** (v0.0.95) Was `"csp": null` — no policy
      at all. Now `script-src 'self'`, no eval, no wildcards, with `asset:`
      and `data:` in img-src for person and activity thumbnails,
      `ipc: http://ipc.localhost` for invoke, and `frame-src` pinned to the
      labeler origin. `tests/test_webview_csp.py` pins the policy against the
      frontend that needs it — including a guard that fails if `LABEL_PORT`
      and the `frame-src` port ever drift apart, since that would render the
      naming step as an empty iframe.
- [x] **Stop the sidecar capability accepting arbitrary args.** (v0.0.96)
      Removed the whole `shell:` grant rather than narrowing it to an argument
      allowlist, which the queue originally called for. The allowlist was
      unnecessary: every sidecar spawn happens in Rust via
      `app.shell().sidecar(...)`, which performs no scope check — the scope is
      only read by the plugin's `execute`/`spawn` IPC commands, and the
      frontend never imports the shell plugin at all. So the grant was pure
      surface. Verified against the compiled ACL, not just the source.
      `tests/test_capability_scope.py` pins it, including the premise: if the
      frontend ever does import the shell plugin, the test fails and says to
      scope the permission deliberately rather than restore `args: true`.
- [ ] **Labeler needs Origin checking and a CSP.** `facetag/web.py` has no
      CSRF token, no `Origin`/`Referer` check on its POSTs, and sends no
      `Content-Security-Policy`. It binds localhost, so the threat is a
      malicious page in the user's browser POSTing names into their library.
      Reject cross-origin POSTs, add a CSP header.
- [ ] **Signing key passphrase is empty.** `~/.tauri/spotted.key` and the
      `TAURI_PRIVATE_KEY` Actions secret have no passphrase, so the key file
      alone is enough to sign an update for the whole fleet. Needs a
      passphrase + `TAURI_KEY_PASSWORD` secret. **Ask Landon before doing
      this** — it rotates the update-signing key and mis-sequencing it breaks
      the auto-updater for anyone on an older build.
- [ ] **Ad-hoc signing + dyld entitlements.** The app is unsigned and relies
      on entitlements that permit unsigned library loading. Real fix is an
      Apple Developer cert; until then, document the exposure in README and
      drop any entitlement not actually needed by the sidecar.

## Deferred features

- [ ] **Partial-failure Done screen.** Today a batch is green or it isn't.
      Show per-clip outcomes when some clips failed and others didn't, so a
      run that half-worked reads as half-worked.
- [ ] **Energy tagging reviewable / opt-out.** Energy buckets and peak markers
      are written with no review step and no way to turn them off, unlike
      faces and activity tags which both have one.
- [ ] **Name autocomplete in the labeler.** Typing a name that already exists
      in the library should complete, so "Grayson" and "grayson" stop being
      two people.
- [ ] **Scan ETA.** Long scans show progress with no time estimate; on a
      200-clip drop the user can't tell a slow run from a hung one.
- [ ] **Per-clip pruning in the activity review.** Drop a tag from ONE clip
      rather than dropping the whole tag.
- [ ] **Report export.** Write what was tagged to a file the user keeps.
- [ ] **iCloud pre-check.** Warn before scanning a folder whose files are
      not downloaded, instead of failing per clip.

## Roadmap (multi-tick — split before starting)

- [ ] **Backup + undo for Reset Library.** Ranked #4 in ROADMAP.md, half a
      day, cheap to reverse. One bad click currently loses every name the
      user has typed.
- [ ] **Opt-in telemetry + error reporting.** Ranked #2. Blind on where users
      drop off. Local-first product, so this must be opt-in and say what it
      sends.
- [ ] **In-app library search.** Ranked #5.
- [ ] **Keyboard-driven labeler.** Ranked #6.
- [ ] **Bundle diet.** Ranked #8. 720MB and growing; drop transformers, slim
      the tokenizer.

## Blocked

- **R3D / RED direct support** (DAYTA-23) — needs a real .R3D sample from
  Ellie and confirmation REDCINE-X is installed on her machine.
  `scripts/redline_probe.sh` reports what REDline can do once she has it.
- **Two-frame Resolve offset** — needs a Resolve-proven source mapping from
  her real artifact.

## Waiting on Ellie

v0.0.94 fixes she has not confirmed: face thumbnails in the naming step,
dead frames on 24+30fps batches (timeline now 120fps, deliberately), squashed
portrait frames degrading detection, labeler "failed to start" at 200+ clips.

---

## Log

- 2026-08-09 — queue created, loop restarted after a 2-day gap.
- 2026-08-09 — v0.0.95: main-window CSP locked down. No tester text; nothing
  she can see changed, and a CSP is only news if it broke something.
- 2026-08-09 — v0.0.96: dropped the webview's shell grant entirely. No tester
  text, same reason.
