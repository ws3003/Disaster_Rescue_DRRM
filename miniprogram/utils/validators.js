// L1 本地输入校验：必须在发起任何请求之前执行，且不依赖 Mock 模式。
// 规则来自契约 pagination 定义；显式无效值必须拦截，不得静默截断、夹紧或改默认值。

const { CATEGORIES, PAGINATION } = require('./dto-schema');
const { createAppError } = require('./errors');

function isMissing(value) {
  return value === undefined || value === null || value === '';
}

function isInteger(value) {
  return typeof value === 'number' && Number.isInteger(value);
}

function validatePagination(raw) {
  const input = raw || {};
  const value = {
    page: PAGINATION.pageDefault,
    page_size: PAGINATION.pageSizeDefault
  };
  const fieldErrors = [];

  if (!isMissing(input.page)) {
    if (!isInteger(input.page) || input.page < 1) {
      fieldErrors.push('page 必须为大于等于 1 的整数');
    } else {
      value.page = input.page;
    }
  }

  if (!isMissing(input.page_size)) {
    if (
      !isInteger(input.page_size) ||
      input.page_size < 1 ||
      input.page_size > PAGINATION.pageSizeMax
    ) {
      fieldErrors.push('page_size 必须为 1 至 ' + PAGINATION.pageSizeMax + ' 的整数');
    } else {
      value.page_size = input.page_size;
    }
  }

  if (fieldErrors.length > 0) {
    return {
      ok: false,
      error: createAppError('LOCAL_INVALID_PARAMETER', {
        fieldErrors,
        detail: fieldErrors.join('；')
      })
    };
  }
  return { ok: true, value };
}

function validateCategory(raw) {
  if (isMissing(raw)) {
    return { ok: true, value: undefined };
  }
  if (typeof raw !== 'string' || CATEGORIES.indexOf(raw) === -1) {
    return {
      ok: false,
      error: createAppError('LOCAL_INVALID_PARAMETER', {
        fieldErrors: ['category 仅允许 ' + CATEGORIES.join(' / ')],
        detail: 'category 仅允许 ' + CATEGORIES.join(' / ')
      })
    };
  }
  return { ok: true, value: raw };
}

// 只做契约声明的格式检查：person:<event_id> 或 sensor:<alert_id>。
// 不猜测、不拼接、不解析后端标识本身的类型。
const ALERT_KEY_RE = /^(person|sensor):[^\s]+$/;

function validateAlertKey(raw) {
  if (isMissing(raw) || typeof raw !== 'string') {
    return {
      ok: false,
      error: createAppError('LOCAL_INVALID_PARAMETER', {
        fieldErrors: ['alert_key 不能为空'],
        detail: 'alert_key 不能为空'
      })
    };
  }
  if (!ALERT_KEY_RE.test(raw)) {
    return {
      ok: false,
      error: createAppError('LOCAL_INVALID_PARAMETER', {
        fieldErrors: ['alert_key 必须为 person:<event_id> 或 sensor:<alert_id> 格式'],
        detail: 'alert_key 必须为 person:<event_id> 或 sensor:<alert_id> 格式'
      })
    };
  }
  return { ok: true, value: raw, prefix: raw.slice(0, raw.indexOf(':')) + ':' };
}

module.exports = {
  isMissing,
  isInteger,
  validatePagination,
  validateCategory,
  validateAlertKey
};
