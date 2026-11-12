// 详情页“上一条/下一条”的上下文。
// 列表页在跳转前把“当前已展示（含本地筛选后）”的 alert_key 顺序写入内存，
// 详情页只在这个已加载集合内前后移动，不额外请求、也不假设未加载数据的存在。

let contextKeys = [];
let contextLabel = '';

// 纯函数：计算相邻项，越界时返回 false 与空 key
function neighborsOf(keys, alertKey) {
  const list = Array.isArray(keys) ? keys : [];
  const index = list.indexOf(alertKey);
  if (index === -1) {
    return {
      inContext: false,
      position: 0,
      total: list.length,
      hasPrev: false,
      hasNext: false,
      prevKey: '',
      nextKey: ''
    };
  }
  return {
    inContext: true,
    position: index + 1,
    total: list.length,
    hasPrev: index > 0,
    hasNext: index < list.length - 1,
    prevKey: index > 0 ? list[index - 1] : '',
    nextKey: index < list.length - 1 ? list[index + 1] : ''
  };
}

function setContext(keys, label) {
  contextKeys = Array.isArray(keys) ? keys.slice() : [];
  contextLabel = label || '';
}

function clearContext() {
  contextKeys = [];
  contextLabel = '';
}

function getNeighbors(alertKey) {
  const result = neighborsOf(contextKeys, alertKey);
  result.label = contextLabel;
  return result;
}

module.exports = {
  neighborsOf,
  setContext,
  clearContext,
  getNeighbors
};
