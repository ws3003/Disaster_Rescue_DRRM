// 客户端本地状态：关注列表与搜索历史。
// 这些都是“本机偏好”，不进入契约、不请求服务端、也不代表告警在服务端的真实状态。
// 纯函数部分单独导出，便于在 Node 中测试。

const FOLLOW_KEY = 's4_followed_keys';
const SEARCH_KEY = 's4_search_history';
const SEARCH_LIMIT = 6;

function hasStorage() {
  return typeof wx !== 'undefined' && wx && typeof wx.getStorageSync === 'function';
}

function readList(key) {
  if (!hasStorage()) {
    return [];
  }
  try {
    const value = wx.getStorageSync(key);
    return Array.isArray(value) ? value.slice() : [];
  } catch (err) {
    return [];
  }
}

function writeList(key, list) {
  if (!hasStorage()) {
    return;
  }
  try {
    wx.setStorageSync(key, list);
  } catch (err) {
    // 本地存储失败不影响主流程
  }
}

// 纯函数：在列表中加入/移除某个 key（保持顺序，新项追加到末尾）
function toggleKey(list, key) {
  const source = Array.isArray(list) ? list.slice() : [];
  if (!key) {
    return { list: source, added: false };
  }
  const index = source.indexOf(key);
  if (index === -1) {
    source.push(key);
    return { list: source, added: true };
  }
  source.splice(index, 1);
  return { list: source, added: false };
}

// 纯函数：关键词入历史（去重、最新在前、限制条数）
function pushKeyword(list, keyword, limit) {
  const source = Array.isArray(list) ? list.slice() : [];
  const value = String(keyword === null || keyword === undefined ? '' : keyword).trim();
  const max = limit || SEARCH_LIMIT;
  if (!value) {
    return source;
  }
  const next = source.filter(function (item) { return item !== value; });
  next.unshift(value);
  return next.slice(0, max);
}

function getFollowed() {
  return readList(FOLLOW_KEY);
}

function isFollowed(alertKey) {
  return getFollowed().indexOf(alertKey) !== -1;
}

function toggleFollow(alertKey) {
  const result = toggleKey(getFollowed(), alertKey);
  writeList(FOLLOW_KEY, result.list);
  return { followed: result.added, list: result.list };
}

function getKeywords() {
  return readList(SEARCH_KEY);
}

function addKeyword(keyword) {
  const next = pushKeyword(getKeywords(), keyword, SEARCH_LIMIT);
  writeList(SEARCH_KEY, next);
  return next;
}

function clearKeywords() {
  writeList(SEARCH_KEY, []);
  return [];
}

module.exports = {
  FOLLOW_KEY,
  SEARCH_KEY,
  SEARCH_LIMIT,
  toggleKey,
  pushKeyword,
  getFollowed,
  isFollowed,
  toggleFollow,
  getKeywords,
  addKeyword,
  clearKeywords
};
