// Mock 适配器：返回与真实请求同形的 { statusCode, body }，
// 由 services/s4-service.js 走同一套信封/DTO 校验。
// Mock 模式绝不调用 wx.request。

const defaultFixtures = require('./fixtures');
const { ENDPOINTS } = require('../utils/dto-schema');

function okBody(data, meta) {
  return {
    statusCode: 200,
    body: {
      code: 'OK',
      message: 'ok',
      data: data,
      meta: meta || {}
    }
  };
}

function failBody(statusCode, code) {
  return {
    statusCode: statusCode,
    body: {
      code: code,
      message: code,
      data: null,
      meta: {}
    }
  };
}

function numericId(alertKey) {
  const parts = String(alertKey).split(':');
  return Number(parts[1]);
}

function sortPersonAlerts(items) {
  return items.slice().sort(function (a, b) {
    if (a.ts !== b.ts) {
      return a.ts < b.ts ? 1 : -1;
    }
    return numericId(b.alert_key) - numericId(a.alert_key);
  });
}

function sortSensorAlerts(items) {
  return items.slice().sort(function (a, b) {
    if (a.occurred_at !== b.occurred_at) {
      const ta = Date.parse(a.occurred_at);
      const tb = Date.parse(b.occurred_at);
      if (!Number.isNaN(ta) && !Number.isNaN(tb) && ta !== tb) {
        return tb - ta;
      }
      return a.occurred_at < b.occurred_at ? 1 : -1;
    }
    return numericId(b.alert_key) - numericId(a.alert_key);
  });
}

function createMockAdapter(options) {
  const opts = options || {};
  const fixtures = opts.fixtures || defaultFixtures;
  const latencyMs = opts.latencyMs === undefined ? 120 : opts.latencyMs;

  function respond(payload) {
    if (latencyMs <= 0) {
      return Promise.resolve(payload);
    }
    return new Promise(function (resolve) {
      setTimeout(function () {
        resolve(payload);
      }, latencyMs);
    });
  }

  function findAlert(alertKey) {
    const all = fixtures.allAlerts();
    for (let i = 0; i < all.length; i += 1) {
      if (all[i].alert_key === alertKey) {
        return all[i];
      }
    }
    return null;
  }

  function handleList(query) {
    const personGroup = sortPersonAlerts(fixtures.personAlerts);
    const sensorGroup = sortSensorAlerts(fixtures.sensorAlerts);
    const category = query.category;

    let groups;
    if (category === 'person') {
      groups = [personGroup];
    } else if (category === 'sensor_environment') {
      groups = [sensorGroup];
    } else {
      // 契约：人员告警组优先，传感器告警组其次；不做跨组全局时间排序。
      groups = [personGroup, sensorGroup];
    }

    let total = 0;
    let merged = [];
    groups.forEach(function (group) {
      total += group.length;
      merged = merged.concat(group);
    });

    const page = query.page;
    const pageSize = query.page_size;
    const offset = (page - 1) * pageSize;
    const items = merged.slice(offset, offset + pageSize);

    return okBody(items, {
      page: page,
      page_size: pageSize,
      total: total,
      has_more: page * pageSize < total
    });
  }

  function handleDetail(alertKey) {
    const alert = findAlert(alertKey);
    if (!alert) {
      return failBody(404, 'NOT_FOUND');
    }
    return okBody(alert, {});
  }

  function handleRelated(alertKey) {
    const alert = findAlert(alertKey);
    if (!alert) {
      return failBody(404, 'NOT_FOUND');
    }
    if (alert.category === 'person') {
      // 人员告警不关联环境读数，也不伪造媒体。
      return okBody({ readings: [], media: [], media_supported: false }, {});
    }
    // 仅通过 sensor_alerts.reading_id 建立关联，最多一条；
    // 不按时间接近、同一设备等方式推测关联。
    const reading = alert.reading_id ? fixtures.readingsById[alert.reading_id] : null;
    return okBody(
      {
        readings: reading ? [reading] : [],
        media: [],
        media_supported: false
      },
      {}
    );
  }

  function request(path, query) {
    if (path === ENDPOINTS.systemStatus) {
      return respond(okBody(fixtures.systemStatus, {}));
    }
    if (path === ENDPOINTS.alerts) {
      return respond(handleList(query || {}));
    }
    if (path.indexOf('/api/v1/alerts/') === 0 && /\/related$/.test(path)) {
      const encoded = path
        .slice('/api/v1/alerts/'.length)
        .replace(/\/related$/, '');
      return respond(handleRelated(decodeURIComponent(encoded)));
    }
    if (path.indexOf('/api/v1/alerts/') === 0) {
      const encoded = path.slice('/api/v1/alerts/'.length);
      return respond(handleDetail(decodeURIComponent(encoded)));
    }
    // 首期不提供环境读数页面；S4-06 契约保留但不在此 Mock 范围。
    return respond(failBody(404, 'NOT_FOUND'));
  }

  return {
    request: request,
    fixtures: fixtures
  };
}

module.exports = {
  createMockAdapter,
  sortPersonAlerts,
  sortSensorAlerts,
  failBody,
  okBody
};
