/*
 * Executes the real canvas renderers against a STRICT canvas stub.
 *
 * Why strict: an earlier harness stubbed the 2D context with a permissive
 * Proxy that swallowed every call, and it reported PASS on a renderer that
 * threw IndexSizeError in Chrome on any negative pitch. A stub that accepts
 * everything cannot detect a renderer that emits invalid geometry, so this one
 * enforces what browsers actually enforce: radii must be finite and >= 0, and
 * no coordinate may be NaN or Infinity.
 *
 * Why it matters beyond one bug: a throw inside draw() escapes the
 * requestAnimationFrame callback, which abandons the frame AFTER clearRect and
 * BEFORE the next frame is scheduled. The canvas goes blank permanently while
 * nodes stay clickable at stale projected coordinates -- a failure that looks
 * like "the graph disappeared" and gives no console clue unless you are
 * looking. Cheap to prevent, expensive to diagnose.
 *
 * Run directly (`node tests/render_smoke.js`) or via test_render_smoke.py.
 * Uses synthetic data so it needs no scan, no database and no running server.
 */
const fs = require("fs");
const path = require("path");
const vm = require("vm");

// Overridable so the test can be pointed at a pre-fix copy to confirm it
// actually detects the failure it claims to guard against.
const UI = process.env.CARTOGRAPHER_UI ||
           path.join(__dirname, "..", "cartographer", "ui");

let ops = 0;
let maxArcR = 0;
function guard(name, args, radiusIdx) {
  for (const i of radiusIdx) {
    const v = args[i];
    if (typeof v !== "number" || !isFinite(v)) {
      throw new Error(name + ": non-finite radius arg" + i + " (" + v + ")");
    }
    if (v < 0) {
      throw new Error("IndexSizeError: " + name + " negative radius arg" + i +
                      " (" + v + ")");
    }
  }
  for (const v of args) {
    if (typeof v === "number" && !isFinite(v)) {
      throw new Error(name + ": non-finite argument (" + v + ")");
    }
  }
}

// Captured plate outlines. Recorded for BOTH path form and ellipse form: an
// earlier version of this check watched only path points, so a renderer that
// drew plates with ctx.ellipse produced zero captures and the assertion passed
// vacuously against code that was visibly broken.
let plates = [];
let curPath = null;
let capture = false;

const ctx = {
  canvas: { width: 1600, height: 900 },
  measureText: () => ({ width: 40 }),
  clearRect() { ops++; }, save() { ops++; }, restore() { ops++; },
  beginPath() { ops++; if (capture) curPath = []; },
  moveTo(x, y) { ops++; if (capture && curPath) curPath.push([x, y]); },
  lineTo(x, y) { ops++; if (capture && curPath) curPath.push([x, y]); },
  quadraticCurveTo() { ops++; }, fill() { ops++; }, stroke() { ops++; },
  setLineDash() { ops++; }, setTransform() { ops++; }, translate() { ops++; },
  scale() { ops++; },
  closePath() {
    ops++;
    if (capture && curPath && curPath.length > 20) plates.push(curPath);
    curPath = null;
  },
  rect() { ops++; },
  fillRect() { ops++; }, strokeRect() { ops++; },
  fillText(t, x, y) { guard("fillText", [x, y], []); ops++; },
  strokeText(t, x, y) { guard("strokeText", [x, y], []); ops++; },
  // Browsers throw on non-finite gradient geometry and on a negative radius,
  // exactly as they do for arc/ellipse.
  createRadialGradient(...a) {
    guard("createRadialGradient", a, [2, 5]);
    ops++;
    return { addColorStop() {} };
  },
  createLinearGradient(...a) {
    guard("createLinearGradient", a, []);
    ops++;
    return { addColorStop() {} };
  },
  arc(...a) { guard("arc", a, [2]); maxArcR = Math.max(maxArcR, a[2]); ops++; },
  roundRect(...a) { guard("roundRect", a, []); ops++; },
  ellipse(...a) {
    guard("ellipse", a, [2, 3]);
    ops++;
    if (capture) {
      const [x, y, rx, ry, rot] = a;
      const poly = [];
      for (let i = 0; i <= 72; i++) {
        const t = i / 72 * 6.2832, c2 = Math.cos(rot || 0), s2 = Math.sin(rot || 0);
        poly.push([x + Math.cos(t) * rx * c2 - Math.sin(t) * ry * s2,
                   y + Math.cos(t) * rx * s2 + Math.sin(t) * ry * c2]);
      }
      plates.push(poly);
    }
  },
};

