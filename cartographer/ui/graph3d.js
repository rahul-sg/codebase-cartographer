/* 3D layered architecture view.
 *
 * No three.js: a WebGL library would be a ~600KB CDN request that simply fails
 * on a locked-down machine, and the requirement here is narrow enough that a
 * hand-rolled projection is both smaller and easier to reason about.
 *
 * The honest case for 3D: it is good for seeing the SHAPE of the estate --
 * clients above, services in the middle, topics and data below -- and bad for
 * tracing one specific path, because edges occlude each other. So this view
 * deliberately fixes each node to its architectural layer rather than letting
 * a force simulation scatter them in three dimensions. You orbit a building,
 * not a hairball.
 */
(function (global) {
  "use strict";

  var LAYER_NAME = ["clients & legacy", "services", "messaging", "data", "libraries"];
  var LAYER_Y = [-210, -70, 60, 190, 310];

  function Graph3D(canvas, opts) {
    this.cv = canvas;
    this.ctx = canvas.getContext("2d");
    this.opts = opts || {};
    this.nodes = [];
    this.edges = [];
    this.byId = {};
    this.yaw = 0.6; this.pitch = 0.46; this.dist = 1650;
    this.cx = 0; this.cy = 0;
    this.selected = null;
    this.hover = null;
    this.spin = true;
    this.running = false;
    this._bind();
  }

  Graph3D.prototype.setData = function (data) {
    var self = this;
    var byLayer = {};
    (data.nodes || []).forEach(function (n) {
      var L = n.layer == null ? 1 : Math.min(4, n.layer);
      (byLayer[L] = byLayer[L] || []).push(n);
    });

    this.nodes = [];
    Object.keys(byLayer).forEach(function (L) {
      var arr = byLayer[L], n = arr.length;
      // Phyllotaxis: even coverage of a disc without clumping at the centre,
      // which a naive polar grid gives you.
      arr.forEach(function (d, i) {
        var t = (i + 0.5) / n;
        var rad = 360 * Math.sqrt(t);
        var ang = i * 2.39996;
        var node = Object.assign({}, d);
        node.px = Math.cos(ang) * rad;
        node.pz = Math.sin(ang) * rad;
        node.py = LAYER_Y[L] || 0;
        node.L = +L;
        node.r = 5 + Math.min(11, Math.sqrt(d.size || 0) * 1.5)
                   + Math.min(6, (d.rank || 0) * 110);
        self.nodes.push(node);
      });
    });
    this.byId = {};
    this.nodes.forEach(function (n) { self.byId[n.id] = n; });
    this.edges = (data.edges || []).map(function (e) {
      return Object.assign({}, e, { s: self.byId[e.source], t: self.byId[e.target] });
    }).filter(function (e) { return e.s && e.t; });
    this.start();
  };

  Graph3D.prototype.project = function (n) {
    var cy = Math.cos(this.yaw), sy = Math.sin(this.yaw);
    var cp = Math.cos(this.pitch), sp = Math.sin(this.pitch);
    var x = n.px, y = n.py, z = n.pz;
    var x1 = x * cy - z * sy;
    var z1 = x * sy + z * cy;
    var y1 = y * cp - z1 * sp;
    var z2 = y * sp + z1 * cp;
    var depth = z2 + this.dist;
    if (depth < 40) depth = 40;
    var f = 720 / depth;
    return {
      x: this.cv.clientWidth / 2 + x1 * f + this.cx,
      y: this.cv.clientHeight / 2 + y1 * f + this.cy,
      s: f, depth: depth
    };
  };

  Graph3D.prototype.resize = function () {
    var dpr = Math.min(window.devicePixelRatio || 1, 2);
    var r = this.cv.getBoundingClientRect();
    this.cv.width = Math.max(1, r.width * dpr);
    this.cv.height = Math.max(1, r.height * dpr);
    this.ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  };

  Graph3D.prototype.draw = function () {
    var ctx = this.ctx, self = this;
    var W = this.cv.clientWidth, H = this.cv.clientHeight;
    ctx.clearRect(0, 0, W, H);

    this.nodes.forEach(function (n) { n._p = self.project(n); });

    // Layer plates first, back to front, so the stack reads as a building.
    var plates = [0, 1, 2, 3, 4].map(function (L) {
      var probe = { px: 0, py: LAYER_Y[L], pz: 0 };
      return { L: L, p: self.project(probe) };
    }).sort(function (a, b) { return b.p.depth - a.p.depth; });

    plates.forEach(function (pl) {
      var has = self.nodes.some(function (n) { return n.L === pl.L; });
      if (!has) return;
      var f = pl.p.s;
      ctx.save();
      ctx.globalAlpha = 0.055;
      ctx.fillStyle = cssVar("--fg");
      ctx.beginPath();
      ctx.ellipse(pl.p.x, pl.p.y, 400 * f, 400 * f * Math.sin(self.pitch + 0.001),
                  0, 0, 6.2832);
      ctx.fill();
      ctx.globalAlpha = 0.5;
      ctx.fillStyle = cssVar("--muted");
      ctx.font = "11px ui-sans-serif";
      ctx.textAlign = "left";
      ctx.fillText(LAYER_NAME[pl.L], pl.p.x + 400 * f * 0.78, pl.p.y);
      ctx.restore();
    });

    // Edges, far to near.
    var es = this.edges.slice().sort(function (a, b) {
      return (b.s._p.depth + b.t._p.depth) - (a.s._p.depth + a.t._p.depth);
    });
    es.forEach(function (e) {
      if (self.opts.edgeVisible && !self.opts.edgeVisible(e)) return;
      var dim = self.selected &&
        e.source !== self.selected && e.target !== self.selected;
      ctx.strokeStyle = cssVar("--" + ((e.style && e.style.kind) || "code"));
      ctx.globalAlpha = dim ? 0.05 : 0.34;
      ctx.lineWidth = Math.min(4, 0.6 + Math.log1p(e.count || 1) * 0.6);
      ctx.setLineDash(e.provenance === "INFERRED" ? [4, 4] : []);
      // Bow the line so same-layer edges are not hidden inside the plate.
      var mx = (e.s._p.x + e.t._p.x) / 2;
      var my = (e.s._p.y + e.t._p.y) / 2 - (e.s.L === e.t.L ? 26 : 0);
      ctx.beginPath();
      ctx.moveTo(e.s._p.x, e.s._p.y);
      ctx.quadraticCurveTo(mx, my, e.t._p.x, e.t._p.y);
      ctx.stroke();
    });
    ctx.setLineDash([]);

    // Nodes, far to near.
    var ns = this.nodes.slice().sort(function (a, b) {
      return b._p.depth - a._p.depth;
    });
    ns.forEach(function (n) {
      var p = n._p, rr = Math.max(2, n.r * p.s);
      var dim = self.selected && self.selected !== n.id &&
        !self.edges.some(function (e) {
          return (e.source === self.selected && e.target === n.id) ||
                 (e.target === self.selected && e.source === n.id);
        });
      var col = cssVar(KIND_COLOR[n.kind] || "--code");
      ctx.globalAlpha = dim ? 0.12 : Math.max(0.35, Math.min(1, 1400 / p.depth));
      ctx.beginPath();
      ctx.arc(p.x, p.y, rr, 0, 6.2832);
      ctx.fillStyle = col;
      ctx.fill();
      if (self.selected === n.id) {
        ctx.strokeStyle = cssVar("--sel");
        ctx.lineWidth = 2.5;
        ctx.globalAlpha = 1;
        ctx.beginPath();
        ctx.arc(p.x, p.y, rr + 5, 0, 6.2832);
        ctx.stroke();
      }
      var named = n.kind === 'service' || n.kind === 'library' ||
                  n.kind === 'topic' || n.kind === 'datastore';
      if (!dim && (rr > 5.5 || named || self.selected === n.id ||
                   self.hover === n)) {
        ctx.globalAlpha = Math.max(0.5, Math.min(1, 1100 / p.depth));
        ctx.fillStyle = cssVar("--fg");
        ctx.font = Math.max(9, Math.min(13, rr * 0.95)) + "px ui-sans-serif";
        ctx.textAlign = "center";
        ctx.fillText(shortLabel(n), p.x, p.y - rr - 5);
      }
    });
    ctx.globalAlpha = 1;
  };

  Graph3D.prototype.nodeAt = function (mx, my) {
    var best = null, bd = 1e9;
    for (var i = 0; i < this.nodes.length; i++) {
      var n = this.nodes[i];
      if (!n._p) continue;
      var rr = Math.max(3, n.r * n._p.s);
      var d = Math.hypot(n._p.x - mx, n._p.y - my);
      if (d < rr + 5 && d < bd) { bd = d; best = n; }
    }
    return best;
  };

  Graph3D.prototype.start = function () {
    if (this.running) return;
    this.running = true;
    var self = this;
    (function loop() {
      if (!self.running) return;
      if (self.spin) self.yaw += 0.0016;
      self.draw();
      requestAnimationFrame(loop);
    })();
  };

  Graph3D.prototype.stop = function () { this.running = false; };

  Graph3D.prototype._bind = function () {
    var self = this, drag = null, moved = 0;

    this.cv.addEventListener("mousedown", function (ev) {
      var r = self.cv.getBoundingClientRect();
      drag = { x: ev.clientX, y: ev.clientY, yaw: self.yaw, pitch: self.pitch,
               shift: ev.shiftKey, cx: self.cx, cy: self.cy,
               mx: ev.clientX - r.left, my: ev.clientY - r.top };
      moved = 0;
      self.spin = false;
      self.cv.classList.add("drag");
    });

    window.addEventListener("mousemove", function (ev) {
      var r = self.cv.getBoundingClientRect();
      var mx = ev.clientX - r.left, my = ev.clientY - r.top;
      if (drag) {
        moved++;
        if (drag.shift) {
          self.cx = drag.cx + (ev.clientX - drag.x);
          self.cy = drag.cy + (ev.clientY - drag.y);
        } else {
          self.yaw = drag.yaw + (ev.clientX - drag.x) * 0.006;
          self.pitch = Math.max(-0.2, Math.min(1.25,
            drag.pitch + (ev.clientY - drag.y) * 0.004));
        }
        return;
      }
      if (mx < 0 || my < 0 || mx > r.width || my > r.height) return;
      var h = self.nodeAt(mx, my);
      if (h !== self.hover) {
        self.hover = h;
        self.cv.classList.toggle("hit", !!h);
      }
      if (self.opts.onHover) self.opts.onHover(h, null, mx, my);
    });

    window.addEventListener("mouseup", function () {
      if (drag && moved < 3) {
        var n = self.nodeAt(drag.mx, drag.my);
        if (n && self.opts.onClick) self.opts.onClick(n);
        else if (!n && self.opts.onBlank) self.opts.onBlank();
      }
      drag = null;
      self.cv.classList.remove("drag");
    });

    this.cv.addEventListener("wheel", function (ev) {
      ev.preventDefault();
      self.dist = Math.max(320, Math.min(4200,
        self.dist * (ev.deltaY > 0 ? 1.1 : 1 / 1.1)));
    }, { passive: false });

    this.cv.addEventListener("dblclick", function (ev) {
      var r = self.cv.getBoundingClientRect();
      var n = self.nodeAt(ev.clientX - r.left, ev.clientY - r.top);
      if (n && self.opts.onDrill) self.opts.onDrill(n);
    });
  };

  global.Graph3D = Graph3D;
})(window);
