// B4 voice orb: shared recorder + speaker for chat.html and phone.html.
// B7 free fallback: when the server has no audio surface (no OpenAI /
// no Groq credential), the orb falls back to device-local browser voice
// (Web Speech recognition + speechSynthesis). No fetch, no keys, audio
// never leaves the device. One circle, three visible states, tap to
// interrupt. Text stays the source of truth; every label is
// textContent-only (same XSS posture as the chat bubbles). The page owns
// auth (cookies vs inv_ key) and passes transcribe/synthesize fetchers
// in; this module owns mic level, playback, and orb classes only.
(function () {
  "use strict";

  function setState(orb, state) {
    orb.classList.remove("is-listening", "is-working", "is-speaking",
      "is-asking");
    if (state) orb.classList.add(state);
    orb.style.setProperty("--level", "0");
  }

  function setStatus(statusEl, text) {
    if (statusEl) statusEl.textContent = text || "";
  }

  function hasBrowserSTT() {
    return !!(window.SpeechRecognition || window.webkitSpeechRecognition);
  }

  function hasBrowserTTS() {
    return !!(window.speechSynthesis && window.SpeechSynthesisUtterance);
  }

  // Server said "no audio provider" (not "voice off"): eligible for the
  // free device-local fallback. Voice-off / mute intent is respected —
  // the fallback never overrides an operator kill-switch refusal.
  function isCapabilityError(err) {
    var msg = (err && err.message) || "";
    return /no transcription surface|no connected provider|connect openai for mic|no speech surface/i.test(msg);
  }

  // Drives --level on the orb from mic RMS while recording.
  function attachLevel(stream, orb) {
    var ctx = null;
    var raf = 0;
    var analyser = null;
    var data = null;
    try {
      var AC = window.AudioContext || window.webkitAudioContext;
      if (!AC) return function () {};
      ctx = new AC();
      var src = ctx.createMediaStreamSource(stream);
      analyser = ctx.createAnalyser();
      analyser.fftSize = 512;
      data = new Uint8Array(analyser.frequencyBinCount);
      src.connect(analyser);
      var tick = function () {
        analyser.getByteTimeDomainData(data);
        var sum = 0;
        for (var i = 0; i < data.length; i++) {
          var v = (data[i] - 128) / 128;
          sum += v * v;
        }
        var level = Math.min(1, Math.sqrt(sum / data.length) * 4);
        orb.style.setProperty("--level", level.toFixed(3));
        raf = requestAnimationFrame(tick);
      };
      raf = requestAnimationFrame(tick);
    } catch (e) {
      return function () {};
    }
    return function () {
      try { cancelAnimationFrame(raf); } catch (e) { /* ignore */ }
      try { if (ctx) ctx.close(); } catch (e) { /* ignore */ }
    };
  }

  // Creates an orb controller. Callers:
  //   transcribe(blob) -> Promise<string> (throw on failure)
  //   synthesize(text) -> Promise<Blob|null> (null = silent fallback)
  //   onText(text) — transcript ready, send like typed input
  function createVoiceOrb(opts) {
    var orb = opts.orb;
    var statusEl = opts.status || null;
    var idleLabel = opts.idleLabel || "";
    var recorder = null;
    var chunks = [];
    var stream = null;
    var stopLevel = null;
    var audio = null;
    var recog = null;
    // Item-5 quiet UI: optional autotuck panel + screen-corner mode
    // border (PWA phone.html passes both; chat.html passes neither and
    // every helper below null-guards). Ids, realms, and textContent-only
    // inserts are untouched.
    var panel = opts.panel || null;
    var border = opts.border || null;
    var tuckTimer = null;
    // Item-6 hold-to-ask + chirp (Bluey feel, text stays truth).
    // 300ms press-and-hold marks this turn as asked; a short tap keeps
    // the B4/B8 behavior. No haptics dependency; reduced-motion uses
    // the same static listening style.
    var HOLD_MS = 300;
    var holdTimer = null;
    var holdPointerId = null;
    var holding = false;
    var askedNext = false;
    var suppressClick = false;
    var sharedCtx = null;

    function isMuted() {
      try {
        if (opts.muted) return true;
        if (typeof opts.isMuted === "function") return !!opts.isMuted();
      } catch (e) { /* ignore */ }
      return false;
    }

    function ensureSharedAudio() {
      try {
        var AC = window.AudioContext || window.webkitAudioContext;
        if (!AC) return null;
        if (!sharedCtx) sharedCtx = new AC();
        try {
          if (sharedCtx.state === "suspended") sharedCtx.resume();
        } catch (e) { /* backstop below */ }
        return sharedCtx;
      } catch (e) {
        return null;
      }
    }

    function chirpSyllables(words) {
      var n = 0;
      if (typeof words === "number") {
        n = words;
      } else if (typeof words === "string") {
        var t = words.trim();
        n = t ? t.split(/\s+/).length : 0;
      }
      return Math.min(Math.max(n || 0, 3), 5);
    }

    // Orb state plus its mirror on the screen-corner border. The orb
    // takes is-asking while held; the border mirrors it as is-listening
    // (no new border color — blue listening edge in CSS).
    function setOrbState(state) {
      var borderState = state === "is-asking" ? "is-listening" : state;
      setState(orb, state);
      if (border) {
        border.classList.remove("is-listening", "is-working",
          "is-speaking");
        if (borderState) border.classList.add(borderState);
      }
    }

    // Mic active (recording, browser-listening, speaking back, or held
    // asking) pauses the tuck timer: the panel never tucks mid-record.
    function isRecording() {
      return !!(recorder || recog || holding) ||
        orb.classList.contains("is-listening") ||
        orb.classList.contains("is-asking") ||
        orb.classList.contains("is-speaking");
    }

    function clearTuck() {
      if (tuckTimer) {
        try { clearTimeout(tuckTimer); } catch (e) { /* ignore */ }
        tuckTimer = null;
      }
    }

    function setTucked(tucked) {
      if (!panel) return;
      panel.classList.toggle("is-tucked", tucked);
      orb.setAttribute("aria-expanded", tucked ? "false" : "true");
    }

    function scheduleTuck() {
      clearTuck();
      if (!panel || panel.classList.contains("is-tucked")) return;
      var secs = 6;
      try {
        var n = parseInt(panel.getAttribute("data-autotuck"), 10);
        if (n > 0) secs = n;
      } catch (e) { /* default 6s */ }
      tuckTimer = setTimeout(function () {
        tuckTimer = null;
        if (isRecording()) { scheduleTuck(); return; }
        setTucked(true);
      }, secs * 1000);
    }

    function untuck() {
      if (!panel) return;
      clearTuck();
      setTucked(false);
      scheduleTuck();
    }

    if (panel) {
      // Any pointerdown/input in the panel resets the quiet clock.
      ["pointerdown", "input"].forEach(function (ev) {
        panel.addEventListener(ev, function () {
          if (panel.classList.contains("is-tucked")) return;
          scheduleTuck();
        });
      });
      try {
        window.addEventListener("pagehide", clearTuck);
      } catch (e) { /* ignore */ }
      scheduleTuck();
    }

    function stopTracks() {
      if (stream) {
        try {
          stream.getTracks().forEach(function (t) { t.stop(); });
        } catch (e) { /* ignore */ }
        stream = null;
      }
      if (stopLevel) { try { stopLevel(); } catch (e) { /* ignore */ } stopLevel = null; }
    }

    function stopAudio() {
      if (audio) {
        try { audio.pause(); } catch (e) { /* ignore */ }
        audio = null;
      }
      try {
        if (window.speechSynthesis) window.speechSynthesis.cancel();
      } catch (e) { /* ignore */ }
    }

    function stopRecog() {
      if (recog) {
        try { recog.stop(); } catch (e) { /* ignore */ }
        recog = null;
      }
    }

    function toIdle(msg) {
      recorder = null;
      chunks = [];
      holding = false;
      clearHoldTimer();
      stopTracks();
      stopAudio();
      stopRecog();
      setOrbState(null);
      orb.setAttribute("aria-label", "Talk");
      setStatus(statusEl, msg || idleLabel);
      scheduleTuck();
    }

    function clearHoldTimer() {
      if (holdTimer) {
        try { clearTimeout(holdTimer); } catch (e) { /* ignore */ }
        holdTimer = null;
      }
      holdPointerId = null;
    }

    function friendlyTranscribeError(err) {
      var msg = (err && err.message) || "";
      if (/no transcription surface|connect openai for mic/i.test(msg)) {
        return hasBrowserSTT()
          ? "Free mode: no audio provider — trying browser voice…"
          : "Voice needs an OpenAI or Groq provider — connect one, text still works.";
      }
      if (/voice is disabled/i.test(msg)) return "Voice is off — text still works.";
      if (/no connected provider/i.test(msg)) {
        return hasBrowserSTT()
          ? "Free mode: no provider — trying browser voice…"
          : "Connect a provider first.";
      }
      return msg ? msg + " — type instead." : "Transcription failed — type instead.";
    }

    // Device-local recognition: no fetch, no key, audio stays on device.
    function browserListen() {
      var SR = window.SpeechRecognition || window.webkitSpeechRecognition;
      if (!SR) {
        toIdle("Browser voice not supported here — type instead.");
        return;
      }
      try {
        recog = new SR();
      } catch (e) {
        toIdle("Browser voice unavailable — type instead.");
        return;
      }
      recog.lang = "en-US";
      recog.interimResults = false;
      recog.maxAlternatives = 1;
      setOrbState("is-listening");
      orb.setAttribute("aria-label", "Stop and send");
      setStatus(statusEl, "Free mode listening… tap to stop.");
      recog.onresult = function (ev) {
        var transcript = "";
        try {
          for (var i = 0; i < ev.results.length; i++) {
            transcript += ev.results[i][0].transcript;
          }
        } catch (e) { transcript = ""; }
        transcript = (transcript || "").trim();
        recog = null;
        if (transcript) {
          var wasAsked = askedNext;
          askedNext = false;
          holding = false;
          toIdle("");
          opts.onText(transcript, wasAsked);
        } else {
          askedNext = false;
          toIdle("Heard nothing — try again.");
        }
      };
      recog.onend = function () {
        if (recog) {
          recog = null;
          toIdle("");
        }
      };
      recog.onerror = function () {
        recog = null;
        toIdle("Browser mic error — type instead.");
      };
      try {
        recog.start();
      } catch (e) {
        recog = null;
        toIdle("Browser mic error — type instead.");
      }
    }

    // Device-local speech: no fetch, no key.
    function browserSpeak(text) {
      try {
        if (!hasBrowserTTS()) return false;
        window.speechSynthesis.cancel();
        var utter = new SpeechSynthesisUtterance(text.slice(0, 4000));
        utter.onend = function () { toIdle(""); };
        utter.onerror = function () { toIdle(""); };
        setOrbState("is-speaking");
        orb.setAttribute("aria-label", "Stop speaking");
        setStatus(statusEl, "Free mode speaking… tap to stop.");
        window.speechSynthesis.speak(utter);
        return true;
      } catch (e) {
        return false;
      }
    }

    async function upload(blob) {
      var uploadAsked = askedNext;
      setOrbState("is-working");
      setStatus(statusEl, "Transcribing…");
      try {
        var text = await opts.transcribe(blob);
        text = (text || "").trim();
        if (!text) {
          askedNext = false;
          holding = false;
          toIdle("Heard nothing — try again.");
          return;
        }
        askedNext = false;
        holding = false;
        toIdle("");
        opts.onText(text, uploadAsked);
      } catch (err) {
        if (isCapabilityError(err) && hasBrowserSTT()) {
          setStatus(statusEl, friendlyTranscribeError(err));
          browserListen();
          return;
        }
        askedNext = false;
        holding = false;
        toIdle(friendlyTranscribeError(err));
      }
    }

    async function start() {
      var MR = window.MediaRecorder;
      if (!MR || !navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
        // No recorder at all: free browser voice is the only option.
        browserListen();
        return;
      }
      try {
        stream = await navigator.mediaDevices.getUserMedia({ audio: true });
      } catch (e) {
        browserListen();
        return;
      }
      chunks = [];
      try {
        recorder = new MR(stream);
      } catch (e) {
        stopTracks();
        browserListen();
        return;
      }
      recorder.ondataavailable = function (ev) {
        if (ev.data && ev.data.size) chunks.push(ev.data);
      };
      recorder.onstop = function () {
        var rec = recorder;
        recorder = null;
        if (stopLevel) { try { stopLevel(); } catch (e) { /* ignore */ } stopLevel = null; }
        var blob = new Blob(chunks, { type: (rec && rec.mimeType) || "audio/webm" });
        chunks = [];
        stopTracks();
        if (blob.size) upload(blob);
        else toIdle("");
      };
      stopLevel = attachLevel(stream, orb);
      recorder.start();
      if (holding || askedNext) {
        setOrbState("is-asking");
        orb.setAttribute("aria-label", "Release to ask");
        setStatus(statusEl, "Asking… release to send.");
      } else {
        setOrbState("is-listening");
        orb.setAttribute("aria-label", "Stop and send");
        setStatus(statusEl, "Listening… tap to send.");
      }
    }

    function cancelHold() {
      clearHoldTimer();
      if (holding) {
        holding = false;
        askedNext = false;
        if (!recorder && !recog) setOrbState(null);
      }
    }

    // Item-6 hold-to-ask: 300ms press-and-hold marks the turn asked.
    // Pointerdown unlocks/resumes the shared AudioContext (gesture) so
    // the later async chirp is allowed; fail-silent stays the backstop.
    // Primary pointer only; multi-touch and extra mouse buttons ignored.
    orb.addEventListener("pointerdown", function (ev) {
      try {
        if (ev && ev.isPrimary === false) return;
        if (ev && ev.buttons !== undefined && ev.buttons > 1) return;
        if (holdPointerId !== null) return;
        try {
          holdPointerId = ev && ev.pointerId !== undefined
            ? ev.pointerId : 0;
        } catch (e) { holdPointerId = 0; }
      } catch (e) { return; }
      ensureSharedAudio();
      scheduleTuck();
      clearHoldTimer();
      holdTimer = setTimeout(function () {
        holdTimer = null;
        holding = true;
        askedNext = true;
        suppressClick = true;
        setOrbState("is-asking");
        orb.setAttribute("aria-label", "Release to ask");
        setStatus(statusEl, "Asking… release to send.");
        scheduleTuck();
        if (!recorder && !recog &&
            !orb.classList.contains("is-working") &&
            !orb.classList.contains("is-speaking")) {
          start();
        } else if (recorder || recog) {
          setOrbState("is-asking");
        }
      }, HOLD_MS);
    });

    ["pointerup", "pointerleave", "pointercancel"].forEach(function (ev) {
      orb.addEventListener(ev, function (e) {
        try {
          if (e && e.pointerId !== undefined && holdPointerId !== null &&
              e.pointerId !== holdPointerId &&
              ev !== "pointerleave") return;
        } catch (err) { /* ignore */ }
        var wasHolding = holding;
        clearHoldTimer();
        if (wasHolding) {
          holding = false;
          // Hold-submit: stop recording / recognition; the transcript
          // resolves via upload/browserListen with asked:true. The
          // following click is suppressed (see suppressClick).
          if (recog) {
            try { recog.stop(); } catch (err2) { toIdle(""); }
          } else if (recorder) {
            try { recorder.stop(); } catch (err3) { toIdle(""); }
          } else if (wasHolding && askedNext) {
            setOrbState(null);
          }
          try {
            if (ev === "pointerleave" || ev === "pointercancel") {
              suppressClick = false;
            }
          } catch (err4) { /* ignore */ }
        } else {
          askedNext = false;
        }
      });
    });

    orb.addEventListener("click", function () {
      // Fix 1: hold-submit + tap-click double-fire. A hold sets
      // suppressClick; swallow the synthetic click that follows
      // pointerup so hold asks exactly once.
      if (suppressClick) {
        suppressClick = false;
        return;
      }
      // Item-5: tapping the orb reopens a tucked panel (and restarts the
      // quiet clock); the tap then behaves exactly as before.
      if (panel) {
        if (panel.classList.contains("is-tucked")) untuck();
        else scheduleTuck();
      }
      // Tap while speaking interrupts the reply (Grok/GPT feel) —
      // works for server audio and free browser speech alike.
      if (orb.classList.contains("is-speaking")) {
        toIdle("");
        return;
      }
      if (recog) {
        try { recog.stop(); } catch (e) { toIdle(""); }
        return;
      }
      if (recorder) {
        try { recorder.stop(); } catch (e) { toIdle(""); }
        return;
      }
      if (orb.classList.contains("is-working")) return;
      start();
    });

    // Bluey-style chirp: pentatonic scoop/wobble blip when a mic reply
    // starts. Syllables from the reply word count
    // (min(max(words,3),5)). Fail-silent, no fetch, no keys. Only called
    // for orb-initiated (mic) turns; typed turns stay fully silent (B8).
    // Mute kills the chirp. Reuses the gesture-unlocked shared context
    // (AudioContext created on pointerdown); a suspended context skips.
    function playChirp(words) {
      try {
        if (isMuted()) return;
        var ctx = sharedCtx || ensureSharedAudio();
        if (!ctx) {
          try {
            var AC0 = window.AudioContext || window.webkitAudioContext;
            if (!AC0) return;
            ctx = new AC0();
            sharedCtx = ctx;
          } catch (e) { return; }
        }
        try {
          if (ctx.state === "suspended") return;
        } catch (e) { /* play anyway, backstop below */ }
        var syllables = chirpSyllables(words);
        // C major pentatonic two octaves up (C6…E7); 0.5x octave drop
        // keeps it round, never piercing. Notes 0.07–0.09s, last 0.13s.
        var scale = [1046.5, 1174.7, 1318.5, 1568.0,
          1760.0, 2093.0, 2349.3, 2637.0];
        var idx = 1 + Math.floor(Math.random() * 3);
        var t0 = ctx.currentTime + 0.01;
        for (var i = 0; i < syllables; i++) {
          if (i > 0) {
            var step = [-1, 1, 1, 2][Math.floor(Math.random() * 4)];
            idx = Math.min(Math.max(idx + step, 0), scale.length - 1);
          }
          var note = scale[idx] * 0.5;
          var dur = i === syllables - 1
            ? 0.13 : 0.07 + Math.random() * 0.02;
          var gap = 0.028;
          var start = t0;
          var gain = ctx.createGain();
          gain.gain.setValueAtTime(0.0001, start);
          gain.gain.exponentialRampToValueAtTime(0.18, start + 0.006);
          gain.gain.exponentialRampToValueAtTime(0.0001,
            start + dur);
          gain.connect(ctx.destination);
          var osc = ctx.createOscillator();
          osc.type = "sine";
          var scoopFrom = note * 0.82;
          try {
            osc.frequency.setValueAtTime(scoopFrom, start);
            osc.frequency.exponentialRampToValueAtTime(note,
              start + 0.025);
            if (i === syllables - 1) {
              osc.frequency.setValueAtTime(note, start + 0.025);
              osc.frequency.linearRampToValueAtTime(note * 1.012,
                start + dur * 0.6);
              osc.frequency.linearRampToValueAtTime(note,
                start + dur);
            }
          } catch (e2) {
            try { osc.frequency.setValueAtTime(note, start); }
            catch (e3) { /* ignore */ }
          }
          var osc2 = null;
          try {
            osc2 = ctx.createOscillator();
            osc2.type = "sine";
            osc2.frequency.setValueAtTime(note * 2, start);
            var g2 = ctx.createGain();
            g2.gain.setValueAtTime(0.12, start);
            g2.gain.exponentialRampToValueAtTime(0.0001, start + dur);
            osc2.connect(g2);
            g2.connect(gain);
            osc2.start(start);
            osc2.stop(start + dur + 0.02);
          } catch (e4) { osc2 = null; }
          osc.connect(gain);
          osc.start(start);
          osc.stop(start + dur + 0.02);
          t0 = start + dur + gap;
        }
      } catch (e) { /* ignore: chirp never breaks chat */ }
    }

    return {
      // Plays an already-persisted assistant reply. Server voice first;
      // free browser speech when the server has no speech surface.
      // Silent only when neither exists: the text bubble is the record.
      async speak(text) {
        if (isMuted()) return;
        stopAudio();
        stopRecog();
        text = (text || "").trim().slice(0, 4000);
        if (!text || !opts.synthesize) return;
        var blob = null;
        try {
          blob = await opts.synthesize(text);
        } catch (e) { blob = null; }
        if (blob) {
          try {
            var url = URL.createObjectURL(blob);
            audio = new Audio(url);
            setOrbState("is-speaking");
            orb.setAttribute("aria-label", "Stop speaking");
            setStatus(statusEl, "Speaking… tap to stop.");
            audio.onended = function () {
              try { URL.revokeObjectURL(url); } catch (e) { /* ignore */ }
              if (audio) { audio = null; toIdle(""); }
            };
            await audio.play();
            return;
          } catch (e) {
            toIdle("");
            return;
          }
        }
        if (!browserSpeak(text)) toIdle("");
      },
      stop: toIdle,
      chirp: playChirp,
      untuck: untuck,
      destroy: clearTuck,
      holdMs: HOLD_MS,
    };
  }

  window.VoiceOrb = { create: createVoiceOrb };
})();
