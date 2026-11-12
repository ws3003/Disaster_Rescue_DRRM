// L1：首页状态机——S4-01 状态 + 两组 S4-02 最近告警 + meta.total 摘要。
const test = require('node:test');
const assert = require('node:assert/strict');

const { createHomeController } = require('../logic/home-controller');
const fixtures = require('../mock/fixtures');

function listResult(category, total) {
  const items = category === 'person'
    ? fixtures.personAlerts.slice(0, 3)
    : fixtures.sensorAlerts.slice(0, 3);
  return {
    items: items,
    meta: { page: 1, page_size: 3, total: total, has_more: total > 3 }
  };
}

test('首页并行加载：状态 + 两组最近告警，摘要来自 meta.total', async () => {
  const alertCalls = [];
  const controller = createHomeController({
    mockMode: true,
    fetchStatus: function () {
      return Promise.resolve(fixtures.systemStatus);
    },
    fetchAlerts: function (params) {
      alertCalls.push(params);
      return Promise.resolve(
        listResult(params.category, params.category === 'person' ? 23 : 17)
      );
    }
  });

  await controller.load();
  const state = controller.getState();

  assert.equal(alertCalls.length, 2);
  assert.deepEqual(alertCalls[0], {
    category: 'person',
    page: 1,
    page_size: 3
  });
  assert.deepEqual(alertCalls[1], {
    category: 'sensor_environment',
    page: 1,
    page_size: 3
  });

  assert.equal(state.status.view.deviceStatus, 'unknown');
  assert.equal(state.status.view.modulesEmpty, true);
  assert.equal(state.person.items.length, 3);
  assert.equal(state.sensor.items.length, 3);
  assert.equal(state.summary.personText, '23');
  assert.equal(state.summary.sensorText, '17');
  assert.equal(state.loading, false);
});

test('状态接口失败不影响两组告警与摘要', async () => {
  const controller = createHomeController({
    fetchStatus: function () {
      return Promise.reject({ kind: 'INTERNAL_ERROR', message: '服务端异常' });
    },
    fetchAlerts: function (params) {
      return Promise.resolve(
        listResult(params.category, params.category === 'person' ? 23 : 17)
      );
    }
  });

  await controller.load();
  const state = controller.getState();

  assert.equal(state.status.error.kind, 'INTERNAL_ERROR');
  assert.equal(state.status.view, null);
  assert.equal(state.person.items.length, 3);
  assert.equal(state.sensor.items.length, 3);
  assert.equal(state.summary.personText, '23');
  assert.equal(state.summary.sensorText, '17');
});

test('单个告警组失败时摘要对应项显示为 —，不虚构计数', async () => {
  const controller = createHomeController({
    fetchStatus: function () {
      return Promise.resolve(fixtures.systemStatus);
    },
    fetchAlerts: function (params) {
      if (params.category === 'person') {
        return Promise.reject({ kind: 'NETWORK_ERROR', message: '网络异常' });
      }
      return Promise.resolve(listResult(params.category, 17));
    }
  });

  await controller.load();
  const state = controller.getState();

  assert.equal(state.person.error.kind, 'NETWORK_ERROR');
  assert.equal(state.person.items.length, 0);
  assert.equal(state.summary.personText, '—');
  assert.equal(state.summary.sensorText, '17');
});

test('加载进行中重复触发不会重复请求', async () => {
  let statusCalls = 0;
  let alertCalls = 0;
  const controller = createHomeController({
    fetchStatus: function () {
      statusCalls += 1;
      return Promise.resolve(fixtures.systemStatus);
    },
    fetchAlerts: function () {
      alertCalls += 1;
      return Promise.resolve(listResult('person', 1));
    }
  });

  const first = controller.load();
  const second = controller.load();
  await Promise.all([first, second]);

  assert.equal(statusCalls, 1);
  assert.equal(alertCalls, 2, '两组告警各请求一次');
});

test('重试状态接口只重发状态请求', async () => {
  let statusCalls = 0;
  let alertCalls = 0;
  const controller = createHomeController({
    fetchStatus: function () {
      statusCalls += 1;
      if (statusCalls === 1) {
        return Promise.reject({ kind: 'INTERNAL_ERROR', message: '服务端异常' });
      }
      return Promise.resolve(fixtures.systemStatus);
    },
    fetchAlerts: function (params) {
      alertCalls += 1;
      return Promise.resolve(
        listResult(params.category, params.category === 'person' ? 1 : 1)
      );
    }
  });

  await controller.load();
  assert.equal(statusCalls, 1);
  assert.equal(alertCalls, 2);

  await controller.retryStatus();
  assert.equal(statusCalls, 2);
  assert.equal(alertCalls, 2, '重试状态不应重发告警请求');
  assert.equal(controller.getState().status.error, null);
});
