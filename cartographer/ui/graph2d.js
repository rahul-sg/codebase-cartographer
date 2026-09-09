/* 2D force-directed graph.
 *
 * Written from scratch rather than pulling d3: this has to run offline on a
 * locked-down machine, where a CDN request just fails. It is also less code
 * than configuring a library, because the requirements are narrow -- a few
 * hundred nodes per view, since the hierarchy does the heavy lifting.
 *
 * Barnes-Hut is deliberately omitted. At the node counts a readable view
 * actually contains, O(n^2) repulsion is a fraction of a frame, and an
 * approximation would only add code to get wrong.
 */
(function (global) {
  "use strict";

  var KIND_COLOR = {
    service: "--accent", library: "--build", topic: "--async",
    datastore: "--data", table: "--data", host: "--deploy",
    infra: "--deploy", "legacy-page": "--legacy", package: "--accent",
    file: "--code", "class": "--accent", "function": "--code",
    route: "--contract"
  };

  function cssVar(name) {
    return getComputedStyle(document.documentElement)
      .getPropertyValue(name).trim() || "#888";
  }

  /**
   * Deterministic PRNG. The starfield must be identical every frame or the
   * sky boils; seeding a small generator gives a fixed field without storing
   * one.
   */
  function rng(seed) {
    return function () {
      seed |= 0; seed = (seed + 0x6D2B79F5) | 0;
      var t = Math.imul(seed ^ (seed >>> 15), 1 | seed);
      t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t;
      return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
    };
  }

  /**
   * Shared deep-field backdrop: nebula, then a parallaxed starfield.
   *
   * Lives here rather than inside either engine because BOTH views draw it,
   * and a hundred lines of starfield duplicated across two files would drift
   * apart the first time one of them was touched. graph2d.js loads first, so
   * this uses the same publish-to-global arrangement already established for
   * cssVar / KIND_COLOR / shortLabel.
   *
   * Each caller holds its own instance: that is where the size and gradient
   * caches live, and the two views differ in both size and parallax source.
   *
   * Costs three cached gradients and ~240 one-pixel rects per frame. Stars
   * are rects rather than arcs because at this size the shape is
   * indistinguishable and the fill is markedly cheaper.
   */
  function makeSky() {
    return {
      draw: function (ctx, W, H, lit, parX, parY) {
        var tint = cssVar("--bg") + cssVar("--async") + cssVar("--accent");

        if (this._w !== W || this._h !== H || this._tint !== tint) {
          this._w = W; this._h = H; this._tint = tint;
          // Nebula: broad, off-centre, low-alpha pools of colour.
          this._neb = [
            { x: 0.30, y: 0.34, r: 0.62, c: cssVar("--accent"), a: lit ? 0.17 : 0.05 },
            { x: 0.72, y: 0.30, r: 0.50, c: cssVar("--async"),  a: lit ? 0.14 : 0.04 },
            { x: 0.54, y: 0.78, r: 0.66, c: cssVar("--data"),   a: lit ? 0.09 : 0.03 }
          ].map(function (n) {
            var R = Math.max(W, H) * n.r;
            var g = ctx.createRadialGradient(W * n.x, H * n.y, 0,
                                             W * n.x, H * n.y, R);
            g.addColorStop(0, n.c);
            g.addColorStop(1, "transparent");
            return { g: g, a: n.a };
          });
          // Stars in normalised space, so a resize does not reshuffle them.
          var rand = rng(20260908);
          this._stars = [];
          for (var i = 0; i < 240; i++) {
            this._stars.push({
              x: rand(), y: rand(),
              s: rand() < 0.9 ? 1 : 2,             // a few brighter ones
              a: 0.20 + rand() * 0.55,
              tw: 0.4 + rand() * 1.7,              // twinkle rate
              ph: rand() * 6.283,
              par: 0.25 + rand() * 0.75            // parallax depth
            });
          }
          var bg = ctx.createRadialGradient(W / 2, H * 0.52, 0,
                                            W / 2, H * 0.52, Math.max(W, H) * 0.78);
          bg.addColorStop(0, cssVar("--panel"));
          bg.addColorStop(1, cssVar("--bg"));
          this._base = bg;
        }

        ctx.save();
        ctx.globalAlpha = 0.5;
        ctx.fillStyle = this._base;
        ctx.fillRect(0, 0, W, H);

        if (lit) {
          ctx.globalCompositeOperation = "lighter";
          for (var k = 0; k < this._neb.length; k++) {
            ctx.globalAlpha = this._neb[k].a;
            ctx.fillStyle = this._neb[k].g;
            ctx.fillRect(0, 0, W, H);
          }
          var t = Date.now() / 1000;
          var px = parX || 0, py = parY || 0;
          ctx.fillStyle = "#ffffff";
          for (var j = 0; j < this._stars.length; j++) {
            var st = this._stars[j];
            var sx = st.x - px * st.par;
            var sy = st.y - py * st.par;
            sx = sx - Math.floor(sx);
            sy = sy - Math.floor(sy);
            ctx.globalAlpha = st.a * (0.55 + 0.45 * Math.sin(t * st.tw + st.ph));
            ctx.fillRect(sx * W, sy * H, st.s, st.s);
          }
        }
        ctx.restore();
      }
    };
  }

  /**
   * Motion budget, honoured by both engines.
   *
   * A codebase is not a static diagram -- things flow through it and parts of
   * it change constantly -- and the view is more honest when it shows that.
   * But motion has to MEAN something or it is just noise, so the two animated
   * properties are both tied to real measurements:
   *
   *   edge flow   travels source -> target, so direction is visible at a
   *               glance instead of requiring a click. Speed rises with call
   *               volume.
   *   node pulse  beats at a rate set by PageRank -- the busier a thing is in
   *               the graph, the more alive it looks.
   *
   * Anyone who has asked their OS for less motion gets a completely static
   * render, not a slower one.
   */
  var REDUCED = (function () {
    try {
      return window.matchMedia &&
             window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    } catch (e) { return false; }
  })();

  function now() { return REDUCED ? 0 : Date.now() / 1000; }

  /** Luminance test on --bg, so the sky only lights up on dark themes. */
  function isDark() {
    var hex = String(cssVar("--bg")).trim().replace("#", "");
    if (hex.length === 3) {
      hex = hex[0] + hex[0] + hex[1] + hex[1] + hex[2] + hex[2];
    }
    var v = parseInt(hex, 16);
    if (isNaN(v) || hex.length !== 6) return true;   // dark is the default
    return (0.2126 * ((v >> 16) & 255) + 0.7152 * ((v >> 8) & 255) +
            0.0722 * (v & 255)) < 128;
  }

  function Graph2D(canvas, opts) {
    this.cv = canvas;
    this.ctx = canvas.getContext("2d");
    this.opts = opts || {};
    this.nodes = [];
    this.edges = [];
    this.byId = {};
    this.tx = 0; this.ty = 0; this.scale = 1;
    this.alpha = 1;
    this.frozen = false;
    // Both engines share ONE canvas and bind the same five events to it and
    // to window. Without an active flag, interacting with the 3D view also
    // drove this one -- and since the pan and wheel handlers call draw()
    // directly (not via the animation loop, which stop() does halt), the 2D
    // map was painted straight over the 3D scene. Hence "the Layers tab
    // reverts to the map when I move around".
    this.active = true;
    this.selected = null;
    this.hover = null;
    this.hoverEdge = null;
    this.highlight = null;      // set of ids to emphasise
    this.running = false;
    this._bind();
  }

  Graph2D.prototype.setData = function (data, keepView) {
    var self = this;
    var prev = {};
    this.nodes.forEach(function (n) { prev[n.id] = n; });

    var W = this.cv.clientWidth || 900, H = this.cv.clientHeight || 600;
    var total = Math.max(1, (data.nodes || []).length);
    this.nodes = (data.nodes || []).map(function (d, i) {
      var old = prev[d.id];
      // Phyllotaxis seed, as Graph3D already uses. The previous seed was
      // `90 + (i % 7) * 42`, which places every node on one of exactly SEVEN
      // concentric rings -- and any frame drawn before the simulation has
      // relaxed shows those rings as a striking, entirely artificial pattern.
      // The rings are gone within a few hundred milliseconds, but "a few
      // hundred milliseconds" includes every first paint, every tab switch
      // and every filter toggle, so in practice they are seen constantly.
      // Phyllotaxis covers the disc evenly from frame zero, so an unrelaxed
      // layout looks merely rough rather than wrong.
      var ang = i * 2.39996;                       // golden angle
      var rad = 18 * Math.sqrt(i + 0.5);
      var n = Object.assign({}, d);
      n.x = old ? old.x : W / 2 + Math.cos(ang) * rad;
      n.y = old ? old.y : H / 2 + Math.sin(ang) * rad;
      n.vx = 0; n.vy = 0;
      n.pin = old ? old.pin : false;
      n.deg = 0;
      n.r = self._radius(n);
      // Fixed phase per node: without it every node pulses in unison, which
      // reads as the whole canvas throbbing rather than as individual life.
      n._ph = (i * 2.399) % 6.283;
      return n;
    });
    this.byId = {};
    this.nodes.forEach(function (n) { self.byId[n.id] = n; });

    this.edges = (data.edges || []).map(function (e) {
      return Object.assign({}, e, {
        s: self.byId[e.source], t: self.byId[e.target]
      });
    }).filter(function (e) { return e.s && e.t; });
    this.edges.forEach(function (e) { e.s.deg++; e.t.deg++; });

    // Bundle parallel edges so multiple relationship kinds between the same
    // pair stay individually visible instead of drawing on top of each other.
    var pairs = {};
    this.edges.forEach(function (e) {
      var k = e.source < e.target ? e.source + " " + e.target
                                  : e.target + " " + e.source;
      (pairs[k] = pairs[k] || []).push(e);
    });
    Object.keys(pairs).forEach(function (k) {
      var arr = pairs[k], n = arr.length;
      arr.forEach(function (e, i) { e.curve = n === 1 ? 0 : (i - (n - 1) / 2) * 16; });
    });

    this.alpha = keepView ? Math.max(this.alpha, 0.55) : 1;
    if (!keepView) {
      this.tx = 0; this.ty = 0; this.scale = 1;
      // Keep re-fitting while the layout settles. A single fit on a timer
      // frames whatever half-arranged state existed at that instant, which is
      // why drilling into a small view used to leave nodes off-screen.
      this.autofit = true;
    }
    this.start();
  };

  Graph2D.prototype._radius = function (n) {
    var base = 6;
    if (n.kind === "service") base = 13;
    else if (n.kind === "package") base = 11;
    else if (n.kind === "library" || n.kind === "datastore" ||
             n.kind === "topic" || n.kind === "host") base = 9;
    var size = n.size || 0;
    return base + Math.min(13, Math.sqrt(size) * 1.7) + Math.min(7, (n.rank || 0) * 130);
  };

  /* ---------- simulation ---------- */

  Graph2D.prototype.step = function () {
    if (this.frozen || this.alpha < 0.002) return;
    var N = this.nodes, E = this.edges, k = this.alpha;
    var W = this.cv.clientWidth || 900, H = this.cv.clientHeight || 600;
    var i, j, a, b, dx, dy, d2, d, f, fx, fy;
    // Scale repulsion with the crowd: a five-node view needs far less push
    // apart than a fifty-node one, or it flings everything off screen.
    //
    // The old ceiling of 4200 was reached at ~40 nodes, so every view from
    // there to twelve hundred got exactly the same push -- which is why a
    // large estate collapsed into one dense ball with a spray of leaves. The
    // ceiling is now high enough to keep scaling into the thousands; the
    // per-iteration displacement cap further down is what keeps that safe,
    // and it did not exist when 4200 was chosen.
    var rep = Math.max(1400, Math.min(9000, 700 + N.length * 90));

    // Screens are wider than they are tall, and so is a readable graph. A
    // circular blob wastes the left and right thirds of the canvas; biasing
    // horizontal repulsion spreads the estate across the space that exists.
    var ASPECT = 1.5;

    for (i = 0; i < N.length; i++) {
      a = N[i];
      for (j = i + 1; j < N.length; j++) {
        b = N[j];
        dx = b.x - a.x; dy = b.y - a.y; d2 = dx * dx + dy * dy;
        if (d2 > 260000) continue;
        // Floor the separation well above zero. At d2 = 1 this term reaches
        // (rep + r*190) / 1, roughly 15,000 in a single step for two large
        // nodes -- one near-coincident pair was enough to fling the whole
        // component across the plane.
        if (d2 < 64) { d2 = 64; dx = Math.random() - 0.5; dy = Math.random() - 0.5; }
        f = (rep + (a.r + b.r) * 190) / d2;
        d = Math.sqrt(d2); fx = dx / d * f * ASPECT; fy = dy / d * f;
        a.vx -= fx * k; a.vy -= fy * k; b.vx += fx * k; b.vy += fy * k;
      }
    }
    for (i = 0; i < E.length; i++) {
      var e = E[i];
      dx = e.t.x - e.s.x; dy = e.t.y - e.s.y;
      d = Math.hypot(dx, dy) || 1;
      // Longer rest length: edges that are too short pull every neighbour
      // into the hub and produce the dense core with a leaf fringe.
      var rest = 168 + e.s.r + e.t.r;
      f = (d - rest) * 0.016 * k;
      fx = dx / d * f; fy = dy / d * f;
      e.s.vx += fx; e.s.vy += fy; e.t.vx -= fx; e.t.vy -= fy;
    }
    for (i = 0; i < N.length; i++) {
      a = N[i];
      if (a.pin) { a.vx = a.vy = 0; continue; }
      // Gentle layer banding: hosts/clients drift up, data sinks down. It
      // makes the picture read top-to-bottom like an architecture diagram.
      var target = H * (0.18 + 0.2 * (a.layer == null ? 2 : a.layer));
      a.vy += (target - a.y) * 0.0035 * k;
      // Weaker horizontal gathering than vertical: the layer banding above
      // should keep the picture legible top-to-bottom, while x is allowed to
      // spread into the width of the screen.
      a.vx += (W / 2 - a.x) * 0.0007 * k;
      a.vx *= 0.86; a.vy *= 0.86;
      // Cap displacement per iteration -- Fruchterman-Reingold's "temperature",
      // and the piece this simulation was missing. At estate scale every node
      // sits inside the repulsion cutoff of ~1000 others, so velocity compounds
      // faster than damping removes it: coordinates reached 1e15 within 40
      // steps, fit() then computed a scale of ~1e-12, and the Map view
      // rendered the whole estate as a single speck. Damping alone cannot fix
      // that, because the sum of forces grows with the node count while the
      // damping factor does not.
      var sp = Math.hypot(a.vx, a.vy);
      if (sp > 30) { a.vx = a.vx / sp * 30; a.vy = a.vy / sp * 30; }
      a.x += a.vx; a.y += a.vy;
      // Last line of defence: a non-finite coordinate poisons every force it
      // takes part in, so one bad node would corrupt the entire layout.
      if (!isFinite(a.x) || !isFinite(a.y)) {
        a.x = W / 2 + (Math.random() - 0.5) * 40;
        a.y = H / 2 + (Math.random() - 0.5) * 40;
        a.vx = a.vy = 0;
      }
    }
    this.alpha *= 0.9935;
  };

  /* ---------- rendering ---------- */

  Graph2D.prototype.resize = function () {
    var dpr = Math.min(window.devicePixelRatio || 1, 2);
    var r = this.cv.getBoundingClientRect();
    this.cv.width = Math.max(1, r.width * dpr);
    this.cv.height = Math.max(1, r.height * dpr);
    this.ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  };

  Graph2D.prototype.draw = function () {
    // Guarded here as well as in the handlers: draw() is reachable from
    // several call sites, and one stray frame is enough to replace the view.
    if (!this.active) return;
    var ctx = this.ctx;
    var W = this.cv.clientWidth, H = this.cv.clientHeight;
    ctx.clearRect(0, 0, W, H);

    // Same deep field as the Layers view, drawn in SCREEN space before the
    // pan/zoom transform -- otherwise the stars would scale with the graph
    // and stop reading as a distant sky. Parallax comes from the pan offset,
    // heavily damped so the field drifts rather than tracks.
    if (!this._skyField) this._skyField = makeSky();
    this._skyField.draw(ctx, W, H, isDark(), this.tx / 12000, this.ty / 12000);

    ctx.save();
    ctx.translate(this.tx, this.ty);
    ctx.scale(this.scale, this.scale);

    var T = now();
    var hl = this.highlight;
    var showLabels = this.scale > 0.55;
    this._labelCells = {};
    // Draw the biggest nodes first so that when labels compete for space the
    // more important one is the one that survives.
    this.nodes.sort(function (a, b) { return b.r - a.r; });

    for (var i = 0; i < this.edges.length; i++) {
      var e = this.edges[i];
      if (this.opts.edgeVisible && !this.opts.edgeVisible(e)) continue;
      var dim = hl && !(hl[e.source] && hl[e.target]);
      var col = cssVar("--" + ((e.style && e.style.kind) || "code"));
      ctx.strokeStyle = col;
      // Flow: dashes travel from source to target, so direction reads without
      // clicking. Only on the kinds that actually carry traffic -- structural
      // edges (imports, extends) do not "flow" and animating them would be a
      // lie told for decoration.
      var fkind = (e.style && e.style.kind) || "code";
      var flows = !dim && !REDUCED &&
                  (fkind === "sync" || fkind === "async" || fkind === "data");
      if (flows) {
        var speed = 14 + Math.min(46, Math.log1p(e.count || 1) * 18);
        ctx.setLineDash([7, 9]);
        ctx.lineDashOffset = -(T * speed) % 16;
      }
      // A seam between two repositories is worth more contrast than a link
      // inside one. Intra-repo edges recede rather than disappear, so the
      // structure is still legible underneath.
      var base = e._cross ? 0.85 : (e._intra ? 0.22 : 0.42);
      ctx.globalAlpha = dim ? 0.06 : (e === this.hoverEdge ? 0.95 : base);
      ctx.lineWidth = Math.min(6,
        0.7 + Math.log1p(e.count || 1) * 0.85 + (e.runtime ? 1.4 : 0));
      ctx.setLineDash(e.provenance === "INFERRED" ? [4, 4]
                      : (e.style && e.style.kind === "async" ? [7, 4] : []));
      this._edgePath(ctx, e);
      ctx.stroke();
      if (flows) { ctx.setLineDash([]); ctx.lineDashOffset = 0; }
      if (!dim && this.scale > 0.5) this._arrow(ctx, e, col);
    }
    ctx.setLineDash([]);
    ctx.globalAlpha = 1;

    for (var j = 0; j < this.nodes.length; j++) {
      var n = this.nodes[j];
      var dimN = hl && !hl[n.id];
      // `_repoColor` is set by the app layer when "colour by repo" is on, and
      // only for nodes that actually have a repo -- shared resources keep
      // their kind colour so they stay readable as tables/topics.
      var color = n._repoColor || cssVar(KIND_COLOR[n.kind] || "--code");
      ctx.beginPath();
      // Pulse: amplitude and rate rise with centrality, so the parts of the
      // estate that everything depends on visibly beat harder. Bounded at 6%
      // so it never changes which node looks bigger than which.
      var pr = REDUCED ? 0
        : Math.sin(T * (0.7 + Math.min(1.9, (n.rank || 0) * 150)) + n._ph) *
          0.06 * Math.min(1, 0.35 + (n.rank || 0) * 90);
      ctx.arc(n.x, n.y, n.r * (1 + pr), 0, 6.2832);
      ctx.fillStyle = color;
      ctx.globalAlpha = dimN ? 0.14 : (n.boundary ? 0.35 : 0.92);
      ctx.fill();
      ctx.globalAlpha = dimN ? 0.2 : 1;
      ctx.lineWidth = n.boundary ? 1.4 : 1;
      ctx.strokeStyle = color;
      ctx.stroke();

      if (n.drillable && !dimN) {          // dotted ring = has an inside to open
        ctx.beginPath();
        ctx.arc(n.x, n.y, n.r + 3.2, 0, 6.2832);
        ctx.setLineDash([2, 3]);
        ctx.globalAlpha = 0.55;
        ctx.stroke();
        ctx.setLineDash([]);
      }
      if (this.selected === n.id) {
        ctx.beginPath();
        ctx.arc(n.x, n.y, n.r + 6, 0, 6.2832);
        ctx.strokeStyle = cssVar("--sel"); ctx.lineWidth = 2.5;
        ctx.globalAlpha = 1; ctx.stroke();
      }
      if (!dimN && this._labelOk(n, showLabels)) {
        ctx.globalAlpha = 1;
        ctx.font = (n.kind === "service" ? "600 " : "") +
                   Math.max(10, Math.min(13, 9 + n.r * 0.18)) + "px ui-sans-serif";
        ctx.textAlign = "center";
        var label = shortLabel(n);
        // A halo keeps text readable where it crosses an edge.
        ctx.lineWidth = 3;
        ctx.strokeStyle = cssVar("--bg");
        ctx.strokeText(label, n.x, n.y + n.r + 12);
        ctx.fillStyle = cssVar("--fg");
        ctx.fillText(label, n.x, n.y + n.r + 12);
      }
    }
    ctx.globalAlpha = 1;
    ctx.restore();
  };

  /* Long identifiers are unreadable at graph scale and collide with their
   * neighbours. Datastores are the worst offender: `mysql://mysqldb/salesdev`
   * says far less than `salesdev` in a picture where the host is the same
   * everywhere. The full value stays in the tooltip and the detail panel. */
  function shortLabel(n) {
    var s = String(n.label || "");
    if (n.kind === "datastore") {
      var m = s.match(/^([a-z]+):\/\/[^/]+\/(.+)$/i);
      if (m) return m[2];
      if (s.indexOf("jndi") === 0) return s.split("/").pop();
    }
    if (n.kind === "host") return s.split(".")[0];
    if (n.kind === "legacy-page") return s.split("/").pop();
    if (s.length > 28) return s.slice(0, 27) + "…";
    return s;
  }

  /* Draw a label only when it will not land on top of one already drawn.
   * Cheap grid occupancy: exact enough for a picture, and far cheaper than
   * real label placement. */
  Graph2D.prototype._labelOk = function (n, showLabels) {
    if (this.selected === n.id || this.hover === n) return true;
    if (!showLabels && n.r <= 14) return false;
    var cell = 34;
    var kx = Math.round((n.x) / cell), ky = Math.round((n.y + n.r) / cell);
    var key = kx + "," + ky;
    if (this._labelCells[key]) return false;
    this._labelCells[key] = 1;
    return true;
  };

  Graph2D.prototype._edgePath = function (ctx, e) {
    ctx.beginPath();
    ctx.moveTo(e.s.x, e.s.y);
    if (e.curve) {
      var mx = (e.s.x + e.t.x) / 2, my = (e.s.y + e.t.y) / 2;
      var dx = e.t.x - e.s.x, dy = e.t.y - e.s.y, d = Math.hypot(dx, dy) || 1;
      ctx.quadraticCurveTo(mx - dy / d * e.curve, my + dx / d * e.curve,
                           e.t.x, e.t.y);
    } else {
      ctx.lineTo(e.t.x, e.t.y);
    }
  };

  Graph2D.prototype._arrow = function (ctx, e, col) {
    var dx = e.t.x - e.s.x, dy = e.t.y - e.s.y, d = Math.hypot(dx, dy) || 1;
    var ux = dx / d, uy = dy / d;
    var px = e.t.x - ux * (e.t.r + 2), py = e.t.y - uy * (e.t.r + 2);
    var a = 5.5;
    ctx.beginPath();
    ctx.moveTo(px, py);
    ctx.lineTo(px - ux * a - uy * a * 0.6, py - uy * a + ux * a * 0.6);
    ctx.lineTo(px - ux * a + uy * a * 0.6, py - uy * a - ux * a * 0.6);
    ctx.closePath();
    ctx.fillStyle = col;
    ctx.fill();
  };

  /* ---------- hit testing ---------- */

  Graph2D.prototype.toWorld = function (mx, my) {
    return { x: (mx - this.tx) / this.scale, y: (my - this.ty) / this.scale };
  };

  Graph2D.prototype.nodeAt = function (mx, my) {
    var p = this.toWorld(mx, my), best = null, bd = 1e9;
    for (var i = 0; i < this.nodes.length; i++) {
      var n = this.nodes[i];
      var d = Math.hypot(n.x - p.x, n.y - p.y);
      if (d < n.r + 5 && d < bd) { bd = d; best = n; }
    }
    return best;
  };

  Graph2D.prototype.edgeAt = function (mx, my) {
    var p = this.toWorld(mx, my), best = null, bd = 7 / this.scale;
    for (var i = 0; i < this.edges.length; i++) {
      var e = this.edges[i];
      if (this.opts.edgeVisible && !this.opts.edgeVisible(e)) continue;
      var d = distToSeg(p, e.s, e.t);
      if (d < bd) { bd = d; best = e; }
    }
    return best;
  };

  function distToSeg(p, a, b) {
    var dx = b.x - a.x, dy = b.y - a.y, L = dx * dx + dy * dy;
    if (L === 0) return Math.hypot(p.x - a.x, p.y - a.y);
    var t = Math.max(0, Math.min(1, ((p.x - a.x) * dx + (p.y - a.y) * dy) / L));
    return Math.hypot(p.x - (a.x + t * dx), p.y - (a.y + t * dy));
  }

  /* ---------- view control ---------- */

  Graph2D.prototype.fit = function (pad) {
    if (!this.nodes.length) return;
    pad = pad == null ? 60 : pad;
    var x0 = Infinity, y0 = Infinity, x1 = -Infinity, y1 = -Infinity;
    this.nodes.forEach(function (n) {
      x0 = Math.min(x0, n.x - n.r); y0 = Math.min(y0, n.y - n.r);
      x1 = Math.max(x1, n.x + n.r); y1 = Math.max(y1, n.y + n.r);
    });
    var W = this.cv.clientWidth, H = this.cv.clientHeight;
    var s = Math.min((W - pad * 2) / Math.max(1, x1 - x0),
                     (H - pad * 2) / Math.max(1, y1 - y0));
    this.scale = Math.max(0.08, Math.min(2.4, s));
    this.tx = W / 2 - ((x0 + x1) / 2) * this.scale;
    this.ty = H / 2 - ((y0 + y1) / 2) * this.scale;
    this.draw();
  };

  /**
   * Zoom range derived from the content, not from constants.
   *
   * A hard floor of 0.06 let the estate shrink to a speck in the middle of an
   * empty canvas -- technically zoomed out, practically useless. The floor is
   * now a fraction of the scale at which the graph fits, so it stays sensible
   * whether the view holds twelve nodes or twelve hundred.
   */
  Graph2D.prototype._scaleLimits = function () {
    if (!this.nodes.length) return { min: 0.06, max: 8, fit: 1 };
    var x0 = Infinity, y0 = Infinity, x1 = -Infinity, y1 = -Infinity;
    for (var i = 0; i < this.nodes.length; i++) {
      var n = this.nodes[i];
      if (!isFinite(n.x) || !isFinite(n.y)) continue;
      if (n.x - n.r < x0) x0 = n.x - n.r;
      if (n.y - n.r < y0) y0 = n.y - n.r;
      if (n.x + n.r > x1) x1 = n.x + n.r;
      if (n.y + n.r > y1) y1 = n.y + n.r;
    }
    var W = this.cv.clientWidth || 900, H = this.cv.clientHeight || 600;
    if (!isFinite(x0) || !isFinite(x1)) return { min: 0.06, max: 8, fit: 1 };
    var fit = Math.min((W - 120) / Math.max(1, x1 - x0),
                       (H - 120) / Math.max(1, y1 - y0));
    return { min: Math.max(0.06, fit * 0.55), max: 8, fit: fit };
  };

  /**
   * Ease pan back toward a centred view. Called only when zooming OUT: with
   * both directions anchored on the cursor the offsets never cancel, so
   * zooming in at one point and out at another walks the graph off-centre.
   */
  Graph2D.prototype._recentre = function (amount) {
    var x0 = Infinity, y0 = Infinity, x1 = -Infinity, y1 = -Infinity;
    for (var i = 0; i < this.nodes.length; i++) {
      var n = this.nodes[i];
      if (!isFinite(n.x) || !isFinite(n.y)) continue;
      if (n.x < x0) x0 = n.x; if (n.x > x1) x1 = n.x;
      if (n.y < y0) y0 = n.y; if (n.y > y1) y1 = n.y;
    }
    if (!isFinite(x0)) return;
    var W = this.cv.clientWidth, H = this.cv.clientHeight;
    var wantX = W / 2 - ((x0 + x1) / 2) * this.scale;
    var wantY = H / 2 - ((y0 + y1) / 2) * this.scale;
    this.tx += (wantX - this.tx) * amount;
    this.ty += (wantY - this.ty) * amount;
  };

  Graph2D.prototype.zoomBy = function (f) {
    this.autofit = false;
    var W = this.cv.clientWidth / 2, H = this.cv.clientHeight / 2;
    var lim = this._scaleLimits();
    var prev = this.scale;
    var ns = Math.max(lim.min, Math.min(lim.max, prev * f));
    this.tx = W - (W - this.tx) * (ns / prev);
    this.ty = H - (H - this.ty) * (ns / prev);
    var shrink = ns / prev;
    this.scale = ns;
    if (ns < prev) this._recentre(1 - shrink);
    this.draw();
  };

  Graph2D.prototype.centerOn = function (id, zoom) {
    var n = this.byId[id];
    if (!n) return;
    if (zoom) this.scale = Math.max(this.scale, 1.1);
    this.tx = this.cv.clientWidth / 2 - n.x * this.scale;
    this.ty = this.cv.clientHeight / 2 - n.y * this.scale;
    this.draw();
  };

  Graph2D.prototype.setHighlight = function (ids) {
    if (!ids) { this.highlight = null; this.draw(); return; }
    var m = {};
    ids.forEach(function (i) { m[i] = 1; });
    this.highlight = m;
    this.draw();
  };

  Graph2D.prototype.neighbours = function (id) {
    var out = [id];
    this.edges.forEach(function (e) {
      if (e.source === id) out.push(e.target);
      else if (e.target === id) out.push(e.source);
    });
    return out;
  };

  Graph2D.prototype.start = function () {
    if (this.running) return;
    this.running = true;
    var self = this;
    (function loop() {
      if (!self.running) return;
      self.step();
      if (self.autofit) {
        if (self.alpha > 0.28) self.fit();
        else self.autofit = false;
      }
      // Same protection as Graph3D: a throw escaping the loop abandons the
      // frame after clearRect and before the next is scheduled, leaving a
      // blank canvas that never recovers. The force layout can produce NaN
      // coordinates when two nodes land exactly on top of each other, and
      // ctx.arc() throws on a NaN radius.
      try {
        self.draw();
      } catch (err) {
        if (!self._drawFailed) {
          self._drawFailed = true;
          console.error("Graph2D.draw failed; rendering continues", err);
        }
      }
      requestAnimationFrame(loop);
    })();
  };

  Graph2D.prototype.stop = function () { this.running = false; };

  /* ---------- input ---------- */

  Graph2D.prototype._bind = function () {
    var self = this, drag = null, pan = null, moved = 0;

    this.cv.addEventListener("mousedown", function (ev) {
      if (!self.active) return;
      var r = self.cv.getBoundingClientRect();
      var mx = ev.clientX - r.left, my = ev.clientY - r.top;
      var n = self.nodeAt(mx, my);
      moved = 0;
      self.autofit = false;
      if (n) { drag = n; n.pin = true; }
      else {
        self.autofit = false;
      pan = { x: ev.clientX - self.tx, y: ev.clientY - self.ty };
        self.cv.classList.add("drag");
      }
    });

    window.addEventListener("mousemove", function (ev) {
      if (!self.active) return;
      var r = self.cv.getBoundingClientRect();
      var mx = ev.clientX - r.left, my = ev.clientY - r.top;
      if (drag) {
        moved++;
        var p = self.toWorld(mx, my);
        drag.x = p.x; drag.y = p.y;
        self.alpha = Math.max(self.alpha, 0.25);
        return;
      }
      if (pan) {
        moved++;
        self.tx = ev.clientX - pan.x; self.ty = ev.clientY - pan.y;
        self.draw();
        return;
      }
      if (mx < 0 || my < 0 || mx > r.width || my > r.height) return;
      var hn = self.nodeAt(mx, my);
      var he = hn ? null : self.edgeAt(mx, my);
      if (hn !== self.hover || he !== self.hoverEdge) {
        self.hover = hn; self.hoverEdge = he;
        self.cv.classList.toggle("hit", !!(hn || he));
        if (self.opts.onHover) self.opts.onHover(hn, he, mx, my);
        self.draw();
      } else if (hn || he) {
        if (self.opts.onHover) self.opts.onHover(hn, he, mx, my);
      }
    });

    window.addEventListener("mouseup", function () {
      // State is always cleared, even when inactive, so a drag
      // interrupted by a view switch cannot leave it stuck.
      if (drag && moved < 3 && self.opts.onClick) self.opts.onClick(drag);
      else if (pan && moved < 3) {
        if (self.hoverEdge && self.opts.onEdgeClick) self.opts.onEdgeClick(self.hoverEdge);
        else if (self.opts.onBlank) self.opts.onBlank();
      }
      drag = null; pan = null;
      self.cv.classList.remove("drag");
    });

    this.cv.addEventListener("dblclick", function (ev) {
      if (!self.active) return;
      var r = self.cv.getBoundingClientRect();
      var n = self.nodeAt(ev.clientX - r.left, ev.clientY - r.top);
      if (n && self.opts.onDrill) self.opts.onDrill(n);
    });

    this.cv.addEventListener("wheel", function (ev) {
      if (!self.active) return;
      ev.preventDefault();
      self.autofit = false;
      var r = self.cv.getBoundingClientRect();
      var mx = ev.clientX - r.left, my = ev.clientY - r.top;
      var f = ev.deltaY < 0 ? 1.12 : 1 / 1.12;
      var lim = self._scaleLimits();
      var ns = Math.max(lim.min, Math.min(lim.max, self.scale * f));
      if (ns === self.scale) {
        // Already at the limit. Keep easing toward centre anyway when the
        // gesture is "out" -- otherwise recentring stops the moment the floor
        // is reached and the graph is left stranded wherever it happened to
        // be, which is most of the way through a hard zoom-out.
        if (ev.deltaY > 0) { self._recentre(0.12); self.draw(); }
        return;
      }
      // Zoom in toward the cursor; zoom out back toward centre. See
      // Graph2D.prototype._recentre for why the asymmetry matters.
      if (ns > self.scale) {
        self.tx = mx - (mx - self.tx) * (ns / self.scale);
        self.ty = my - (my - self.ty) * (ns / self.scale);
        self.scale = ns;
      } else {
        var shrink = ns / self.scale;
        self.tx = mx - (mx - self.tx) * shrink;
        self.ty = my - (my - self.ty) * shrink;
        self.scale = ns;
        self._recentre(1 - shrink);
      }
      self.draw();
    }, { passive: false });
  };

  global.Graph2D = Graph2D;
  global.cssVar = cssVar;
  global.KIND_COLOR = KIND_COLOR;
  global.shortLabel = shortLabel;
  global.makeSky = makeSky;
  global.liveNow = now;
  global.reducedMotion = REDUCED;
  global.isDarkTheme = isDark;
})(window);
