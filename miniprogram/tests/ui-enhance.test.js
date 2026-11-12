// L3：展示层增强（相对时间、日期分组、本地筛选、字段分组）不改变契约语义。
const test = require('node:test');
const assert = require('node:assert/strict');

const {
  dateKeyOf,
  formatRelativeTime,
  dateGroupLabel
} = require('../utils/time');
const {
  buildAlertItemView,
  buildAlertListViews,
  decorateListGroups,
  buildDetailView
} = require('../utils/view');
const { filterAlertViews } = require('../utils/filter');
const fixtures = require('../mock/fixtures');

const NOW = new Date(2026, 9, 27, 12, 0, 0).getTime(); // 2026-10-27 12:00 本地时间

function msAgo(minutes) {
  return NOW - minutes * 60 * 1000;
}

test('dateKeyOf 只取字符串日期前缀，不做时区换算', () => {
  assert.equal(dateKeyOf('2026-10-27T10:42:15'), '2026-10-27');
  assert.equal(dateKeyOf('2026-10-27T10:40:00+08:00'), '2026-10-27');
  assert.equal(dateKeyOf('not-a-date'), '');
  assert.equal(dateKeyOf(null), '');
});

test('相对时间：刚刚 / 分钟 / 小时 / 天，无法解析时返回空串', () => {
  assert.equal(formatRelativeTime('2026-10-27T11:59:30', NOW), '刚刚');
  assert.equal(formatRelativeTime('2026-10-27T11:57:00', NOW), '3 分钟前');
  assert.equal(formatRelativeTime('2026-10-27T09:00:00', NOW), '3 小时前');
  assert.equal(formatRelativeTime('2026-10-24T12:00:00', NOW), '3 天前');
  assert.equal(formatRelativeTime('2026-10-27T12:01:00', NOW), '1 分钟后');
  assert.equal(formatRelativeTime('bad', NOW), '');
  assert.equal(formatRelativeTime(null, NOW), '');
});

test('带时区时间的相对时间按 ISO 偏移解析（与时区无关）', () => {
  const zonedNow = Date.parse('2026-10-27T12:00:00+08:00');
  assert.equal(formatRelativeTime('2026-10-27T11:00:00+08:00', zonedNow), '1 小时前');
  assert.equal(formatRelativeTime('2026-10-27T03:00:00Z', zonedNow), '1 小时前');
  assert.equal(formatRelativeTime('2026-10-27T13:30:00+08:00', zonedNow), '1 小时后');
});

test('日期分组标题：今天 / 昨天 / 今年日期 / 跨年日期', () => {
  assert.equal(dateGroupLabel('2026-10-27', NOW), '今天');
  assert.equal(dateGroupLabel('2026-10-26', NOW), '昨天');
  assert.equal(dateGroupLabel('2026-10-20', NOW), '10月20日');
  assert.equal(dateGroupLabel('2025-12-31', NOW), '2025年12月31日');
  assert.equal(dateGroupLabel('', NOW), '');
});

test('列表项视图带相对时间与严重程度配色，不改动原始时间文本', () => {
  const dto = Object.assign({}, fixtures.personAlerts[0], { ts: '2026-10-27T11:57:00' });
  const view = buildAlertItemView(dto, { nowMs: NOW });
  assert.equal(view.timeText, '2026-10-27 11:57:00');
  assert.equal(view.relativeText, '3 分钟前');
  assert.equal(view.dateKey, '2026-10-27');
  assert.equal(view.severityClass, 'sev-' + String(dto.severity).toLowerCase());
});

test('日期/类别分组只在相邻变化时插入标题，且保持原有顺序', () => {
  const views = buildAlertListViews(
    [
      Object.assign({}, fixtures.personAlerts[0], { ts: '2026-10-27T11:00:00' }),
      Object.assign({}, fixtures.personAlerts[1], { ts: '2026-10-27T08:00:00' }),
      Object.assign({}, fixtures.personAlerts[2], { ts: '2026-10-26T08:00:00' }),
      Object.assign({}, fixtures.sensorAlerts[0], { occurred_at: '2026-10-25T08:00:00+08:00' })
    ],
    { nowMs: NOW }
  );
  const decorated = decorateListGroups(views, { nowMs: NOW, showCategoryHeader: true });

  assert.equal(decorated[0].dateHeader, '今天');
  assert.equal(decorated[1].dateHeader, '');
  assert.equal(decorated[2].dateHeader, '昨天');
  assert.equal(decorated[3].dateHeader, '10月25日');
  assert.equal(decorated[0].categoryHeader, '人员告警');
  assert.equal(decorated[1].categoryHeader, '');
  assert.equal(decorated[3].categoryHeader, '传感器环境告警');
  // 顺序不变
  assert.deepEqual(
    decorated.map(function (v) { return v.alertKey; }),
    views.map(function (v) { return v.alertKey; })
  );
});