function makeCanvas() {
  const handlers = {};
  return {
    width: 1600, height: 900, clientWidth: 1600, clientHeight: 900,
    getContext: () => ctx,
    getBoundingClientRect: () => ({ left: 0, top: 0, width: 1600, height: 900 }),
    // Captured so gestures can be replayed through the real handlers rather
    // than by poking engine state, which would test nothing.
    addEventListener(type, fn) { (handlers[type] = handlers[type] || []).push(fn); },
    fire(type, ev) { (handlers[type] || []).forEach((f) => f(ev)); },
    classList: { add() {}, remove() {}, toggle() {} },
    style: {},
  };
}

/**
 * views.js needs a DOM to define itself, but the layout function inside it is
 * pure. Stub just enough to load the file so the layout can be measured -- a
 * diagram's layer count and row width are the difference between a diagram
 * and a picture of a graph, and neither is assertable through the renderer.
 */
function loadViews() {
  // Rich enough to run the Architecture view end to end: the viewport is
  // where fit/pan/zoom live, and none of that is assertable through a stub
  // that throws away attributes and handlers.
  const stub = (tag) => {
    const at = {}, kids = [], hs = {};
    const e = {
      tagName: tag, attrs: at, children: kids, _h: hs,
      style: { cssText: "", setProperty() {} },
      classList: { add() {}, remove() {}, toggle() {} },
      appendChild(c) { kids.push(c); c._parent = e; return c; },
      // Enforced, not simulated. A permissive insertBefore let a render that
      // throws NotFoundError in every browser pass the suite and ship a
      // blank panel: the code cleared the container and then inserted before
      // a node that was no longer a child. A stub looser than the DOM tests
      // nothing about the DOM.
      insertBefore(c, ref) {
        if (ref && kids.indexOf(ref) === -1) {
          throw new Error("NotFoundError: insertBefore reference is not a " +
                          "child of this node");
        }
        const i = ref ? kids.indexOf(ref) : kids.length;
        kids.splice(i, 0, c);
        c._parent = e;
        return c;
      },
      removeChild(c) {
        const i = kids.indexOf(c);
        if (i === -1) throw new Error("NotFoundError: removeChild");
        kids.splice(i, 1);
        return c;
      },
      setAttribute(k, v) { at[k] = String(v); },
      getAttribute(k) { return at[k]; },
      addEventListener(t, f) { (hs[t] = hs[t] || []).push(f); },
      getBoundingClientRect() {
        return { left: 0, top: 0, width: 1200, height: 600 };
      },
      querySelectorAll() { return []; },
      onclick: null, onchange: null, type: "", checked: false,
      title: "", value: "",
    };
    Object.defineProperty(e, "innerHTML", {
      get() { return ""; },
      set() { kids.forEach((k) => { k._parent = null; }); kids.length = 0; },
    });
    Object.defineProperty(e, "textContent",
      { get() { return e._t || ""; }, set(v) { e._t = v; } });
    return e;
  };
  const S = {
    console, Math, Object, Array, JSON, String, Number, Boolean,
    isNaN, isFinite, Date, parseInt, parseFloat,
  };
  S.window = S; S.globalThis = S;
  S.addEventListener = () => {};
  S.document = {
    createElement: (t) => stub(t),
    createElementNS: (ns, t) => stub(t),
    createTextNode: () => stub("#text"),
    documentElement: {}, querySelector: () => stub("div"),
    addEventListener() {},
  };
  S.getComputedStyle = () => ({ getPropertyValue: () => "#888888" });
  vm.createContext(S);
  vm.runInContext(fs.readFileSync(path.join(UI, "views.js"), "utf8"), S,
                  { filename: "views.js" });
  return S;
}

function loadEngines() {
  // In a browser `window` IS the global object, so the `})(window)` footer of
  // each engine publishes names that the other engine then resolves bare
  // (graph3d uses cssVar/KIND_COLOR/shortLabel, all defined in graph2d). A
  // Node sandbox must alias itself or it invents ReferenceErrors that do not
  // exist in the browser.
  const S = {
    console, Math, Object, Array, JSON, String, Number, Boolean,
    isNaN, isFinite, Date, parseInt, parseFloat,
  };
  S.window = S;
  S.globalThis = S;
  S.devicePixelRatio = 1;
  S.document = { documentElement: {}, addEventListener() {} };
  S.getComputedStyle = () => ({ getPropertyValue: () => "#888888" });
  S.requestAnimationFrame = () => 0;
  S.addEventListener = () => {};
  vm.createContext(S);
  for (const f of ["graph2d.js", "graph3d.js"]) {
    vm.runInContext(fs.readFileSync(path.join(UI, f), "utf8"), S, { filename: f });
  }
  return S;
}

