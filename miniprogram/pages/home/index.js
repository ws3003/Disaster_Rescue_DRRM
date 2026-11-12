const { buildHomeFocus } = require('../../utils/home-focus');
const { createDefaultService } = require('../../services/index');
const { createHomeController } = require('../../logic/home-controller');
const { buildAlertListViews } = require('../../utils/view');
const { formatClock } = require('../../utils/time');
const net = require('../../utils/net');
const { buildCategoryChart, buildTemperatureChart } = require('../../utils/charts');

Page({
  data: {
    showSystemDetails: false,
    expandedAlerts: false,
    focusAlerts: [],
    remainingAlerts: [],
    mockMode: false,
    mockNotice: '',
    loading: false,
    updatedAt: '',
    offline: false,
    status: { loading: false, error: null, data: null, view: null },
    person: { loading: false, error: null, items: [], views: [], total: null },
    sensor: { loading: false, error: null, items: [], views: [], total: null },
    summary: { personText: '—', sensorText: '—' },
    categoryChart: buildCategoryChart({}, {}),
    temperatureChart: buildTemperatureChart({ loading: true }),
    sceneAlertKey: '',
    sceneMediaSupported: false,
    sceneMediaItems: [],
    sceneMediaLoading: false,
    sceneMediaError: null
  },

  toggleAlerts() {
    this.setData({ expandedAlerts: !this.data.expandedAlerts });
  },

  toggleSystemDetails() {
    this.setData({ showSystemDetails: !this.data.showSystemDetails });
  },

  onLoad() {
    const app = getApp();
    const globalData = (app && app.globalData) || {};
    this.setData({
      mockMode: globalData.useMock === true,
      mockNotice: globalData.mockNotice || ''
    });

    this._unsubscribeNet = net.subscribe((online) => {
      this.setData({ offline: !online });
    });

    const service = createDefaultService();
    this._service = service;
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
    const focus = buildHomeFocus(buildAlertListViews(state.person.items.concat(state.sensor.items), { nowMs: nowMs }));
    this.setData(
      {
        focusAlerts: focus.focus,
        remainingAlerts: focus.remaining,
        loading: state.loading,
        status: state.status,
        person: Object.assign({}, state.person, {
          views: buildAlertListViews(state.person.items, { nowMs: nowMs })
        }),
        sensor: Object.assign({}, state.sensor, {
          views: buildAlertListViews(state.sensor.items, { nowMs: nowMs })
        }),
        summary: state.summary,
        categoryChart: buildCategoryChart(state.person, state.sensor),
        temperatureChart: buildTemperatureChart(state.sensor),
        updatedAt: state.loading ? this.data.updatedAt : formatClock(Date.now())
      },
      () => this.animateSummary(state.summary)
    );
    this.loadLatestPersonImage(state.person.items);
  },

  // 首页只展示最新人员告警已核实的现场图片；不把本机预览伪装为云端证据。
  loadLatestPersonImage(items) {
    const latest = Array.isArray(items) ? items[0] : null;
    const alertKey = latest && latest.alert_key ? latest.alert_key : '';
    if (!alertKey || !this._service || this._service.useMock) {
      this.setData({
        sceneAlertKey: alertKey,
        sceneMediaSupported: false,
        sceneMediaItems: [],
        sceneMediaLoading: false,
        sceneMediaError: null
      });
      return;
    }
    if (alertKey === this._sceneLoadedAlertKey) return;
    this._sceneLoadedAlertKey = alertKey;
    const requestId = (this._sceneRequestId || 0) + 1;
    this._sceneRequestId = requestId;
    this.setData({
      sceneAlertKey: alertKey,
      sceneMediaSupported: true,
      sceneMediaItems: [],
      sceneMediaLoading: true,
      sceneMediaError: null
    });
    this._service.listAlertMedia(alertKey, { page: 1, page_size: 20 }).then((result) => {
      if (requestId !== this._sceneRequestId) return;
      const images = (result.items || []).filter((item) => item && item.media_type === 'image');
      this.setData({ sceneMediaItems: images, sceneMediaLoading: false, sceneMediaError: null });
    }).catch((error) => {
      if (requestId !== this._sceneRequestId) return;
      this.setData({
        sceneMediaItems: [],
        sceneMediaLoading: false,
        sceneMediaError: { message: error && error.message ? error.message : '现场图片暂不可用' }
      });
    });
  },

  onRequestMediaAccess(event) {
    const detail = event && event.detail ? event.detail : {};
    if (!this._service || !detail.mediaId) return;
    wx.showLoading({ title: '获取图片中' });
    this._service.getMediaAccess(detail.mediaId).then((access) => {
      wx.previewImage({ current: access.access_url, urls: [access.access_url] });
    }).catch(function (error) {
      wx.showToast({ title: error && error.message ? error.message : '图片暂不可用', icon: 'none' });
    }).then(function () {
      wx.hideLoading();
    });
  },

  // 纯展示动效：数字从当前值缓动到新值，不改变任何数据来源。
  animateSummary(summary) {
    const targets = [
      { key: 'personText', value: summary.personTotal },
      { key: 'sensorText', value: summary.sensorTotal }
    ].filter(function (target) {
      return typeof target.value === 'number' && Number.isFinite(target.value);
    });
    if (!targets.length) {
      return;
    }

    const self = this;
    if (this._summaryTimer) {
      clearTimeout(this._summaryTimer);
      this._summaryTimer = null;
    }

    const from = targets.map(function (target) {
      const parsed = parseInt(self.data.summary[target.key], 10);
      return Number.isFinite(parsed) ? parsed : 0;
    });
    const unchanged = targets.every(function (target, index) {
      return target.value === from[index];
    });
    if (unchanged) {
      return;
    }

    const start = Date.now();
    const duration = 720;

    function tick() {
      const progress = Math.min(1, (Date.now() - start) / duration);
      const eased = 1 - Math.pow(1 - progress, 3);
      const patch = {};
      targets.forEach(function (target, index) {
        patch[target.key] = String(Math.round(from[index] + (target.value - from[index]) * eased));
      });
      self.setData({ summary: Object.assign({}, self.data.summary, patch) });
      if (progress < 1) {
        self._summaryTimer = setTimeout(tick, 40);
      } else {
        self._summaryTimer = null;
      }
    }

    tick();
  },

  onUnload() {
    if (this._unsubscribeNet) {
      this._unsubscribeNet();
      this._unsubscribeNet = null;
    }
    if (this._summaryTimer) {
      clearTimeout(this._summaryTimer);
      this._summaryTimer = null;
    }
  },

  refreshAll() {
    if (this._controller) {
      this._sceneLoadedAlertKey = '';
      this._controller.load();
    }
  },

  onPullDownRefresh() {
    if (!this._controller) {
      wx.stopPullDownRefresh();
      return;
    }
    this._sceneLoadedAlertKey = '';
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

  onChartCategoryTap(event) {
    if (event.detail.category === 'person') this.goAlertsPerson();
    else if (event.detail.category === 'sensor_environment') this.goAlertsSensor();
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
