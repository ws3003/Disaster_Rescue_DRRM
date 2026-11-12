// L3：静态契约验证。直接读取 schemas/s4_contract.json（只读），
// 逐项核对客户端常量、DTO 定义与 Mock 示例，不修改契约。
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');

const contractPath = path.join(__dirname, '..', '..', 'schemas', 's4_contract.json');
const contract = JSON.parse(fs.readFileSync(contractPath, 'utf8'));

const schema = require('../utils/dto-schema');
const { ENVELOPE_FIELDS } = require('../utils/envelope');
const { CONTRACT_ERRORS } = require('../utils/errors');
const fixtures = require('../mock/fixtures');

function endpointsByReq() {
  const map = {};
  contract.endpoints.forEach(function (endpoint) {
    map[endpoint.req_number] = endpoint;
  });
  return map;
}

function fieldNames(fields) {
  return fields.map(function (field) { return field.name; }).sort();
}

function assertSchemaMatches(contractFields, ourFields, label) {
  assert.equal(ourFields.length, contractFields.length, label + ' 字段数量');
  contractFields.forEach(function (cf, index) {
    const of = ourFields[index];
    assert.equal(of.name, cf.name, label + ' 字段名顺序');
    assert.equal(of.type, cf.type, label + '.' + cf.name + ' 类型');
    assert.equal(of.nullable, cf.nullable, label + '.' + cf.name + ' 可空性');
    if (cf.allowed) {
      assert.deepEqual(of.allowed, cf.allowed, label + '.' + cf.name + ' 枚举');
    }
    const constMatch = /固定值\s+(\w+)/.exec(cf.source || '');
    if (constMatch) {
      assert.equal(of.constValue, constMatch[1], label + '.' + cf.name + ' 固定值');
    }
  });
}

test('契约版本与范围', () => {
  assert.equal(contract.contract_version, schema.CONTRACT_VERSION);
  assert.equal(contract.contract_version, '0.1.0');
  assert.equal(contract.scope.no_s4_05, true);
  assert.equal(contract.scope.read_only, true);
  assert.equal(
    contract.endpoints.some(function (e) { return e.req_number === 'S4-05'; }),
    false
  );
});

test('五个接口路径与客户端常量一致', () => {
  const byReq = endpointsByReq();
  assert.equal(byReq['S4-01'].path, schema.ENDPOINTS.systemStatus);
  assert.equal(byReq['S4-02'].path, schema.ENDPOINTS.alerts);
  assert.equal(
    decodeURIComponent(schema.ENDPOINTS.alertDetail('{alert_key}')),
    byReq['S4-03'].path
  );
  assert.equal(
    decodeURIComponent(schema.ENDPOINTS.alertRelated('{alert_key}')),
    byReq['S4-04'].path
  );
  assert.equal(byReq['S4-06'].path, schema.ENDPOINTS.environmentReadings);

  contract.endpoints.forEach(function (endpoint) {
    assert.equal(endpoint.method, 'GET');
  });
});

test('告警键格式与客户端前缀一致', () => {
  assert.equal(contract.alert_identifiers.person_alert_key, 'person:<event_id>');
  assert.equal(contract.alert_identifiers.sensor_alert_key, 'sensor:<alert_id>');
  assert.equal(schema.DTO.personAlert[0].prefix, 'person:');
  assert.equal(schema.DTO.sensorAlert[0].prefix, 'sensor:');
});

test('分页默认值、上限与 meta 字段一致', () => {
  assert.equal(schema.PAGINATION.pageDefault, contract.pagination.page_default);
  assert.equal(schema.PAGINATION.pageSizeDefault, contract.pagination.page_size_default);
  assert.equal(schema.PAGINATION.pageSizeMax, contract.pagination.page_size_max);
  assert.deepEqual(schema.PAGINATION.metaFields, contract.pagination.meta_fields);
  assert.equal(contract.pagination.no_global_cross_group_time_sort, true);
});

test('category 枚举值一致', () => {
  const listEndpoint = endpointsByReq()['S4-02'];
  const categoryParam = listEndpoint.params.find(function (param) {
    return param.name === 'category';
  });
  assert.deepEqual(schema.CATEGORIES, categoryParam.values);
});

test('错误码与 HTTP 状态码映射一致', () => {
  const contractCodes = Object.keys(contract.errors).sort();
  const ourCodes = Object.keys(CONTRACT_ERRORS).sort();
  assert.deepEqual(ourCodes, contractCodes);
  contractCodes.forEach(function (code) {
    assert.equal(
      CONTRACT_ERRORS[code].httpStatus,
      contract.errors[code].http_status,
      code
    );
  });
});

