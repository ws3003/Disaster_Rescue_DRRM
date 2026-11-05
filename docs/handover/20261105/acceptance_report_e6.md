# V4.4 B 线 E6 云边协同推理专项验收报告

- 报告日期：2026-11-05（Asia/Shanghai）
- 目标分支：`feature/v4.4-b-backend`
- 验收对应版本（HEAD）：`d89f9f42e7ac0616caca1e47e3840bb14ebc78ce`

## 1. 验收背景与目标

E6 的目标是验证云边协同推理的**完整运行链路**：云端优先、云端故障本地回退、故障熔断、冷却后恢复探测、云端重新接管。

本次为**真实运行环境的阶段性专项验收**，验证的是**生产程序实际调用链**，而非单独的 API 连通测试。本报告不代表 E6 所有需求均已获得验收（未完成项见「9. 未完成事项与验收限制」）。

## 2. 验收环境与版本信息

| 项 | 值 |
| --- | --- |
| Windows 项目目录 | `D:\Disaster_Rescue_DRRM（2）` |
| Raspberry Pi 项目目录 | `/home/pi/Disaster_Rescue_DRRM_V4.4_B` |
| 分支 | `feature/v4.4-b-backend` |
| HEAD | `d89f9f42e7ac0616caca1e47e3840bb14ebc78ce` |
| Windows X4 服务端端口 | 8316 |
| Windows 局域网地址 | 192.168.43.206 |
| Raspberry Pi 地址 | 192.168.43.238 |
| X4 云模型 ID | yolov26l |
| X4 实际模型文件 | `D:/YOLOv26数据集/yolov26l_best.pt` |
| Pi 本地回退模型 | disaster_best |
| Pi 生产入口 | `src/edge_client.py` |

> 本报告不记录任何 Token / 密码 / 密钥的实际值。

## 3. 云边协同生产调用链

实际生产调用关系：

```
src/edge_client.py
  → build_x4_perception
  → X4DetectionService
  → CloudYoloClient
  → Windows X4 /predict（config/x4_server.yaml，模型 yolov26l）
```

- 云端调用**成功**时使用 Windows YOLOv26l 云模型，直接返回检测结果。
- 云端调用**失败**时使用 Pi 本地 `disaster_best` 模型回退。

本次测试验证的是**生产程序运行链路**（`edge_client` 经组合根装配 X4 服务），不只是单独 API 调用。

## 4. Pi 到 Windows 的直接推理连通性

| 项 | 实际结果 |
| --- | --- |
| 测试图片 | `data/test_images/002_fall.jpg` |
| 请求 | `POST /predict` |
| 模型 | yolov26l |
| HTTP 状态 | 200 |
| 检测目标数量 | 1 |
| 类别 | civilian |
| 置信度 | 约 0.7634 |
| 首次推理耗时 | 约 14469 ms（首次模型加载） |

**特别注明**：
- 首次 14469 ms 包含模型加载影响，**不作为稳定推理性能指标**。
- 本结果为**人员目标检测**（civilian），**不是**跌倒状态识别。

## 5. 正常云端优先模式

Pi 生产程序运行期间，Windows X4 服务端收到来自 Pi 的多次 `POST /predict` 请求（HTTP 200）；Pi SQLite `agent_logs` 记录：

| log_id | tool_name | provider | model_id | success | fallback_used | 延迟 |
| --- | --- | --- | --- | --- | --- | --- |
| 1-6 | x4_infer | cloud | yolov26l | true | false | 约 291-483 ms |

**结论**：生产程序实际进入 `cloud_preferred` 状态，使用 Windows 云端 YOLOv26l 模型进行推理。

## 6. 云端中断与本地回退

测试过程：主动停止 Windows X4 服务端，Raspberry Pi 的 `edge_client` 保持运行。

| log_id | tool_name | provider | model_id | success | fallback_used | error_code |
| --- | --- | --- | --- | --- | --- | --- |
| 7-8 | x4_infer | edge | disaster_best | true | true | cloud_timeout |
| 9 | x4_circuit | — | — | — | — | (见下) |
| 10 | x4_infer | edge | disaster_best | true | true | — |

log_id 9（熔断事件）：
- event = `enter_cooldown`
- from_state = `cloud_preferred` → to_state = `cloud_cooldown`
- failure_count = 3，threshold = 3，cooldown = 30 seconds

**结论**：云端不可用时，生产程序使用 Pi 本地 `disaster_best` 模型完成推理，并在连续三次失败后触发熔断进入冷却。

## 7. 云端恢复与重新接管

测试过程：重新启动 Windows X4 服务端后，Pi 再次向服务端发起请求并收到 HTTP 200。

| log_id | tool_name | event | from_state → to_state | 关键数值 |
| --- | --- | --- | --- | --- |
| 11 | x4_probe | enter_recovery | cloud_cooldown → recovery_probe | — |
| 15 | x4_circuit | restored | recovery_probe → cloud_preferred | success_count=2，recovery_threshold=2 |

log_id 16-19：

| tool_name | provider | model_id | success | fallback_used | 延迟 |
| --- | --- | --- | --- | --- | --- |
| x4_infer | cloud | yolov26l | true | false | 约 262-607 ms |

**结论**：云端服务恢复后，生产程序经恢复探测，在**连续两次成功**后重新进入 `cloud_preferred`，并重新使用 Windows YOLOv26l 云模型。

## 8. 综合验收结果

| 验证项 | 结果 |
| --- | --- |
| Pi 到 Windows X4 网络连通 | 通过 |
| 真实云端模型推理 | 通过 |
| 生产程序云端优先 | 通过 |
| 云端故障本地回退 | 通过 |
| 连续失败熔断 | 通过 |
| 冷却后恢复探测 | 通过 |
| 连续两次成功后恢复云端 | 通过 |
| 恢复后重新使用云端模型 | 通过 |

## 9. 未完成事项与验收限制

### 9.1 E6-07 同帧云边对比
当前云端推理成功后直接返回，**没有**完成"同一帧分别由云端模型和本地模型推理并进行对比"的验收。

**结论：未完成，不能列为通过。**

### 9.2 SRS 指定帧筛选上传
当前生产链路按现有帧循环调用云端推理，**尚未实现** SRS 要求的指定帧筛选机制。

**结论：未完成，不能列为通过。**

### 9.3 性能数据范围
- 首次 14469 ms 是包含模型加载影响的首次请求耗时，**不作为稳定推理性能指标**。
- 291-483 ms 与 262-607 ms 是本次记录到的运行耗时范围，**不得表述为**长期稳定性能保证。

### 9.4 能力边界
本次直接推理检测结果为 `civilian` 人员目标，**不代表**跌倒状态识别通过。

### 9.5 验收范围
本报告只证明本次测试环境下的云端优先、故障回退、熔断与恢复链路通过，**不代表 E6 所有需求全部完成**。

## 10. 阶段性验收结论

**E6 云边协同推理核心运行链路阶段性验收通过。**

已通过真实 Raspberry Pi 与 Windows X4 服务端的联调，验证：
- 云端优先推理；
- 云端故障后本地模型回退；
- 连续失败熔断；
- 冷却后恢复探测；
- 云端重新接管。

E6-07 同帧云边对比与 SRS 指定帧筛选上传**仍未完成**，后续应作为独立事项继续推进。因此，本结论**不等同于** E6 全部需求完成。