test('不勾选类别标题时不在同类别切换处插入标题', () => {
  const views = buildAlertListViews(fixtures.personAlerts.slice(0, 2), { nowMs: NOW });
  const decorated = decorateListGroups(views, { nowMs: NOW, showCategoryHeader: false });
  assert.equal(decorated[1].categoryHeader, '');
});

test('本地筛选：按 key / 标题 / chips 匹配，空关键词返回全量', () => {
  const views = buildAlertListViews(fixtures.personAlerts.slice(0, 3), { nowMs: NOW });

  const all = filterAlertViews(views, '   ');
  assert.equal(all.active, false);
  assert.equal(all.views.length, 3);

  const byKey = filterAlertViews(views, views[0].alertKey);
  assert.equal(byKey.active, true);
  assert.equal(byKey.matched, 1);
  assert.equal(byKey.views[0].alertKey, views[0].alertKey);

  const byChip = filterAlertViews(views, String(fixtures.personAlerts[1].person_id));
  assert.ok(byChip.matched >= 1);

  const none = filterAlertViews(views, 'zzz-not-exist');
  assert.equal(none.matched, 0);
  assert.equal(none.total, 3);
});

test('本地筛选：按严重程度与状态组合过滤（状态仅传感器告警有）', () => {
  const personViews = buildAlertListViews(fixtures.personAlerts, { nowMs: NOW });
  const sensorViews = buildAlertListViews(fixtures.sensorAlerts, { nowMs: NOW });

  const highPerson = filterAlertViews(personViews, '', { severity: 'high' });
  assert.equal(highPerson.active, true);
  assert.ok(highPerson.views.length > 0);
  assert.ok(highPerson.views.every(function (v) { return v.severityKey === 'high'; }));

  const activeSensor = filterAlertViews(sensorViews, '', { status: 'active' });
  assert.ok(activeSensor.views.every(function (v) { return v.statusKey === 'active'; }));

  // 人员告警没有 status 字段，按状态筛选时不应被计入
  const activeAll = filterAlertViews(personViews.concat(sensorViews), '', { status: 'active' });
  assert.ok(activeAll.views.every(function (v) { return v.category === 'sensor_environment'; }));

  const combined = filterAlertViews(sensorViews, '', { severity: 'high', status: 'resolved' });
  assert.ok(combined.views.every(function (v) {
    return v.severityKey === 'high' && v.statusKey === 'resolved';
  }));

  const none = filterAlertViews(personViews, '', { status: 'active' });
  assert.equal(none.matched, 0);
});

test('详情视图读数对比：温度对阈值，湿度不虚构阈值', () => {
  const sensorDto = fixtures.sensorAlerts[0];
  const sensor = buildDetailView(sensorDto, { nowMs: NOW });
  const temp = sensor.metrics.find(function (m) { return m.label === '温度'; });
  const humidity = sensor.metrics.find(function (m) { return m.label === '湿度'; });

  assert.ok(temp, '温度指标应存在');
  assert.equal(temp.thresholdText, String(sensorDto.threshold) + ' ℃');
  assert.equal(typeof temp.fillPct, 'number');
  assert.ok(temp.fillPct > 0 && temp.fillPct <= 100);
  assert.equal(temp.over, sensorDto.temperature > sensorDto.threshold);

  assert.ok(humidity, '湿度指标应存在');
  assert.equal(humidity.thresholdPct, null);
  assert.equal(humidity.over, null);
  assert.equal(humidity.thresholdText, '');

  // 人员告警没有触发快照字段，不生成任何指标
  const person = buildDetailView(fixtures.personAlerts[0], { nowMs: NOW });
  assert.deepEqual(person.metrics, []);
});

test('详情视图分组标题覆盖全部字段，字段总数不变', () => {
  const person = buildDetailView(fixtures.personAlerts[0], { nowMs: NOW });
  assert.equal(person.fields.length, 11);
  const personGrouped = person.fieldGroups.reduce(function (sum, group) {
    return sum + group.fields.length;
  }, 0);
  assert.equal(personGrouped, 11);
  assert.deepEqual(person.fieldGroups.map(function (g) { return g.title; }), ['告警信息', '人员信息', '时间']);

  const sensor = buildDetailView(fixtures.sensorAlerts[0], { nowMs: NOW });
  assert.equal(sensor.fields.length, 14);
  const sensorGrouped = sensor.fieldGroups.reduce(function (sum, group) {
    return sum + group.fields.length;
  }, 0);
  assert.equal(sensorGrouped, 14);
  assert.deepEqual(
    sensor.fieldGroups.map(function (g) { return g.title; }),
    ['告警信息', '触发快照', '时间', '关联标识']
  );
});
