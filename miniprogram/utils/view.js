// 视图模型转换。页面 WXML 只消费这里生成的展示字段，
// 所有展示值都来自契约字段，缺失/可空字段统一显示为 “—”，不伪造默认值。

const { formatNaiveTime, formatZonedTime, displayRaw } = require('./time');

function dash(value) {
  if (value === null || value === undefined || value === '') {
    return '—';
  }
  return String(value);
}

function withUnit(value, unit) {
  if (value === null || value === undefined) {
    return '—';
  }
  return String(value) + ' ' + unit;
}

function categoryLabel(category) {
  if (category === 'person') {
    return '人员告警';
  }
  if (category === 'sensor_environment') {
    return '传感器环境告警';
  }
  return dash(category);
}

function categoryClass(category) {
  if (category === 'person') {
    return 'tag-person';
  }
  if (category === 'sensor_environment') {
    return 'tag-sensor';
  }
  return '';
}

function statusLabel(status) {
  if (status === 'active') {
    return '活动（active）';
  }
  if (status === 'resolved') {
    return '已解除（resolved）';
  }
  return dash(status);
}

function statusClass(status) {
  if (status === 'active') {
    return 'tag-active';
  }
  if (status === 'resolved') {
    return 'tag-resolved';
  }
  return '';
}

function buildAlertItemView(dto) {
  const isPerson = dto.category === 'person';
  const chips = [];

  if (isPerson) {
    if (dto.state !== null && dto.state !== undefined) {
      chips.push({ label: '状态', value: String(dto.state) });
    }
    if (dto.priority !== null && dto.priority !== undefined) {
      chips.push({ label: '优先级', value: String(dto.priority) });
    }
    if (dto.risk_score !== null && dto.risk_score !== undefined) {
      chips.push({ label: '风险分', value: String(dto.risk_score) });
    }
    if (dto.person_id !== null && dto.person_id !== undefined) {
      chips.push({ label: '人员 ID', value: String(dto.person_id) });
    }
  } else {
    if (dto.status !== null && dto.status !== undefined) {
      chips.push({ label: '状态', value: statusLabel(dto.status) });
    }
    if (dto.temperature !== null && dto.temperature !== undefined) {
      chips.push({ label: '温度', value: withUnit(dto.temperature, '℃') });
    }
    if (dto.humidity !== null && dto.humidity !== undefined) {
      chips.push({ label: '湿度', value: withUnit(dto.humidity, '%RH') });
    }
    if (dto.threshold !== null && dto.threshold !== undefined) {
      chips.push({ label: '阈值', value: withUnit(dto.threshold, '℃') });
    }
  }

  return {
    alertKey: dto.alert_key,
    category: dto.category,
    categoryLabel: categoryLabel(dto.category),
    categoryClass: categoryClass(dto.category),
    title: isPerson
      ? dash(dto.title || dto.type || '人员高风险告警')
      : dash(dto.type || '传感器环境告警'),
    timeLabel: isPerson ? '事件时间' : '发生时间',
    timeText: isPerson ? formatNaiveTime(dto.ts) : formatZonedTime(dto.occurred_at),
    severityText: dash(dto.severity),
    statusText: isPerson ? '' : statusLabel(dto.status),
    statusClass: isPerson ? '' : statusClass(dto.status),
    chips
  };
}

function buildAlertListViews(items) {
  return items.map(buildAlertItemView);
}

function buildPersonDetailFields(dto) {
  return [
    { label: '告警键', value: dash(dto.alert_key), mono: true },
    { label: '来源', value: dash(dto.source) },
    { label: '分类', value: dash(dto.category) },
    { label: '类型', value: dash(dto.type) },
    { label: '严重程度', value: dash(dto.severity) },
    { label: '事件时间（本地无时区）', value: formatNaiveTime(dto.ts) },
    { label: '人员 ID', value: dash(dto.person_id) },
    { label: '标题', value: dash(dto.title) },
    { label: '人员状态', value: dash(dto.state) },
    { label: '优先级', value: dash(dto.priority) },
    { label: '风险分', value: dash(dto.risk_score) }
  ];
}

