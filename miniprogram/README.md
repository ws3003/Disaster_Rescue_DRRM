# V4.4 A 线微信小程序（首期三页）

本目录是 V4.4 A 线的微信小程序实现，依据：

- `docs/灾害现场智能监测与语音交互系统_v4.4_SRS_v0.8R1.md`
- `《V4.4 A 线正式执行任务书（定稿）》`
- `schemas/s4_contract.json`（契约版本 `0.1.0`）

首期只包含三个页面：

| 页面 | 路径 | 使用接口 |
|---|---|---|
| 首页 | `pages/home/index` | S4-01、S4-02（按 category 分别请求两组） |
| 告警列表 | `pages/alerts/index` | S4-02 |
| 告警详情 | `pages/detail/index` | S4-03、S4-04 |

S4-06（环境读数）契约保留，但按任务书首期不新增第四个页面。

## 首页信息来源与契约边界

- **系统运行状态**：S4-01。契约当前把 `device.status` 固定为占位值 `unknown`，
  `modules` 为空对象，且 `implementation_status=placeholder_not_implemented`。
  页面只如实展示占位结构，不补任何未经契约定义的字段。
- **最近告警**：S4-02 分别以 `category=person` 与 `category=sensor_environment`
  请求第 1 页（`page_size=3`），两组各自保持服务端顺序。
  契约明确禁止跨组全局时间排序，因此首页是“分组最近”，不是全局时间合并。
- **高风险摘要**：契约没有聚合/统计接口。摘要只使用 S4-02 每组响应的
  `meta.total` 做分类计数（人员组本身仅包含 `high_risk_alert`）。
  不推导 severity、状态或风险分值维度的统计。

以上三点中，“最近告警的全局合并排序”和“severity 维度高风险统计”属于契约无法支持的需求，
已按任务书要求记录为缺口，未修改契约、未虚构字段。

## 待决策清单与截止点

以下事项不阻塞当前 Mock 开发，但必须在对应时点前由项目负责人明确决定；
涉及共享契约的事项不得由 A/B 任何一方单方面修改。

| 编号 | 待决事项 | 需要决定什么 | 为什么不能拖 | 截止点 | 当前状态 |
|---|---|---|---|---|---|
| D-01 | 公网访问方式 | 后端以何种 HTTPS 域名/反向代理对外提供 S4 接口；域名备案与证书由谁准备 | 备案/证书周期长；体验版要求 HTTPS + 合法域名，不能用 IP | 后端部署启动时开始，体验版上传前必须完成 | 未决 |
| D-02 | 体验者 Token 方案 | 采用“输入访问口令”页（不改契约），还是新增登录/签发接口（需契约变更） | 若走登录接口，必须在 B 线实现 S4 路由前确定，否则返工 | B 线冻结 S4 接口前 | 未决 |
| D-03 | 首页“最近告警”范围 | 接受“人员组/传感器组各取最近 3 条、不做跨组全局排序”，还是申请契约变更支持全局最近 N 条 | 契约当前禁止跨组全局时间排序；变更会影响 B 线 S4-02 | B 线实现 S4-02 前 | 未决（当前按分组实现） |
| D-04 | 首页“高风险摘要”口径 | 接受“两分类 `meta.total` 计数”，还是申请聚合接口/字段支持 severity、active 等维度 | 契约没有聚合接口；新增统计口径属于契约变更 | B 线实现 S4-02 前 | 未决（当前按分类计数实现） |
| D-05 | S4-06 环境读数页面 | 首期是否新增第四个页面 | 任务书暂缓；新增页面扩大首期范围，需明确授权 | 首期验收前 | 待决（按任务书保持不新增） |
| D-06 | 正式小程序 AppID / 主体 | 用个人还是学校/企业主体注册；正式 AppID 何时提供 | 影响体验版/发布与部分开放能力；切换只需改 `project.config.json` | 体验版上传前 | 可后置 |

说明：

- D-03 / D-04 若决定维持现状，则无需变更契约，当前实现即为首期最终形态。
- D-01 / D-02 属于真实联调（L4）前置条件，未完成前 `USE_MOCK` 保持 `true`。
- D-05 未决期间不新增页面、不删除 S4-06 的需求与契约记录、不将其标记为完成。
- 任何契约变更必须按任务书 10.4 流程由 A/B 共同确认；
  A 线不得单方面修改 `schemas/s4_contract.json`。

## Mock 与真实模式

- 由 `config/env.js` 的 `USE_MOCK` 控制，当前为 `true`。
- Mock 模式只调用 `mock/mock-adapter.js`，**绝不调用 `wx.request`**；
  页面顶部固定显示“当前为 Mock 演示数据，非真实后端数据”。
- 真实模式只调用 `utils/request.js`，请求失败不会回退 Mock。
- 两种模式共用同一套本地参数校验、四字段信封校验和 DTO 校验。
- Token 从本地存储键 `s4_api_token` 读取，仓库内不含任何真实 Token。

真实模式启用前仍需确定（任务书 11.2）：Base URL/端口、request 合法域名、
HTTPS 部署、Token 注入方式与生命周期、访问控制、最终错误文案。
`project.config.json` 中的 `appid` 已填为正式小程序 AppID（`wxff90e9b39021f3db`），
该值只用于开发者工具，不属于敏感信息。

## 目录结构

```
miniprogram/
  app.js / app.json / app.wxss / project.config.json / sitemap.json
  config/env.js                  # USE_MOCK、Base URL 占位、Token 存储键
  utils/
    dto-schema.js                # 契约常量与 DTO 字段定义（0.1.0）
    validators.js                # L1 本地参数校验
    envelope.js                  # 信封 / DTO / meta / 关联数据校验
    errors.js                    # 错误码语义与 HTTP 映射
    request.js                   # 真实模式 wx.request + Bearer Token
    time.js                      # naive / zoned 时间展示（不改语义）
    view.js                      # 页面视图模型
  mock/fixtures.js               # 契约一致的 Mock 示例数据
  mock/mock-adapter.js           # 契约语义的分组、排序、分页、关联
  services/s4-service.js         # 统一校验与模式隔离
  services/index.js              # 默认服务工厂
  logic/                         # 可独立测试的页面状态机
  pages/home|alerts|detail/      # 三个页面
  templates/alert-item.wxml      # 列表项模板
  tests/                         # L1/L2/L3 测试
```

## 测试

在 `miniprogram/` 目录运行：

```bash
node --test tests/*.test.js
```

覆盖层级：

- **L1**：本地参数校验、Mock 分页/排序/空列表、页面状态机、重复请求与旧响应保护。
- **L2**：测试桩模拟 400/401/403/404/422/500、响应缺失 `code`、`has_more` 类型异常、
  真实模式失败不回退 Mock、Token 缺失本地拦截。
- **L3**：直接读取 `schemas/s4_contract.json`，核对接口路径、分页规则、错误码、
  11/14/7 个 DTO 字段的名称/类型/可空性/枚举、时间格式与 Mock 字段集合。

未执行：

- **L4 真实后端联调**：`server/s4.py` 尚未建立，B 线未就绪。
- **L5 微信开发者工具编译/真机**：当前环境未执行，不得标记为通过。
