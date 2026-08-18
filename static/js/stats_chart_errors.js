/**
 * stats_chart_errors.js — module panel "Top mã lỗi".
 * Nguồn dữ liệu: GET /api/stats/error-codes.
 * Panel gồm 2 biểu đồ riêng biệt, chuyển qua lại bằng view-toggle (giống
 * pattern "Theo khu vực / Theo kỹ thuật viên / …" ở panel khác):
 *   1. "Top 10 mã lỗi"   — payload.top20    (đã có sẵn)
 *   2. "Top 20 trụ lỗi"  — payload.top_poles (tính trong
 *      stats_charts_top_error_poles.py, được stats_charts_error_codes.py
 *      gộp sẵn vào cùng payload nên chỉ cần 1 lần fetch)
 * Xem stats_core.js để biết cách thêm module mới.
 */
(function () {
  "use strict";
  const Core = window.StatsCore;
  const { setKPIs } = Core;

  const BAR_COLORS = [
    "#3b82f6", "#10b981", "#f59e0b", "#ef4444", "#8b5cf6",
    "#0ea5e9", "#ec4899", "#14b8a6", "#f97316", "#6366f1",
  ];

  let chartErrors = null;
  let chartPoles = null;
  let fullPayload = null;
  let currentPayload = null;
  let panelEl = null;
  let refs = null;
  let activeView = "errors"; // "errors" | "poles"

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

  // ---------- Biểu đồ 1: Top 10 mã lỗi ----------

  function drawErrorsChart() {
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

    if (chartErrors) chartErrors.destroy();
    chartErrors = new Chart(refs.canvas.getContext("2d"), {
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

  // ---------- Biểu đồ 2: Top 20 trụ lỗi ----------

  function drawPolesChart() {
    const payload = currentPayload;
    const top = (payload && payload.top_poles) || {};

    const cpIds = (top.cp_ids || top.labels || []).slice(0, 20);
    const counts = (top.counts || []).slice(0, 20);
    const stations = (top.station_codes || []).slice(0, 20);
    const techs = (top.techs || []).slice(0, 20);
    const breakdowns = (top.error_breakdowns || []).slice(0, 20);

    // === THÊM ĐOẠN NÀY: tạo map code → tên lỗi từ top10/top20 ===
    const topErrors = payload.top10 || payload.top20 || {};
    const errorLabels = topErrors.labels || [];
    const errorNames = topErrors.display_names || topErrors.labels || [];
    const codeToName = {};
    errorLabels.forEach((code, idx) => {
      codeToName[code] = errorNames[idx] || code;
    });
    // =========================================================

    if (!cpIds.length) {
      refs.loadingPoles.style.display = "block";
      refs.loadingPoles.textContent = "Không có trụ nào lên Error Code trong 30 ngày.";
      refs.chartWrapPoles.style.display = "none";
      return;
    }
    refs.loadingPoles.style.display = "none";
    refs.chartWrapPoles.style.display = "block";
    refs.chartWrapPoles.style.height = Math.max(300, cpIds.length * 40 + 60) + "px";

    const yLabels = cpIds.map((cp, i) => {
      const station = stations[i] || "—";
      const tech = techs[i] || "—";
      return [cp + "-" + station];
    });

    const barColors = counts.map((_, i) => BAR_COLORS[i % BAR_COLORS.length]);

    if (chartPoles) chartPoles.destroy();
    chartPoles = new Chart(refs.canvasPoles.getContext("2d"), {
      type: "bar",
      plugins: typeof ChartDataLabels !== "undefined" ? [ChartDataLabels] : [],
      data: {
        labels: yLabels,
        datasets: [{
          label: "Số ticket lỗi",
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
                return "Trụ " + (cpIds[i] || "");
              },
              label: (item) => " Tổng ticket lỗi: " + item.raw,
              afterBody: (items) => {
                const i = items[0].dataIndex;
                const lines = [
                  "Trạm: " + (stations[i] || "—"),
                  "KT phụ trách: " + (techs[i] || "—"),
                  "",
                  "Tần suất mã lỗi:",
                ];
                const bd = breakdowns[i] || [];
                bd.forEach((b) => {
                  const code = b.code || "";
                  const name = codeToName[code] || code;   // ← lấy tên từ map
                  lines.push("  " + name + ": " + b.count);
                });
                return lines;
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

  // ---------- View toggle ----------

  function setActiveView(view) {
    activeView = view;
    refs.toggleBtns.forEach((btn) => {
      btn.classList.toggle("active", btn.dataset.view === view);
    });
    refs.sectionErrors.style.display = view === "errors" ? "" : "none";
    refs.sectionPoles.style.display = view === "poles" ? "" : "none";
    // Chart.js cần canvas đang hiển thị mới tính đúng kích thước, nên chỉ
    // (re)vẽ biểu đồ của view vừa được bật lên.
    if (!currentPayload) return;
    if (view === "errors") drawErrorsChart();
    else drawPolesChart();
  }

  function drawActiveChart() {
    if (activeView === "errors") drawErrorsChart();
    else drawPolesChart();
  }

  // ---------- KPI + source note (chung cho panel) ----------

  function renderKPIs(payload) {
    const t = payload.total_with_error_code || 0;
    const u = (payload.top10 || payload.top20 || {}).unique_codes || 0;
    const poleCount = (payload.top_poles || {}).unique_poles || 0;
    setKPIs([
      { label: "Ticket có mã lỗi", value: t.toLocaleString("vi-VN"), sub: "30 ngày" },
      { label: "Số mã lỗi", value: u, sub: "unique codes" },
      { label: "Số trụ lên lỗi", value: poleCount, sub: "unique CP" },
      { label: "Ticket (filter)", value: (payload.counts && payload.counts[Core.cpType]) || "—", sub: Core.cpType.toUpperCase() },
    ]);
  }

  function renderSourceNote(payload) {
    Core.setSourceBadge(payload.source === "sample" ? "Dữ liệu mẫu" : "Cache");
    Core.setGeneratedAt(payload.generated_at);
    refs.sourceNote.textContent = "Top 10 Error Code · 30 ngày · " + (payload.generated_at || "");
    refs.sourceNotePoles.textContent = "Top 20 trụ lên lỗi nhiều nhất · 30 ngày · " + (payload.generated_at || "");
  }

  // ---------- Load / lifecycle ----------

  async function load() {
    refs.loading.style.display = "block";
    refs.loading.textContent = "⏳ Đang tải top lỗi…";
    refs.loadingPoles.style.display = "block";
    refs.loadingPoles.textContent = "⏳ Đang tải top trụ lỗi…";
    try {
      const res = await fetch("/api/stats/error-codes");
      if (!res.ok) throw new Error("HTTP " + res.status);
      const payload = await res.json();
      if (payload.error) throw new Error(payload.error);
      fullPayload = payload;
      currentPayload = slicePayload(payload, Core.cpType);
      renderSourceNote(payload);
      renderKPIs(currentPayload);
      drawActiveChart();
    } catch (e) {
      refs.loading.textContent = "Chưa có Error Code trong cache — restart để cào lại.";
      refs.loadingPoles.textContent = "Chưa có dữ liệu trụ lỗi trong cache — restart để cào lại.";
      console.warn(e);
    }
  }

  function onShow() {
    if (!fullPayload) { load(); return; }
    renderSourceNote(fullPayload);
    currentPayload = slicePayload(fullPayload, Core.cpType);
    renderKPIs(currentPayload);
    drawActiveChart();
  }

  function onCpTypeChange() {
    if (!fullPayload) return;
    currentPayload = slicePayload(fullPayload, Core.cpType);
    if (panelEl.style.display !== "none") {
      renderKPIs(currentPayload);
      drawActiveChart();
    }
  }

  function mount(panel) {
    panelEl = panel;
    panel.innerHTML = `
      <div class="view-toggle" style="margin-bottom:12px;">
        <button type="button" class="active" data-view="errors">Top 10 mã lỗi</button>
        <button type="button" data-view="poles">Top 20 trụ lỗi</button>
      </div>

      <div class="section-errors">
        <div class="card-header">
          <div>
            <h2>Top 10 mã lỗi</h2>
            <div class="desc">Nhóm theo <strong>Error Code</strong> · 30 ngày gần nhất · dưới mỗi cột là KT gặp mã đó nhiều nhất</div>
          </div>
        </div>
        <div class="loading chart-loading" style="display:none;">⏳ Đang tải top lỗi…</div>
        <div class="chart-wrap" style="display:none; height:420px;"><canvas></canvas></div>
        <div class="source-note"></div>
      </div>

      <div class="section-poles" style="display:none;">
        <div class="card-header">
          <div>
            <h2>Top 20 trụ lỗi</h2>
            <div class="desc">Nhóm theo <strong>Mã trụ (Charge Point ID)</strong> · 30 ngày gần nhất · hover để xem Mã trạm, KT phụ trách và tần suất từng mã lỗi</div>
          </div>
        </div>
        <div class="loading chart-loading-poles" style="display:none;">⏳ Đang tải top trụ lỗi…</div>
        <div class="chart-wrap chart-wrap-poles" style="display:none; height:420px;"><canvas></canvas></div>
        <div class="source-note source-note-poles"></div>
      </div>
    `;
    refs = {
      toggleBtns: Array.from(panel.querySelectorAll(".view-toggle button")),
      sectionErrors: panel.querySelector(".section-errors"),
      sectionPoles: panel.querySelector(".section-poles"),
      loading: panel.querySelector(".chart-loading"),
      chartWrap: panel.querySelector(".chart-wrap"),
      canvas: panel.querySelector(".section-errors canvas"),
      sourceNote: panel.querySelector(".source-note"),
      loadingPoles: panel.querySelector(".chart-loading-poles"),
      chartWrapPoles: panel.querySelector(".chart-wrap-poles"),
      canvasPoles: panel.querySelector(".section-poles canvas"),
      sourceNotePoles: panel.querySelector(".source-note-poles"),
    };
    refs.toggleBtns.forEach((btn) => {
      btn.addEventListener("click", () => setActiveView(btn.dataset.view));
    });
    activeView = "errors";
  }

  Core.registerChart({
    key: "errors",
    label: "Top mã lỗi",
    mount,
    onShow,
    onCpTypeChange,
  });
})();