function buildSensorDetailFields(dto) {
  return [
    { label: '告警键', value: dash(dto.alert_key), mono: true },
    { label: '来源', value: dash(dto.source) },
    { label: '分类', value: dash(dto.category) },
    { label: '类型', value: dash(dto.type) },
    { label: '严重程度', value: dash(dto.severity) },
    { label: '状态', value: statusLabel(dto.status) },
    { label: '温度（触发快照）', value: withUnit(dto.temperature, '℃') },
    { label: '湿度（触发快照）', value: withUnit(dto.humidity, '%RH') },
    { label: '阈值（触发快照）', value: withUnit(dto.threshold, '℃') },
    { label: '发生时间（含时区）', value: formatZonedTime(dto.occurred_at) },
    { label: '解除时间', value: displayRaw(dto.resolved_at) },
    { label: '设备采集时间', value: displayRaw(dto.device_captured_at) },
    { label: '关联读数键', value: dash(dto.reading_id), mono: true },
    { label: '服务端接收时间', value: displayRaw(dto.received_at) }
  ];
}

function buildDetailView(dto) {
  const isPerson = dto.category === 'person';
  return {
    alertKey: dto.alert_key,
    category: dto.category,
    categoryLabel: categoryLabel(dto.category),
    categoryClass: categoryClass(dto.category),
    headline: isPerson
      ? dash(dto.title || dto.type || '人员高风险告警')
      : dash(dto.type || '传感器环境告警'),
    statusText: isPerson ? '' : statusLabel(dto.status),
    statusClass: isPerson ? '' : statusClass(dto.status),
    fields: isPerson ? buildPersonDetailFields(dto) : buildSensorDetailFields(dto)
  };
}

function buildReadingView(reading) {
  return {
    readingId: reading.reading_id,
    fields: [
      { label: '读数 ID', value: dash(reading.reading_id), mono: true },
      { label: '传感器 ID', value: dash(reading.sensor_id) },
      { label: '温度', value: withUnit(reading.temperature, '℃') },
      { label: '湿度', value: withUnit(reading.humidity, '%RH') },
      { label: '设备采集时间（含时区）', value: formatZonedTime(reading.device_captured_at) },
      { label: '边缘接收时间', value: displayRaw(reading.edge_received_at) },
      { label: '服务端接收时间', value: displayRaw(reading.received_at) }
    ]
  };
}

function buildRelatedView(related) {
  const readings = (related.readings || []).map(buildReadingView);
  let mediaText;
  if (related.media_supported === false) {
    mediaText = '当前契约尚未支持媒体关联（media_supported=false），不展示任何媒体 URL。';
  } else if (!related.media || related.media.length === 0) {
    mediaText = '暂无可展示的关联媒体。';
  } else {
    // 当前契约未定义媒体项字段，禁止推测或拼接媒体 URL。
    mediaText = '服务端返回了媒体记录，但当前契约未定义媒体项字段，客户端不展示媒体细节。';
  }
  return {
    readings,
    hasReadings: readings.length > 0,
    readingsEmptyText: '该告警没有契约允许的关联环境读数。',
    mediaSupported: related.media_supported,
    mediaText
  };
}

function buildSystemStatusView(data) {
  const modules = [];
  const moduleKeys = Object.keys(data.modules || {});
  for (let i = 0; i < moduleKeys.length; i += 1) {
    const key = moduleKeys[i];
    const value = data.modules[key];
    modules.push({
      name: key,
      value: typeof value === 'object' && value !== null
        ? JSON.stringify(value)
        : String(value)
    });
  }
  const status = data.device.status;
  return {
    deviceStatus: status,
    deviceStatusText: status === 'unknown'
      ? '未知（契约占位值，不代表真实设备健康状态）'
      : String(status),
    modules,
    modulesEmpty: modules.length === 0,
    modulesNote: '契约中 modules 为占位对象，未定义模块字段；仅展示服务端实际返回的键值。'
  };
}

function buildSummaryView(personTotal, sensorTotal) {
  return {
    personTotal: personTotal === null || personTotal === undefined ? null : personTotal,
    sensorTotal: sensorTotal === null || sensorTotal === undefined ? null : sensorTotal,
    personText: personTotal === null || personTotal === undefined ? '—' : String(personTotal),
    sensorText: sensorTotal === null || sensorTotal === undefined ? '—' : String(sensorTotal)
  };
}

module.exports = {
  dash,
  categoryLabel,
  categoryClass,
  statusLabel,
  statusClass,
  buildAlertItemView,
  buildAlertListViews,
  buildDetailView,
  buildReadingView,
  buildRelatedView,
  buildSystemStatusView,
  buildSummaryView
};
