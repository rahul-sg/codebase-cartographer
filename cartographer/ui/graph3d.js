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
  // Floor heights are recomputed per dataset in setData. The defaults matter
  // only until the first load.
  //
  // They used to be these constants outright, with a fixed 140-unit gap. That
  // was survivable while every disc had radius 360, and became wrong the
  // moment discs were sized by population: a 950-radius floor projects a band
  // several hundred pixels tall, so a 140-unit gap put three floors on top of
  // one another. The stack has to grow vertically when it grows horizontally,
  // or "wider" just means "more overlap".
  var LAYER_Y = [-210, -70, 60, 190, 310];

  // Near plane, and the zoom range that respects it.
  //
  // The scene is a stack of discs ~360 wide spanning y -210..310, so its half
  // extent is roughly 470 world units. Any camera distance below that puts
  // part of the cloud BEHIND the near plane, where the perspective divide
  // explodes: depth was floored at 40, giving a 18x scale factor and single
  // nodes 288px across covering a fifth of the viewport and occluding
  // everything. That floor was a divide-by-zero guard being used as a near
  // plane, which is not what it is.
  //
  // Anything closer than NEAR is now culled outright, the way a real renderer
  // does it, and faded over FADE units so it dissolves rather than pops.
  // Floor footprint. Nodes and the plate under them MUST derive from the same
  // two numbers, or the ring stops bounding the thing it is drawn for.
  var STRETCH_X = 1.55, STRETCH_Z = 0.82;

  var NEAR = 260;
  var FADE = 170;
  var MIN_DIST = 430, MAX_DIST = 4200;
  var MAX_R = 72;      // no single node may swallow the view

  function Graph3D(canvas, opts) {
    this.cv = canvas;
    this.ctx = canvas.getContext("2d");
    this.opts = opts || {};
    this.nodes = [];
    this.edges = [];
    this.byId = {};
    // 0.26, chosen by sweeping pitch against floor spacing rather than by
    // eye. Separation and band height pull in opposite directions as pitch
    // rises, so a flatter default is what makes a SHORTER stack readable:
    // measured, this gives a floor gap 0.94x the band height (clearly
    // separated) while using the most canvas width of any combination tried.
    // Tilt up to look into a floor -- this is only where the camera starts.
    this.yaw = 0.6; this.pitch = 0.26; this.dist = 1650;
    this.cx = 0; this.cy = 0;
    // World-space height the camera orbits around. Previously the pivot was
    // hardcoded to the origin, which sits between layers 1 and 2 -- so once
    // you zoomed in, orbiting swung the top layer (clients, y=-210) and the
    // bottom layer (libraries, y=310) out of frame with no way to reach them.
    // Moving the pivot is what makes every layer navigable.
    this.ty = 0;
    this.hiddenLayers = {};   // L -> true, for isolating one floor at a time
    // See the note in graph2d: one shared canvas, both engines bound to it.
    this.active = false;
    this.selected = null;
    this.hover = null;
    this.spin = true;
    this.running = false;
    this._bind();
  }

  Graph3D.prototype.setData = function (data, keepView) {
    // `keepView` matters more here than in 2D: without it, ticking any filter
    // checkbox snapped the camera back to its default angle and restarted the
    // spin, so adjusting the view fought you. Graph2D has always taken this
    // argument; Graph3D silently ignored it.
    var self = this;
    var cam = keepView ? {yaw: this.yaw, pitch: this.pitch, dist: this.dist,
                          cx: this.cx, cy: this.cy, spin: this.spin} : null;
    var byLayer = {};
    (data.nodes || []).forEach(function (n) {
      var L = n.layer == null ? 1 : Math.min(4, n.layer);
      (byLayer[L] = byLayer[L] || []).push(n);
    });

    this.nodes = [];
    // Each floor gets a disc sized for what is ON it. The radius used to be a
    // constant 360 for every layer, so a 37-node floor and a 565-node floor
    // were given identical space: the sparse ones looked airy at ~104 units
    // between nodes while the dense ones were packed at ~27, which reads as
    // an unreadable smear and has nothing to do with how many connections
    // exist. Scaling by sqrt(count) holds the AREA PER NODE roughly constant,
    // which is the thing the eye actually responds to.
    this.layerR = {};
    var maxR = 360;
    Object.keys(byLayer).forEach(function (L) {
      // 30*sqrt(n) rather than 40: the wider a floor gets, the taller the
      // whole stack has to become to keep floors apart, and past a point
      // that costs more legibility than the extra spread buys.
      self.layerR[L] = Math.max(360, 30 * Math.sqrt(byLayer[L].length));
      if (self.layerR[L] > maxR) maxR = self.layerR[L];
    });
    // Gap proportional to the widest floor, so the stack stays readable as a
    // stack. Clamped: below ~200 floors merge, above ~900 the estate becomes
    // a tower you have to scroll rather than a building you can see.
    // Shorter than it was. Floor separation depends on BOTH the gap and how
    // edge-on the discs are: band height goes with sin(pitch), the projected
    // gap with cos(pitch). Flattening the default camera (0.40 -> 0.32) cuts
    // band height ~20% and widens the projected gap, which buys back the room
    // to bring the floors ~30% closer without them merging again.
    var gap = Math.max(170, Math.min(620, maxR * 0.62));
    LAYER_Y = [-2, -1, 0, 1, 2].map(function (i) { return i * gap; });
    // Re-publish: the export below is bound at load time, and the app reads
    // it to step the camera between floors. A stale copy would aim the camera
    // at heights nothing occupies.
    global.GRAPH3D_LAYER_Y = LAYER_Y;
    Object.keys(byLayer).forEach(function (L) {
      var arr = byLayer[L], n = arr.length;
      var R = self.layerR[L];
      // Phyllotaxis: even coverage of a disc without clumping at the centre,
      // which a naive polar grid gives you.
      arr.forEach(function (d, i) {
        var t = (i + 0.5) / n;
        var rad = R * Math.sqrt(t);
        var ang = i * 2.39996;
        var node = Object.assign({}, d);
        // Elliptical floors, not circular.
        //
        // A circular disc stacked vertically produces a scene far taller than
        // it is wide -- measured, 30% of the canvas width against 91% of its
        // height, so the estate was a narrow column in a widescreen frame and
        // read as "too tall". Stretching each floor along x and easing it in
        // z spends that unused width while keeping area (and therefore
        // crowding) essentially unchanged. Screens are 16:9; floor plans may
        // as well be.
        node.px = Math.cos(ang) * rad * STRETCH_X;
        node.pz = Math.sin(ang) * rad * STRETCH_Z;
        node.py = LAYER_Y[L] || 0;
        node.L = +L;
        node.r = 5 + Math.min(11, Math.sqrt(d.size || 0) * 1.5)
                   + Math.min(6, (d.rank || 0) * 110);
        node._ph = (i * 2.399) % 6.283;
        self.nodes.push(node);
      });
    });
    this.byId = {};
    this.nodes.forEach(function (n) { self.byId[n.id] = n; });
    this.edges = (data.edges || []).map(function (e) {
      return Object.assign({}, e, { s: self.byId[e.source], t: self.byId[e.target] });
    }).filter(function (e) { return e.s && e.t; });
    if (cam) {
      this.yaw = cam.yaw; this.pitch = cam.pitch; this.dist = cam.dist;
      this.cx = cam.cx; this.cy = cam.cy; this.spin = cam.spin;
    }
    this.start();
  };

  Graph3D.prototype.project = function (n) {
    var cy = Math.cos(this.yaw), sy = Math.sin(this.yaw);
    var cp = Math.cos(this.pitch), sp = Math.sin(this.pitch);
    var x = n.px, y = n.py - this.ty, z = n.pz;
    var x1 = x * cy - z * sy;
    var z1 = x * sy + z * cy;
    var y1 = y * cp - z1 * sp;
    var z2 = y * sp + z1 * cp;
    var depth = z2 + this.dist;
    // Guard the division only. Culling against NEAR is the drawing code's
    // job -- flooring here instead both magnified near nodes absurdly and
    // moved them to positions they are not actually at.
    if (depth < 1) depth = 1;
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
    if (!this.active) return;
    var ctx = this.ctx, self = this;
    var W = this.cv.clientWidth, H = this.cv.clientHeight;
    ctx.clearRect(0, 0, W, H);

    this._sky(ctx, W, H);

    this.nodes.forEach(function (n) { n._p = self.project(n); });

    // Layer plates first, back to front, so the stack reads as a building.
    var captions = [];
    var plates = [0, 1, 2, 3, 4].map(function (L) {
      var probe = { px: 0, py: LAYER_Y[L], pz: 0 };
      return { L: L, p: self.project(probe) };
    }).sort(function (a, b) { return b.p.depth - a.p.depth; });

    plates.forEach(function (pl) {
      if (!self.layerVisible(pl.L)) return;
      if (pl.p.depth < NEAR) return;
      var has = self.nodes.some(function (n) { return n.L === pl.L; });
      if (!has) return;
      var f = pl.p.s;
      // Canvas throws IndexSizeError on a negative radius and TypeError on a
      // non-finite one, and a throw here aborts the whole frame -- after
      // clearRect has already run and before requestAnimationFrame is
      // rescheduled, so the canvas goes blank permanently. Clamp rather than
      // trust the camera.
      // Plate follows the layer's own disc, not a constant, or a widened
      // floor spills past its own ring.
      var PR = (self.layerR && self.layerR[pl.L] ? self.layerR[pl.L] : 360) * 1.11;

      // Project the boundary; do not fake it with a screen-space ellipse.
      //
      // The plate used to be one `ctx.ellipse` sized from the centre probe.
      // That was already only approximately right -- it applied a single
      // depth to the whole ring, so the near edge was not larger than the far
      // one -- and it became visibly wrong the moment floors stopped being
      // circular: an x-stretched, z-squashed footprint ROTATES with yaw into
      // a tilted ellipse, which an axis-aligned screen ellipse cannot
      // represent at all. Hence rings that sat beside their own nodes.
      //
      // Sampling the same parametric curve the nodes are placed on and
      // projecting each point is exact by construction, and costs 72 points
      // per visible floor.
      var STEPS = 72, rim = [], bad = false, maxX = -1e9, atY = pl.p.y;
      for (var si = 0; si <= STEPS; si++) {
        var th = si / STEPS * 6.2832;
        var pp = self.project({ px: Math.cos(th) * PR * STRETCH_X,
                                py: LAYER_Y[pl.L],
                                pz: Math.sin(th) * PR * STRETCH_Z });
        if (pp.depth < NEAR || !isFinite(pp.x) || !isFinite(pp.y)) { bad = true; break; }
        rim.push(pp);
        if (pp.x > maxX) { maxX = pp.x; atY = pp.y; }
      }
      if (bad || rim.length < 3) return;

      ctx.save();
      ctx.globalAlpha = 0.05;
      ctx.fillStyle = cssVar("--fg");
      ctx.beginPath();
      ctx.moveTo(rim[0].x, rim[0].y);
      for (var ri = 1; ri < rim.length; ri++) ctx.lineTo(rim[ri].x, rim[ri].y);
      ctx.closePath();
      ctx.fill();
      // Dashed rim: reads as an orbital ring rather than a solid plate, which
      // suits a stack of layers suspended in space and keeps the outline from
      // competing with the edges crossing it.
      ctx.globalAlpha = 0.30;
      ctx.strokeStyle = cssVar("--accent");
      ctx.lineWidth = 1;
      ctx.setLineDash([2, 7]);
      ctx.stroke();              // same projected path as the fill
      ctx.setLineDash([]);
      ctx.restore();
      // Caption position only. Drawn in a later pass: plates are painted
      // first, so anything written here is buried by every edge, node and
      // label that follows -- which is why the layer names were unreadable.
      captions.push({ L: pl.L, x: maxX + 10, y: atY });
    });

    // Edges, far to near.
    var es = this.edges.slice().sort(function (a, b) {
      return (b.s._p.depth + b.t._p.depth) - (a.s._p.depth + a.t._p.depth);
    });
    // Additive blending: overlapping connections accumulate light instead of
    // painting over each other, so density becomes visible as brightness and
    // the bundles read as fibre rather than as scribble. Alpha is lowered to
    // compensate, otherwise dense regions clip to solid white.
    var lit = this._dark();
    if (lit) ctx.globalCompositeOperation = "lighter";
    var eA = lit ? 0.20 : 0.34, eDim = lit ? 0.07 : 0.16;

    es.forEach(function (e) {
      if (self.opts.edgeVisible && !self.opts.edgeVisible(e)) return;
      if (!self.layerVisible(e.s.L) || !self.layerVisible(e.t.L)) return;
      // An edge with an endpoint inside the near plane draws as a wild streak
      // across the whole canvas -- the diagonal slashes in the zoomed-in view.
      if (e.s._p.depth < NEAR || e.t._p.depth < NEAR) return;
      var dim = self.selected &&
        e.source !== self.selected && e.target !== self.selected;
      ctx.strokeStyle = cssVar("--" + ((e.style && e.style.kind) || "code"));
      // 0.05 was effectively invisible on a dark background: selecting one
      // node at estate scale dimmed ~99% of the scene and read as "the graph
      // disappeared". Kept low enough to recede, high enough to stay context.
      ctx.globalAlpha = dim ? eDim
        : (e._cross ? Math.min(0.95, eA * 2.6)
                    : (e._intra ? eA * 0.5 : eA));
      ctx.lineWidth = Math.min(4, 0.6 + Math.log1p(e.count || 1) * 0.6);
      // Flow, on the kinds that carry traffic. INFERRED keeps its own dash
      // pattern: provenance outranks decoration, because a dashed line here
      // means "not proven" and that must not be overwritten by an effect.
      var fkind = (e.style && e.style.kind) || "code";
      if (e.provenance === "INFERRED") {
        ctx.setLineDash([4, 4]);
        ctx.lineDashOffset = 0;
      } else if (!dim && T &&
                 (fkind === "sync" || fkind === "async" || fkind === "data")) {
        ctx.setLineDash([7, 9]);
        ctx.lineDashOffset = -(T * (14 + Math.min(46,
                              Math.log1p(e.count || 1) * 18))) % 16;
      } else {
        ctx.setLineDash([]);
        ctx.lineDashOffset = 0;
      }
      // Bow the line so same-layer edges are not hidden inside the plate.
      var mx = (e.s._p.x + e.t._p.x) / 2;
      var my = (e.s._p.y + e.t._p.y) / 2 - (e.s.L === e.t.L ? 26 : 0);
      if (!isFinite(mx) || !isFinite(my) || !isFinite(e.s._p.x) ||
          !isFinite(e.s._p.y) || !isFinite(e.t._p.x) || !isFinite(e.t._p.y)) return;
      ctx.beginPath();
      ctx.moveTo(e.s._p.x, e.s._p.y);
      ctx.quadraticCurveTo(mx, my, e.t._p.x, e.t._p.y);
      ctx.stroke();
    });
    ctx.setLineDash([]);
    ctx.globalCompositeOperation = "source-over";

    // Nodes, far to near.
    var ns = this.nodes.slice().sort(function (a, b) {
      return b._p.depth - a._p.depth;
    });
    var adj = this._adjacency();
    var T = (typeof liveNow === "function") ? liveNow() : 0;
    var labels = [];
    ns.forEach(function (n) {
      if (!self.layerVisible(n.L)) return;
      var p = n._p;
      if (p.depth < NEAR) return;                       // in front of the camera
      var rr = Math.min(MAX_R, Math.max(2, n.r * p.s));
      // One node with a bad projection must not abort the whole frame.
      if (!isFinite(p.x) || !isFinite(p.y) || !isFinite(rr)) return;
      // Dissolve across the near plane instead of popping out of existence.
      var near = Math.max(0, Math.min(1, (p.depth - NEAR) / FADE));
      var dim = adj && !adj[n.id];
      // `_repoColor` is set by the app layer when "colour by repository" is
      // on. graph2d honoured it and this did not, so the toggle appeared to do
      // nothing in the Layers tab.
      var col = n._repoColor || cssVar(KIND_COLOR[n.kind] || "--code");
      var baseA = near *
        (dim ? 0.28 : Math.max(0.35, Math.min(1, 1400 / p.depth)));
      ctx.globalAlpha = baseA;

      // Focus glow, for the two nodes that matter at any moment. shadowBlur is
      // expensive, so it is never applied to the other twelve hundred.
      var focused = (self.selected === n.id || self.hover === n);
      if (focused) {
        ctx.shadowColor = cssVar("--sel");
        ctx.shadowBlur = 22;
      } else if (rr > 7 && !dim && lit) {
        // Cheap bloom: one wide, very faint disc under the node. A real blur
        // pass or a per-node gradient would cost far more than this is worth,
        // and at these alphas the difference is not visible -- but the halo
        // is what stops the bigger services looking like flat stickers.
        ctx.globalAlpha = baseA * 0.10;
        ctx.beginPath();
        ctx.arc(p.x, p.y, rr * 2.1, 0, 6.2832);
        ctx.fillStyle = col;
        ctx.fill();
        ctx.globalAlpha = baseA;
      }
      // Same pulse as the Map: rate and amplitude from centrality, phase
      // fixed per node so the estate breathes rather than throbs in unison.
      var pr = T
        ? Math.sin(T * (0.7 + Math.min(1.9, (n.rank || 0) * 150)) + (n._ph || 0)) *
          0.06 * Math.min(1, 0.35 + (n.rank || 0) * 90)
        : 0;
      var rrp = Math.max(1, rr * (1 + pr));
      ctx.beginPath();
      ctx.arc(p.x, p.y, rrp, 0, 6.2832);
      ctx.fillStyle = col;
      ctx.fill();
      if (focused) { ctx.shadowBlur = 0; ctx.shadowColor = "transparent"; }

      // Rim light: a hairline edge separates overlapping discs, which at this
      // density is the difference between reading depth and seeing a blob.
      if (rr > 3.5 && !dim) {
        ctx.globalAlpha = baseA * 0.5;
        ctx.strokeStyle = self._dark() ? "#ffffff" : "#000000";
        ctx.lineWidth = 1;
        ctx.stroke();
        ctx.globalAlpha = baseA;
      }
      // A single offset highlight arc reads as a lit sphere rather than a
      // flat disc. Only on the larger nodes: below ~8px it is invisible, and
      // a per-node radial gradient at 1219 nodes would cost real frames.
      if (rr > 8 && !dim) {
        ctx.globalAlpha *= 0.22;
        ctx.beginPath();
        ctx.arc(p.x - rr * 0.28, p.y - rr * 0.3, rr * 0.62, 0, 6.2832);
        ctx.fillStyle = "#ffffff";
        ctx.fill();
        ctx.globalAlpha = near *
          (dim ? 0.28 : Math.max(0.35, Math.min(1, 1400 / p.depth)));
      }
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
        // Collected, not drawn: labels are laid out in a second pass so the
        // nearest and largest win the contested space, and so every label
        // sits above every node instead of being overpainted by whatever
        // was drawn after it.
        labels.push({ n: n, p: p, rr: rr, named: named });
      }
    });

    self._drawLabels(ctx, labels);
    self._drawCaptions(ctx, captions, W, H);
    ctx.globalAlpha = 1;
  };

  /**
   * Layer captions, drawn last and kept on screen.
   *
   * These are the one piece of permanent orientation in the view -- they say
   * which floor you are looking at -- so they must survive the density rather
   * than be buried by it. Two problems, both visible at estate scale: they
   * were painted with the plates, before every edge and node, so anything
   * dense wrote straight over them; and they were anchored to the plate rim,
   * which at most camera angles is off-canvas, clipping "MESSAGING" to
   * "SSAGING".
   *
   * Now: drawn after everything, clamped inside the viewport, and set on a
   * dark plate so they read against whatever they happen to overlap.
   */
  Graph3D.prototype._drawCaptions = function (ctx, captions, W, H) {
    if (!captions.length) return;
    ctx.save();
    ctx.font = "700 10.5px ui-sans-serif";
    ctx.textAlign = "left";
    ctx.textBaseline = "middle";
    for (var i = 0; i < captions.length; i++) {
      var c = captions[i];
      var txt = LAYER_NAME[c.L].toUpperCase();
      if (!isFinite(c.x) || !isFinite(c.y)) continue;
      var tw = ctx.measureText(txt).width;
      // Keep the caption fully on screen: the rim it is anchored to is often
      // outside the canvas, and a half-visible word is worse than a moved one.
      var x = Math.max(10, Math.min(W - tw - 12, c.x));
      var y = Math.max(14, Math.min(H - 12, c.y));
      ctx.globalAlpha = 0.82;
      ctx.fillStyle = cssVar("--bg");
      var padX = 6, padY = 4;
      if (ctx.roundRect) {
        ctx.beginPath();
        ctx.roundRect(x - padX, y - 8 - padY + 2, tw + padX * 2, 16 + padY, 5);
        ctx.fill();
      } else {
        ctx.fillRect(x - padX, y - 8 - padY + 2, tw + padX * 2, 16 + padY);
      }
      ctx.globalAlpha = 0.95;
      ctx.fillStyle = cssVar("--accent");
      ctx.fillText(txt, x, y);
    }
    ctx.restore();
  };

  /**
   * Label pass with collision culling.
   *
   * At estate scale several hundred labels compete for the same band of
   * pixels and overlap into an unreadable smear -- the middle of the stack
   * turned into a solid block of white. Graph2D has always culled labels on a
   * grid; the 3D view drew every one unconditionally.
   *
   * Priority is apparent size (`rr`), which in a perspective view means the
   * nearest and most important node wins the space, with the selection and
   * hover always kept. A halo in the background colour keeps the survivors
   * legible where they cross edges.
   */
  Graph3D.prototype._drawLabels = function (ctx, labels) {
    var self = this;
    var cells = {}, CW = 46, CH = 15;
    // Budget per floor. A layer seen near edge-on projects to something close
    // to a horizontal line, so grid-based collision alone still permits a
    // solid rank of text across the screen -- which is what made the 497-node
    // messaging floor read as a wall rather than as nodes. Capping per layer
    // keeps the biggest few labelled and lets the rest speak through position.
    // Label budget rises as you close in.
    //
    // A fixed budget is wrong at both ends: ten labels is right for the whole
    // estate and absurdly stingy once you have flown down to one floor, where
    // the labels are the entire reason you went there. Scale with how far
    // inside the fit distance the camera is, so zooming in progressively
    // reveals names rather than requiring a click each time.
    var lim = this._zoomLimits();
    var zoomK = Math.max(1, (lim.fit || this.dist) / Math.max(1, this.dist));
    var perLayer = {}, BUDGET = Math.round(Math.max(10,
                                Math.min(60, 10 * Math.pow(zoomK, 1.6))));
    labels.sort(function (a, b) {
      var pa = (self.selected === a.n.id || self.hover === a.n) ? 1e6 : a.rr;
      var pb = (self.selected === b.n.id || self.hover === b.n) ? 1e6 : b.rr;
      return pb - pa;
    });
    var halo = cssVar("--bg");
    ctx.textAlign = "center";
    ctx.lineJoin = "round";
    for (var i = 0; i < labels.length; i++) {
      var L = labels[i], p = L.p;
      var forced = (self.selected === L.n.id || self.hover === L.n);
      var x = p.x, y = p.y - L.rr - 5;
      if (!isFinite(x) || !isFinite(y)) continue;
      var key = Math.round(x / CW) + "," + Math.round(y / CH);
      if (!forced && cells[key]) continue;
      var lay = L.n.L;
      if (!forced) {
        if ((perLayer[lay] || 0) >= BUDGET) continue;
        perLayer[lay] = (perLayer[lay] || 0) + 1;
      }
      cells[key] = 1;
      ctx.globalAlpha = forced ? 1 : Math.max(0.5, Math.min(1, 1100 / p.depth));
      ctx.font = Math.max(9, Math.min(13, L.rr * 0.95)) + "px ui-sans-serif";
      // Halo first, then the glyph: cheaper and more legible than a shadow,
      // and it keeps text readable over the densest edge bundles.
      ctx.strokeStyle = halo;
      ctx.lineWidth = 3;
      ctx.strokeText(shortLabel(L.n), x, y);
      ctx.fillStyle = cssVar(forced ? "--sel" : "--fg");
      ctx.fillText(shortLabel(L.n), x, y);
    }
  };

  /**
   * `{id: true}` for the selection plus everything one hop from it, or null
   * when nothing is selected.
   *
   * Built once per selection change rather than per node per frame. The
   * previous form called `edges.some()` inside the node loop, which at estate
   * scale (1219 nodes x 2815 edges) is ~3.4M comparisons *every frame* -- so
   * selecting anything collapsed the framerate and the view appeared to freeze
   * while it was also being dimmed. Keyed on the edge array identity too, so
   * `setData` invalidates it without needing to remember to.
   */
  Graph3D.prototype._adjacency = function () {
    if (!this.selected) { this._adjFor = null; this._adj = null; return null; }
    if (this._adjFor === this.selected && this._adjEdges === this.edges) {
      return this._adj;
    }
    var m = {};
    m[this.selected] = true;
    for (var i = 0; i < this.edges.length; i++) {
      var e = this.edges[i];
      if (e.source === this.selected) m[e.target] = true;
      else if (e.target === this.selected) m[e.source] = true;
    }
    this._adjFor = this.selected;
    this._adjEdges = this.edges;
    this._adj = m;
    return m;
  };

  Graph3D.prototype.nodeAt = function (mx, my) {
    var best = null, bd = 1e9;
    for (var i = 0; i < this.nodes.length; i++) {
      var n = this.nodes[i];
      // A hidden layer must not be clickable, or you select things you
      // cannot see -- the exact confusion the blank-canvas bug caused.
      if (!n._p || !this.layerVisible(n.L)) continue;
      if (n._p.depth < NEAR) continue;      // culled from the picture, so not clickable
      var rr = Math.min(MAX_R, Math.max(3, n.r * n._p.s));
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
      // A throw inside draw() used to escape here, which meant the frame was
      // abandoned AFTER clearRect and BEFORE the next frame was scheduled --
      // so one bad camera angle blanked the canvas permanently while nodes
      // stayed clickable at their stale projected positions. Rendering now
      // survives a bad frame; the error is reported once so it still gets
      // fixed rather than silently swallowed.
      try {
        self.draw();
      } catch (err) {
        if (!self._drawFailed) {
          self._drawFailed = true;
          console.error("Graph3D.draw failed; rendering continues", err);
        }
      }
      requestAnimationFrame(loop);
    })();
  };

  Graph3D.prototype.stop = function () { this.running = false; };

  /**
   * Clamped zoom for the +/- buttons.
   *
   * They previously did `g3.dist /= 1.2` inline with no bound at all, so
   * holding zoom-in walked the camera to a distance of nearly zero. Same
   * argument convention as Graph2D.zoomBy: >1 moves closer.
   */
  Graph3D.prototype.zoomBy = function (factor) {
    var lim = this._zoomLimits();
    this.dist = Math.max(lim.min, Math.min(lim.max, this.dist / factor));
    this.spin = false;
    this._clampPan();
  };

  /**
   * Is the current theme dark?
   *
   * Additive edge blending looks like a lit fibre-optic bundle on a dark
   * background and like washed-out fog on a light one, so the effect has to
   * be conditional. Cached per theme value rather than parsed every frame.
   */
  /**
   * Screen-space bounding box of everything currently drawable, or null when
   * nothing is. Projects on demand so it is valid outside a draw pass.
   */
  /**
   * Where a zoom gesture at (mx,my) should actually converge.
   *
   * Over a node: that node, which is the whole point of pointer zoom.
   * Near the graph: the nearest visible node, so aiming at the gap between
   * two nodes still does the intuitive thing.
   * Far from everything: the centre of the content, because zooming "into"
   * empty space has no meaning and the old behaviour was to fly away from
   * the graph entirely.
   */
  Graph3D.prototype._zoomAnchor = function (mx, my) {
    var best = null, bd = 1e9;
    for (var i = 0; i < this.nodes.length; i++) {
      var n = this.nodes[i];
      if (!this.layerVisible(n.L)) continue;
      var p = n._p || this.project(n);
      if (p.depth < NEAR || !isFinite(p.x) || !isFinite(p.y)) continue;
      var d = Math.hypot(p.x - mx, p.y - my);
      if (d < bd) { bd = d; best = p; }
    }
    // Comfortably larger than a node so the gaps inside a cluster still count
    // as "on the graph", small enough that open background does not.
    if (best && bd < 140) return { x: best.x, y: best.y, depth: best.depth };
    var b = this._contentBox();
    if (!b) return { x: mx, y: my, depth: this.dist };
    return { x: (b.minX + b.maxX) / 2, y: (b.minY + b.maxY) / 2,
             depth: this.dist };
  };

  /**
   * Largest distance from the pivot to a visible node, in world units.
   * Cached per (visible set, pivot) because it only changes when a layer is
   * toggled or the camera is re-aimed, not per frame.
   */
  /**
   * Deep field behind the graph. The implementation is shared with the Map
   * view and lives in graph2d.js, which loads first -- same arrangement as
   * cssVar/KIND_COLOR/shortLabel. Duplicating ~100 lines of starfield in two
   * engines would guarantee they drift apart.
   */
  Graph3D.prototype._sky = function (ctx, W, H) {
    if (!this._skyField) this._skyField = makeSky();
    // Parallax from the camera: yaw wraps, so the field scrolls seamlessly
    // all the way round an orbit.
    this._skyField.draw(ctx, W, H, this._dark(),
                        this.yaw * 0.055 + this.cx / 9000,
                        this.pitch * 0.10 + this.cy / 9000);
  };

  Graph3D.prototype._pivotHome = function () {
    var ys = [], self = this;
    [0, 1, 2, 3, 4].forEach(function (L) {
      if (!self.layerVisible(L)) return;
      if (self.nodes.some(function (n) { return n.L === L; })) ys.push(LAYER_Y[L]);
    });
    if (!ys.length) return 0;
    return (Math.min.apply(null, ys) + Math.max.apply(null, ys)) / 2;
  };

  Graph3D.prototype._worldRadius = function () {
    var key = this.ty + "|" + Object.keys(this.hiddenLayers).filter(
      function (k) { return this.hiddenLayers[k]; }, this).join(",") +
      "|" + this.nodes.length;
    if (this._wrKey === key) return this._wrVal;
    var max = 0;
    for (var i = 0; i < this.nodes.length; i++) {
      var n = this.nodes[i];
      if (!this.layerVisible(n.L)) continue;
      var dy = n.py - this.ty;
      var d = Math.sqrt(n.px * n.px + dy * dy + n.pz * n.pz);
      if (d > max) max = d;
    }
    this._wrKey = key;
    this._wrVal = max;
    return max;
  };

  /**
   * Zoom range derived from the content, not from constants.
   *
   * A fixed 4200 maximum let the estate shrink to a thumbnail in the middle
   * of an empty screen -- technically zoomed out, practically useless. The
   * limits are now expressed in terms of how much of the viewport the graph
   * fills, so they stay sensible when a single layer is isolated (small
   * content, closer limits) or everything is shown.
   */
  Graph3D.prototype._zoomLimits = function () {
    // While a single floor is the subject, the range is about that floor --
    // otherwise the far end of the stack sets the limits and you cannot get
    // close to the thing you asked to look at.
    if (this.focusL != null && this.layerR && this.layerR[this.focusL]) {
      var lf = this._layerFit(this.focusL);
      return { min: Math.max(MIN_DIST, lf * 0.16), max: lf * 2.2, fit: lf };
    }
    var R = this._worldRadius();
    var vp = Math.min(this.cv.clientWidth || 0, this.cv.clientHeight || 0);
    if (!R || !vp) return { min: MIN_DIST, max: MAX_DIST, fit: 1650 };
    // Distance at which the content radius maps to 45% of the smaller axis,
    // i.e. the graph comfortably fills the frame.
    var fit = 720 * R / (vp * 0.45);
    return {
      min: Math.max(MIN_DIST, fit * 0.18),   // close enough to read one cluster
      max: Math.min(MAX_DIST, fit * 1.35),   // never smaller than a legible whole
      fit: fit
    };
  };

  /** Pull the current distance back inside the derived range. */
  Graph3D.prototype._clampDist = function () {
    var L = this._zoomLimits();
    this.dist = Math.max(L.min, Math.min(L.max, this.dist));
  };

  Graph3D.prototype._contentBox = function () {
    var minX = 1e9, minY = 1e9, maxX = -1e9, maxY = -1e9, found = false;
    for (var i = 0; i < this.nodes.length; i++) {
      var n = this.nodes[i];
      if (!this.layerVisible(n.L)) continue;
      var p = this.project(n);
      if (p.depth < NEAR || !isFinite(p.x) || !isFinite(p.y)) continue;
      found = true;
      if (p.x < minX) minX = p.x;
      if (p.x > maxX) maxX = p.x;
      if (p.y < minY) minY = p.y;
      if (p.y > maxY) maxY = p.y;
    }
    return found ? { minX: minX, minY: minY, maxX: maxX, maxY: maxY } : null;
  };

  /**
   * Keep the graph on screen.
   *
   * Anchoring zoom on the pointer is right when the pointer is over the graph
   * and wrong when it is over empty space: it walks the camera into the void,
   * leaving the estate shoved into a corner behind a screenful of black.
   * Rather than special-casing one gesture, this constrains the *result* --
   * pan and zoom may do anything so long as a usable amount of the graph is
   * still in frame, so drag-panning is covered by the same rule.
   */
  Graph3D.prototype._clampPan = function () {
    var b = this._contentBox();
    if (!b) return;
    var W = this.cv.clientWidth, H = this.cv.clientHeight;
    // Require a meaningful SPAN of content to remain in frame, not merely a
    // corner pixel: 40% of each axis, or the whole graph when it is smaller
    // than that (zoomed out, or a single isolated layer).
    var needX = Math.min(b.maxX - b.minX, W * 0.4);
    var needY = Math.min(b.maxY - b.minY, H * 0.4);
    if (b.maxX < needX) this.cx += needX - b.maxX;
    else if (b.minX > W - needX) this.cx -= b.minX - (W - needX);
    if (b.maxY < needY) this.cy += needY - b.maxY;
    else if (b.minY > H - needY) this.cy -= b.minY - (H - needY);
  };

  Graph3D.prototype._dark = function () {
    var bg = cssVar("--bg");
    if (bg !== this._darkFor) {
      this._darkFor = bg;
      var hex = String(bg).trim().replace("#", "");
      if (hex.length === 3) {
        hex = hex[0] + hex[0] + hex[1] + hex[1] + hex[2] + hex[2];
      }
      var v = parseInt(hex, 16);
      if (isNaN(v) || hex.length !== 6) {
        this._darkVal = true;   // dark is the default; a bad parse should not flip it
      } else {
        var r = (v >> 16) & 255, g = (v >> 8) & 255, b = v & 255;
        this._darkVal = (0.2126 * r + 0.7152 * g + 0.0722 * b) < 128;
      }
    }
    return this._darkVal;
  };

  /** Is this layer currently shown? */
  Graph3D.prototype.layerVisible = function (L) {
    return !this.hiddenLayers[L];
  };

  /**
   * Point the camera at one layer and frame it.
   *
   * Orbiting is only useful if you can orbit the thing you care about. This
   * moves the pivot to the layer's own height, so rotating keeps it centred
   * instead of swinging it off-screen, and backs off to a distance that fits
   * a 360-radius disc comfortably.
   */
  Graph3D.prototype.focusLayer = function (L) {
    this.ty = LAYER_Y[L] || 0;
    this.cx = 0; this.cy = 0;
    // Frame THIS floor, not the estate.
    //
    // The distance used to come from _worldRadius(), which measures from the
    // pivot to the farthest node anywhere -- so focusing the top floor put
    // the pivot at its height and then measured all the way down to the
    // bottom one. Stepping through floors with the arrow keys therefore threw
    // the camera between wildly different distances (measured: 1800 to 3235
    // across five floors) and repeatedly framed the whole stack instead of
    // the floor asked for.
    this.focusL = L;
    this.dist = this._layerFit(L);
    this._clampDist();
    this.pitch = Math.max(this.pitch, 0.20);
    this.spin = false;
    this._adjFor = null;      // pivot changed; projections are all stale
  };

  /** Distance at which one floor's disc comfortably fills the frame. */
  Graph3D.prototype._layerFit = function (L) {
    var R = (this.layerR && this.layerR[L]) || 360;
    var vp = Math.min(this.cv.clientWidth || 0, this.cv.clientHeight || 0);
    if (!vp) return 1200;
    return Math.max(MIN_DIST, 720 * R / (vp * 0.42));
  };

  /**
   * Frame every visible layer at once -- the "show me everything" state.
   * Pivots at the midpoint of what is actually shown, so isolating the
   * bottom two layers frames those rather than empty space where the
   * hidden ones used to be.
   */
  Graph3D.prototype.fitAll = function () {
    var self = this, ys = [];
    this.focusL = null;      // back to looking at the whole building
    [0, 1, 2, 3, 4].forEach(function (L) {
      if (!self.layerVisible(L)) return;
      if (self.nodes.some(function (n) { return n.L === L; })) ys.push(LAYER_Y[L]);
    });
    this.ty = ys.length ? (Math.min.apply(null, ys) + Math.max.apply(null, ys)) / 2 : 0;
    var span = ys.length ? (Math.max.apply(null, ys) - Math.min.apply(null, ys)) : 0;
    this.cx = 0; this.cy = 0;

    // Fit from the MEASURED projection, not from a spherical radius.
    //
    // Modelling the estate as a sphere and fitting it to the smaller viewport
    // axis wastes the width on a stack that is wider than it is tall -- the
    // shape this becomes as soon as floors are sized by population. Because
    // projected size varies as 1/distance, one measure-and-scale step lands
    // essentially exactly, and a second is not worth the extra pass.
    this.dist = this._zoomLimits().fit * (1 + Math.min(0.25, span / 3000));
    this._clampDist();
    var W = this.cv.clientWidth || 0, H = this.cv.clientHeight || 0;
    var box = this._contentBox();
    if (box && W && H) {
      var bw = Math.max(1, box.maxX - box.minX);
      var bh = Math.max(1, box.maxY - box.minY);
      // Fill 86% of whichever axis binds first.
      var k = Math.min((W * 0.86) / bw, (H * 0.86) / bh);
      if (isFinite(k) && k > 0) {
        this.dist = this.dist / k;
        this._clampDist();
      }
    }
    this._adjFor = null;
  };

  /** Centre the camera on one node, keeping the current angle. */
  Graph3D.prototype.focusNode = function (id) {
    var n = this.byId[id];
    if (!n) return false;
    if (this.hiddenLayers[n.L]) this.hiddenLayers[n.L] = false;
    this.ty = n.py;
    this.cx = 0; this.cy = 0;
    this.dist = Math.min(this.dist, 1000);
    this.spin = false;
    this._adjFor = null;
    return true;
  };

  Graph3D.prototype._bind = function () {
    var self = this, drag = null, moved = 0;

    this.cv.addEventListener("mousedown", function (ev) {
      if (!self.active) return;
      var r = self.cv.getBoundingClientRect();
      // Right-drag and middle-drag pan as well as shift-drag: that is the
      // convention in every 3D tool, and shift-drag alone was undiscoverable.
      drag = { x: ev.clientX, y: ev.clientY, yaw: self.yaw, pitch: self.pitch,
               shift: ev.shiftKey || ev.button === 1 || ev.button === 2,
               cx: self.cx, cy: self.cy,
               mx: ev.clientX - r.left, my: ev.clientY - r.top };
      moved = 0;
      self.spin = false;
      self.cv.classList.add("drag");
    });

    window.addEventListener("mousemove", function (ev) {
      if (!self.active) return;
      var r = self.cv.getBoundingClientRect();
      var mx = ev.clientX - r.left, my = ev.clientY - r.top;
      if (drag) {
        moved++;
        if (drag.shift) {
          self.cx = drag.cx + (ev.clientX - drag.x);
          self.cy = drag.cy + (ev.clientY - drag.y);
          self._clampPan();   // panning must not lose the graph either
        } else {
          self.yaw = drag.yaw + (ev.clientX - drag.x) * 0.006;
          // Floor at a small POSITIVE pitch. The old floor of -0.2 let the
          // camera drop below the layer plates, which both inverts the "stack
          // of floors" metaphor and makes the plate ellipse radius negative --
          // and `ctx.ellipse()` throws IndexSizeError on a negative radius,
          // killing the render loop for good. See the radius guard in draw().
          self.pitch = Math.max(0.02, Math.min(1.25,
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

    // Without this, right-drag-to-pan opens the browser context menu instead.
    this.cv.addEventListener("contextmenu", function (ev) {
      if (self.active) ev.preventDefault();
    });

    this.cv.addEventListener("wheel", function (ev) {
      if (!self.active) return;
      ev.preventDefault();

      // Zoom toward the pointer, not the centre of the canvas.
      //
      // Changing `dist` alone always magnifies about the projection centre, so
      // aiming at a node on the edge of the view pushed it further off-screen
      // as you zoomed -- you had to zoom and then pan, repeatedly. Anchoring
      // means the thing under the cursor stays under the cursor.
      //
      // A point projects to  screen = C + pan + rot * (720 / depth).
      // Holding `screen` fixed while depth changes by d(dist) gives
      //   pan' = pan + (screen - C - pan) * (1 - k),   k = depth / depth'
      // which is exact for anything at the reference depth and a good
      // approximation elsewhere -- the standard behaviour for this gesture.
      var r = self.cv.getBoundingClientRect();
      var mx = ev.clientX - r.left, my = ev.clientY - r.top;
      var old = self.dist;
      var lim = self._zoomLimits();
      var next = Math.max(lim.min, Math.min(lim.max,
        old * (ev.deltaY > 0 ? 1.1 : 1 / 1.1)));
      if (next === old) {
        // At the limit already. A continued zoom-out gesture should still
        // finish centring the view rather than becoming a no-op partway.
        if (ev.deltaY > 0) {
          self.cx *= 0.88;
          self.cy *= 0.88;
          var h0 = self._pivotHome();
          self.ty += (h0 - self.ty) * 0.12;
          self._clampPan();
        }
        return;
      }

      // Zoom OUT unwinds, it does not anchor.
      //
      // Every 3D viewer worth copying is asymmetric here: zooming in dollies
      // toward the cursor, zooming out pulls back toward the orbit centre.
      // Anchoring both directions is what left the graph stranded off-centre
      // -- zoom in at one point, out at another, and the pan offsets never
      // cancel, so you drift further from centre with every gesture.
      //
      // Decaying pan by the same ratio the distance grew makes the return
      // exactly proportional: by the time you are fully zoomed out, the pan
      // and the layer pivot have both relaxed to home and the view is
      // centred, without ever snapping.
      if (next > old) {
        var f = old / next;               // < 1 when zooming out
        self.cx *= f;
        self.cy *= f;
        var home = self._pivotHome();
        self.ty += (home - self.ty) * (1 - f);
        self.dist = next;
        self.spin = false;
        self._clampPan();
        return;
      }

      // Anchor on the GRAPH, not on wherever the pointer happens to be.
      // Aiming at empty background and scrolling used to march the camera off
      // into the void; now the anchor falls back to the nearest content, so
      // zooming always converges on the estate rather than away from it.
      var anchor = self._zoomAnchor(mx, my);

      // Prefer the depth of whatever is actually under the cursor: anchoring
      // on the node you are aiming at is more accurate than assuming the
      // pivot plane, and the pivot plane may be nowhere near it.
      var refDepth = anchor.depth || old;
      var newDepth = refDepth + (next - old);
      if (!(newDepth > 1) || !isFinite(newDepth)) { self.dist = next; return; }

      var k = refDepth / newDepth;
      var dx = anchor.x - self.cv.clientWidth / 2 - self.cx;
      var dy = anchor.y - self.cv.clientHeight / 2 - self.cy;
      self.cx += dx * (1 - k);
      self.cy += dy * (1 - k);
      self.dist = next;
      self.spin = false;   // zooming is deliberate aiming; drifting fights it
      self._clampPan();
    }, { passive: false });

    this.cv.addEventListener("dblclick", function (ev) {
      if (!self.active) return;
      var r = self.cv.getBoundingClientRect();
      var n = self.nodeAt(ev.clientX - r.left, ev.clientY - r.top);
      if (n && self.opts.onDrill) self.opts.onDrill(n);
    });
  };

  global.Graph3D = Graph3D;
  // The app builds the layer navigator from these, so the names and heights
  // stay defined in exactly one place.
  global.GRAPH3D_LAYERS = LAYER_NAME;
  global.GRAPH3D_LAYER_Y = LAYER_Y;
})(window);
