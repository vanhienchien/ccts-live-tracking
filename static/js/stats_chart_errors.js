/**
 * stats_chart_errors.js — module biểu đồ "Top 10 mã lỗi".
 * Nguồn dữ liệu: GET /api/stats/error-codes.
 * Tự đăng ký vào StatsCore — xem stats_core.js để biết cách thêm module mới.
 */
(function () {
  "use strict";
  const Core = window.StatsCore;
  const { setKPIs } = Core;

  const BAR_COLORS = [
    "#3b82f6", "#10b981", "#f59e0b", "#ef4444", "#8b5cf6",
    "#0ea5e9", "#ec4899", "#14b8a6", "#f97316", "#6366f1",
  ];

  let chart = null;
  let fullPayload = null;
  let currentPayload = null;
  let panelEl = null;
  let refs = null;

  function slicePayload(full, cp) {
    if (!full) return null;
    const by = full.by_cp_type || {};
    const sub = by[cp] || by.all || full;
    return Object.assign({}, full, sub, {
      by_cp_type: full.by_cp_type,
      counts: full.counts,
      chart_days: full.chart_days,
      source: full.source,
      generated_at: full.generated_at,
      meta: full.meta,
      cp_type: cp,
    });
  }

  function drawChart() {
    const payload = currentPayload;
    const top = (payload && (payload.top10 || payload.top20)) || {};

    const labels = (top.labels || []).slice(0, 10);
    const counts = (top.counts || []).slice(0, 10);
    const displayNames = (top.display_names || top.labels || []).slice(0, 10);
    const techs = (top.top_techs || []).slice(0, 10);
    const techCounts = (top.top_tech_counts || []).slice(0, 10);

    if (!labels.length) {
      refs.loading.style.display = "block";
      refs.loading.textContent = "Không có Error Code trong 30 ngày.";
      refs.chartWrap.style.display = "none";
      return;
    }
    refs.loading.style.display = "none";
    refs.chartWrap.style.display = "block";
    refs.chartWrap.style.height = Math.max(300, labels.length * 40 + 60) + "px";

    const yLabels = labels.map((code, i) => {
      const name = displayNames[i] || code;
      const short = name.length > 42 ? name.slice(0, 40) + "…" : name;
      const tech = techs[i] || "—";
      const tc = techCounts[i] || 0;
      return [short, "KT: " + tech + " (" + tc + ")"];
    });

    const barColors = counts.map((_, i) => BAR_COLORS[i % BAR_COLORS.length]);

    if (chart) chart.destroy();
    chart = new Chart(refs.canvas.getContext("2d"), {
      type: "bar",
      plugins: typeof ChartDataLabels !== "undefined" ? [ChartDataLabels] : [],
      data: {
        labels: yLabels,
        datasets: [{
          label: "Số ticket",
          data: counts,
          backgroundColor: barColors,
          borderRadius: 4,
          barThickness: 16,
        }],
      },
      options: {
        indexAxis: "y",
        responsive: true,
        maintainAspectRatio: false,
        layout: { padding: { right: 48, left: 8 } },
        plugins: {
          legend: { display: false },
          tooltip: {
            callbacks: {
              title: (items) => {
                const i = items[0].dataIndex;
                return displayNames[i] || labels[i];
              },
              label: (item) => " Số ticket: " + item.raw,
              afterBody: (items) => {
                const i = items[0].dataIndex;
                return ["KT nhiều nhất: " + (techs[i] || "—") + " (" + (techCounts[i] || 0) + ")"];
              },
            },
          },
          datalabels: {
            anchor: "end",
            align: "right",
            offset: 6,
            color: "#0f172a",
            font: { size: 12, weight: "700" },
            formatter: (v) => (v == null ? "" : String(v)),
          },
        },
        scales: {
          x: {
            beginAtZero: true,
            grace: "8%",
            ticks: { color: "#64748b", precision: 0 },
            grid: { color: "rgba(148,163,184,.2)" },
          },
          y: {
            ticks: { color: "#0f172a", font: { size: 11, weight: "600" }, autoSkip: false },
            grid: { display: false },
          },
        },
      },
    });
  }

  function renderKPIs(payload) {
    const t = payload.total_with_error_code || 0;
    const u = (payload.top10 || payload.top20 || {}).unique_codes || 0;
    setKPIs([
      { label: "Ticket có mã lỗi", value: t.toLocaleString("vi-VN"), sub: "30 ngày" },
      { label: "Số mã lỗi", value: u, sub: "unique codes" },
      { label: "Top N", value: "10", sub: "mã lỗi" },
      { label: "Ticket (filter)", value: (payload.counts && payload.counts[Core.cpType]) || "—", sub: Core.cpType.toUpperCase() },
    ]);
  }

  function renderSourceNote(payload) {
    Core.setSourceBadge(payload.source === "sample" ? "Dữ liệu mẫu" : "Cache");
    Core.setGeneratedAt(payload.generated_at);
    refs.sourceNote.textContent = "Top 10 Error Code · 30 ngày · " + (payload.generated_at || "");
  }

  async function load() {
    refs.loading.style.display = "block";
    refs.loading.textContent = "⏳ Đang tải top lỗi…";
    try {
      const res = await fetch("/api/stats/error-codes");
      if (!res.ok) throw new Error("HTTP " + res.status);
      const payload = await res.json();
      if (payload.error) throw new Error(payload.error);
      fullPayload = payload;
      currentPayload = slicePayload(payload, Core.cpType);
      renderSourceNote(payload);
      renderKPIs(currentPayload);
      drawChart();
    } catch (e) {
      refs.loading.textContent = "Chưa có Error Code trong cache — restart để cào lại.";
      console.warn(e);
    }
  }

  function onShow() {
    if (!fullPayload) { load(); return; }
    renderSourceNote(fullPayload);
    currentPayload = slicePayload(fullPayload, Core.cpType);
    renderKPIs(currentPayload);
    drawChart();
  }

  function onCpTypeChange() {
    if (!fullPayload) return;
    currentPayload = slicePayload(fullPayload, Core.cpType);
    if (panelEl.style.display !== "none") {
      renderKPIs(currentPayload);
      drawChart();
    }
  }

  function mount(panel) {
    panelEl = panel;
    panel.innerHTML = `
      <div class="card-header">
        <div>
          <h2>Top 10 mã lỗi</h2>
          <div class="desc">Nhóm theo <strong>Error Code</strong> (vd. A0110) · 30 ngày gần nhất · dưới mỗi cột là KT gặp mã đó nhiều nhất</div>
        </div>
      </div>
      <div class="loading chart-loading" style="display:none;">⏳ Đang tải top lỗi…</div>
      <div class="chart-wrap" style="display:none; height:420px;"><canvas></canvas></div>
      <div class="source-note"></div>
    `;
    refs = {
      loading: panel.querySelector(".chart-loading"),
      chartWrap: panel.querySelector(".chart-wrap"),
      canvas: panel.querySelector("canvas"),
      sourceNote: panel.querySelector(".source-note"),
    };
  }

  Core.registerChart({
    key: "errors",
    label: "Top mã lỗi",
    mount,
    onShow,
    onCpTypeChange,
  });
})();