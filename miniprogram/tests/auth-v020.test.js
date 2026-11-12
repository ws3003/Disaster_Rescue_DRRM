const test = require('node:test');
const assert = require('node:assert/strict');

const schema = require('../utils/dto-schema');
const { createAuthService } = require('../services/auth-service');
const { createRequest } = require('../utils/request');
const { createS4Service } = require('../services/s4-service');

function envelope(data, meta) {
  return { statusCode: 200, body: { code: 'OK', message: 'ok', data, meta: meta || {} } };
}

test('v0.2.0 用户态路径与认证、媒体路径固定', () => {
  assert.equal(schema.USER_CONTRACT_VERSION, '0.2.0');
  assert.equal(schema.AUTH_ENDPOINTS.login, '/api/v1/auth/login');
  assert.equal(schema.USER_ENDPOINTS.alerts, '/api/v1/user/alerts');
  assert.equal(schema.USER_ENDPOINTS.alertDetail('person:1'), '/api/v1/user/alerts/person%3A1');
  assert.equal(schema.MEDIA_ENDPOINTS.alertMedia('sensor:A'), '/api/v1/alerts/sensor%3AA/media');
  assert.equal(schema.MEDIA_ENDPOINTS.mediaAccess('M-1'), '/api/v1/media/M-1/access');
});

test('登录只发送 wx code，不携带 Authorization，并保存短期用户 Session', async () => {
  const originalWx = global.wx;
  const storage = {};
  const captured = [];
  global.wx = {
    login(options) { options.success({ code: 'wx-one-time-code' }); },
    request(options) {
      captured.push(options);
      options.success({
        statusCode: 200,
        data: { code: 'OK', message: 'ok', data: {
          session_token: 'session-only', expires_in: 7200, user_id: 'U-1'
        }, meta: {} }
      });
    },
    getStorageSync(key) { return storage[key]; },
    setStorageSync(key, value) { storage[key] = value; },
    removeStorageSync(key) { delete storage[key]; }
  };
  try {
    let auth;
    const request = createRequest({
      baseUrl: 'https://api.example.test', timeout: 1000,
      getToken() { return auth ? auth.getToken() : ''; }
    });
    auth = createAuthService({ request, storageKey: 'session', now: () => 1000 });
    const session = await auth.ensureSession();
    assert.equal(session.token, 'session-only');
    assert.equal(captured.length, 1);
    assert.equal(captured[0].url, 'https://api.example.test/api/v1/auth/login');
    assert.equal(captured[0].method, 'POST');
    assert.equal(captured[0].header.Authorization, undefined);
    assert.deepEqual(captured[0].data, { code: 'wx-one-time-code' });
    assert.equal(auth.getToken(), 'session-only');
  } finally {
    global.wx = originalWx;
  }
});

test('并发 ensureSession 只调用一次 wx.login', async () => {
  const originalWx = global.wx;
  const storage = {};
  let loginCalls = 0;
  global.wx = {
    login(options) { loginCalls += 1; setTimeout(() => options.success({ code: 'code' }), 1); },
    getStorageSync(key) { return storage[key]; },
    setStorageSync(key, value) { storage[key] = value; },
    removeStorageSync(key) { delete storage[key]; }
  };
  try {
    const auth = createAuthService({
      storageKey: 'session', now: () => 0,
      request() {
        return Promise.resolve(envelope({ session_token: 's', expires_in: 3600, user_id: 'u' }, {}));
      }
    });
    const results = await Promise.all([auth.ensureSession(), auth.ensureSession()]);
    assert.equal(loginCalls, 1);
    assert.equal(results[0].token, 's');
    assert.equal(results[1].token, 's');
  } finally {
    global.wx = originalWx;
  }
});

test('媒体列表只接收元数据，媒体访问必须 POST 且只返回短时地址', async () => {
  const calls = [];
  const service = createS4Service({
    useMock: false,
    endpoints: schema.USER_ENDPOINTS,
    request(path, input) {
      calls.push({ path, input });
      if (path.endsWith('/media')) {
        return Promise.resolve(envelope([{
          media_id: 'M-1', media_type: 'video', status: 'success',
          upload_status: 'success', captured_at: null, uploaded_at: '2026-11-04T20:00:00+08:00'
        }], { page: 1, page_size: 20, total: 1, has_more: false }));
      }
      return Promise.resolve(envelope({
        access_url: 'https://oss.example.test/signed',
        expires_at: '2026-11-04T20:05:00+08:00'
      }, {}));
    }
  });
  const list = await service.listAlertMedia('sensor:A', {});
  assert.equal(list.items[0].media_id, 'M-1');
  const access = await service.getMediaAccess('M-1');
  assert.equal(access.access_url, 'https://oss.example.test/signed');
  assert.equal(calls[1].input.method, 'POST');
  assert.equal(Object.prototype.hasOwnProperty.call(list.items[0], 'object_key'), false);
});

test('媒体响应夹带 object_key 或永久 URL 字段时拒绝', async () => {
  const service = createS4Service({
    useMock: false,
    request() {
      return Promise.resolve(envelope([{
        media_id: 'M-1', media_type: 'video', status: 'success', upload_status: 'success',
        captured_at: null, uploaded_at: null, object_key: 'secret/path.mp4'
      }], { page: 1, page_size: 20, total: 1, has_more: false }));
    }
  });
  await assert.rejects(service.listAlertMedia('sensor:A', {}), (error) => error.kind === 'RESPONSE_INVALID');
});
