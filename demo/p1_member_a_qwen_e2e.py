"""
demo/p1_member_a_qwen_e2e.py — 成员A 真实 VLM + LLM 端到端验证 (Stage5)
=======================================================================
仅作验证入口, 不进入业务主流程, 不修改任何架构。

真实运行要求:
  1. 提前在环境变量中设置两个 Key(见 .env.example, 本脚本不读 .env 文件):
       export VLM_API_KEY=xxx      # Qwen VLM
       export LLM_API_KEY=xxx      # Qwen LLM
  2. 提供一张灾害现场图片路径。

用法:
  python demo/p1_member_a_qwen_e2e.py --image <图片路径> [--person-events <json>] [--out outputs/e2e]

流程: 真实VLM → raw → VLM Parser → vlm_output → 真实LLM → raw → LLM Parser
      → llm_output → 逐一过 schemas/vlm_output.json 与 llm_output.json 校验。

输出:
  <out>/vlm_raw.json    VLM 原始返回(文本)
  <out>/vlm_output.json 标准化 vlm_output
  <out>/llm_raw.json    LLM 原始返回(文本)
  <out>/llm_output.json 标准化 llm_output
  (outputs/ 已被 .gitignore 忽略, 生成结果不会入库)

说明: 本文件为验证/冒烟脚本, 与 demo/p0_member_a_demo.py(纯 mock 链路)
      对应; 真实 Key 缺失或调用失败时按请求/API/返回格式分类打印错误。
"""

import argparse
import json
import os
import sys

import jsonschema

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from src.llm.client import LLMClient
from src.llm.parser import parse as parse_llm
from src.vlm.client import VLMClient
from src.vlm.parser import parse as parse_vlm

# 本阶段 B 侧尚未联调, person_events 使用固定 mock (数据仅作证据注入)
DEFAULT_PERSONS = [
    {"person_id": 1, "state": "Fall", "risk_score": 0.92, "priority": 1},
]

_SCHEMA_DIR = os.path.join(ROOT, "schemas")


def _load_schema(name: str):
    with open(os.path.join(_SCHEMA_DIR, f"{name}.json"), encoding="utf-8") as fh:
        return json.load(fh)


def validate_schema(name: str, data) -> None:
    """data 不满足 schemas/<name>.json 时抛 jsonschema.ValidationError。"""
    jsonschema.validate(data, _load_schema(name))


def _save_json(path: str, obj, indent: int = 2) -> None:
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(obj, fh, ensure_ascii=False, indent=indent)


def _save_raw(path: str, text: str) -> None:
    """原始模型返回(文本)直接落盘。"""
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)


def run_e2e(image_path: str, person_events, out_dir: str) -> dict:
    """执行 A 侧真实 VLM+LLM 端到端流程, 返回各阶段产物。"""
    os.makedirs(out_dir, exist_ok=True)

    # [1] 真实 VLM → 原始返回
    vlm_raw = VLMClient(provider="qwen").analyze(image_path)
    _save_raw(os.path.join(out_dir, "vlm_raw.json"), vlm_raw)

    # [2] VLM Parser → 标准化 vlm_output
    vlm_output = parse_vlm(vlm_raw).to_dict()
    _save_json(os.path.join(out_dir, "vlm_output.json"), vlm_output)
    validate_schema("vlm_output", vlm_output)

    # [3] 真实 LLM → 原始返回
    llm_raw = LLMClient(provider="qwen").infer(vlm_output, person_events)
    _save_raw(os.path.join(out_dir, "llm_raw.json"), llm_raw)

    # [4] LLM Parser → 标准化 llm_output
    llm_output = parse_llm(llm_raw).to_dict()
    _save_json(os.path.join(out_dir, "llm_output.json"), llm_output)
    validate_schema("llm_output", llm_output)

    return {"vlm_raw": vlm_raw, "vlm_output": vlm_output,
            "llm_raw": llm_raw, "llm_output": llm_output}


def _load_persons(path: str):
    with open(path, encoding="utf-8") as fh:
        data = json.load(fh)
    if not isinstance(data, list):
        raise ValueError("--person-events 需为人员事件 JSON 数组")
    return data


def main() -> int:
    parser = argparse.ArgumentParser(description="成员A 真实 VLM+LLM 端到端验证")
    parser.add_argument("--image", required=True, help="灾害现场图片路径")
    parser.add_argument("--person-events", default=None,
                        help="人员事件 JSON 数组文件路径; 缺省使用内置固定 mock")
    parser.add_argument("--out", default=os.path.join(ROOT, "outputs", "e2e"),
                        help="输出目录(默认 outputs/e2e)")
    args = parser.parse_args()

    person_events = (
        _load_persons(args.person_events) if args.person_events else DEFAULT_PERSONS
    )

    try:
        results = run_e2e(args.image, person_events, args.out)
    except Exception as exc:  # noqa: BLE001 — 验证脚本按类别打印并退出
        cls = type(exc).__name__
        if isinstance(exc, (FileNotFoundError, ValueError)):
            kind = "输入错误"
        elif exc.__class__.__name__.endswith("ConfigError"):
            kind = "配置错误(请检查相关 API Key 环境变量)"
        elif exc.__class__.__name__.endswith("ProviderError"):
            kind = "API 请求错误(网络/超时/HTTP/协议)"
        elif exc.__class__.__name__.endswith("ParseError"):
            kind = "返回格式错误(模型输出无法解析)"
        elif exc.__class__.__name__ == "ValidationError":
            kind = "Schema 校验失败"
        else:
            kind = "未知错误"
        print(f"[E2E 失败] {kind} | {cls}: {exc}")
        return 1

    vlm_ok = json.loads(results["vlm_raw"])
    print("=" * 40)
    print("Member A Qwen E2E completed successfully")
    print("=" * 40)
    print("VLM scene_type :", results["vlm_output"].get("scene_type"))
    print("VLM severity   :", results["vlm_output"].get("overall_severity"))
    print("LLM proposals  :", len(results["llm_output"].get("task_proposals", [])))
    print("Output dir     :", args.out)
    print("请注意: 返回内容真实与否取决于模型; 上述通过仅表示链路与契约正确。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())