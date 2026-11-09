// A 线运行时配置。禁止在此文件中写入任何真实 Token 或密钥。
// USE_MOCK=true 时只使用 Mock 适配器，绝不调用 wx.request。
// 切换为真实模式（false）前，必须先满足任务书第 10.5 节联调前置条件。
// 体验版局域网联调：手机、Windows 后端和树莓派必须在同一 Wi-Fi/热点。
const USE_MOCK = false;

// Base URL / 合法域名 / AppID / 端口均为待定部署参数（任务书 11.2）。
// 此处保留明显的占位值，避免被误认为真实后端地址。
const API_BASE_URL = 'http://192.168.0.3:8000';
// 小程序只保存由 /api/v1/auth/login 返回的短期用户 Session。
// S4_API_TOKEN、C4_TOKEN、OSS AK/SK 均不得进入小程序。
const SESSION_STORAGE_KEY = 'v44_user_session';
const REQUEST_TIMEOUT_MS = 10000;

module.exports = {
  USE_MOCK,
  API_BASE_URL,
  SESSION_STORAGE_KEY,
  REQUEST_TIMEOUT_MS
};
