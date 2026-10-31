const { markLatestByCategory } = require('../../utils/home-focus');
const { createDefaultService } = require('../../services/index');
const { createAlertsListController } = require('../../logic/alerts-list-controller');
const { buildAlertListViews, decorateListGroups } = require('../../utils/view');
const { filterAlertViews } = require('../../utils/filter');
const { CATEGORIES } = require('../../utils/dto-schema');
const store = require('../../utils/local-store');
const navContext = require('../../utils/nav-context');
const net = require('../../utils/net');

Page({
  data: {
    showAdvancedFilters: false,
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
    keywords: [],
    severityFilter: '',
    statusFilter: '',
    followedOnly: false,
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
    showBackTop: false,
    offline: false
  },

  toggleAdvancedFilters() {
    this.setData({ showAdvancedFilters: !this.data.showAdvancedFilters });
  },

  onLoad(query) {
    const app = getApp();
    const globalData = (app && app.globalData) || {};
    this.setData({
      mockMode: globalData.useMock === true,
      mockNotice: globalData.mockNotice || '',
      keywords: store.getKeywords()
    });

    this._unsubscribeNet = net.subscribe((online) => {
      this.setData({ offline: !online });
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

  onShow() {
    // 从详情页返回时，关注状态可能已变化，重新计算本地筛选结果。
    if (this._rawViews && this._rawViews.length) {
      this.renderViews();
    }
  },

  onUnload() {
    if (this._unsubscribeNet) {
      this._unsubscribeNet();
      this._unsubscribeNet = null;
    }
  },

  applyState(state) {
    const nowMs = Date.now();
    this._rawViews = markLatestByCategory(buildAlertListViews(state.items, { nowMs: nowMs }));
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

  // 先按关键词/严重程度/状态/关注做本地过滤，再按相邻日期与类别插入分组标题。
  // 顺序仍是服务端返回的顺序，不重排、不合并。
  renderViews() {
    const nowMs = Date.now();
    const filtered = filterAlertViews(this._rawViews, this.data.keyword, {
      severity: this.data.severityFilter,
      status: this.data.statusFilter,
      followedOnly: this.data.followedOnly,
      followedKeys: store.getFollowed()
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

  onKeywordConfirm() {
    const value = String(this.data.keyword || '').trim();
    if (value) {
      this.setData({ keywords: store.addKeyword(value) });
    }
  },

  onKeywordTap(event) {
    const value = event.currentTarget.dataset.value || '';
    this.setData({ keyword: value });
    this.renderViews();
  },

  onClearKeyword() {
    this.setData({ keyword: '' });
    this.renderViews();
  },

  onClearHistory() {
    this.setData({ keywords: store.clearKeywords() });
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

  onToggleFollowFilter() {
    this.setData({ followedOnly: !this.data.followedOnly });
    this.renderViews();
  },

  onResetFilters() {
    this.setData({
      keyword: '',
      severityFilter: '',
      statusFilter: '',
      followedOnly: false
    });
    this.renderViews();
  },

  onTabTap(event) {
    const value = event.currentTarget.dataset.value;
    if (value === this.data.category || !this._controller) {
      return;
    }
    // 切换类别时清空本地筛选，避免旧筛选造成“看起来没数据”的误解。
    this.setData({
      keyword: '',
      severityFilter: '',
      statusFilter: '',
      followedOnly: false
    });
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
    // 把“当前已展示的顺序”交给详情页，用于上一条/下一条导航。
    navContext.setContext(
      this.data.views.map(function (view) { return view.alertKey; }),
      '告警列表'
    );
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
