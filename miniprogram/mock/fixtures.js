// Mock 示例数据。字段名、类型、可空性与时间格式严格遵循
// schemas/s4_contract.json 0.1.0。本文件只用于 Mock 演示与前期测试，
// 不代表任何真实后端数据；运行时页面会显示 Mock 提示。

const NAIVE_DATE = '2026-10-27';
const TZ_OFFSET = '+08:00';

function pad2(value) {
  return value < 10 ? '0' + value : String(value);
}

function clockParts(baseSeconds, secondsBack) {
  const total = baseSeconds - secondsBack;
  return {
    hour: Math.floor(total / 3600) % 24,
    minute: Math.floor(total / 60) % 60,
    second: total % 60
  };
}

// 本地 naive ISO：不带时区，不做任何时区换算。
function naiveTime(baseSeconds, minutesBack) {
  const p = clockParts(baseSeconds, minutesBack * 60);
  return NAIVE_DATE + 'T' + pad2(p.hour) + ':' + pad2(p.minute) + ':' + pad2(p.second);
}

// 带时区 ISO：显式保留偏移。
function zonedTime(baseSeconds, minutesBack) {
  return naiveTime(baseSeconds, minutesBack) + TZ_OFFSET;
}

function round1(value) {
  return Math.round(value * 10) / 10;
}

const PERSON_STATES = ['Fall', 'Lying', 'Help-Waving', 'Sitting', 'Standing'];
const PRIORITIES = ['P1', 'P2', 'P3'];
const PERSON_BASE = 10 * 3600 + 42 * 60 + 15; // 10:42:15
const SENSOR_BASE = 10 * 3600 + 40 * 60 + 0; // 10:40:00

function buildPersonAlerts() {
  const items = [];
  for (let i = 0; i < 23; i += 1) {
    const minutesBack = i * 7;
    const eventId = 1100 - i;
    items.push({
      alert_key: 'person:' + eventId,
      source: 'person',
      category: 'person',
      type: 'high_risk_alert',
      severity: i % 4 === 3 ? null : (i % 3 === 0 ? 'high' : 'medium'),
      ts: naiveTime(PERSON_BASE, minutesBack),
      person_id: i % 7 === 6 ? null : 100 + i,
      title: i % 4 === 0 ? null : '示例：检测到人员状态 ' + PERSON_STATES[i % PERSON_STATES.length],
      state: i % 6 === 5 ? null : PERSON_STATES[i % PERSON_STATES.length],
      priority: i % 4 === 2 ? null : PRIORITIES[i % PRIORITIES.length],
      risk_score: i % 5 === 0 ? null : round1(95 - i * 1.7)
    });
  }
  return items;
}

function buildSensorAlerts() {
  const items = [];
  for (let i = 0; i < 17; i += 1) {
    const minutesBack = i * 9;
    const alertId = 5000 - i;
    const hasReading = i % 5 !== 4;
    const resolved = i % 3 === 0;
    items.push({
      alert_key: 'sensor:' + alertId,
      source: 'sensor',
      category: 'sensor_environment',
      type: 'high_temperature',
      severity: i % 3 === 0 ? 'high' : (i % 3 === 1 ? 'medium' : null),
      status: resolved ? 'resolved' : 'active',
      temperature: hasReading ? round1(46.5 - i * 0.35) : null,
      humidity: i % 6 === 5 ? null : round1(38 + i * 0.6),
      threshold: i % 5 === 4 ? null : 45,
      occurred_at: zonedTime(SENSOR_BASE, minutesBack),
      resolved_at: resolved ? zonedTime(SENSOR_BASE, minutesBack - 6) : null,
      device_captured_at: i % 7 === 6 ? null : zonedTime(SENSOR_BASE, minutesBack + 1),
      reading_id: hasReading ? 'reading-' + alertId : null,
      received_at: null
    });
  }
  return items;
}

const personAlerts = buildPersonAlerts();
const sensorAlerts = buildSensorAlerts();

const readingsById = {};
sensorAlerts.forEach(function (alert) {
  if (!alert.reading_id) {
    return;
  }
  readingsById[alert.reading_id] = {
    reading_id: alert.reading_id,
    sensor_id: 'sensor-01',
    temperature: alert.temperature,
    humidity: alert.humidity,
    device_captured_at: zonedTime(SENSOR_BASE, (5000 - Number(alert.alert_key.split(':')[1])) * 9 + 1),
    edge_received_at: zonedTime(SENSOR_BASE, (5000 - Number(alert.alert_key.split(':')[1])) * 9),
    received_at: null
  };
});

module.exports = {
  systemStatus: {
    device: { status: 'unknown' },
    modules: {}
  },
  personAlerts: personAlerts,
  sensorAlerts: sensorAlerts,
  readingsById: readingsById,
  allAlerts: function () {
    return personAlerts.concat(sensorAlerts);
  }
};
