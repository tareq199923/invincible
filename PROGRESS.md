# PROGRESS.md — session diary (committed, kept short)

> Read at clock-in, update at clock-out. This is the cross-session memory.
> Active feature lives in `feature_list.json` (WIP=1: only one `active`).

## Current State

- Item-1b done (`passing`): tip only when working + dock lift
  (`agent/overlay.py`: pure `tip_alpha` 1.0 while flight/drag/press-window
  else 0.0, `_draw` hides tip polygon when docked, head + eyes always on,
  `state["dragging"]` from existing IPC key, `OVERLAY_DOCK_LIFT=48` raises
  br/bl docks above the taskbar, top corners unchanged).
  New pins in `tests/test_overlay.py` (3 tests: tip dock/fly/press/drag,
  lift clears taskbar; dock corner pins updated for the lift).
  `ruff` clean, 60 passed overlay/computer slice, full
  1959 passed / 11 skipped / 1 deselected.
- Item-1 done (`passing`): smoother cursor — Bluey motion port
  (`agent/overlay.py`: cubic `timing_value/rate` launch/redirect/steady,
  `flight_position/tangent`, `plan_flight` alongside frozen
  `bowed_waypoints`, `press_scale`/`hover_offset` overlay-only affordance;
  `_run_window` target path uses child-stamped monotonic flights with
  velocity-carry redirect, dragging straight/steady via
  `announce(dragging)` IPC (missing key=False); `runner` passes
  dragging only for drag jobs so click call shape is unchanged).
  New pins in `tests/test_overlay.py` (8 tests: easing, bow direction,
  no-kink carry, drag/exact-land, press/hover boundaries, drag IPC +
  drag-job). `ruff` clean, 57 passed overlay/computer slice, full
  1956 passed / 11 skipped / 1 deselected.
- Overlay look pass done (`passing`, uncommitted): Bluey-like berry head —
  stacked light/mid/deep gradient, white rim, teardrop tip, shine dot,
  faint glow; eyes/tracking unchanged, original art. Pins for
  `berry_shades`/`teardrop_points` in `test_overlay.py`.
- F18 done (`passing`, uncommitted): chord fix from the live transcript —
  `press_keys` modifier chords dropped their modifier (ctrl+z typed bare
  `z`) because single-char mains rode KEYEVENTF_UNICODE, which ignores
  held modifiers. Chord mains now ride virtual-key codes
  (`_COMPUTER_VK_ALNUM`); modifier-free presses keep the Unicode path.
  New pins in `tests/test_computer_use.py` (VK-chord + Unicode-bare).
  `ruff` clean, 128 passed in computer/tool-executor slice.
- F17 done (`passing`, committed as `94a99ab`): loop-cost guidance — `environment_note`
  teaches lean turns in all modes (skip todo/memory overhead on 1–2 tool
  tasks) plus manual/auto computer-use discipline (default-browser
  `open_url`, dispatched-not-verified + fresh-look verification, media
  probes only on ask). Static prompts and tool goldens untouched. New
  pin `test_environment_note_lean_turn_discipline`. `ruff` clean,
  65 passed in webchat/golden/registry slice.
- Hygiene (`passing`, committed as `12b7313`): full suite 1944 passed, 11 skipped,
  1 deselected (`pytest -q`, ~7.5 min, PG reachable); `ruff check .`
  clean; only `?? .vscode/` untracked (pre-existing, not ours).
  Zero `active` items (F05 vector seam stays `planned`).
- F16 done (`passing`, committed as `53c884d`): overlay Bluey states — press squish
  on target arrival, hover grow while gliding, fading trail, `⌖ x,y`
  bubble (`agent/overlay.py`: `head_radii`/`trail_push`/`bubble_text_for`
  pure + `_run_window` wiring; runner/registry untouched, headless
  latency unchanged). New pins in `tests/test_overlay.py` (3 tests).
  `ruff` clean, 46 passed in overlay/computer slice.
- F15 done (`passing`, committed as `16513f5`): coordinate parity — SendInput moves
  carry VIRTUALDESK (`core/tool_executor.py`), `look_at_screen` maps UIA
  rects + mouse against virtual origin/size via `look_grid_virtual`
  (inverse of `overlay.grid_to_pixel`), overlay already virtual. New pins:
  virtual round-trip (`test_look_at_screen.py`), VIRTUALDESK flag
  (`test_computer_use.py`). Live multi-monitor glide-vs-click check still
  deferred to a visible Windows desktop. `ruff` clean, 50 passed in
  look/computer/overlay slice.
