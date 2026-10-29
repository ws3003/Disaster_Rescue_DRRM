// L1/L3：Mock 适配器必须实现契约的分组、排序、分页、空列表与关联规则。
const test = require('node:test');
const assert = require('node:assert/strict');

const {
  createMockAdapter,
  sortPersonAlerts,
  sortSensorAlerts
} = require('../mock/mock-adapter');
const fixtures = require('../mock/fixtures');
const {
  parseEnvelope,
  validateAlertListData,
  validateAlertDetailData,
  validateRelatedData,
  validateSystemStatusData
} = require('../utils/envelope');

const adapter = createMockAdapter({ latencyMs: 0 });

function numericId(key) {
  return Number(key.split(':')[1]);
}

test('默认列表：人员组优先、跨组分页、has_more 由公式决定', async () => {
  const page1 = await adapter.request('/api/v1/alerts', { page: 1, page_size: 20 });
  assert.equal(page1.statusCode, 200);
  const parsed1 = parseEnvelope(page1.body);
  assert.equal(parsed1.status, 'success');
  assert.equal(page1.body.data.length, 20);
  assert.equal(page1.body.meta.total, 40);
  assert.equal(page1.body.meta.has_more, true);
  assert.ok(page1.body.data.every(function (item) {
    return item.category === 'person';
  }));

  const page2 = await adapter.request('/api/v1/alerts', { page: 2, page_size: 20 });
  assert.equal(page2.body.data.length, 20);
  assert.equal(page2.body.meta.total, 40);
  assert.equal(page2.body.meta.has_more, false);
  assert.equal(
    page2.body.data.filter(function (item) { return item.category === 'person'; }).length,
    3
  );
  assert.equal(page2.body.data[3].category, 'sensor_environment');
});

test('category 筛选只返回对应组，total 与 has_more 按该组计算', async () => {
  const person1 = await adapter.request('/api/v1/alerts', {
    category: 'person',
    page: 1,
    page_size: 20
  });
  assert.equal(person1.body.data.length, 20);
  assert.equal(person1.body.meta.total, 23);
  assert.equal(person1.body.meta.has_more, true);
  assert.ok(person1.body.data.every(function (item) {
    return item.category === 'person';
  }));

  const person2 = await adapter.request('/api/v1/alerts', {
    category: 'person',
    page: 2,
    page_size: 20
  });
  assert.equal(person2.body.data.length, 3);
  assert.equal(person2.body.meta.has_more, false);

  const sensor = await adapter.request('/api/v1/alerts', {
    category: 'sensor_environment',
    page: 1,
    page_size: 100
  });
  assert.equal(sensor.body.data.length, 17);
  assert.equal(sensor.body.meta.total, 17);
  assert.equal(sensor.body.meta.has_more, false);
  assert.ok(sensor.body.data.every(function (item) {
    return item.category === 'sensor_environment';
  }));
});

test('组内排序：人员按 ts DESC + event_id DESC；传感器按 occurred_at DESC + alert_id DESC', () => {
  const persons = sortPersonAlerts(fixtures.personAlerts);
  for (let i = 1; i < persons.length; i += 1) {
    assert.ok(persons[i - 1].ts >= persons[i].ts);
    if (persons[i - 1].ts === persons[i].ts) {
      assert.ok(numericId(persons[i - 1].alert_key) > numericId(persons[i].alert_key));
    }
  }

  const sensors = sortSensorAlerts(fixtures.sensorAlerts);
  for (let i = 1; i < sensors.length; i += 1) {
    assert.ok(
      Date.parse(sensors[i - 1].occurred_at) >= Date.parse(sensors[i].occurred_at)
    );
    if (sensors[i - 1].occurred_at === sensors[i].occurred_at) {
      assert.ok(numericId(sensors[i - 1].alert_key) > numericId(sensors[i].alert_key));
    }
  }

  const personTie = sortPersonAlerts([
    { alert_key: 'person:1', ts: '2026-01-01T00:00:00' },
    { alert_key: 'person:2', ts: '2026-01-01T00:00:00' }
  ]);
  assert.deepEqual(personTie.map(function (x) { return x.alert_key; }), ['person:2', 'person:1']);

  const sensorTie = sortSensorAlerts([
    { alert_key: 'sensor:1', occurred_at: '2026-01-01T00:00:00+08:00' },
    { alert_key: 'sensor:2', occurred_at: '2026-01-01T00:00:00+08:00' }
  ]);
  assert.deepEqual(sensorTie.map(function (x) { return x.alert_key; }), ['sensor:2', 'sensor:1']);
});

