const { drawTemperatureTrend } = require('../../utils/draw-trend');

Component({
  properties: {
    categoryChart: { type: Object, value: {} },
    temperatureChart: { type: Object, value: {} },
    mockMode: { type: Boolean, value: false }
  },
  data: { canvasUnavailable: false },
  observers: {
    temperatureChart: function () { this.scheduleDraw(); }
  },
  lifetimes: {
    ready() { this._chartReady = true; this.scheduleDraw(); },
    detached() { this._chartReady = false; this._drawVersion = (this._drawVersion || 0) + 1; }
  },
  pageLifetimes: {
    resize() { this.scheduleDraw(); }
  },
  methods: {
    openCategory(event) {
      this.triggerEvent('categorytap', { category: event.currentTarget.dataset.category });
    },
    scheduleDraw() {
      if (!this._chartReady) return;
      const version = this._drawVersion = (this._drawVersion || 0) + 1;
      const draw = () => {
        if (!this._chartReady || version !== this._drawVersion || !this.properties.temperatureChart.hasValues) return;
        this.createSelectorQuery().select('#temperatureCanvas').fields({ node: true, size: true }).exec((result) => {
          if (!this._chartReady || version !== this._drawVersion) return;
          const target = result && result[0];
          if (!target || !target.node || !target.width || !target.height) {
            this.setData({ canvasUnavailable: true });
            return;
          }
          try {
            const canvas = target.node;
            const ctx = canvas.getContext('2d');
            const info = typeof wx.getWindowInfo === 'function' ? wx.getWindowInfo() : {};
            const ratio = Number.isFinite(info.pixelRatio) && info.pixelRatio > 0 ? info.pixelRatio : 1;
            canvas.width = Math.round(target.width * ratio);
            canvas.height = Math.round(target.height * ratio);
            ctx.scale(ratio, ratio);
            drawTemperatureTrend(ctx, this.properties.temperatureChart, target.width, target.height);
          } catch (error) {
            this.setData({ canvasUnavailable: true });
          }
        });
      };
      this.setData({ canvasUnavailable: false }, () => {
        if (typeof wx.nextTick === 'function') wx.nextTick(draw);
        else draw();
      });
    }
  }
});
