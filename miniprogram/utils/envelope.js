// 响应信封与 DTO 校验。依据 schemas/s4_contract.json 0.1.0。
// 所有响应必须按契约校验，不能只看 HTTP 状态码。

const { DTO, PAGINATION, SENSOR_STATUS_VALUES } = require('./dto-schema');
const { isNaiveIso, isZonedIso } = require('./time');
const { createAppError } = require('./errors');

const ENVELOPE_FIELDS = ['code', 'message', 'data', 'meta'];

function isPlainObject(value) {
  return (
    value !== null &&
    typeof value === 'object' &&
    !Array.isArray(value)
  );
}

function hasOwn(obj, key) {
  return Object.prototype.hasOwnProperty.call(obj, key);
}

function invalid(detail) {
  return {
    ok: false,
    error: createAppError('RESPONSE_INVALID', { detail })
  };
}

// 解析四字段信封。
// 返回 status='success' | 'failure'，结构异常时返回 ok=false。
function parseEnvelope(body) {
  if (!isPlainObject(body)) {
    return invalid('响应不是 JSON 对象');
  }
  for (let i = 0; i < ENVELOPE_FIELDS.length; i += 1) {
    if (!hasOwn(body, ENVELOPE_FIELDS[i])) {
      return invalid('响应缺少契约字段：' + ENVELOPE_FIELDS[i]);
    }
  }
  if (typeof body.code !== 'string') {
    return invalid('响应 code 不是字符串');
  }
  if (typeof body.message !== 'string') {
    return invalid('响应 message 不是字符串');
  }
  if (!isPlainObject(body.meta)) {
    return invalid('响应 meta 不是对象');
  }

  if (body.code === 'OK') {
    if (body.message !== 'ok') {
      return invalid('成功响应 message 必须为 ok');
    }
    if (body.data === null || body.data === undefined) {
      return invalid('成功响应 data 不能为空');
    }
    return {
      ok: true,
      status: 'success',
      code: body.code,
      message: body.message,
      data: body.data,
      meta: body.meta
    };
  }

  // 失败响应：契约要求 data=null、meta={}。
  if (body.data !== null) {
    return invalid('失败响应 data 必须为 null');
  }
  if (Object.keys(body.meta).length !== 0) {
    return invalid('失败响应 meta 必须为空对象');
  }
  return {
    ok: true,
    status: 'failure',
    code: body.code,
    message: body.message,
    data: null,
    meta: body.meta
  };
}

function validateDto(dto, schema, path, issues) {
  if (!isPlainObject(dto)) {
    issues.push(path + ' 不是对象');
    return;
  }
  for (let i = 0; i < schema.length; i += 1) {
    const field = schema[i];
    const fieldPath = path + '.' + field.name;
    if (!hasOwn(dto, field.name)) {
      issues.push(fieldPath + ' 缺失（契约要求显式字段，缺失以 null 表示）');
      continue;
    }
    const value = dto[field.name];
    if (value === null) {
      if (!field.nullable) {
        issues.push(fieldPath + ' 不允许为 null');
      }
      continue;
    }
    if (field.type === 'string') {
      if (typeof value !== 'string') {
        issues.push(fieldPath + ' 不是字符串');
        continue;
      }
      if (field.constValue !== undefined && value !== field.constValue) {
        issues.push(fieldPath + ' 必须为 ' + field.constValue);
      }
      if (field.prefix !== undefined && value.indexOf(field.prefix) !== 0) {
        issues.push(fieldPath + ' 必须以 ' + field.prefix + ' 开头');
      }
      if (field.allowed !== undefined && field.allowed.indexOf(value) === -1) {
        issues.push(fieldPath + ' 取值不在契约允许范围');
      }
      if (field.format === 'naive-iso' && !isNaiveIso(value)) {
        issues.push(fieldPath + ' 不是契约要求的 naive ISO 时间');
      }
      if (field.format === 'zoned-iso' && !isZonedIso(value)) {
        issues.push(fieldPath + ' 不是契约要求的带时区 ISO 时间');
      }
    } else if (field.type === 'integer') {
      if (typeof value !== 'number' || !Number.isInteger(value)) {
        issues.push(fieldPath + ' 不是整数');
      }
    } else if (field.type === 'number') {
      if (typeof value !== 'number' || !Number.isFinite(value)) {
        issues.push(fieldPath + ' 不是有效数字');
      }
    } else if (field.type === 'boolean') {
      if (typeof value !== 'boolean') {
        issues.push(fieldPath + ' 不是布尔值');
      }
    } else {
      issues.push(fieldPath + ' 存在未知契约类型定义');
    }
  }
}

function validatePersonAlert(dto, path, issues) {
  validateDto(dto, DTO.personAlert, path, issues);
}

function validateSensorAlert(dto, path, issues) {
  validateDto(dto, DTO.sensorAlert, path, issues);
}

function validateEnvironmentReading(dto, path, issues) {
  validateDto(dto, DTO.environmentReading, path, issues);
}

