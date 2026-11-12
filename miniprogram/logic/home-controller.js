// 首页状态机（纯 JS，可独立测试）。
// 首页数据来源（严格按共享契约）：
// - 系统状态：S4-01。
// - 最近告警 + 高风险摘要：S4-02 按 category 分别请求人员组与传感器组，
//   两组分别按服务端顺序展示，不做跨组全局时间排序。
//   摘要仅使用 meta.total 做分类计数，不推导契约未定义的统计字段。

const { normalizeError } = require('../utils/errors');
const { buildSystemStatusView, buildSummaryView } = require('../utils/view');

function createHomeController(options) {
  const opts = options || {};
  const fetchStatus = opts.fetchStatus;
  const fetchAlerts = opts.fetchAlerts;
  const recentPageSize = opts.recentPageSize || 3;
  const onStateChange = opts.onStateChange || function () {};

  const state = {
    mockMode: opts.mockMode === true,
    status: { loading: false, error: null, data: null, view: null },
    person: { loading: false, error: null, items: [], total: null },
    sensor: { loading: false, error: null, items: [], total: null },
    summary: buildSummaryView(null, null)
  };

  let statusSeq = 0;
  let personSeq = 0;
  let sensorSeq = 0;

  function snapshot() {
    return {
      mockMode: state.mockMode,
      status: {
        loading: state.status.loading,
        error: state.status.error,
        data: state.status.data,
        view: state.status.view
      },
      person: {
        loading: state.person.loading,
        error: state.person.error,
        items: state.person.items.slice(),
        total: state.person.total
      },
      sensor: {
        loading: state.sensor.loading,
        error: state.sensor.error,
        items: state.sensor.items.slice(),
        total: state.sensor.total
      },
      summary: state.summary,
      loading:
        state.status.loading || state.person.loading || state.sensor.loading
    };
  }

  function emit() {
    onStateChange(snapshot());
  }

  function refreshSummary() {
    state.summary = buildSummaryView(state.person.total, state.sensor.total);
  }

  function loadStatus() {
    const current = ++statusSeq;
    state.status.loading = true;
    state.status.error = null;
    emit();

    return fetchStatus()
      .then(function (data) {
        if (current !== statusSeq) {
          return;
        }
        state.status.data = data;
        state.status.view = buildSystemStatusView(data);
      })
      .catch(function (err) {
        if (current !== statusSeq) {
          return;
        }
        state.status.error = normalizeError(err);
      })
      .then(function () {
        if (current !== statusSeq) {
          return;
        }
        state.status.loading = false;
        emit();
      });
  }

  function loadRecent(which, category) {
    const isPerson = which === 'person';
    const current = isPerson ? ++personSeq : ++sensorSeq;
    const section = isPerson ? state.person : state.sensor;
    section.loading = true;
    section.error = null;
    emit();

    return fetchAlerts({
      category: category,
      page: 1,
      page_size: recentPageSize
    })
      .then(function (result) {
        if (current !== (isPerson ? personSeq : sensorSeq)) {
          return;
        }
        section.items = result.items.slice();
        section.total = result.meta.total;
        refreshSummary();
      })
      .catch(function (err) {
        if (current !== (isPerson ? personSeq : sensorSeq)) {
          return;
        }
        section.error = normalizeError(err);
      })
      .then(function () {
        if (current !== (isPerson ? personSeq : sensorSeq)) {
          return;
        }
        section.loading = false;
        emit();
      });
  }

  function load() {
    if (state.status.loading || state.person.loading || state.sensor.loading) {
      return Promise.resolve();
    }
    return Promise.all([
      loadStatus(),
      loadRecent('person', 'person'),
      loadRecent('sensor', 'sensor_environment')
    ]);
  }

  function retryStatus() {
    if (state.status.loading) {
      return Promise.resolve();
    }
    return loadStatus();
  }

  function retryRecent() {
    if (state.person.loading || state.sensor.loading) {
      return Promise.resolve();
    }
    return Promise.all([
      loadRecent('person', 'person'),
      loadRecent('sensor', 'sensor_environment')
    ]);
  }

  return {
    getState: snapshot,
    load: load,
    retryStatus: retryStatus,
    retryRecent: retryRecent
  };
}

module.exports = {
  createHomeController
};
