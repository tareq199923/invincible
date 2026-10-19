# PROGRESS.md — session diary (committed, kept short)

> Read at clock-in, update at clock-out. This is the cross-session memory.
> Active feature lives in `feature_list.json` (WIP=1: only one `active`).

## Current State

- F26 done (`passing`): overlay work-area docking + opt-in DPI. The
  companion face and parked pointer now dock on the PRIMARY monitor's
  work area (screen minus the real taskbar) instead of the virtual-
  desktop bounding-box corner, which can be empty space when monitor
  heights differ. New pure helpers: `work_area()` (`SPI_GETWORKAREA`,
  `None` off Windows/on failure), `dock_area()` (validates the work
  area is positive and inside the virtual screen, returns it with
  `OVERLAY_WORKAREA_LIFT`; junk/missing/out-of-desktop falls back to
  the old `vrect` + `OVERLAY_DOCK_LIFT`), and `shift_rect()` (absolute
  rect -> child canvas space). `dock_corner`/`face_box`/`pointer_home`/
  `pointer_at_home` take an optional lift; `_run_window` uses
  `drect`/`dock_lift` for the face, home, dock, and hover checks. The
  runner's pre-action dwell now seeds from the same `dock_area` +
  `pointer_home(..., lift)` so the glide estimate matches the child.
  DPI is strictly opt-in: `settings.overlay_dpi_aware()` owns
  `INVINCIBLE_OVERLAY_DPI_AWARE` (`1`/`true`/`on`/`yes`; default off),
  and `overlay_main` calls the best-effort per-monitor-v2 ->
  per-monitor -> system-aware setup before Tk starts only when set.
  No tool, registry, approval, or model-visible surface changes; a
  missing work area is a strict no-op. Docs: CONFIGURATION overlay row
  (work-area dock + fallback) and new DPI row, TESTING F26 pins.
  Tests: seven overlay helper/settings pins (lift override, multi-
  monitor empty corner, one-screen 24px rise, fallbacks, negative
  origin canvas shift, live shape, DPI opt-in) plus the runner's
  work-area dwell seed. `ruff` clean; audit 11/11; targeted 166 passed;
  FULL suite 2075 passed / 11 skipped / 1 deselected (Windows; CI-shaped
  passwordless test DSN + `PGPASSWORD`). Live click-through and the
  multi-monitor glide-vs-click check were not run (no paired desktop
  session). Clean-state checklist 5/5 ticked. Committed as `262746b`.
- F25 done (`passing`): `open_app` launch wait (Tier 2a follow-up of the
  repeat-app efficiency plan). New optional
  `window`/`wait_kind`/`wait_label`/`timeout_ms` on `open_app` using
  `look_at_screen`'s selector/wait vocabulary (launch default 8000 ms,
  cap 10000; `timeout_ms` alone is ignored). `look_call_args` gained
  `default_ms`/`wait_window`; `normalize_computer_args` validates and
  stages the wait spec (raw selector + resolved kind/label/timeout/flag)
  and is idempotent under the agent's Wall 2 re-normalization;
  `_look_walk` polls for the selected window itself when `wait_window`
  (a bare selector is ready as soon as the window exists; controls may
  still be rendering); `_computer_act` merges the first look payload
  flat into the `ok` launch result (look `status` dropped, launch fields
  win, `verified: false` kept) and degrades to `ready: false` + `note`
  when the readiness read fails (the app did open). Plain
  `open_app(name, args)` is unchanged in behavior, result shape, and
  staged shape; look's 3000 ms default and wire shape are untouched.
  Guidance: `COMPUTER_EFFICIENCY` prefers one-call
  `open_app(window=..., wait_kind/wait_label=...)` with
  `look_at_screen`'s wait as the already-open fallback; plan stays
  read-only; F13/F14/F17 preserved. Docs: MCP_PROTOCOL open_app section,
  ROADMAP MCP + Dashboard rows, ARCHITECTURE environment_note sentence,
  SECURITY §12 readiness-snapshot note. Tests: wait-arg
  normalization/idempotency, merge shape + drift pins, plain launch
  never reads, timeout/reader-failure honesty, stage→confirm forwarding,
  look helper/schema pins, webchat guidance pin; MCP/webchat goldens
  updated additively. `ruff` clean; targeted 141 passed; FULL suite 2074
  passed / 11 skipped / 1 deselected (Windows); live Windows checks:
  launch+filter matched Notepad's text field (C12) and the bare-selector
  mode returned `ready` with 20 controls. Clean-state checklist 5/5
  ticked (build, tests, feature list, no artifacts, PROGRESS).
  Committed as `4574e36`.
