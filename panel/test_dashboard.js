"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");

const {
  beijingDay,
  filterLatestRows,
  memoryChartGeometry,
} = require("./dashboard.js");

test("latest list keeps all rows until a Beijing day is selected", () => {
  const rows = [
    { id: "before-midnight", created: "2026-08-05T15:59:59Z" },
    { id: "after-midnight", created: "2026-08-05T16:00:00Z" },
    { id: "naive-utc", created: "2026-08-05T16:30:00" },
  ];

  assert.equal(beijingDay(rows[0].created), "2026-08-05");
  assert.equal(beijingDay(rows[1].created), "2026-08-06");
  assert.equal(beijingDay(rows[2].created), "2026-08-06");
  assert.deepEqual(filterLatestRows(rows, ""), rows);
  assert.deepEqual(
    filterLatestRows(rows, "2026-08-06").map((row) => row.id),
    ["after-midnight", "naive-utc"],
  );
});

test("thirty-day chart spans the full plot before and after resize", () => {
  const days = Array.from({ length: 30 }, (_, index) => [
    `2026-07-${String(index + 1).padStart(2, "0")}`,
    index % 4,
  ]);

  const wide = memoryChartGeometry(days, 1000);
  assert.equal(wide.points.length, 30);
  assert.equal(wide.points[0].x, wide.plot.left);
  assert.equal(wide.points.at(-1).x, wide.plot.right);
  assert.equal(wide.plot.right, 988);

  const narrow = memoryChartGeometry(days, 640);
  assert.equal(narrow.points[0].x, narrow.plot.left);
  assert.equal(narrow.points.at(-1).x, narrow.plot.right);
  assert.equal(narrow.plot.right, 628);
  assert.ok(narrow.points.at(-1).x < wide.points.at(-1).x);
});
