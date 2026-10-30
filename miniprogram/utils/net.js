// 网络状态订阅。只用于界面提示（离线横幅），不改变请求行为，
// 也不在离线时伪造缓存数据或回退 Mock。

let online = true;
let listeners = [];

function hasWx() {
  return typeof wx !== 'undefined' && wx;
}

function notify() {
  const current = online;
  listeners.slice().forEach(function (fn) {
    try {
      fn(current);
    } catch (err) {
      // 单个订阅者异常不影响其他页面
    }
  });
}

function setOnline(value) {
  const next = value !== false;
  if (next === online) {
    return;
  }
  online = next;
  notify();
}

function init() {
  if (!hasWx()) {
    return;
  }
  try {
    wx.getNetworkType({
      success: function (res) {
        setOnline(res.networkType !== 'none');
      }
    });
    wx.onNetworkStatusChange(function (res) {
      setOnline(res.isConnected);
    });
  } catch (err) {
    // 某些基础库版本不支持时静默降级
  }
}

function isOnline() {
  return online;
}

function subscribe(fn) {
  if (typeof fn !== 'function') {
    return function () {};
  }
  listeners.push(fn);
  fn(online);
  return function unsubscribe() {
    listeners = listeners.filter(function (item) { return item !== fn; });
  };
}

// 仅用于测试
function resetForTest(value) {
  online = value !== false;
  listeners = [];
}

module.exports = {
  init,
  isOnline,
  subscribe,
  resetForTest
};