- F14 done (`passing`, committed as `cda787a`): loop-cost guard — `IDENTICAL_LOOK_LIMIT=3`
  consecutive identical `look_at_screen` results end the tool loop via the
  repeat-guard close-out (`core/webchat_agent.py`: `_look_result_key` +
  non-resetting counter, unchanged-screen close-out note). Interleaved
  actions don’t reset it; differing looks don’t trip it. New pins in
  `tests/test_webchat_agent.py` (key unit + trip/no-trip integration).
  `ruff` clean, 60 passed in webchat/look/computer slice.
- F13 done (`passing`, committed as `bd2ed83`): computer-use honesty — `result_ok`
  counts `unavailable` as not-ok (`core/tool_dispatch.py`), stale C ids
  return error results via `ValueError` catch (`core/tool_executor.py:
  _computer_act`), every `ok` carries `verified: False` (dispatched, not
  pixels verified) + SECURITY §12 known-limits note. New pins in
  `tests/test_computer_use.py` (3 tests). `ruff` clean, 157 passed in
  computer/tool/webchat slice.
- Phone markdown follow-up (`passing`, code committed as `fd5631e`): `POST
  /dashboard/chat/render` (cookie realm, presentation-only, same
  `render_chat_markdown`) lets the PWA match desktop chat without
  touching the `/v1/*` wire shape; `phone.html` renders assistant
  replies via it (textContent fallback, speech still plain `answer`)
  and now clears the `pending` faint class on success. Realm gates
  extended, new `test_render_markdown_xss_and_bounds`. `ruff` clean,
  29 webchat + 44 voice passed.
- Chat markdown done (`passing`, committed as `6c2e20a`): assistant bubbles now render
  markdown via new `core/markdown.py:render_chat_markdown()` (markdown-it
  CommonMark + tables, raw HTML disabled so `<script>`/`<img onerror>`
  escape as text). `endpoints/chat.py:_renderable_turns` pre-renders
  history `html`, `chat_stream:done` sends markdown `bubble_html`
  (streaming stays plain textContent, swaps on done); `templates/chat.html`
  renders `t.html | safe` in `.md`, user bubbles stay plain; `app.css`
  adds scoped `.md` styles (headings/lists/code/pre/table/quote).
  New `tests/test_chat_markdown.py` (6 hermetic); updated
  `test_stream_happy_path_and_history` bubble expectation. `ruff` clean,
  34 webchat+markdown passed.
- Embodied slice done (`passing`, uncommitted): chirp + anchored target tag +
  bowed flights — `voice-orb.js:playChirp()` two-tone blip on mic replies
  only (fail-silent); `run_agent_turn` tracks `last_target` into `done.target`
  and `chat.html` appends `⌖ <target>` to bubble meta (textContent-safe);
  `overlay.bowed_waypoints()` arcs target glides (8%/48px up, exact landing),
  settle stays straight, dwell/headless unchanged. Pins in `test_voice.py`,
  `test_webchat_agent.py`, `test_overlay.py`.
- Bluey steal pack 4 done (`passing`, committed as `836fd9e`, pushed): prompt split + asked flag —
  `PERSONALITY/SAFETY_GUIDE/TOOL_GUIDE/COMPUTER_GROUNDING` split in
  `core/webchat_agent.py` (manual uses shared grounding, byte-identical);
  `asked:true` mic flag persisted in message JSONB (no migration), stripped
  before upstream in `core/chat_service.py`, rendered as `You asked:` by
  `core/projection.py:format_agent_prompt()`; webchat `chat.html` sends
  `asked`, PWA `phone.html` pushes it into `/v1` history; dashboard
  `chat.py:_user_message()` accepts it. New: `tests/test_agent_prompt.py`
  (6 tests). Full gate: `ruff` clean, 1922 passed, 11 skipped, 1 deselected.
- B9 done (`passing`, committed as `3ae6c82`, pushed): Gemini-style composer bar — mic orb
  + send live inside one rounded row (`composer-actions`) on webchat +
  PWA; orb is a quiet `currentColor` mic glyph on theme vars with
  ring-pulse states only; mic moved right of the input like Gemini.
  Behavior/realms/ids unchanged. Static test pins the bar + skin.
