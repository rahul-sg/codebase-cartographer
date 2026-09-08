/* Main application: view routing, drill-down, selection, detail panel. */
(function () {
  "use strict";

  var $ = function (s) { return document.querySelector(s); };
  var stage = $("#stage"), cv = $("#cv"), detail = $("#detail"),
      tooltip = $("#tooltip"), crumbs = $("#crumbs"), overlay = $("#overlay");

  var state = {
    view: "map",
    level: "estate",
    focus: null,
    pkg: null,
    selected: null,
    filters: {},          // edge style-kind -> bool
    hideInferred: false,
    hideLibs: true,
    mtime: 0
  };

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
      var keep = {};
      nodes.forEach(function (n) { keep[n.id] = 1; });
      var edges = d.edges.filter(function (e) {
        return keep[e.source] && keep[e.target];
      });
      state.lastGraph = { nodes: nodes, edges: edges, breadcrumb: d.breadcrumb,
                          stats: d.stats, truncated: d.truncated || 0 };
      engine().setData(state.lastGraph, keepView);
      drawCrumbs(d.breadcrumb);
      var hint = d.stats.nodes + " nodes · " + d.stats.edges + " connections";
      if (d.stats.rolled_up) {
        hint += "  ·  " + d.stats.rolled_up.toLocaleString() +
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
    g2.selected = null; g3.selected = null;
    g2.setHighlight(null);
    detail.innerHTML = "<div class='placeholder'><h3>Nothing selected</h3>" +
      "<p>Click a node to inspect it. Double-click to drill in. " +
      "Click an edge to see the exact <code>file:line</code> that proves it.</p></div>";
  }

  function select(id) {
    state.selected = id;
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

  function switchView(v, arg) {
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

    if (v === "map") {
      g3.stop(); g2.resize(); g2.start();
      if (state.lastGraph) g2.setData(state.lastGraph);
      else loadGraph();
      $("#viewhint").textContent = "drag to pan · scroll to zoom · " +
        "double-click a ringed node to open it";
      return;
    }
    if (v === "layers") {
      g2.stop(); g3.resize();
      // The layered stack only makes sense at estate level.
      state.level = "estate"; state.focus = null; state.pkg = null;
      loadGraph();
      $("#viewhint").textContent =
        "drag to orbit · shift-drag to pan · scroll to zoom — " +
        "good for seeing the SHAPE of the estate; use Map to trace a path, " +
        "where edges do not occlude each other";
      return;
    }

    g2.stop(); g3.stop();
    pane = el("div", "viewpane");
    pane.id = "viewpane";
    stage.appendChild(pane);
    var pick = function (id) { switchView("map"); setTimeout(function () {
      select(id);
    }, 60); };
    if (v === "data") Views.data(pane, api, pick);
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
      $("#status").textContent = when ? "scanned " + when.slice(0, 16).replace("T", " ") : "";
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
  $("#zoomIn").onclick = function () { engine().zoomBy ? g2.zoomBy(1.25) : (g3.dist /= 1.2); };
  $("#zoomOut").onclick = function () { engine().zoomBy ? g2.zoomBy(0.8) : (g3.dist *= 1.2); };
  $("#fit").onclick = function () {
    if (state.view === "layers") { g3.dist = 1250; g3.cx = g3.cy = 0; }
    else g2.fit();
  };
  $("#freeze").onclick = function () {
    if (state.view === "layers") { g3.spin = !g3.spin; return; }
    g2.frozen = !g2.frozen;
    if (!g2.frozen) g2.alpha = Math.max(g2.alpha, 0.3);
  };
  $("#hideInferred").onchange = function () {
    state.hideInferred = this.checked; engine().draw();
  };
  $("#hideLibs").onchange = function () {
    state.hideLibs = this.checked; loadGraph(true);
  };
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
    else if (ev.key === "Escape") clearSelection();
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

  buildFilters();
  buildLegend();
  loadStats();
  g2.resize(); g3.resize();
  clearSelection();
  loadGraph();
})();
