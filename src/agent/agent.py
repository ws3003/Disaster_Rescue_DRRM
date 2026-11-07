"""
src/agent/agent.py

Disaster Rescue DRRM Agent
--------------------------

Main orchestration layer:

    Trigger
       ↓
     Agent
       ↓
    Memory
       ↓
    Planner
       ↓
    Executor
       ↓
     Tools
       ↓
    SQLite
"""

from __future__ import annotations

import sys
import json
import re
import inspect
from pathlib import Path
from datetime import datetime
from typing import Any


# ============================================================
# Project root compatibility
# ============================================================

PROJECT_ROOT = Path(__file__).resolve().parents[2]

if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


# ============================================================
# Project imports
# ============================================================

from src.agent import triggers as triggers_module
from src.agent.memory import Memory
from src.agent.planner import Planner
from src.agent.executor import Executor


# ============================================================
# Agent
# ============================================================

class Agent:
    """
    Main orchestration layer of the DRRM system.

    Agent coordinates:

        Trigger
           ↓
        Memory
           ↓
        Planner
           ↓
        Executor
           ↓
        Tools
           ↓
        SQLite

    Agent does not directly execute individual tools.
    """

    def __init__(
        self,
        db_path: str | None = None,
        config: dict[str, Any] | None = None,
    ):

        cfg = config or {}
        if db_path is None:
            db_path = cfg.get("db", {}).get("path", "data/drrm.db")
        self.db_path = db_path

        agent_cfg = cfg.get("agent", {}) or {}

        # --------------------------------------------------------
        # Trigger module
        # --------------------------------------------------------

        trigger_cfg = agent_cfg.get("trigger", {}) or {}
        self.trigger_manager = self._create_trigger_manager(trigger=trigger_cfg)

        # --------------------------------------------------------
        # Memory
        # --------------------------------------------------------

        memory_cfg = agent_cfg.get("memory", {}) or {}
        memory_db = None
        if config is not None:
            from src.database.db import Database
            memory_db = Database(db_path=db_path)  # 复用 config.db.path
        self.memory = Memory(
            cooldown_minutes=memory_cfg.get("cooldown_minutes", 30),
            db=memory_db,
        )

        # --------------------------------------------------------
        # Planner
        # --------------------------------------------------------

        planner_cfg = agent_cfg.get("planner", {}) or {}
        self.planner = Planner(
            memory=self.memory,
            cooldown_minutes=planner_cfg.get("cooldown_minutes", 30),
            max_steps=planner_cfg.get("max_steps", 3),
        )

        # --------------------------------------------------------
        # Executor
        # --------------------------------------------------------

        tools_config = {
            "vision": cfg.get("vision", {}) or {},
            "communication": cfg.get("communication", {}) or {},
            "rescue": cfg.get("rescue", {}) or {},
        } if config is not None else None

        self.executor = Executor(
            db_path=db_path,
            tools_config=tools_config,
        )

        # --------------------------------------------------------
        # Runtime statistics
        # --------------------------------------------------------

        self.stats = {
            "triggers_received": 0,
            "plans_created": 0,
            "tasks_completed": 0,
            "tasks_failed": 0,
            "tasks_suppressed": 0,
        }

    # ============================================================
    # Trigger module compatibility
    # ============================================================

    def _create_trigger_manager(self, trigger: dict[str, Any] | None = None):
        """
        Create the trigger engine from the existing triggers.py.

        The current project may use a different class name inside
        triggers.py. Therefore Agent does not require the class to
        be specifically named TriggerManager.

        trigger: 可选配置 dict(confirm_frames/…), 传给兼容的 Trigger 构造器;
                 不支持这些参数的类会优雅回退到无参构造。
        """
        trigger_kwargs = dict(trigger or {})

        # --------------------------------------------------------
        # 1. Prefer commonly used trigger class names
        # --------------------------------------------------------

        preferred_names = [
            "TriggerManager",
            "TriggerEngine",
            "TriggerSystem",
            "TriggerProcessor",
            "TriggerHandler",
        ]

        for class_name in preferred_names:

            cls = getattr(
                triggers_module,
                class_name,
                None,
            )

            if cls is None:
                continue

            if not inspect.isclass(cls):
                continue

            try:
                return cls(**trigger_kwargs)
            except TypeError:
                try:
                    return cls()
                except TypeError:
                    try:
                        return cls(
                            memory=self.memory
                        )
                    except Exception:
                        continue

        # --------------------------------------------------------
        # 2. Automatically search trigger-related classes
        # --------------------------------------------------------

        for name, cls in inspect.getmembers(
            triggers_module,
            inspect.isclass,
        ):

            if cls.__module__ != triggers_module.__name__:
                continue

            lower_name = name.lower()

            if "trigger" not in lower_name:
                continue

            try:
                return cls(**trigger_kwargs)
            except Exception:
                try:
                    return cls()
                except Exception:
                    continue

        # --------------------------------------------------------
        # 3. No compatible trigger class
        # --------------------------------------------------------

        print(
            "[Agent] Warning: no trigger manager class "
            "was found in triggers.py."
        )

        return None

    # ============================================================
    # Main trigger processing
    # ============================================================

    def process_trigger(
        self,
        trigger: dict[str, Any],
        context: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """
        Process one trigger:

            Trigger
                ↓
            Memory
                ↓
            Planner
                ↓
            Executor
        """

        if not isinstance(
            trigger,
            dict,
        ):

            return {
                "ok": False,
                "stage": "agent",
                "error": (
                    "Trigger must be a dictionary."
                ),
            }

        self.stats[
            "triggers_received"
        ] += 1

        # --------------------------------------------------------
        # Normalize
        # --------------------------------------------------------

        normalized_trigger = (
            self._normalize_trigger(
                trigger
            )
        )

        # --------------------------------------------------------
        # Memory
        # --------------------------------------------------------

        self._remember_trigger(
            normalized_trigger
        )

        # --------------------------------------------------------
        # Context
        # --------------------------------------------------------

        planner_context = (
            self._build_context(
                trigger=normalized_trigger,
                context=context,
            )
        )

        # --------------------------------------------------------
        # Planner
        # --------------------------------------------------------

        plan = self.planner.plan(
            normalized_trigger,
            context=planner_context,
        )

        if not isinstance(
            plan,
            dict,
        ):

            return {
                "ok": False,
                "stage": "planner",
                "error": (
                    "Planner returned "
                    "an invalid result."
                ),
                "trigger": normalized_trigger,
            }

        self.stats[
            "plans_created"
        ] += 1

        # --------------------------------------------------------
        # Suppressed
        # --------------------------------------------------------

        if plan.get(
            "status"
        ) == "suppressed":

            self.stats[
                "tasks_suppressed"
            ] += 1

            return {
                "ok": True,
                "stage": "planner",
                "status": "suppressed",
                "task_id": plan.get(
                    "task_id"
                ),
                "trigger_type": plan.get(
                    "trigger_type"
                ),
                "person_id": plan.get(
                    "person_id"
                ),
                "priority": plan.get(
                    "priority"
                ),
                "reason": plan.get(
                    "reason"
                ),
                "plan": plan,
            }

        # --------------------------------------------------------
        # Validate actions
        # --------------------------------------------------------

        actions = plan.get(
            "actions",
            [],
        )

        if not isinstance(
            actions,
            list,
        ):

            return {
                "ok": False,
                "stage": "planner",
                "error": (
                    "Planner actions "
                    "must be a list."
                ),
                "plan": plan,
            }

        # --------------------------------------------------------
        # No action
        # --------------------------------------------------------

        if len(actions) == 0:

            return {
                "ok": True,
                "stage": "planner",
                "status": "no_action",
                "task_id": plan.get(
                    "task_id"
                ),
                "trigger_type": plan.get(
                    "trigger_type"
                ),
                "plan": plan,
            }

        # --------------------------------------------------------
        # V4.3 Alarm Fusion (规则路径): 送 Executor 前经融合门。
        # --------------------------------------------------------
        # 临时 debug: 打印 fusion 前后通信 send_alert 的 severity/priority。
        _pre = [((a.get("args") or {}).get("severity"),
                 (a.get("args") or {}).get("priority"))
                for a in actions
                if (a.get("args") or {}).get("action") == "send_alert"]
        self._apply_alarm_fusion(actions, context)
        _post = [((a.get("args") or {}).get("severity"),
                  (a.get("args") or {}).get("priority"))
                 for a in actions
                 if a.get("tool") == "communication_tool"
                 and (a.get("args") or {}).get("action") == "send_alert"]
        print(f"[AlarmFusion][rule] pre={_pre} post={_post}")

        # --------------------------------------------------------
        # Executor
        # --------------------------------------------------------

        execution_result = (
            self.executor.execute(
                plan
            )
        )

        if not isinstance(
            execution_result,
            dict,
        ):

            return {
                "ok": False,
                "stage": "executor",
                "error": (
                    "Executor returned "
                    "an invalid result."
                ),
                "plan": plan,
            }

        # --------------------------------------------------------
        # Statistics
        # --------------------------------------------------------

        status = execution_result.get(
            "status"
        )

        if status == "completed":

            self.stats[
                "tasks_completed"
            ] += 1

        elif status == "failed":

            self.stats[
                "tasks_failed"
            ] += 1

        # 决策3: 仅在报警真正成功(communication_tool 返回 ok)时记 cooldown;
        # 通信失败不会调用 record_alert
        if isinstance(execution_result, dict):
            for _r in (execution_result.get("results", []) or []):
                if isinstance(_r, dict) and _r.get("tool") == "communication_tool" \
                        and _r.get("ok"):
                    self.memory.record_alert(
                        person_id=normalized_trigger.get("person_id"),
                        state=normalized_trigger.get("state"),
                        risk_score=normalized_trigger.get("risk_score"),
                        priority=normalized_trigger.get("priority"),
                    )
                    break

        # --------------------------------------------------------
        # Final result
        # --------------------------------------------------------

        return {
            "ok": bool(
                execution_result.get(
                    "ok",
                    False,
                )
            ),
            "stage": "completed",
            "status": status,
            "task_id": execution_result.get(
                "task_id"
            ),
            "trigger_type": execution_result.get(
                "trigger_type"
            ),
            "person_id": execution_result.get(
                "person_id"
            ),
            "priority": execution_result.get(
                "priority"
            ),
            "actions_total": execution_result.get(
                "actions_total"
            ),
            "actions_executed": execution_result.get(
                "actions_executed"
            ),
            "plan": plan,
            "execution": execution_result,
        }

    # ============================================================
    # LLM ingestion (方案 A: 统一编排入口)
    # ============================================================

    def extract_authoritative_observation(self, llm_output: dict[str, Any]):
        """A→B Contract(Phase1): 提取顶层 observation 权威块。

        返回 (error|None, persons_map|None, observation|None)。
        - 无 observation/persons → (None, None, None)：视为 legacy Contract
        - observation 存在但缺 observation_id 或 persons 非列表 → error（拒绝执行）
        - persons[] 内 person_id 重复 → 后者覆盖（确定性）；person_id 仅 observation 内有效
        """
        persons = llm_output.get("persons")
        observation = llm_output.get("observation")
        if observation is None and persons is None:
            return None, None, None
        if not isinstance(observation, dict) or not observation.get("observation_id"):
            return "observation missing or missing observation_id", None, None
        if not isinstance(persons, list):
            return "observation present but 'persons' missing or not a list", None, None
        persons_map = {}
        for p in persons:
            if isinstance(p, dict) and p.get("person_id") is not None:
                persons_map[str(p["person_id"])] = p
        return None, persons_map, observation

    def ingest_llm_output(
        self,
        llm_output: dict[str, Any],
        context: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        """把 LLM 决策正式纳入统一编排（A→B Contract Phase1）。

        llm_output → planner.plan_from_llm_output → agent_task
        → Policy(Memory.should_alert 冷却门, 仅拦截 send_alert)
        → Executor.execute_agent_task → Tools → SQLite
        → 通信成功后 Memory.record_alert。

        Contract(Phase1, 权威): 若顶层带 `observation` + `persons[]`，
          state/state_conf/risk_score/priority/bbox/frame_idx 以 observation.persons[target]
          为唯一权威（Edge FACT），忽略 task_proposals.payload 中的同名字段；
          target 缺失或不在 observation.persons → 拒绝该 send_alert（不执行、不 record）。
        旧 Contract(无 observation/persons): 保持向后兼容（payload facts 视为 legacy 来源）。

        返回: list[dict]（与 process_frame 一致；含 suppressed / rejected 计数）。
        """
        context = context or {}

        # Phase1 权威观测: (error, persons_map|None, observation|None)
        auth_error, persons_map, _obs = self.extract_authoritative_observation(llm_output)
        if auth_error:
            return [{"ok": False, "stage": "contract", "error": auth_error,
                     "task": llm_output}]

        task = self.planner.plan_from_llm_output(llm_output, context=context)
        if not isinstance(task, dict) or not task.get("ok"):
            err = task.get("error", "llm planning failed") if isinstance(task, dict) else "invalid task"
            return [{"ok": False, "stage": "planner", "error": err, "task": task}]
        if task.get("status") == "suppressed":
            return [{"ok": True, "stage": "planner", "status": "suppressed", "task": task}]

        actions = task.get("actions", []) or []
        filtered: list[dict[str, Any]] = []
        suppressed: list[dict[str, Any]] = []
        rejected: list[dict[str, Any]] = []

        # Policy: 权威回填 + 冷却门 —— 仅在发送前处理 send_alert; 其他 action 照常
        for a in actions:
            if isinstance(a, dict) and a.get("tool") == "communication_tool" \
                    and (a.get("args", {}) or {}).get("action") == "send_alert":
                args = dict(a.get("args", {}) or {})
                pid = args.get("person_id")
                st = None
                if persons_map is not None:
                    # 权威模式: target 必须是本次 observation 中的一个 person
                    if pid is None:
                        rejected.append({"ok": False, "tool": "communication_tool",
                                         "action": "send_alert", "person_id": None,
                                         "rejected": True, "reason": "target missing"})
                        continue
                    if str(pid) not in persons_map:
                        rejected.append({"ok": False, "tool": "communication_tool",
                                         "action": "send_alert", "person_id": pid,
                                         "rejected": True,
                                         "reason": "target not in observation.persons"})
                        continue
                    p = persons_map[str(pid)]
                    # 回填 Edge FACT（覆盖 LLM payload 中的 state/priority/risk, 权威优先）
                    args["state"] = p.get("state")
                    args["priority"] = p.get("priority")
                    args["risk_score"] = p.get("risk_score")
                    st = args["state"]
                else:
                    # legacy: 以 payload 字段为来源
                    st = args.get("state")
                    if pid is None:
                        rejected.append({"ok": False, "tool": "communication_tool",
                                         "action": "send_alert", "person_id": None,
                                         "rejected": True, "reason": "target missing"})
                        continue

                if pid is not None and st:
                    if not self.memory.should_alert(
                        person_id=pid, state=st,
                        current_risk=args.get("risk_score"),
                        minutes=self.memory.cooldown_minutes,
                    ):
                        suppressed.append({
                            "ok": False, "tool": "communication_tool", "action": "send_alert",
                            "person_id": pid, "state": st, "suppressed": True,
                            "reason": "Memory cooldown",
                            "authoritative": persons_map is not None,
                        })
                        continue
                a["args"] = args
            filtered.append(a)

        # ── persons 注入(方案A): 把权威 persons_map 传给 rescue_tool / vision_tool。
        #    仅限这两个 tool; 保留原 args; 已有 persons 不覆盖;
        #    仅发生在 LLM 路径 ingest_llm_output, 不影响规则路径 process_frame。
        if persons_map is not None:
            injected_persons = list(persons_map.values())
            for act in filtered:
                if isinstance(act, dict) and act.get("tool") in ("rescue_tool", "vision_tool"):
                    args = act.get("args")
                    if isinstance(args, dict) and "persons" not in args:
                        args["persons"] = injected_persons
                        print(f"[LLM->Agent] persons injected into {act.get('tool')} "
                              f"action ({len(injected_persons)} persons)")

        # ── 图片注入(最小补丁): 把 context 携带的当前分析图公网 URL 接到 send_alert。
        #    强保证: 只要 context.image_url 是 http 且 filtered 存在 send_alert, 执行前必补 image_path。
        #    注入前规范化: 兼容 Markdown 链接 [url](url) → 纯 http(s) URL。
        raw_url = (context or {}).get("image_url")
        img_url = ""
        if isinstance(raw_url, str) and raw_url.strip():
            _raw_s = raw_url.strip()
            _m = re.search(r"\[.*?\]\((https?://[^)]+)\)", _raw_s)
            img_url = (_m.group(1).strip() if _m else _raw_s)
            if not img_url.lower().startswith(("http://", "https://")):
                img_url = ""
            print(f"[IMAGE] original_url = {raw_url}")
            print(f"[IMAGE] normalized_url = {img_url}")

        if img_url:
            # communication_tool / send_alert:
            #   → args["image_path"]=img_url  (已有 image_path 不覆盖)
            for act in filtered:
                if isinstance(act, dict) and act.get("tool") == "communication_tool" \
                        and (act.get("args") or {}).get("action") == "send_alert":
                    args = act.get("args")
                    if isinstance(args, dict) and not args.get("image_path"):
                        args["image_path"] = img_url
                        print(f"[LLM->Agent] image injected into communication_tool "
                              f"action: {img_url}")
            # vision_tool / get_snapshot → args["image_url"]=img_url (供返回 OSS 公网 URL)
            for act in filtered:
                if isinstance(act, dict) and act.get("tool") == "vision_tool" \
                        and (act.get("args") or {}).get("action") == "get_snapshot":
                    vargs = act.get("args")
                    if isinstance(vargs, dict) and not vargs.get("image_url"):
                        vargs["image_url"] = img_url
                        print(f"[LLM->Agent] image injected into vision_tool action: {img_url}")

        # ── V4.3 报警融合门: person(Edge) 与 环境(env) 双维; 在送 Executor 前改写 send_alert。
        try:
            self._apply_alarm_fusion(filtered, context)
        except Exception:  # noqa: BLE001 — 融合门失败不改写(保守保留), 不阻断 Agent
            pass

        execution_results: list[dict[str, Any]] = []
        if filtered:
            exec_task = dict(task)
            exec_task["actions"] = filtered
            res = self.executor.execute_agent_task(exec_task)
            if isinstance(res, dict):
                execution_results = res.get("results", []) or []

            # 通信成功才 record_alert(与规则链一致); 失败不记、不入 cooldown
            # 按 action 逐条记录: 每个实际成功的 send_alert 记各自 (person,state) 冷却
            for act, res in zip(filtered, execution_results):
                if isinstance(act, dict) and act.get("tool") == "communication_tool" \
                        and isinstance(res, dict) and res.get("ok"):
                    args = act.get("args", {}) or {}
                    self.memory.record_alert(
                        person_id=args.get("person_id"),
                        state=args.get("state"),
                        risk_score=args.get("risk_score"),
                        priority=args.get("priority"),
                    )

        execution_results.extend(rejected)
        execution_results.extend(suppressed)
        return [{
            "ok": True, "stage": "completed",
            "task": task, "execution": execution_results,
            "suppressed": len(suppressed), "rejected": len(rejected),
        }]

    def _apply_alarm_fusion(self, actions: list, context: dict | None) -> None:
        """报警融合门(新报警矩阵): person / environment 两类独立。

        规则:
          - person alert       ⇔ person_exists 且 person.priority==HIGH
          - environment alert  ⇔ person_exists 且 environment_severity==HIGH
          - 二者独立; person HIGH + env HIGH → 同时保留人员 + 生成环境(不再去重)
          - 无人 + env HIGH → 禁止生成环境报警
          - 其余 → 不产生通信 send_alert(保留已有非通信动作)

        原地把 actions 规整为"仅保留 person/env 两类 send_alert(打 alert_type)"。
        """
        context = context or {}
        env_sev = str(context.get("environment_severity", "unknown")).upper()

        person_exists = False
        person_high = False
        first_person = None       # 首个带人员信息的 args(供 person alert)
        first_person_alert = None # 首个原始 send_alert args（保留标题/正文）
        for act in actions or []:
            if not isinstance(act, dict):
                continue
            args = act.get("args") or {}
            if (act.get("tool") == "communication_tool"
                    and str(args.get("action") or "") == "send_alert"
                    and first_person_alert is None):
                first_person_alert = dict(args)
            if (args.get("person_id") is not None
                    or args.get("priority") is not None
                    or args.get("state") not in (None, "")):
                if not person_exists:
                    first_person = dict(args)
                person_exists = True
                if str(args.get("priority") or "unknown").upper() == "HIGH":
                    person_high = True

        new_actions: list = []
        # 1) person 报警(仅人员风险)
        if person_exists and person_high:
            # 以原 communication action 为基底，避免前序 database/rescue
            # action 的 person 字段意外丢失 send_alert 所需的 title/message。
            pa = dict(first_person_alert or first_person or {})
            pa["action"] = "send_alert"
            pa["alert_type"] = "person"
            person_label = pa.get("person_id")
            state_label = pa.get("state") or "HIGH"
            pa.setdefault("title", f"[HIGH] Person {person_label} Alert")
            pa.setdefault("message", f"检测到高风险人员状态：{state_label}")
            new_actions.append({"tool": "communication_tool", "args": pa})
            # 高危人员报警还必须写入本地事件库。DatabaseTool 会在同一事务中
            # 创建 C4 Outbox 记录，随后由 C4 worker 同步到 Windows / 小程序。
            # 已有 save_event 时不重复创建，保留规划器给出的原始事件。
            has_person_event = any(
                isinstance(item, dict)
                and item.get("tool") == "database_tool"
                and str((item.get("args") or {}).get("action") or "") == "save_event"
                and (item.get("args") or {}).get("person_id") is not None
                for item in actions or []
            )
            if not has_person_event:
                risk_score = pa.get("risk_score")
                # C4 canonical 只接受精确十进制的风险分，避免二进制 float
                # 造成不可解析而转入 c4_verify。
                if isinstance(risk_score, float):
                    risk_score = repr(risk_score)
                new_actions.append({
                    "tool": "database_tool",
                    "args": {
                        "action": "save_event",
                        "type": "person_alert",
                        "severity": "HIGH",
                        "person_id": pa.get("person_id"),
                        "payload": {
                            "state": pa.get("state"),
                            "priority": pa.get("priority", "HIGH"),
                            "risk_score": risk_score,
                            "timestamp": datetime.now().astimezone().isoformat(),
                            "title": pa.get("title"),
                            "message": pa.get("message"),
                        },
                    },
                })
        # 2) environment 报警(仅当检测到人员)
        if person_exists and env_sev == "HIGH":
            has_environment_event = any(
                isinstance(item, dict)
                and item.get("tool") == "database_tool"
                and str((item.get("args") or {}).get("action") or "") == "save_event"
                and str((item.get("args") or {}).get("type") or "") == "environment_alert"
                for item in actions or []
            )
            new_actions.append({
                "tool": "communication_tool",
                "args": {
                    "action": "send_alert",
                    "alert_type": "environment",
                    "person_id": None,
                    "environment_severity": "HIGH",
                    "title": "环境高风险报警",
                    "message": "检测到高危险灾害环境，请关注现场情况",
                },
            })
            # 环境高风险已发出通知时，也同步写入 C4，确保小程序能展示。
            if not has_environment_event:
                new_actions.append({
                    "tool": "database_tool",
                    "args": {
                        "action": "save_event",
                        "type": "environment_alert",
                        "severity": "HIGH",
                        "person_id": None,
                        "payload": {
                            "state": "environment",
                            "priority": "HIGH",
                            "timestamp": datetime.now().astimezone().isoformat(),
                            "title": "环境高风险报警",
                            "message": "检测到高危险灾害环境，请关注现场情况",
                        },
                    },
                })

        # 3) 保留非通信(非 send_alert)既有动作; 通信 send_alert 已由上面规整取代
        for act in actions or []:
            if not isinstance(act, dict):
                new_actions.append(act)
                continue
            args = act.get("args") or {}
            if act.get("tool") == "communication_tool" \
                    and str(args.get("action") or "") == "send_alert":
                continue
            new_actions.append(act)

        actions[:] = new_actions

    # ============================================================
    # Frame processing
    # ============================================================

    def process_frame(
        self,
        frame_record: dict[str, Any],
        context: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        """
        Process one structured perception frame.
        """

        if not isinstance(
            frame_record,
            dict,
        ):

            return [
                {
                    "ok": False,
                    "stage": "agent",
                    "error": (
                        "frame_record must "
                        "be a dictionary."
                    ),
                }
            ]

        # --------------------------------------------------------
        # Memory
        # --------------------------------------------------------

        self._remember_frame(
            frame_record
        )

        # --------------------------------------------------------
        # Trigger generation
        # --------------------------------------------------------

        trigger_result = (
            self._generate_triggers_from_frame(
                frame_record
            )
        )

        if trigger_result is None:

            return [
                {
                    "ok": False,
                    "stage": "trigger_manager",
                    "error": (
                        "The existing triggers.py "
                        "does not expose a supported "
                        "frame-processing method."
                    ),
                }
            ]

        # --------------------------------------------------------
        # Normalize result
        # --------------------------------------------------------

        if isinstance(
            trigger_result,
            dict,
        ):

            triggers = [
                trigger_result
            ]

        elif isinstance(
            trigger_result,
            list,
        ):

            triggers = trigger_result

        else:

            return [
                {
                    "ok": False,
                    "stage": "trigger_manager",
                    "error": (
                        "Unsupported trigger result type."
                    ),
                }
            ]

        # --------------------------------------------------------
        # Process each trigger
        # --------------------------------------------------------

        results = []

        for trigger in triggers:

            if not isinstance(
                trigger,
                dict,
            ):
                continue

            result = self.process_trigger(
                trigger=trigger,
                context=context,
            )

            results.append(
                result
            )

        return results

    # ============================================================
    # Try existing trigger API
    # ============================================================

    def _generate_triggers_from_frame(
        self,
        frame_record: dict[str, Any],
    ) -> Any:
        """
        Try the existing API exposed by triggers.py.

        This method intentionally supports several common names
        without changing triggers.py.
        """

        manager = (
            self.trigger_manager
        )

        if manager is None:
            return None

        # --------------------------------------------------------
        # Preferred
        # --------------------------------------------------------

        for method_name in [
            "process_frame",
            "check_frame",
            "update",
            "process",
            "handle_frame",
            "evaluate",
        ]:

            method = getattr(
                manager,
                method_name,
                None,
            )

            if method is None:
                continue

            if not callable(
                method
            ):
                continue

            try:

                return method(
                    frame_record
                )

            except TypeError:

                try:

                    return method(
                        frame=frame_record
                    )

                except Exception:
                    continue

            except Exception as exc:

                print(
                    "[Agent] Trigger warning:",
                    exc,
                )

                return None

        return None

    # ============================================================
    # Manual trigger
    # ============================================================

    def run_manual(
        self,
        person_id: int | None = None,
        reason: str = "Manual Agent test.",
        context: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """
        Generate a MANUAL trigger.
        """

        trigger = {
            "trigger_type": "MANUAL",
            "trigger_event": "MANUAL",
            "person_id": person_id,
            "priority": "NORMAL",
            "risk_score": None,
            "frame_idx": None,
            "state": None,
            "reason": reason,
        }

        return self.process_trigger(
            trigger=trigger,
            context=context,
        )

    # ============================================================
    # Heartbeat
    # ============================================================

    def run_heartbeat(
        self,
        frame_idx: int | None = None,
        context: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """
        Generate a HEARTBEAT trigger.
        """

        trigger = {
            "trigger_type": "HEARTBEAT",
            "trigger_event": "HEARTBEAT",
            "person_id": None,
            "priority": "NORMAL",
            "risk_score": None,
            "frame_idx": frame_idx,
            "state": None,
            "reason": "Periodic scene heartbeat.",
        }

        return self.process_trigger(
            trigger=trigger,
            context=context,
        )

    # ============================================================
    # JSONL
    # ============================================================

    def process_jsonl(
        self,
        jsonl_path: str | Path,
        max_frames: int | None = None,
    ) -> list[dict[str, Any]]:
        """
        Process JSONL perception records.
        """

        path = Path(
            jsonl_path
        )

        if not path.exists():

            return [
                {
                    "ok": False,
                    "stage": "jsonl",
                    "error": (
                        f"File not found: {path}"
                    ),
                }
            ]

        results = []

        frame_count = 0

        with path.open(
            "r",
            encoding="utf-8",
        ) as file:

            for line in file:

                line = line.strip()

                if not line:
                    continue

                try:

                    frame_record = json.loads(
                        line
                    )

                except json.JSONDecodeError as exc:

                    results.append(
                        {
                            "ok": False,
                            "stage": "jsonl",
                            "error": (
                                f"Invalid JSON: {exc}"
                            ),
                        }
                    )

                    continue

                frame_results = (
                    self.process_frame(
                        frame_record
                    )
                )

                results.extend(
                    frame_results
                )

                frame_count += 1

                if (
                    max_frames is not None
                    and frame_count >= max_frames
                ):

                    break

        return results

    # ============================================================
    # Memory
    # ============================================================

    def _remember_frame(
        self,
        frame_record: dict[str, Any],
    ) -> None:

        try:

            self.memory.remember_frame(
                frame_record
            )

        except Exception as exc:

            print(
                f"[Agent] Memory warning: {exc}"
            )

    def _remember_trigger(
        self,
        trigger: dict[str, Any],
    ) -> None:

        frame_record = {
            "frame_idx": trigger.get(
                "frame_idx"
            ),
            "person_id": trigger.get(
                "person_id"
            ),
            "state": trigger.get(
                "state"
            ),
            "risk_score": trigger.get(
                "risk_score"
            ),
            "priority": trigger.get(
                "priority"
            ),
            "trigger_type": trigger.get(
                "trigger_type"
            ),
            "timestamp": datetime.now().isoformat(),
        }

        self._remember_frame(
            frame_record
        )

    # ============================================================
    # Context
    # ============================================================

    def build_scene_context(
        self,
    ) -> dict[str, Any]:

        try:

            digest = self.memory.digest(
                window_minutes=30
            )

        except Exception:

            digest = ""

        return {
            "memory_digest": digest,
            "generated_at": (
                datetime.now().isoformat()
            ),
        }

    def _build_context(
        self,
        trigger: dict[str, Any],
        context: dict[str, Any] | None,
    ) -> dict[str, Any]:

        base_context = (
            self.build_scene_context()
        )

        base_context[
            "trigger"
        ] = trigger

        if context:

            base_context[
                "external_context"
            ] = context

        return base_context

    # ============================================================
    # Trigger normalization
    # ============================================================

    def _normalize_trigger(
        self,
        trigger: dict[str, Any],
    ) -> dict[str, Any]:

        normalized = dict(
            trigger
        )

        # --------------------------------------------------------
        # trigger_type
        # --------------------------------------------------------

        if not normalized.get(
            "trigger_type"
        ):

            normalized[
                "trigger_type"
            ] = normalized.get(
                "type",
                normalized.get(
                    "event_type",
                    "UNKNOWN_EVENT",
                ),
            )

        # --------------------------------------------------------
        # trigger_event
        # --------------------------------------------------------

        if not normalized.get(
            "trigger_event"
        ):

            normalized[
                "trigger_event"
            ] = normalized.get(
                "event",
                normalized[
                    "trigger_type"
                ],
            )

        # --------------------------------------------------------
        # person_id
        # --------------------------------------------------------

        if (
            "person_id"
            not in normalized
        ):

            normalized[
                "person_id"
            ] = normalized.get(
                "pid"
            )

        # --------------------------------------------------------
        # risk_score
        # --------------------------------------------------------

        if (
            "risk_score"
            not in normalized
        ):

            normalized[
                "risk_score"
            ] = normalized.get(
                "risk"
            )

        # --------------------------------------------------------
        # priority
        # --------------------------------------------------------

        if not normalized.get(
            "priority"
        ):

            risk_score = normalized.get(
                "risk_score"
            )

            if (
                isinstance(
                    risk_score,
                    (int, float),
                )
                and risk_score >= 0.8
            ):

                normalized[
                    "priority"
                ] = "HIGH"

            elif (
                isinstance(
                    risk_score,
                    (int, float),
                )
                and risk_score >= 0.5
            ):

                normalized[
                    "priority"
                ] = "MEDIUM"

            else:

                normalized[
                    "priority"
                ] = "NORMAL"

        return normalized

    # ============================================================
    # Statistics
    # ============================================================

    def get_stats(
        self,
    ) -> dict[str, Any]:

        return dict(
            self.stats
        )


# ================================================================
# Standalone test
# ================================================================

if __name__ == "__main__":

    print("=" * 60)
    print("Testing DRRM Agent")
    print("=" * 60)

    # ------------------------------------------------------------
    # Create Agent
    # ------------------------------------------------------------

    agent = Agent(
        db_path="data/drrm.db"
    )

    # ------------------------------------------------------------
    # Show detected trigger class
    # ------------------------------------------------------------

    if agent.trigger_manager is not None:

        print(
            "\n[Agent] Trigger module:",
            type(
                agent.trigger_manager
            ).__name__,
        )

    else:

        print(
            "\n[Agent] Trigger module: "
            "not automatically detected"
        )

    # ------------------------------------------------------------
    # Test 1: HIGH_RISK
    # ------------------------------------------------------------

    print(
        "\n[1] HIGH_RISK trigger"
    )

    high_trigger = {
        "trigger_type": "HIGH_RISK",
        "trigger_event": "HIGH_RISK",
        "person_id": 1,
        "state": "Fall",
        "risk_score": 0.92,
        "priority": "HIGH",
        "frame_idx": 1204,
    }

    result = agent.process_trigger(
        high_trigger
    )

    print(
        json.dumps(
            result,
            indent=2,
            ensure_ascii=False,
        )
    )

    if not result.get(
        "ok"
    ):

        raise RuntimeError(
            "HIGH_RISK Agent test failed."
        )

    # ------------------------------------------------------------
    # Test 2: HEARTBEAT
    # ------------------------------------------------------------

    print(
        "\n[2] HEARTBEAT trigger"
    )

    heartbeat_result = (
        agent.run_heartbeat(
            frame_idx=500
        )
    )

    print(
        json.dumps(
            heartbeat_result,
            indent=2,
            ensure_ascii=False,
        )
    )

    if not heartbeat_result.get(
        "ok"
    ):

        raise RuntimeError(
            "HEARTBEAT Agent test failed."
        )

    # ------------------------------------------------------------
    # Test 3: MANUAL
    # ------------------------------------------------------------

    print(
        "\n[3] MANUAL trigger"
    )

    manual_result = (
        agent.run_manual(
            person_id=1,
            reason="Manual integration test.",
        )
    )

    print(
        json.dumps(
            manual_result,
            indent=2,
            ensure_ascii=False,
        )
    )

    if not manual_result.get(
        "ok"
    ):

        raise RuntimeError(
            "MANUAL Agent test failed."
        )

    # ------------------------------------------------------------
    # Statistics
    # ------------------------------------------------------------

    print(
        "\n[4] Agent statistics"
    )

    print(
        json.dumps(
            agent.get_stats(),
            indent=2,
            ensure_ascii=False,
        )
    )

    print(
        "\n" + "=" * 60
    )

    print(
        "Agent integration test completed successfully."
    )

    print(
        "=" * 60
    )