// S4-01 data：{ device: { status }, modules: {} }
function validateSystemStatusData(data) {
  const issues = [];
  if (!isPlainObject(data)) {
    return { ok: false, issues: ['data 不是对象'] };
  }
  if (!isPlainObject(data.device)) {
    issues.push('data.device 不是对象');
  } else {
    if (typeof data.device.status !== 'string') {
      issues.push('data.device.status 不是字符串');
    } else if (data.device.status.length === 0) {
      issues.push('data.device.status 为空字符串');
    }
  }
  if (!isPlainObject(data.modules)) {
    issues.push('data.modules 不是对象');
  }
  return { ok: issues.length === 0, issues };
}

// S4-02 meta：page/page_size/total/has_more
function validateListMeta(meta) {
  const issues = [];
  const fields = PAGINATION.metaFields;
  if (!isPlainObject(meta)) {
    return { ok: false, issues: ['meta 不是对象'] };
  }
  for (let i = 0; i < fields.length; i += 1) {
    if (!hasOwn(meta, fields[i])) {
      issues.push('meta 缺少字段：' + fields[i]);
    }
  }
  if (!Number.isInteger(meta.page) || meta.page < 1) {
    issues.push('meta.page 不是大于等于 1 的整数');
  }
  if (
    !Number.isInteger(meta.page_size) ||
    meta.page_size < 1 ||
    meta.page_size > PAGINATION.pageSizeMax
  ) {
    issues.push('meta.page_size 不是 1 至 ' + PAGINATION.pageSizeMax + ' 的整数');
  }
  if (!Number.isInteger(meta.total) || meta.total < 0) {
    issues.push('meta.total 不是大于等于 0 的整数');
  }
  if (typeof meta.has_more !== 'boolean') {
    issues.push('meta.has_more 不是布尔值');
  }
  return { ok: issues.length === 0, issues };
}

function validateEmptyMeta(meta) {
  if (!isPlainObject(meta)) {
    return { ok: false, issues: ['meta 不是对象'] };
  }
  if (Object.keys(meta).length !== 0) {
    return { ok: false, issues: ['该接口成功响应 meta 必须为空对象'] };
  }
  return { ok: true, issues: [] };
}

// S4-02 data：人员 DTO + 传感器 DTO 的合并数组。
function validateAlertListData(data, expectedCategory) {
  const issues = [];
  if (!Array.isArray(data)) {
    return { ok: false, issues: ['data 不是数组'] };
  }
  for (let i = 0; i < data.length; i += 1) {
    const item = data[i];
    const path = 'data[' + i + ']';
    if (!isPlainObject(item)) {
      issues.push(path + ' 不是对象');
      continue;
    }
    if (item.category === 'person') {
      validatePersonAlert(item, path, issues);
      if (expectedCategory === 'sensor_environment') {
        issues.push(path + ' 不属于请求的 category');
      }
    } else if (item.category === 'sensor_environment') {
      validateSensorAlert(item, path, issues);
      if (expectedCategory === 'person') {
        issues.push(path + ' 不属于请求的 category');
      }
    } else {
      issues.push(path + '.category 不是契约允许的告警分类');
    }
  }
  return { ok: issues.length === 0, issues };
}

// S4-03 data：单条 DTO，且必须与请求的 alert_key 一致。
function validateAlertDetailData(data, alertKey, prefix) {
  const issues = [];
  if (!isPlainObject(data)) {
    return { ok: false, issues: ['data 不是对象'] };
  }
  if (data.alert_key !== alertKey) {
    issues.push('data.alert_key 与请求的 alert_key 不一致');
  }
  if (prefix === 'person:') {
    validatePersonAlert(data, 'data', issues);
  } else if (prefix === 'sensor:') {
    validateSensorAlert(data, 'data', issues);
  } else {
    issues.push('无法根据 alert_key 前缀确定 DTO 类型');
  }
  return { ok: issues.length === 0, issues };
}

// S4-04 data：{ readings: [], media: [], media_supported: boolean }
function validateRelatedData(data, prefix) {
  const issues = [];
  if (!isPlainObject(data)) {
    return { ok: false, issues: ['data 不是对象'] };
  }
  if (!Array.isArray(data.readings)) {
    issues.push('data.readings 不是数组');
  } else {
    if (data.readings.length > 1) {
      issues.push('data.readings 最多只允许 1 条');
    }
    for (let i = 0; i < data.readings.length; i += 1) {
      validateEnvironmentReading(data.readings[i], 'data.readings[' + i + ']', issues);
    }
    if (prefix === 'person:' && data.readings.length > 0) {
      issues.push('人员告警不得关联环境读数');
    }
  }
  if (!Array.isArray(data.media)) {
    issues.push('data.media 不是数组');
  }
  if (typeof data.media_supported !== 'boolean') {
    issues.push('data.media_supported 不是布尔值');
  }
  if (data.media_supported === false && Array.isArray(data.media) && data.media.length > 0) {
    issues.push('media_supported=false 时 media 必须为空数组');
  }
  return { ok: issues.length === 0, issues };
}

function issuesToError(issues) {
  return createAppError('RESPONSE_INVALID', {
    detail: issues.join('；'),
    issues
  });
}

module.exports = {
  ENVELOPE_FIELDS,
  SENSOR_STATUS_VALUES,
  isPlainObject,
  parseEnvelope,
  validateDto,
  validatePersonAlert,
  validateSensorAlert,
  validateEnvironmentReading,
  validateSystemStatusData,
  validateListMeta,
  validateEmptyMeta,
  validateAlertListData,
  validateAlertDetailData,
  validateRelatedData,
  issuesToError
};
