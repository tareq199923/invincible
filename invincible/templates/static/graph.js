// invincible/templates/static/graph.js
// Memory graph renderer for the merged /dashboard/memory page.
//
// Consumes the /memories/graph JSON projection (the permanent data
// contract built by core/memory_projection.py) and renders a
// force-directed network: nodes repel, edges pull, the user sits at
// the center. Similar memories link to each other with faint dashed
// edges. No dependencies, no build step - same identity as the rest
// of the console.
//
// Interaction: drag nodes, drag the canvas to pan, wheel to zoom,
// hover a memory node for a full-content preview card, click (or
// keyboard-select) a node to inspect it in the side panel AND collapse
// the graph to its local neighborhood (Obsidian-style local graph;
// Esc, Clear, or Reset view returns to the global graph). Memory node
// labels fade in as you zoom in. prefers-reduced-motion skips the
// animation loop: the simulation still runs, but synchronously and
// without tweening.
(function () {
  "use strict";

  // Mirror of _SOURCE_PALETTE in core/memory_projection.py - keep in
  // sync. Mapped with the same ord-sum hash so source colors match the
  // server-rendered legend and timeline.
  var PALETTE = ["#58a6ff", "#3fb950", "#e3b341", "#f47067", "#bc8cff",
                 "#39c5cf", "#ffa657", "#7ee787", "#ff7b72", "#d2a8ff"];
  var KIND_STROKE = {  // memory kind -> stroke-dasharray ("" = solid)
    note: "", fact: "4 2", preference: "2 3",
    decision: "", task: "1 4"
  };

  var LINK = {  // edge kind -> [rest length, strength, faint]
    owned_by: [140, 0.02, false],
    belongs_to: [60, 0.05, false],
    saved_by: [170, 0.008, true],
    similar_to: [110, 0.012, true]
  };

  var NODE_R = { user: 14, project: 10, source: 8, memory: 5 };

  function sourceColor(source) {
    var h = 0;
    for (var i = 0; i < source.length; i++) h += source.charCodeAt(i);
    return PALETTE[h % PALETTE.length];
  }
  function hash01(id) {  // deterministic per-node jitter (no RNG)
    var h = 2166136261;
    for (var i = 0; i < id.length; i++) {
      h ^= id.charCodeAt(i);
      h = (h * 16777619) | 0;
    }
    return ((h >>> 0) % 1000) / 1000;
  }
  var SVGNS = "http://www.w3.org/2000/svg";
  function el(tag, attrs) {
    var node = document.createElementNS(SVGNS, tag);
    for (var k in attrs) node.setAttribute(k, attrs[k]);
    return node;
  }

  function MemoryGraph(svg) {
    this.svg = svg;
    this.root = svg.querySelector("#memgraph-root");
    if (!this.root) this.root = el("g", { id: "memgraph-root" });
    while (this.root.firstChild) this.root.removeChild(this.root.firstChild);
    this.nodes = [];
    this.byId = {};
    this.edges = [];
    this.adj = {};
    this.selected = null;
    this.hovered = null;
    this.view = { x: 0, y: 0, k: 1 };  // pan/zoom transform
    this.energy = Infinity;
    this.running = false;
    this.reduced = window.matchMedia(
      "(prefers-reduced-motion: reduce)").matches;
  }

  MemoryGraph.prototype.load = function (payload) {
    var i, n, e;
    this.nodes = [];
    this.byId = {};
    for (i = 0; i < payload.nodes.length; i++) {
      n = payload.nodes[i];
      var extra = 0;
      this.nodes.push({
        id: n.id, kind: n.kind, label: n.label, content: n.content,
        source: n.source, memory_kind: n.memory_kind, layer: n.layer,
        confidence: n.confidence, keywords: n.keywords, count: n.count,
        ts: n.ts, similar: extra, vx: 0, vy: 0, el: null, labelEl: null
      });
      this.byId[n.id] = this.nodes[this.nodes.length - 1];
    }
    this.edges = [];
    for (i = 0; i < payload.edges.length; i++) {
      e = payload.edges[i];
      if (!this.byId[e.source] || !this.byId[e.target]) continue;
      this.edges.push({
        source: this.byId[e.source], target: this.byId[e.target],
        kind: e.kind, weight: e.weight || 1, el: null
      });
    }
    // Radius bump per similar edge (computed before the DOM build).
    for (i = 0; i < this.edges.length; i++) {
      if (this.edges[i].kind === "similar_to") {
        this.edges[i].source.similar++;
        this.edges[i].target.similar++;
      }
    }
    // Adjacency for hover highlighting.
    this.adj = {};
    for (i = 0; i < this.edges.length; i++) {
      e = this.edges[i];
      (this.adj[e.source.id] = this.adj[e.source.id] || {})[e.target.id] = e;
      (this.adj[e.target.id] = this.adj[e.target.id] || {})[e.source.id] = e;
    }
    this._seedPositions();
    this._buildDom();
    this._tick(300);  // settle synchronously first
    this._render();
    if (!this.reduced) this._run();
    this._bind();
  };

  MemoryGraph.prototype._seedPositions = function () {
    // Rings around the canvas center, angle from the id hash - the
    // same deterministic spirit as the server's _layout().
    var cx = 400, cy = 280;
    for (var i = 0; i < this.nodes.length; i++) {
      var n = this.nodes[i];
      var f = hash01(n.id);
      var ring = { user: 0, project: 90, source: 230, memory: 150 }[n.kind];
      n.x = cx + ring * Math.cos(6.283 * f);
      n.y = cy + ring * Math.sin(6.283 * f);
    }
  };

  MemoryGraph.prototype._buildDom = function () {
    var i;
    for (i = 0; i < this.edges.length; i++) {
      var e = this.edges[i];
      var attrs = { "class": "mg-edge " + e.kind };
      if (e.kind === "similar_to") attrs["stroke-width"] = e.weight;
      e.el = el("line", attrs);
      if (LINK[e.kind] && LINK[e.kind][2]) e.el.setAttribute("stroke-opacity", "0.35");
      this.root.appendChild(e.el);
    }
    for (i = 0; i < this.nodes.length; i++) {
      var n = this.nodes[i];
      var g = el("g", {
        "class": "mg-node", "data-node": n.id, tabindex: "0",
        role: "button",
        "aria-label": (n.kind === "memory"
          ? "memory: " + n.label + " (" + n.memory_kind + ", " + n.source + ")"
          : n.kind + ": " + n.label)
      });
      var r = this._radius(n);
      var circle = el("circle", { r: r });
      if (n.kind === "user") {
        circle.setAttribute("fill", "#e6edf3");
      } else if (n.kind === "project") {
        circle.setAttribute("fill", "#11161f");
        circle.setAttribute("stroke", "#7e8a9e");
        circle.setAttribute("stroke-width", "2");
      } else if (n.kind === "source") {
        circle.setAttribute("fill", sourceColor(n.label));
        circle.setAttribute("fill-opacity", "0.55");
      } else {
        circle.setAttribute("fill", sourceColor(n.source));
        circle.setAttribute("stroke", "#0a0d13");
        var dash = KIND_STROKE[n.memory_kind];
        if (dash) {
          circle.setAttribute("stroke-dasharray", dash);
          circle.setAttribute("stroke-width", "1.5");
        }
        var conf = typeof n.confidence === "number" ? n.confidence : 1;
        circle.setAttribute("fill-opacity", 0.55 + 0.45 * conf);
      }
      g.appendChild(circle);
      if (n.kind === "project" || n.kind === "user") {
        n.labelEl = el("text", { y: r + 18 });
        n.labelEl.textContent = n.label + (
          n.kind === "project" ? " (" + n.count + ")" : "");
        g.appendChild(n.labelEl);
      } else if (n.kind === "memory") {
        // Always-on label (Obsidian-style): content prefix that fades
        // in with zoom via the --mem-label-opacity custom property.
        n.labelEl = el("text", { "class": "mem-label", y: r + 12 });
        n.labelEl.textContent = (n.content || n.label).slice(0, 26) +
          ((n.content || n.label).length > 26 ? "…" : "");
        g.appendChild(n.labelEl);
      }
      n.el = g;
      this.root.appendChild(g);
    }
  };

  MemoryGraph.prototype._radius = function (n) {
    var base = NODE_R[n.kind] || 5;
    if (n.kind !== "memory") return base;
    return Math.min(base + n.similar, 9);
  };

  // --- simulation ----------------------------------------------------------

  MemoryGraph.prototype._tick = function (iterations) {
    var i, j, a, b, e, k, iterations = iterations || 1;
    for (k = 0; k < iterations; k++) {
      // Pairwise repulsion (n <= ~110, fine).
      for (i = 0; i < this.nodes.length; i++) {
        for (j = i + 1; j < this.nodes.length; j++) {
          a = this.nodes[i]; b = this.nodes[j];
          var dx = a.x - b.x, dy = a.y - b.y;
          var d2 = dx * dx + dy * dy || 0.01;
          var f = Math.min(2200 / d2, 4);
          var d = Math.sqrt(d2);
          a.vx += (dx / d) * f; a.vy += (dy / d) * f;
          b.vx -= (dx / d) * f; b.vy -= (dy / d) * f;
        }
      }
      // Spring attraction along edges: pull proportional to the
      // displacement from the rest length, along the unit direction.
      for (i = 0; i < this.edges.length; i++) {
        e = this.edges[i];
        var spec = LINK[e.kind] || [100, 0.01, false];
        var rest = spec[0], strength = spec[1];
        if (e.kind === "similar_to") strength *= Math.min(e.weight, 4) / 2;
        var ex = e.target.x - e.source.x, ey = e.target.y - e.source.y;
        var ed = Math.sqrt(ex * ex + ey * ey) || 0.01;
        var pull = strength * (ed - rest);
        var fx = (ex / ed) * pull, fy = (ey / ed) * pull;
        e.source.vx += fx; e.source.vy += fy;
        e.target.vx -= fx; e.target.vy -= fy;
      }
      // Weak gravity toward the user node's center + integrate.
      var user = this.byId["user"];
      var cx = user ? user.x : 400, cy = user ? user.y : 280;
      var energy = 0;
      for (i = 0; i < this.nodes.length; i++) {
        a = this.nodes[i];
        a.vx += (cx - a.x) * 0.0008;
        a.vy += (cy - a.y) * 0.0008;
        if (a.pinned) { a.vx = 0; a.vy = 0; continue; }
        a.vx *= 0.85; a.vy *= 0.85;
        a.x += Math.max(-18, Math.min(18, a.vx));
        a.y += Math.max(-18, Math.min(18, a.vy));
        a.x = Math.max(-100, Math.min(900, a.x));
        a.y = Math.max(-100, Math.min(660, a.y));
        energy += Math.abs(a.vx) + Math.abs(a.vy);
      }
      this.energy = energy / Math.max(1, this.nodes.length);
      if (this.energy < 0.05) break;
    }
  };

  MemoryGraph.prototype._run = function () {
    if (this.running) return;
    this.running = true;
    var self = this, frame = 0;
    (function loop() {
      if (frame++ > 3000) { self.running = false; return; }
      self._tick(1);
      self._render();
      if (self.energy >= 0.05) requestAnimationFrame(loop);
      else self.running = false;
    })();
  };

  // --- rendering ------------------------------------------------------------

  MemoryGraph.prototype._render = function () {
    var v = this.view;
    this.root.setAttribute(
      "transform",
      "translate(" + v.x + "," + v.y + ") scale(" + v.k + ")");
    // Obsidian-style label fade: memory labels appear as you zoom in
    // (0 at default zoom, fully visible at ~2.5x). Project/user labels
    // are always on.
    this.root.style.setProperty(
      "--mem-label-opacity",
      String(Math.max(0, Math.min(1, (v.k - 1) / 1.5))));
    for (var i = 0; i < this.edges.length; i++) {
      var e = this.edges[i];
      e.el.setAttribute("x1", e.source.x); e.el.setAttribute("y1", e.source.y);
      e.el.setAttribute("x2", e.target.x); e.el.setAttribute("y2", e.target.y);
    }
    for (i = 0; i < this.nodes.length; i++) {
      var n = this.nodes[i];
      if (n.dragX != null) { n.x = n.dragX; n.y = n.dragY; }
      n.el.setAttribute("transform", "translate(" + n.x + "," + n.y + ")");
    }
    this._highlight();
  };

  MemoryGraph.prototype._focusId = function () {
    return this.selected ? this.selected.id
         : this.hovered ? this.hovered.id : null;
  };

  MemoryGraph.prototype._highlight = function () {
    var focus = this._focusId();
    var keep = null;
    if (focus) keep = this.adj[focus] || {};
    // Local graph mode (Obsidian-style): when a node is selected, only
    // it and its direct neighbors stay visible; the rest hide. Cleared
    // by Esc / Clear / Reset view via select(null).
    var local = this.local;
    var localKeep = local ? this.adj[local] || {} : null;
    var i;
    for (i = 0; i < this.nodes.length; i++) {
      var n = this.nodes[i];
      var dim = focus && n.id !== focus && !keep[n.id];
      n.el.classList.toggle("dimmed", !!dim);
      n.el.classList.toggle("selected", n.id === focus);
      n.el.classList.toggle(
        "local-hidden",
        !!(localKeep && n.id !== local && !localKeep[n.id]));
    }
    for (i = 0; i < this.edges.length; i++) {
      var e = this.edges[i];
      var on = !focus || e.source.id === focus || e.target.id === focus;
      e.el.classList.toggle("dimmed", !on);
      e.el.classList.toggle(
        "local-hidden",
        !!(local && e.source.id !== local && e.target.id !== local));
    }
  };

  // --- hover preview card (Obsidian page preview) ---------------------------

  MemoryGraph.prototype._tipEl = function () {
    if (this.tip) return this.tip;
    var stage = this.svg.closest(".memgraph-stage");
    if (!stage) return null;
    var tip = document.createElement("div");
    tip.id = "memgraph-tip";
    tip.hidden = true;
    var body = document.createElement("div");
    body.className = "tip-content";
    var meta = document.createElement("div");
    meta.className = "tip-meta";
    tip.appendChild(body);
    tip.appendChild(meta);
    stage.appendChild(tip);
    this.tip = tip;
    this.tipBody = body;
    this.tipMeta = meta;
    return tip;
  };

  MemoryGraph.prototype._showTip = function (n, clientX, clientY) {
    if (!n || n.kind !== "memory") {
      if (this.tip) this.tip.hidden = true;
      return;
    }
    var tip = this._tipEl();
    if (!tip) return;
    // textContent only - saved memory text is never trusted markup.
    this.tipBody.textContent = n.content || n.label;
    this.tipMeta.textContent =
      n.memory_kind + " · " + n.layer + " · " + n.source;
    tip.hidden = false;
    var rect = tip.parentNode.getBoundingClientRect();
    var left = clientX - rect.left + 14;
    var top = clientY - rect.top + 14;
    // Keep the card inside the stage box.
    tip.style.left =
      Math.max(4, Math.min(left, rect.width - tip.offsetWidth - 4)) + "px";
    tip.style.top =
      Math.max(4, Math.min(top, rect.height - tip.offsetHeight - 4)) + "px";
  };

  // --- interaction ------------------------------------------------------------

  MemoryGraph.prototype._bind = function () {
    var self = this;

    function nodeFor(evt) {
      var target = evt.target;
      while (target && target !== self.root) {
        if (target.parentNode === self.root &&
            target.classList.contains("mg-node")) {
          return self.byId[target.getAttribute("data-node")];
        }
        target = target.parentNode;
      }
      return null;
    }

    this.svg.addEventListener("pointerdown", function (evt) {
      var n = nodeFor(evt);
      if (n) {
        self.drag = n;
        n.pinned = true;
        n.dragX = n.x; n.dragY = n.y;
      } else {
        self.panning = true;
        self.svg.classList.add("panning");
        self.panFrom = { x: evt.clientX, y: evt.clientY,
                         vx: self.view.x, vy: self.view.y };
      }
      self.svg.setPointerCapture && self.svg.setPointerCapture(evt.pointerId);
      evt.preventDefault();
    });

    this.svg.addEventListener("pointermove", function (evt) {
      if (self.drag) {
        self._showTip(null);
        // Screen -> viewBox coords (approximate: scale by the ratio of
        // viewBox width to client width, then undo the view transform).
        var rect = self.svg.getBoundingClientRect();
        var sx = 800 / rect.width;
        var vx = (evt.clientX - rect.left) * sx;
        var vy = (evt.clientY - rect.top) * (560 / rect.height);
        self.drag.dragX = (vx - self.view.x) / self.view.k;
        self.drag.dragY = (vy - self.view.y) / self.view.k;
        self._tick(1);
        self._render();
        return;
      }
      if (self.panning) {
        self._showTip(null);
        var r2 = self.svg.getBoundingClientRect();
        var kx = 800 / r2.width, ky = 560 / r2.height;
        self.view.x = self.panFrom.vx + (evt.clientX - self.panFrom.x) * kx;
        self.view.y = self.panFrom.vy + (evt.clientY - self.panFrom.y) * ky;
        self._render();
        return;
      }
      var hov = nodeFor(evt);
      if (hov !== self.hovered) {
        self.hovered = hov;
        self._highlight();
      }
      self._showTip(hov, evt.clientX, evt.clientY);
    });

    function endDrag() {
      if (self.drag) {
        self.drag.x = self.drag.dragX; self.drag.y = self.drag.dragY;
        self.drag.dragX = self.drag.dragY = null;
        self.drag.pinned = false;
        self.drag = null;
        if (!self.reduced) self._run();
      }
      self.panning = false;
      self.svg.classList.remove("panning");
    }
    this.svg.addEventListener("pointerup", endDrag);
    this.svg.addEventListener("pointercancel", endDrag);

    this.svg.addEventListener("wheel", function (evt) {
      evt.preventDefault();
      var rect = self.svg.getBoundingClientRect();
      var px = (evt.clientX - rect.left) / rect.width * 800;
      var py = (evt.clientY - rect.top) / rect.height * 560;
      var k = self.view.k * (evt.deltaY < 0 ? 1.12 : 1 / 1.12);
      k = Math.max(0.4, Math.min(6, k));
      // Zoom around the cursor: keep the point under it fixed.
      self.view.x = px - (px - self.view.x) * (k / self.view.k);
      self.view.y = py - (py - self.view.y) * (k / self.view.k);
      self.view.k = k;
      self._render();
    }, { passive: false });

    this.svg.addEventListener("click", function (evt) {
      var n = nodeFor(evt);
      if (n) self.select(n);
    });
    this.svg.addEventListener("pointerleave", function () {
      self._showTip(null);
    });
    // Keyboard: show the preview card anchored to the focused node.
    this.svg.addEventListener("focusin", function (evt) {
      var n = nodeFor(evt);
      if (!n) return;
      var rect = self.svg.getBoundingClientRect();
      var px = rect.left +
        (n.x * self.view.k + self.view.x) / 800 * rect.width;
      var py = rect.top +
        (n.y * self.view.k + self.view.y) / 560 * rect.height;
      self._showTip(n, px, py);
    });
    this.svg.addEventListener("focusout", function () {
      self._showTip(null);
    });
    this.svg.addEventListener("keydown", function (evt) {
      if (evt.key === "Escape") {
        self.select(null);
      } else if (evt.key === "Enter" || evt.key === " ") {
        var n = nodeFor(evt);
        if (n) { evt.preventDefault(); self.select(n); }
      }
    });

    var reset = document.getElementById("memgraph-reset");
    if (reset) reset.addEventListener("click", function () {
      self.select(null);  // exits local graph mode + clears the panel
      self.view = { x: 0, y: 0, k: 1 };
      self._seedPositions();
      self._tick(300);
      self._render();
      if (!self.reduced) self._run();
    });
  };

  // --- selection / detail panel ---------------------------------------------

  MemoryGraph.prototype.select = function (n) {
    this.selected = n;
    this.local = n ? n.id : null;  // local graph mode follows selection
    this._showTip(null);
    this._highlight();
    var body = document.getElementById("memdetail-body");
    var clear = document.getElementById("memdetail-clear");
    var hint = this.svg.closest(".memgraph-layout")
                 .querySelector(".memdetail-hint");
    if (!body) return;
    if (!n) {
      body.innerHTML = "";
      if (clear) { clear.hidden = true; clear.onclick = null; }
      if (hint) hint.hidden = false;
      return;
    }
    var when = n.ts ? new Date(n.ts * 1000).toLocaleString() : null;
    var rows = [];
    if (n.kind === "memory") {
      rows.push(["content", n.label]);
      rows.push(["kind", n.memory_kind]);
      rows.push(["layer", n.layer]);
      if (typeof n.confidence === "number")
        rows.push(["confidence", n.confidence.toFixed(2)]);
      rows.push(["source", n.source]);
      if (when) rows.push(["saved", when]);
      if (n.keywords && n.keywords.length)
        rows.push(["keywords", n.keywords.map(function (k) {
          return "<code>" + k.replace(/[<>&]/g, "") + "</code>";
        }).join(" ")]);
      var rel = [];
      for (var id in (this.adj[n.id] || {})) {
        var e = this.adj[n.id][id];
        if (e.kind === "similar_to") {
          var other = e.source.id === n.id ? e.target : e.source;
          rel.push(other.label);
        }
      }
      if (rel.length)
        rows.push(["related", rel.slice(0, 6).join("<br>")]);
    } else if (n.kind === "project") {
      rows.push(["project", n.label]);
      rows.push(["memories", n.count]);
    } else if (n.kind === "source") {
      rows.push(["source", n.label]);
      rows.push(["memories saved", n.count]);
    } else {
      rows.push(["user", n.label]);
      rows.push(["projects", n.count || ""]);
    }
    body.innerHTML = "<dl>" + rows.map(function (r) {
      return "<dt>" + r[0] + "</dt><dd>" + r[1] + "</dd>";
    }).join("") + "</dl>";
    if (hint) hint.hidden = true;
    if (clear) {
      var self = this;
      clear.hidden = false;
      clear.onclick = function () { self.select(null); };
    }
  };

  // --- boot -------------------------------------------------------------------

  function boot() {
    var svg = document.getElementById("memgraph");
    if (!svg) return;
    var src = svg.getAttribute("data-graph-src") || "/memories/graph";
    var params = [];
    var kind = svg.getAttribute("data-graph-kind");
    var project = svg.getAttribute("data-graph-project");
    if (kind) params.push("kind=" + encodeURIComponent(kind));
    if (project && project !== "0")
      params.push("project_id=" + encodeURIComponent(project));
    var url = params.length ? src + "?" + params.join("&") : src;
    fetch(url, { headers: { Accept: "application/json" } })
      .then(function (r) { return r.ok ? r.json() : Promise.reject(r); })
      .then(function (payload) {
        if (!payload || !payload.nodes ||
            !payload.nodes.some(function (n) { return n.kind === "memory"; })) {
          return;  // empty store: server-rendered empty state stands
        }
        new MemoryGraph(svg).load(payload);
      })
      .catch(function () { /* server-rendered fallback stands */ });
  }

  if (document.readyState === "loading")
    document.addEventListener("DOMContentLoaded", boot);
  else boot();
})();