- F24 done (`passing`): Tier 1 lean-turn computer-use guidance (repeat-app
  efficiency plan). New `COMPUTER_EFFICIENCY` constant in
  `core/webchat_agent.py`, appended to the manual/auto `environment_note`
  guidance in both executions (local + agent): capability ladder
  (open_url/open_app or CLI launch/deep link > press_keys > look_at_screen
  + C-id click > pixel/vision guessing), 3+ deterministic-step batching
  into one `execute_bash` script with in-script waits (hard-to-undo steps
  stay out), cold-app window waits via
  `look_at_screen(window=process:..., wait_kind/wait_label=...)`, the
  launch/focus -> process:/title: -> stable anchor -> keyboard-first ->
  one verify playbook, and one final look instead of a look after every
  action. Reconciliation stays explicit: a C-id click still needs its
  grounding look (ids go stale on a re-read) and a fresh look follows a
  screen change. Static `MODE_SYSTEM_PROMPTS`, plan-mode read-only
  guidance, F13/F14/F17 wording, tool schemas/goldens, approvals, and
  agent/runner code are untouched; the user's overlay WIP stayed
  unstaged. Tests: three new pins in `tests/test_webchat_agent.py`
  (manual/auto x local/agent ladder + batching + cold-app + playbook +
  final-look; plan read-only; F13/F17 preservation). Docs: ARCHITECTURE
  webchat agent-modes sentence + ROADMAP webchat row clause. `ruff`
  clean; audit 11/11; targeted 83 passed; FULL suite 2065 passed /
  11 skipped / 1 deselected (Windows). Clean-state checklist 5/5 ticked.
  Committed as `f3bf22f`.
- CI fix (`98cba4e`, follow-up to F23): the F23 commit accidentally
  swept in one uncommitted `runner._overlay_delay_fn` hunk from the
  work-area docking branch (`overlay.dock_area`/`work_area`) without
  the matching `overlay.py` helpers, so CI run #320 fell back to the
  0.35s base dwell and failed
  `test_overlay_delay_fn_covers_planned_flight` on all five Pythons.
  Reproduced on Linux (WSL Ubuntu 26, Python 3.14, PostgreSQL 18):
  `dwell 0.35 < flight 0.8956`; reverted the hunk to
  `overlay.pointer_home(vrect)`. The user's work-area overlay WIP
  (`overlay.py`, `test_overlay.py`, uncommitted) stays untouched —
  re-apply the dock-aware dwell when that feature lands. CI-exact
  Linux run (coverage): 2061 passed / 5 skipped / 1 deselected.
  Committed as `98cba4e`; pushed; CI run #321 pending.
- F23 done (`passing`): `look_at_screen` window targeting + readiness
  wait (Tier 2a of the repeat-app efficiency plan). New pure helpers:
  `look_window_target` (`process:spotify`, `title:<substring>`,
  `pid:<n>`, or a bare process/title; foreground by default),
  `look_window_match` (process basename, so a player's changing track
  title no longer breaks the target), `look_wait_match` (kind + a
  case-insensitive collapsed-label substring), `look_call_args`
  (wait defaults 3000ms, caps 10000, ignored without a filter), and
  `look_window_label` for honest timeout notes. The isolated child
  walk now resolves the window and re-walks in place every 0.15s
  until a control matches; the payload adds `window`
  `{title,process,pid}`, `ready`, `matched`, `waited_ms`, and a
  `note` on an empty/timeout read. A timeout is a `status: "look"`
  result (last walk + `ready:false`), not an error. Named targets
  prefer the foreground match, then the largest visible window;
  process names come from `QueryFullProcessImageNameW` (no new dep).
  Runner forwards only the supplied args; dispatch/webchat/MCP pass
  the job args through. Regression guard: F14 `_look_result_key`
  drops `waited_ms` (timing noise) so unchanged-screen detection
  still fires; `harness_tools` keeps the wait-kind enum literal and a
  pin test ties it to `LOOK_KIND_NAMES`. Docs: MCP_PROTOCOL (args,
  result, truncated/wait caveat), SECURITY (readiness is not a
  lock), ROADMAP. Live-verified: process-targeted read (brave.exe,
  117 controls), wait hit `C1` in 264ms, 300ms miss → `ready:false`
  + note, malformed pid refused. `ruff` clean; targeted 187 passed;
  FULL suite 2055 passed / 11 skipped / 1 deselected. (Two later
  reruns lost random DB-backed tests to another session truncating
  `invincible_test` — 8 then 7 unrelated failures, each green in
  isolation.) Clean-state checklist 5/5 ticked. Committed as
  `2c6a570`.
