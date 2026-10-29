const { createDefaultService } = require('../../services/index');
const { createAlertsListController } = require('../../logic/alerts-list-controller');
const { buildAlertListViews, decorateListGroups } = require('../../utils/view');
const { filterAlertViews } = require('../../utils/filter');
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
    isEmpty: false,
    // 本地筛选（仅作用于已加载数据，不发起服务端搜索）
    keyword: '',
    severityFilter: '',
    statusFilter: '',
    severityOptions: [
      { value: '', label: '全部' },
      { value: 'high', label: '高' },
      { value: 'medium', label: '中' },
      { value: 'low', label: '低' }
    ],
    statusOptions: [
      { value: '', label: '全部' },
      { value: 'active', label: '活动' },
      { value: 'resolved', label: '已解除' }
    ],
    showStatusFilter: false,
    loadedCount: 0,
    filteredCount: 0,
    highCount: 0,
    filtering: false,
    filterEmpty: false,
    showBackTop: false
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
    this.setData({
      category: initialCategory,
      showStatusFilter: initialCategory === 'sensor_environment'
    });

    this._rawViews = [];
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
    const nowMs = Date.now();
    this._rawViews = buildAlertListViews(state.items, { nowMs: nowMs });
    this.setData({
      category: state.category,
      showStatusFilter: state.category === 'sensor_environment',
      total: state.total,
      hasMore: state.hasMore,
      loading: state.loading,
      error: state.error,
      initialized: state.initialized,
      isEmpty: state.isEmpty
    });
    this.renderViews();
  },

  // 先按关键词/严重程度/状态做本地过滤，再按相邻日期与类别插入分组标题。
  // 顺序仍是服务端返回的顺序，不重排、不合并。
  renderViews() {
    const nowMs = Date.now();
    const filtered = filterAlertViews(this._rawViews, this.data.keyword, {
      severity: this.data.severityFilter,
      status: this.data.statusFilter
    });
    const decorated = decorateListGroups(filtered.views, {
      nowMs: nowMs,
      showCategoryHeader: this.data.category === ''
    });

    let highCount = 0;
    this._rawViews.forEach(function (view) {
      if (String(view.severityKey) === 'high') {
        highCount += 1;
      }
    });

    this.setData({
      views: decorated,
      loadedCount: filtered.total,
      filteredCount: filtered.matched,
      highCount: highCount,
      filtering: filtered.active,
      filterEmpty: filtered.active && filtered.matched === 0
    });
  },

  onKeywordInput(event) {
    this.setData({ keyword: event.detail.value || '' });
    this.renderViews();
  },

  onClearKeyword() {
    this.setData({ keyword: '' });
    this.renderViews();
  },

  onSeverityTap(event) {
    const value = event.currentTarget.dataset.value || '';
    if (value === this.data.severityFilter) {
      return;
    }
    this.setData({ severityFilter: value });
    this.renderViews();
  },

  onStatusTap(event) {
    const value = event.currentTarget.dataset.value || '';
    if (value === this.data.statusFilter) {
      return;
    }
    this.setData({ statusFilter: value });
    this.renderViews();
  },

  onResetFilters() {
    this.setData({ keyword: '', severityFilter: '', statusFilter: '' });
    this.renderViews();
  },

  onTabTap(event) {
    const value = event.currentTarget.dataset.value;
    if (value === this.data.category || !this._controller) {
      return;
    }
    // 切换类别时清空本地筛选，避免旧筛选造成“看起来没数据”的误解。
    this.setData({ keyword: '', severityFilter: '', statusFilter: '' });
    // 控制器会重置分页并让旧请求失效。
    this._controller.setCategory(value);
  },

  onReachBottom() {
    if (this._controller) {
      this._controller.loadMore();
    }
  },

  onPageScroll(event) {
    const show = event.scrollTop > 320;
    if (show !== this.data.showBackTop) {
      this.setData({ showBackTop: show });
    }
  },

  backToTop() {
    wx.pageScrollTo({ scrollTop: 0, duration: 260 });
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
  },

  onShareAppMessage() {
    return {
      title: '灾害现场智能监测 · 告警列表',
      path: '/pages/alerts/index'
    };
  }
});
