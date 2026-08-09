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
- [x] **Labeler needs Origin checking and a CSP.** (v0.0.97) The queue was out
      of date on one point: the labeler already had a per-session token, and
      that closes ordinary CSRF on its own. What it could not cover is the
      token leaking — it rides in the iframe URL's query string, because an
      `<img>` has no way to send a header. So: an `Origin` check on writes
      (browsers set it and will not let a page forge it, absent means a
      non-browser caller and the token stands alone), `Referrer-Policy:
      no-referrer` so that token-bearing URL never leaves in a `Referer`,
      `no-store` on the HTML that embeds the token, `compare_digest` on the
      token compare, and a nonce-based CSP with `default-src 'none'`.
      Deliberately no `frame-ancestors` and no `X-Frame-Options`: the parent
      is `tauri://localhost` and WebKit cannot be relied on to match a custom
      scheme, and a blank naming step is worse than what it would prevent.
      `tests/test_labeler_headers.py` covers it, including a rendered card,
      since `default-src 'none'` is only safe while the page loads nothing but
      its own images.
- [ ] **Signing key passphrase is empty.** `~/.tauri/spotted.key` and the
      `TAURI_PRIVATE_KEY` Actions secret have no passphrase, so the key file
      alone is enough to sign an update for the whole fleet. Needs a
      passphrase + `TAURI_KEY_PASSWORD` secret. **Ask Landon before doing
      this** — it rotates the update-signing key and mis-sequencing it breaks
      the auto-updater for anyone on an older build.
- [ ] **Ad-hoc signing + dyld entitlements.** BLOCKED on disk space, see the
      Blocked section. Partly answered: `disable-library-validation` is
      definitively required and must stay. Whether the other two can go is
      still unproven, so do not ship a reduction on the strength of the note
      below.

## Deferred features

- [x] **Partial-failure Done screen.** (v0.0.98) `tag-write` exits non-zero if
      any single clip fails, so 1 bad clip in 107 rendered identically to all
      107 failing — "Couldn't finish" — which sends someone back to re-run a
      batch that was fine. The per-clip detail was already on the wire and the
      frontend was discarding it in a `console.warn`. Now kept and shown:
      "Tagged 106 of 107 clips", plus the failing filenames (capped at 8, and
      the cap announces itself). `tag-skip` was emitted by the sidecar and
      missing from the TS union entirely, so it fell through the switch; a
      test now compares emitted `tag-*` events against declared ones. The
      partial count comes from the sidecar's own tally, never from counting
      the per-clip events, which would understate damage if the sidecar died
      partway.
- [x] **Energy tagging opt-out.** (v0.0.99) A checkbox on the tags screen, on
      by default. The trap: `scan --no-energy` alone looks like a complete
      opt-out and is not, because `energy_bucket` and the peak rows persist in
      the index, so a clip scored on an earlier drop keeps them. Wired to the
      scan only, the checkbox would appear to work on a fresh folder and do
      nothing on a re-drop, which is the state a real library is usually in.
      So it acts at all three stages: `--no-energy` (stop computing),
      `--no-energy-keywords` (stop writing the keyword), `--no-energy-markers`
      (stop the peak cues, and skip the peak lookup that decides which clips
      are in the marker set at all). Kept off `--exclude-tags` deliberately —
      exclusions are persisted as review rejections and would have deleted a
      user's own tag if they had typed "high energy".
- [x] **Energy REVIEW step.** (v0.0.100) The review screen now has an Energy
      section: a row per level with clip count, a sample name and thumbnails,
      each unchecked individually. `activity-suggest` emits `energy-summary`
      alongside `activity-complete`, read off the same captured stdout.
      Two things that would have made it nearly invisible: `activity-suggest`
      returns early when the user typed no tags, so the emit had to go BEFORE
      that bail; and the frontend skipped the review whenever no tags matched,
      which is the normal state for someone who never typed any. Unchecking a
      level drops its keyword AND its peak cues, which needs gating in two
      places, because a clip can enter the marker set through its named faces
      and would otherwise collect energy cues on the way past.
