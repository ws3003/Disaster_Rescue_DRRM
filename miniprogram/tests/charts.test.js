const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const { createRequire } = require('node:module');
const { buildCategoryChart, buildTemperatureChart } = require('../utils/charts');
const { drawTemperatureTrend } = require('../utils/draw-trend');

function sensor(key, temperature, time) {
  return { alert_key: key, temperature, occurred_at: time || '2026-10-27T10:40:00+08:00' };
}

test('分类柱状图使用 total，保留真实零值，未知或失败不冒充零条', () => {
  const model = buildCategoryChart({ total: 18, items: [] }, { total: 0, items: [1, 2, 3] });
  assert.deepEqual(model.bars.map(b => b.value), [18, 0]);
  assert.equal(model.bars[1].missing, false);
  assert.equal(model.bars[1].heightPct, 0);
  const failed = buildCategoryChart({ total: 18, error: { code: 'NETWORK_ERROR' } }, { total: null });
  assert.equal(failed.hasValues, false);
  assert.deepEqual(failed.bars.map(b => b.valueText), ['—', '—']);
  assert.equal(buildCategoryChart({ total: 0 }, { total: 0 }).hasValues, true);
});

test('分类统计部分失败及刷新中能区分，不把缓存当作新结果', () => {
  const failed = buildCategoryChart({ total: 18 }, { total: 17, error: {} });
  assert.equal(failed.hasValues, true);
  assert.equal(failed.partial, true);
  assert.equal(failed.bars[1].value, null);
  const refreshing = buildCategoryChart({ total: 18, loading: true }, { total: 17 });
  assert.equal(refreshing.loading, true);
  assert.equal(refreshing.bars[0].value, 18);
});

test('温度图只取最近三条的副本，保留时区、原始数据及服务端列表顺序', () => {
  const input = [sensor('s:3', 0), sensor('s:2', null, '2026-10-26T08:00:00Z'), sensor('s:1', -5), sensor('s:0', 99)];
  const before = JSON.stringify(input);
  const model = buildTemperatureChart({ items: input });
  assert.equal(JSON.stringify(input), before);
  assert.deepEqual(model.points.map(p => p.key), ['s:1', 's:2', 's:3']);
  assert.deepEqual(model.points.map(p => p.value), [-5, null, 0]);
  assert.equal(model.points[1].timeText, '2026-10-26 08:00:00Z');
  assert.equal(model.points[2].timeText, '2026-10-27 10:40:00+08:00');
  assert.deepEqual(model.points.map(p => p.latest), [false, false, true]);
});

test('温度图错误、空列表、全缺失与单有效点各自保留真实状态', () => {
  assert.equal(buildTemperatureChart({ loading: true }).state, 'loading');
  assert.equal(buildTemperatureChart({ items: [] }).state, 'empty');
  const stale = buildTemperatureChart({ error: {}, items: [sensor('s:1', 10)] });
  assert.equal(stale.state, 'error');
  assert.equal(stale.hasValues, false);
  const invalid = buildTemperatureChart({ items: [sensor('s:1', null), sensor('s:2', '12'), sensor('s:3', Infinity)] });
  assert.equal(invalid.state, 'missing');
  assert.equal(invalid.points.length, 3);
  assert.equal(buildTemperatureChart({ items: [sensor('s:1', 12)] }).availableCount, 1);
});

function recordingContext() {
  const strokes = [], labels = [], circles = [];
  let active = [];
  return {
    strokes, labels, circles,
    clearRect() {}, beginPath() { active = []; },
    moveTo(x, y) { active.push(['move', x, y]); },
    lineTo(x, y) { active.push(['line', x, y]); },
    stroke() { if (active.some(p => p[0] === 'line')) strokes.push({ color: this.strokeStyle, points: active.slice() }); },
    fillText(text) { labels.push(text); },
    arc(x, y, r) { circles.push({ x, y, r }); }, fill() {}, scale() {}
  };
}