- B8 done (`passing`, committed as `394edcc`, pushed): speak only on mic turns — orb sets
  a per-turn flag consumed at submit; typed turns stay text-only on
  webchat + PWA. Static test pins the gating; `ruff` clean; 91
  voice+webchat+handoff passed.
- B7 done (`passing`, committed as `49d4139`, pushed): free browser voice fallback — orb
  uses device-local Web Speech recognition + speechSynthesis when the
  server has no audio surface; no fetch, no keys, tap-to-stop covers
  both; server voice preferred when credentials exist, voice-off never
  falls back. Static test pins the fallback strings; docs: PROVIDERS
  matrix, SECURITY device-local note, phone hint.
- B6 done (`passing`, committed as `3603d47`, pushed): free mic-in on Groq —
  `whisper-large-v3-turbo` per-provider STT (OpenAI stays `whisper-1`
  first); Groq TTS still clean 502 text-only. 41 voice tests green;
  `ruff` clean; docs: PROVIDERS matrix, SECURITY §14.
- B5 done (`passing`, committed as `b663a68`, pushed): voice 402 now shows the provider's
  reason (e.g. `Transcription refused (HTTP 402: Insufficient
  credits.)`) via safe body excerpt in `core/voice.py`; no keys in
  messages. New test pins it. `ruff` clean; voice+webchat+handoff 89
  passed.
- B4 done (`passing`, committed as `bb3bee9`, pushed): voice reliability + orb — OpenAI-base
  credentials tried first, 404 skips to next (all-404 is a clear 502
  "no transcription/speech surface; connect OpenAI", never
  "Transcription refused HTTP 404"); audio models pinned to
  whisper-1/tts-1 (never the chat model_id); legacy `baymax` maps to
  `alloy`. New shared `templates/static/voice-orb.{css,js}` (pulse by
  mic RMS, listening/working/speaking states, tap-to-interrupt,
  textContent-only) replaces mic+mute on `chat.html` (`#voice-orb`)
  and `phone.html` (`#phone-orb`, drops Web Speech wake path, sends
  `alloy`); realms unchanged (cookie vs inv_ key), text stays source
  of truth. New: 6 tests in `tests/test_voice.py` (404-skip both
  directions, model pinning, alloy mapping, ordering, orb markup);
  docs: SECURITY §14, PROVIDERS voice matrix. FULL suite 1912 passed
  (1906 baseline + 6), 11 skipped, 1 deselected; `ruff check .` clean.
- B3 done (`passing`, committed as `ff46c9e`, pushed): in-app voice — mic PTT in + spoken
  Bmax out + ephemeral tokens. New `core/voice.py` (stateless HMAC
  `vv1.{user}.{scope}.{expiry}.{device}` tokens, 5-min TTL, `stt`/`tts`
  scope + device binding, fail closed without `INVINCIBLE_OWNER_SECRET`;
  BYOK-only `transcribe_audio`/`synthesize_audio` with caps + 502
  text-only fallback; `voice_allowed()` = kill-switch AND per-user
  `voice` override) + `endpoints/voice.py` (`/v1/voice/token`,
  `/v1/audio/transcriptions` raw-body upload, `/v1/audio/speech` in the
  inv_ realm with matching-scope token auth, `/dashboard/voice/*`
  triplets in the session-cookie realm — realms never mix, dashboard
  ignores voice bearers). `settings.voice_enabled()` (`INVINCIBLE_VOICE`,
  no raw env reads) + `voice` in `OVERRIDABLE_KEYS` + Settings page
  toggle. PWA `phone.html` (tap-record → upload → send, spoken replies
  + mute) + webchat `chat.html` (mic → transcribe → auto-submit, speak
  on done + mute) — text bubbles stay the source of truth, all inserts
  textContent-only, JS node-checked. Zero tools (goldens/drift frozen);
  no migration (no new tables); no python-multipart (raw-body upload).
  New: `tests/test_voice.py` (33 hermetic tests: mint/verify,
  tamper/expiry/scope theft refused, cross-user + cross-realm refused,
  key-material scans, kill-switch/mute fail-safe with text chat green).
  Docs same PR: SECURITY §14, ARCHITECTURE §6c, PROVIDERS voice matrix,
  CONFIGURATION row. F05 vector seam still deferred.