test('响应信封字段一致且 Bearer 鉴权约定一致', () => {
  assert.deepEqual(ENVELOPE_FIELDS, contract.response_envelope.fields);
  assert.deepEqual(contract.response_envelope.success, { code: 'OK', message: 'ok' });
  assert.equal(contract.response_envelope.error.data, null);
  assert.deepEqual(contract.response_envelope.error.meta, {});
  assert.equal(contract.authentication.scheme, 'Bearer');
  assert.equal(
    contract.authentication.request_header,
    'Authorization: Bearer <token>'
  );
});

test('人员告警 DTO：11 个字段，与契约名称/类型/可空性一致', () => {
  const contractFields = contract.dto_rules.person_alert.fields;
  assert.equal(contractFields.length, 11);
  assertSchemaMatches(contractFields, schema.DTO.personAlert, 'person_alert');

  const tsField = schema.DTO.personAlert.find(function (field) {
    return field.name === 'ts';
  });
  assert.equal(tsField.format, 'naive-iso');
  const contractTs = contractFields.find(function (field) {
    return field.name === 'ts';
  });
  assert.match(contractTs.source, /naive ISO/);
});

test('传感器告警 DTO：14 个字段，status 枚举与带时区时间一致', () => {
  const contractFields = contract.dto_rules.sensor_alert.fields;
  assert.equal(contractFields.length, 14);
  assertSchemaMatches(contractFields, schema.DTO.sensorAlert, 'sensor_alert');

  const statusField = schema.DTO.sensorAlert.find(function (field) {
    return field.name === 'status';
  });
  assert.deepEqual(statusField.allowed, contract.dto_rules.sensor_alert.fields
    .find(function (field) { return field.name === 'status'; }).allowed);

  const occurredField = schema.DTO.sensorAlert.find(function (field) {
    return field.name === 'occurred_at';
  });
  assert.equal(occurredField.format, 'zoned-iso');
  const contractOccurred = contractFields.find(function (field) {
    return field.name === 'occurred_at';
  });
  assert.match(contractOccurred.source, /带时区/);
});

test('环境读数 DTO：7 个字段，device_captured_at 为带时区时间', () => {
  const contractFields = contract.dto_rules.environment_reading.fields;
  assert.equal(contractFields.length, 7);
  assertSchemaMatches(contractFields, schema.DTO.environmentReading, 'environment_reading');

  const capturedField = schema.DTO.environmentReading.find(function (field) {
    return field.name === 'device_captured_at';
  });
  assert.equal(capturedField.format, 'zoned-iso');
  const contractCaptured = contractFields.find(function (field) {
    return field.name === 'device_captured_at';
  });
  assert.match(contractCaptured.source, /带时区/);
});

test('Mock 示例字段集合与契约完全一致（无多余、无缺失）', () => {
  const personFields = fieldNames(contract.dto_rules.person_alert.fields);
  const sensorFields = fieldNames(contract.dto_rules.sensor_alert.fields);
  const readingFields = fieldNames(contract.dto_rules.environment_reading.fields);

  fixtures.personAlerts.forEach(function (dto) {
    assert.deepEqual(Object.keys(dto).sort(), personFields, dto.alert_key);
  });
  fixtures.sensorAlerts.forEach(function (dto) {
    assert.deepEqual(Object.keys(dto).sort(), sensorFields, dto.alert_key);
  });
  Object.keys(fixtures.readingsById).forEach(function (key) {
    assert.deepEqual(
      Object.keys(fixtures.readingsById[key]).sort(),
      readingFields,
      key
    );
  });
});

test('S4-04 关联数据规则与客户端校验一致', () => {
  const related = endpointsByReq()['S4-04'];
  assert.match(related.relation_rule, /最多一条/);
  assert.match(related.relation_rule, /sensor_alerts\.reading_id/);
  assert.match(related.relation_rule, /人员告警不关联环境读数/);
  assert.equal(contract.implementation_constraints.indexOf('media 数组目前仅为占位，不伪造媒体 URL。') !== -1, true);
});

test('契约声明的实现状态仍为未实现（避免 Mock 被误认为真实后端）', () => {
  const byReq = endpointsByReq();
  assert.equal(byReq['S4-01'].implementation_status, 'placeholder_not_implemented');
  ['S4-02', 'S4-03', 'S4-04', 'S4-06'].forEach(function (req) {
    assert.equal(byReq[req].implementation_status, 'not_implemented');
  });
  assert.equal(contract.authentication.implementation_status, 'not_implemented');
  assert.equal(contract.dao_capabilities.implemented_status, 'not_implemented');
});