function syntheticGraph() {
  const kinds = ["service", "library", "topic", "datastore", "class", "file"];
  const nodes = [], edges = [];
  for (let i = 0; i < 60; i++) {
    nodes.push({
      id: "n" + i, label: "node" + i, kind: kinds[i % kinds.length],
      service: "svc" + (i % 5), repo: "repo" + (i % 3),
      layer: i % 5, rank: 0.001 * i, r: 3 + (i % 7),
    });
  }
  for (let i = 0; i < 140; i++) {
    const s = "n" + (i % 60), t = "n" + ((i * 7 + 3) % 60);
    if (s === t) continue;
    edges.push({
      source: s, target: t, kind: ["calls-route", "writes-table", "imports"][i % 3],
      count: 1 + (i % 4), provenance: i % 5 === 0 ? "INFERRED" : "EXTRACTED",
    });
  }
  // Hubs at the maximum radius the sizing formula can produce (r = 5 + 11 + 6).
  // Without these the fixture tops out around r=11 while the real estate
  // reaches 22, and a near-plane blow-up lands just under the threshold the
  // assertion below uses -- the fixture, not the renderer, decides whether the
  // bug is detectable.
  for (let i = 0; i < 5; i++) {
    nodes.push({
      id: "hub" + i, label: "hub" + i, kind: "service", layer: i,
      service: "svc" + i, repo: "repo0", size: 400, rank: 0.06,
    });
    edges.push({ source: "hub" + i, target: "n" + (i * 3), kind: "calls-route",
                 count: 9, provenance: "EXTRACTED" });
  }

  // A degenerate pair: two nodes at the identical position is the classic way
  // a force layout produces NaN, via a zero-length separation vector.
  nodes.push({ id: "dupA", label: "dupA", kind: "service", layer: 2, rank: 0, r: 5 });
  nodes.push({ id: "dupB", label: "dupB", kind: "service", layer: 2, rank: 0, r: 5 });
  edges.push({ source: "dupA", target: "dupB", kind: "imports", count: 1,
               provenance: "EXTRACTED" });
  return { nodes: nodes, edges: edges };
}

