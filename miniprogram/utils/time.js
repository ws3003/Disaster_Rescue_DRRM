// 时间字段展示。原则：
// - 人员告警 ts 是本地 naive ISO，绝不能附加时区或按 UTC 解释。
// - occurred_at / device_captured_at（环境读数）是带时区 ISO，展示时保留原偏移。
// - 绝对时间直接透传原始字符串（只把 T 换成空格），不做 Date 解析再格式化。
// - 相对时间/日期分组是“展示层派生值”，不写回 DTO，也不改变契约语义。
//   naive 时间没有时区信息，只能按运行设备的本地时区解释（这是不额外附加信息的唯一选择）；
//   zoned 时间按 ISO 偏移解析。

const NAIVE_ISO_RE = /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?$/;
const ZONED_ISO_RE = /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?(Z|[+-]\d{2}:\d{2})$/;
const DATE_PREFIX_RE = /^(\d{4})-(\d{2})-(\d{2})/;

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

// 取日期部分（YYYY-MM-DD）。只读字符串前缀，不做时区换算，因此对 naive / zoned 都安全。
function dateKeyOf(value) {
  if (typeof value !== 'string') {
    return '';
  }
  const matched = DATE_PREFIX_RE.exec(value);
  return matched ? matched[1] + '-' + matched[2] + '-' + matched[3] : '';
}

// 解析为毫秒时间戳，仅用于派生展示。
// - zoned：按 ISO 偏移解析（正确）。
// - naive：按设备本地时区解析（契约没有时区，不能凭空附加 UTC 或 +08:00）。
// 无法解析时返回 NaN。
function parseTimeMs(value) {
  if (typeof value !== 'string') {
    return NaN;
  }
  if (isZonedIso(value)) {
    return Date.parse(value);
  }
  if (isNaiveIso(value)) {
    const matched = /^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})(\.\d+)?$/.exec(value);
    if (!matched) {
      return NaN;
    }
    return new Date(
      Number(matched[1]),
      Number(matched[2]) - 1,
      Number(matched[3]),
      Number(matched[4]),
      Number(matched[5]),
      Number(matched[6])
    ).getTime();
  }
  return NaN;
}

function formatRelativeTime(value, nowMs) {
  const ms = parseTimeMs(value);
  if (!Number.isFinite(ms)) {
    return '';
  }
  const now = Number.isFinite(nowMs) ? nowMs : Date.now();
  const diff = now - ms;
  const abs = Math.abs(diff);
  const future = diff < 0;
  const suffix = future ? '后' : '前';

  if (abs < 60 * 1000) {
    return future ? '即将' : '刚刚';
  }
  if (abs < 60 * 60 * 1000) {
    return Math.floor(abs / (60 * 1000)) + ' 分钟' + suffix;
  }
  if (abs < 24 * 60 * 60 * 1000) {
    return Math.floor(abs / (60 * 60 * 1000)) + ' 小时' + suffix;
  }
  if (abs < 7 * 24 * 60 * 60 * 1000) {
    return Math.floor(abs / (24 * 60 * 60 * 1000)) + ' 天' + suffix;
  }
  return '';
}

function startOfLocalDay(ms) {
  const d = new Date(ms);
  return new Date(d.getFullYear(), d.getMonth(), d.getDate()).getTime();
}

// 日期分组标题：今天 / 昨天 / M月D日 / YYYY年M月D日。
function dateGroupLabel(dateKey, nowMs) {
  const matched = /^(\d{4})-(\d{2})-(\d{2})$/.exec(dateKey || '');
  if (!matched) {
    return '';
  }
  const now = Number.isFinite(nowMs) ? nowMs : Date.now();
  const today = startOfLocalDay(now);
  const dayMs = 24 * 60 * 60 * 1000;
  const target = new Date(Number(matched[1]), Number(matched[2]) - 1, Number(matched[3])).getTime();

  if (target === today) {
    return '今天';
  }
  if (target === today - dayMs) {
    return '昨天';
  }
  if (Number(matched[1]) === new Date(today).getFullYear()) {
    return Number(matched[2]) + '月' + Number(matched[3]) + '日';
  }
  return matched[1] + '年' + Number(matched[2]) + '月' + Number(matched[3]) + '日';
}

// 本地时钟 HH:mm:ss，仅用于“更新于 …”这类界面提示。
function formatClock(ms) {
  const value = Number.isFinite(ms) ? ms : Date.now();
  const d = new Date(value);
  function pad(n) { return n < 10 ? '0' + n : String(n); }
  return pad(d.getHours()) + ':' + pad(d.getMinutes()) + ':' + pad(d.getSeconds());
}

module.exports = {
  isNaiveIso,
  isZonedIso,
  formatNaiveTime,
  formatZonedTime,
  displayRaw,
  dateKeyOf,
  parseTimeMs,
  formatRelativeTime,
  dateGroupLabel,
  formatClock
};