test('折线实际绘制不跨缺失点连线，不为单点制造走势；数值不被零填充', () => {
  const missing = buildTemperatureChart({ items: [sensor('s:3', 8), sensor('s:2', null), sensor('s:1', 0)] });
  const ctx = recordingContext();
  drawTemperatureTrend(ctx, missing, 300, 190);
  assert.equal(ctx.strokes.filter(s => s.color === '#0e7490').length, 0);
  assert.equal(ctx.circles.length, 2);
  assert.ok(ctx.labels.includes('0'));
  const single = recordingContext();
  drawTemperatureTrend(single, buildTemperatureChart({ items: [sensor('s:1', 8)] }), 300, 190);
  assert.equal(single.circles.length, 1);
  assert.equal(single.strokes.filter(s => s.color === '#0e7490').length, 0);
  const connected = recordingContext();
  drawTemperatureTrend(connected, buildTemperatureChart({ items: [sensor('s:2', 9), sensor('s:1', 8)] }), 300, 190);
  assert.equal(connected.strokes.filter(s => s.color === '#0e7490').length, 1);
});

test('首页将现有结果接入图表，分类入口保留原路由', () => {
  const file = path.join(__dirname, '../pages/home/index.js');
  let page;
  const urls = [];
  vm.runInNewContext(fs.readFileSync(file, 'utf8'), {
    require: createRequire(file), Page(definition) { page = definition; },
    wx: { navigateTo(options) { urls.push(options.url); } },
    clearTimeout, setTimeout
  });
  page.setData = function (patch) { Object.assign(this.data, patch); };
  page.applyState({ loading: false, status: {}, person: { items: [], total: 18 },
    sensor: { items: [sensor('sensor:1', 46.5)], total: 17 }, summary: {} });
  assert.equal(page.data.categoryChart.bars[0].value, 18);
  assert.equal(page.data.temperatureChart.points[0].value, 46.5);
  page.onChartCategoryTap({ detail: { category: 'person' } });
  page.onChartCategoryTap({ detail: { category: 'sensor_environment' } });
  page.onChartCategoryTap({ detail: { category: 'invalid' } });
  assert.deepEqual(urls, ['/pages/alerts/index?category=person', '/pages/alerts/index?category=sensor_environment']);
});

function component() {
  const file = path.join(__dirname, '../components/monitor-charts/index.js');
  let definition;
  const queries = [];
  vm.runInNewContext(fs.readFileSync(file, 'utf8'), {
    require: createRequire(file), Component(value) { definition = value; },
    wx: { nextTick(fn) { fn(); }, getWindowInfo() { return { pixelRatio: 2 }; } }
  });
  const instance = {
    properties: { temperatureChart: buildTemperatureChart({ items: [sensor('sensor:1', 12)] }) },
    data: {}, setData(patch, callback) { Object.assign(this.data, patch); if (callback) callback(); },
    createSelectorQuery() { return { select() { return this; }, fields() { return this; }, exec(fn) { queries.push(fn); } }; }
  };
  Object.keys(definition.methods).forEach(key => { instance[key] = definition.methods[key].bind(instance); });
  return { instance, definition, queries };
}

test('Canvas 迟到查询不能覆盖新结果，组件卸载后不再绘图', () => {
  const { instance, definition, queries } = component();
  definition.lifetimes.ready.call(instance);
  instance.scheduleDraw();
  const ctx = recordingContext();
  const node = { getContext() { return ctx; } };
  queries[0]([{ node, width: 300, height: 190 }]);
  assert.equal(ctx.circles.length, 0);
  queries[1]([{ node, width: 300, height: 190 }]);
  assert.equal(ctx.circles.length, 1);
  assert.equal(node.width, 600);
  instance.scheduleDraw();
  definition.lifetimes.detached.call(instance);
  queries[2]([{ node, width: 300, height: 190 }]);
  assert.equal(ctx.circles.length, 1);
});

test('Canvas 不可用时保留原始数值供原生列表展示', () => {
  const { instance, definition, queries } = component();
  definition.lifetimes.ready.call(instance);
  queries[0]([{}]);
  assert.equal(instance.data.canvasUnavailable, true);
  assert.equal(instance.properties.temperatureChart.points[0].value, 12);
});
