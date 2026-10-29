const { createDefaultService } = require('../../services/index');
const { createAlertsListController } = require('../../logic/alerts-list-controller');
const { buildAlertListViews } = require('../../utils/view');
const { CATEGORIES } = require('../../utils/dto-schema');

Page({
  data: {
    mockMode: false,
    mockNotice: '',
    tabs: [
      { value: '', label: '全部' },
      { value: 'person', label: '人员告警' },
      { value: 'sensor_environment', label: '传感器环境' }
    ],
    category: '',
    views: [],
    total: 0,
    hasMore: false,
    loading: false,
    error: null,
    initialized: false,
    isEmpty: false
  },

  onLoad(query) {
    const app = getApp();
    const globalData = (app && app.globalData) || {};
    this.setData({
      mockMode: globalData.useMock === true,
      mockNotice: globalData.mockNotice || ''
    });

    let initialCategory = '';
    if (query && query.category) {
      const decoded = decodeURIComponent(query.category);
      if (CATEGORIES.indexOf(decoded) !== -1) {
        initialCategory = decoded;
      }
    }
    this.setData({ category: initialCategory });

    const service = createDefaultService();
    this._controller = createAlertsListController({
      fetchAlerts: function (params) {
        return service.listAlerts(params);
      },
      onStateChange: (state) => this.applyState(state)
    });

    this._controller.setCategory(initialCategory);
  },

  applyState(state) {
    this.setData({
      category: state.category,
      views: buildAlertListViews(state.items),
      total: state.total,
      hasMore: state.hasMore,
      loading: state.loading,
      error: state.error,
      initialized: state.initialized,
      isEmpty: state.isEmpty
    });
  },

  onTabTap(event) {
    const value = event.currentTarget.dataset.value;
    if (value === this.data.category || !this._controller) {
      return;
    }
    // 切换类别：控制器会重置分页并让旧请求失效。
    this._controller.setCategory(value);
  },

  onReachBottom() {
    if (this._controller) {
      this._controller.loadMore();
    }
  },

  onPullDownRefresh() {
    if (!this._controller) {
      wx.stopPullDownRefresh();
      return;
    }
    this._controller.refresh().then(function () {
      wx.stopPullDownRefresh();
    });
  },

  retry() {
    if (this._controller) {
      this._controller.retry();
    }
  },

  onItemTap(event) {
    const alertKey = event.currentTarget.dataset.key;
    if (!alertKey) {
      return;
    }
    wx.navigateTo({
      url: '/pages/detail/index?alert_key=' + encodeURIComponent(alertKey)
    });
  }
});