- F21 done (`passing`): overlay pointer + companion face — the single
  gliding head split in two. `agent/overlay.py`: the moving piece is a
  cursor-like pointer (classic up-left arrow, tip = hotspot, so an
  announce lands the TIP on the action pixel) carrying the pink robot
  face (two eyes, a wink slash, a zigzag mouth), and every F16 effect
  moves with it (Bluey flights, landing tap via `press_scale` + land
  ring, coordinate bubble, 18pt typing glyphs, fading trail drawn
  under it). The stationary piece is a web-style squircle face,
  `OVERLAY_FACE_PX` 110, shifted `OVERLAY_FACE_SHIFT_LEFT` 52 left of
  the dock corner (toast lane clear); glow + ring are squircle
  polygons (a circular glow crossed the corners); face eyes draw at
  `OVERLAY_FACE_EYE_SCALE` 1.5 so the Bmax proportions hold; the shine
  nudged up-left out of the enlarged eye; the face owns every Item-7
  state (working squint / searching sweep / happy bounce in place /
  error X / sleeping / 60s doze). Eyes follow the travelling pointer,
  then the real mouse once it rests (`face_gaze_target`). The parked
  pointer waits `OVERLAY_PARK_GAP` 14 beside the face (`pointer_home`);
  the runner dwell estimator seeds from the same helper
  (`_OVERLAY_HEAD_PX` → `_OVERLAY_POINTER_PX`, `_overlay_note_head` →
  `_overlay_note_pointer`). Retired with the head: `OVERLAY_HEAD_PX`,
  `head_radii` + squish/hover-grow constants, `tip_alpha`,
  `teardrop_points`. Choreo/IPC verbs, the `Overlay` API, the face
  `face` verb semantics and headless latency untouched. Recorded, not
  changed: a direct target still never restamps the dock quiet clock
  (a settled pointer re-docks a frame after landing). Pins:
  squish/teardrop/tip-alpha tests retired; new squircle-sampler,
  tip-anchored arrow + pointer-face-parts (linear under press), face
  box + park geometry, happy bounce, gaze-target tests; the dock test
  now proves the face shift + park non-overlap; dwell pins updated in
  `test_computer_use.py`. Docs: CONFIGURATION + TESTING rows
  retargeted. Live-verified on this machine with a temp stdin harness +
  screenshots (parked pair / face working/happy/error / mid-flight
  trail + bubble / landing ring / typing glyphs / re-park), child
  stderr clean. `ruff` clean, 85 passed `tests/test_overlay.py`, full
  2044 passed / 11 skipped / 1 deselected. Clean-state checklist 5/5
  ticked. Committed as `3ea34d9`.
