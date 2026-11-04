// 首页图表只读现有 S4-02 结果，不补采样、不补历史、不改 DTO 或服务端顺序。
const { displayRaw } = require('./time');

function count(value) {
  return Number.isInteger(value) && value >= 0 ? value : null;
}

function countCeiling(value) {
  if (value <= 0) return 2;
  const target = value * 1.15;
  const power = Math.pow(10, Math.floor(Math.log10(target)));
  const step = [2, 4, 6, 10].find(function (n) { return n * power >= target; });
  return Math.max(2, step * power);
}

function buildCategoryChart(person, sensor) {
  const groups = [
    { category: 'person', label: '人员告警', color: 'teal', section: person || {} },
    { category: 'sensor_environment', label: '传感器告警', color: 'blue', section: sensor || {} }
  ];
  const values = groups.map(function (group) {
    // 刷新失败时不把上一次结果当成当前统计，也不把错误填成零。
    return group.section.error ? null : count(group.section.total);
  });
  const ceiling = countCeiling(Math.max.apply(null, values.map(function (v) { return v === null ? 0 : v; })));
  return {
    loading: groups.some(function (group) { return group.section.loading; }),
    partial: values.some(function (value) { return value === null; }),
    hasValues: values.some(function (value) { return value !== null; }),
    ticks: [ceiling, ceiling / 2, 0],
    bars: groups.map(function (group, index) {
      const value = values[index];
      return {
        category: group.category, label: group.label, color: group.color,
        value: value, valueText: value === null ? '—' : String(value),
        heightPct: value === null ? 0 : value / ceiling * 100,
        missing: value === null
      };
    })
  };
}

function buildTemperatureChart(section) {
  const source = section || {};
  if (source.error) return { state: 'error', hasValues: false, points: [], message: '温度快照获取失败，请重试最近告警。' };
  const items = Array.isArray(source.items) ? source.items.slice(0, 3) : [];
  if (!items.length) return {
    state: source.loading ? 'loading' : 'empty', hasValues: false, points: [],
    message: source.loading ? '正在加载最近告警…' : '暂无传感器告警，尚无温度快照。'
  };
  // 仅反转传感器组副本，让最新项位于右端；不比较人员时间、不修改列表。
  const points = items.reverse().map(function (item, index) {
    const valid = typeof item.temperature === 'number' && Number.isFinite(item.temperature);
    return {
      key: item.alert_key, ordinal: index + 1, value: valid ? item.temperature : null,
      valueText: valid ? String(item.temperature) + ' ℃' : '未提供温度',
      timeText: displayRaw(item.occurred_at),
      shortTime: typeof item.occurred_at === 'string' ? item.occurred_at.slice(11, 16) : '—',
      latest: index === items.length - 1
    };
  });
  const available = points.filter(function (point) { return point.value !== null; });
  return {
    state: available.length ? 'ready' : 'missing', hasValues: available.length > 0,
    points: points, sampleCount: points.length, availableCount: available.length,
    message: !available.length ? '这些告警未提供温度，无法绘制折线。' :
      available.length === 1 ? '仅有一条有效温度，仅显示数据点。' :
      '按告警先后排列；缺失温度留空，不补点、不跨缺口连线。'
  };
}

module.exports = { buildCategoryChart, buildTemperatureChart };
