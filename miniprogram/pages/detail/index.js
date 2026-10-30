const { createDefaultService } = require('../../services/index');
const { createDetailController } = require('../../logic/detail-controller');
const store = require('../../utils/local-store');
const navContext = require('../../utils/nav-context');
const net = require('../../utils/net');

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
    relatedError: null,
    followed: false,
    offline: false,
    nav: {
      inContext: false,
      position: 0,
      total: 0,
      hasPrev: false,
      hasNext: false,
      prevKey: '',
      nextKey: ''
    }
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

    this._unsubscribeNet = net.subscribe((online) => {
      this.setData({ offline: !online });
    });

    let alertKey = '';
    if (query && query.alert_key) {
      alertKey = decodeURIComponent(query.alert_key);
    }
    this.setData({
      followed: store.isFollowed(alertKey),
      nav: navContext.getNeighbors(alertKey)
    });
    this._controller.load(alertKey);
  },

  onUnload() {
    if (this._unsubscribeNet) {
      this._unsubscribeNet();
      this._unsubscribeNet = null;
    }
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

  toggleFollow() {
    const key = this.data.alertKey;
    if (!key) {
      return;
    }
    const result = store.toggleFollow(key);
    this.setData({ followed: result.followed });
    wx.showToast({
      title: result.followed ? '已加入本地关注' : '已取消关注',
      icon: 'none'
    });
  },

  goPrev() {
    if (this.data.nav.hasPrev) {
      wx.redirectTo({
        url: '/pages/detail/index?alert_key=' + encodeURIComponent(this.data.nav.prevKey)
      });
    }
  },

  goNext() {
    if (this.data.nav.hasNext) {
      wx.redirectTo({
        url: '/pages/detail/index?alert_key=' + encodeURIComponent(this.data.nav.nextKey)
      });
    }
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
