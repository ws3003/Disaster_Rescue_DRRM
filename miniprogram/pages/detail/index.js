const { createDefaultService } = require('../../services/index');
const { createDetailController } = require('../../logic/detail-controller');

Page({
  data: {
    mockMode: false,
    mockNotice: '',
    alertKey: '',
    loading: false,
    initialized: false,
    error: null,
    canRetry: false,
    detailView: null,
    relatedView: null,
    relatedLoading: false,
    relatedError: null
  },

  onLoad(query) {
    const app = getApp();
    const globalData = (app && app.globalData) || {};
    this.setData({
      mockMode: globalData.useMock === true,
      mockNotice: globalData.mockNotice || ''
    });

    const service = createDefaultService();
    this._controller = createDetailController({
      fetchDetail: function (alertKey) {
        return service.getAlertDetail(alertKey);
      },
      fetchRelated: function (alertKey) {
        return service.getAlertRelated(alertKey);
      },
      onStateChange: (state) => this.applyState(state)
    });

    let alertKey = '';
    if (query && query.alert_key) {
      alertKey = decodeURIComponent(query.alert_key);
    }
    this._controller.load(alertKey);
  },

  applyState(state) {
    this.setData({
      alertKey: state.alertKey,
      loading: state.loading,
      initialized: state.initialized,
      error: state.error,
      canRetry: !!state.error && state.error.kind !== 'LOCAL_INVALID_PARAMETER',
      detailView: state.detailView,
      relatedView: state.relatedView,
      relatedLoading: state.relatedLoading,
      relatedError: state.relatedError
    });
  },

  onPullDownRefresh() {
    if (!this._controller) {
      wx.stopPullDownRefresh();
      return;
    }
    this._controller.retry().then(function () {
      wx.stopPullDownRefresh();
    });
  },

  retry() {
    if (this._controller) {
      this._controller.retry();
    }
  },

  retryRelated() {
    if (this._controller) {
      this._controller.retry();
    }
  },

  copyAlertKey() {
    const key = this.data.alertKey;
    if (!key) {
      return;
    }
    wx.setClipboardData({
      data: key,
      success: function () {
        wx.showToast({ title: '已复制告警标识', icon: 'none' });
      }
    });
  },

  onShareAppMessage() {
    const view = this.data.detailView;
    const key = this.data.alertKey;
    return {
      title: view ? view.headline + ' · ' + view.categoryLabel : '灾害现场智能监测',
      path: '/pages/detail/index?alert_key=' + encodeURIComponent(key || '')
    };
  }
});
