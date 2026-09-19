"""
demo/p0_member_a_demo.py — 成员A P0 Stage1 Mock 链路演示
=========================================================
人工可运行入口, 演示成员A的完整 Mock 链路:

  VLMClient(mock) → VLM raw → VLM Parser → vlm_output
       ↓ (vlm_output + person_events)
  LLMClient(mock) → LLM raw → LLM Parser → llm_output

不连接任何真实模型, 不需要 API Key, 不产生网络请求。
person_events 读取仓库现有 data/mock/person_event.json (不复制数据)。
运行: venv/Scripts/python.exe demo/p0_member_a_demo.py
"""

import json
import os
import sys

# 项目根目录注入 sys.path (demo/ 不是包)
ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from src.llm.client import LLMClient
from src.llm.parser import parse as parse_llm_output
from src.vlm.client import VLMClient
from src.vlm.parser import parse as parse_vlm_output

# Mock 模式下的占位图片输入 (VLMClient.analyze 支持任意占位值)
IMAGE_INPUT = "mock_disaster_scene"

# 人员事件来源: 仓库现有 mock 文件
PERSON_EVENT_PATH = os.path.join(ROOT, "data", "mock", "person_event.json")


def load_person_events(path: str = PERSON_EVENT_PATH):
    """读取 data/mock/person_event.json 并规整为人员事件列表。

    文件结构为 {"person_event": {...}, "note": ...}, 此处取出单人员
    事件并包装成列表, 供 LLMClient.infer 消费。
    """
    with open(path, encoding="utf-8") as fh:
        data = json.load(fh)
    return [data["person_event"]]


def run_pipeline():
    """执行完整 Mock 链路, 返回各阶段产物 (供测试复用, 不打印)。"""
    # [1] VLM Mock → raw → Parser → vlm_output
    raw_vlm = VLMClient(provider="mock").analyze(IMAGE_INPUT)
    vlm_output = parse_vlm_output(raw_vlm).to_dict()

    # [2] person_events (来自 data/mock/person_event.json)
    person_events = load_person_events()

    # [3] LLM Mock → raw → Parser → llm_output
    raw_llm = LLMClient(provider="mock").infer(
        vlm_output=vlm_output, person_events=person_events
    )
    llm_output = parse_llm_output(raw_llm).to_dict()

    return raw_vlm, vlm_output, person_events, raw_llm, llm_output


def _pretty(obj) -> str:
    return json.dumps(obj, ensure_ascii=False, indent=2)


def main() -> None:
    raw_vlm, vlm_output, person_events, raw_llm, llm_output = run_pipeline()

    print("=" * 40)
    print("Disaster_Rescue_DRRM V4.0")
    print("Member A P0 Stage 1 Demo")
    print("=" * 40)

    print("\n[1] VLM Mock")
    print("Raw Output:")
    if isinstance(raw_vlm, str):
        print(json.dumps(json.loads(raw_vlm), ensure_ascii=False, indent=2))
    else:
        print(_pretty(raw_vlm))
    print("\nParsed vlm_output:")
    print(_pretty(vlm_output))

    print("\n[2] Person Events")
    print("Loaded:")
    print(_pretty(person_events))

    print("\n[3] LLM Mock")
    print("Raw Output:")
    if isinstance(raw_llm, str):
        print(json.dumps(json.loads(raw_llm), ensure_ascii=False, indent=2))
    else:
        print(_pretty(raw_llm))
    print("\nParsed llm_output:")
    print(_pretty(llm_output))

    print("\n" + "=" * 40)
    print("Member A pipeline completed successfully")
    print("=" * 40)


if __name__ == "__main__":
    main()