test('空列表：data=[], total=0, has_more=false', async () => {
  const emptyFixtures = {
    systemStatus: fixtures.systemStatus,
    personAlerts: [],
    sensorAlerts: [],
    readingsById: {},
    allAlerts: function () { return []; }
  };
  const emptyAdapter = createMockAdapter({ latencyMs: 0, fixtures: emptyFixtures });
  const res = await emptyAdapter.request('/api/v1/alerts', { page: 1, page_size: 20 });
  assert.deepEqual(res.body.data, []);
  assert.equal(res.body.meta.total, 0);
  assert.equal(res.body.meta.has_more, false);
});

test('Mock 详情：存在的告警返回 DTO，不存在的返回 404 NOT_FOUND', async () => {
  const person = fixtures.personAlerts[0];
  const found = await adapter.request(
    '/api/v1/alerts/' + encodeURIComponent(person.alert_key),
    {}
  );
  assert.equal(found.statusCode, 200);
  assert.equal(found.body.data.alert_key, person.alert_key);

  const missing = await adapter.request('/api/v1/alerts/person%3A999999', {});
  assert.equal(missing.statusCode, 404);
  const parsed = parseEnvelope(missing.body);
  assert.equal(parsed.status, 'failure');
  assert.equal(parsed.code, 'NOT_FOUND');
});

test('Mock 关联数据：人员无读数；传感器只按 reading_id 关联最多一条', async () => {
  const person = fixtures.personAlerts[0];
  const personRelated = await adapter.request(
    '/api/v1/alerts/' + encodeURIComponent(person.alert_key) + '/related',
    {}
  );
  assert.deepEqual(personRelated.body.data.readings, []);
  assert.deepEqual(personRelated.body.data.media, []);
  assert.equal(personRelated.body.data.media_supported, false);

  const withReading = fixtures.sensorAlerts.find(function (item) {
    return item.reading_id !== null;
  });
  const readingRelated = await adapter.request(
    '/api/v1/alerts/' + encodeURIComponent(withReading.alert_key) + '/related',
    {}
  );
  assert.equal(readingRelated.body.data.readings.length, 1);
  assert.equal(
    readingRelated.body.data.readings[0].reading_id,
    withReading.reading_id
  );

  const withoutReading = fixtures.sensorAlerts.find(function (item) {
    return item.reading_id === null;
  });
  const emptyRelated = await adapter.request(
    '/api/v1/alerts/' + encodeURIComponent(withoutReading.alert_key) + '/related',
    {}
  );
  assert.deepEqual(emptyRelated.body.data.readings, []);
});

test('Mock 系统状态严格使用契约占位结构', async () => {
  const res = await adapter.request('/api/v1/system/status', {});
  assert.deepEqual(res.body.data, { device: { status: 'unknown' }, modules: {} });
  assert.deepEqual(res.body.meta, {});
  assert.equal(validateSystemStatusData(res.body.data).ok, true);
});

test('全部 Mock 数据通过契约校验（L3）', async () => {
  const list = await adapter.request('/api/v1/alerts', { page: 1, page_size: 100 });
  assert.equal(validateAlertListData(list.body.data).ok, true);

  for (let i = 0; i < fixtures.allAlerts().length; i += 1) {
    const alert = fixtures.allAlerts()[i];
    const detail = await adapter.request(
      '/api/v1/alerts/' + encodeURIComponent(alert.alert_key),
      {}
    );
    const prefix = alert.category === 'person' ? 'person:' : 'sensor:';
    assert.equal(
      validateAlertDetailData(detail.body.data, alert.alert_key, prefix).ok,
      true,
      alert.alert_key
    );

    const related = await adapter.request(
      '/api/v1/alerts/' + encodeURIComponent(alert.alert_key) + '/related',
      {}
    );
    assert.equal(
      validateRelatedData(related.body.data, prefix).ok,
      true,
      alert.alert_key
    );
  }
});
