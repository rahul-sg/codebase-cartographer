/* Main application: view routing, drill-down, selection, detail panel. */
(function () {
  "use strict";

  var $ = function (s) { return document.querySelector(s); };
  var stage = $("#stage"), cv = $("#cv"), detail = $("#detail"),
      tooltip = $("#tooltip"), crumbs = $("#crumbs"), overlay = $("#overlay");

  /**
   * A stored UTC timestamp rendered in the viewer's own timezone, labelled.
   *
   * `scanned_at` is stored as ISO-8601 UTC ("2026-09-08T23:52:00Z"), which is
   * correct. The display was not: slicing the string to 16 characters threw
   * the "Z" away and printed UTC digits as though they were local, so a scan
   * run at 16:52 PDT showed as 23:52 -- seven hours in the future, with no
   * timezone shown to reveal the discrepancy.
   *
   * The zone label comes from the browser rather than being hardcoded,
   * because "PST" is only correct from roughly November to March; the rest of
   * the year Pacific is PDT, an hour off. This also means the timestamp reads
   * correctly for anyone running the UI in another zone.
   *
   * Date and time come from the `sv-SE` locale purely because it formats as
   * `YYYY-MM-DD HH:MM` (ISO-like, unambiguous, sorts correctly); the zone
   * abbreviation is taken from `en-US`, which spells it "PDT" rather than
   * "GMT-7".
   */
  function fmtWhen(iso) {
    if (!iso) return "";
    var d = new Date(iso);
    if (isNaN(d.getTime())) return String(iso).slice(0, 16).replace("T", " ");
    try {
      var dt = d.toLocaleString("sv-SE", {
        year: "numeric", month: "2-digit", day: "2-digit",
        hour: "2-digit", minute: "2-digit", hour12: false
      });
      var tz = "";
      var parts = new Intl.DateTimeFormat("en-US", { timeZoneName: "short" })
        .formatToParts(d);
      for (var i = 0; i < parts.length; i++) {
        if (parts[i].type === "timeZoneName") { tz = " " + parts[i].value; break; }
      }
      return dt + tz;
    } catch (e) {
      // Intl missing or locale unsupported: still better to be explicit about
      // the zone than to print bare digits that look local and are not.
      return d.toISOString().slice(0, 16).replace("T", " ") + " UTC";
    }
  }

  /** "3 hours ago" -- so a stale graph is obvious without doing date maths. */
  function relAge(iso) {
    var d = new Date(iso);
    if (isNaN(d.getTime())) return "";
    var mins = Math.round((Date.now() - d.getTime()) / 60000);
    if (mins < 0) return "clock skew (timestamp is in the future)";
    if (mins < 1) return "just now";
    if (mins < 60) return mins + (mins === 1 ? " minute ago" : " minutes ago");
    var hrs = Math.round(mins / 60);
    if (hrs < 24) return hrs + (hrs === 1 ? " hour ago" : " hours ago");
    var days = Math.round(hrs / 24);
    return days + (days === 1 ? " day ago" : " days ago");
  }

  var state = {
    view: "map",
    level: "estate",
    focus: null,
    pkg: null,
    selected: null,
    filters: {},          // edge style-kind -> bool
    hideInferred: false,
    hideLibs: true,
    // Off by default: hiding data is a user's choice, not the tool's. The
    // dense view is a LAYOUT problem, fixed in the engines; this filter is
    // for when you deliberately want only the load-bearing structure.
    simplify: false,
    crossRepo: true,      // emphasise edges that cross a git repo boundary
    prunedCount: 0,
    repoFilter: null,     // Set of enabled repo names, or null for "all"
    // On by default: the reason to map several repos together is to see
    // where they touch, and that is invisible in a single colour.
    colorByRepo: true,
    mtime: 0
  };

  /* ---------------- repo attribution ----------------
     Only `service` nodes carry `repo`; topics, tables, datastores and
     libraries carry none, because they are shared and not owned by one repo.
     So repo-scoped views work off the service nodes, and a shared resource is
     identified by the repos of the services attached to it -- which is a
     two-hop relationship, not an edge property. */

  var REPO_PALETTE = [
    "#5b8ff9", "#5ad8a6", "#f6bd16", "#e8684a", "#6dc8ec",
    "#9270ca", "#ff9d4d", "#269a99", "#ff99c3", "#a0a0a0"
  ];
  var repoColorMap = {};

  function repoColor(repo) {
    if (!repo) return null;
    if (!(repo in repoColorMap)) {
      repoColorMap[repo] = REPO_PALETTE[Object.keys(repoColorMap).length
                                        % REPO_PALETTE.length];
    }
    return repoColorMap[repo];
  }

  /* Modules of the services directly attached to each node, so a shared thing
     (topic, table, library) can be kept visible whenever anything it connects
     to is still on screen.

     Deliberately NOT used to flag "cross-repo coupling": cartographer treats
     each Maven module as its own repo, so at estate level almost everything is
     touched by 20+ "repos" and the signal is meaningless. Real data coupling
     between services lives in the Data tab, off /api/schema's writers/readers. */
  function reposTouching(nodes, edges) {
    var repoOf = {}, touch = {};
    nodes.forEach(function (n) {
      var g = n.git_repo || n.repo;
      if (g) repoOf[n.id] = g;
    });
    edges.forEach(function (e) {
      [[e.source, e.target], [e.target, e.source]].forEach(function (p) {
        var r = repoOf[p[0]];
        if (!r) return;
        (touch[p[1]] = touch[p[1]] || {})[r] = 1;
      });
    });
    var out = {};
    Object.keys(touch).forEach(function (id) {
      out[id] = Object.keys(touch[id]);
    });
    return out;
  }

  var EDGE_GROUPS = [
    ["sync", "HTTP / RPC calls", "--sync"],
    ["async", "events & topics", "--async"],
    ["data", "tables, caches, stores", "--data"],
    ["code", "calls, imports, extends", "--code"],
    ["build", "build dependencies", "--build"],
    ["contract", "exposed endpoints", "--contract"],
    ["deploy", "hosts & routing", "--deploy"],
    ["legacy", "legacy pages", "--legacy"],
    ["structure", "file structure", "--code"],
    ["other", "other", "--code"]
  ];
  EDGE_GROUPS.forEach(function (g) {
    state.filters[g[0]] = !(g[0] === "structure" || g[0] === "deploy");
  });

  function api(path) {
    return fetch(path).then(function (r) {
      if (!r.ok) return r.json().then(function (j) { throw new Error(j.error || r.status); });
      // NOTE: some endpoints report failure as 200 + {"error": ...}. That body
      // is returned as-is on purpose -- callers such as renderDetail and the
      // Impact view check `.error` themselves and render it inline. Throwing
      // here instead would turn those into unhandled rejections, because none
      // of those call sites attach a .catch.
      return r.json();
    });
  }

  /* ---------------- graph engines ---------------- */

  function edgeVisible(e) {
    var k = (e.style && e.style.kind) || "other";
    if (!state.filters[k]) return false;
    if (state.hideInferred && e.provenance === "INFERRED") return false;
    return true;
  }

  var handlers = {
    edgeVisible: edgeVisible,
    onClick: function (n) { select(n.id); },
    onDrill: function (n) { drill(n); },
    onBlank: function () { clearSelection(); },
    onEdgeClick: function (e) { showEdge(e); },
    onHover: function (n, e, mx, my) { showTip(n, e, mx, my); }
  };

  var g2 = new Graph2D(cv, handlers);
  var g3 = new Graph3D(cv, handlers);
  function engine() { return state.view === "layers" ? g3 : g2; }

  /* ---------------- loading ---------------- */

  function loadGraph(keepView) {
    var q = "/api/graph?level=" + encodeURIComponent(state.level);
    if (state.focus) q += "&focus=" + encodeURIComponent(state.focus);
    if (state.pkg) q += "&package=" + encodeURIComponent(state.pkg);
    return api(q).then(function (d) {
      $("#empty").hidden = true;
      var nodes = d.nodes;
      if (state.hideLibs) {
        nodes = nodes.filter(function (n) {
          // Internal shared libraries are part of the architecture; a
          // third-party package on npm or Maven Central is not.
          return !(n.kind === "library" && !(n.meta && n.meta.artifactId));
        });
      }
      // Computed on the FULL response, before filtering, so a shared node's
      // attachments are known even for modules hidden from the current view.
      var touching = reposTouching(d.nodes, d.edges);
      // Group by git repository, not by build unit: 26 Maven modules share one
      // checkout, and offering 26 "repos" is both wrong and unusable.
      state.repoList = Object.keys(d.nodes.reduce(function (a, n) {
        var g = n.git_repo || n.repo;
        if (g) a[g] = 1; return a;
      }, {})).sort();
      // Rebuilt here rather than at each of loadGraph's ten call sites.
      drawRepoList();

      if (state.repoFilter) {
        var rf = state.repoFilter;
        // Keep an unattributed node (topic, table, library) only when it still
        // connects to a visible repo -- otherwise the view fills with orphans.
        nodes = nodes.filter(function (n) {
          var g = n.git_repo || n.repo;
          if (g) return rf[g];
          var t = touching[n.id];
          if (!t) return false;
          return t.some(function (r) { return rf[r]; });
        });
      }

      // ---- dead-end pruning ------------------------------------------
      // A node connected to exactly one thing tells you nothing about the
      // shape of the estate: it cannot be on a path between two services, it
      // cannot be a coupling point, and it cannot be a bottleneck. On a real
      // estate these dominate -- 77% of one 1219-node view was topics,
      // libraries and legacy pages hanging off a single owner -- and they
      // bury the ~280 nodes that actually carry the topology under a fringe
      // of unreadable dots. Hidden by default at estate level, where the
      // question is "what is the shape"; kept everywhere else, where you have
      // already drilled to something specific and its leaves are the point.
      var pruned = 0;
      if (state.simplify && state.level === "estate") {
        var deg = {};
        d.edges.forEach(function (e) {
          deg[e.source] = (deg[e.source] || 0) + 1;
          deg[e.target] = (deg[e.target] || 0) + 1;
        });
        var before = nodes.length;
        nodes = nodes.filter(function (n) {
          if (n.id === state.selected) return true;   // never hide the subject
          return (deg[n.id] || 0) > 1;
        });
        pruned = before - nodes.length;
      }
      state.prunedCount = pruned;

      var keep = {};
      nodes.forEach(function (n) { keep[n.id] = 1; });
      var edges = d.edges.filter(function (e) {
        return keep[e.source] && keep[e.target];
      });
      // Hand the per-module colour to the renderer.
      nodes.forEach(function (n) {
        n._repoColor = state.colorByRepo
          ? repoColor(n.git_repo || n.repo) : null;
      });

      // Mark edges that cross a GIT repository boundary.
      //
      // These are the point of a multi-repo map: coupling inside one repo can
      // be found by reading that repo, while coupling between two is what
      // nobody owns and nothing documents. On a real estate they are only
      // ~20% of attributed edges, so emphasising them costs little contrast.
      //
      // Deliberately keyed on `git_repo`, not `repo`. An earlier attempt at
      // this keyed on the build unit, where every Maven module counts as its
      // own "repo", and produced a couple of hundred meaningless seams that
      // were mostly shared libraries. Edges with an unattributed endpoint --
      // topics, tables, libraries, 62% of the total -- are neither: they are
      // shared by design and are left alone.
      var repoOfId = {};
      nodes.forEach(function (n) {
        var r = n.git_repo || n.repo;
        if (r) repoOfId[n.id] = r;
      });
      edges.forEach(function (ed) {
        var a2 = repoOfId[ed.source], b2 = repoOfId[ed.target];
        ed._cross = !!(state.crossRepo && a2 && b2 && a2 !== b2);
        ed._intra = !!(state.crossRepo && a2 && b2 && a2 === b2);
      });
      state.lastGraph = { nodes: nodes, edges: edges, breadcrumb: d.breadcrumb,
                          stats: d.stats, truncated: d.truncated || 0 };
      engine().setData(state.lastGraph, keepView);
      // Counts come from the engine's own layer assignment, so this has to
      // run after setData rather than off the raw payload.
      if (state.view === "layers") drawLayerList();
      drawCrumbs(d.breadcrumb);
      var pn = $("#prunedN");
      if (pn) {
        pn.textContent = pruned
          ? "(" + pruned.toLocaleString() + " hidden)"
          : (state.level === "estate" ? "" : "(estate only)");
      }
      writeHash();   // level / focus / package may all have changed
      if (!nodes.length) {
        // An empty canvas with no message is indistinguishable from a bug.
        // Say which view came back empty and what to do about it.
        $("#empty").hidden = false;
        $("#empty").innerHTML =
          "<h2>Nothing to show here</h2><p>" +
          esc("level=" + state.level + (state.focus ? ", focus=" + state.focus : "") +
              (state.pkg ? ", package=" + state.pkg : "")) +
          " returned no nodes.</p><p class='muted'>" +
          (state.hideLibs ? "Third-party libraries are hidden — try showing them. "
                          : "") +
          "Otherwise go up a level via the breadcrumb, or use search.</p>";
      }
      // `stats` is {} on some empty responses; fall back to what was rendered
      // so the hint never reads "undefined nodes".
      var st = d.stats || {};
      var nCount = (st.nodes != null) ? st.nodes : nodes.length;
      var eCount = (st.edges != null) ? st.edges : edges.length;
      var hint = nCount + " nodes · " + eCount + " connections";
      if (st.rolled_up) {
        hint += "  ·  " + st.rolled_up.toLocaleString() +
                " more rolled up inside them";
      }
      if (d.truncated) {
        // Say when the view is a subset. Silently showing the top N and
        // calling it the picture is how a map starts lying to you.
        hint += "  ·  showing the " + d.nodes.length +
                " highest-ranked of " + (d.nodes.length + d.truncated) +
                " — use search to reach the rest";
      }
      hint += "   —   double-click a ringed node to open it";
      $("#viewhint").textContent = hint;
    }).catch(function (err) {
      $("#empty").hidden = false;
      $("#empty").innerHTML = "<h2>No graph</h2><p>" + esc(err.message) + "</p>";
    });
  }

  function drawCrumbs(bc) {
    crumbs.innerHTML = "";
    (bc || []).forEach(function (c, i, arr) {
      if (i) {
        var sep = el("span", "sep", "›");
        crumbs.appendChild(sep);
      }
      if (i === arr.length - 1) {
        crumbs.appendChild(el("span", "cur", c.label));
      } else {
        var a = el("a", null, c.label);
        a.onclick = function () {
          state.level = c.level; state.focus = c.focus || null;
          state.pkg = c.package || null;
          loadGraph();
        };
        crumbs.appendChild(a);
      }
    });
  }

  function drill(n) {
    if (n.kind === "service" || (n.kind === "library" && n.meta && n.meta.artifactId)) {
      state.level = "service"; state.focus = n.label; state.pkg = null;
      loadGraph();
    } else if (n.kind === "package") {
      state.level = "package"; state.pkg = n.package; loadGraph();
    } else if (n.kind === "file") {
      state.level = "file"; state.focus = n.id; loadGraph();
    } else {
      select(n.id);
    }
  }

  /* ---------------- selection & detail ---------------- */

  function clearSelection() {
    state.selected = null;
    writeHash();
    g2.selected = null; g3.selected = null;
    g2.setHighlight(null);
    detail.innerHTML = "<div class='placeholder'><h3>Nothing selected</h3>" +
      "<p>Click a node to inspect it. Double-click to drill in. " +
      "Click an edge to see the exact <code>file:line</code> that proves it.</p></div>";
  }

  /**
   * Back to where you started: nothing selected, whole estate, default camera.
   *
   * Clicking around leaves you in three states at once -- drilled into a
   * subtree, dimmed by a selection, and panned somewhere -- and until now each
   * had a different, separately-undiscoverable way out (Backspace, Escape,
   * fit). One control undoes all three.
   */
  function resetView() {
    var drilled = state.level !== "estate" || state.focus || state.pkg;
    clearSelection();
    state.level = "estate"; state.focus = null; state.pkg = null;
    g3.yaw = 0.6; g3.pitch = 0.46; g3.dist = 1650; g3.cx = 0; g3.cy = 0;
    g3.ty = 0; g3.hiddenLayers = {}; g3._adjFor = null; g3.focusL = null;
    state._savedNav = null;      // Reset means reset, including the stash
    if (drilled) { loadGraph(); return; }   // reload already redraws
    if (state.view === "layers") { g3.draw(); drawLayerList(); }
    else { g2.fit(); g2.draw(); }
  }

  function select(id) {
    state.selected = id;
    writeHash();
    g2.selected = id; g3.selected = id;
    if (state.view === "map") g2.setHighlight(g2.neighbours(id));
    g2.draw();
    detail.innerHTML = "<p class='muted'>loading…</p>";
    api("/api/node?id=" + encodeURIComponent(id)).then(renderDetail);
  }

  function renderDetail(n) {
    detail.innerHTML = "";
    if (n.error) { detail.appendChild(el("p", "muted", n.error)); return; }

    if (n.synthetic) {
      detail.appendChild(el("h2", null, n.label));
      detail.appendChild(el("div", "sub", "package in " + n.service));
      detail.appendChild(el("h3", null, "Files (" + n.files.length + ")"));
      n.files.forEach(function (f) {
        var r = el("div", "item");
        var a = el("span", "lbl", f.file);
        a.onclick = function () { select(f.id); };
        r.appendChild(a);
        detail.appendChild(r);
      });
      return;
    }

    detail.appendChild(el("h2", null, n.name || (n.file || "").split("/").pop() || n.id));
    var sub = [n.kind];
    if (n.service) sub.push(n.service);
    if (n.lang) sub.push(n.lang);
    detail.appendChild(el("div", "sub", sub.join("  ·  ")));

    if (n.file) {
      var loc = el("div", "mono muted");
      loc.style.wordBreak = "break-all";
      loc.textContent = n.file + (n.line ? ":" + n.line : "");
      detail.appendChild(loc);
    }

    var badges = el("div");
    badges.style.marginTop = "8px";
    if (n.rank) {
      badges.appendChild(el("span", "badge",
        "centrality " + n.rank.toFixed(5)));
    }
    var m = n.meta || {};
    if (m.in_reactor === false) {
      badges.appendChild(el("span", "badge warn", "outside the build reactor"));
    }
    if (m.war) badges.appendChild(el("span", "badge", "WAR"));
    if (m.frontend) badges.appendChild(el("span", "badge", m.framework || "frontend"));
    if (m.runtime_resolved) {
      badges.appendChild(el("span", "badge rt", "topic from constant " + (m.constant || "")));
    }
    if (m.authoritative) badges.appendChild(el("span", "badge ex", "from spec"));
    if (m.external) badges.appendChild(el("span", "badge inf", "DDL not on disk"));
    if (m.migration_system) {
      badges.appendChild(el("span", "badge", m.migration_system + " migration"));
    }
    if (m.declared_at) {
      var d = el("div", "ev mono");
      d.textContent = "declared at " + m.declared_at;
      badges.appendChild(d);
    }
    if (badges.childNodes.length) detail.appendChild(badges);

    if (n.contains && Object.keys(n.contains).length) {
      detail.appendChild(el("h3", null, "Contains"));
      var c = el("div", "kv");
      Object.keys(n.contains).sort().forEach(function (k) {
        c.appendChild(el("div", "k", k));
        c.appendChild(el("div", "v", String(n.contains[k])));
      });
      detail.appendChild(c);
    }

    if (n.metrics) {
      detail.appendChild(el("h3", null, "Change history"));
      var g = el("div", "kv");
      [["revisions", n.metrics.revisions], ["authors", n.metrics.authors],
       ["complexity", Math.round(n.metrics.complexity || 0)],
       ["hotspot", (n.metrics.hotspot || 0).toFixed(3)],
       ["main author", n.metrics.main_author || "-"],
       ["last change", (n.metrics.last_change || "").slice(0, 10)]
      ].forEach(function (kv) {
        g.appendChild(el("div", "k", kv[0]));
        g.appendChild(el("div", "v", String(kv[1])));
      });
      detail.appendChild(g);
      if (n.metrics.author_share >= 0.8 && n.metrics.revisions >= 3) {
        detail.appendChild(el("div", "badge warn",
          "bus factor 1 — " + n.metrics.main_author + " holds " +
          Math.round(n.metrics.author_share * 100) + "% of the history"));
      }
    }

    if (n.owners && n.owners.length) {
      detail.appendChild(el("h3", null, "Who to ask"));
      n.owners.forEach(function (o) {
        var r = el("div", "item");
        r.innerHTML = "<b>" + esc(o.author) + "</b> " +
          "<span class='muted'>" + o.commits + " commits, last " +
          esc((o.last || "").slice(0, 10)) + "</span>";
        detail.appendChild(r);
      });
    }

    if (n.coupled && n.coupled.length) {
      detail.appendChild(el("h3", null, "Changes together with"));
      n.coupled.forEach(function (c) {
        var other = c.a.indexOf(n.file) >= 0 ? c.b : c.a;
        var r = el("div", "item");
        r.innerHTML = "<span class='mono'>" + esc(other) + "</span>" +
          (c.cross_repo ? " <span class='badge warn'>cross-repo</span>" : "") +
          "<div class='ev'>" + c.shared + " shared commits · degree " +
          c.degree.toFixed(2) + "</div>";
        detail.appendChild(r);
      });
    }

    renderSide(detail, "Depends on", n.outgoing);
    renderSide(detail, "Depended on by", n.incoming);

    if (n.gaps && n.gaps.length) {
      detail.appendChild(el("h3", null, "Open questions here"));
      n.gaps.forEach(function (g) {
        var r = el("div", "item");
        r.innerHTML = "<div>" + esc(g.detail) + "</div>" +
          (g.hint ? "<div class='ev'>" + esc(g.hint) + "</div>" : "");
        detail.appendChild(r);
      });
    }

    var btns = el("div", "btnrow");
    var b1 = el("button", null, "blast radius");
    b1.onclick = function () {
      switchView("impact", n.name || n.file);
    };
    btns.appendChild(b1);
    if (n.kind === "service" || n.kind === "library") {
      var b2 = el("button", null, "open service");
      b2.onclick = function () {
        state.level = "service"; state.focus = n.name; state.pkg = null;
        switchView("map"); loadGraph();
      };
      btns.appendChild(b2);
    }
    var b3 = el("button", null, "copy for Claude");
    b3.onclick = function () {
      var lines = [
        (n.kind || "") + " " + (n.name || ""),
        n.file ? n.file + (n.line ? ":" + n.line : "") : "",
        n.service ? "service: " + n.service : ""
      ].filter(Boolean);
      (n.outgoing || []).slice(0, 20).forEach(function (o) {
        lines.push("  -> " + o.kind + " " + (o.other_label || "") +
                   (o.evidence ? "  [" + o.evidence + "]" : "") +
                   (o.provenance === "INFERRED" ? "  (inferred)" : ""));
      });
      (n.incoming || []).slice(0, 20).forEach(function (o) {
        lines.push("  <- " + o.kind + " " + (o.other_label || "") +
                   (o.evidence ? "  [" + o.evidence + "]" : "") +
                   (o.provenance === "INFERRED" ? "  (inferred)" : ""));
      });
      navigator.clipboard.writeText(lines.join("\n")).then(function () {
        b3.textContent = "copied";
        setTimeout(function () { b3.textContent = "copy for Claude"; }, 1400);
      });
    };
    btns.appendChild(b3);
    detail.appendChild(btns);
  }

  function renderSide(root, title, arr) {
    if (!arr || !arr.length) return;
    root.appendChild(el("h3", null, title + " (" + arr.length + ")"));
    var groups = {};
    arr.forEach(function (o) { (groups[o.kind] = groups[o.kind] || []).push(o); });
    Object.keys(groups).sort().forEach(function (kind) {
      var items = groups[kind];
      var h = el("div", "muted");
      h.style.cssText = "margin-top:6px;font-size:11px;text-transform:uppercase;" +
                        "letter-spacing:.5px";
      h.textContent = kind + " (" + items.length + ")";
      root.appendChild(h);
      items.slice(0, 25).forEach(function (o) {
        var r = el("div", "item");
        var a = el("span", "lbl", o.other_label || o.other_id || "?");
        if (o.other_id) a.onclick = function () { select(o.other_id); };
        r.appendChild(a);
        if (o.provenance === "INFERRED") {
          r.appendChild(el("span", "badge inf", "inferred"));
        }
        if (o.runtime) r.appendChild(el("span", "badge rt", "runtime"));
        if (o.via) r.appendChild(el("div", "ev", o.via));
        if (o.evidence) r.appendChild(el("div", "ev mono", o.evidence));
        root.appendChild(r);
      });
      if (items.length > 25) {
        root.appendChild(el("div", "muted", "…and " + (items.length - 25) + " more"));
      }
    });
  }

  /* ---------------- edge inspection ---------------- */

  function showEdge(e) {
    detail.innerHTML = "<p class='muted'>loading citations…</p>";
    var q = "/api/edge?source=" + encodeURIComponent(e.source) +
            "&target=" + encodeURIComponent(e.target) +
            "&kind=" + encodeURIComponent(e.kind) +
            "&level=" + encodeURIComponent(state.level);
    if (state.focus) q += "&focus=" + encodeURIComponent(state.focus);
    api(q).then(function (d) {
      detail.innerHTML = "";
      detail.appendChild(el("h2", null, e.kind));
      detail.appendChild(el("div", "sub",
        shortId(e.source) + "  →  " + shortId(e.target)));
      var b = el("div");
      b.appendChild(el("span", "badge " +
        (e.provenance === "EXTRACTED" ? "ex" : "inf"),
        e.provenance === "EXTRACTED" ? "extracted" : "inferred"));
      if (e.runtime) {
        b.appendChild(el("span", "badge rt",
          "runtime-confirmed" + (e.calls ? " · " +
            Number(e.calls).toLocaleString() + " calls" : "")));
      }
      b.appendChild(el("span", "badge", d.count + " underlying"));
      detail.appendChild(b);

      if (e.provenance === "INFERRED") {
        var w = el("p", "muted");
        w.textContent = "Inferred means name-matched, not scope-resolved. " +
          "Treat each line below as a lead to verify, not a fact.";
        detail.appendChild(w);
      }

      detail.appendChild(el("h3", null, "Evidence"));
      d.edges.forEach(function (x) {
        var r = el("div", "item");
        var head = el("div");
        head.innerHTML = "<span class='mono'>" + esc(x.src_label || "") +
          "</span> → <span class='mono'>" + esc(x.dst_label || "") + "</span>";
        r.appendChild(head);
        if (x.via) r.appendChild(el("div", "ev", x.via));
        if (x.evidence) r.appendChild(el("div", "ev mono", x.evidence));
        if (x.provenance === "INFERRED") {
          r.appendChild(el("span", "badge inf", "inferred"));
        }
        detail.appendChild(r);
      });
      if (d.count > d.edges.length) {
        detail.appendChild(el("div", "muted",
          "…and " + (d.count - d.edges.length) + " more"));
      }
    });
  }

  function shortId(id) {
    if (id.indexOf("pkg:") === 0) return id.split(":").slice(2).join(":");
    var p = id.split(" ");
    return p.length >= 4 ? p.slice(3).join(" ") : id;
  }

  /* ---------------- tooltip ---------------- */

  function showTip(n, e, mx, my) {
    if (!n && !e) { tooltip.hidden = true; return; }
    var html = "";
    if (n) {
      html = "<div class='t'>" + esc(n.label) + "</div>" +
        "<div class='muted'>" + esc(n.kind) +
        (n.service && n.service !== n.label ? " · " + esc(n.service) : "") + "</div>";
      if (n.size) {
        html += "<div class='muted'>" + n.size + " items inside" +
          (n.drillable ? " — double-click to open" : "") + "</div>";
      }
      if (n.file) html += "<div class='muted mono'>" + esc(n.file) + "</div>";
    } else {
      html = "<div class='t'>" + esc(e.kind) + " × " + (e.count || 1) + "</div>" +
        "<div class='muted'>" + esc(shortId(e.source)) + " → " +
        esc(shortId(e.target)) + "</div>" +
        "<div class='muted'>" +
        (e.provenance === "INFERRED" ? "inferred — verify" : "extracted") +
        (e.runtime ? " · runtime-confirmed" : "") + "</div>" +
        "<div class='muted'>click for the exact file:line</div>";
    }
    tooltip.innerHTML = html;
    tooltip.hidden = false;
    var r = stage.getBoundingClientRect();
    var x = Math.min(mx + 14, r.width - tooltip.offsetWidth - 8);
    var y = Math.min(my + 14, r.height - tooltip.offsetHeight - 8);
    tooltip.style.left = Math.max(4, x) + "px";
    tooltip.style.top = Math.max(4, y) + "px";
  }

  /* ---------------- views ---------------- */

  /**
   * Wrapper so the URL stays in step however switchView exits. The
   * implementation has several early returns (one per view family) and
   * patching each of them is how one path ends up forgotten.
   */
  function switchView(v, arg) {
    switchViewImpl(v, arg);
    writeHash();
    showFrozen();
  }

  function switchViewImpl(v, arg) {
    state.view = v;
    document.querySelectorAll("#views button").forEach(function (b) {
      b.classList.toggle("on", b.dataset.view === v);
    });
    var pane = document.getElementById("viewpane");
    if (pane) pane.remove();
    tooltip.hidden = true;

    var graphy = (v === "map" || v === "layers");
    cv.style.display = graphy ? "block" : "none";
    $("#stagectl").style.display = graphy ? "flex" : "none";
    $("#legendPanel").style.display = graphy ? "block" : "none";
    $("#filters").parentElement.style.display = graphy ? "block" : "none";
    // Layer navigation only means anything in the 3D stack.
    $("#layerPanel").hidden = (v !== "layers");

    if (v === "map") {
      // Exactly one engine may be active: they share a canvas, and the
      // inactive one must not respond to input or paint a frame over the other.
      g3.active = false; g2.active = true;
      g3.stop(); g2.resize(); g2.start();
      // Returning to Map: put the caller back where they were, once.
      var back = state._savedNav;
      state._savedNav = null;
      if (back) {
        state.level = back.level; state.focus = back.focus; state.pkg = back.pkg;
        loadGraph().then(function () {
          if (back.selected) select(back.selected);
        }, function () {});
      } else if (state.lastGraph) {
        g2.setData(state.lastGraph);
      } else {
        loadGraph();
      }
      $("#viewhint").textContent = "drag to pan · scroll to zoom · " +
        "double-click a ringed node to open it";
      return;
    }
    if (v === "layers") {
      // Set before loadGraph: setData starts the loop, and draw() no-ops
      // while inactive, so activating afterwards would render nothing.
      g2.active = false; g3.active = true;
      g2.stop(); g3.resize();
      // The layered stack only makes sense at estate level -- but discarding
      // the caller's position outright means a glance at Layers costs you the
      // drill-down you were in the middle of, with no way back. Stash it and
      // restore when Map is next opened.
      if (state.level !== "estate") {
        state._savedNav = { level: state.level, focus: state.focus,
                            pkg: state.pkg, selected: state.selected };
      }
      state.level = "estate"; state.focus = null; state.pkg = null;
      loadGraph();
      $("#viewhint").textContent =
        "drag to orbit · right-drag to pan · scroll to zoom · " +
        "↑↓ to move between layers — click a layer name on the left to " +
        "point the camera at it";
      return;
    }

    // A non-graph tab: the canvas is hidden, so neither engine should draw
    // or claim the shared mouse events.
    g2.active = false; g3.active = false;
    g2.stop(); g3.stop();
    pane = el("div", "viewpane");
    pane.id = "viewpane";
    stage.appendChild(pane);
    var pick = function (id) { switchView("map"); setTimeout(function () {
      select(id);
    }, 60); };
    if (v === "arch") Views.architecture(pane, api, pick);
    else if (v === "data") Views.data(pane, api, pick, arg);
    else if (v === "flow") Views.flow(pane, api, pick, arg);
    else if (v === "impact") Views.impact(pane, api, pick, arg);
    else if (v === "time") Views.time(pane, api);
    else if (v === "coverage") Views.coverage(pane, api);
    $("#viewhint").textContent = "";
  }

  /* ---------------- chrome ---------------- */

  function buildFilters() {
    var f = $("#filters");
    f.innerHTML = "";
    EDGE_GROUPS.forEach(function (g) {
      var lab = el("label");
      var cb = el("input");
      cb.type = "checkbox";
      cb.checked = state.filters[g[0]];
      cb.onchange = function () {
        state.filters[g[0]] = cb.checked;
        engine().draw();
      };
      var sw = el("span", "swatch");
      sw.style.background = "var(" + g[2] + ")";
      lab.appendChild(cb); lab.appendChild(sw);
      lab.appendChild(document.createTextNode(g[1]));
      f.appendChild(lab);
    });
  }

  function buildLegend() {
    var L = $("#legend");
    L.innerHTML = "";
    [["service", "service"], ["library", "shared library"],
     ["topic", "topic / event"], ["datastore", "datastore"],
     ["table", "table"], ["host", "host"], ["infra", "infrastructure"],
     ["legacy-page", "legacy page"], ["package", "package"], ["file", "file"]
    ].forEach(function (k) {
      var d = el("div", "li");
      var dot = el("span", "dot");
      dot.style.background = "var(" + (KIND_COLOR[k[0]] || "--code") + ")";
      d.appendChild(dot);
      d.appendChild(document.createTextNode(k[1]));
      L.appendChild(d);
    });
    var extra = el("div");
    extra.style.cssText = "margin-top:8px;font-size:11px;color:var(--muted);line-height:1.7";
    extra.innerHTML =
      "dotted ring — can be opened<br>" +
      "solid line — synchronous<br>" +
      "dashed line — event, or inferred<br>" +
      "thicker line — more underlying connections<br>" +
      "line width also grows when confirmed by traces";
    L.appendChild(extra);
  }

  function loadStats() {
    api("/api/stats").then(function (c) {
      var g = $("#gstats");
      g.innerHTML = "";
      var rows = [
        ["nodes", c.nodes], ["edges", c.edges],
        ["extracted", (c.edges_by_provenance || {}).EXTRACTED || 0],
        ["inferred", (c.edges_by_provenance || {}).INFERRED || 0],
        ["repos", (c.repos || []).length],
        ["open gaps", c.gaps]
      ];
      rows.forEach(function (r) {
        g.appendChild(el("div", "k", r[0]));
        g.appendChild(el("div", "v", String(r[1])));
      });
      var when = (c.meta && c.meta.scanned_at) || "";
      var stat = $("#status");
      stat.textContent = when ? "scanned " + fmtWhen(when) : "";
      // Canonical UTC plus relative age on hover: the label answers "when",
      // the tooltip answers "is this stale?" without spending header width.
      stat.title = when
        ? when + " (UTC)  ·  " + relAge(when) + "\nGraph freshness"
        : "Graph freshness";
      var fs2 = $("#footStamp");
      if (fs2) {
        fs2.textContent = when
          ? "graph " + fmtWhen(when) + " · " + relAge(when) : "";
      }
    }).catch(function () {});
  }

  /* ---------------- search ---------------- */

  var results = $("#results"), qbox = $("#q"), searchTimer = null, sel = -1;

  qbox.addEventListener("input", function () {
    clearTimeout(searchTimer);
    var v = qbox.value.trim();
    if (!v) { results.hidden = true; return; }
    searchTimer = setTimeout(function () {
      api("/api/search?q=" + encodeURIComponent(v)).then(function (d) {
        results.innerHTML = "";
        sel = -1;
        if (!d.results.length) {
          results.innerHTML = "<div class='r muted'>no match</div>";
        }
        d.results.forEach(function (r) {
          var row = el("div", "r");
          row.innerHTML = "<div>" +
            (r.container ? "<span class='muted'>" + esc(r.container) + ".</span>" : "") +
            "<b>" + esc(r.name || (r.file || "").split("/").pop()) + "</b></div>" +
            "<div class='k'>" + esc(r.kind) +
            (r.service ? " · " + esc(r.service) : "") + "</div>" +
            (r.file ? "<div class='muted mono' style='font-size:11px'>" +
             esc(r.file) + (r.line ? ":" + r.line : "") + "</div>" : "");
          row.onclick = function () {
            results.hidden = true; qbox.blur();
            if (state.view !== "map") switchView("map");
            select(r.id);
            setTimeout(function () { g2.centerOn(r.id, true); }, 80);
          };
          results.appendChild(row);
        });
        results.hidden = false;
      });
    }, 160);
  });

  qbox.addEventListener("keydown", function (ev) {
    var rows = results.querySelectorAll(".r");
    if (ev.key === "Escape") { results.hidden = true; qbox.blur(); return; }
    if (!rows.length) return;
    if (ev.key === "ArrowDown" || ev.key === "ArrowUp") {
      ev.preventDefault();
      sel = Math.max(0, Math.min(rows.length - 1,
        sel + (ev.key === "ArrowDown" ? 1 : -1)));
      rows.forEach(function (r, i) { r.classList.toggle("sel", i === sel); });
      rows[sel].scrollIntoView({ block: "nearest" });
    } else if (ev.key === "Enter" && sel >= 0) {
      rows[sel].click();
    }
  });

  document.addEventListener("click", function (ev) {
    if (!ev.target.closest(".search")) results.hidden = true;
  });

  /* ---------------- wiring ---------------- */

  document.querySelectorAll("#views button").forEach(function (b) {
    b.onclick = function () { switchView(b.dataset.view); };
  });

  /**
   * URL routing for the whole UI.
   *
   * Two directions, and both matter:
   *
   *   inbound  -- `#view=data&focus=T_ORDER` opens that tab on that row, so a
   *               finding can be *shown* rather than described. A file:line
   *               proves a claim; it does not convey shape.
   *   outbound -- every navigation rewrites the hash, so whatever you are
   *               looking at is already a copyable link. Without this half,
   *               deep links are something only the author can produce, which
   *               in practice means nobody uses them.
   *
   * Recognised keys: `view`, `focus`, `level`, `pkg`. Unknown views and
   * missing ids are ignored rather than thrown -- a stale link should open
   * the app, not break it.
   */
  var VIEWS = ["map", "layers", "arch", "data", "flow", "impact", "time", "coverage"];
  var hashLock = false;   // set while WE rewrite it, so we do not re-route

  function writeHash() {
    if (hashLock) return;
    var parts = ["view=" + state.view];
    if (state.level && state.level !== "estate") parts.push("level=" + state.level);
    if (state.pkg) parts.push("pkg=" + encodeURIComponent(state.pkg));
    if (state.selected) parts.push("focus=" + encodeURIComponent(state.selected));
    else if (state.focus) parts.push("focus=" + encodeURIComponent(state.focus));
    var next = "#" + parts.join("&");
    if (next === location.hash) return;
    hashLock = true;
    try {
      // replaceState, not assignment: a graph tool generates a lot of
      // navigation and filling the back stack with every click makes the
      // browser Back button useless.
      history.replaceState(null, "", next);
    } catch (e) {
      location.hash = next;      // file:// and other no-history contexts
    }
    hashLock = false;
  }

  function parseHash() {
    var h = (location.hash || "").replace(/^#/, "");
    if (!h) return null;
    var q = {};
    h.split("&").forEach(function (kv) {
      var i = kv.indexOf("=");
      if (i > 0) {
        q[kv.slice(0, i)] =
          decodeURIComponent(kv.slice(i + 1).replace(/\+/g, " "));
      }
    });
    return q;
  }

  function applyHash() {
    if (hashLock) return false;
    var q = parseHash();
    if (!q) return false;
    var v = q.view && VIEWS.indexOf(q.view) >= 0 ? q.view : null;
    if (!v && !q.focus) return false;

    if (q.level) state.level = q.level;
    if (q.pkg) state.pkg = q.pkg;

    // flow/impact/data take their subject as the switchView argument; the
    // graph views resolve it against the loaded engine instead.
    if (v) switchView(v, q.focus || undefined);

    if (q.focus && (state.view === "map" || state.view === "layers")) {
      // The graph loads asynchronously; wait for the node to exist rather
      // than racing the fetch, and give up quietly so a bad id does not poll
      // forever.
      var tries = 0;
      var seek = setInterval(function () {
        tries++;
        var has = (state.view === "layers")
          ? (g3.byId && g3.byId[q.focus])
          : (g2.byId && g2.byId[q.focus]);
        if (has) {
          clearInterval(seek);
          select(q.focus);
          if (state.view === "layers") g3.focusNode(q.focus);
          else g2.centerOn(q.focus, true);
        } else if (tries > 40) {
          clearInterval(seek);
          console.warn("cartographer: no node '" + q.focus + "' in this view");
        }
      }, 100);
    }
    return true;
  }

  window.addEventListener("hashchange", applyHash);
  // Both engines now implement zoomBy with the same convention (>1 = closer)
  // and both clamp. The old form tested `engine().zoomBy` and then always
  // called it on g2, which would have zoomed the wrong engine the moment
  // Graph3D gained the method.
  $("#zoomIn").onclick = function () { engine().zoomBy(1.25); };
  $("#zoomOut").onclick = function () { engine().zoomBy(0.8); };
  $("#fit").onclick = function () {
    // Frames whatever is currently visible, including the pivot -- the old
    // version only reset distance and pan, so a camera pointed at one layer
    // stayed pointed there and "fit" appeared to do nothing.
    if (state.view === "layers") g3.fitAll();
    else g2.fit();
  };

  $("#allLayers").onclick = function () {
    g3.hiddenLayers = {};
    g3.fitAll();
    g3.draw();
    drawLayerList();
  };
  $("#reset").onclick = resetView;

  var scPanel = $("#shortcuts");
  function toggleHelp(show) {
    scPanel.hidden = (show === undefined) ? !scPanel.hidden : !show;
  }
  $("#helpBtn").onclick = function () { toggleHelp(true); };
  scPanel.onclick = function () { toggleHelp(false); };

  /**
   * Freeze is a toggle with no visible state, and it is bound to Space --
   * easy to hit by accident, after which the layout silently stops relaxing
   * and the view looks stuck for reasons the user cannot see. Show it.
   */
  function showFrozen() {
    var on = (state.view === "layers") ? !g3.spin : g2.frozen;
    var b = $("#freeze");
    b.classList.toggle("on", !!on);
    b.title = on
      ? (state.view === "layers" ? "Spin paused (Space)" : "Layout paused (Space)")
      : "Pause/resume layout (Space)";
  }

  $("#freeze").onclick = function () {
    if (state.view === "layers") { g3.spin = !g3.spin; showFrozen(); return; }
    g2.frozen = !g2.frozen;
    if (!g2.frozen) g2.alpha = Math.max(g2.alpha, 0.3);
    showFrozen();
  };
  $("#crossRepo").onchange = function () {
    state.crossRepo = this.checked;
    loadGraph(true);
  };
  $("#simplify").onchange = function () {
    state.simplify = this.checked;
    loadGraph(true);        // keepView: this is a filter, not a navigation
  };
  $("#hideInferred").onchange = function () {
    state.hideInferred = this.checked; engine().draw();
  };
  $("#hideLibs").onchange = function () {
    state.hideLibs = this.checked; loadGraph(true);
  };
  $("#colorByRepo").onchange = function () {
    state.colorByRepo = this.checked;
    loadGraph(true).then(drawRepoList);
  };

  /* Repo checkboxes, rebuilt from whatever the current view actually contains
     rather than a fixed list, so drilling into a service does not offer repos
     that cannot appear at that level. */
  /**
   * The layer navigator: one row per architectural floor.
   *
   * Two separate jobs, deliberately kept in one row. The name is a button that
   * moves the camera pivot to that layer -- without it, orbiting a zoomed-in
   * view swings the top and bottom floors out of frame and they are simply
   * unreachable. The checkbox hides the layer, which is the only thing that
   * makes a 1219-node estate readable: looking at one floor at a time turns a
   * hairball into something you can actually read.
   */
  function drawLayerList() {
    var box = $("#layerList");
    if (!box || !window.GRAPH3D_LAYERS) return;
    var names = window.GRAPH3D_LAYERS;
    box.innerHTML = "";
    names.forEach(function (label, L) {
      var count = g3.nodes.filter(function (n) { return n.L === L; }).length;
      var row = document.createElement("div");
      row.className = "layerrow" + (count ? "" : " empty");

      var cb = document.createElement("input");
      cb.type = "checkbox";
      cb.checked = g3.layerVisible(L);
      cb.disabled = !count;
      cb.title = count ? "Show/hide this layer" : "No nodes on this layer";
      cb.onchange = function () {
        g3.hiddenLayers[L] = !cb.checked;
        g3._adjFor = null;
        g3.draw();
        drawLayerList();
      };

      var name = document.createElement("button");
      name.className = "layername";
      name.textContent = label;
      name.disabled = !count;
      name.title = count ? "Point the camera at this layer" : "No nodes here";
      name.onclick = function () {
        g3.hiddenLayers[L] = false;
        g3.focusLayer(L);
        g3.draw();
        drawLayerList();
      };

      var num = document.createElement("span");
      num.className = "layercount";
      num.textContent = count ? String(count) : "—";

      // "Only" is the fast path: isolating a floor is the common intent, and
      // doing it with checkboxes means four clicks instead of one.
      var solo = document.createElement("button");
      solo.className = "layersolo";
      solo.textContent = "only";
      solo.disabled = !count;
      solo.title = "Show only this layer";
      solo.onclick = function () {
        names.forEach(function (_x, i) { g3.hiddenLayers[i] = (i !== L); });
        g3.focusLayer(L);
        g3.draw();
        drawLayerList();
      };

      row.appendChild(cb);
      row.appendChild(name);
      row.appendChild(num);
      row.appendChild(solo);
      box.appendChild(row);
    });
  }

  function drawRepoList() {
    var box = $("#repoList");
    if (!box) return;
    var repos = state.repoList || [];
    box.innerHTML = "";
    if (repos.length < 2) {
      box.innerHTML = "<div class='hint'>one repo in this view</div>";
      return;
    }
    repos.forEach(function (r) {
      var lab = document.createElement("label");
      lab.className = "row";
      var cb = document.createElement("input");
      cb.type = "checkbox";
      cb.checked = !state.repoFilter || !!state.repoFilter[r];
      cb.onchange = function () {
        var f = {};
        Array.prototype.forEach.call(box.querySelectorAll("input"),
          function (i, idx) { if (i.checked) f[repos[idx]] = 1; });
        var on = Object.keys(f);
        // All boxes ticked is the same as no filter; keep null so the
        // unattributed-node rule below does not start dropping shared nodes.
        state.repoFilter = (on.length === repos.length || !on.length) ? null : f;
        loadGraph(true);
      };
      lab.appendChild(cb);
      if (state.colorByRepo) {
        var sw = document.createElement("span");
        sw.style.cssText = "display:inline-block;width:9px;height:9px;" +
          "border-radius:2px;margin:0 5px;background:" + repoColor(r);
        lab.appendChild(sw);
      }
      lab.appendChild(document.createTextNode(" " + r));
      box.appendChild(lab);
    });
  }
  $("#themeBtn").onclick = function () {
    var cur = document.documentElement.getAttribute("data-theme");
    var next = cur === "dark" ? "light" : (cur === "light" ? null : "dark");
    if (next) document.documentElement.setAttribute("data-theme", next);
    else document.documentElement.removeAttribute("data-theme");
    try { localStorage.setItem("cart-theme", next || ""); } catch (e) {}
    engine().draw();
  };
  try {
    var th = localStorage.getItem("cart-theme");
    if (th) document.documentElement.setAttribute("data-theme", th);
  } catch (e) {}

  window.addEventListener("resize", function () {
    g2.resize(); g3.resize(); engine().draw();
  });

  document.addEventListener("keydown", function (ev) {
    if (ev.target.tagName === "INPUT") return;
    if (ev.key === "/") { ev.preventDefault(); qbox.focus(); }
    else if (ev.key === "Escape") {
      if (!scPanel.hidden) toggleHelp(false); else clearSelection();
    }
    else if (ev.key === "?") { ev.preventDefault(); toggleHelp(); }
    else if (ev.key === "r" || ev.key === "R") resetView();
    else if ((ev.key === "ArrowUp" || ev.key === "ArrowDown") &&
             state.view === "layers") {
      // Step the camera between floors. Layer 0 is the top of the stack
      // (smallest world Y), so "up" means the previous index.
      ev.preventDefault();
      var names = window.GRAPH3D_LAYERS || [];
      var ys = window.GRAPH3D_LAYER_Y || [];
      var cur = 0, best = Infinity;
      ys.forEach(function (y, i) {
        var d = Math.abs(y - g3.ty);
        if (d < best) { best = d; cur = i; }
      });
      var next = cur + (ev.key === "ArrowUp" ? -1 : 1);
      // Skip empty floors rather than parking the camera on nothing.
      while (next >= 0 && next < names.length &&
             !g3.nodes.some(function (n) { return n.L === next; })) {
        next += (ev.key === "ArrowUp" ? -1 : 1);
      }
      if (next >= 0 && next < names.length) {
        g3.hiddenLayers[next] = false;
        g3.focusLayer(next);
        g3.draw();
        drawLayerList();
      }
    }
    else if (ev.key === " ") { ev.preventDefault(); $("#freeze").click(); }
    else if (ev.key === "f") $("#fit").click();
    else if (ev.key === "Backspace") {
      var bc = state.lastGraph && state.lastGraph.breadcrumb;
      if (bc && bc.length > 1) {
        var up = bc[bc.length - 2];
        state.level = up.level; state.focus = up.focus || null;
        state.pkg = up.package || null;
        loadGraph();
      }
    }
  });

  // Pick up a rescan without a reload: the graph file changing on disk is the
  // signal, so `cartographer scan` in another terminal just works.
  setInterval(function () {
    api("/api/ping").then(function (p) {
      if (!state.mtime) { state.mtime = p.mtime; return; }
      if (p.mtime && p.mtime !== state.mtime) {
        state.mtime = p.mtime;
        $("#status").classList.add("stale");
        $("#status").textContent = "graph changed — reloading";
        loadStats();
        if (state.view === "map" || state.view === "layers") loadGraph(true);
        else switchView(state.view);
        setTimeout(function () { $("#status").classList.remove("stale"); }, 2500);
      }
    }).catch(function () {});
  }, 2500);

  // Footer: the year comes from the clock so it does not silently go stale,
  // and the scan stamp is repeated here because the header one scrolls out of
  // reach on a narrow window.
  (function () {
    var y = $("#footYear");
    if (y) y.textContent = String(new Date().getFullYear());
  })();

  buildFilters();
  buildLegend();
  loadStats();
  g2.resize(); g3.resize();
  clearSelection();
  loadGraph();
  // Honour a deep link on first paint. Runs after loadGraph so the default
  // view is already in flight; applyHash waits for the node to exist before
  // selecting it.
  applyHash();
})();
