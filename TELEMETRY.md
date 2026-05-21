# Spotted — Telemetry

Anonymous, opt-in, fire-and-forget. Off by default to align with Spotted's
local-first privacy story.

## What's collected

Each event sends:

- **Event name** — `app-launch`, `scan-complete`, `tag-write-complete`,
  `activity-suggest-complete`. Categorical labels; no free-text user input.
- **App version** — e.g. `0.0.34`. So we can slice metrics by release and
  catch a regression that only hits one version.
- **OS family** — `macos`, `windows`, `linux`, `other`. So we can see who's
  on what when we add cross-platform.
- **Status** — `ok` / `error` on most events, so we know how often things
  fail in the wild.
- **Hashed install ID** — SHA-256(UUID + salt). Stable per install, opaque
  to TelemetryDeck. Used only to count distinct active installs.
- **Session ID** — random UUID generated per launch. Lets us group events
  from the same session without identifying the user across sessions.

## What's never collected

- File paths, folder names, clip names, person names, tag names.
- Face embeddings, photo content, frame content, audio.
- IP address (TelemetryDeck drops it server-side).
- Email, account info, machine identifiers beyond the install UUID.
- Anything from inside `~/.facetag/`.

If a sensitive field could be inferred from a payload, the payload doesn't
get added. When in doubt, leave it out.

## How the user controls it

- **First launch**: native dialog asks once. Choice persists in
  `~/Library/Application Support/Spotted/config.json`. No is the default; if
  the user dismisses the dialog without picking, nothing sends.
- **Anytime after**: Spotted menu → Telemetry… toggles it on or off.
- **Hard kill switch**: delete `~/Library/Application Support/Spotted/`. The
  install ID and all telemetry state go with it (next launch generates a
  fresh ID and asks again).

## Where the data goes

[TelemetryDeck](https://telemetrydeck.com/) free tier (under 100K signals/
month). Privacy-focused, no cookies, no fingerprinting, drops IPs.

The TelemetryDeck app ID is baked in at compile time via the
`SPOTTED_TELEMETRY_APP_ID` env var. If unset (typical for local dev
builds), all telemetry calls become no-ops regardless of user opt-in — so
your `cargo run` launches never pollute prod metrics.

## How to set up

1. Sign up at https://telemetrydeck.com/ (free).
2. Create an app named "Spotted".
3. Copy the app ID from the dashboard.
4. In `.github/workflows/release.yml`, add to the build step:

   ```yaml
   env:
     SPOTTED_TELEMETRY_APP_ID: ${{ secrets.TELEMETRY_DECK_APP_ID }}
   ```

5. Add the app ID to GitHub repo Secrets as `TELEMETRY_DECK_APP_ID`.

That's it. The next release ships with telemetry compiled in; users who
opt in start sending events.

## Crash reporting (Sentry)

Telemetry events tell us *what* users do. Sentry tells us *what broke*. They
complement each other and share a single opt-in flag — one consent dialog,
one toggle in Spotted → Telemetry…, both turn on or off together.

### What Sentry captures

- **Panics** in the Rust code (the Sentry `panic` integration is installed
  on init). Stack traces, the panic message, app version, OS.
- **Anonymous user ID** — same SHA-256(install_id + salt) we use for
  TelemetryDeck. Lets us count distinct crashing installs without ever
  linking back to a human.
- **Release tag** — `0.0.X` from `CARGO_PKG_VERSION`. So we can see
  "v0.0.31 crashes 4x more often than v0.0.30" without manual labeling.

### What Sentry does NOT capture

- `send_default_pii` is explicitly `false`. Sentry won't auto-attach the
  user's email, IP, hostname, or any other identifying field that the
  default config would grab.
- We never call `sentry::capture_message` with user content — only
  panic-derived messages, which are our own strings.
- JS-side errors are NOT yet wired to Sentry. They go to the devtools
  console for now. Add `@sentry/browser` later if we need that visibility.

### How to set up

1. Sign up at https://sentry.io (free tier: 5K errors/month).
2. Create a project named "Spotted" (platform: Rust).
3. Copy the DSN.
4. Add to GitHub repo Secrets as `SENTRY_DSN`.
5. Next release auto-picks it up. Until then, Sentry init is a no-op.

## Why TelemetryDeck and not something else

Considered alternatives:

- **PostHog Cloud Free** — generous tier (1M events/mo) but designed for
  web product analytics; heavier than we need. The session-recording
  feature is irrelevant to a desktop app.
- **Aptabase** — desktop-first, would have worked. Picked TelemetryDeck for
  the explicit privacy-by-design (hashed IDs are non-optional, IPs dropped
  server-side) which mirrors how Spotted itself works.
- **Sentry only** — too narrow for usage analytics; great for crash
  reports specifically. Plan to add Sentry separately for error reporting.
- **Self-hosted on Cloudflare Workers** — total cost $0, full control. Ruled
  out because the dashboard tax (we'd have to build our own) outweighs the
  vendor cost (which is $0 in our tier).

## How to add a new event

In Rust:

```rust
use std::collections::HashMap;
let mut payload = HashMap::new();
payload.insert("clips_named".into(), count.to_string());
telemetry::track("library-opened", payload);
```

In TypeScript (one-shot from the frontend):

```ts
await invoke("track_event", {
  name: "welcome-dismissed",
  payload: { variant: "got-it" }
});
```

Keep event names hyphenated and verb-first ("scan-complete", not
"completedScan"). Payload values must be strings — convert numbers via
`.to_string()` in Rust or template literals in TS.
