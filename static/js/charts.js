/* Chart helpers built on Chart.js 4. Colours come from CSS tokens so light/dark modes stay in sync.
   Conventions: 2px lines, thin bars with rounded data-ends, recessive grid, hover tooltips on every chart. */
(function () {
  "use strict";
  const SIS = (window.SIS = window.SIS || {});
  const registry = [];

  function token(name) {
    return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
  }
  function alpha(hex, a) {
    const h = hex.replace("#", "");
    const n = parseInt(h.length === 3 ? h.split("").map((c) => c + c).join("") : h, 16);
    return `rgba(${(n >> 16) & 255}, ${(n >> 8) & 255}, ${n & 255}, ${a})`;
  }
  function shortDate(iso) {
    const d = new Date(iso + "T00:00:00");
    return d.toLocaleDateString(undefined, { day: "numeric", month: "short" });
  }

  function baseOptions(extra) {
    const ink = token("--chart-ink"), grid = token("--chart-grid"), muted = token("--chart-muted");
    Chart.defaults.font.family = getComputedStyle(document.body).fontFamily;
    Chart.defaults.font.size = 11;
    Chart.defaults.color = muted;
    return Object.assign({
      responsive: true,
      maintainAspectRatio: false,
      animation: { duration: 250 },
      interaction: { mode: "index", intersect: false },
      plugins: {
        legend: { display: false },
        tooltip: {
          backgroundColor: token("--sis-surface") || "#fff",
          titleColor: token("--sis-ink"), bodyColor: ink, borderColor: token("--sis-border"), borderWidth: 1,
          padding: 10, boxPadding: 4, usePointStyle: true, cornerRadius: 8,
        },
      },
      scales: {
        x: { grid: { display: false }, border: { color: token("--chart-axis") }, ticks: { maxRotation: 0, autoSkipPadding: 18, color: muted } },
        y: { beginAtZero: true, grid: { color: grid, drawTicks: false }, border: { display: false }, ticks: { padding: 8, color: muted } },
      },
    }, extra || {});
  }

  function mount(canvas, build) {
    if (!canvas || typeof Chart === "undefined") return;
    const entry = { canvas, build, chart: null };
    entry.chart = new Chart(canvas, build());
    registry.push(entry);
  }
  document.addEventListener("sis:themechange", function () {
    registry.forEach(function (e) { e.chart.destroy(); e.chart = new Chart(e.canvas, e.build()); });
  });

  /** History (solid) + forecast (dashed) with an 80% interval band. */
  SIS.historyForecastChart = function (canvas, data, opts) {
    opts = opts || {};
    mount(canvas, function () {
      const c1 = token("--series-1"), c2 = token("--series-2");
      const hist = data.history || [], fc = data.forecast || [];
      const labels = hist.map((p) => p.x).concat(fc.map((p) => p.x));
      const pad = (arr, before) => new Array(before).fill(null).concat(arr);
      const histVals = hist.map((p) => (opts.key ? p[opts.key] : p.y));
      return {
        type: "line",
        data: {
          labels,
          datasets: [
            { label: opts.historyLabel || "Actual", data: histVals, borderColor: c1, backgroundColor: c1, borderWidth: 2, pointRadius: 0, pointHoverRadius: 4, tension: 0.25 },
            { label: "Forecast", data: pad(fc.map((p) => p.y), hist.length), borderColor: c2, backgroundColor: c2, borderWidth: 2, borderDash: [5, 4], pointRadius: 0, pointHoverRadius: 4, tension: 0.25 },
            { label: "80% interval (upper)", data: pad(fc.map((p) => p.hi), hist.length), borderWidth: 0, pointRadius: 0, fill: "+1", backgroundColor: alpha(c2, 0.14) },
            { label: "80% interval (lower)", data: pad(fc.map((p) => p.lo), hist.length), borderWidth: 0, pointRadius: 0, fill: false },
          ],
        },
        options: baseOptions({
          scales: Object.assign(baseOptions().scales, {
            x: Object.assign(baseOptions().scales.x, { ticks: { maxRotation: 0, autoSkipPadding: 24, callback: function (v) { return shortDate(this.getLabelForValue(v)); } } }),
          }),
          plugins: Object.assign(baseOptions().plugins, {
            tooltip: Object.assign(baseOptions().plugins.tooltip, {
              filter: (item) => item.raw !== null && !item.dataset.label.startsWith("80%"),
              callbacks: {
                title: (items) => shortDate(items[0].label),
                label: (item) => ` ${item.dataset.label}: ${SIS.fmtNumber(item.raw, 1)}${opts.unit ? " " + opts.unit : ""}`,
                afterBody: (items) => {
                  const i = items[0].dataIndex - hist.length;
                  return i >= 0 && fc[i] ? [`Range: ${SIS.fmtNumber(fc[i].lo, 1)} – ${SIS.fmtNumber(fc[i].hi, 1)}`] : [];
                },
              },
            }),
          }),
        }),
      };
    });
  };

  /** Single-series line (e.g. revenue). */
  SIS.lineChart = function (canvas, points, opts) {
    opts = opts || {};
    mount(canvas, function () {
      const c = token(opts.color || "--series-1");
      return {
        type: "line",
        data: {
          labels: points.map((p) => p.x),
          datasets: [{ label: opts.label || "Value", data: points.map((p) => p.y), borderColor: c, backgroundColor: alpha(c, 0.08), fill: true, borderWidth: 2, pointRadius: 0, pointHoverRadius: 4, tension: 0.25 }],
        },
        options: baseOptions({
          scales: Object.assign(baseOptions().scales, {
            x: Object.assign(baseOptions().scales.x, { ticks: { maxRotation: 0, autoSkipPadding: 24, callback: function (v) { return shortDate(this.getLabelForValue(v)); } } }),
            y: Object.assign(baseOptions().scales.y, { ticks: { padding: 8, callback: (v) => (opts.money ? SIS.fmtNumber(v / 1000) + "k" : SIS.fmtNumber(v)) } }),
          }),
          plugins: Object.assign(baseOptions().plugins, {
            tooltip: Object.assign(baseOptions().plugins.tooltip, {
              callbacks: {
                title: (items) => shortDate(items[0].label),
                label: (item) => " " + (opts.money ? SIS.fmtMoney(item.raw) : SIS.fmtNumber(item.raw, 1)),
              },
            }),
          }),
        }),
      };
    });
  };

  /** Horizontal bars (ranking). colors: optional array of CSS token names per bar. */
  SIS.barChart = function (canvas, rows, opts) {
    opts = opts || {};
    mount(canvas, function () {
      const colors = rows.map((r, i) => token((opts.colors && opts.colors[i]) || "--series-1"));
      return {
        type: "bar",
        data: {
          labels: rows.map((r) => r.label),
          datasets: [{ label: opts.label || "Value", data: rows.map((r) => r.value), backgroundColor: colors, borderRadius: 4, borderSkipped: "start", barThickness: opts.thickness || 14, maxBarThickness: 18 }],
        },
        options: baseOptions({
          indexAxis: opts.vertical ? "x" : "y",
          interaction: { mode: "nearest", intersect: true, axis: opts.vertical ? "x" : "y" },
          scales: opts.vertical ? baseOptions().scales : {
            x: { beginAtZero: true, grid: { color: token("--chart-grid"), drawTicks: false }, border: { display: false }, ticks: { color: token("--chart-muted"), callback: (v) => (opts.money ? SIS.fmtNumber(v / 1000) + "k" : SIS.fmtNumber(v)) } },
            y: { grid: { display: false }, border: { color: token("--chart-axis") }, ticks: { color: token("--chart-ink") } },
          },
          plugins: Object.assign(baseOptions().plugins, {
            tooltip: Object.assign(baseOptions().plugins.tooltip, {
              callbacks: {
                label: (item) => {
                  const r = rows[item.dataIndex];
                  let s = " " + (opts.money ? SIS.fmtMoney(item.raw) : SIS.fmtNumber(item.raw, 1));
                  if (r.qty !== undefined) s += ` · ${SIS.fmtNumber(r.qty)} units`;
                  return s;
                },
              },
            }),
          }),
        }),
      };
    });
  };

  /** Sensor estimate vs ledger (IoT device page). */
  SIS.sensorChart = function (canvas, points) {
    mount(canvas, function () {
      const c1 = token("--series-1"), c2 = token("--series-3");
      return {
        type: "line",
        data: {
          labels: points.map((p) => p.x),
          datasets: [
            { label: "Sensor estimate", data: points.map((p) => p.est), borderColor: c1, backgroundColor: c1, borderWidth: 2, pointRadius: 0, pointHoverRadius: 4 },
            { label: "Ledger stock", data: points.map((p) => p.rec), borderColor: c2, backgroundColor: c2, borderWidth: 2, borderDash: [5, 4], pointRadius: 0, pointHoverRadius: 4, stepped: true },
          ],
        },
        options: baseOptions({
          scales: Object.assign(baseOptions().scales, {
            x: Object.assign(baseOptions().scales.x, { ticks: { maxRotation: 0, autoSkipPadding: 30, callback: function (v) { const d = new Date(this.getLabelForValue(v)); return d.toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit" }); } } }),
            y: Object.assign(baseOptions().scales.y, { beginAtZero: false }),
          }),
          plugins: Object.assign(baseOptions().plugins, {
            tooltip: Object.assign(baseOptions().plugins.tooltip, {
              callbacks: { title: (items) => new Date(items[0].label).toLocaleString(), label: (i) => ` ${i.dataset.label}: ${SIS.fmtNumber(i.raw, 2)}` },
            }),
          }),
        }),
      };
    });
  };
})();
