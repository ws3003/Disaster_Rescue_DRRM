// Device-only preview state. No persistence, upload, API fields, or inferred alert association.
Component({
  properties: {
    contextKey: { type: String, value: '', observer: function () { this.clearPreview(); } }
  },
  data: { imagePath: '', videoPath: '', choosing: false },
  methods: {
    choosePreview(event) {
      if (this.data.choosing) return;
      const kind = event.currentTarget.dataset.kind;
      if (kind !== 'image' && kind !== 'video') return;
      if (typeof wx.chooseMedia !== 'function') {
        wx.showToast({ title: '当前微信版本不支持本机预览', icon: 'none' });
        return;
      }
      const context = this.properties.contextKey;
      this.setData({ choosing: true });
      wx.chooseMedia({
        count: 1, mediaType: [kind], sourceType: ['album'],
        success: (result) => {
          if (this.properties.contextKey !== context) return;
          const file = result.tempFiles && result.tempFiles[0];
          if (!file || !file.tempFilePath) return;
          this.setData(kind === 'image' ? { imagePath: file.tempFilePath } : { videoPath: file.tempFilePath });
        },
        fail: (error) => {
          if (!/cancel/i.test(error.errMsg || '')) wx.showToast({ title: '未能打开本机文件', icon: 'none' });
        },
        complete: () => this.setData({ choosing: false })
      });
    },
    previewImage() {
      if (this.data.imagePath) wx.previewImage({ current: this.data.imagePath, urls: [this.data.imagePath] });
    },
    expandVideo() {
      if (!this.data.videoPath) return;
      const video = wx.createVideoContext('sceneVideo', this);
      video.requestFullScreen({
        success: () => video.play(),
        fail: () => wx.showToast({ title: '请点击播放器右下角全屏按钮', icon: 'none' })
      });
    },
    clearPreview() {
      this.setData({ imagePath: '', videoPath: '' });
    },
    handleMediaError(event) {
      const kind = event.currentTarget.dataset.kind;
      this.setData(kind === 'image' ? { imagePath: '' } : { videoPath: '' });
      wx.showToast({ title: '文件无法预览，请重新选择', icon: 'none' });
    }
  }
});
