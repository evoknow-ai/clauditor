/* clauditor dashboard logic (SPEC.md §10).
 *
 * Single-page app, no framework, no build step. Fetches from /api/* and renders
 * progressively (skeletons fill in as each request resolves; one slow query does
 * not block the rest of the UI).
 *
 * CARRIED-FORWARD ITEM 2 (SPEC.md §10): ALL UI state is held in plain
 * module-scope JS variables below. There is intentionally NO localStorage /
 * sessionStorage anywhere in this file -- the dashboard must run in a sandboxed
 * renderer where browser storage is unavailable.
 */
(function () {
  "use strict";

  // --- UI state (in-memory only; no browser storage) ------------------------
  var state = {
    rangePreset: "30",   // '7' | '30' | '90' | 'all' | 'custom'
    from: null,          // ISO date string or null
    to: null,            // ISO date string or null
    project: "",         // '' = all projects
    breakdownBy: "project",
  };

  // Live Chart.js instances, so we can destroy/replace on refetch.
  var charts = { timeseries: null, breakdown: null };

  // Stable color per model/key so chart segments stay consistent across renders.
  var PALETTE = [
    "#4f8cff", "#38d39f", "#f5b740", "#ef5350", "#a06bff",
    "#36c5d6", "#ff8a5c", "#9ccc65", "#ec407a", "#7e8aa0",
  ];
  var colorCache = {};
  function colorFor(key) {
    if (!(key in colorCache)) {
      colorCache[key] = PALETTE[Object.keys(colorCache).length % PALETTE.length];
    }
    return colorCache[key];
  }

  // --- helpers --------------------------------------------------------------

  function $(id) { return document.getElementById(id); }

  function buildQuery() {
    var p = new URLSearchParams();
    if (state.from) { p.set("from", state.from); }
    if (state.to) { p.set("to", state.to); }
    if (state.project) { p.set("project", state.project); }
    return p;
  }

  function apiGet(path, extraParams) {
    var p = buildQuery();
    if (extraParams) {
      Object.keys(extraParams).forEach(function (k) { p.set(k, extraParams[k]); });
    }
    var qs = p.toString();
    var url = path + (qs ? "?" + qs : "");
    return fetch(url).then(function (r) {
      if (!r.ok) {
        return r.json().catch(function () { return {}; }).then(function (body) {
          var msg = (body && body.error) || ("HTTP " + r.status);
          throw new Error(msg);
        });
      }
      return r.json();
    });
  }

  function fmtMoney(n) {
    var v = Number(n || 0);
    return "$" + v.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  }
  function fmtInt(n) {
    return Number(n || 0).toLocaleString();
  }
  function fmtTokens(n) {
    var v = Number(n || 0);
    if (v >= 1e9) { return (v / 1e9).toFixed(2) + "B"; }
    if (v >= 1e6) { return (v / 1e6).toFixed(2) + "M"; }
    if (v >= 1e3) { return (v / 1e3).toFixed(1) + "k"; }
    return String(v);
  }
  function fmtPct(frac) {
    return (Number(frac || 0) * 100).toFixed(1) + "%";
  }

  function setValue(id, text) {
    var el = $(id);
    el.textContent = text;
    el.classList.remove("skeleton");
  }

  // --- 1. Pricing badge (header) -------------------------------------------

  function renderPricingBadge(updated) {
    var badge = $("pricing-badge");
    if (!updated) {
      badge.textContent = "pricing updated: unknown";
      badge.classList.add("stale");
      return;
    }
    badge.textContent = "pricing updated: " + updated;
    var when = new Date(updated + "T00:00:00Z");
    var ageDays = (Date.now() - when.getTime()) / 86400000;
    if (isNaN(ageDays) || ageDays > 30) {
      badge.classList.add("stale");
      badge.title = "Pricing data is more than 30 days old";
    } else {
      badge.classList.remove("stale");
      badge.title = "Pricing data date";
    }
  }

  // --- 2. Summary cards -----------------------------------------------------

  function loadSummary() {
    ["val-spend", "val-tokens", "val-calls", "val-cache"].forEach(function (id) {
      $(id).classList.add("skeleton");
    });
    apiGet("/api/summary").then(function (data) {
      setValue("val-spend", fmtMoney(data.total_spend_usd));
      setValue("val-tokens", fmtTokens(data.total_tokens));
      setValue("val-calls", fmtInt(data.call_count));
      setValue("val-cache", fmtPct(data.cache_efficiency));
    }).catch(function (err) {
      setValue("val-spend", "—");
      setValue("val-tokens", "—");
      setValue("val-calls", "—");
      setValue("val-cache", "—");
      console.error("summary failed:", err);
    });
  }

  // --- 3. Spend over time (stacked by model) -------------------------------

  function loadTimeseries() {
    var placeholder = $("timeseries-placeholder");
    var canvas = $("timeseries-chart");
    var empty = $("timeseries-empty");
    placeholder.hidden = false;
    empty.hidden = true;

    apiGet("/api/timeseries", { granularity: "day" }).then(function (data) {
      var series = data.series || [];
      placeholder.hidden = true;

      if (series.length === 0) {
        canvas.hidden = true;
        empty.hidden = false;
        if (charts.timeseries) { charts.timeseries.destroy(); charts.timeseries = null; }
        return;
      }

      // Pivot: buckets (x axis) x models (stacked datasets) -> spend.
      var buckets = [];
      var bucketSeen = {};
      var models = [];
      var modelSeen = {};
      var cell = {}; // model -> { bucket -> spend }

      series.forEach(function (row) {
        if (!bucketSeen[row.bucket]) { bucketSeen[row.bucket] = true; buckets.push(row.bucket); }
        if (!modelSeen[row.model]) { modelSeen[row.model] = true; models.push(row.model); cell[row.model] = {}; }
        cell[row.model][row.bucket] = row.spend_usd;
      });
      buckets.sort();

      var datasets = models.map(function (m) {
        return {
          label: m,
          data: buckets.map(function (b) { return cell[m][b] || 0; }),
          backgroundColor: colorFor(m),
          borderWidth: 0,
        };
      });

      canvas.hidden = false;
      if (charts.timeseries) { charts.timeseries.destroy(); }
      charts.timeseries = new Chart(canvas, {
        type: "bar",
        data: { labels: buckets, datasets: datasets },
        options: stackedSpendOptions(),
      });
    }).catch(function (err) {
      placeholder.hidden = true;
      empty.hidden = false;
      empty.textContent = "Could not load spend over time: " + err.message;
      console.error("timeseries failed:", err);
    });
  }

  function stackedSpendOptions() {
    return {
      responsive: true,
      maintainAspectRatio: false,
      interaction: { mode: "index", intersect: false },
      plugins: {
        legend: { labels: { color: "#cbd5e1", boxWidth: 12 } },
        tooltip: {
          callbacks: {
            label: function (ctx) { return ctx.dataset.label + ": " + fmtMoney(ctx.parsed.y); },
          },
        },
      },
      scales: {
        x: { stacked: true, ticks: { color: "#8a97a8" }, grid: { color: "#2c3645" } },
        y: {
          stacked: true,
          ticks: { color: "#8a97a8", callback: function (v) { return "$" + v; } },
          grid: { color: "#2c3645" },
        },
      },
    };
  }

  // --- 4. Breakdown (bar + table) ------------------------------------------

  function loadBreakdown() {
    var placeholder = $("breakdown-placeholder");
    var canvas = $("breakdown-chart");
    var tbody = $("breakdown-tbody");
    var empty = $("breakdown-empty");
    placeholder.hidden = false;
    empty.hidden = true;
    tbody.innerHTML = "";

    apiGet("/api/breakdown", { by: state.breakdownBy }).then(function (data) {
      var groups = data.groups || [];
      placeholder.hidden = true;

      if (groups.length === 0) {
        canvas.hidden = true;
        empty.hidden = false;
        if (charts.breakdown) { charts.breakdown.destroy(); charts.breakdown = null; }
        return;
      }

      var labels = groups.map(function (g) { return g.key === null ? "(none)" : g.key; });
      var spend = groups.map(function (g) { return g.spend_usd; });

      canvas.hidden = false;
      if (charts.breakdown) { charts.breakdown.destroy(); }
      charts.breakdown = new Chart(canvas, {
        type: "bar",
        data: {
          labels: labels,
          datasets: [{
            label: "Spend",
            data: spend,
            backgroundColor: labels.map(colorFor),
            borderWidth: 0,
          }],
        },
        options: {
          indexAxis: "y",
          responsive: true,
          maintainAspectRatio: false,
          plugins: {
            legend: { display: false },
            tooltip: { callbacks: { label: function (ctx) { return fmtMoney(ctx.parsed.x); } } },
          },
          scales: {
            x: { ticks: { color: "#8a97a8", callback: function (v) { return "$" + v; } }, grid: { color: "#2c3645" } },
            y: { ticks: { color: "#cbd5e1" }, grid: { display: false } },
          },
        },
      });

      // Table.
      var frag = document.createDocumentFragment();
      groups.forEach(function (g) {
        var tr = document.createElement("tr");
        tr.appendChild(td(g.key === null ? "(none)" : g.key));
        tr.appendChild(td(fmtMoney(g.spend_usd), "num"));
        tr.appendChild(td(fmtTokens(g.tokens), "num"));
        tr.appendChild(td(fmtInt(g.call_count), "num"));
        frag.appendChild(tr);
      });
      tbody.appendChild(frag);
    }).catch(function (err) {
      placeholder.hidden = true;
      empty.hidden = false;
      empty.textContent = "Could not load breakdown: " + err.message;
      console.error("breakdown failed:", err);
    });
  }

  function td(text, cls) {
    var el = document.createElement("td");
    el.textContent = text;
    if (cls) { el.className = cls; }
    return el;
  }

  // --- 5. Savings suggestions feed -----------------------------------------

  function loadSuggestions() {
    var placeholder = $("suggestions-placeholder");
    var list = $("suggestions-list");
    var empty = $("suggestions-empty");
    placeholder.hidden = false;
    empty.hidden = true;
    list.innerHTML = "";

    apiGet("/api/suggestions").then(function (data) {
      var suggestions = data.suggestions || [];
      placeholder.hidden = true;

      if (suggestions.length === 0) {
        empty.hidden = false;
        empty.textContent = "No savings suggestions for this range. Nice and efficient.";
        return;
      }

      var frag = document.createDocumentFragment();
      suggestions.forEach(function (s) {
        frag.appendChild(suggestionCard(s));
      });
      list.appendChild(frag);
    }).catch(function (err) {
      placeholder.hidden = true;
      empty.hidden = false;
      empty.textContent = "Could not load suggestions: " + err.message;
      console.error("suggestions failed:", err);
    });
  }

  function suggestionCard(s) {
    var card = document.createElement("div");
    card.className = "suggestion-card";

    var head = document.createElement("div");
    head.className = "suggestion-head";

    var title = document.createElement("div");
    title.className = "suggestion-title";
    title.textContent = s.title || "Suggestion";
    head.appendChild(title);

    var savings = document.createElement("div");
    savings.className = "suggestion-savings";
    savings.textContent = fmtMoney(s.estimated_monthly_savings_usd) + "/mo";
    head.appendChild(savings);

    card.appendChild(head);

    var detail = document.createElement("p");
    detail.className = "suggestion-detail";
    detail.textContent = s.detail || "";
    card.appendChild(detail);

    var conf = document.createElement("span");
    var level = (s.confidence || "medium").toLowerCase();
    conf.className = "confidence conf-" + level;
    conf.textContent = "confidence: " + level;
    card.appendChild(conf);

    return card;
  }

  // --- 6. Budget gauges -----------------------------------------------------

  function loadBudgets() {
    var placeholder = $("budgets-placeholder");
    var list = $("budgets-list");
    var empty = $("budgets-empty");
    placeholder.hidden = false;
    empty.hidden = true;
    list.innerHTML = "";

    // /api/alerts ignores range filters (current period only); fetch plainly.
    fetch("/api/alerts").then(function (r) {
      if (!r.ok) { throw new Error("HTTP " + r.status); }
      return r.json();
    }).then(function (data) {
      var budgets = data.budgets || [];
      placeholder.hidden = true;

      if (budgets.length === 0) {
        empty.hidden = false;
        empty.textContent = "No budgets configured. Set them in config.json under \"budgets\".";
        return;
      }

      var frag = document.createDocumentFragment();
      budgets.forEach(function (b) { frag.appendChild(budgetGauge(b)); });
      list.appendChild(frag);
    }).catch(function (err) {
      placeholder.hidden = true;
      empty.hidden = false;
      empty.textContent = "Could not load budgets: " + err.message;
      console.error("budgets failed:", err);
    });
  }

  function budgetGauge(b) {
    var row = document.createElement("div");
    row.className = "budget-gauge";

    var label = document.createElement("div");
    label.className = "budget-label";
    var scopeName = b.scope === "global" ? "Global" : (b.project || b.scope);
    label.textContent = scopeName + " — " + b.period;
    row.appendChild(label);

    var bar = document.createElement("div");
    bar.className = "budget-bar";
    var fill = document.createElement("div");
    fill.className = "budget-fill level-" + (b.level || "ok");
    var frac = (b.fraction_used === null || b.fraction_used === undefined) ? 0 : b.fraction_used;
    var widthPct = Math.max(0, Math.min(frac, 1)) * 100;
    fill.style.width = widthPct.toFixed(1) + "%";
    bar.appendChild(fill);
    row.appendChild(bar);

    var meta = document.createElement("div");
    meta.className = "budget-meta";
    var fracText = (b.fraction_used === null || b.fraction_used === undefined)
      ? "—" : fmtPct(b.fraction_used);
    meta.textContent = fmtMoney(b.spend) + " / " + fmtMoney(b.budget) + " (" + fracText + ")";
    row.appendChild(meta);

    return row;
  }

  // --- Project filter options (one-time, from breakdown by project) ---------

  function loadProjectOptions() {
    // Use an unfiltered (project-less) breakdown to enumerate projects.
    fetch("/api/breakdown?by=project").then(function (r) {
      return r.ok ? r.json() : { groups: [] };
    }).then(function (data) {
      var sel = $("project-filter");
      var current = state.project;
      // Reset to just "All projects" then repopulate.
      sel.length = 1;
      (data.groups || []).forEach(function (g) {
        if (g.key === null || g.key === "") { return; }
        var opt = document.createElement("option");
        opt.value = g.key;
        opt.textContent = g.key;
        sel.appendChild(opt);
      });
      sel.value = current;
    }).catch(function () { /* non-fatal */ });
  }

  // --- Refresh orchestration -----------------------------------------------

  // Fire all three data fetches independently so the UI fills progressively.
  function refreshAll() {
    loadSummary();
    loadTimeseries();
    loadBreakdown();
    loadSuggestions();
    loadBudgets();
  }

  // --- Wiring ---------------------------------------------------------------

  function setActivePreset(value) {
    var buttons = document.querySelectorAll(".preset");
    buttons.forEach(function (b) {
      b.classList.toggle("active", b.getAttribute("data-range") === value);
    });
  }

  function applyPreset(value) {
    state.rangePreset = value;
    setActivePreset(value);
    if (value === "all") {
      state.from = null;
      state.to = null;
    } else {
      var days = parseInt(value, 10);
      var to = new Date();
      var from = new Date(Date.now() - days * 86400000);
      state.from = from.toISOString().slice(0, 10);
      state.to = to.toISOString().slice(0, 10);
      // Reflect in the custom inputs.
      $("from-date").value = state.from;
      $("to-date").value = state.to;
    }
    refreshAll();
  }

  function wire() {
    document.querySelectorAll(".preset").forEach(function (btn) {
      btn.addEventListener("click", function () {
        applyPreset(btn.getAttribute("data-range"));
      });
    });

    $("apply-custom").addEventListener("click", function () {
      var f = $("from-date").value;
      var t = $("to-date").value;
      state.from = f || null;
      state.to = t || null;
      state.rangePreset = "custom";
      setActivePreset("custom"); // clears preset highlight
      refreshAll();
    });

    $("project-filter").addEventListener("change", function (e) {
      state.project = e.target.value || "";
      refreshAll();
    });

    document.querySelectorAll(".bk-toggle").forEach(function (btn) {
      btn.addEventListener("click", function () {
        state.breakdownBy = btn.getAttribute("data-by");
        document.querySelectorAll(".bk-toggle").forEach(function (b) {
          b.classList.toggle("active", b === btn);
        });
        loadBreakdown();
      });
    });
  }

  function init() {
    wire();
    // Default active toggles.
    document.querySelectorAll(".bk-toggle").forEach(function (b) {
      b.classList.toggle("active", b.getAttribute("data-by") === state.breakdownBy);
    });

    // Header metadata (pricing badge) -- independent of the range.
    fetch("/api/health").then(function (r) { return r.json(); }).then(function (h) {
      renderPricingBadge(h.pricing_updated);
    }).catch(function () { renderPricingBadge(null); });

    loadProjectOptions();
    applyPreset("30"); // default range = last 30 days (SPEC.md §7), triggers refreshAll
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
