const env = require('./config/env');
const net = require('./utils/net');

App({
  globalData: {
    useMock: env.USE_MOCK,
    // Mock 数据仅用于演示与前期测试，绝不能被描述为真实后端数据。
    mockNotice: env.USE_MOCK ? '当前为 Mock 演示数据，非真实后端数据' : ''
  },

  onLaunch() {
    // 网络状态只用于界面提示（离线横幅），不改变请求行为。
    net.init();
    // 预留：真实模式下的 Token 注入与生命周期管理需在联调前确认，
    // 本阶段不实现任何登录、签发、刷新或撤销能力。
  }
});
