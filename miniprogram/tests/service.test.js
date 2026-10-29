// L1/L2：Mock 与真实模式隔离、本地拦截、错误码处理、响应异常判定。
const test = require('node:test');
const assert = require('node:assert/strict');

const { createS4Service } = require('../services/s4-service');
const { createMockAdapter } = require('../mock/mock-adapter');
const { createRequest, buildUrl } = require('../utils/request');
const fixtures = require('../mock/fixtures');

function okEnvelope(data, meta) {
  return {
    statusCode: 200,
    body: { code: 'OK', message: 'ok', data: data, meta: meta || {} }
  };
}

function failEnvelope(status, code) {
  return {
    statusCode: status,
    body: { code: code, message: code, data: null, meta: {} }
  };
}

function listMeta(page, pageSize, total, hasMore) {
  return { page: page, page_size: pageSize, total: total, has_more: hasMore };
}

test('Mock 模式不调用 wx.request', async () => {
  let wxRequestCalls = 0;
  const originalWx = global.wx;
  global.wx = {
    request: function () {
      wxRequestCalls += 1;
    },
    getStorageSync: function () {
      return 'unused-token';
    }
  };

  try {
    const service = createS4Service({
      useMock: true,
      mockAdapter: createMockAdapter({ latencyMs: 0 })
    });
    const result = await service.listAlerts({ page: 1, page_size: 5 });
    assert.equal(result.items.length, 5);
    assert.equal(result.meta.total, 40);
    await service.getSystemStatus();
    await service.getAlertDetail(fixtures.personAlerts[0].alert_key);
    await service.getAlertRelated(fixtures.sensorAlerts[0].alert_key);
    assert.equal(wxRequestCalls, 0);
  } finally {
    global.wx = originalWx;
  }
});

test('本地分页参数校验失败时不发起请求（Mock 与真实模式都必须生效）', async () => {
  let calls = 0;
  const request = function () {
    calls += 1;
    return Promise.resolve(okEnvelope([], listMeta(1, 20, 0, false)));
  };
  const realService = createS4Service({ useMock: false, request: request });
  await assert.rejects(realService.listAlerts({ page: 0 }), function (err) {
    return err.kind === 'LOCAL_INVALID_PARAMETER';
  });
  await assert.rejects(realService.listAlerts({ page_size: 101 }), function (err) {
    return err.kind === 'LOCAL_INVALID_PARAMETER';
  });
  await assert.rejects(realService.listAlerts({ category: 'all' }), function (err) {
    return err.kind === 'LOCAL_INVALID_PARAMETER';
  });
  assert.equal(calls, 0);

  let mockCalls = 0;
  const mockAdapter = {
    request: function () {
      mockCalls += 1;
      return Promise.resolve(okEnvelope([], listMeta(1, 20, 0, false)));
    }
  };
  const mockService = createS4Service({ useMock: true, mockAdapter: mockAdapter });
  await assert.rejects(mockService.listAlerts({ page: -1 }));
  assert.equal(mockCalls, 0);
});

test('非法 alert_key 在本地拦截，不发起请求', async () => {
  let calls = 0;
  const service = createS4Service({
    useMock: false,
    request: function () {
      calls += 1;
      return Promise.resolve(okEnvelope(fixtures.personAlerts[0], {}));
    }
  });
  await assert.rejects(service.getAlertDetail('person:'), function (err) {
    return err.kind === 'LOCAL_INVALID_PARAMETER';
  });
  await assert.rejects(service.getAlertRelated('bad-key'), function (err) {
    return err.kind === 'LOCAL_INVALID_PARAMETER';
  });
  assert.equal(calls, 0);
});

test('真实模式成功路径：请求参数、列表 DTO 与 meta', async () => {
  const seen = [];
  const request = function (path, query) {
    seen.push({ path: path, query: query });
    return Promise.resolve(
      okEnvelope([fixtures.personAlerts[0]], listMeta(1, 20, 1, false))
    );
  };
  const service = createS4Service({ useMock: false, request: request });
  const result = await service.listAlerts({ category: 'person', page: 1, page_size: 20 });
  assert.equal(result.meta.total, 1);
  assert.equal(result.items[0].alert_key, fixtures.personAlerts[0].alert_key);
  assert.equal(seen[0].path, '/api/v1/alerts');
  assert.deepEqual(seen[0].query, { page: 1, page_size: 20, category: 'person' });
});

test('真实模式错误码：401/403/404/500 按契约语义处理，且不回退 Mock', async () => {
  let mockCalls = 0;
  const mockAdapter = {
    request: function () {
      mockCalls += 1;
      return Promise.resolve(okEnvelope([], listMeta(1, 20, 0, false)));
    }
  };

  const cases = [
    { status: 401, code: 'UNAUTHORIZED', kind: 'UNAUTHORIZED' },
    { status: 403, code: 'FORBIDDEN', kind: 'FORBIDDEN' },
    { status: 404, code: 'NOT_FOUND', kind: 'NOT_FOUND' },
    { status: 500, code: 'INTERNAL_ERROR', kind: 'INTERNAL_ERROR' }
  ];

  for (const item of cases) {
    const service = createS4Service({
      useMock: false,
      mockAdapter: mockAdapter,
      request: function () {
        return Promise.resolve(failEnvelope(item.status, item.code));
      }
    });
    await assert.rejects(service.getAlertDetail('person:1100'), function (err) {
      return err.kind === item.kind && err.httpStatus === item.status;
    });
  }
  assert.equal(mockCalls, 0);
});

