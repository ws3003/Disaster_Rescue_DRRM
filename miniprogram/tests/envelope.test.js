// L2/L3：信封与 DTO 校验，逐项对照共享契约 0.1.0。
const test = require('node:test');
const assert = require('node:assert/strict');

const fixtures = require('../mock/fixtures');
const {
  parseEnvelope,
  validateSystemStatusData,
  validateListMeta,
  validateEmptyMeta,
  validatePersonAlert,
  validateSensorAlert,
  validateEnvironmentReading,
  validateAlertListData,
  validateAlertDetailData,
  validateRelatedData
} = require('../utils/envelope');

function clone(value) {
  return JSON.parse(JSON.stringify(value));
}

function issuesOf(validator, dto) {
  const issues = [];
  validator(dto, 'dto', issues);
  return issues;
}

test('成功信封结构校验', () => {
  const parsed = parseEnvelope({ code: 'OK', message: 'ok', data: [], meta: {} });
  assert.equal(parsed.ok, true);
  assert.equal(parsed.status, 'success');
});

test('失败信封结构校验', () => {
  const parsed = parseEnvelope({
    code: 'INVALID_PARAMETER',
    message: 'bad request',
    data: null,
    meta: {}
  });
  assert.equal(parsed.ok, true);
  assert.equal(parsed.status, 'failure');
});

test('信封结构异常不得当作成功', () => {
  const badCases = [
    null,
    [],
    { message: 'ok', data: [], meta: {} },
    { code: 'OK', data: [], meta: {} },
    { code: 'OK', message: 'ok', data: [], meta: [] },
    { code: 'OK', message: 'OK', data: [], meta: {} },
    { code: 'OK', message: 'ok', data: null, meta: {} },
    { code: 'NOT_FOUND', message: 'x', data: {}, meta: {} },
    { code: 'NOT_FOUND', message: 'x', data: null, meta: { page: 1 } }
  ];
  badCases.forEach(function (body, index) {
    const parsed = parseEnvelope(body);
    assert.equal(parsed.ok, false, 'case ' + index);
    assert.equal(parsed.error.kind, 'RESPONSE_INVALID');
  });
});

test('S4-01 data 结构校验（占位值允许，但字段类型必须符合契约）', () => {
  assert.equal(validateSystemStatusData(fixtures.systemStatus).ok, true);

  assert.equal(validateSystemStatusData({ modules: {} }).ok, false);
  assert.equal(validateSystemStatusData({ device: {}, modules: {} }).ok, false);
  assert.equal(validateSystemStatusData({ device: { status: '' }, modules: {} }).ok, false);
  assert.equal(validateSystemStatusData({ device: { status: 'unknown' }, modules: [] }).ok, false);
});

test('S4-02 meta 字段类型校验', () => {
  assert.equal(
    validateListMeta({ page: 1, page_size: 20, total: 0, has_more: false }).ok,
    true
  );
  assert.equal(
    validateListMeta({ page: 1, page_size: 20, total: 0, has_more: 'false' }).ok,
    false
  );
  assert.equal(
    validateListMeta({ page: 0, page_size: 20, total: 0, has_more: false }).ok,
    false
  );
  assert.equal(
    validateListMeta({ page: 1, page_size: 101, total: 0, has_more: false }).ok,
    false
  );
  assert.equal(
    validateListMeta({ page: 1, page_size: 20, has_more: false }).ok,
    false
  );
});

test('S4-01/03/04 成功响应 meta 必须为空对象', () => {
  assert.equal(validateEmptyMeta({}).ok, true);
  assert.equal(validateEmptyMeta({ page: 1 }).ok, false);
  assert.equal(validateEmptyMeta([]).ok, false);
});

test('人员告警 DTO：必填、可空、时间格式', () => {
  fixtures.personAlerts.forEach(function (dto) {
    assert.deepEqual(issuesOf(validatePersonAlert, dto), [], dto.alert_key);
  });

  const zonedTs = clone(fixtures.personAlerts[0]);
  zonedTs.ts = '2026-10-27T10:42:15+08:00';
  assert.ok(issuesOf(validatePersonAlert, zonedTs).length > 0);

  const badScore = clone(fixtures.personAlerts[0]);
  badScore.risk_score = '91.4';
  assert.ok(issuesOf(validatePersonAlert, badScore).length > 0);

  const missingNullable = clone(fixtures.personAlerts[0]);
  delete missingNullable.title;
  assert.ok(issuesOf(validatePersonAlert, missingNullable).length > 0);

  const wrongPrefix = clone(fixtures.personAlerts[0]);
  wrongPrefix.alert_key = 'sensor:1';
  assert.ok(issuesOf(validatePersonAlert, wrongPrefix).length > 0);

  const badPersonId = clone(fixtures.personAlerts[0]);
  badPersonId.person_id = 1.5;
  assert.ok(issuesOf(validatePersonAlert, badPersonId).length > 0);
});

