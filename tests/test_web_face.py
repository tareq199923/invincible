# tests/test_web_face.py
"""F19 web Bmax face: the robot-heads port as a static asset + page wiring.

Hermetic static pins only — the face itself is canvas JS, so these lock
the provenance (MIT), the public API shape, the Bmax skin defaults, the
page mounts (data-head), and the lifecycle wiring (turn events -> face
states, the orb onState hook). Pixel behaviour was verified by hand
against the reference render; see PROGRESS.md.
"""
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
TEMPLATES = REPO / "invincible" / "templates"
STATIC = TEMPLATES / "static"

ASSET = STATIC / "robot-head.js"
ORB_JS = STATIC / "voice-orb.js"
CSS = STATIC / "app.css"
CHAT = TEMPLATES / "chat.html"
PHONE = TEMPLATES / "phone.html"

REL = "robot-head.js"


def _read(path):
    return path.read_text(encoding="utf-8")


def test_robot_head_asset_is_the_mit_port():
    """One readable vanilla file, MIT provenance kept, no build step."""
    js = _read(ASSET)
    # Provenance: upstream name, version, commit, copyright, licence.
    assert "robot-heads 0.3.0 (commit 58ebf64)" in js
    assert "MIT License. Copyright (c) 2026 Fayaz Ahmed." in js
    assert "Permission is hereby granted" in js
    # Readable single-file port: no module syntax, no leftovers.
    assert '"use strict";' in js
    assert "\nimport " not in js and "\nexport " not in js
    assert "__APPEND__" not in js
    # The public API the pages consume.
    assert "window.RobotHead = {" in js
    for member in ("mount: mount,", "mountAll: mountAll,", "states: STATES,",
                   "shapes: SHAPE_NAMES,", "flashMs: FLASH_MS,",
                   "defaults: DEFAULTS"):
        assert member in js
    # All nine upstream states, in the upstream order.
    assert ('var STATES = ["idle", "thinking", "searching", "listening", '
            '"speaking", "working", "happy", "error", "sleeping"];') in js
    # mount returns a controller with the full lifecycle.
    for member in ("setState: setState,", "flash: flash,", "poke: poke,",
                   "setPaused: setPaused,", "destroy: destroy"):
        assert member in js


def test_bmax_skin_is_the_default_look():
    """Navy shell, pink LEDs, the deeper squircle, glow on."""
    js = _read(ASSET)
    assert 'shape: "bmax",' in js
    assert 'color: "#0d142a",' in js
    assert 'trimColor: "#4d5b83",' in js
    assert 'screenColor: "#ff5fa6",' in js
    assert "glow: true," in js
    # The bmax preset is its own shape: squarer corners than 'square',
    # a deeper body, and the LED face filling more of the screen.
    assert ('bmax: function () { return makeShape("bmax", '
            '[[-0.27, -0.27], [0.27, -0.27], [0.27, 0.27], '
            '[-0.27, 0.27]], 0.18, 0.72, CORNERS, BACK_CORNERS, 0.85); }'
            ) in js
    assert '"bmax"' in js.split("var SHAPE_NAMES = [")[1].split("]")[0]
    # The glow pass and the flatter front face are the skin's own knobs.
    assert "opts.glow && this.energy > 0.004" in js
    assert "opts.plateDome === undefined ? 0.17 : opts.plateDome" in js
    # A partial pose override is normalised against the rest pose.
    assert ("poseOverride = Object.assign(restPose(stateKey), poseOverride)"
            ) in js


def test_flash_timings_mirror_the_desktop_overlay():
    """happy/error bursts hold exactly as long as the Tk overlay's."""
    from invincible.agent.overlay import (
        OVERLAY_FACE_ERROR_SECONDS,
        OVERLAY_FACE_HAPPY_SECONDS,
    )

    js = _read(ASSET)
    assert "var FLASH_MS = { happy: 1200, error: 1800 };" in js
    assert int(OVERLAY_FACE_HAPPY_SECONDS * 1000) == 1200
    assert int(OVERLAY_FACE_ERROR_SECONDS * 1000) == 1800
    # And like the overlay's, a burst expires back to idle — not to the
    # state it interrupted (a turn end would otherwise strand the head on
    # "speaking"/"working" forever). A setState mid-burst cancels it.
    flash_fn = js.split("function flash(name, ms) {")[1].split("function poke()")[0]
    assert 'setState("idle");' in flash_fn
    assert "setState(back);" not in flash_fn
    assert "clearTimeout(flashTimer);" in flash_fn


