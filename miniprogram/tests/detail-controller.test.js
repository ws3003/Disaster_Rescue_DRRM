// L1/L2：告警详情状态机——本地格式拦截、详情/关联独立失败、旧请求保护。
const test = require('node:test');
const assert = require('node:assert/strict');

const { createDetailController } = require('../logic/detail-controller');
const fixtures = require('../mock/fixtures');

function deferred() {
  let resolve;
  let reject;
  const promise = new Promise(function (res, rej) {
    resolve = res;
    reject = rej;
  });
  return { promise: promise, resolve: resolve, reject: reject };
}

function emptyRelated() {
  return { readings: [], media: [], media_supported: false };
}

test('人员告警详情：11 个契约字段，关联读数为空且媒体不支持', async () => {
  const person = fixtures.personAlerts[0];
  const controller = createDetailController({
    fetchDetail: function () {
      return Promise.resolve(person);
    },
    fetchRelated: function () {
      return Promise.resolve(emptyRelated());
    }
  });

  await controller.load(person.alert_key);
  const state = controller.getState();

  assert.equal(state.error, null);
  assert.equal(state.detailView.alertKey, person.alert_key);
  assert.equal(state.detailView.fields.length, 11);
  assert.equal(state.relatedView.hasReadings, false);
  assert.equal(state.relatedView.mediaSupported, false);
  assert.match(state.relatedView.mediaText, /尚未支持/);
});

test('传感器告警详情：14 个契约字段，关联读数 7 个字段', async () => {
  const sensor = fixtures.sensorAlerts.find(function (item) {
    return item.reading_id !== null;
  });
  const reading = fixtures.readingsById[sensor.reading_id];
  const controller = createDetailController({
    fetchDetail: function () {
      return Promise.resolve(sensor);
    },
    fetchRelated: function () {
      return Promise.resolve({
        readings: [reading],
        media: [],
        media_supported: false
      });
    }
  });

  await controller.load(sensor.alert_key);
  const state = controller.getState();

  assert.equal(state.detailView.fields.length, 14);
  assert.equal(state.relatedView.hasReadings, true);
  assert.equal(state.relatedView.readings.length, 1);
  assert.equal(state.relatedView.readings[0].fields.length, 7);
  assert.equal(state.relatedView.readings[0].readingId, reading.reading_id);
});

test('非法 alert_key 本地拦截，不发起详情或关联请求', async () => {
  let detailCalls = 0;
  let relatedCalls = 0;
  const controller = createDetailController({
    fetchDetail: function () {
      detailCalls += 1;
      return Promise.resolve(fixtures.personAlerts[0]);
    },
    fetchRelated: function () {
      relatedCalls += 1;
      return Promise.resolve(emptyRelated());
    }
  });

  await controller.load('bad-key');
  const state = controller.getState();

  assert.equal(detailCalls, 0);
  assert.equal(relatedCalls, 0);
  assert.equal(state.error.kind, 'LOCAL_INVALID_PARAMETER');
  assert.equal(state.initialized, true);
  assert.equal(state.detailView, null);
});

test('详情 404 展示资源不存在，不用本地数据冒充详情', async () => {
  const controller = createDetailController({
    fetchDetail: function () {
      return Promise.reject({ kind: 'NOT_FOUND', message: '资源不存在' });
    },
    fetchRelated: function () {
      return Promise.resolve(emptyRelated());
    }
  });

  await controller.load('person:999999');
  const state = controller.getState();

  assert.equal(state.error.kind, 'NOT_FOUND');
  assert.equal(state.detailView, null);
  assert.equal(state.loading, false);
});

test('关联数据失败不影响详情展示', async () => {
  const person = fixtures.personAlerts[0];
  const controller = createDetailController({
    fetchDetail: function () {
      return Promise.resolve(person);
    },
    fetchRelated: function () {
      return Promise.reject({ kind: 'RESPONSE_INVALID', message: '响应格式异常' });
    }
  });

  await controller.load(person.alert_key);
  const state = controller.getState();

  assert.equal(state.error, null);
  assert.equal(state.detailView.alertKey, person.alert_key);
  assert.equal(state.relatedError.kind, 'RESPONSE_INVALID');
  assert.equal(state.relatedView, null);
});

test('连续加载不同告警时，旧响应不能覆盖新详情', async () => {
  const firstDetail = deferred();
  const firstRelated = deferred();
  const secondDetail = deferred();
  const secondRelated = deferred();

  const controller = createDetailController({
    fetchDetail: function (key) {
      return key === 'person:1' ? firstDetail.promise : secondDetail.promise;
    },
    fetchRelated: function (key) {
      return key === 'person:1' ? firstRelated.promise : secondRelated.promise;
    }
  });

  const firstLoad = controller.load('person:1');
  const secondLoad = controller.load('person:2');

  secondDetail.resolve(fixtures.personAlerts[1]);
  secondRelated.resolve(emptyRelated());
  await secondLoad;

  firstDetail.resolve(fixtures.personAlerts[0]);
  firstRelated.resolve(emptyRelated());
  await firstLoad;

  const state = controller.getState();
  assert.equal(state.alertKey, 'person:2');
  assert.equal(state.detailView.alertKey, fixtures.personAlerts[1].alert_key);
});