- [x] **Name autocomplete in the labeler.** (v0.0.101) Two halves, because
      autocomplete alone only lowers the odds. The labeler now suggests every
      name already in the library via a native `<datalist>` (no script, which
      matters under the page's nonce CSP), and `merge_clusters_by_name` groups
      case- and whitespace-insensitively, so a second spelling stops mattering
      when someone types it anyway. The surviving row takes the spelling of
      the largest cluster; without settling that, the merge would be invisible
      in the keyword written into the file, which is the only place the user
      sees the name. Suggestions are library-wide even though the labeler is
      batch-scoped, since reusing a name from an earlier drop is the point.
- [x] **Scan ETA.** The queue was stale: the scan already had one
      (`etaSuffix`, "12 of 107 · about 3 min left"). The complaint behind it
      was still true one phase later, so (v0.0.102) the WRITE phase got the
      same treatment — it showed a filename over a moving bar with no position
      and no estimate, and it runs exiftool twice plus xattr twice per clip, so
      it is the long phase where a hang would matter most because it is the one
      touching the user's files. Both phases now share one estimator. The write
      phase times itself rather than reusing the scan's clock, which starts
      before the labeler and would date from whenever the user began typing
      names. `tag-skip` also gained a position, so a run of containers that
      cannot hold keywords stops leaving the bar parked.
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
- **Trimming the dyld entitlements** — needs disk space on this machine. The
  only honest way to test is against the real frozen sidecar, and the way to
  get one without CI is to download the shipped `Spotted.app.tar.gz`, re-sign
  the sidecar with a reduced entitlement set, and run its `selftest`. That
  needs roughly 3GB free: 690MB for the bundle and about a gigabyte more for
  PyInstaller's unpack plus CoreML's model compilation scratch. The volume sat
  at 99% (2-3GB free) and the runs became unreproducible — the same config
  passed at 3.0GB free and failed at 2.4GB with
  `Error compiling model: I/O error`, which reads like an entitlement problem
  and is not one.

  What the runs did establish, because it fails instantly and identically
  regardless of disk: **`disable-library-validation` is required.** Without it
  the sidecar dies before any Python runs, at
  `dlopen(Python.framework)` → *"mapping process and mapped file
  (non-platform) have different Team IDs"*. That is the entitlement the file's
  comment actually justifies.

  Unproven: `allow-unsigned-executable-memory` and
  `allow-dyld-environment-variables`. One clean run passed the full selftest
  with only `disable-library-validation`, and it could not be reproduced under
  disk pressure. One observation is not enough to change what the whole fleet
  auto-installs. Note also that `selftest` exercises the sidecar only — the
  Tauri parent shares the same entitlements file and cannot be tested here
  without launching the GUI.

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
- 2026-08-09 — v0.0.102: write-phase position + ETA. Preflight caught a test
  of mine from v0.0.98 that anchored on `_emit("tag-skip"` as one line, so
  wrapping the call read as the sidecar having stopped emitting it. Second
  time a literal-anchored test has mis-reported a formatting change as a
  defect; both are now whitespace-tolerant, and fixing it also un-weakened a
  neighbouring regex test that had silently stopped covering tag-skip.
- 2026-08-09 — v0.0.101: name autocomplete + case-insensitive person merge.
  This one retroactively repairs existing libraries: any already-split person
  is consolidated the next time the labeler's Done button runs the merge.
- 2026-08-09 — v0.0.100: energy review screen. Closes the asymmetry — nothing
  Spotted decides about someone's footage now reaches their files unseen.
- 2026-08-09 — v0.0.99: energy opt-out. Split the queue item: the opt-out
  shipped, the review screen is still open and re-queued above.
- 2026-08-09 — entitlement trimming attempted and NOT shipped. Proved
  `disable-library-validation` is required; could not get a reproducible
  result on the other two because this machine's disk is at 99%. Moved to
  Blocked with the method written down. No release.
- 2026-08-09 — v0.0.98: partial-failure Done screen. First tick whose change
  Ellie would actually see, but not texting her: she has not run a batch since
  2026-08-03, and this only shows up when a clip fails.
- 2026-08-09 — v0.0.97: labeler response headers + Origin gate. Preflight
  earned its keep here: the CSP nonce attribute broke the test that
  node-syntax-checks the labeler's JavaScript, which anchored on a literal
  `<script>`. Fixed the anchor, then re-broke the JS on purpose to confirm the
  test still catches what it was written for. No tester text.
