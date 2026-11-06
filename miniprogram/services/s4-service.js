// S4 查询服务。Mock 与真实模式严格隔离：
// - useMock=true  只调用 mockAdapter.request，绝不调用 wx.request。
// - useMock=false 只调用真实 request，失败后绝不回退 Mock。
// 两种模式共用同一套本地校验、信封校验和 DTO 校验。

const { ENDPOINTS, MEDIA_ENDPOINTS } = require('../utils/dto-schema');
const {
  validatePagination,
  validateCategory,
  validateAlertKey
} = require('../utils/validators');
const {
  parseEnvelope,
  validateSystemStatusData,
  validateListMeta,
  validateEmptyMeta,
  validateAlertListData,
  validateAlertDetailData,
  validateRelatedData,
  validateMediaListData,
  validateMediaAccessData,
  issuesToError
} = require('../utils/envelope');
const { createAppError, mapHttpError } = require('../utils/errors');

function rejectWith(error) {
  return Promise.reject(error);
}

function createS4Service(options) {
  const opts = options || {};
  const useMock = opts.useMock === true;
  const mockAdapter = opts.mockAdapter;
  const realRequest = opts.request;
  const endpoints = opts.endpoints || ENDPOINTS;

  function dispatch(path, query, requestOptions) {
    if (useMock) {
      if (!mockAdapter || typeof mockAdapter.request !== 'function') {
        return rejectWith(new Error('Mock 模式缺少 mockAdapter'));
      }
      return mockAdapter.request(path, query);
    }
    if (typeof realRequest !== 'function') {
      return rejectWith(new Error('真实模式缺少 request 实现'));
    }
    return realRequest(path, requestOptions || query);
  }

  // 返回成功信封的 data/meta；失败信封按契约错误码抛出。
  function call(path, query, requestOptions) {
    return dispatch(path, query, requestOptions).then(function (res) {
      const parsed = parseEnvelope(res.body);
      if (!parsed.ok) {
        throw parsed.error;
      }
      if (parsed.status === 'failure') {
        throw mapHttpError(res.statusCode, parsed.code);
      }
      return { data: parsed.data, meta: parsed.meta };
    });
  }

  function assertEmptyMeta(meta) {
    const checked = validateEmptyMeta(meta);
    if (!checked.ok) {
      throw issuesToError(checked.issues);
    }
  }

  function getSystemStatus() {
    return call(endpoints.systemStatus, {}).then(function (result) {
      assertEmptyMeta(result.meta);
      const checked = validateSystemStatusData(result.data);
      if (!checked.ok) {
        throw issuesToError(checked.issues);
      }
      return result.data;
    });
  }

  function listAlerts(params) {
    const input = params || {};
    const pagination = validatePagination(input);
    if (!pagination.ok) {
      return rejectWith(pagination.error);
    }
    const category = validateCategory(input.category);
    if (!category.ok) {
      return rejectWith(category.error);
    }

    const query = {
      page: pagination.value.page,
      page_size: pagination.value.page_size
    };
    if (category.value) {
      query.category = category.value;
    }

    return call(endpoints.alerts, query).then(function (result) {
      const metaChecked = validateListMeta(result.meta);
      if (!metaChecked.ok) {
        throw issuesToError(metaChecked.issues);
      }
      const dataChecked = validateAlertListData(result.data, category.value);
      if (!dataChecked.ok) {
        throw issuesToError(dataChecked.issues);
      }
      return {
        items: result.data,
        meta: result.meta
      };
    });
  }

  function getAlertDetail(alertKey) {
    const keyChecked = validateAlertKey(alertKey);
    if (!keyChecked.ok) {
      return rejectWith(keyChecked.error);
    }
    return call(endpoints.alertDetail(keyChecked.value), {}).then(function (result) {
      assertEmptyMeta(result.meta);
      const checked = validateAlertDetailData(
        result.data,
        keyChecked.value,
        keyChecked.prefix
      );
      if (!checked.ok) {
        throw issuesToError(checked.issues);
      }
      return result.data;
    });
  }

  function getAlertRelated(alertKey) {
    const keyChecked = validateAlertKey(alertKey);
    if (!keyChecked.ok) {
      return rejectWith(keyChecked.error);
    }
    return call(endpoints.alertRelated(keyChecked.value), {}).then(function (result) {
      assertEmptyMeta(result.meta);
      const checked = validateRelatedData(result.data, keyChecked.prefix);
      if (!checked.ok) {
        throw issuesToError(checked.issues);
      }
      return result.data;
    });
  }

  function listAlertMedia(alertKey, params) {
    const keyChecked = validateAlertKey(alertKey);
    if (!keyChecked.ok) return rejectWith(keyChecked.error);
    const pagination = validatePagination(params || {});
    if (!pagination.ok) return rejectWith(pagination.error);
    const query = {
      page: pagination.value.page,
      page_size: pagination.value.page_size
    };
    return call(MEDIA_ENDPOINTS.alertMedia(keyChecked.value), query).then(function (result) {
      const metaChecked = validateListMeta(result.meta);
      if (!metaChecked.ok) throw issuesToError(metaChecked.issues);
      const dataChecked = validateMediaListData(result.data);
      if (!dataChecked.ok) throw issuesToError(dataChecked.issues);
      return { items: result.data, meta: result.meta };
    });
  }

  function getMediaAccess(mediaId) {
    const id = mediaId === undefined || mediaId === null ? '' : String(mediaId).trim();
    if (!id || /\s/.test(id)) {
      return rejectWith(createAppError('LOCAL_INVALID_PARAMETER', {
        detail: 'media_id 必须是非空且不含空白的字符串'
      }));
    }
    return call(MEDIA_ENDPOINTS.mediaAccess(id), {}, {
      method: 'POST',
      data: {}
    }).then(function (result) {
      assertEmptyMeta(result.meta);
      const checked = validateMediaAccessData(result.data);
      if (!checked.ok) throw issuesToError(checked.issues);
      return result.data;
    });
  }

  return {
    useMock: useMock,
    getSystemStatus: getSystemStatus,
    listAlerts: listAlerts,
    getAlertDetail: getAlertDetail,
    getAlertRelated: getAlertRelated,
    listAlertMedia: listAlertMedia,
    getMediaAccess: getMediaAccess
  };
}

module.exports = {
  createS4Service
};
