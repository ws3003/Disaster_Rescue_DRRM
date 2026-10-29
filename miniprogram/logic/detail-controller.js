// 告警详情状态机（纯 JS，可独立测试）。
// 详情与关联数据分别请求 S4-03 / S4-04；
// 详情失败时不用任何 Mock 数据冒充，关联数据为空时不虚构读数或媒体。

const { validateAlertKey } = require('../utils/validators');
const { normalizeError } = require('../utils/errors');
const { buildDetailView, buildRelatedView } = require('../utils/view');

function settle(promise) {
  return promise.then(
    function (value) {
      return { ok: true, value: value };
    },
    function (error) {
      return { ok: false, error: error };
    }
  );
}

function createDetailController(options) {
  const opts = options || {};
  const fetchDetail = opts.fetchDetail;
  const fetchRelated = opts.fetchRelated;
  const onStateChange = opts.onStateChange || function () {};

  const state = {
    alertKey: '',
    loading: false,
    initialized: false,
    error: null,
    detail: null,
    detailView: null,
    related: null,
    relatedView: null,
    relatedLoading: false,
    relatedError: null
  };

  let seq = 0;

  function snapshot() {
    return {
      alertKey: state.alertKey,
      loading: state.loading,
      initialized: state.initialized,
      error: state.error,
      detail: state.detail,
      detailView: state.detailView,
      related: state.related,
      relatedView: state.relatedView,
      relatedLoading: state.relatedLoading,
      relatedError: state.relatedError
    };
  }

  function emit() {
    onStateChange(snapshot());
  }

  function load(alertKey) {
    const checked = validateAlertKey(alertKey);
    seq += 1;
    const current = seq;

    state.alertKey = alertKey === undefined || alertKey === null ? '' : String(alertKey);
    state.loading = false;
    state.relatedLoading = false;
    state.detail = null;
    state.detailView = null;
    state.related = null;
    state.relatedView = null;
    state.relatedError = null;
    state.error = null;
    state.initialized = true;

    if (!checked.ok) {
      // 本地格式校验失败：不发起任何请求。
      state.error = checked.error;
      emit();
      return Promise.resolve();
    }

    state.loading = true;
    state.relatedLoading = true;
    emit();

    const detailPromise = settle(fetchDetail(checked.value));
    const relatedPromise = settle(fetchRelated(checked.value));

    return Promise.all([detailPromise, relatedPromise]).then(function (results) {
      if (current !== seq) {
        return;
      }
      const detailResult = results[0];
      const relatedResult = results[1];

      if (detailResult.ok) {
        state.detail = detailResult.value;
        state.detailView = buildDetailView(detailResult.value);
        state.error = null;
      } else {
        state.error = normalizeError(detailResult.error);
      }

      if (relatedResult.ok) {
        state.related = relatedResult.value;
        state.relatedView = buildRelatedView(relatedResult.value);
        state.relatedError = null;
      } else {
        state.relatedError = normalizeError(relatedResult.error);
      }

      state.loading = false;
      state.relatedLoading = false;
      emit();
    });
  }

  function retry() {
    if (state.loading) {
      return Promise.resolve();
    }
    return load(state.alertKey);
  }

  return {
    getState: snapshot,
    load: load,
    retry: retry
  };
}

module.exports = {
  createDetailController
};
