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

  V.data = function (pane, api, onPick) {
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
      var sum = el("div");
      sum.innerHTML =
        "<span class='pill'>" + d.tables.length + " tables</span>" +
        "<span class='pill'>" + d.services.length + " services touch data</span>" +
        "<span class='pill'" + (shared.length ? " style='background:var(--warn);color:#fff'" : "") +
        ">" + shared.length + " shared by 2+ services</span>";
      pane.appendChild(sum);

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

      // Shared tables first: they are the finding, so they should not be
      // buried alphabetically halfway down a long list.
      var rows = d.tables.slice().sort(function (a, b) {
        var d1 = b.touched_by.length - a.touched_by.length;
        return d1 || a.name.localeCompare(b.name);
      });

      rows.forEach(function (tb) {
        var tr = el("tr");
        var td = el("td");
        var a = el("span", "lbl");
        a.textContent = tb.name;
        a.style.cssText = "cursor:pointer;color:var(--accent)";
        a.onclick = function () { onPick(tb.id); };
        td.appendChild(a);
        if (tb.touched_by.length > 1) {
          var b = el("span", "badge warn", tb.touched_by.length + " services");
          b.style.marginLeft = "6px";
          td.appendChild(b);
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

  global.Views = V;
  global.esc = esc;
  global.el = el;
})(window);
