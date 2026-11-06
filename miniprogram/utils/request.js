// 真实 API 模式下的 wx.request 封装。
// 只在 USE_MOCK=false 时由 services/s4-service.js 调用。
// Session Token 从本地存储读取，禁止硬编码、禁止写入 Mock 数据或仓库配置。

const { createAppError, mapNetworkError } = require('./errors');

function buildUrl(baseUrl, path, query) {
  const parts = [];
  const input = query || {};
  Object.keys(input).forEach(function (key) {
    const value = input[key];
    if (value === undefined || value === null) {
      return;
    }
    parts.push(encodeURIComponent(key) + '=' + encodeURIComponent(String(value)));
  });
  const qs = parts.join('&');
  return String(baseUrl).replace(/\/+$/, '') + path + (qs ? '?' + qs : '');
}

function getTokenFromStorage(storageKey) {
  if (typeof wx === 'undefined' || typeof wx.getStorageSync !== 'function') {
    return '';
  }
  try {
    return wx.getStorageSync(storageKey) || '';
  } catch (err) {
    return '';
  }
}

function createRequest(options) {
  const opts = options || {};
  const baseUrl = opts.baseUrl;
  const timeout = opts.timeout;
  const getToken = opts.getToken || function () { return ''; };

  return function request(path, input) {
    return new Promise(function (resolve, reject) {
      const explicit = input && (
        Object.prototype.hasOwnProperty.call(input, 'method') ||
        Object.prototype.hasOwnProperty.call(input, 'query') ||
        Object.prototype.hasOwnProperty.call(input, 'data') ||
        Object.prototype.hasOwnProperty.call(input, 'auth')
      );
      const config = explicit ? input : { query: input || {} };
      const requiresAuth = config.auth !== false;
      let token = '';
      try {
        token = getToken() || '';
      } catch (err) {
        token = '';
      }
      // 真实模式下未配置 Token 时，本地请求前拦截，不发起 wx.request。
      if (requiresAuth && !token) {
        reject(createAppError('TOKEN_MISSING', { detail: '真实模式下未配置用户 Session' }));
        return;
      }
      if (typeof wx === 'undefined' || typeof wx.request !== 'function') {
        reject(createAppError('NETWORK_ERROR', { detail: 'wx.request 不可用' }));
        return;
      }
      const header = Object.assign({}, config.header || {});
      if (requiresAuth) {
        header.Authorization = 'Bearer ' + token;
      }
      if (config.data !== undefined && !header['content-type']) {
        header['content-type'] = 'application/json';
      }
      wx.request({
        url: buildUrl(baseUrl, path, config.query),
        method: String(config.method || 'GET').toUpperCase(),
        timeout: timeout,
        header: header,
        data: config.data,
        success: function (res) {
          resolve({
            statusCode: res.statusCode,
            body: res.data
          });
        },
        fail: function (err) {
          reject(mapNetworkError(err));
        }
      });
    });
  };
}

module.exports = {
  buildUrl,
  getTokenFromStorage,
  createRequest
};
