// 默认服务工厂：页面只依赖这里，不直接接触 wx.request 或 Mock 细节。
// 模式切换只由 config/env.js 的 USE_MOCK 决定。

const env = require('../config/env');
const { createMockAdapter } = require('../mock/mock-adapter');
const { USER_ENDPOINTS } = require('../utils/dto-schema');
const { createRequest } = require('../utils/request');
const { createAuthService } = require('./auth-service');
const { createS4Service } = require('./s4-service');

function createDefaultService() {
  if (env.USE_MOCK) {
    return createS4Service({
      useMock: true,
      mockAdapter: createMockAdapter()
    });
  }
  let auth = null;
  const request = createRequest({
    baseUrl: env.API_BASE_URL,
    timeout: env.REQUEST_TIMEOUT_MS,
    getToken: function () { return auth ? auth.getToken() : ''; }
  });
  auth = createAuthService({
    request: request,
    storageKey: env.SESSION_STORAGE_KEY
  });
  const service = createS4Service({
    useMock: false,
    request: request,
    endpoints: USER_ENDPOINTS
  });

  function withSession(action) {
    return auth.ensureSession().then(action).catch(function (error) {
      if (!error || error.kind !== 'UNAUTHORIZED') throw error;
      auth.clearSession();
      return auth.ensureSession().then(action);
    });
  }

  return {
    useMock: false,
    getSystemStatus: function () { return withSession(service.getSystemStatus); },
    listAlerts: function (params) {
      return withSession(function () { return service.listAlerts(params); });
    },
    getAlertDetail: function (key) {
      return withSession(function () { return service.getAlertDetail(key); });
    },
    getAlertRelated: function (key) {
      return withSession(function () { return service.getAlertRelated(key); });
    },
    listAlertMedia: function (key, params) {
      return withSession(function () { return service.listAlertMedia(key, params); });
    },
    getMediaAccess: function (mediaId) {
      return withSession(function () { return service.getMediaAccess(mediaId); });
    },
    logout: auth.logout
  };
}

module.exports = {
  createDefaultService
};