function main() {
  const S = loadEngines();
  const data = syntheticGraph();
  const failures = [];
  let frames = 0, blank = 0;

  // ---- Graph3D across the full camera space -----------------------------
  const g3 = new S.Graph3D(makeCanvas(), {});
  g3.active = true;
  g3.setData(data);

  const pitches = [-0.6, -0.2, -0.02, 0, 0.02, 0.46, 0.9, 1.25, 1.6];
  const yaws = [-3.2, 0, 0.6, 1.57, 3.14, 6.5];
  const dists = [320, 1650, 4200];
  const pans = [[0, 0], [-900, -600], [900, 600]];
  const sels = [null, "n0", "dupA"];

  for (const pitch of pitches) {
    for (const yaw of yaws) {
      for (const dist of dists) {
        for (const pan of pans) {
          for (const sel of sels) {
            g3.pitch = pitch; g3.yaw = yaw; g3.dist = dist;
            g3.cx = pan[0]; g3.cy = pan[1]; g3.selected = sel;
            ops = 0; frames++;
            try {
              g3.draw();
              if (ops === 0) blank++;
            } catch (err) {
              failures.push("Graph3D pitch=" + pitch + " yaw=" + yaw +
                            " dist=" + dist + " sel=" + sel + " :: " + err.message);
            }
          }
        }
      }
    }
  }

  // ---- Graph2D, including a settled force layout ------------------------
  const g2 = new S.Graph2D(makeCanvas(), {});
  g2.active = true;
  g2.setData(data);
  for (let i = 0; i < 60; i++) {
    try {
      g2.step();
    } catch (err) {
      failures.push("Graph2D.step :: " + err.message);
      break;
    }
  }
  for (const scale of [0.2, 0.55, 1, 3]) {
    g2.scale = scale;
    ops = 0; frames++;
    try {
      g2.draw();
      if (ops === 0) blank++;
    } catch (err) {
      failures.push("Graph2D scale=" + scale + " :: " + err.message);
    }
  }
  const nan2d = g2.nodes.filter((n) => !isFinite(n.x) || !isFinite(n.y));
  if (nan2d.length) {
    failures.push("Graph2D produced " + nan2d.length + " non-finite node positions");
  }

  // ---- every layer must be reachable ------------------------------------
  // The 3D camera used to pivot on the world origin, so zooming in put the
  // top and bottom floors permanently out of frame with no control to reach
  // them. focusLayer moves the pivot; this asserts it actually works rather
  // than merely not throwing.
  const g3n = new S.Graph3D(makeCanvas(), {});
  g3n.active = true;
  g3n.setData(data);
  g3n.dist = 600;
  const H = 900;
  (S.GRAPH3D_LAYERS || []).forEach(function (name, L) {
    const on = g3n.nodes.filter((n) => n.L === L);
    if (!on.length) return;
    g3n.focusLayer(L);
    // Zoom back IN after focusing. focusLayer deliberately backs off to frame
    // the disc, and that alone is enough to fit every floor on screen -- which
    // would mask a pivot that never moved. The reported problem happened while
    // zoomed in, so the assertion has to hold there.
    g3n.dist = 600;
    const ys = on.map((n) => g3n.project(n).y);
    const visible = ys.filter((y) => y >= 0 && y <= H).length;
    const offset = Math.abs(ys.reduce((a, b) => a + b, 0) / ys.length - H / 2);
    if (visible / on.length < 0.9) {
      failures.push("layer " + L + " (" + name + ") not reachable: only " +
                    visible + "/" + on.length + " on screen after focusLayer");
    }
    if (offset > 120) {
      failures.push("layer " + L + " (" + name + ") not centred after " +
                    "focusLayer: " + Math.round(offset) + "px from centre " +
                    "(camera pivot did not move to the layer)");
    }
  });

  // Hiding a layer must remove it from hit-testing too, or you select nodes
  // you cannot see -- the same confusion the blank-canvas bug produced.
  // Asserted through nodeAt at each hidden node's own screen position, which
  // is the code path a click actually takes.
  g3n.fitAll();
  (S.GRAPH3D_LAYERS || []).forEach(function (name, L) {
    if (!g3n.nodes.some((n) => n.L === L)) return;
    (S.GRAPH3D_LAYERS || []).forEach((_x, i) => { g3n.hiddenLayers[i] = (i !== L); });
    g3n.draw();   // assigns _p
    let leaked = 0;
    for (const n of g3n.nodes) {
      if (n.L === L || !n._p) continue;
      const hit = g3n.nodeAt(n._p.x, n._p.y);
      if (hit && hit.L !== L) leaked++;
    }
    if (leaked) {
      failures.push("soloing " + name + ": " + leaked +
                    " node(s) on hidden layers are still clickable");
    }
  });
  g3n.hiddenLayers = {};

  // ---- zoom must anchor on the pointer ----------------------------------
  // Changing distance alone magnifies about the centre of the projection, so
  // aiming at an off-centre node pushed it further off-screen as you zoomed.
  // Replayed through the real wheel handler, at the node's own screen
  // position: after zooming, it must still be under the cursor.
  const zc = makeCanvas();
  const gz = new S.Graph3D(zc, {});
  gz.active = true;
  gz.setData(data);
  gz.draw();
  const aimed = gz.nodes
    .filter((n) => n._p && n._p.x > 0 && n._p.x < 1600 && n._p.y > 0 && n._p.y < H)
    .sort((a, b) => b.r - a.r)
    .slice(0, 8);
  for (const target of aimed) {
    gz.dist = 1650; gz.cx = 0; gz.cy = 0; gz.ty = 0;
    gz.draw();
    const before = gz.project(target);
    // Aim well away from centre, where centre-anchored zoom drifts worst.
    if (Math.hypot(before.x - 800, before.y - H / 2) < 120) continue;
    for (let i = 0; i < 5; i++) {
      zc.fire("wheel", {
        clientX: before.x, clientY: before.y, deltaY: -100,
        preventDefault() {},
      });
      gz.draw();
    }
    const after = gz.project(target);
    const drift = Math.hypot(after.x - before.x, after.y - before.y);
    if (drift > 12) {
      failures.push("zoom is not anchored on the pointer: " +
                    (target.label || target.id) + " drifted " +
                    Math.round(drift) + "px over 5 notches");
    }
  }

  // ---- no node may swallow the viewport ---------------------------------
  // Below the near plane the perspective divide explodes. The old code floored
  // depth at 40 -- a divide-by-zero guard used as a near plane -- which at the
  // closest zoom produced single nodes 288px across covering a fifth of the
  // canvas and occluding everything behind them.
  const gz2 = new S.Graph3D(makeCanvas(), {});
  gz2.active = true;
  gz2.setData(data);
  for (const dist of [4200, 1650, 900, 700, 560, 460, 430]) {
    gz2.dist = dist; gz2.cx = 0; gz2.cy = 0; gz2.ty = 0;
    maxArcR = 0;
    gz2.draw();
    if (maxArcR > 90) {
      failures.push("at dist=" + dist + " a node draws " + Math.round(maxArcR) +
                    "px across -- near-plane culling or the radius cap is not " +
                    "holding");
    }
  }

  // Zoom must stay outside the point cloud however it is driven.
  const zc2 = makeCanvas();
  const gz3 = new S.Graph3D(zc2, {});
  gz3.active = true;
  gz3.setData(data);
  gz3.draw();
  for (let i = 0; i < 80; i++) {
    zc2.fire("wheel", { clientX: 800, clientY: 450, deltaY: -100,
                        preventDefault() {} });
  }
  if (gz3.dist < 400) {
    failures.push("wheel zoom drove distance to " + Math.round(gz3.dist) +
                  " -- inside the scene, where the projection blows up");
  }
  for (let i = 0; i < 80; i++) gz3.zoomBy(1.25);
  if (gz3.dist < 400) {
    failures.push("zoomBy drove distance to " + Math.round(gz3.dist) +
                  " -- the +/- buttons are unclamped");
  }

  // ---- zooming must converge on the graph, never into the void ----------
  // Pointer-anchored zoom is right over the graph and wrong over empty
  // background, where it walked the camera away until the estate sat off
  // screen entirely behind a screenful of black.
  const vc = makeCanvas();
  const gv = new S.Graph3D(vc, {});
  gv.active = true;
  gv.setData(data);
  gv.draw();
  const visible = () => {
    const b = gv._contentBox();
    if (!b) return 0;
    const w = Math.max(0, Math.min(1600, b.maxX) - Math.max(0, b.minX));
    const h = Math.max(0, Math.min(H, b.maxY) - Math.max(0, b.minY));
    return (w * h) / (1600 * H) * 100;
  };
  for (let i = 0; i < 25; i++) {
    vc.fire("wheel", { clientX: 60, clientY: 60, deltaY: -100,
                       preventDefault() {} });
    gv.draw();
  }
  if (visible() < 5) {
    failures.push("zooming over empty background lost the graph: only " +
                  visible().toFixed(1) + "% of the viewport still shows content");
  }
  // Panning must not be able to lose it either.
  gv.cx = -9000; gv.cy = -9000; gv._clampPan(); gv.draw();
  if (visible() < 5) {
    failures.push("panning lost the graph: only " + visible().toFixed(1) +
                  "% of the viewport still shows content");
  }

  // ---- zoom range must stay practical -----------------------------------
  // A fixed 4200 maximum let the estate shrink to a thumbnail floating in an
  // empty screen: technically zoomed out, practically useless. Limits are now
  // derived from content extent, so they must keep the graph legible at both
  // ends however hard the wheel is spun.
  const rc = makeCanvas();
  const gr2 = new S.Graph3D(rc, {});
  gr2.active = true;
  gr2.setData(data);
  gr2.draw();
  const spanPct = () => {
    const b = gr2._contentBox();
    if (!b) return 0;
    return Math.max(0, Math.min(1600, b.maxX) - Math.max(0, b.minX)) / 1600 * 100;
  };
  for (let i = 0; i < 80; i++) {
    rc.fire("wheel", { clientX: 800, clientY: 450, deltaY: 100,
                       preventDefault() {} });
  }
  gr2.draw();
  if (spanPct() < 15) {
    failures.push("zoomed all the way out the graph spans only " +
                  spanPct().toFixed(0) + "% of the viewport -- a thumbnail");
  }
  for (let i = 0; i < 160; i++) {
    rc.fire("wheel", { clientX: 800, clientY: 450, deltaY: -100,
                       preventDefault() {} });
  }
  gr2.draw();
  const lim = gr2._zoomLimits();
  if (gr2.dist < lim.min - 1) {
    failures.push("wheel zoomed past the derived minimum: " +
                  Math.round(gr2.dist) + " < " + Math.round(lim.min));
  }

  // ---- zoom out must unwind to centre -----------------------------------
  // Zoom in at one point, out at another: with both directions anchored on
  // the pointer the pan offsets never cancel and the graph drifts further
  // off-centre with every gesture. Real 3D viewers dolly toward the cursor
  // going in and pull back to the orbit centre coming out.
  const cc = makeCanvas();
  const gc = new S.Graph3D(cc, {});
  gc.active = true;
  gc.setData(data);
  gc.draw();
  const offset = () => {
    const b = gc._contentBox();
    if (!b) return 1e9;
    return Math.hypot((b.minX + b.maxX) / 2 - 800, (b.minY + b.maxY) / 2 - H / 2);
  };
  const spin = (x, y, dir, n) => {
    for (let i = 0; i < n; i++) {
      cc.fire("wheel", { clientX: x, clientY: y, deltaY: dir,
                         preventDefault() {} });
    }
    gc.draw();
  };
  spin(400, 300, -100, 12);     // in, upper left
  spin(1300, 800, 100, 60);     // out, lower right, all the way
  if (offset() > 120) {
    failures.push("zooming out did not recentre: content centre is " +
                  Math.round(offset()) + "px from the middle of the viewport");
  }

  // ---- the force layout must not explode -------------------------------
  // At estate scale every node sits inside the repulsion cutoff of about a
  // thousand others, and a single coincident pair injects an enormous impulse
  // at the old separation floor: (rep + r*190) / 1 is roughly 14,000 of force
  // in one step. Real data reached max |coordinate| ~2.4e4, fit() computed a
  // scale below its own floor, and the Map view drew the whole estate as a
  // speck.
  //
  // The assertion is on the INVARIANT the fix provides -- a per-iteration
  // displacement cap, Fruchterman-Reingold's "temperature" -- rather than on
  // emergent divergence, which depends on topology the fixture cannot
  // faithfully reproduce. Two earlier attempts asserted the emergent
  // behaviour and both passed against deliberately broken code.
  const big = { nodes: [], edges: [] };
  for (let i = 0; i < 700; i++) {
    big.nodes.push({ id: "b" + i, label: "b" + i,
                     kind: i % 50 === 0 ? "service" : "file",
                     service: "s" + (i % 6), repo: "r0", layer: i % 5,
                     rank: 0.05, size: (i % 60) * 15 });
  }
  for (let i = 0; i < 1700; i++) {
    const a2 = "b" + (i % 700), b2 = "b" + ((i * 13 + 5) % 700);
    if (a2 !== b2) {
      big.edges.push({ source: a2, target: b2, kind: "imports", count: 1,
                       provenance: "EXTRACTED" });
    }
  }
  const gb = new S.Graph2D(makeCanvas(), {});
  gb.active = true;
  gb.setData(big);
  // The real estate contains exactly one coincident pair; reproduce it.
  gb.nodes[0].x = gb.nodes[1].x;
  gb.nodes[0].y = gb.nodes[1].y;

  const before = gb.nodes.map((n) => ({ x: n.x, y: n.y }));
  gb.step();
  let maxMove = 0;
  for (let i = 0; i < gb.nodes.length; i++) {
    const n = gb.nodes[i];
    if (!isFinite(n.x) || !isFinite(n.y)) {
      failures.push("force layout produced a non-finite coordinate");
      break;
    }
    maxMove = Math.max(maxMove,
      Math.hypot(n.x - before[i].x, n.y - before[i].y));
  }
  if (maxMove > 35) {
    failures.push("force layout moved a node " + Math.round(maxMove) +
                  "px in one step -- the per-iteration displacement cap is " +
                  "missing, so velocity compounds and the layout diverges");
  }

  // And it must still settle to something framable.
  for (let i = 0; i < 400; i++) gb.step();
  let maxCoord = 0;
  for (const n of gb.nodes) {
    if (!isFinite(n.x) || !isFinite(n.y)) { maxCoord = Infinity; break; }
    maxCoord = Math.max(maxCoord, Math.abs(n.x), Math.abs(n.y));
  }
  if (!(maxCoord < 20000)) {
    failures.push("force layout did not settle: max |coordinate| " +
                  maxCoord.toExponential(1));
  }

  // ---- a floor's ring must bound that floor's nodes ---------------------
  // Plates were drawn as one axis-aligned screen ellipse sized from the
  // centre probe. That applies a single depth to the whole ring (so the near
  // edge is not larger than the far edge) and cannot represent a footprint
  // that yaw has rotated -- which is what happens the moment floors stop
  // being circular. The result was rings sitting beside their own nodes:
  // measured, as low as 50% containment. The boundary is now projected from
  // the same curve the nodes are placed on.
  const gp = new S.Graph3D(makeCanvas(), {});
  gp.active = true;
  gp.setData(data);
  const inPoly = (pt, poly) => {
    let c = false;
    for (let i = 0, j = poly.length - 1; i < poly.length; j = i++) {
      const xi = poly[i][0], yi = poly[i][1], xj = poly[j][0], yj = poly[j][1];
      if (((yi > pt[1]) !== (yj > pt[1])) &&
          (pt[0] < (xj - xi) * (pt[1] - yi) / (yj - yi) + xi)) c = !c;
    }
    return c;
  };
  for (const yaw of [0, 0.6, 1.6, 3.0, 4.5]) {
    for (const pitch of [0.20, 0.26, 0.45, 0.9]) {
      gp.yaw = yaw; gp.pitch = pitch; gp.fitAll();
      plates = []; curPath = null; capture = true;
      gp.draw();
      capture = false;
      let tot = 0, inside = 0;
      for (const L of [0, 1, 2, 3, 4]) {
        const ns = gp.nodes.filter((n) => n.L === L)
          .map((n) => gp.project(n)).filter((p) => p.depth >= 260);
        if (!ns.length || !plates.length) continue;
        const cyN = ns.reduce((a2, p) => a2 + p.y, 0) / ns.length;
        let best = null, bd = 1e9;
        for (const pl of plates) {
          const c2 = pl.reduce((a2, q) => a2 + q[1], 0) / pl.length;
          if (Math.abs(c2 - cyN) < bd) { bd = Math.abs(c2 - cyN); best = pl; }
        }
        if (!best) continue;
        for (const p of ns) { tot++; if (inPoly([p.x, p.y], best)) inside++; }
      }
      if (tot && inside / tot < 0.90) {
        failures.push("layer plate does not bound its nodes at yaw=" + yaw +
                      " pitch=" + pitch + ": only " +
                      Math.round(inside / tot * 100) + "% inside");
      }
    }
  }

  // ---- architecture diagram must lay out as a diagram --------------------
  // Longest-path layering is correct but literal: on a real estate 37
  // services came out as 17 layers two nodes wide, and every shared store
  // landed in one 70-wide row about 11,000px across. Both are technically a
  // layout and neither is a diagram.
  const AV = loadViews();
  if (AV.Views && AV.Views._layout) {
    const svcN = 40, wpN = 70;
    const anodes = {}, alinks = [];
    for (let i = 0; i < svcN; i++) {
      anodes["s" + i] = { id: "s" + i, label: "svc" + i, kind: "service" };
    }
    for (let i = 0; i < wpN; i++) {
      anodes["w" + i] = { id: "w" + i, label: "tbl" + i, kind: "table",
                          writers: 2, readers: 2, significant: true };
    }
    // A deep call chain (which is what produced 17 layers) plus fan-out, and
    // a cycle, because service graphs have them.
    for (let i = 0; i < svcN - 1; i++) {
      alinks.push({ source: "s" + i, target: "s" + (i + 1), count: 2,
                    kinds: [["http", 2]], provenance: "EXTRACTED" });
    }
    alinks.push({ source: "s" + (svcN - 1), target: "s0", count: 1,
                  kinds: [["http", 1]], provenance: "EXTRACTED" });
    for (let i = 0; i < wpN; i++) {
      alinks.push({ source: "s" + (i % svcN), target: "w" + i, count: 3,
                    kinds: [["writes-table", 3]], provenance: "EXTRACTED" });
      alinks.push({ source: "w" + i, target: "s" + ((i * 7 + 1) % svcN), count: 1,
                    kinds: [["reads-table", 1]], provenance: "EXTRACTED" });
    }
    const L = AV.Views._layout(anodes, alinks);
    const rowKeys = Object.keys(L.rows);
    let widest = 0;
    rowKeys.forEach((k) => { widest = Math.max(widest, L.rows[k].length); });
    if (rowKeys.length > 30) {
      failures.push("architecture layout produced " + rowKeys.length +
                    " layers from a 40-service chain -- a ribbon, not a diagram");
    }
    if (widest > 24) {
      failures.push("architecture layout produced a row " + widest +
                    " boxes wide -- it does not wrap the data band");
    }
    // Cycles must be detected, not silently laid out as if acyclic.
    if (!Object.keys(L.back).length) {
      failures.push("architecture layout did not detect the cycle in the fixture");
    }
  } else {
    failures.push("views.js did not export the architecture layout");
  }

  // ---- the diagram must fit, and zoom about the pointer -----------------
  // It was first shipped as a fixed-size SVG in a scrollbox: to see the whole
  // architecture you had to scroll in two directions, which is the one thing
  // a diagram must not require. Fit/pan/zoom are all one viewBox now, so this
  // asserts the two properties that matter.
  if (AV.Views && AV.Views.architecture) {
    const svcs = [], lks = [];
    for (let i = 0; i < 24; i++) {
      svcs.push({ id: "s" + i, label: "svc" + i, kind: "service" });
    }
    for (let i = 0; i < 23; i++) {
      lks.push({ source: "s" + i, target: "s" + (i + 1), count: 1,
                 kinds: [["http", 1]], provenance: "EXTRACTED" });
    }
    const payload = { nodes: svcs, links: lks, services: 24, waypoints: 0,
                      significant: 0, excluded: { build_time_edges: 99 } };
    const pane = AV.document.createElement("div");
    // Rendering must not throw. It did: the view cleared its container and
    // then inserted before a node that was no longer a child, which browsers
    // reject with NotFoundError -- the panel came up blank with nothing in
    // the console but that one error.
    let renderErr = null;
    try {
      AV.Views.architecture(
        pane,
        () => ({ then: (f) => { f(payload); return { catch() {} }; } }),
        () => {});
    } catch (err) {
      renderErr = err;
      failures.push("architecture view threw while rendering: " + err.message);
    }
    let svg = null, boxes = [];
    if (renderErr) { svg = null; }
    (function walk(n) {
      if (!n || !n.children) return;
      if (n.tagName === "svg") svg = n;
      if (n.tagName === "rect") {
        boxes.push({ x: +n.getAttribute("x"), y: +n.getAttribute("y"),
                     w: +n.getAttribute("width"), h: +n.getAttribute("height") });
      }
      n.children.forEach(walk);
    })(pane);

    if (!svg) {
      failures.push("architecture view produced no svg");
    } else {
      const vb = String(svg.getAttribute("viewBox") || "").split(/\s+/).map(Number);
      if (vb.length !== 4 || !vb.every((n) => isFinite(n))) {
        failures.push("architecture svg has no usable viewBox: " +
                      svg.getAttribute("viewBox"));
      } else if (boxes.length) {
        const minX = Math.min(...boxes.map((b) => b.x));
        const maxX = Math.max(...boxes.map((b) => b.x + b.w));
        const minY = Math.min(...boxes.map((b) => b.y));
        const maxY = Math.max(...boxes.map((b) => b.y + b.h));
        if (!(vb[0] <= minX && vb[1] <= minY &&
              vb[0] + vb[2] >= maxX && vb[1] + vb[3] >= maxY)) {
          failures.push("architecture does not open fitted: content " +
                        "x[" + Math.round(minX) + "," + Math.round(maxX) + "] " +
                        "is outside viewBox " + svg.getAttribute("viewBox"));
        }
        // Cursor-anchored zoom, same rule as the graph views.
        const wheel = svg._h && svg._h.wheel && svg._h.wheel[0];
        if (!wheel) {
          failures.push("architecture svg has no wheel handler -- cannot zoom");
        } else {
          const px = 900, py = 140;
          const read = () => String(svg.getAttribute("viewBox")).split(/\s+/).map(Number);
          const at = (v) => ({ x: v[0] + px / 1200 * v[2], y: v[1] + py / 600 * v[3] });
          const before = at(read());
          for (let i = 0; i < 6; i++) {
            wheel({ clientX: px, clientY: py, deltaY: -100, preventDefault() {} });
          }
          const after = at(read());
          const drift = Math.hypot(after.x - before.x, after.y - before.y);
          if (drift > 2) {
            failures.push("architecture zoom is not anchored on the pointer: " +
                          drift.toFixed(1) + "px drift over six notches");
          }
        }
      }
    }
  }

  console.log("frames rendered : " + frames);
  console.log("zero-op frames  : " + blank);
  console.log("failures        : " + failures.length);
  failures.slice(0, 10).forEach((f) => console.log("  " + f));
  if (failures.length || blank) {
    console.log("FAIL");
    process.exit(1);
  }
  console.log("PASS");
}

main();
