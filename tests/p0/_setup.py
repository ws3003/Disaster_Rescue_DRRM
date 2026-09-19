"""tests/p0 共享工具: 将项目根目录加入 sys.path, 复用 mock/schema 加载。"""

import json
import os
import sys

# 项目根目录 (Disaster_Rescue_DRRM 的仓库根)
ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

DATA_MOCK = os.path.join(ROOT, "data", "mock")
SCHEMAS = os.path.join(ROOT, "schemas")


def load_json(relpath: str):
    """按仓库相对路径加载 JSON 文件。"""
    with open(os.path.join(ROOT, *relpath.split("/")), encoding="utf-8") as fh:
        return json.load(fh)


def load_mock(name: str):
    """加载 data/mock/<name>.json。"""
    return load_json(os.path.join("data", "mock", f"{name}.json"))


def load_schema(name: str):
    """加载 schemas/<name>.json。"""
    return load_json(os.path.join("schemas", f"{name}.json"))