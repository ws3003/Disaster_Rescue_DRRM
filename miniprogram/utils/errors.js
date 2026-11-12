// 统一错误模型。错误语义严格来自 schemas/s4_contract.json 0.1.0。
// 用户可见文案可在联调前统一确认，但不得改变契约定义的错误语义。

const CONTRACT_ERRORS = {
  INVALID_PARAMETER: { httpStatus: 400, message: '参数无效', retryable: false },
  UNAUTHORIZED: { httpStatus: 401, message: '未登录或访问凭证无效', retryable: false },
  FORBIDDEN: { httpStatus: 403, message: '无访问权限', retryable: false },
  NOT_FOUND: { httpStatus: 404, message: '资源不存在', retryable: false },
  VALIDATION_ERROR: { httpStatus: 422, message: '请求校验失败', retryable: false },
  INTERNAL_ERROR: { httpStatus: 500, message: '服务端异常', retryable: true }
};

// v0.2.0 媒体接口新增状态；与 v0.1.0 核心错误表分开，避免改变旧契约。
const USER_API_ERRORS = {
  CONFLICT: { httpStatus: 409, message: '资源状态冲突', retryable: false },
  SERVICE_UNAVAILABLE: { httpStatus: 503, message: '服务暂不可用', retryable: true }
};

const LOCAL_ERRORS = {
  LOCAL_INVALID_PARAMETER: { message: '参数无效（本地校验未通过，未发起请求）', retryable: false },
  TOKEN_MISSING: { message: '未配置访问凭证，已拦截请求', retryable: false },
  NETWORK_ERROR: { message: '网络异常，请稍后重试', retryable: true },
  TIMEOUT: { message: '请求超时，请稍后重试', retryable: true },
  RESPONSE_INVALID: { message: '响应格式异常，不符合接口契约', retryable: false }
};

function createAppError(kind, overrides) {
  const base = CONTRACT_ERRORS[kind] || USER_API_ERRORS[kind] || LOCAL_ERRORS[kind] || {
    message: '未知错误',
    retryable: false
  };
  return Object.assign(
    {
      kind,
      message: base.message,
      retryable: base.retryable,
      httpStatus: base.httpStatus === undefined ? null : base.httpStatus
    },
    overrides || {}
  );
}

function isKnownContractCode(code) {
  return Object.prototype.hasOwnProperty.call(CONTRACT_ERRORS, code) ||
    Object.prototype.hasOwnProperty.call(USER_API_ERRORS, code);
}

// HTTP 状态码与业务错误码都必须参与错误判定。
// 优先采用契约定义的业务错误码语义；无已知业务码时按 HTTP 状态码归类。
function mapHttpError(httpStatus, envelopeCode) {
  if (isKnownContractCode(envelopeCode)) {
    return createAppError(envelopeCode, {
      httpStatus,
      code: envelopeCode,
      // 记录状态码与业务码不一致的情况，但不改变业务码语义。
      statusMismatch:
        (CONTRACT_ERRORS[envelopeCode] || USER_API_ERRORS[envelopeCode]).httpStatus !== Number(httpStatus)
    });
  }

  const byStatus = {
    400: 'INVALID_PARAMETER',
    401: 'UNAUTHORIZED',
    403: 'FORBIDDEN',
    404: 'NOT_FOUND',
    409: 'CONFLICT',
    422: 'VALIDATION_ERROR',
    500: 'INTERNAL_ERROR',
    503: 'SERVICE_UNAVAILABLE'
  };
  const kind = byStatus[Number(httpStatus)];
  if (kind) {
    return createAppError(kind, {
      httpStatus,
      code: envelopeCode === undefined || envelopeCode === null ? null : envelopeCode
    });
  }

  return createAppError('NETWORK_ERROR', {
    kind: 'HTTP_UNEXPECTED',
    httpStatus,
    code: envelopeCode === undefined || envelopeCode === null ? null : envelopeCode,
    message: '服务异常（HTTP ' + httpStatus + '）',
    retryable: true
  });
}

// 统一把未知异常归一为可展示的错误对象，避免页面直接显示原始异常。
function normalizeError(err) {
  if (err && typeof err === 'object' && err.kind && err.message) {
    return err;
  }
  return createAppError('NETWORK_ERROR', {
    detail: err && err.message ? String(err.message) : String(err)
  });
}

function mapNetworkError(wxError) {
  const message = wxError && wxError.errMsg ? String(wxError.errMsg) : '';
  if (/timeout/i.test(message)) {
    return createAppError('TIMEOUT', { detail: message });
  }
  return createAppError('NETWORK_ERROR', { detail: message });
}

module.exports = {
  CONTRACT_ERRORS,
  USER_API_ERRORS,
  LOCAL_ERRORS,
  createAppError,
  isKnownContractCode,
  mapHttpError,
  normalizeError,
  mapNetworkError
};
