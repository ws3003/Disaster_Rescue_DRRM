"""
tools/test_stable_id.py — Stable ID 关联器轻量测试 (无 pytest 依赖)
=================================================================
验证 StableIdAssociator 的跨帧 bbox 匹配与正份 id 分配。

运行:
    python tools/test_stable_id.py
"""

import os
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
SRC = os.path.join(ROOT, "src")
if SRC not in sys.path:
    sys.path.insert(0, SRC)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from edge_perception import StableIdAssociator  # noqa: E402

W, H = 1280, 720
_passed = 0
_failed = 0


def check(name, cond, detail=""):
    global _passed, _failed
    if cond:
        _passed += 1
        print(f"  [OK]   {name}")
    else:
        _failed += 1
        print(f"  [FAIL] {name}  {detail}")


def run():
    print("=== Stable ID Tests ===")

    # 测试1: 同一个 bbox 连续两帧 → id 一致
    print("\n-- test1: same bbox two frames --")
    t = StableIdAssociator()
    b1 = [ [10, 10, 110, 210] ]
    id1 = t.associate(b1, W, H)
    id2 = t.associate(b1, W, H)
    check("same bbox keeps id", id1[0] == id2[0], f"{id1} vs {id2}")

    # 测试2: 小范围移动(阈值内) → id 一致
    print("\n-- test2: small movement --")
    t = StableIdAssociator()
    a1 = t.associate([[100, 100, 200, 300]], W, H)[0]
    # 向左移 40px (cx 移 40/1280≈0.03 < 0.10)
    a2 = t.associate([[60, 100, 160, 300]], W, H)[0]
    check("small move keeps id", a1 == a2, f"{a1} vs {a2}")

    # 测试3: 大范围移动(超阈值) → 新 id
    print("\n-- test3: large movement --")
    t = StableIdAssociator()
    c1 = t.associate([[100, 100, 200, 300]], W, H)[0]
    # 向左移 400px (cx 移 400/1280≈0.31 > 0.10)
    c2 = t.associate([[-300, 100, -200, 300]], W, H)[0]
    check("large move new id", c1 != c2, f"{c1} vs {c2}")

    # 测试4: 两个 person → 两个不同 id
    print("\n-- test4: two persons --")
    t = StableIdAssociator()
    ids = t.associate([[10, 10, 100, 200], [300, 10, 400, 200]], W, H)
    check("two persons distinct", len(set(ids)) == 2, f"{ids}")

    # 测试5: bbox 字段存在 (在 EdgePerception 输出层面由调用方保证; 此处验证 track 返回数)
    print("\n-- test5: bbox presence (via event integration) --")
    t = StableIdAssociator()
    e1 = t.associate([[20, 20, 120, 220]], W, H)
    e2 = t.associate([[25, 25, 125, 225]], W, H)
    check("track count consistent", len(e1) == 1 and len(e2) == 1)
    # 验证 track 保存了 bbox (用于后续 events 输出)
    sid = e2[0]
    tr = t.tracks[sid]
    check("track holds bbox", len(tr.bbox) == 4, f"bbox={tr.bbox}")

    # 附加: 丢失 max_miss 帧后移除, 重现获新 id
    print("\n-- extra: miss then remove/recreate --")
    t = StableIdAssociator()
    m1 = t.associate([[10, 10, 110, 210]], W, H)[0]
    # 空帧 miss 计数 (模拟无人)
    for _ in range(t.max_miss + 1):
        t.associate([], W, H)
    check("track removed after max_miss", m1 not in t.tracks, f"tracks={list(t.tracks)}")
    m2 = t.associate([[10, 10, 110, 210]], W, H)[0]
    check("recreated with new id", m2 != m1, f"{m1} vs {m2}")

    # 附加: 同一 track 不共享给两个检测 (一帧两检测中心都近同一 track)
    print("\n-- extra: no id sharing --")
    t = StableIdAssociator()
    d1 = t.associate([[50, 50, 100, 160]], W, H)
    d2 = t.associate([[60, 55, 110, 165], [400, 400, 500, 560]], W, H)
    check("ids not shared", d2[0] != d2[1], f"{d2}")
    # 且与上一帧最近者沿用
    check("closest reuses id", d2[0] == d1[0], f"{d1} vs {d2}")

    print(f"\n=== PASSED={_passed} FAILED={_failed} ===")
    return 0 if _failed == 0 else 1


if __name__ == "__main__":
    sys.exit(run())