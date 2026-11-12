// 微信登录 code 仅发送给自有后端换取用户 Session。
// AppSecret、S4_API_TOKEN、C4_TOKEN 与 OSS 密钥不得出现在小程序端。

const { AUTH_ENDPOINTS } = require('../utils/dto-schema');
const { parseEnvelope, validateEmptyMeta } = require('../utils/envelope');
const { createAppError, mapHttpError } = require('../utils/errors');

const EXPIRY_SKEW_MS = 60 * 1000;

function createAuthService(options) {
  const opts = options || {};
  const request = opts.request;
  const storageKey = opts.storageKey;
  const now = opts.now || function () { return Date.now(); };
  let loginPromise = null;

  function readSession() {
    if (typeof wx === 'undefined' || typeof wx.getStorageSync !== 'function') return null;
    try {
      const value = wx.getStorageSync(storageKey);
      if (!value || typeof value !== 'object') return null;
      if (typeof value.token !== 'string' || !value.token) return null;
      if (!Number.isFinite(value.expiresAt)) return null;
      return value;
    } catch (err) {
      return null;
    }
  }

  function clearSession() {
    if (typeof wx === 'undefined' || typeof wx.removeStorageSync !== 'function') return;
    try { wx.removeStorageSync(storageKey); } catch (err) {}
  }

  function writeSession(data) {
    if (!data || typeof data.session_token !== 'string' || !data.session_token) {
      throw createAppError('RESPONSE_INVALID', { detail: 'login.data.session_token 无效' });
    }
    if (!Number.isInteger(data.expires_in) || data.expires_in <= 0) {
      throw createAppError('RESPONSE_INVALID', { detail: 'login.data.expires_in 无效' });
    }
    if (typeof data.user_id !== 'string' || !data.user_id) {
      throw createAppError('RESPONSE_INVALID', { detail: 'login.data.user_id 无效' });
    }
    const session = {
      token: data.session_token,
      userId: data.user_id,
      expiresAt: now() + data.expires_in * 1000
    };
    if (typeof wx === 'undefined' || typeof wx.setStorageSync !== 'function') {
      throw createAppError('NETWORK_ERROR', { detail: '本地 Session 存储不可用' });
    }
    wx.setStorageSync(storageKey, session);
    return session;
  }

  function getToken() {
    const session = readSession();
    return session && session.expiresAt > now() + EXPIRY_SKEW_MS ? session.token : '';
  }

  function getWxCode() {
    return new Promise(function (resolve, reject) {
      if (typeof wx === 'undefined' || typeof wx.login !== 'function') {
        reject(createAppError('NETWORK_ERROR', { detail: 'wx.login 不可用' }));
        return;
      }
      wx.login({
        success: function (res) {
          if (res && typeof res.code === 'string' && res.code) resolve(res.code);
          else reject(createAppError('NETWORK_ERROR', { detail: 'wx.login 未返回 code' }));
        },
        fail: function (err) {
          reject(createAppError('NETWORK_ERROR', { detail: err && err.errMsg ? err.errMsg : 'wx.login 失败' }));
        }
      });
    });
  }

  function login() {
    if (loginPromise) return loginPromise;
    const pending = getWxCode().then(function (code) {
      return request(AUTH_ENDPOINTS.login, {
        method: 'POST',
        auth: false,
        data: { code: code }
      });
    }).then(function (res) {
      const parsed = parseEnvelope(res.body);
      if (!parsed.ok) throw parsed.error;
      if (parsed.status === 'failure') throw mapHttpError(res.statusCode, parsed.code);
      const meta = validateEmptyMeta(parsed.meta);
      if (!meta.ok) throw createAppError('RESPONSE_INVALID', { detail: meta.issues.join('；') });
      return writeSession(parsed.data);
    });
    loginPromise = pending.then(
      function (value) { loginPromise = null; return value; },
      function (error) { loginPromise = null; throw error; }
    );
    return loginPromise;
  }

  function ensureSession() {
    const session = readSession();
    if (session && session.expiresAt > now() + EXPIRY_SKEW_MS) return Promise.resolve(session);
    clearSession();
    return login();
  }

  function logout() {
    const token = getToken();
    if (!token) {
      clearSession();
      return Promise.resolve();
    }
    return request(AUTH_ENDPOINTS.logout, { method: 'POST' }).catch(function () {
      // 本地退出不因网络失败而保留失效凭证。
    }).then(function () { clearSession(); });
  }

  return { readSession, getToken, clearSession, ensureSession, login, logout };
}

module.exports = { createAuthService, EXPIRY_SKEW_MS };
