"use strict";

(function exposeDashboardHelpers(root, factory) {
  const helpers = factory();
  if (typeof module === "object" && module.exports) module.exports = helpers;
  if (root) root.MindMemDashboard = helpers;
})(typeof globalThis === "object" ? globalThis : this, () => {
  const BEIJING_TZ = "Asia/Shanghai";
  const BEIJING_DAY_FORMAT = new Intl.DateTimeFormat("en-CA", {
    timeZone: BEIJING_TZ,
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
  });

  function parseTimestamp(value) {
    if (value instanceof Date) return value;
    if (typeof value === "number") return new Date(value);
    let text = String(value || "").trim();
    if (!text) return null;
    if (/^\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}(\.\d+)?$/.test(text)) {
      text = `${text.replace(" ", "T")}Z`;
    }
    const parsed = new Date(text);
    return Number.isNaN(parsed.getTime()) ? null : parsed;
  }

  function beijingDay(value) {
    const parsed = parseTimestamp(value);
    if (!parsed) return "";
    const parts = Object.fromEntries(BEIJING_DAY_FORMAT.formatToParts(parsed).map((part) => [part.type, part.value]));
    return `${parts.year}-${parts.month}-${parts.day}`;
  }

  function filterLatestRows(rows, selectedDay) {
    if (!selectedDay) return rows;
    return rows.filter((row) => beijingDay(row.created) === selectedDay);
  }

  function memoryChartGeometry(days, requestedWidth, height = 250) {
    const width = Math.max(320, Math.round(Number(requestedWidth) || 0));
    const plot = { left: 43, right: width - 12, top: 12, bottom: height - 29 };
    let cumulative = 0;
    const series = days.map(([day, count]) => {
      const normalizedCount = Number(count) || 0;
      cumulative += normalizedCount;
      return { day, count: normalizedCount, value: cumulative };
    });
    if (!series.length) series.push({ day: "—", count: 0, value: 0 });
    const peak = Math.max(series.at(-1).value, 1);
    const lastIndex = series.length - 1;
    const horizontalSpan = plot.right - plot.left;
    const verticalSpan = plot.bottom - plot.top;
    const points = series.map((row, index) => ({
      ...row,
      x: lastIndex === 0 ? plot.left : index === lastIndex ? plot.right : plot.left + horizontalSpan * (index / lastIndex),
      y: plot.top + verticalSpan * (1 - row.value / peak),
    }));
    return { width, height, plot, peak, points };
  }

  return { BEIJING_TZ, parseTimestamp, beijingDay, filterLatestRows, memoryChartGeometry };
});
