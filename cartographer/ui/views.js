/* The non-graph views.
 *
 * Some relationships read far better as a table than as a node-link diagram.
 * Data ownership is the clearest example: "which services touch which tables"
 * is a matrix question, and drawing it as a graph hides exactly the thing you
 * want to see -- the columns with more than one mark in them.
 */
(function (global) {
  "use strict";

  var V = {};

  function el(tag, cls, text) {
    var e = document.createElement(tag);
    if (cls) e.className = cls;
    if (text != null) e.textContent = text;
    return e;
  }
  function esc(s) {
    return String(s == null ? "" : s).replace(/[&<>"]/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c];
    });
  }

  /* ---------------- data ownership matrix ---------------- */

  V.data = function (pane, api, onPick, focus) {
    pane.innerHTML = "";
    pane.appendChild(el("h2", null, "Data ownership"));
    var lead = el("p", "lead");
    lead.innerHTML =
      "Which service <b>declares</b>, <b>writes</b> and <b>reads</b> each table. " +
      "With no ORM in this codebase there is no other way to see this. " +
      "A column with more than one mark is a coupling no API contract " +
      "documents &mdash; two services on one table are bound together whatever " +
      "the interfaces say.";
    pane.appendChild(lead);

    api("/api/schema").then(function (d) {
      if (!d.tables || !d.tables.length) {
        pane.appendChild(el("p", "muted",
          "No tables in the graph. Either this codebase has no SQL on disk, " +
          "or its DDL lives in a repo you have not scanned."));
        return;
      }

      var shared = d.tables.filter(function (t) { return t.touched_by.length > 1; });
      // Two services WRITING one table with no agreed owner is a different,
      // sharper problem than two services merely reading it: there is no
      // contract saying who wins. Call it out separately.
      var multiW = d.tables.filter(function (t) {
        return (t.writers || []).length > 1;
      });
      var sum = el("div");
      sum.innerHTML =
        "<span class='pill'>" + d.tables.length + " tables</span>" +
        "<span class='pill'>" + d.services.length + " services touch data</span>" +
        "<span class='pill'" + (shared.length ? " style='background:var(--warn);color:#fff'" : "") +
        ">" + shared.length + " shared by 2+ services</span>" +
        "<span class='pill'" + (multiW.length ? " style='background:#c62828;color:#fff'" : "") +
        ">" + multiW.length + " with multiple writers</span>";
      pane.appendChild(sum);

      var only = el("label", "row");
      only.style.cssText = "margin-top:8px;display:inline-flex;align-items:center;gap:6px";
      var cb = el("input");
      cb.type = "checkbox";
      only.appendChild(cb);
      only.appendChild(document.createTextNode(
        "only tables with multiple writers (" + multiW.length + ")"));
      pane.appendChild(only);
      if (multiW.length) {
        var why = el("p", "muted");
        why.style.cssText = "margin:6px 0 0";
        why.textContent = "Each of these is written by 2+ services with no " +
          "documented owner. Ask the team who owns the write path before " +
          "changing any of them.";
        pane.appendChild(why);
      }

      var wrap = el("div");
      wrap.style.cssText = "overflow:auto;max-height:calc(100vh - 260px);margin-top:14px";
      var t = el("table", "grid");

      var head = el("tr");
      head.appendChild(el("th", null, "table"));
      head.appendChild(el("th", null, "declared in"));
      d.services.forEach(function (s) {
        var th = el("th");
        th.style.cssText = "writing-mode:vertical-rl;transform:rotate(180deg);" +
                           "max-height:150px;font-weight:600";
        th.textContent = s;
        head.appendChild(th);
      });
      t.appendChild(head);

      // Findings first, and multiple writers ahead of merely-shared: neither
      // should be buried alphabetically halfway down 1,400 rows.
      var rows = d.tables.slice().sort(function (a, b) {
        var aw = (a.writers || []).length > 1 ? 1 : 0;
        var bw = (b.writers || []).length > 1 ? 1 : 0;
        return (bw - aw)
            || (b.touched_by.length - a.touched_by.length)
            || a.name.localeCompare(b.name);
      });

      cb.onchange = function () {
        var on = this.checked;
        Array.prototype.forEach.call(t.querySelectorAll("tr[data-multiw]"),
          function (tr) {
            tr.style.display = (on && tr.getAttribute("data-multiw") === "0")
              ? "none" : "";
          });
      };

      rows.forEach(function (tb) {
        var tr = el("tr");
        var td = el("td");
        var a = el("span", "lbl");
        a.textContent = tb.name;
        a.style.cssText = "cursor:pointer;color:var(--accent)";
        a.onclick = function () { onPick(tb.id); };
        td.appendChild(a);
        tr.setAttribute("data-multiw",
                        (tb.writers || []).length > 1 ? "1" : "0");
        // Tagged so `#view=data&focus=<table>` can scroll to a specific row.
        tr.setAttribute("data-table", tb.name);
        tr.setAttribute("data-id", tb.id);
        if (tb.touched_by.length > 1) {
          var b = el("span", "badge warn", tb.touched_by.length + " services");
          b.style.marginLeft = "6px";
          td.appendChild(b);
        }
        if ((tb.writers || []).length > 1) {
          var mw = el("span", "badge",
                      (tb.writers.length) + " writers: " + tb.writers.join(", "));
          mw.style.cssText = "margin-left:6px;background:#c62828;color:#fff";
          mw.title = "No documented owner for the write path";
          td.appendChild(mw);
        }
        if (tb.external) {
          td.appendChild(el("span", "badge inf", "DDL not on disk"));
        }
        tr.appendChild(td);

        var src = el("td");
        src.innerHTML = tb.declared_at
          ? "<span class='mono' style='font-size:11px'>" +
            esc(String(tb.declared_at).split("/").slice(-1)[0]) + "</span>" +
            (tb.migration ? " <span class='pill'>" + esc(tb.migration) + "</span>" : "")
          : "<span class='muted'>elsewhere</span>";
        tr.appendChild(src);

        d.services.forEach(function (s) {
          var cell = el("td", "c");
          var marks = [];
          if (tb.declared_by.indexOf(s) >= 0) marks.push(["O", "cellO", "declares the table"]);
          if (tb.writers.indexOf(s) >= 0) marks.push(["W", "cellW", "writes"]);
          if (tb.readers.indexOf(s) >= 0) marks.push(["R", "cellR", "reads"]);
          if (marks.length) {
            cell.innerHTML = marks.map(function (m) {
              return "<span class='" + m[1] + "' title='" + m[2] + "'>" + m[0] + "</span>";
            }).join("");
          }
          tr.appendChild(cell);
        });
        t.appendChild(tr);
      });

      // Deep link into one table: scroll it into view and mark it, rather
      // than leaving the reader to find one row among fourteen hundred.
      if (focus) {
        var want = String(focus).toLowerCase();
        var hit = null;
        Array.prototype.forEach.call(t.querySelectorAll("tr[data-table]"),
          function (tr) {
            if (hit) return;
            var nm = (tr.getAttribute("data-table") || "").toLowerCase();
            var id = (tr.getAttribute("data-id") || "").toLowerCase();
            if (nm === want || id === want || want.indexOf(nm) >= 0) hit = tr;
          });
        if (hit) {
          hit.style.outline = "2px solid var(--sel)";
          hit.style.background = "color-mix(in srgb, var(--sel) 12%, transparent)";
          setTimeout(function () {
            hit.scrollIntoView({ block: "center" });
          }, 60);
        }
      }
      wrap.appendChild(t);
      pane.appendChild(wrap);

      var key = el("p", "muted");
      key.style.marginTop = "10px";
      key.innerHTML = "<span class='cellO'>O</span> declares the DDL &nbsp; " +
        "<span class='cellW'>W</span> writes &nbsp; " +
        "<span class='cellR'>R</span> reads &nbsp;&mdash;&nbsp; " +
        "read from migration SQL and from SQL strings inside DAOs. " +
        "Queries assembled through a QueryBuilder rather than as literals are " +
        "under-reported here.";
      pane.appendChild(key);
    });
  };

  /* ---------------- flow ---------------- */

  V.flow = function (pane, api, onPick, entity) {
    pane.innerHTML = "";
    pane.appendChild(el("h2", null, "Flow"));
    var lead = el("p", "lead");
    lead.textContent =
      "Follow one entity end to end. Every hop below is an edge that exists in " +
      "the graph with evidence behind it; where a hop could not be established " +
      "it is shown as a break rather than bridged with a guess.";
    pane.appendChild(lead);

    var bar = el("div", "timectl");
    var inp = el("input");
    inp.value = entity || "order";
    inp.placeholder = "entity, e.g. order";
    inp.style.cssText = "flex:0 0 260px;padding:6px 10px;border:1px solid var(--line2);" +
                        "border-radius:7px;background:var(--panel)";
    var go = el("button", null, "trace");
    go.style.cssText = "padding:6px 12px;border:1px solid var(--line2);" +
                       "border-radius:7px;background:var(--panel);cursor:pointer";
    bar.appendChild(inp); bar.appendChild(go);
    pane.appendChild(bar);

    var out = el("div");
    pane.appendChild(out);

    function run() {
      out.innerHTML = "<p class='muted'>tracing…</p>";
      api("/api/flow?entity=" + encodeURIComponent(inp.value)).then(function (d) {
        out.innerHTML = "";
        if (!d.steps.length) {
          out.appendChild(el("p", "muted",
            "Nothing matched. Try a term that appears in a route, a table, or " +
            "a topic name."));
          return;
        }
        var order = ["client", "entry", "data", "event", "consumer"];
        var seen = {};
        order.forEach(function (stage) {
          var items = d.steps.filter(function (s) { return s.stage === stage; });
          if (!items.length) return;
          items.forEach(function (s) {
            var key = stage + "|" + s.id;
            if (seen[key]) return;
            seen[key] = 1;
            var row = el("div", "flowstep");
            row.appendChild(el("div", "stage", stage));
            var body = el("div");
            var a = el("span");
            a.textContent = s.label;
            a.style.cssText = "cursor:pointer;color:var(--accent);font-weight:600";
            a.onclick = function () { onPick(s.id); };
            body.appendChild(a);
            var meta = [];
            if (s.service) meta.push(s.service);
            if (s.access) meta.push(s.access.replace("-table", ""));
            if (s.topic) meta.push("via " + s.topic);
            if (meta.length) {
              body.appendChild(el("div", "muted", meta.join("  ·  ")));
            }
            if (s.evidence) {
              body.appendChild(el("div", "ev mono", s.evidence));
            }
            row.appendChild(body);
            out.appendChild(row);
          });
        });
        if (d.breaks && d.breaks.length) {
          var b = el("div", "blind");
          b.appendChild(el("h4", null, "Breaks in the chain"));
          var ul = el("ul");
          d.breaks.forEach(function (x) { ul.appendChild(el("li", null, x)); });
          b.appendChild(ul);
          out.appendChild(b);
        }
      });
    }
    go.onclick = run;
    inp.onkeydown = function (e) { if (e.key === "Enter") run(); };
    run();
  };

  /* ---------------- impact ---------------- */

  V.impact = function (pane, api, onPick, target) {
    pane.innerHTML = "";
    pane.appendChild(el("h2", null, "Blast radius"));
    var lead = el("p", "lead");
    lead.innerHTML =
      "What breaks if this changes, across both the symbol graph and the " +
      "service topology. <b>Absence of an edge is not proof of safety</b> " +
      "&mdash; a dependent may live in a repo you do not have, or be reached " +
      "through reflection, DI, a service mesh, or config.";
    pane.appendChild(lead);

    var bar = el("div", "timectl");
    var inp = el("input");
    inp.value = target || "";
    inp.placeholder = "symbol, Class.method, file, or service";
    inp.style.cssText = "flex:0 0 340px;padding:6px 10px;border:1px solid var(--line2);" +
                        "border-radius:7px;background:var(--panel)";
    var go = el("button", null, "analyse");
    go.style.cssText = "padding:6px 12px;border:1px solid var(--line2);" +
                       "border-radius:7px;background:var(--panel);cursor:pointer";
    bar.appendChild(inp); bar.appendChild(go);
    pane.appendChild(bar);
    var out = el("div");
    pane.appendChild(out);

    function run() {
      if (!inp.value.trim()) return;
      out.innerHTML = "<p class='muted'>analysing…</p>";
      api("/api/impact?target=" + encodeURIComponent(inp.value)).then(function (d) {
        out.innerHTML = "";
        if (d.error) { out.appendChild(el("p", "muted", d.error)); return; }
        var h = el("div");
        h.innerHTML = "<h3>" + esc(d.target.label) + "</h3>" +
          "<div class='muted mono'>" + esc(d.target.kind) + " · " +
          esc(d.target.service || "-") + " · " +
          esc((d.target.file || "") + (d.target.line ? ":" + d.target.line : "")) +
          "</div>";
        out.appendChild(h);

        if (d.contract && d.contract.length) {
          var c = el("div");
          c.appendChild(el("h3", null, "Contract surface of affected services"));
          var seen = {};
          d.contract.forEach(function (x) {
            var k = x.service + "|" + x.name;
            if (seen[k]) return; seen[k] = 1;
            var r = el("div", "item");
            r.innerHTML = "<b>" + esc(x.service) + "</b> " +
              "<span class='pill'>" + esc(x.kind) + "</span> " + esc(x.name) +
              (x.authoritative ? " <span class='badge ex'>from spec</span>" : "");
            c.appendChild(r);
          });
          out.appendChild(c);
        }

        var s = el("div");
        s.appendChild(el("h3", null,
          "Services affected (" + Object.keys(d.services).length + ")"));
        Object.keys(d.services).forEach(function (svc) {
          var r = el("div", "item");
          var reasons = d.services[svc];
          var origin = reasons.some(function (x) { return x.kind === "origin"; });
          r.innerHTML = "<b>" + esc(svc) + "</b>" +
            (origin ? " <span class='badge'>origin</span>" : "") +
            reasons.filter(function (x) { return x.kind !== "origin"; })
              .slice(0, 4).map(function (x) {
                return "<div class='ev'>" +
                  (x.provenance === "INFERRED" ? "<span class='inf'>?</span> " : "") +
                  esc(x.reason) + (x.evidence ? " <span class='muted'>" +
                  esc(x.evidence) + "</span>" : "") + "</div>";
              }).join("");
          s.appendChild(r);
        });
        out.appendChild(s);

        if (d.direct.length) {
          var dd = el("div");
          dd.appendChild(el("h3", null, "Direct dependents (" + d.direct.length + ")"));
          d.direct.forEach(function (x) {
            var r = el("div", "item");
            var a = el("span", "lbl", x.label);
            a.onclick = function () { onPick(x.id); };
            r.appendChild(a);
            if (x.provenance === "INFERRED") {
              r.appendChild(el("span", "badge inf", "inferred"));
            }
            r.appendChild(el("div", "ev mono", x.via + "  " + (x.evidence || "")));
            dd.appendChild(r);
          });
          out.appendChild(dd);
        }

        if (d.coupling && d.coupling.length) {
          var cc = el("div");
          cc.appendChild(el("h3", null, "Historically changes together"));
          d.coupling.forEach(function (x) {
            var r = el("div", "item");
            r.innerHTML = "<span class='mono'>" + esc(x.path) + "</span>" +
              (x.cross_repo ? " <span class='badge warn'>cross-repo</span>" : "") +
              "<div class='ev'>" + x.shared + " shared commits, degree " +
              x.degree.toFixed(2) + "</div>";
            cc.appendChild(r);
          });
          out.appendChild(cc);
        }

        if (d.owners && d.owners.length) {
          var o = el("div");
          o.appendChild(el("h3", null, "Suggested reviewers"));
          o.appendChild(el("div", "muted",
            d.owners.map(function (x) { return x.author; }).join(", ")));
          out.appendChild(o);
        }

        if (d.invisible && d.invisible.length) {
          var b = el("div", "blind");
          b.appendChild(el("h4", null, "Limits of visibility"));
          var ul = el("ul");
          d.invisible.forEach(function (x) { ul.appendChild(el("li", null, x)); });
          b.appendChild(ul);
          out.appendChild(b);
        }
        if (d.notes && d.notes.length) {
          d.notes.forEach(function (n) {
            var p = el("p", "muted");
            p.textContent = "! " + n;
            out.appendChild(p);
          });
        }

        var btns = el("div", "btnrow");
        var cp = el("button", null, "copy for Claude");
        cp.onclick = function () {
          navigator.clipboard.writeText(d.text || "").then(function () {
            cp.textContent = "copied";
            setTimeout(function () { cp.textContent = "copy for Claude"; }, 1400);
          });
        };
        btns.appendChild(cp);
        out.appendChild(btns);
      });
    }
    go.onclick = run;
    inp.onkeydown = function (e) { if (e.key === "Enter") run(); };
    if (inp.value) run();
  };

  /* ---------------- time-lapse ---------------- */

  V.time = function (pane, api) {
    pane.innerHTML = "";
    pane.appendChild(el("h2", null, "How the estate got this way"));
    var lead = el("p", "lead");
    lead.textContent =
      "Monthly activity per service from git history. The only honest fourth " +
      "dimension available: not a projection, just what actually happened. " +
      "Drag the slider, or press play.";
    pane.appendChild(lead);

    var out = el("div");
    pane.appendChild(out);
    out.innerHTML = "<p class='muted'>loading…</p>";

    api("/api/timeline").then(function (d) {
      out.innerHTML = "";
      if (!d.months.length) {
        out.appendChild(el("p", "muted",
          "No history. Either the repos are not git checkouts, or the scan ran " +
          "with --skip-history."));
        return;
      }
      var byMonth = {};
      d.rows.forEach(function (r) {
        (byMonth[r.month] = byMonth[r.month] || {})[r.service] = r;
      });
      var maxC = Math.max.apply(null, d.rows.map(function (r) { return r.commits; }));

      var ctl = el("div", "timectl");
      var range = el("input");
      range.type = "range"; range.min = 0; range.max = d.months.length - 1;
      range.value = d.months.length - 1;
      var play = el("button", null, "play");
      play.style.cssText = "padding:5px 12px;border:1px solid var(--line2);" +
                           "border-radius:7px;background:var(--panel);cursor:pointer";
      var lab = el("div", "mono");
      lab.style.cssText = "min-width:70px;text-align:right";
      ctl.appendChild(play); ctl.appendChild(range); ctl.appendChild(lab);
      out.appendChild(ctl);

      var grid = el("div");
      grid.style.cssText = "display:grid;grid-template-columns:150px 1fr;gap:3px 12px;" +
                           "align-items:center";
      out.appendChild(grid);

      var cum = el("p", "muted");
      out.appendChild(cum);

      function render(idx) {
        var m = d.months[idx];
        lab.textContent = m;
        grid.innerHTML = "";
        var totalC = 0, active = 0;
        d.services.forEach(function (s) {
          var r = (byMonth[m] || {})[s];
          var c = r ? r.commits : 0;
          totalC += c;
          if (c) active++;
          var name = el("div", "mono");
          name.textContent = s;
          name.style.cssText = "font-size:12px;color:" +
            (c ? "var(--fg)" : "var(--muted)");
          var barWrap = el("div", "bar");
          barWrap.style.margin = "0";
          var bar = el("span");
          var pct = maxC ? (c / maxC) * 100 : 0;
          bar.style.cssText = "width:" + pct + "%;background:" +
            (c ? "var(--accent)" : "transparent");
          barWrap.appendChild(bar);
          barWrap.title = c + " commits";
          grid.appendChild(name);
          grid.appendChild(barWrap);
        });
        cum.textContent = m + ": " + totalC + " commits across " + active +
          " active service" + (active === 1 ? "" : "s") + ".";
      }
      range.oninput = function () { render(+range.value); };
      render(d.months.length - 1);

      var timer = null;
      play.onclick = function () {
        if (timer) {
          clearInterval(timer); timer = null; play.textContent = "play"; return;
        }
        play.textContent = "pause";
        var i = 0;
        range.value = 0; render(0);
        timer = setInterval(function () {
          i++;
          if (i >= d.months.length) {
            clearInterval(timer); timer = null; play.textContent = "play"; return;
          }
          range.value = i; render(i);
        }, 550);
      };
    });
  };

  /* ---------------- coverage ---------------- */

  V.coverage = function (pane, api) {
    pane.innerHTML = "";
    pane.appendChild(el("h2", null, "What this map can and cannot see"));
    var lead = el("p", "lead");
    lead.innerHTML =
      "A map that hides its own gaps is worse than one that admits them. " +
      "Everything below is what the scan could <i>not</i> establish &mdash; " +
      "and each item is a good, specific question to bring to a teammate.";
    pane.appendChild(lead);

    var out = el("div");
    pane.appendChild(out);
    out.innerHTML = "<p class='muted'>loading…</p>";

    api("/api/coverage").then(function (d) {
      out.innerHTML = "";
      var tot = d.extracted + d.inferred || 1;
      var box = el("div");
      box.innerHTML =
        "<h3>Evidence quality</h3>" +
        "<div class='bar' style='height:14px'>" +
          "<span style='width:" + (d.extracted / tot * 100).toFixed(1) +
          "%;background:var(--accent2);float:left'></span>" +
          "<span style='width:" + (d.inferred / tot * 100).toFixed(1) +
          "%;background:var(--warn);float:left'></span>" +
        "</div>" +
        "<div class='muted'>" +
          "<b>" + d.extracted + "</b> extracted (read from source, config, a " +
          "spec or a trace) &nbsp;·&nbsp; " +
          "<b>" + d.inferred + "</b> inferred (name-matched &mdash; a lead, " +
          "not a fact)" +
          (d.runtime_confirmed ? " &nbsp;·&nbsp; <b>" + d.runtime_confirmed +
           "</b> confirmed by runtime traces" : "") +
        "</div>";
      out.appendChild(box);

      if (!d.runtime_confirmed) {
        var t = el("div", "blind");
        t.innerHTML = "<h4>No runtime traces loaded</h4>" +
          "<p class='muted' style='margin:4px 0 0'>This is the single biggest " +
          "improvement available. Ask which APM the team runs, export its " +
          "service dependency graph, and re-scan with " +
          "<code>--trace &lt;file&gt;</code>. Traces are measured rather than " +
          "inferred, and they reveal services whose repos you do not have.</p>";
        out.appendChild(t);
      }

      if (d.services_without_repo && d.services_without_repo.length) {
        var s = el("div", "blind");
        s.appendChild(el("h4", null,
          "Services with no repo on this machine (" +
          d.services_without_repo.length + ")"));
        var p = el("p", "muted");
        p.style.margin = "4px 0 0";
        p.textContent = d.services_without_repo.join(", ") +
          " — visible only from the outside. Their internals are unmapped.";
        s.appendChild(p);
        out.appendChild(s);
      }

      (d.blind_spots || []).forEach(function (b) {
        var box = el("div", "blind");
        box.appendChild(el("h4", null,
          b.category.replace(/-/g, " ") + " (" + b.count + ")"));
        box.appendChild(el("div", "muted", b.why));
        var ul = el("ul");
        b.items.slice(0, 12).forEach(function (x) {
          ul.appendChild(el("li", null, x));
        });
        if (b.count > 12) {
          ul.appendChild(el("li", "muted", "…and " + (b.count - 12) + " more"));
        }
        box.appendChild(ul);
        out.appendChild(box);
      });

      var note = el("p", "muted");
      note.style.marginTop = "18px";
      note.textContent = d.note;
      out.appendChild(note);
    });
  };

  /* ==================================================================
     Architecture — the runtime diagram, derived rather than drawn.

     Why this is a separate view rather than a filter on Map: an
     architecture diagram is not a graph drawing. It is LAYERED (callers
     above, stores below), it uses one arrow per relationship rather than
     one per underlying edge, and its positions are stable so that the
     picture in your head survives a rescan. A force layout gives none of
     those, which is why every team redraws this by hand -- and then lets
     it go stale.

     What it deliberately omits: build-time coupling. On a real estate,
     imports/extends/Maven dependencies are 82% of service-to-service
     edges and they drown the runtime picture. That omission is stated in
     the UI rather than left for the reader to discover.
     ================================================================== */

  /**
   * Assign each node a layer via longest-path from the sources.
   *
   * Cycles are real in service graphs (A calls B, B calls back), so
   * back-edges found during a depth-first pass are removed for layering
   * only -- they are still drawn, just marked, because a cycle is
   * information rather than an error.
   */
  function layerize(ids, links) {
    var out = {}, indeg = {};
    ids.forEach(function (i) { out[i] = []; indeg[i] = 0; });
    var back = {};
    var colour = {};                       // 0 unvisited, 1 in stack, 2 done
    var adj = {};
    ids.forEach(function (i) { adj[i] = []; });
    links.forEach(function (l) {
      if (adj[l.source] && adj[l.target] !== undefined) adj[l.source].push(l);
    });
    function dfs(u) {
      colour[u] = 1;
      adj[u].forEach(function (l) {
        var v = l.target;
        if (colour[v] === 1) { back[l.source + "|" + l.target] = 1; return; }
        if (!colour[v]) dfs(v);
      });
      colour[u] = 2;
    }
    ids.forEach(function (i) { if (!colour[i]) dfs(i); });

    links.forEach(function (l) {
      if (back[l.source + "|" + l.target]) return;
      if (!out[l.source] || indeg[l.target] === undefined) return;
      out[l.source].push(l.target);
      indeg[l.target]++;
    });

    var layer = {}, queue = [];
    ids.forEach(function (i) { layer[i] = 0; if (!indeg[i]) queue.push(i); });
    // Kahn, keeping the longest path so a node sits below everything that
    // reaches it rather than beside it.
    var seen = 0;
    while (queue.length) {
      var u = queue.shift();
      seen++;
      out[u].forEach(function (v) {
        if (layer[v] < layer[u] + 1) layer[v] = layer[u] + 1;
        if (--indeg[v] === 0) queue.push(v);
      });
    }
    return { layer: layer, back: back };
  }

  /**
   * Order nodes within each layer to reduce crossings (barycentre sweeps).
   * Four passes is well past the point of diminishing returns for graphs
   * this size and costs nothing measurable.
   */
  function orderLayers(rows, links) {
    var pos = {};
    Object.keys(rows).forEach(function (L) {
      rows[L].forEach(function (id, i) { pos[id] = i; });
    });
    var keys = Object.keys(rows).map(Number).sort(function (a, b) { return a - b; });
    for (var pass = 0; pass < 4; pass++) {
      var down = pass % 2 === 0;
      var seq = down ? keys : keys.slice().reverse();
      seq.forEach(function (L) {
        var bary = {};
        rows[L].forEach(function (id) { bary[id] = []; });
        links.forEach(function (l) {
          var from = down ? l.target : l.source;
          var to = down ? l.source : l.target;
          if (bary[from] && pos[to] !== undefined) bary[from].push(pos[to]);
        });
        rows[L].sort(function (a, b) {
          var ba = bary[a] && bary[a].length
            ? bary[a].reduce(function (x, y) { return x + y; }, 0) / bary[a].length
            : pos[a];
          var bb = bary[b] && bary[b].length
            ? bary[b].reduce(function (x, y) { return x + y; }, 0) / bary[b].length
            : pos[b];
          return ba - bb;
        });
        rows[L].forEach(function (id, i) { pos[id] = i; });
      });
    }
    return pos;
  }

  /**
   * Full diagram layout: layer, compress, settle the data band, order.
   *
   * Extracted as one function purely so it is testable without a DOM. An
   * earlier version of the quality check re-implemented these steps inside
   * the test, which meant the test measured its own copy and stayed green
   * through a layout change -- the same failure mode as every other
   * decorative assertion found today.
   */
  function layoutDiagram(keep, links) {
    var ids = Object.keys(keep);
    var lay = layerize(ids, links);

    // Compress the service chain. Longest-path layering is correct but
    // literal: 37 services came out as 17 layers, two per row, a tall
    // ribbon nobody can read. Squashing proportionally keeps the ordering
    // (a caller still sits above its callee) while producing a diagram
    // with the proportions of a diagram.
    var MAX_SVC_LAYERS = 6;
    var maxL = 0;
    ids.forEach(function (i) {
      if (keep[i].kind === "service") maxL = Math.max(maxL, lay.layer[i]);
    });
    if (maxL > MAX_SVC_LAYERS - 1) {
      ids.forEach(function (i) {
        if (keep[i].kind !== "service") return;
        lay.layer[i] = Math.round(lay.layer[i] / maxL * (MAX_SVC_LAYERS - 1));
      });
      maxL = MAX_SVC_LAYERS - 1;
    }

    // Stores and queues settle at the bottom whatever the topology says --
    // the eye reads a diagram top-to-bottom and expects data underneath.
    // They WRAP, though: 70 of them in a single row is 11,000px wide and
    // no more readable than the hairball this view exists to replace.
    var wps = ids.filter(function (i) { return keep[i].kind !== "service"; });
    var PER_ROW = 14;
    // Order them under the services they belong to before wrapping, so a
    // queue lands beneath its writer rather than in arrival order.
    var svcPos = {};
    ids.forEach(function (i) {
      if (keep[i].kind === "service") svcPos[i] = lay.layer[i];
    });
    var bary = {};
    wps.forEach(function (w) { bary[w] = []; });
    links.forEach(function (l) {
      if (bary[l.target] && svcPos[l.source] !== undefined) bary[l.target].push(l.source);
      if (bary[l.source] && svcPos[l.target] !== undefined) bary[l.source].push(l.target);
    });
    wps.sort(function (a2, b2) {
      var f = function (w) {
        var n = bary[w];
        return n && n.length ? n.map(function (x) { return x; }).sort()[0] : "";
      };
      return String(f(a2)).localeCompare(String(f(b2)));
    });
    wps.forEach(function (w, i) {
      lay.layer[w] = maxL + 1 + Math.floor(i / PER_ROW);
    });

    var rows = {};
    ids.forEach(function (i) {
      (rows[lay.layer[i]] = rows[lay.layer[i]] || []).push(i);
    });
    var pos = orderLayers(rows, links);
    return { rows: rows, pos: pos, layer: lay.layer, back: lay.back };
  }

  V.architecture = function (pane, api, onPick) {
    pane.innerHTML = "";
    pane.appendChild(el("h2", null, "Runtime architecture"));
    var lead = el("p", "lead");
    lead.innerHTML =
      "Services, what they call at run time, and the queues and tables they " +
      "meet in the middle. Derived from the graph, so it cannot go stale, and " +
      "every arrow is clickable for the <code>file:line</code> behind it. " +
      "<b>Build-time coupling is excluded</b> &mdash; imports, extends and " +
      "Maven dependencies are the large majority of service-to-service edges " +
      "and they bury the runtime picture. Use <b>Map</b> for those.";
    pane.appendChild(lead);

    var bar = el("div", "timectl");
    var cbW = el("input"); cbW.type = "checkbox"; cbW.checked = true;
    var labW = el("label", "row"); labW.appendChild(cbW);
    labW.appendChild(document.createTextNode(" shared queues & tables"));
    var cbAll = el("input"); cbAll.type = "checkbox";
    var labAll = el("label", "row"); labAll.appendChild(cbAll);
    labAll.appendChild(document.createTextNode(" include single-writer ones"));
    bar.appendChild(labW); bar.appendChild(labAll);
    var note = el("span", "hint"); bar.appendChild(note);
    pane.appendChild(bar);

    // A viewport, not a scrollbox. A diagram you have to scroll in two
    // directions is one you cannot see, and scrolling is the wrong verb
    // anyway: the useful gestures on a diagram are pan and zoom.
    // The pane becomes a column and the viewport takes what is left, rather
    // than subtracting a guessed header height from the viewport.
    pane.className = (pane.className || "") + " fills";
    var host = el("div", "grow");
    host.style.cssText = "position:relative;overflow:hidden;" +
                         "border:1px solid var(--line);border-radius:10px;" +
                         "background:var(--bg);cursor:grab";
    pane.appendChild(host);

    var ctl = el("div");
    ctl.style.cssText = "position:absolute;right:10px;bottom:10px;display:flex;" +
                        "flex-direction:column;gap:4px;z-index:2";
    function ctlBtn(txt, title) {
      var b2 = el("button", null, txt);
      b2.title = title;
      b2.style.cssText = "width:30px;height:30px;border:1px solid var(--line2);" +
        "background:var(--panel);border-radius:8px;cursor:pointer;font:inherit";
      ctl.appendChild(b2);
      return b2;
    }
    var bIn = ctlBtn("+", "Zoom in"),
        bOut = ctlBtn("\u2212", "Zoom out"),
        bFit = ctlBtn("\u2922", "Fit the whole diagram (F)");
    host.appendChild(ctl);

    var state = { vb: null, keepView: false };

    // One set of window-level pan handlers for the life of the view.
    var port = { svg: null, vb: null, apply: null, drag: null, moved: 0 };
    function onMove(ev) {
      if (!port.drag || !port.svg) return;
      port.moved++;
      var r = port.svg.getBoundingClientRect();
      if (!r.width || !r.height) return;
      port.vb.x = port.drag.vx - (ev.clientX - port.drag.x) / r.width * port.vb.w;
      port.vb.y = port.drag.vy - (ev.clientY - port.drag.y) / r.height * port.vb.h;
      port.apply();
    }
    function onUp() {
      port.drag = null;
      host.style.cursor = "grab";
    }
    if (window.addEventListener) {
      window.addEventListener("mousemove", onMove);
      window.addEventListener("mouseup", onUp);
    }

    api("/api/architecture").then(function (d) {
      if (d.error) {
        host.appendChild(el("p", "muted", d.error));
        return;
      }
      function render() {
        host.innerHTML = "";
        host.appendChild(ctl);          // survives the clear above
        var showWp = cbW.checked, showAll = cbAll.checked;
        var byId = {};
        d.nodes.forEach(function (n) { byId[n.id] = n; });

        var keep = {};
        d.nodes.forEach(function (n) {
          if (n.kind === "service") { keep[n.id] = n; return; }
          if (!showWp) return;
          if (showAll || n.significant) keep[n.id] = n;
        });
        var links = d.links.filter(function (l) {
          return keep[l.source] && keep[l.target];
        });
        var ids = Object.keys(keep);

        note.textContent = ids.length + " boxes · " + links.length +
          " arrows · " + d.excluded.build_time_edges.toLocaleString() +
          " build-time edges not shown";

        var LAY = layoutDiagram(keep, links);
        var lay = { layer: LAY.layer, back: LAY.back };
        var rows = LAY.rows, pos = LAY.pos;

        var BW = 132, BH = 34, GX = 26, GY = 92;
        var widest = 0;
        Object.keys(rows).forEach(function (L) {
          widest = Math.max(widest, rows[L].length);
        });
        var W = Math.max(900, widest * (BW + GX) + GX);
        var layers = Object.keys(rows).map(Number).sort(function (a, b) { return a - b; });
        var H = layers.length * GY + 60;

        var xy = {};
        layers.forEach(function (L, li) {
          var row = rows[L];
          var span = row.length * (BW + GX) - GX;
          var x0 = (W - span) / 2;
          row.forEach(function (id, i) {
            xy[id] = { x: x0 + i * (BW + GX) + BW / 2, y: 40 + li * GY };
          });
        });

        var NS = "http://www.w3.org/2000/svg";
        var svg = document.createElementNS(NS, "svg");
        // The SVG fills the viewport; what you see is chosen by the viewBox.
        // That makes fit, pan and zoom one mechanism instead of three, and
        // keeps text crisp at every scale -- which is the whole reason this
        // view is SVG rather than canvas.
        svg.setAttribute("width", "100%");
        svg.setAttribute("height", "100%");
        svg.setAttribute("preserveAspectRatio", "xMidYMid meet");
        svg.style.cssText = "display:block;width:100%;height:100%";

        var defs = document.createElementNS(NS, "defs");
        ["sync", "async", "data", "code"].forEach(function (k) {
          var m = document.createElementNS(NS, "marker");
          m.setAttribute("id", "ah-" + k);
          m.setAttribute("viewBox", "0 0 8 8");
          m.setAttribute("refX", "7"); m.setAttribute("refY", "4");
          m.setAttribute("markerWidth", "7"); m.setAttribute("markerHeight", "7");
          m.setAttribute("orient", "auto-start-reverse");
          var p = document.createElementNS(NS, "path");
          p.setAttribute("d", "M0,0 L8,4 L0,8 z");
          p.setAttribute("fill", "var(--" + k + ")");
          m.appendChild(p); defs.appendChild(m);
        });
        svg.appendChild(defs);

        var KIND2STYLE = {
          http: "sync", "calls-route": "sync",
          event: "async", produces: "async", "consumed-by": "async",
          "reads-table": "data", "writes-table": "data",
          "uses-datastore": "data", "uses-cache": "data"
        };

        links.forEach(function (l) {
          var a = xy[l.source], b = xy[l.target];
          if (!a || !b) return;
          var style = KIND2STYLE[l.kinds[0][0]] || "code";
          var isBack = !!lay.back[l.source + "|" + l.target];
          var path = document.createElementNS(NS, "path");
          var midY = (a.y + b.y) / 2;
          // Cubic with vertical control points: reads as a flow rather than
          // as a straight line through unrelated boxes.
          path.setAttribute("d", "M" + a.x + "," + (a.y + BH / 2) +
            " C" + a.x + "," + midY + " " + b.x + "," + midY +
            " " + b.x + "," + (b.y - BH / 2));
          path.setAttribute("fill", "none");
          path.setAttribute("stroke", "var(--" + style + ")");
          path.setAttribute("stroke-width",
                            Math.min(4, 1 + Math.log1p(l.count) * 0.7));
          path.setAttribute("opacity", isBack ? "0.75" : "0.5");
          if (isBack) path.setAttribute("stroke-dasharray", "5,4");
          if (l.provenance === "INFERRED") path.setAttribute("stroke-dasharray", "2,4");
          path.setAttribute("marker-end", "url(#ah-" + style + ")");
          path.style.cursor = "pointer";
          var t = document.createElementNS(NS, "title");
          t.textContent = (byId[l.source] ? byId[l.source].label : l.source) +
            " → " + (byId[l.target] ? byId[l.target].label : l.target) +
            "\n" + l.kinds.map(function (k) { return k[0] + " x" + k[1]; }).join(", ") +
            (isBack ? "\n(cycle: also called in the other direction)" : "") +
            (l.provenance === "INFERRED" ? "\nINFERRED - name match, not proven" : "") +
            (l.evidence ? "\n" + l.evidence : "");
          path.appendChild(t);
          path.onclick = function () { if (onPick) onPick(l.source); };
          svg.appendChild(path);
        });

        ids.forEach(function (id) {
          var n = keep[id], p = xy[id];
          if (!p) return;
          var g = document.createElementNS(NS, "g");
          g.style.cursor = "pointer";
          var r = document.createElementNS(NS, "rect");
          r.setAttribute("x", p.x - BW / 2); r.setAttribute("y", p.y - BH / 2);
          r.setAttribute("width", BW); r.setAttribute("height", BH);
          r.setAttribute("rx", n.kind === "service" ? 7 : 15);
          r.setAttribute("fill", "var(--panel)");
          r.setAttribute("stroke", n.kind === "service" ? "var(--accent)"
                          : (n.significant ? "var(--warn)" : "var(--line2)"));
          r.setAttribute("stroke-width", n.significant ? 2 : 1);
          g.appendChild(r);
          var tx = document.createElementNS(NS, "text");
          tx.setAttribute("x", p.x); tx.setAttribute("y", p.y + 4);
          tx.setAttribute("text-anchor", "middle");
          tx.setAttribute("font-size", "11.5");
          tx.setAttribute("fill", "var(--fg)");
          var lbl = String(n.label || id);
          tx.textContent = lbl.length > 18 ? lbl.slice(0, 17) + "…" : lbl;
          g.appendChild(tx);
          var ti = document.createElementNS(NS, "title");
          ti.textContent = lbl + (n.kind === "service" ? "" :
            "\n" + n.kind + " — " + n.writers + " writer(s), " +
            n.readers + " reader(s)" +
            (n.significant ? "\nMultiple writers: no documented owner" : ""));
          g.appendChild(ti);
          g.onclick = function () { if (onPick) onPick(id); };
          svg.appendChild(g);
        });

        // Append, do not insertBefore(ctl): render() clears `host` above, so
        // `ctl` is no longer a child by the time we get here and
        // insertBefore would throw NotFoundError and abandon the whole
        // render -- which is exactly what left the panel blank. The controls
        // are re-attached below; z-index keeps them above the diagram.
        host.appendChild(svg);

        // ---- fit / pan / zoom, all through the viewBox ------------------
        var PAD = 40;
        var full = { x: -PAD, y: -PAD, w: W + PAD * 2, h: H + PAD * 2 };
        var vb = state.vb && state.keepView
          ? { x: state.vb.x, y: state.vb.y, w: state.vb.w, h: state.vb.h }
          : { x: full.x, y: full.y, w: full.w, h: full.h };
        state.keepView = true;

        function apply() {
          svg.setAttribute("viewBox",
            vb.x + " " + vb.y + " " + vb.w + " " + vb.h);
          state.vb = { x: vb.x, y: vb.y, w: vb.w, h: vb.h };
        }
        function fit() {
          vb.x = full.x; vb.y = full.y; vb.w = full.w; vb.h = full.h;
          apply();
        }
        // Zoom about a point in SVG user space, so whatever is under the
        // cursor stays under the cursor -- the same rule as the graph views.
        function zoomAt(cx, cy, k) {
          var nw = Math.max(full.w * 0.05, Math.min(full.w * 3, vb.w * k));
          var scale = nw / vb.w;
          vb.x = cx - (cx - vb.x) * scale;
          vb.y = cy - (cy - vb.y) * scale;
          vb.w = nw;
          vb.h = vb.h * scale;
          apply();
        }
        function toUser(ev) {
          var r = svg.getBoundingClientRect();
          return { x: vb.x + (ev.clientX - r.left) / r.width * vb.w,
                   y: vb.y + (ev.clientY - r.top) / r.height * vb.h };
        }

        svg.addEventListener("wheel", function (ev) {
          ev.preventDefault();
          var p = toUser(ev);
          zoomAt(p.x, p.y, ev.deltaY > 0 ? 1.12 : 1 / 1.12);
        }, { passive: false });

        // Pan handlers live on `window` because the pointer routinely leaves
        // the viewport mid-drag. They are registered ONCE, in the enclosing
        // scope, and read the live viewport through `port` -- registering
        // them here would add another pair on every filter toggle, each
        // closing over a stale viewBox.
        port.svg = svg;
        port.vb = vb;
        port.apply = apply;
        svg.addEventListener("mousedown", function (ev) {
          port.drag = { x: ev.clientX, y: ev.clientY, vx: vb.x, vy: vb.y };
          port.moved = 0;
          host.style.cursor = "grabbing";
        });
        // A drag that moved is a pan, not a click: without this every pan
        // that happens to start on a box also selects it.
        svg.addEventListener("click", function (ev) {
          if (port.moved > 3) { ev.stopPropagation(); ev.preventDefault(); }
        }, true);

        bIn.onclick = function () { zoomAt(vb.x + vb.w / 2, vb.y + vb.h / 2, 1 / 1.3); };
        bOut.onclick = function () { zoomAt(vb.x + vb.w / 2, vb.y + vb.h / 2, 1.3); };
        bFit.onclick = fit;

        apply();

        if (!links.length) {
          host.appendChild(el("p", "muted",
            "No runtime interactions found. Either this estate genuinely has " +
            "none, or its HTTP/queue/DB calls are made in a way the " +
            "extractors do not recognise — check the Coverage tab."));
        }
      }
      cbW.onchange = render;
      cbAll.onchange = render;
      render();
    });
  };

  // Exported for tests: layout quality (layer count, row width, crossings)
  // is the difference between a diagram and a picture of a graph, and it is
  // only assertable if the layout can be run without a DOM.
  V._layerize = layerize;
  V._orderLayers = orderLayers;
  V._layout = layoutDiagram;

  global.Views = V;
  global.esc = esc;
  global.el = el;
})(window);
