// L3：本地偏好与导航上下文的纯函数行为（不依赖 wx 运行时）。
const test = require('node:test');
const assert = require('node:assert/strict');

const { toggleKey, pushKeyword } = require('../utils/local-store');
const { neighborsOf } = require('../utils/nav-context');
const { filterAlertViews } = require('../utils/filter');
const { buildAlertListViews } = require('../utils/view');
const fixtures = require('../mock/fixtures');

test('关注列表 toggle：加入追加到末尾，再次调用移除', () => {
  let result = toggleKey([], 'person:1100');
  assert.deepEqual(result.list, ['person:1100']);
  assert.equal(result.added, true);

  result = toggleKey(result.list, 'sensor:5000');
  assert.deepEqual(result.list, ['person:1100', 'sensor:5000']);

  result = toggleKey(result.list, 'person:1100');
  assert.deepEqual(result.list, ['sensor:5000']);
  assert.equal(result.added, false);

  assert.deepEqual(toggleKey(['a'], '').list, ['a']);
  assert.deepEqual(toggleKey(null, 'x').list, ['x']);
});

test('搜索历史：去重置顶、忽略空白、限制条数', () => {
  let list = pushKeyword([], '温度');
  assert.deepEqual(list, ['温度']);

  list = pushKeyword(list, '人员');
  assert.deepEqual(list, ['人员', '温度']);

  list = pushKeyword(list, '温度');
  assert.deepEqual(list, ['温度', '人员']);

  assert.deepEqual(pushKeyword(list, '   '), ['温度', '人员']);
  assert.deepEqual(pushKeyword(['a', 'b', 'c'], 'd', 3), ['d', 'a', 'b']);
});

test('详情导航：上下文内返回前后项，越界与缺失时有明确标记', () => {
  const keys = ['person:1', 'person:2', 'person:3'];

  const middle = neighborsOf(keys, 'person:2');
  assert.deepEqual(
    { hasPrev: middle.hasPrev, hasNext: middle.hasNext, prev: middle.prevKey, next: middle.nextKey, pos: middle.position, total: middle.total, inContext: middle.inContext },
    { hasPrev: true, hasNext: true, prev: 'person:1', next: 'person:3', pos: 2, total: 3, inContext: true }
  );

  const first = neighborsOf(keys, 'person:1');
  assert.equal(first.hasPrev, false);
  assert.equal(first.prevKey, '');
  assert.equal(first.hasNext, true);

  const last = neighborsOf(keys, 'person:3');
  assert.equal(last.hasNext, false);
  assert.equal(last.nextKey, '');

  const outside = neighborsOf(keys, 'person:9');
  assert.equal(outside.inContext, false);
  assert.equal(outside.position, 0);
  assert.equal(outside.total, 3);

  const empty = neighborsOf([], 'x');
  assert.equal(empty.inContext, false);
  assert.equal(empty.total, 0);
});

test('本地筛选：仅看关注与关键词、严重程度可叠加', () => {
  const views = buildAlertListViews(fixtures.personAlerts.slice(0, 4));
  const followedKey = views[1].alertKey;

  const onlyFollowed = filterAlertViews(views, '', { followedOnly: true, followedKeys: [followedKey] });
  assert.equal(onlyFollowed.active, true);
  assert.equal(onlyFollowed.matched, 1);
  assert.equal(onlyFollowed.views[0].alertKey, followedKey);

  const followedAndKeyword = filterAlertViews(views, 'zzz', {
    followedOnly: true,
    followedKeys: [followedKey]
  });
  assert.equal(followedAndKeyword.matched, 0);

  const noFollowed = filterAlertViews(views, '', { followedOnly: true, followedKeys: [] });
  assert.equal(noFollowed.matched, 0);
  assert.equal(noFollowed.total, 4);
});