def test_reduced_motion_parks_the_head_still():
    """prefers-reduced-motion keeps the loop off; the still pose paints
    instead and a media-query change resyncs."""
    js = _read(ASSET)
    assert 'matchMedia("(prefers-reduced-motion: reduce)")' in js
    assert "var want = !(still() || reducedMotionNow());" in js
    assert 'mql.addEventListener("change", onMql)' in js
    assert 'mql.removeEventListener("change", onMql)' in js  # destroy cleans up


def test_chat_page_mounts_and_drives_the_face():
    chat = _read(CHAT)
    # The asset rides the same cache-busted static pattern as everything
    # else (no inline script, no build step).
    assert ("<script src=\"/static/robot-head.js?v={{ asset_rev("
            "'robot-head.js') }}\" defer></script>") in chat
    # Empty-chat hero: the posed still (~112px, decorative).
    assert 'class="chat-face" id="chat-face" aria-hidden="true"' in chat
    assert '"size":112' in chat and '"pose":{"yaw":0.5' in chat
    # Composer: a small head beside the status line, no shadow/antenna.
    assert 'class="chat-face-sm" id="chat-face-status"' in chat
    assert '"size":30,"antenna":false,"floorShadow":false' in chat
    # Mounted from the data-head JSON on load (the asset is deferred).
    assert 'heads = window.RobotHead.mountAll("[data-head]");' in chat
    # One lifecycle: turn events drive the head, turnActive keeps the
    # orb's states off it while a turn is in flight.
    assert "var turnActive = false;" in chat
    assert "turnActive = true;\n      faceSet(\"thinking\");" in chat
    assert 'faceSet("speaking");' in chat
    assert 'faceSet(sensing ? "searching" : "working");' in chat
    assert 'toolName.indexOf("look_at_screen")' in chat
    assert 'toolName.indexOf("web_research")' in chat
    assert 'faceSet("listening");' in chat
    assert 'faceTurnEnd("happy");' in chat
    assert chat.count('faceTurnEnd("error");') == 3  # stream, JSON, fetch
    assert "if (turnActive) faceTurnEnd(null);" in chat  # silent stream end
    # The orb mirrors onto the head through the new onState hook.
    assert "onState: function (state) {" in chat
    assert "if (turnActive) return;" in chat
    assert 'faceSet(ORB_FACES[state] || "idle");' in chat


def test_phone_page_wears_the_face_instead_of_the_flat_dot():
    phone = _read(PHONE)
    assert REL + "?v={{ asset_rev('" + REL + "') }}" in phone
    # The face replaces the dot in the phone header (dot stays on
    # assistant bubbles).
    assert 'class="phone-face-head" id="phone-face-head"' in phone
    assert '"size":64,"antenna":false,"interactive":true' in phone
    assert 'class="baymax-dot baymax-dot-lg"' not in phone
    assert 'heads = window.RobotHead.mountAll("[data-head]");' in phone
    # Lifecycle: thinking -> happy/error bursts, hop works the face.
    assert 'faceSet("thinking");' in phone
    assert phone.count('faceFlash("happy");') == 2  # answer + hop queued
    assert phone.count('faceFlash("error");') == 4  # send + 3 hop paths
    assert 'faceSet("working");' in phone
    # The orb's states yield to an in-flight send.
    assert "if (sendBtn.disabled) return;" in phone
    assert 'faceSet(ORB_FACES[state] || "idle");' in phone


def test_voice_orb_exposes_an_on_state_hook():
    orb = _read(ORB_JS)
    assert "onState(state) \u2014 optional: mirrors the orb's class state" in orb
    # Fired inside setOrbState, next to the border mirror, fail-silent.
    assert 'opts.onState(state || "")' in orb
    assert "/* face keeps its state */" in orb
    state_fn = orb.split("function setOrbState(state) {")[1].split("if (border)")[0]
    assert "setState(orb, state);" in state_fn
    assert "opts.onState" in state_fn


def test_bubble_dot_wears_the_face_pink():
    css = _read(CSS)
    dot = css.split(".baymax-dot {")[1].split("}")[0]
    assert "#ff5fa6" in dot
    # The dot's old soft-white fill is gone.
    assert "#fff" not in dot
    # Mount hosts get their own layout seams.
    assert ".chat-face { display: inline-block;" in css
    assert ".chat-face-sm { display: inline-flex; flex: none; }" in css
    assert ".phone-face-head { display: inline-flex; flex: none; }" in css
    # The old phone-only dot size is dead CSS now.
    assert ".baymax-dot-lg" not in css


def test_face_stays_scoped_to_chat_and_phone():
    """v1 surfaces only; the other pages are a later feature."""
    for template in TEMPLATES.glob("*.html"):
        if template.name in ("chat.html", "phone.html"):
            continue
        assert "robot-head.js" not in _read(template), template.name
