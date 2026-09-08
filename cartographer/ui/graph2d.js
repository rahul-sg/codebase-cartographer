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
      var ang = (i / total) * Math.PI * 2;
      var rad = 90 + (i % 7) * 42;
      var n = Object.assign({}, d);
      n.x = old ? old.x : W / 2 + Math.cos(ang) * rad;
      n.y = old ? old.y : H / 2 + Math.sin(ang) * rad;
      n.vx = 0; n.vy = 0;
      n.pin = old ? old.pin : false;
      n.deg = 0;
      n.r = self._radius(n);
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
    var rep = Math.max(1400, Math.min(4200, 700 + N.length * 90));

    for (i = 0; i < N.length; i++) {
      a = N[i];
      for (j = i + 1; j < N.length; j++) {
        b = N[j];
        dx = b.x - a.x; dy = b.y - a.y; d2 = dx * dx + dy * dy;
        if (d2 > 260000) continue;
        if (d2 < 1) { d2 = 1; dx = Math.random() - 0.5; dy = Math.random() - 0.5; }
        f = (rep + (a.r + b.r) * 190) / d2;
        d = Math.sqrt(d2); fx = dx / d * f; fy = dy / d * f;
        a.vx -= fx * k; a.vy -= fy * k; b.vx += fx * k; b.vy += fy * k;
      }
    }
    for (i = 0; i < E.length; i++) {
      var e = E[i];
      dx = e.t.x - e.s.x; dy = e.t.y - e.s.y;
      d = Math.hypot(dx, dy) || 1;
      var rest = 112 + e.s.r + e.t.r;
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
      a.vx += (W / 2 - a.x) * 0.0016 * k;
      a.vx *= 0.86; a.vy *= 0.86;
      a.x += a.vx; a.y += a.vy;
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
    var ctx = this.ctx;
    var W = this.cv.clientWidth, H = this.cv.clientHeight;
    ctx.clearRect(0, 0, W, H);
    ctx.save();
    ctx.translate(this.tx, this.ty);
    ctx.scale(this.scale, this.scale);

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
      ctx.globalAlpha = dim ? 0.06 : (e === this.hoverEdge ? 0.95 : 0.42);
      ctx.lineWidth = Math.min(6,
        0.7 + Math.log1p(e.count || 1) * 0.85 + (e.runtime ? 1.4 : 0));
      ctx.setLineDash(e.provenance === "INFERRED" ? [4, 4]
                      : (e.style && e.style.kind === "async" ? [7, 4] : []));
      this._edgePath(ctx, e);
      ctx.stroke();
      if (!dim && this.scale > 0.5) this._arrow(ctx, e, col);
    }
    ctx.setLineDash([]);
    ctx.globalAlpha = 1;

    for (var j = 0; j < this.nodes.length; j++) {
      var n = this.nodes[j];
      var dimN = hl && !hl[n.id];
      var color = cssVar(KIND_COLOR[n.kind] || "--code");
      ctx.beginPath();
      ctx.arc(n.x, n.y, n.r, 0, 6.2832);
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
   * neighbours. Datastores are the worst offender: `mysql://mysqldb/orddev`
   * says far less than `orddev` in a picture where the host is the same
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

  Graph2D.prototype.zoomBy = function (f) {
    this.autofit = false;
    var W = this.cv.clientWidth / 2, H = this.cv.clientHeight / 2;
    var ns = Math.max(0.06, Math.min(8, this.scale * f));
    this.tx = W - (W - this.tx) * (ns / this.scale);
    this.ty = H - (H - this.ty) * (ns / this.scale);
    this.scale = ns; this.draw();
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
      self.draw();
      requestAnimationFrame(loop);
    })();
  };

  Graph2D.prototype.stop = function () { this.running = false; };

  /* ---------- input ---------- */

  Graph2D.prototype._bind = function () {
    var self = this, drag = null, pan = null, moved = 0;

    this.cv.addEventListener("mousedown", function (ev) {
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
      if (drag && moved < 3 && self.opts.onClick) self.opts.onClick(drag);
      else if (pan && moved < 3) {
        if (self.hoverEdge && self.opts.onEdgeClick) self.opts.onEdgeClick(self.hoverEdge);
        else if (self.opts.onBlank) self.opts.onBlank();
      }
      drag = null; pan = null;
      self.cv.classList.remove("drag");
    });

    this.cv.addEventListener("dblclick", function (ev) {
      var r = self.cv.getBoundingClientRect();
      var n = self.nodeAt(ev.clientX - r.left, ev.clientY - r.top);
      if (n && self.opts.onDrill) self.opts.onDrill(n);
    });

    this.cv.addEventListener("wheel", function (ev) {
      ev.preventDefault();
      self.autofit = false;
      var r = self.cv.getBoundingClientRect();
      var mx = ev.clientX - r.left, my = ev.clientY - r.top;
      var f = ev.deltaY < 0 ? 1.12 : 1 / 1.12;
      var ns = Math.max(0.06, Math.min(8, self.scale * f));
      self.tx = mx - (mx - self.tx) * (ns / self.scale);
      self.ty = my - (my - self.ty) * (ns / self.scale);
      self.scale = ns;
      self.draw();
    }, { passive: false });
  };

  global.Graph2D = Graph2D;
  global.cssVar = cssVar;
  global.KIND_COLOR = KIND_COLOR;
  global.shortLabel = shortLabel;
})(window);
