// 默认服务工厂：页面只依赖这里，不直接接触 wx.request 或 Mock 细节。
// 模式切换只由 config/env.js 的 USE_MOCK 决定。

const env = require('../config/env');
const { createMockAdapter } = require('../mock/mock-adapter');
const { createRequest, getTokenFromStorage } = require('../utils/request');
const { createS4Service } = require('./s4-service');

function createDefaultService() {
  if (env.USE_MOCK) {
    return createS4Service({
      useMock: true,
      mockAdapter: createMockAdapter()
    });
  }
  return createS4Service({
    useMock: false,
    request: createRequest({
      baseUrl: env.API_BASE_URL,
      timeout: env.REQUEST_TIMEOUT_MS,
      getToken: function () {
        return getTokenFromStorage(env.TOKEN_STORAGE_KEY);
      }
    })
  });
}

module.exports = {
  createDefaultService
};