- B2 done (`passing`, committed as `2d1248b`, pushed): phone app + Hey Baymax hop phone → PC.
  Presence reuses hello/machines (zero new tables — phone hellos with
  `platform: *-pwa` + `capabilities: {phone}` read via
  `GET /agent/machines|status` as `{online, device}`). Handoff is a
  fire-and-forget control message (NOT a tool job — 34-tool
  goldens/drift frozen): `POST /agent/handoff` (inv_ realm, no
  target-user field, 400 on blank/overlong, 503 kill-switch) →
  per-user `_handoffs` queue (bounded 10, oldest-first, prune-aware) →
  WS-first `push_ws` (drop queued copy on relay) with poll fallback
  (`"handoff"` key additive-only so `{"job": null}` shape is intact).
  PC runner `handle_handoff()` wakes the B1 overlay best-effort, never
  breaks jobs. PWA-first `GET /dashboard/phone` (cookie page, inv_ key
  in localStorage only): big mic + in-app "Hey Baymax" (Web Speech,
  PTT fallback), Bmax face (`.baymax-dot` reuse), bubbles over
  `POST /v1/chat/completions`, Hop-to-PC with "hopping to PC…" →
  ack. Kill-switch `INVINCIBLE_HANDOFF` via
  `settings.handoff_enabled()` (no raw env reads). Restart orphans
  queued handoffs — documented in ARCHITECTURE §6b + SECURITY §13,
  not fixed. Docs same PR: SECURITY §13 (owner binding, no LAN trust
  vs Bluey Bonjour, fail-closed), MCP_PROTOCOL §8, ARCHITECTURE §6b,
  CONFIGURATION row. New: `tests/test_handoff.py` (21 tests).
  Meeting-notes reuse deferred to follow-up per lock.
- F10 done (`passing`, uncommitted): Pack 3 computer-use — click,
  type_text, press_keys, scroll, drag, open_app, open_url as
  agent-side tools on the paired PC (SendInput/ShellExecute via
  ctypes, no COM in-process; control resolution reuses the
  look_at_screen read, password probe isolated like F09 with
  GetFocusedElement idx 8 + IsPassword 30019 verified live).
  needs_approval=True (MUTATING 3→10, manual/auto only),
  agent-only (MCP 27→34, webchat order 24→31). Two routing traps
  closed: pre-AGENT_ONLY staged branch + batch-gate MUTATING
  exclusion (regression test fails without it, fast). Denylist:
  win+l/alt+f4/ctrl+alt+del/ctrl+shift+esc Wall 1+2; fail-fast
  staging (no dangling tokens); result-echo of resolved targets.
  New: `tests/test_computer_use.py` (16 tests); goldens regenerated
  from code then frozen; drift pins extended (never weakened); docs:
  MCP_PROTOCOL §7 tools, SECURITY §12, ROADMAP count 34. Vetted
  plan survived contact: one real find during vetting (the batch
  trap), confirmed in code before building.
- Last commit: `53c884d` (main) — F16 overlay Bluey states.
- F12 done (`passing`, uncommitted): multimodal image parts — the model
  truly sees screenshots (new pure `invincible/core/vision.py`:
  extract/attach around trim, PNG-IHDR token estimates, 4-image +
  512KB caps with drop-with-note). Router pipeline is now
  vision-extract → compress → relay → trim → vision-attach, per
  attempt; attach matches by tool_call_id (pairing-neutral, same
  message). Gating: `vision` bool in providers.yaml schema +
  catalog (openai true, rest fail-closed false) + BYOK candidates.
  Two live finds during build: (1) compression's 4000-char tool cap
  would slice blobs into garbage — extraction runs FIRST; (2) the
  wiring itself needed the same care (send_messages clobber).
  `estimate_tokens` counts image parts from the image, not base64.
  New: `tests/test_vision.py` (16 tests incl. byte-exact large-blob
  wire test) + catalog pin; docs: ARCHITECTURE §4b, PROVIDERS schema
  + compat matrix. Zero registry surface; persisted shapes unchanged.