- F20 done (`passing`): overlay Bmax restyle — the desktop Tk face now
  wears the web skin's character. `agent/overlay.py`: the berry-blue
  palette is gone; `OVERLAY_SHELL_MID #0d142a` / `OVERLAY_HEAD_OUTLINE
  #4d5b83` / `OVERLAY_EYE_FILL #ff5fa6` are copied verbatim from the
  web `robot-head.js` DEFAULTS and pinned against it; derived shades
  renamed (`shell_shades()`; `OVERLAY_BERRY_*`/`OVERLAY_HEAD_FILL`
  deleted, `OVERLAY_GLOW_FILL` rides the LED pink, ring left = pink /
  right = slate trim, bubble = navy + pink label). Eye art: the eyes
  are LED blocks now (`create_rectangle`, not ovals) with a stippled
  bloom behind each block (web-style pink bloom), and the happy/
  sleeping arcs, error X, blink, sweep, and glance behaviour are
  untouched; the fat stippled halo became a two-layer neon rim (faint
  dithered band at 1.20× + crisp 2px pink edge ring at 1.06×); the
  type glyphs ride the LED pink (slate-on-grey was near-invisible).
  Two live-harness finds fixed in the same spirit: (1) the trail had no
  aging, so a settled head's trail collapsed onto its own face — a
  stray dot between the new pink eyes; the trail items now draw UNDER
  the head (behind it while gliding, hidden when settled); (2) the
  `OVERLAY_GLOW` kill switch was dead (the tick re-showed the glow
  after creation parked it) — the tick now respects it. Recorded, not
  changed: a direct target never restamps the dock quiet clock, so a
  settled (>5s-old) head re-docks a frame after landing (matches the
  documented "settles back to a screen-corner dock"); the clock counts
  from the child's start / last tour launch. Face semantics
  (`face_kind`/`face_geometry`, the `face` verb) and the runner driving
  loop are untouched. Pins: `test_shell_look_helpers` (renamed) + new
  `test_overlay_wears_the_web_bmax_skin` (overlay constants == the web
  DEFAULTS hexes read from `robot-head.js`, frozen trio, ring/glow
  derivations, no berry stragglers). Docs: TESTING §3 row for
  `test_overlay.py`, CONFIGURATION row names the skin. Live-verified on
  this machine with a temp stdin harness + screenshots (idle / working
  squint / searching sweep / happy / error X / sleeping arcs / click
  bubble + ring / type glyphs / mid-glide trail), child stderr clean.
  `ruff` clean, 82 passed `tests/test_overlay.py`, full 2041 passed /
  11 skipped / 1 deselected. Clean-state checklist 5/5 ticked.
  Committed as `55cc955` (pushed).
- F19 done (`passing`): web Bmax face — robot-heads 0.3.0 ported into
  the web UI as one readable vanilla asset (`/static/robot-head.js`,
  ~2,090 lines; MIT header keeps the upstream commit `58ebf64` +
  © Fayaz Ahmed) exposing `RobotHead.{mount, mountAll, states, shapes,
  flashMs, defaults}` + the controller lifecycle
  (`setState`/`flash`/`poke`/`setPaused`/`destroy`). The default skin IS
  the Bmax look: a `bmax` squircle preset (squarer corners, deeper
  body), navy `#0d142a` shell / `#4d5b83` trim / `#ff5fa6` LEDs, a new
  energy-driven glow wash over the bezel and a flatter front plate
  (`plateDome`); burst timings 1200/1800ms mirror the Tk overlay's
  constants, bursts expire to idle the same way, and
  `prefers-reduced-motion` parks the loop on the still pose.
  `chat.html`: posed 112px hero still on the empty state + a 30px head
  beside the composer status line, driven by the existing turn
  lifecycle (thinking → speaking → searching/working by tool name →
  listening on approval; happy/error bursts; orb states mapped through
  a new fail-silent `onState` hook in `voice-orb.js`; `turnActive`
  keeps the orb off the face while a turn runs). `phone.html`: the
  64px face replaces the flat dot (send + hop reactions, orb yields
  while send is disabled); the assistant bubble dot is pink `#ff5fa6`
  and the dead `.baymax-dot-lg` rule is gone. Three finds while wiring:
  a partial `{yaw,pitch,roll}` hero pose NaN'd the floor-shadow
  gradient (pose overrides now normalise against the rest pose);
  file:// masked JS errors, so verification ran on HTTP-served stub
  renders (Jinja render + headless Edge: both heads mount,
  `canvases=2 heads=2`, zero console errors; the phone page's layout
  numbers identical to the pre-change page; all nine states checked
  against the reference); and the burst revert stranded the head on its
  pre-burst state, so bursts now expire to idle. New:
  `tests/test_web_face.py` (9 static pins: provenance/API/states, skin
  defaults, overlay-timing + idle-expiry parity, reduced-motion
  parking, chat + phone wiring, orb hook, dot colour, scope confined
  to chat+phone). Docs: TESTING §3 row. `ruff` clean, 106 passed
  verification command / 187 passed target slice, full 2040 passed /
  11 skipped / 1 deselected. Clean-state checklist 5/5 ticked.
  Committed as `0d75650` (pushed).
