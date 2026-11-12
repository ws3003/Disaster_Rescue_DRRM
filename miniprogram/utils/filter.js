// 本地筛选工具。契约没有 search/severity/status 查询参数，因此这里只对“已加载”的视图数据做过滤，
// 不发起额外请求、不伪造服务端搜索能力，也不改变服务端分页与排序结果。

function normalizeKeyword(keyword) {
  if (keyword === null || keyword === undefined) {
    return '';
  }
  return String(keyword).trim().toLowerCase();
}

function normalizeChoice(value) {
  if (value === null || value === undefined) {
    return '';
  }
  return String(value).trim().toLowerCase();
}

function haystackOf(view) {
  const parts = [
    view.alertKey,
    view.title,
    view.categoryLabel,
    view.severityText,
    view.statusText,
    view.timeText
  ];
  (view.chips || []).forEach(function (chip) {
    parts.push(chip.label);
    parts.push(chip.value);
  });
  return parts
    .filter(function (part) { return part !== null && part !== undefined; })
    .join(' ')
    .toLowerCase();
}

// options.severity: '' | 'high' | 'medium' | 'low'（对应契约 severity 原值的小写）
// options.status:   '' | 'active' | 'resolved'（只有传感器告警有该字段）
// options.followedOnly + options.followedKeys：本地关注列表过滤（纯客户端状态）
function filterAlertViews(views, keyword, options) {
  const list = views || [];
  const opts = options || {};
  const normalized = normalizeKeyword(keyword);
  const severity = normalizeChoice(opts.severity);
  const status = normalizeChoice(opts.status);
  const followedOnly = opts.followedOnly === true;
  const followedKeys = Array.isArray(opts.followedKeys) ? opts.followedKeys : [];

  const filtering = !!normalized || !!severity || !!status || followedOnly;
  if (!filtering) {
    return {
      views: list.slice(),
      keyword: '',
      severity: '',
      status: '',
      followedOnly: false,
      matched: list.length,
      total: list.length,
      active: false
    };
  }

  const matched = list.filter(function (view) {
    if (normalized && haystackOf(view).indexOf(normalized) === -1) {
      return false;
    }
    if (severity && normalizeChoice(view.severityKey) !== severity) {
      return false;
    }
    if (status && normalizeChoice(view.statusKey) !== status) {
      return false;
    }
    if (followedOnly && followedKeys.indexOf(view.alertKey) === -1) {
      return false;
    }
    return true;
  });

  return {
    views: matched,
    keyword: normalized,
    severity: severity,
    status: status,
    followedOnly: followedOnly,
    matched: matched.length,
    total: list.length,
    active: true
  };
}

module.exports = {
  normalizeKeyword,
  filterAlertViews
};