- F11 done (`passing`, uncommitted): B1 head-cursor overlay — Bmax-head
  cursor on the paired Windows machine (new stdlib-only
  `invincible/agent/overlay.py`: pure grid/ease/eye/dock helpers +
  Tk click-through window + child-process `Overlay` handle with
  stdin-pipe IPC; original soft-white head + two ink eyes, no Disney
  likeness). Runner announces every resolved Pack 3 point via
  `_computer_resolve(..., on_point=)` with a ~350ms pre-action dwell
  ONLY when an overlay is alive (headless jobs pay zero latency);
  settle-to-dock after each job; autostart on connect (Windows),
  `INVINCIBLE_OVERLAY=0` kill-switch via `settings.overlay_enabled()`,
  dead-overlay retry cooldown 60s. Isolation: hook exceptions
  swallowed in `_notify_point`, all sends best-effort, Tk closes
  whenever click-through fails to stick. Zero registry surface
  (goldens/drift untouched). New: `tests/test_overlay.py` (22 tests:
  hermetic geometry/easing/eye/dock/round-trip + handle/wiring +
  Windows-gated live metrics proving the SM_* constants). Docs:
  CONFIGURATION `INVINCIBLE_OVERLAY` row.
- Rename (uncommitted): Baymax → Bmax everywhere user-visible (webchat
  voice prefix, avatar label, overlay docs/ticket text). Internal
  identifiers kept (`BAYMAX_VOICE`, `.baymax-dot`, `ensureBaymaxMark`,
  test/file names) — zero churn; pins updated, suites green.
- F09 done (`passing`): look_at_screen controls grounding
- F07 done (`passing`): Bmax B0 browser-first (voice + avatar +
  screenshot preview).
- F08 done (`passing`): web_research tool (Bluey steal pack 1,
  Responses web_search via caller's BYOK credential).
- F09 done (`passing`, committed as `062ece5`): look_at_screen controls grounding
  (Bluey steal pack 2). `tool_executor._look_at_screen` + UIA walk
  (verified live: 112 controls, menus+buttons on 0-1000 grid); walk runs
  in a child process (raw COM can fail fatal - crash becomes an error
  result, agent survives); runner case, registry + order (agent-only,
  all modes incl plan), dispatch trio, summary previews, goldens, drift
  pins (27 tools), `MCP_PROTOCOL.md`. New:
  `tests/test_look_at_screen.py` (6 tests).
- Ruff/pytest status: `ruff check .` clean; FULL suite 1906 passed
  (1873 baseline + 33 voice), 11 skipped, 1 deselected.
  Clean-state checklist 5/5 ticked. Uncommitted — commit on user's word.
- F04 done (`passing`): read-only fan-out reframed + built as
  `core/harness_fanout.py` (planner + parallel scout/librarian steps,
  main loop synthesizes; trigger ≥6 reads/≥2 planes, once per turn
  shared with explore) + `tests/test_harness_fanout.py` (18 tests).
- F03 done (`passing`, uncommitted): H6b tunneling — `expose_port`
  staged + confirmed, WS-multiplexed bytes, `/t/{token}/` capability
  URLs, caps + audit. New: `core/harness_tunnel.py`,
  `endpoints/tunnel.py`, `tests/test_tunnel_registry.py` (25 tests) +
  `tests/test_tunnel_route.py` (18 tests); edited: settings, policy,
  tools registry (+2 goldens, additive), `mcp.py`, `agents.py`,
  `runner.py`, `sandbox.py`, `tool_executor.py` (`peek_type`), main +
  conftest wiring; docs: SECURITY §11, DEPLOYMENT posture, HARNESS_PLAN
  H6b, ROADMAP Phase 10, MCP_PROTOCOL entries.
- Ruff/pytest status: `ruff check .` clean; audit 5/5; tunnel suites
  43 passed; FULL suite 1768 passed, 11 skipped, 1 deselected.
  Clean-state checklist 5/5 ticked. Uncommitted — commit on user's word.

## In Progress

- None — Item-1b complete (`passing`).
  Zero `active`. Next: F05 vector seam at next clock-in.

## Next Steps

1. F05 vector seam remains `planned` — next candidate; set `active`
   only at next clock-in.
2. Live Tk window (glide/dock/click-through visuals) still unverified —
   needs a visible Windows desktop session; hermetic + read-only live
   metrics are green.
3. F04 fan-out (`harness_fanout.py` + 18 tests) stays green, untouched.

## Blockers

- None.
