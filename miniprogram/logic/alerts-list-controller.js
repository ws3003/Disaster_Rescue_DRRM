// 告警列表状态机（纯 JS，可独立测试）。
// 契约要求：
// - 客户端不合并分组、不全局重排序、不自行推导 has_more。
// - 切换类别时立即重置分页，旧请求不能覆盖新类别结果。
// - 加载中不重复追加同一请求；失败时保留已加载数据。

const { validateCategory } = require('../utils/validators');
const { normalizeError } = require('../utils/errors');
const { PAGINATION } = require('../utils/dto-schema');

function createAlertsListController(options) {
  const opts = options || {};
  const fetchAlerts = opts.fetchAlerts;
  const pageSize = opts.pageSize || PAGINATION.pageSizeDefault;
  const onStateChange = opts.onStateChange || function () {};

  const state = {
    category: '',
    items: [],
    page: 0,
    pageSize: pageSize,
    total: 0,
    hasMore: false,
    loading: false,
    error: null,
    initialized: false,
    lastFailedPage: null
  };

  let seq = 0;

  function snapshot() {
    return {
      category: state.category,
      items: state.items.slice(),
      page: state.page,
      pageSize: state.pageSize,
      total: state.total,
      hasMore: state.hasMore,
      loading: state.loading,
      error: state.error,
      initialized: state.initialized,
      lastFailedPage: state.lastFailedPage,
      isEmpty: state.initialized && !state.loading && !state.error && state.items.length === 0
    };
  }

  function emit() {
    onStateChange(snapshot());
  }

  function requestPage(page, mode) {
    const requestSeq = seq;
    state.loading = true;
    state.error = null;
    emit();

    return fetchAlerts({
      category: state.category === '' ? undefined : state.category,
      page: page,
      page_size: state.pageSize
    })
      .then(function (result) {
        if (requestSeq !== seq) {
          return;
        }
        // 追加/替换严格按服务端返回顺序，不排序、不去重、不合并分组。
        state.items = mode === 'append'
          ? state.items.concat(result.items)
          : result.items.slice();
        // 分页状态以服务端 meta 为准，不由本地推导。
        state.page = result.meta.page;
        state.total = result.meta.total;
        state.hasMore = result.meta.has_more;
        state.initialized = true;
        state.error = null;
        state.lastFailedPage = null;
      })
      .catch(function (err) {
        if (requestSeq !== seq) {
          return;
        }
        // 失败保留已有数据，只展示错误。
        state.error = normalizeError(err);
        state.initialized = true;
        state.lastFailedPage = page;
      })
      .then(function () {
        if (requestSeq !== seq) {
          return;
        }
        state.loading = false;
        emit();
      });
  }

  function setCategory(category) {
    const checked = validateCategory(category === undefined ? '' : category);
    if (!checked.ok) {
      seq += 1;
      state.category = '';
      state.items = [];
      state.page = 0;
      state.total = 0;
      state.hasMore = false;
      state.loading = false;
      state.initialized = true;
      state.lastFailedPage = null;
      state.error = checked.error;
      emit();
      return Promise.resolve();
    }

    const nextCategory = checked.value === undefined ? '' : checked.value;
    if (nextCategory === state.category) {
      // 同一类别：加载中忽略重复触发；已初始化且无错误时无需重复请求。
      if (state.loading) {
        return Promise.resolve();
      }
      if (state.initialized && !state.error) {
        return Promise.resolve();
      }
    }

    // 类别切换：立刻重置列表与分页，并使旧请求失效。
    seq += 1;
    state.category = nextCategory;
    state.items = [];
    state.page = 0;
    state.total = 0;
    state.hasMore = false;
    state.error = null;
    state.initialized = false;
    state.lastFailedPage = null;
    return requestPage(1, 'replace');
  }

  function refresh() {
    if (state.loading) {
      return Promise.resolve();
    }
    // 下拉刷新允许替换当前数据；失败时 requestPage 会保留旧数据。
    seq += 1;
    state.error = null;
    state.lastFailedPage = null;
    return requestPage(1, 'replace');
  }

  function loadMore() {
    if (state.loading || !state.initialized || !state.hasMore) {
      return Promise.resolve();
    }
    return requestPage(state.page + 1, 'append');
  }

  function retry() {
    if (state.loading) {
      return Promise.resolve();
    }
    if (state.lastFailedPage !== null) {
      const mode = state.lastFailedPage > 1 ? 'append' : 'replace';
      return requestPage(state.lastFailedPage, mode);
    }
    return refresh();
  }

  function getState() {
    return snapshot();
  }

  return {
    getState: getState,
    setCategory: setCategory,
    refresh: refresh,
    loadMore: loadMore,
    retry: retry
  };
}

module.exports = {
  createAlertsListController
};
