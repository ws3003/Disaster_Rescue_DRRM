// L1：本地输入校验。必须在请求前拦截无效参数，不得静默修正。
const test = require('node:test');
const assert = require('node:assert/strict');

const {
  validatePagination,
  validateCategory,
  validateAlertKey
} = require('../utils/validators');

test('分页参数省略时使用契约默认值 page=1, page_size=20', () => {
  const cases = [undefined, {}, { page: null, page_size: undefined }, { page: '', page_size: '' }];
  cases.forEach(function (input) {
    const result = validatePagination(input);
    assert.equal(result.ok, true);
    assert.deepEqual(result.value, { page: 1, page_size: 20 });
  });
});

test('显式非法 page 被拦截且不静默修正', () => {
  [0, -1, 1.5, '2', NaN, true].forEach(function (page) {
    const result = validatePagination({ page: page });
    assert.equal(result.ok, false, 'page=' + String(page));
    assert.equal(result.error.kind, 'LOCAL_INVALID_PARAMETER');
    assert.equal(result.error.fieldErrors.length, 1);
  });
});

test('page_size 边界与取值范围', () => {
  assert.equal(validatePagination({ page_size: 1 }).ok, true);
  assert.equal(validatePagination({ page_size: 100 }).ok, true);
  [0, 101, -5, 2.5, '20', NaN].forEach(function (size) {
    const result = validatePagination({ page_size: size });
    assert.equal(result.ok, false, 'page_size=' + String(size));
    assert.equal(result.error.kind, 'LOCAL_INVALID_PARAMETER');
  });
});

test('category 仅允许契约定义的两个值', () => {
  assert.deepEqual(validateCategory(undefined), { ok: true, value: undefined });
  assert.deepEqual(validateCategory(''), { ok: true, value: undefined });
  assert.equal(validateCategory('person').value, 'person');
  assert.equal(validateCategory('sensor_environment').value, 'sensor_environment');
  ['all', 'Person', 'sensor', 0].forEach(function (value) {
    const result = validateCategory(value);
    assert.equal(result.ok, false);
    assert.equal(result.error.kind, 'LOCAL_INVALID_PARAMETER');
  });
});

test('alert_key 只校验契约声明的两种前缀格式', () => {
  assert.equal(validateAlertKey('person:1001').ok, true);
  assert.equal(validateAlertKey('sensor:abc-1').ok, true);
  assert.equal(validateAlertKey('person:1001').prefix, 'person:');
  assert.equal(validateAlertKey('sensor:9').prefix, 'sensor:');

  [undefined, null, '', 'person:', 'sensor:', 'person: 1', 'x:1', 123].forEach(function (key) {
    const result = validateAlertKey(key);
    assert.equal(result.ok, false, 'key=' + String(key));
    assert.equal(result.error.kind, 'LOCAL_INVALID_PARAMETER');
  });
});