test('传感器告警 DTO：status 枚举与带时区时间', () => {
  fixtures.sensorAlerts.forEach(function (dto) {
    assert.deepEqual(issuesOf(validateSensorAlert, dto), [], dto.alert_key);
  });

  const badStatus = clone(fixtures.sensorAlerts[0]);
  badStatus.status = 'closed';
  assert.ok(issuesOf(validateSensorAlert, badStatus).length > 0);

  const naive = clone(fixtures.sensorAlerts[0]);
  naive.occurred_at = '2026-10-27T10:40:00';
  assert.ok(issuesOf(validateSensorAlert, naive).length > 0);

  const missingOptional = clone(fixtures.sensorAlerts[0]);
  delete missingOptional.received_at;
  assert.ok(issuesOf(validateSensorAlert, missingOptional).length > 0);
});

test('环境读数 DTO：temperature 必填、湿度可空、时间格式', () => {
  const readings = Object.keys(fixtures.readingsById).map(function (key) {
    return fixtures.readingsById[key];
  });
  assert.ok(readings.length > 0);
  readings.forEach(function (dto) {
    assert.deepEqual(issuesOf(validateEnvironmentReading, dto), [], dto.reading_id);
  });

  const nullTemp = clone(readings[0]);
  nullTemp.temperature = null;
  assert.ok(issuesOf(validateEnvironmentReading, nullTemp).length > 0);

  const nullHumidity = clone(readings[0]);
  nullHumidity.humidity = null;
  assert.deepEqual(issuesOf(validateEnvironmentReading, nullHumidity), []);

  const naiveCaptured = clone(readings[0]);
  naiveCaptured.device_captured_at = '2026-10-27T10:39:58';
  assert.ok(issuesOf(validateEnvironmentReading, naiveCaptured).length > 0);
});

test('列表 data 必须按 category 匹配请求筛选', () => {
  const mixed = [fixtures.personAlerts[0], fixtures.sensorAlerts[0]];
  assert.equal(validateAlertListData(mixed).ok, true);
  assert.equal(validateAlertListData(mixed, 'person').ok, false);
  assert.equal(validateAlertListData([fixtures.personAlerts[0]], 'sensor_environment').ok, false);
  assert.equal(validateAlertListData({}, undefined).ok, false);
});

test('详情 data 的 alert_key 必须与请求一致，DTO 类型必须匹配前缀', () => {
  const person = fixtures.personAlerts[0];
  assert.equal(validateAlertDetailData(person, person.alert_key, 'person:').ok, true);
  assert.equal(validateAlertDetailData(person, 'person:999999', 'person:').ok, false);

  const sensorAsPerson = fixtures.sensorAlerts[0];
  assert.equal(
    validateAlertDetailData(sensorAsPerson, sensorAsPerson.alert_key, 'person:').ok,
    false
  );
});

test('S4-04 关联数据规则', () => {
  assert.equal(
    validateRelatedData({ readings: [], media: [], media_supported: false }, 'person:').ok,
    true
  );
  assert.equal(
    validateRelatedData({ readings: [], media: [], media_supported: false }, 'sensor:').ok,
    true
  );

  const reading = Object.keys(fixtures.readingsById).map(function (key) {
    return fixtures.readingsById[key];
  })[0];

  assert.equal(
    validateRelatedData({ readings: [reading], media: [], media_supported: false }, 'sensor:').ok,
    true
  );
  assert.equal(
    validateRelatedData({ readings: [reading], media: [], media_supported: false }, 'person:').ok,
    false
  );
  assert.equal(
    validateRelatedData(
      { readings: [reading, reading], media: [], media_supported: false },
      'sensor:'
    ).ok,
    false
  );
  assert.equal(
    validateRelatedData({ readings: [], media: [{}], media_supported: false }, 'sensor:').ok,
    false
  );
  assert.equal(
    validateRelatedData({ readings: [], media: [], media_supported: 'false' }, 'sensor:').ok,
    false
  );
});
