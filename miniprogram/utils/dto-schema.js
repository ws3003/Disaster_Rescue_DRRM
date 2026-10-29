// 契约静态常量映射，唯一依据为 schemas/s4_contract.json（contract_version 0.1.0）。
// 本文件只做客户端实现映射，不构成对契约的修改。
// tests/contract-static.test.js 会逐项核对这些定义与契约文件是否一致。

const CONTRACT_VERSION = '0.1.0';

const ENDPOINTS = {
  systemStatus: '/api/v1/system/status',
  alerts: '/api/v1/alerts',
  alertDetail: function (alertKey) {
    return '/api/v1/alerts/' + encodeURIComponent(alertKey);
  },
  alertRelated: function (alertKey) {
    return '/api/v1/alerts/' + encodeURIComponent(alertKey) + '/related'
  },
  // S4-06 契约保留，但首期不新增环境读数页面（任务书 1.2 / 3.2）。
  environmentReadings: '/api/v1/environment/readings'
};

const CATEGORIES = ['person', 'sensor_environment'];

const PAGINATION = {
  pageDefault: 1,
  pageSizeDefault: 20,
  pageSizeMax: 100,
  metaFields: ['page', 'page_size', 'total', 'has_more']
};

const SENSOR_STATUS_VALUES = ['active', 'resolved'];

const DTO = {
  personAlert: [
    { name: 'alert_key', type: 'string', nullable: false, prefix: 'person:' },
    { name: 'source', type: 'string', nullable: false, constValue: 'person' },
    { name: 'category', type: 'string', nullable: false, constValue: 'person' },
    { name: 'type', type: 'string', nullable: false },
    { name: 'severity', type: 'string', nullable: true },
    { name: 'ts', type: 'string', nullable: false, format: 'naive-iso' },
    { name: 'person_id', type: 'integer', nullable: true },
    { name: 'title', type: 'string', nullable: true },
    { name: 'state', type: 'string', nullable: true },
    { name: 'priority', type: 'string', nullable: true },
    { name: 'risk_score', type: 'number', nullable: true }
  ],
  sensorAlert: [
    { name: 'alert_key', type: 'string', nullable: false, prefix: 'sensor:' },
    { name: 'source', type: 'string', nullable: false, constValue: 'sensor' },
    { name: 'category', type: 'string', nullable: false, constValue: 'sensor_environment' },
    { name: 'type', type: 'string', nullable: false },
    { name: 'severity', type: 'string', nullable: true },
    { name: 'status', type: 'string', nullable: false, allowed: SENSOR_STATUS_VALUES },
    { name: 'temperature', type: 'number', nullable: true },
    { name: 'humidity', type: 'number', nullable: true },
    { name: 'threshold', type: 'number', nullable: true },
    { name: 'occurred_at', type: 'string', nullable: false, format: 'zoned-iso' },
    { name: 'resolved_at', type: 'string', nullable: true },
    { name: 'device_captured_at', type: 'string', nullable: true },
    { name: 'reading_id', type: 'string', nullable: true },
    { name: 'received_at', type: 'string', nullable: true }
  ],
  environmentReading: [
    { name: 'reading_id', type: 'string', nullable: false },
    { name: 'sensor_id', type: 'string', nullable: false },
    { name: 'temperature', type: 'number', nullable: false },
    { name: 'humidity', type: 'number', nullable: true },
    { name: 'device_captured_at', type: 'string', nullable: false, format: 'zoned-iso' },
    { name: 'edge_received_at', type: 'string', nullable: true },
    { name: 'received_at', type: 'string', nullable: true }
  ]
};

module.exports = {
  CONTRACT_VERSION,
  ENDPOINTS,
  CATEGORIES,
  PAGINATION,
  SENSOR_STATUS_VALUES,
  DTO
};
