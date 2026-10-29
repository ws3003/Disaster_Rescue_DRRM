const { createDefaultService } = require('../../services/index');
const { createHomeController } = require('../../logic/home-controller');
const { buildAlertListViews } = require('../../utils/view');

Page({
  data: {
    mockMode: false,
    mockNotice: '',
    loading: false,
    status: { loading: false, error: null, data: null, view: null },
    person: { loading: false, error: null, items: [], views: [], total: null },
    sensor: { loading: false, error: null, items: [], views: [], total: null },
    summary: { personText: '—', sensorText: '—' }
  },

  onLoad() {
    const app = getApp();
    const globalData = (app && app.globalData) || {};
    this.setData({
      mockMode: globalData.useMock === true,
      mockNotice: globalData.mockNotice || ''
    });

    const service = createDefaultService();
    this._controller = createHomeController({
      fetchStatus: function () {
        return service.getSystemStatus();
      },
      fetchAlerts: function (params) {
        return service.listAlerts(params);
      },
      mockMode: service.useMock,
      onStateChange: (state) => this.applyState(state)
    });

    this._controller.load();
  },

  applyState(state) {
    // 相对时间只是展示层派生值，每次渲染取当前时间即可。
    const nowMs = Date.now();
    this.setData({
      loading: state.loading,
      status: state.status,
      person: Object.assign({}, state.person, {
        views: buildAlertListViews(state.person.items, { nowMs: nowMs })
      }),
      sensor: Object.assign({}, state.sensor, {
        views: buildAlertListViews(state.sensor.items, { nowMs: nowMs })
      }),
      summary: state.summary
    });
  },

  onPullDownRefresh() {
    if (!this._controller) {
      wx.stopPullDownRefresh();
      return;
    }
    this._controller.load().then(function () {
      wx.stopPullDownRefresh();
    });
  },

  retryStatus() {
    if (this._controller) {
      this._controller.retryStatus();
    }
  },

  retryRecent() {
    if (this._controller) {
      this._controller.retryRecent();
    }
  },

  goAlerts() {
    wx.navigateTo({ url: '/pages/alerts/index' });
  },

  goAlertsPerson() {
    wx.navigateTo({ url: '/pages/alerts/index?category=person' });
  },

  goAlertsSensor() {
    wx.navigateTo({ url: '/pages/alerts/index?category=sensor_environment' });
  },

  onItemTap(event) {
    const alertKey = event.currentTarget.dataset.key;
    if (!alertKey) {
      return;
    }
    wx.navigateTo({
      url: '/pages/detail/index?alert_key=' + encodeURIComponent(alertKey)
    });
  },

  onShareAppMessage() {
    return {
      title: '灾害现场智能监测 · 现场态势',
      path: '/pages/home/index'
    };
  }
});
