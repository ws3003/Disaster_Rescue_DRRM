// L3：展示层不改变时间语义，不虚构读数/媒体。
const test = require('node:test');
const assert = require('node:assert/strict');

const { isNaiveIso, isZonedIso, formatNaiveTime, formatZonedTime } = require('../utils/time');
const {
  buildAlertItemView,
  buildDetailView,
  buildRelatedView,
  buildSystemStatusView,
  buildSummaryView
} = require('../utils/view');
const fixtures = require('../mock/fixtures');

test('naive / zoned ISO 判定', () => {
  assert.equal(isNaiveIso('2026-10-27T10:42:15'), true);
  assert.equal(isNaiveIso('2026-10-27T10:42:15.123'), true);
  assert.equal(isNaiveIso('2026-10-27T10:42:15+08:00'), false);
  assert.equal(isNaiveIso('2026-10-27T10:42:15Z'), false);

  assert.equal(isZonedIso('2026-10-27T10:42:15+08:00'), true);
  assert.equal(isZonedIso('2026-10-27T10:42:15Z'), true);
  assert.equal(isZonedIso('2026-10-27T10:42:15'), false);
});

test('naive 时间展示不得附加时区', () => {
  const displayed = formatNaiveTime('2026-10-27T10:42:15');
  assert.equal(displayed, '2026-10-27 10:42:15');
  assert.equal(/[Zz]|[+-]\d{2}:\d{2}$/.test(displayed), false);
});

test('带时区时间展示保留原偏移', () => {
  const displayed = formatZonedTime('2026-10-27T10:40:00+08:00');
  assert.equal(displayed, '2026-10-27 10:40:00+08:00');
  assert.match(displayed, /\+08:00$/);
});

test('人员/传感器列表视图字段与标签', () => {
  const person = buildAlertItemView(fixtures.personAlerts[0]);
  assert.equal(person.categoryLabel, '人员告警');
  assert.equal(person.timeLabel, '事件时间');
  assert.match(person.timeText, /^2026-10-27 \d{2}:\d{2}:\d{2}$/);
  assert.ok(person.chips.length > 0);

  const sensor = buildAlertItemView(fixtures.sensorAlerts[0]);
  assert.equal(sensor.categoryLabel, '传感器环境告警');
  assert.equal(sensor.timeLabel, '发生时间');
  assert.match(sensor.statusText, /resolved|active/);
  assert.ok(sensor.chips.some(function (chip) { return chip.label === '温度'; }));
});

test('详情视图字段数量与可空字段展示', () => {
  const personView = buildDetailView(fixtures.personAlerts[0]);
  assert.equal(personView.fields.length, 11);
  assert.ok(personView.fields.some(function (field) {
    return field.label === '事件时间（本地无时区）';
  }));

  const sensorView = buildDetailView(fixtures.sensorAlerts[0]);
  assert.equal(sensorView.fields.length, 14);
  assert.ok(sensorView.fields.some(function (field) {
    return field.label === '发生时间（含时区）';
  }));
});

test('关联数据为空时不生成虚构读数或媒体 URL', () => {
  const related = buildRelatedView({
    readings: [],
    media: [],
    media_supported: false
  });
  assert.equal(related.hasReadings, false);
  assert.match(related.mediaText, /尚未支持/);
  assert.equal(/https?:\/\//.test(related.mediaText), false);

  const unsupportedWithMedia = buildRelatedView({
    readings: [],
    media: [{ url: 'https://fake.example/x.mp4' }],
    media_supported: true
  });
  assert.equal(unsupportedWithMedia.hasReadings, false);
  assert.equal(/https?:\/\//.test(unsupportedWithMedia.mediaText), false);
  assert.match(unsupportedWithMedia.mediaText, /未定义媒体项字段/);
});

test('系统状态视图如实展示契约占位值', () => {
  const view = buildSystemStatusView({ device: { status: 'unknown' }, modules: {} });
  assert.match(view.deviceStatusText, /未知/);
  assert.match(view.deviceStatusText, /占位/);
  assert.equal(view.modulesEmpty, true);
});

test('摘要缺数据时显示 —，不补零', () => {
  const view = buildSummaryView(null, 17);
  assert.equal(view.personText, '—');
  assert.equal(view.sensorText, '17');
});
