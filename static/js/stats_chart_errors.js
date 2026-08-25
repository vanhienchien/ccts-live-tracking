/**
 * stats_chart_errors.js — module panel "Top mã lỗi".
 * Nguồn dữ liệu: GET /api/stats/error-codes.
 * Panel gồm 3 view, chuyển qua lại bằng view-toggle:
 *   1. "Top 10 mã lỗi"      — payload.top20
 *   2. "Top 20 trụ lỗi"     — payload.top_poles
 *   3. "Top thời gian tồn"  — payload.open_long (top 20 ticket đang mở lâu nhất)
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
  let activeView = "errors"; // "errors" | "poles" | "open"

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

  function formatShortTime(isoOrStr) {
    if (!isoOrStr || isoOrStr === "—") return "—";
    const s = String(isoOrStr).trim();
    // "2026-08-22 15:30:00" → "22/08 15:30"
    const m = s.match(/^(\d{4})-(\d{2})-(\d{2})[ T](\d{2}):(\d{2})/);
    if (m) return m[3] + "/" + m[2] + " " + m[4] + ":" + m[5];
    return s.length > 16 ? s.slice(0, 16) : s;
  }

  function drawPolesChart() {
    const payload = currentPayload;
    const top = (payload && payload.top_poles) || {};

    const cpIds = (top.cp_ids || top.labels || []).slice(0, 20);
    const counts = (top.counts || []).slice(0, 20);
    const stations = (top.station_codes || []).slice(0, 20);
    const techs = (top.techs || []).slice(0, 20);
    const breakdowns = (top.error_breakdowns || []).slice(0, 20);
    const latestTimes = (top.latest_create_times || []).slice(0, 20);
    const latestStatuses = (top.latest_ticket_statuses || []).slice(0, 20);
    const latestIds = (top.latest_ticket_ids || []).slice(0, 20);
    const latestOpen = (top.latest_is_open || []).slice(0, 20);

    // Map code → tên lỗi từ top10/top20
    const topErrors = payload.top10 || payload.top20 || {};
    const errorLabels = topErrors.labels || [];
    const errorNames = topErrors.display_names || topErrors.labels || [];
    const codeToName = {};
    errorLabels.forEach((code, idx) => {
      codeToName[code] = errorNames[idx] || code;
    });

    if (!cpIds.length) {
      refs.loadingPoles.style.display = "block";
      refs.loadingPoles.textContent = "Không có trụ nào lên Error Code trong 30 ngày.";
      refs.chartWrapPoles.style.display = "none";
      return;
    }
    refs.loadingPoles.style.display = "none";
    refs.chartWrapPoles.style.display = "block";
    // Tăng chiều cao một chút vì nhãn Y có thêm dòng trạng thái
    refs.chartWrapPoles.style.height = Math.max(320, cpIds.length * 44 + 70) + "px";

    const yLabels = cpIds.map((cp, i) => {
      const station = stations[i] || "—";
      const isOpen = !!latestOpen[i];
      const statusTag = isOpen ? "● Mở" : "○ Đóng";
      const timeShort = formatShortTime(latestTimes[i]);
      return [
        cp + " · " + station,
        statusTag + " · " + timeShort,
      ];
    });

    // Màu cột: đỏ nhạt nếu ticket mới nhất đang mở, giữ palette cũ nếu đã đóng
    const barColors = counts.map((_, i) => {
      if (latestOpen[i]) return "rgba(239, 68, 68, 0.85)"; // red-500
      return BAR_COLORS[i % BAR_COLORS.length];
    });

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
            backgroundColor: "#0f172a",
            titleFont: { size: 13, weight: "600" },
            bodyFont: { size: 12.5 },
            padding: 12,
            cornerRadius: 8,
            callbacks: {
              title: (items) => {
                const i = items[0].dataIndex;
                return "Trụ " + (cpIds[i] || "");
              },
              label: (item) => " Tổng ticket lỗi: " + item.raw,
              afterBody: (items) => {
                const i = items[0].dataIndex;
                const isOpen = !!latestOpen[i];
                const statusLine = latestStatuses[i] || (isOpen ? "Đang mở" : "Đã đóng");
                const lines = [
                  "Trạm: " + (stations[i] || "—"),
                  "KT phụ trách: " + (techs[i] || "—"),
                  "",
                  "── Ticket mới nhất ──",
                  "Ticket ID: " + (latestIds[i] || "—"),
                  "Create Time: " + (latestTimes[i] || "—"),
                  "Trạng thái: " + statusLine,
                  "",
                  "Tần suất mã lỗi:",
                ];
                const bd = breakdowns[i] || [];
                bd.forEach((b) => {
                  const code = b.code || "";
                  const name = codeToName[code] || code;
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
            ticks: {
              color: (ctx) => {
                // Dòng 0 = mã trụ (đen), dòng 1 = trạng thái (đỏ nếu mở, xám nếu đóng)
                const i = ctx.index;
                if (ctx.tick && ctx.tick.label && Array.isArray(ctx.tick.label)) {
                  // Chart.js v3/v4: không dễ đổi màu từng dòng trong multi-line label
                  // → dùng màu mặc định, tooltip + màu cột đã đủ tín hiệu
                }
                return latestOpen[i] ? "#b91c1c" : "#0f172a";
              },
              font: { size: 11, weight: "600" },
              autoSkip: false,
            },
            grid: { display: false },
          },
        },
      },
    });
  }

  // ---------- View 3: Top ticket mở lâu (bảng) ----------

  function escapeHtml(s) {
    if (s == null) return "";
    return String(s)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }

  function statusBadge(status, isReopened) {
    const s = (status || "").toLowerCase();
    let cls = "badge-open";
    let label = status || "—";
    if (s.includes("spare")) { cls = "badge-spare"; }
    else if (s.includes("appointment")) { cls = "badge-appt"; }
    else if (s.includes("asp")) { cls = "badge-asp"; }
    else if (s === "open" && isReopened) { cls = "badge-reopen"; label = "Open (mở lại)"; }
    else if (s === "open") { cls = "badge-open"; }
    return '<span class="status-badge ' + cls + '">' + escapeHtml(label) + "</span>";
  }

  function drawOpenLongTable() {
    const payload = currentPayload;
    const rows = (payload && payload.open_long) || [];

    if (!rows.length) {
      refs.loadingOpen.style.display = "block";
      refs.loadingOpen.textContent = "Không có ticket đang mở trong dữ liệu 60 ngày (sau lọc region).";
      refs.tableWrapOpen.style.display = "none";
      return;
    }
    refs.loadingOpen.style.display = "none";
    refs.tableWrapOpen.style.display = "block";

    const thead = `
      <thead>
        <tr>
          <th>#</th>
          <th>Ticket ID</th>
          <th>Mã trạm</th>
          <th>Mã trụ</th>
          <th>Trạng thái</th>
          <th>Create Time</th>
          <th>Thời gian mở</th>
          <th>Mã / mô tả lỗi</th>
          <th>Ghi chú xử lý</th>
        </tr>
      </thead>`;

    const body = rows.map((r, i) => {
      const note = r.detail_note || "—";
      const noteShort = note.length > 80 ? note.slice(0, 78) + "…" : note;
      const err = r["Error Code"] || "—";
      const errShort = err.length > 36 ? err.slice(0, 34) + "…" : err;
      return `
        <tr>
          <td>${i + 1}</td>
          <td class="mono">${escapeHtml(r["Ticket ID"])}</td>
          <td class="mono">${escapeHtml(r["Station Code"])}</td>
          <td class="mono">${escapeHtml(r["Charge Point ID"])}</td>
          <td>${statusBadge(r["Ticket Status"], r.is_reopened)}</td>
          <td class="mono">${escapeHtml(r["Create Time"])}</td>
          <td><strong>${escapeHtml(r.duration_human || "—")}</strong></td>
          <td title="${escapeHtml(err)}">${escapeHtml(errShort)}</td>
          <td title="${escapeHtml(note)}">${escapeHtml(noteShort)}</td>
        </tr>`;
    }).join("");

    refs.tableOpen.innerHTML = thead + "<tbody>" + body + "</tbody>";
  }

  // ---------- View toggle ----------

  function setActiveView(view) {
    activeView = view;
    refs.toggleBtns.forEach((btn) => {
      btn.classList.toggle("active", btn.dataset.view === view);
    });
    refs.sectionErrors.style.display = view === "errors" ? "" : "none";
    refs.sectionPoles.style.display = view === "poles" ? "" : "none";
    refs.sectionOpen.style.display = view === "open" ? "" : "none";
    if (!currentPayload) return;
    if (view === "errors") drawErrorsChart();
    else if (view === "poles") drawPolesChart();
    else drawOpenLongTable();
  }

  function drawActiveChart() {
    if (activeView === "errors") drawErrorsChart();
    else if (activeView === "poles") drawPolesChart();
    else drawOpenLongTable();
  }

  // ---------- KPI + source note (chung cho panel) ----------

  function renderKPIs(payload) {
    const t = payload.total_with_error_code || 0;
    const u = (payload.top10 || payload.top20 || {}).unique_codes || 0;
    const poleCount = (payload.top_poles || {}).unique_poles || 0;
    const openN = (payload.open_long || []).length;
    setKPIs([
      { label: "Ticket có mã lỗi", value: t.toLocaleString("vi-VN"), sub: "30 ngày" },
      { label: "Số mã lỗi", value: u, sub: "unique codes" },
      { label: "Số trụ lên lỗi", value: poleCount, sub: "unique CP" },
      { label: "Ticket mở (top)", value: openN, sub: "đang mở lâu nhất" },
    ]);
  }

  function renderSourceNote(payload) {
    Core.setSourceBadge(payload.source === "sample" ? "Dữ liệu mẫu" : "Cache");
    Core.setGeneratedAt(payload.generated_at);
    refs.sourceNote.textContent = "Top 10 Error Code · 30 ngày · " + (payload.generated_at || "");
    refs.sourceNotePoles.textContent =
      "Top 20 trụ lỗi · 30 ngày · cột đỏ = ticket mới nhất đang mở · " + (payload.generated_at || "");
    if (refs.sourceNoteOpen) {
      refs.sourceNoteOpen.textContent = "Top 20 ticket đang mở lâu nhất · dữ liệu 60 ngày · " + (payload.generated_at || "");
    }
  }

  // ---------- Load / lifecycle ----------

  async function load() {
    refs.loading.style.display = "block";
    refs.loading.textContent = "⏳ Đang tải top lỗi…";
    refs.loadingPoles.style.display = "block";
    refs.loadingPoles.textContent = "⏳ Đang tải top trụ lỗi…";
    if (refs.loadingOpen) {
      refs.loadingOpen.style.display = "block";
      refs.loadingOpen.textContent = "⏳ Đang tải ticket mở lâu…";
    }
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
      if (refs.loadingOpen) {
        refs.loadingOpen.textContent = "Chưa có dữ liệu ticket mở trong cache — restart để cào lại.";
      }
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
        <button type="button" data-view="open">Top thời gian tồn</button>
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
            <div class="desc">
              Nhóm theo <strong>Mã trụ (Charge Point ID)</strong> · 30 ngày ·
              nhãn Y: <strong>● Mở / ○ Đóng</strong> + thời gian ticket mới nhất ·
              <span style="color:#b91c1c;font-weight:600;">cột đỏ = ticket mới nhất đang mở</span> ·
              hover để xem Ticket ID, Create Time, trạng thái đầy đủ và tần suất mã lỗi
            </div>
          </div>
        </div>
        <div class="loading chart-loading-poles" style="display:none;">⏳ Đang tải top trụ lỗi…</div>
        <div class="chart-wrap chart-wrap-poles" style="display:none; height:420px;"><canvas></canvas></div>
        <div class="source-note source-note-poles"></div>
      </div>

      <div class="section-open" style="display:none;">
        <div class="card-header">
          <div>
            <h2>Top 20 ticket đang mở lâu nhất</h2>
            <div class="desc">
              Trạng thái: <strong>Open / Appointment / Pending for ASP close / Pending for spare parts</strong>
              · dữ liệu 60 ngày · sắp xếp theo thời gian mở giảm dần
            </div>
          </div>
        </div>
        <div class="loading chart-loading-open" style="display:none;">⏳ Đang tải ticket mở lâu…</div>
        <div class="table-wrap-open" style="display:none; overflow-x:auto;">
          <style>
            .table-open { width:100%; border-collapse:collapse; font-size:12.5px; }
            .table-open th { background:#f1f5f9; text-align:left; padding:8px 10px; border-bottom:2px solid #e2e8f0; white-space:nowrap; color:#334155; }
            .table-open td { padding:7px 10px; border-bottom:1px solid #e2e8f0; vertical-align:top; color:#0f172a; }
            .table-open tr:hover td { background:#f8fafc; }
            .table-open .mono { font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace; font-size:12px; }
            .status-badge { display:inline-block; padding:2px 8px; border-radius:999px; font-size:11px; font-weight:600; }
            .badge-open { background:#dbeafe; color:#1e40af; }
            .badge-reopen { background:#fef3c7; color:#92400e; }
            .badge-spare { background:#ffedd5; color:#9a3412; }
            .badge-appt { background:#e0e7ff; color:#3730a3; }
            .badge-asp { background:#dcfce7; color:#166534; }
          </style>
          <table class="table-open"></table>
        </div>
        <div class="source-note source-note-open"></div>
      </div>
    `;
    refs = {
      toggleBtns: Array.from(panel.querySelectorAll(".view-toggle button")),
      sectionErrors: panel.querySelector(".section-errors"),
      sectionPoles: panel.querySelector(".section-poles"),
      sectionOpen: panel.querySelector(".section-open"),
      loading: panel.querySelector(".chart-loading"),
      chartWrap: panel.querySelector(".chart-wrap"),
      canvas: panel.querySelector(".section-errors canvas"),
      sourceNote: panel.querySelector(".source-note"),
      loadingPoles: panel.querySelector(".chart-loading-poles"),
      chartWrapPoles: panel.querySelector(".chart-wrap-poles"),
      canvasPoles: panel.querySelector(".section-poles canvas"),
      sourceNotePoles: panel.querySelector(".source-note-poles"),
      loadingOpen: panel.querySelector(".chart-loading-open"),
      tableWrapOpen: panel.querySelector(".table-wrap-open"),
      tableOpen: panel.querySelector(".table-open"),
      sourceNoteOpen: panel.querySelector(".source-note-open"),
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