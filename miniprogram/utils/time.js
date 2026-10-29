// 时间字段展示。原则：
// - 人员告警 ts 是本地 naive ISO，绝不能附加时区或按 UTC 解释。
// - occurred_at / device_captured_at（环境读数）是带时区 ISO，展示时保留原偏移。
// - 不做 Date 解析再格式化，避免改变时间语义。

const NAIVE_ISO_RE = /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?$/;
const ZONED_ISO_RE = /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?(Z|[+-]\d{2}:\d{2})$/;

function isNaiveIso(value) {
  return typeof value === 'string' && NAIVE_ISO_RE.test(value);
}

function isZonedIso(value) {
  return typeof value === 'string' && ZONED_ISO_RE.test(value);
}

function isEmpty(value) {
  return value === null || value === undefined || value === '';
}

// 仅把日期与时间之间的 T 换成空格，不改动任何时区/精度信息。
function displayRaw(value) {
  if (isEmpty(value)) {
    return '—';
  }
  return String(value).replace('T', ' ');
}

function formatNaiveTime(value) {
  return displayRaw(value);
}

function formatZonedTime(value) {
  return displayRaw(value);
}

module.exports = {
  isNaiveIso,
  isZonedIso,
  formatNaiveTime,
  formatZonedTime,
  displayRaw
};