test('测试桩 400/422 按业务错误处理，不静默修正、不自动重试', async () => {
  const cases = [
    { status: 400, code: 'INVALID_PARAMETER', kind: 'INVALID_PARAMETER' },
    { status: 422, code: 'VALIDATION_ERROR', kind: 'VALIDATION_ERROR' }
  ];

  for (const item of cases) {
    let calls = 0;
    const service = createS4Service({
      useMock: false,
      request: function () {
        calls += 1;
        return Promise.resolve(failEnvelope(item.status, item.code));
      }
    });
    await assert.rejects(service.listAlerts({ page: 1, page_size: 20 }), function (err) {
      return err.kind === item.kind && err.httpStatus === item.status;
    });
    assert.equal(calls, 1, item.code + ' 不得自动重试');
  }
});

test('响应缺少 code 或结构异常判定为 RESPONSE_INVALID，不当作成功', async () => {
  const serviceMissingCode = createS4Service({
    useMock: false,
    request: function () {
      return Promise.resolve({
        statusCode: 200,
        body: { message: 'ok', data: [], meta: {} }
      });
    }
  });
  await assert.rejects(serviceMissingCode.getSystemStatus(), function (err) {
    return err.kind === 'RESPONSE_INVALID';
  });

  const serviceBadMeta = createS4Service({
    useMock: false,
    request: function () {
      return Promise.resolve(
        okEnvelope([fixtures.personAlerts[0]], {
          page: 1,
          page_size: 20,
          total: 1,
          has_more: 'false'
        })
      );
    }
  });
  await assert.rejects(serviceBadMeta.listAlerts({ page: 1, page_size: 20 }), function (err) {
    return err.kind === 'RESPONSE_INVALID';
  });

  const serviceBadDto = createS4Service({
    useMock: false,
    request: function () {
      return Promise.resolve(
        okEnvelope(
          [Object.assign({}, fixtures.personAlerts[0], { ts: '2026-10-27T10:42:15Z' })],
          listMeta(1, 20, 1, false)
        )
      );
    }
  });
  await assert.rejects(serviceBadDto.listAlerts({ page: 1, page_size: 20 }), function (err) {
    return err.kind === 'RESPONSE_INVALID';
  });
});

test('关联数据：人员告警不得返回环境读数', async () => {
  const reading = Object.keys(fixtures.readingsById).map(function (key) {
    return fixtures.readingsById[key];
  })[0];
  const service = createS4Service({
    useMock: false,
    request: function () {
      return Promise.resolve(
        okEnvelope({ readings: [reading], media: [], media_supported: false }, {})
      );
    }
  });
  await assert.rejects(service.getAlertRelated('person:1100'), function (err) {
    return err.kind === 'RESPONSE_INVALID';
  });
});

test('真实请求封装：Token 缺失时本地拦截，不调用 wx.request', async () => {
  let calls = 0;
  const originalWx = global.wx;
  global.wx = {
    request: function () {
      calls += 1;
    }
  };
  try {
    const request = createRequest({
      baseUrl: 'https://example.invalid',
      timeout: 1000,
      getToken: function () {
        return '';
      }
    });
    await assert.rejects(request('/api/v1/system/status', {}), function (err) {
      return err.kind === 'TOKEN_MISSING';
    });
    assert.equal(calls, 0);
  } finally {
    global.wx = originalWx;
  }
});

test('真实请求封装：Bearer 头、GET、URL 编码与超时错误', async () => {
  const captured = [];
  const originalWx = global.wx;
  global.wx = {
    request: function (options) {
      captured.push(options);
      if (options.url.indexOf('timeout') !== -1) {
        options.fail({ errMsg: 'request:fail timeout' });
        return;
      }
      options.success({
        statusCode: 200,
        data: { code: 'OK', message: 'ok', data: {}, meta: {} }
      });
    }
  };
  try {
    const request = createRequest({
      baseUrl: 'https://example.invalid/',
      timeout: 1000,
      getToken: function () {
        return 'abc';
      }
    });
    const res = await request('/api/v1/alerts', { category: 'person', page: 1 });
    assert.equal(res.statusCode, 200);
    assert.equal(captured[0].method, 'GET');
    assert.equal(captured[0].header.Authorization, 'Bearer abc');
    assert.equal(
      captured[0].url,
      'https://example.invalid/api/v1/alerts?category=person&page=1'
    );

    await assert.rejects(request('/timeout', {}), function (err) {
      return err.kind === 'TIMEOUT';
    });
  } finally {
    global.wx = originalWx;
  }
});

test('buildUrl 忽略 undefined/null 参数', () => {
  assert.equal(
    buildUrl('https://x.test/', '/api/v1/alerts', { page: 1, category: undefined, x: null }),
    'https://x.test/api/v1/alerts?page=1'
  );
});
