# V4.0 模块数据接口协议

> 目标: 让成员 A、成员 B 可独立并行开发, 模块之间**禁止直接依赖代码**, 统一以 JSON 数据通信。
> 接口定义见 `schemas/` 下三个 Schema 文件; 测试数据见 `data/mock/`。

## 通用约定

- 所有接口为 **JSON 数据**, 不含任何函数/类引用。
- Schema 文件: `schemas/*.json` (JSON Schema draft-07)。
- 幂等性: 消费方对缺失字段应做**兜底默认**, 不崩溃。
- 数据解耦: V4.0 新模块只读取这些 JSON, 通过配置注入, 不 import 跨层代码。

---

## 接口 1: `schemas/vlm_output.json` — 环境理解输出

**生产者**: `src/vlm/` (VLM client → parser 产出)
**消费者**: `src/llm/` (作证据注入), `src/agent/` (环境状态)
**Mock 样例**: `data/mock/vlm_output.json`

```json
{
  "scene_type": "structural_collapse",
  "overall_severity": "critical",
  "hazards": [
    {"type": "debris", "brief": "一楼承重墙垮塌, 瓦砾覆盖通道"}
  ],
  "environment_note": "三层教学楼局部塌陷, 西北角有浓烟..."
}
```

| 字段 | 类型 | 说明 |
|---|---|---|
| `scene_type` | string | 灾害场景类型 (earthquake/fire/flood/structural_collapse…) |
| `overall_severity` | enum | low / medium / high / critical |
| `hazards` | array | 危险因素列表, 每项 `{type, brief}` |
| `environment_note` | string | 环境整体自然语言说明 |

**兜底规则**: `scene_type`/`overall_severity` 缺失时默认 `unknown`/`medium`; `hazards` 缺失默认空数组。

---

## 接口 2: `schemas/llm_output.json` — 风险推理输出

**生产者**: `src/llm/` (LLM client → 推理)
**消费者**: `src/agent/planner` (翻译为可执行任务)
**Mock 样例**: `data/mock/llm_output.json`

```json
{
  "event_summary": "教学楼坍塌...",
  "risk_explanations": [{"pid": 3, "reason": "昏迷倒地 判定 HIGH"}],
  "rescue_suggestions": ["优先破拆 抢救 id=3"],
  "task_proposals": [
    {"action": "alert", "target": 3, "payload": {"severity": "critical"}}
  ]
}
```

| 字段 | 类型 | 说明 |
|---|---|---|
| `event_summary` | string | 事件整体总结 |
| `risk_explanations` | array | 每人员 `{pid, reason}` 风险解释 |
| `rescue_suggestions` | array<string> | 救援建议列表 |
| `task_proposals` | array | 建议动作 `{action, target, payload}`, 与 agent_task 的 actions 对齐 |

**兜底规则**: `task_proposals` 缺失时, planner 走 **rule-based** 触发, 不依赖 LLM。

---

## 接口 3: `schemas/agent_task.json` — Agent 执行任务

**生产者**: `src/agent/planner`
**消费者**: `src/agent/executor` → `src/tools/`
**Mock 样例**: 由 planner 由 `llm_output.task_proposals` 生成 (无独立 mock)

```json
{
  "task_id": "task_20260919_001",
  "trigger": {
    "source": "llm",
    "reason": "检测到昏迷倒地人员",
    "ts": "2026-09-19T09:12:03+08:00"
  },
  "actions": [
    {"tool": "communication_tool", "args": {"target": 3, "message": "..."}}
  ],
  "status": "pending"
}
```

| 字段 | 类型 | 说明 |
|---|---|---|
| `task_id` | string | 任务唯一标识 |
| `trigger` | object | 触发源 `{source, reason, ts}` |
| `actions` | array | 按序工具动作 `{tool, args}` |
| `status` | enum | pending / running / done / failed |

**兜底规则**: 空 `actions` 视为无操作, 直接置 `status=done` 不入库。

---

## 依赖关系小结

```
vlm_output ──► llm_output ──► agent_task ──► tools
   (D: vlm)      (D: llm)       (D: agent)     (tools 消费, 回写 database)
```

- 成员 A 只产 VLM/LLM 两个接口; 成员 B 只产/消费 agent_task。
- 双方按 Schema 联调, 不共享代码函数, 用 `data/mock/` 各自独立测试。