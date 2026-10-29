// L1/L2：告警列表状态机——分页、类别切换、旧请求保护、失败保留数据。
const test = require('node:test');
const assert = require('node:assert/strict');

const { createAlertsListController } = require('../logic/alerts-list-controller');
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

function meta(page, pageSize, total, hasMore) {
  return { page: page, page_size: pageSize, total: total, has_more: hasMore };
}

function makeQueueController(extraOptions) {
  const queue = [];
  const options = Object.assign(
    {
      fetchAlerts: function (params) {
        const item = deferred();
        item.params = params;
        queue.push(item);
        return item.promise;
      }
    },
    extraOptions || {}
  );
  const controller = createAlertsListController(options);
  return { controller: controller, queue: queue };
}

test('初次加载使用契约默认分页，并以服务端 meta 更新状态', async () => {
  const ctx = makeQueueController();
  const promise = ctx.controller.setCategory('person');
  assert.deepEqual(ctx.queue[0].params, {
    category: 'person',
    page: 1,
    page_size: 20
  });
  assert.equal(ctx.controller.getState().loading, true);

  ctx.queue[0].resolve({
    items: [fixtures.personAlerts[0]],
    meta: meta(1, 20, 23, true)
  });
  await promise;

  const state = ctx.controller.getState();
  assert.equal(state.items.length, 1);
  assert.equal(state.page, 1);
  assert.equal(state.total, 23);
  assert.equal(state.hasMore, true);
  assert.equal(state.loading, false);
  assert.equal(state.error, null);
  assert.equal(state.initialized, true);
});

test('切换类别立即重置分页，旧请求不能覆盖新类别结果', async () => {
  const ctx = makeQueueController();
  const first = ctx.controller.setCategory('person');
  const second = ctx.controller.setCategory('sensor_environment');

  assert.equal(ctx.queue.length, 2);
  assert.equal(ctx.queue[0].params.category, 'person');
  assert.equal(ctx.queue[1].params.category, 'sensor_environment');
  assert.equal(ctx.queue[1].params.page, 1);

  ctx.queue[0].resolve({
    items: [fixtures.personAlerts[0]],
    meta: meta(1, 20, 23, true)
  });
  await first;

  let state = ctx.controller.getState();
  assert.deepEqual(state.items, [], '旧请求结果必须被忽略');
  assert.equal(state.category, 'sensor_environment');
  assert.equal(state.loading, true);

  ctx.queue[1].resolve({
    items: [fixtures.sensorAlerts[0]],
    meta: meta(1, 20, 17, false)
  });
  await second;

  state = ctx.controller.getState();
  assert.equal(state.items.length, 1);
  assert.equal(state.items[0].category, 'sensor_environment');
  assert.equal(state.hasMore, false);
});

test('同一类别加载中重复触发不重复请求', async () => {
  const ctx = makeQueueController();
  const first = ctx.controller.setCategory('person');
  const second = ctx.controller.setCategory('person');

  assert.equal(ctx.queue.length, 1, '同一类别加载中不得重复请求');

  ctx.queue[0].resolve({
    items: [fixtures.personAlerts[0]],
    meta: meta(1, 20, 23, true)
  });
  await Promise.all([first, second]);

  const state = ctx.controller.getState();
  assert.equal(state.items.length, 1);
  assert.equal(state.loading, false);
});

test('加载更多进行中不重复发起或重复追加', async () => {
  const ctx = makeQueueController();
  const initial = ctx.controller.setCategory('');
  ctx.queue[0].resolve({
    items: [fixtures.personAlerts[0]],
    meta: meta(1, 20, 40, true)
  });
  await initial;

  const more1 = ctx.controller.loadMore();
  const more2 = ctx.controller.loadMore();
  assert.equal(ctx.queue.length, 2, '只允许一次加载更多请求');

  ctx.queue[1].resolve({
    items: [fixtures.personAlerts[1]],
    meta: meta(2, 20, 40, false)
  });
  await Promise.all([more1, more2]);

  const state = ctx.controller.getState();
  assert.equal(state.items.length, 2);
  assert.equal(state.page, 2);
  assert.equal(state.hasMore, false);

  await ctx.controller.loadMore();
  assert.equal(ctx.queue.length, 2, 'has_more=false 时不再请求');
});

test('加载失败保留已加载列表，重试续接同一页', async () => {
  const ctx = makeQueueController();
  const initial = ctx.controller.setCategory('');
  ctx.queue[0].resolve({
    items: [fixtures.personAlerts[0]],
    meta: meta(1, 20, 40, true)
  });
  await initial;

  const more = ctx.controller.loadMore();
  ctx.queue[1].reject({ kind: 'INTERNAL_ERROR', message: '服务端异常' });
  await more;

  let state = ctx.controller.getState();
  assert.equal(state.items.length, 1, '失败不得清空已有数据');
  assert.equal(state.error.kind, 'INTERNAL_ERROR');
  assert.equal(state.lastFailedPage, 2);
  assert.equal(state.hasMore, true);

  const retry = ctx.controller.retry();
  assert.deepEqual(ctx.queue[2].params, {
    category: undefined,
    page: 2,
    page_size: 20
  });
  ctx.queue[2].resolve({
    items: [fixtures.personAlerts[1]],
    meta: meta(2, 20, 40, false)
  });
  await retry;

  state = ctx.controller.getState();
  assert.equal(state.items.length, 2);
  assert.equal(state.error, null);
  assert.equal(state.lastFailedPage, null);
});

test('下拉刷新失败时保留旧数据', async () => {
  const ctx = makeQueueController();
  const initial = ctx.controller.setCategory('person');
  ctx.queue[0].resolve({
    items: [fixtures.personAlerts[0]],
    meta: meta(1, 20, 23, true)
  });
  await initial;

  const refresh = ctx.controller.refresh();
  ctx.queue[1].reject({ kind: 'NETWORK_ERROR', message: '网络异常' });
  await refresh;

  const state = ctx.controller.getState();
  assert.equal(state.items.length, 1);
  assert.equal(state.error.kind, 'NETWORK_ERROR');
});

test('空列表状态与服务端 has_more 语义', async () => {
  const ctx = makeQueueController();
  const initial = ctx.controller.setCategory('sensor_environment');
  ctx.queue[0].resolve({ items: [], meta: meta(1, 20, 0, false) });
  await initial;

  const state = ctx.controller.getState();
  assert.equal(state.items.length, 0);
  assert.equal(state.total, 0);
  assert.equal(state.hasMore, false);
  assert.equal(state.isEmpty, true);
});

test('本地非法 category 不发起请求并展示参数错误', async () => {
  const ctx = makeQueueController();
  await ctx.controller.setCategory('all');
  const state = ctx.controller.getState();
  assert.equal(ctx.queue.length, 0);
  assert.equal(state.error.kind, 'LOCAL_INVALID_PARAMETER');
  assert.equal(state.category, '');
  assert.deepEqual(state.items, []);
});
