// B4 voice orb: shared recorder + speaker for chat.html and phone.html.
// One circle, three visible states, tap to interrupt. Text stays the
// source of truth; every label is textContent-only (same XSS posture as
// the chat bubbles). The page owns auth (cookies vs inv_ key) and passes
// transcribe/speak fetchers in; this module owns mic level, playback,
// and orb classes only.
(function () {
  "use strict";

  function setState(orb, state) {
    orb.classList.remove("is-listening", "is-working", "is-speaking");
    if (state) orb.classList.add(state);
    orb.style.setProperty("--level", "0");
  }

  function setStatus(statusEl, text) {
    if (statusEl) statusEl.textContent = text || "";
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
    }

    function toIdle(msg) {
      recorder = null;
      chunks = [];
      stopTracks();
      stopAudio();
      setState(orb, null);
      orb.setAttribute("aria-label", "Talk");
      setStatus(statusEl, msg || idleLabel);
    }

    function friendlyTranscribeError(err) {
      var msg = (err && err.message) || "";
      if (/no transcription surface|connect openai for mic/i.test(msg)) {
        return "Voice needs an OpenAI provider — connect one, text still works.";
      }
      if (/voice is disabled/i.test(msg)) return "Voice is off — text still works.";
      if (/no connected provider/i.test(msg)) return "Connect a provider first.";
      return msg ? msg + " — type instead." : "Transcription failed — type instead.";
    }

    async function upload(blob) {
      setState(orb, "is-working");
      setStatus(statusEl, "Transcribing…");
      try {
        var text = await opts.transcribe(blob);
        text = (text || "").trim();
        if (!text) {
          toIdle("Heard nothing — try again.");
          return;
        }
        toIdle("");
        opts.onText(text);
      } catch (err) {
        toIdle(friendlyTranscribeError(err));
      }
    }

    async function start() {
      var MR = window.MediaRecorder;
      if (!MR || !navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
        setStatus(statusEl, "Voice not supported here — type instead.");
        return;
      }
      try {
        stream = await navigator.mediaDevices.getUserMedia({ audio: true });
      } catch (e) {
        setStatus(statusEl, "Mic blocked — allow it, or type instead.");
        return;
      }
      chunks = [];
      try {
        recorder = new MR(stream);
      } catch (e) {
        stopTracks();
        setStatus(statusEl, "Mic unavailable — type instead.");
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
      setState(orb, "is-listening");
      orb.setAttribute("aria-label", "Stop and send");
      setStatus(statusEl, "Listening… tap to send.");
    }

    orb.addEventListener("click", function () {
      // Tap while speaking interrupts the reply (Grok/GPT feel).
      if (orb.classList.contains("is-speaking")) {
        toIdle("");
        return;
      }
      if (recorder) {
        try { recorder.stop(); } catch (e) { toIdle(""); }
        return;
      }
      if (orb.classList.contains("is-working")) return;
      start();
    });

    return {
      // Plays an already-persisted assistant reply. Silent on any
      // failure: the text bubble is the record.
      async speak(text) {
        stopAudio();
        text = (text || "").trim().slice(0, 4000);
        if (!text || !opts.synthesize) return;
        var blob = null;
        try {
          blob = await opts.synthesize(text);
        } catch (e) { blob = null; }
        if (!blob) return;
        try {
          var url = URL.createObjectURL(blob);
          audio = new Audio(url);
          setState(orb, "is-speaking");
          orb.setAttribute("aria-label", "Stop speaking");
          setStatus(statusEl, "Speaking… tap to stop.");
          audio.onended = function () {
            try { URL.revokeObjectURL(url); } catch (e) { /* ignore */ }
            if (audio) { audio = null; toIdle(""); }
          };
          await audio.play();
        } catch (e) {
          toIdle("");
        }
      },
      stop: toIdle,
    };
  }

  window.VoiceOrb = { create: createVoiceOrb };
})();