- Item-8 done (`passing`): overlay lifecycle hygiene — test spawn guard +
  retry cooldown. **A1 (tests can never pop a head):** new autouse
  `overlay_spawn_guard` in `tests/conftest.py` forces
  `INVINCIBLE_OVERLAY=0` and replaces `Overlay.start` with a recorder
  that refuses to spawn (delegates to the real no-op off Windows); the
  recorded attempts are asserted empty at teardown, so a test that tries
  to spawn fails loudly. Measured before/after on this machine
  (image-aware count: one logical head = venv redirector + real
  interpreter): pre-guard `pytest tests/test_look_at_screen.py` spawned
  pair 14176→11836 and the 5-file slice spawned 17524→12908 +
  4940→11168 (all exited); post-guard the 6-file slice spawned ZERO over
  a 90s watch, and the full suite left the process table byte-identical
  (only the live agent's 12948→11120). A deliberately-spawning temp test
  failed at teardown with "test attempted to spawn a real overlay head"
  (self-check; file deleted). **A2 (lifecycle):** (1) cooldown defect
  fixed — `ensure_overlay` now stamps `_OVERLAY_RETRY_AT` BEFORE every
  spawn attempt (a child that dies right after a good spawn bypassed
  the 60s cooldown and respawned once per job) and `stop_overlay`
  clears the stamp so an intentional stop (WS→poll fallback) respawns
  immediately. (2) Orphan (head outliving a dead parent) NOT
  reproduced: a windowless probe with the exact Popen kwargs saw EOF at
  +1.9s and both processes exited; the real Tk pair spawned by pytest
  exited ~2s after the parent; a 5-minute watch after the 88-test slice
  saw nothing linger; a product-path `Overlay.start()` probe
  (`start: True`, no `stop()`) exited on EOF — the earlier "orphan ≥90s"
  reading was the live agent's own pair (next). No watchdog added. (3)
  "Two heads under one agent" = the venv `Scripts\python.exe`
  redirector: every spawn is shim + real interpreter (12948→11120;
  255,200 B shim vs 106,208 B base, distinct sha256) — not a bug, no
  fix. (4) In-process double-spawn race: impossible by construction
  (sync `ensure_overlay`, one event loop, sequential WS→poll fallback) —
  documented, no fix. (5) Two defensive hardenings for the
  parent-death path (pinned): a full inbox can no longer silently drop
  the EOF quit (makes room by dropping the oldest message) and
  `_destroy_window` suppresses destroy+quit so a teardown hiccup cannot
  kill the tick loop. New pins: 8 in `tests/test_overlay.py` (reader
  EOF quit, full-inbox quit, destroy-window never raises, job paths
  never attempt a spawn, guard refusal + cooldown stamp, live-handle
  reuse, cooldown survives fast child death, stop clears cooldown).
  Docs: TESTING §2 documents the guard; the CONFIGURATION row is
  unchanged (no env change). `ruff` clean, 169 passed 6-file slice,
  full 2031 passed / 11 skipped / 1 deselected. Clean-state checklist
  5/5 ticked. Committed as `41271df` (pushed).
- Item-7 done (`passing`): overlay face v1 — the head shows what the
  agent is doing. `agent/overlay.py`: pure single-slot `face_kind`
  (working/searching persist until replaced, happy 1.2s + error 1.8s
  bursts expire to idle, sleeping derived after 60s quiet, junk/unknown
  reads idle, never raises) + absolute-px `face_geometry` (idle IS the
  frozen eye constants incl. cursor tracking, working squint + lift,
  searching ±5px sine sweep, arc windows for happy, X strokes for
  error, flipped arcs for sleeping) + `face_blink` (0.16s sine, every
  4.4s, squashes ry only) + `face_apply` (wire → slot; bursts stamp
  their end on the child's own clock); the child draws it with 6 new Tk
  items (2 arcs + 4 X strokes, parked off-window when unused) stacked
  below trail/bubble/rings/glyphs; new `{"type":"face","state":...}`
  IPC verb with the stdin allowlist extracted to `_INBOUND_TYPES` (that
  was a silent-drop trap) + an `Overlay.face()` sender. `agent/runner.py`:
  `execute_job` is now a thin face wrapper over `_execute_job` (start →
  working, look_at_screen → searching, ok → happy, error/blocked/
  unavailable → error, raise/cancel → idle) + `handle_handoff` → happy;
  `_overlay_face` reads the live handle only (a face never spawns a
  head), `_result_face` claims happy only for a named non-failure
  status (junk results draw the error). No new env var, zero registry
  surface, headless latency unchanged, idle look byte-identical.
  New pins: 13 in `tests/test_overlay.py` (resolver matrix incl. junk /
  sleeping / burst expiry, idle-draw equality, shape bounds + head
  containment, blink period, wire slot, allowlist, sender, runner
  sequence/raise/cancel/headless/burst-replaces-working, handoff happy).
  Live-verified on this machine with a temp stdin harness + screenshots
  (idle, blink, working, sweeping, happy, error, sleeping; sweep
  measured 10px peak-to-peak vs 0–1px idle, still awake at 58s quiet,
  sleeping at 63s, child stderr clean). Docs: CONFIGURATION
  `INVINCIBLE_OVERLAY` row now names the face. `ruff` clean, 112 passed
  overlay/computer slice, full 2023 passed / 11 skipped / 1 deselected.
  Clean-state checklist 5/5 ticked. Uncommitted — commit on user's word.
- Item-4b done (`passing`): fly-click fixups (4 review findings + claim
  honesty). (1) Click effects anchor at the clicked point — pure
  `overlay.effect_anchor` (grid→pixel) + `state["bubble_xy"]`: `_draw`
  centers the ring on its spawn point and the bubble on the anchor
  while the head glides home (runner sends settle before effect, and
  the choreo idle-home had often launched the head home when a late
  click landed); land rings still spawn at the head and tour bubbles
  still follow it. (2) Bluey pointer warp — `_computer_pointer` /
  `_computer_warp` (GetCursorPos/SetCursorPos on the SendInput api,
  never-raise) wrapped around click, scroll, drag, and the
  type_text/press_keys focus click; bare type/press + open_app/url
  untouched; results unchanged. (3) `choreo_take_control` also clears
  `home_requested` so a real action never re-homes after landing.
  (4) Look tour points seed `_OVERLAY_HEAD_PX` via `_overlay_note_head`
  (suppressed) closing the Item-1c cross-path dwell gap; settle still
  resets to None. Decisions recorded: settle→effect order kept
  (cosmetic once anchored); Item-6 "muted" claim corrected (the
  `isMuted` gate is fail-silent but unwired; voice-off is
  server-gated) + SECURITY §14/PROVIDERS notes; dead
  cancelHold/data-hold-ms left (attribute pinned); stale last-commit
  line fixed. New pins: 1 in `tests/test_overlay.py` (effect_anchor
  pure) + 8 in `tests/test_computer_use.py` (save→act→warp order ×3,
  focus-click warp, bare-type no-touch, raising-warp ok, broken-api
  never-raise, byte round-trip), plus 2 extended (take_control flag,
  look head seed). `ruff` clean, 248 passed targeted slice, full
  2010 passed / 11 skipped / 1 deselected. Clean-state checklist 5/5
  ticked (template committed blank; record kept here).
- Item-1d done (`passing`): glide-cover follow-up — drag dwell now covers
  both legs (`core/tool_executor.py`: new pure `_resolve_drag_delay`
  estimates head→from then from→to, drag branch uses it so the single
  dwell is the two-leg sum; bare-xy drag alias fix in `_computer_resolve`
  accepting x1/y1/x2/y2 — the old code raised KeyError 'x' on real
  `{"x1"..}` jobs, now returns ok with x/y echo). Parent head tracking
  resets on settle/stop/handoff via `runner._overlay_note_settled()` so
  the next estimate dock-seeds instead of drifting. `tip_alpha`
  keeps its frozen signature with ignored to_target/settled documented.
  No registry/goldens/loop/realm/db change; headless zero latency
  unchanged. New pins: 3 in `tests/test_computer_use.py`
  (drag-delay both-legs, head reset, drag-act estimates both legs).
  `ruff` clean, 90 passed overlay/computer slice, 239 passed
  overlay/computer/look/voice/webchat-agent/webchat/handoff slice,
  full 2001 passed / 11 skipped / 1 deselected.
- Item-4 done (`passing`): fly-click feedback (draw only) — Bluey
  `CursorOverlay` softLand/clickEffect/typedEffect/bubble ported into
  `invincible/agent/overlay.py` as pure hermetic helpers (`ring_frame`
  expanding 1.0→1.25x + thinning 3→1 outline, `glyph_frame` 46px
  ease-out rise over 0.9s, `effect_glyph_chars` first-6 non-space) +
  child `_draw` ring oval + 6-text glyph pool; landings stamp press
  0.05 + 30px/0.7s ring, click effects press 0.14 + 38px/0.65s ring
  (right-click deep tint) + 1.4s `⌖ x,y` bubble, type effects press
  0.035 + staggered glyphs (replaces live set, cap 6). Parent
  `Overlay.effect()` sender; `runner` fires it on click/type ok only
  (results/approvals/F13/F14 untouched, errors skip, dead overlay
  swallows). No registry/goldens/docs change (bubble is
  overlay-internal, no protocol wording changed). New pins: 9 in
  `tests/test_overlay.py` (ring/glyph math, glyph cap, sender
  wire+tint+junk/dead, runner click/type/error/flaky) + 3 effect-input
  contracts in `tests/test_computer_use.py`. `ruff` clean, 124 passed
  overlay/computer/webchat-agent slice, full 1998 passed / 11 skipped /
  1 deselected.
- Item-2 done (`passing`): point queue (overlay choreography, visual
  only) — Bluey `RealtimeHost` choreograph ported into
  `invincible/agent/overlay.py` as pure hermetic helpers
  (`choreo_state/point/request_home/set_speaking/take_control/go_home/step`):
  queued tour points hold 1.0s flight + 2.2s minimum FIFO, head homes
  1.2s after a request once quiet else 5.0s quiet, speaking pauses
  homing (quiet stamps on stop), real-action `announce` takeControls
  the queue, `settle` docks + clears. Child `_run_window` drives the
  step every 1/30s tick (same flight/dock code factored into
  `_launch`/`_dock`; the old fixed 2s idle-settle is superseded,
  `OVERLAY_IDLE_SETTLE_SECONDS` removed). Parent `Overlay` gains
  best-effort `point/request_home/set_speaking` senders; `runner`
  queues a tour point at the look result's `near` control (suppressed,
  never blocks tools; computer path + headless `0.0` dwell
  byte-identical). No registry/goldens/loop/realm/db change; no docs
  (no user-visible text changed). New pins: 12 in
  `tests/test_overlay.py` (order/hold/home/request/speaking/clear/
  labels/junk/serializers/runner look×3) + 1 loop-verbatim pin in
  `tests/test_webchat_agent.py`. `ruff` clean, 112 passed
  webchat/overlay/computer slice, full 1986 passed / 11 skipped /
  1 deselected.
- Item-3 done (`passing`): better "this" — `look_at_screen` results now
  carry mouse grounding as additive-only keys (`core/tool_executor.py`:
  pure `look_near` pixel-space smallest-area-contain (Bluey −6px
  inset) else <60px nearest else null, `look_mouse_hint` single-line
  `@x,y[, on C# "label"]` + this/that/here sentence, `_look_assemble`
  wiring with `near{id,kind,label}` + `mouse_hint` + `truncated` bool;
  C order/ids/grid math byte-identical, missing cursor omits hint with
  near null, labels whitespace-collapsed, hint excerpt capped at 60).
  New pins in `tests/test_look_at_screen.py` (8 tests: near
  smallest/tie/inset/nearest/junk, hint sanitize, assemble + missing
  + 150%-style negative-origin parity cross-pinned to
  `overlay.pixel_to_grid`, shape-drift tripwire); goldens/registry
  untouched and green (results ≠ registry, no regen diff). Docs:
  `MCP_PROTOCOL.md` look payload + `SECURITY.md` §12 grounding-aids
  note. `ruff` clean, 15 passed look slice, 65 passed
  golden/registry/webchat-agent slice, full 1973 passed / 11 skipped /
  1 deselected.
- Item-6 done (`passing`): hold-to-ask + chirp — 300ms primary-pointer
  hold on `#voice-orb`/`#phone-orb` adds `is-asking` (border mirrors
  `is-listening`, no new color), sets `askedNext`, gesture-unlocks a
  shared AudioContext, re-arms tuck via `isRecording()`, and submits
  with `asked:true`; `suppressClick` swallows the synthetic tap so hold
  asks once. Taps keep B4/B8 behavior but now send `asked:false`.
  `playChirp(words)` pentatonic scoop/wobble (`min(max(words,3),5)`,
  0.07–0.09s notes, 0.13s last, 0.5x octave, 0.028s gaps, suspended
  skips, fail-silent, device-local); mic replies chirp with reply
  words; typed turns stay silent and voice-off is server-gated (the
  `isMuted` gate is kept fail-silent but unwired — no template passes
  it; claim corrected in Item-4b). Ids, realms, textContent-only, and
  the Item-5 tuck guard frozen. New pin
  `test_hold_to_ask_and_chirp_item6` + updated chirp/border pins.
  `ruff` clean, 97 passed voice/webchat/handoff slice, full
  1965 passed / 11 skipped / 1 deselected.
- Item-5 done (`passing`): quiet UI — PWA credential picks wrapped in
  `#phone-panel[data-autotuck="6"]` (real `.phone-wrap` + `phone-key` /
  `phone-model` nodes, order-pinned) collapsing after 6s idle via
  `clearTimeout`/`scheduleTuck` on `pointerdown`/`input`; mic-active
  guard (`recorder || recog`, `is-listening`/`is-speaking`) re-arms
  instead of tucking, orb tap untucks with `aria-expanded` + `pagehide`
  cleanup. `#mode-border` fixed screen-corner solid edge reusing the
  exact orb `.is-*` names (blue listening 10px, purple working 8px with
  opacity-only `mode-pulse`, pink speaking 8px; `pointer-events:none`,
  55px radius + `env(safe-area-inset-*)`, no glow, reduced-motion off).
  Shared `voice-orb.js` null-guards panel/border (`opts.panel/border`)
  so desktop `chat.html` is untouched; ids, realms, `asked` flag, and
  textContent-only inserts frozen; no registry/golden/wire change.
  New pins: 2 static tests in `tests/test_voice.py` (panel + border).
  `ruff` clean, 96 passed voice/webchat/handoff slice, full
  1964 passed / 11 skipped / 1 deselected.
- Item-1c done (`passing`): glide-cover dwell — the 0.35s fixed dwell was
  shorter than every Bluey flight (0.45s min, ~0.9s typical), so settle
  aborted each glide mid-flight and the head never landed. Fix:
  `tool_executor._computer_act` takes optional `delay_for(point)` and
  each point-bearing branch dwells `max(base, estimate)` via pure
  `_resolve_delay` (float-only callers unchanged); `runner` supplies
  `_overlay_delay_fn` estimating each leg with the same `plan_flight`
  (drag sums both legs, capped 1.3/2.0s + 0.1s buffer, dock-seeded head
  tracking, base fallback). Headless still passes `0.0`/`None`.
  New pins: `_resolve_delay` matrix + estimator cover/drag-sum/fallback
  (3 tests). `ruff` clean, 63 passed overlay/computer slice, full
  1962 passed / 11 skipped / 1 deselected.
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
- Last commit: `55cc955` (main) — F20 overlay Bmax restyle (navy/pink
  skin parity); the hash-record chore sits on top.
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

- None - F26 complete (`passing`). Zero `active`. Next: F22 (face
  voice states; needs the server->agent signal) at the next clock-in;
  `open_url` launch wait is the small symmetric follow-up if wanted.

## Next Steps

1. F22 (face voice states, `planned`) needs the server->agent voice
   signal; text stays source of truth and silent when voice is off.
2. Running `invincible connect` agents hold pre-F23/pre-F25/pre-F26
   code - restart to get `look_at_screen`'s window/wait args,
   `open_app`'s launch wait, and the work-area dock + opt-in DPI.
   F24 guidance is server-side: a new dashboard chat turn picks it up
   with no agent restart.
3. `open_url` has no wait: a launch wait there is the symmetric
   follow-up (reuse `_open_wait_result` + the look selector), if the
   repeat-app plan calls for it.
4. Tests must never spawn a real overlay head (autouse conftest
   guard); a test needing a spawn stubs `Overlay.start` and clears
   `overlay_spawn_guard`.
5. F26 live verification remains: click-through and the
   multi-monitor glide-vs-click check on a real paired Windows
   desktop. `.vscode/` is local-only and must stay untracked.
6. F04 fan-out (`harness_fanout.py` + 18 tests) stays green,
   untouched.

## Blockers

- None.
