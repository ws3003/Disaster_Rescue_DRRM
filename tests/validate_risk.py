"""
Disaster_Rescue_DRRM V0.1.5 Step4 — DRRM 风险模型验证脚本
=========================================================
职责: 验证 src/risk.py (F1/F6/risk) 与 src/ranking.py (排序/priority) 是否符合
      设计预期 (危险序 Fall > Lying > Help-Waving > Sitting > Standing)。
      只做验证与数据输出, 不修改任何算法源码, 不写文件。

Part 1  实测层: data/test_images 5 张图跑完整 pipeline, 逐 civilian 框输出
        state / confidence / det_conf / pose_vis / f1 / f6 / risk / priority / rank;
        另把全图 civilian 合并成"跨图虚拟场景"排一次全局榜。
Part 2  设计符合性 (合成证据, 不需图片):
        2a 先验单调性  2b 等证据排序  2c 同分 tie-break
        2d 置信度×证据网格上的危险序反转边界  2e priority 分级可达性
Part 3  Help-Waving(004) 低风险归因: f1/f6 分解 + 反事实阈值。

运行: venv\\Scripts\\python.exe tests\\validate_risk.py
      (路径基于项目根自动定位, 与当前工作目录无关)
退出码: 2a/2b/2c 断言失败 -> 1; 全过 -> 0。
"""

import argparse
import sys
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))  # 复用 evaluate_states

import cv2  # noqa: E402

import evaluate_states as ev  # noqa: E402
from detector import Detector  # noqa: E402
from pose import PoseEstimator  # noqa: E402
from ranking import STATE_ORDER, PersonEvidence, _priority, rank  # noqa: E402
from risk import NEUTRAL_RISK, Riskevaluator, RiskResult  # noqa: E402
from state import StateClassifier  # noqa: E402

# 设计危险序 (最危险 -> 最安全)
DESIGN_ORDER = ["Fall", "Lying", "Help-Waving", "Sitting", "Standing"]


# ---------------------------------------------------------------------------
# Part 1 — 实测层
# ---------------------------------------------------------------------------
def part1_real(labels, images_dir, det, pe, clf, rk):
    """跑完整 pipeline, 返回 (records, all_evidences); records 为逐框明细 dict。"""
    print("=" * 104)
    print("Part 1  实测层: data/test_images 完整 pipeline (detector->pose->state->risk->rank)")
    print("=" * 104)
    records = []
    all_ev = []  # 跨图虚拟场景证据 (带 tag)
    for item in labels:
        fname, gt = item["file"], item["gt_state"]
        img = cv2.imread(str(images_dir / fname))
        dets = det.detect_frame(img)
        person_dets = [d for d in dets if d.is_person]
        pid_of = {id(d): i for i, d in enumerate(person_dets, start=1)}
        civilians = [d for d in person_dets if d.class_name == "civilian"]
        rescuers = [d for d in person_dets if d.class_name == "rescuer"]
        poses = pe.estimate_persons(img, civilians)

        evidences = []
        rec_by_det = {}
        for d, p in zip(civilians, poses):
            sr = clf.classify(p) if (p and p.is_ok) else None
            rr = rk.assess(d, p, sr) if (p and sr) else None
            if rr is None:
                records.append({
                    "file": fname, "gt": gt, "pid": pid_of[id(d)],
                    "state": "(skip)", "conf": None, "det_conf": float(d.confidence),
                    "pose_vis": None, "f1": None, "f6": None, "risk": None,
                    "priority": "-", "rank": "-",
                })
                continue
            vis = float(getattr(p, "visibility_mean", 0.0) or 0.0)
            ev_obj = PersonEvidence(detection=d, state=sr, risk=rr, pose_vis=vis)
            evidences.append(ev_obj)
            rec = {
                "file": fname, "gt": gt, "pid": pid_of[id(d)],
                "state": rr.state, "conf": float(sr.confidence),
                "det_conf": float(d.confidence), "pose_vis": vis,
                "f1": rr.f1, "f6": rr.f6, "risk": rr.risk_score,
                "priority": _priority(rr.risk_score), "rank": None,
                "tag": f"{fname}#pid{pid_of[id(d)]}",
            }
            rec_by_det[id(d)] = rec
            records.append(rec)
            all_ev.append((rec["tag"], ev_obj))

        ranked = rank(evidences)
        for r in ranked:
            rec_by_det[id(r.detection)]["rank"] = r.rank
        print(f"\n{fname}  (GT={gt}, civilian={len(civilians)}, rescuer={len(rescuers)} 不参与排序)")
        print(f"  {'pid':>3} {'state':<12} {'conf':>5} {'det_conf':>8} {'pose_vis':>8} "
              f"{'f1':>6} {'f6':>6} {'risk':>6} {'priority':>8} {'rank':>4}")
        for r in records:
            if r["file"] != fname:
                continue
            if r["risk"] is None:
                print(f"  {r['pid']:>3} {r['state']:<12} {'-':>5} {r['det_conf']:>8.3f} "
                      f"{'-':>8} {'-':>6} {'-':>6} {'-':>6} {r['priority']:>8} {r['rank']:>4}")
            else:
                print(f"  {r['pid']:>3} {r['state']:<12} {r['conf']:>5.2f} {r['det_conf']:>8.3f} "
                      f"{r['pose_vis']:>8.3f} {r['f1']:>6.3f} {r['f6']:>6.3f} {r['risk']:>6.3f} "
                      f"{r['priority']:>8} {r['rank']:>4}")

    # 跨图虚拟场景
    print("\n" + "-" * 104)
    print("跨图虚拟场景: 全部实测 civilian 合并为同一场景排序 (展示真实输出下的全局榜)")
    print("-" * 104)
    global_ranked = rank([e for _, e in all_ev])
    tag_by_det = {id(e.detection): tag for tag, e in all_ev}
    for r in global_ranked:
        print(f"  #{r.rank}  {tag_by_det[id(r.detection)]:<26} state={r.state:<12} "
              f"risk={r.risk_score:.3f} priority={r.priority:<6} f1={r.details['f1']} f6={r.details['f6']}")
    if not global_ranked:
        print("  (无有效人员)")
    return records


# ---------------------------------------------------------------------------
# Part 2 — 设计符合性 (合成证据)
# ---------------------------------------------------------------------------
def _synth_evidence(state: str, f1: float, f6: float, risk: float, det_conf: float = 0.9,
                    pose_vis: float = 0.9):
    """构造合成 PersonEvidence (SimpleNamespace 桩, 不调用任何算法)。"""
    det = SimpleNamespace(confidence=det_conf, class_name="civilian")
    st = SimpleNamespace(state=state)
    rr = RiskResult(state=state, f1=f1, f6=f6, risk_score=risk)
    return PersonEvidence(detection=det, state=st, risk=rr, pose_vis=pose_vis)


def part2_design(rk):
    print("\n" + "=" * 104)
    print("Part 2  设计符合性检查 (合成证据, 不依赖图片/模型输出)")
    print("=" * 104)
    ok = True
    bases = rk.state_risk

    # 2a 先验单调性
    priors = sorted(bases.items(), key=lambda kv: -kv[1])
    order_by_prior = [s for s, _ in priors]
    pass_2a = order_by_prior == DESIGN_ORDER
    ok &= pass_2a
    print(f"\n[2a] 状态先验危险度单调性: {'PASS' if pass_2a else 'FAIL'}")
    for s in DESIGN_ORDER:
        print(f"     {s:<12} base={bases[s]:.2f}  STATE_ORDER={STATE_ORDER[s]}")
    if not pass_2a:
        print(f"     实际先验序: {order_by_prior}")

    # 2b 等证据排序 (conf=1 -> f1=base; f6=1 -> risk=base)
    evs = [_synth_evidence(s, f1=bases[s], f6=1.0, risk=bases[s]) for s in DESIGN_ORDER]
    got = [r.state for r in rank(evs)]
    pass_2b = got == DESIGN_ORDER
    ok &= pass_2b
    print(f"\n[2b] 等证据(conf=1,f6=1)五状态排序: {'PASS' if pass_2b else 'FAIL'}")
    print(f"     期望 {DESIGN_ORDER}")
    print(f"     实际 {got}")

    # 2c 同分 tie-break (risk 完全相同, 状态不同 -> 按 STATE_ORDER)
    evs = [_synth_evidence(s, f1=0.5, f6=0.6, risk=0.30) for s in
           ["Standing", "Help-Waving", "Fall", "Sitting", "Lying"]]  # 故意打乱
    got = [r.state for r in rank(evs)]
    pass_2c = got == DESIGN_ORDER
    ok &= pass_2c
    print(f"\n[2c] risk 同分时状态危险序 tie-break: {'PASS' if pass_2c else 'FAIL'}")
    print(f"     期望 {DESIGN_ORDER}")
    print(f"     实际 {got}")

    # 2d 反转边界网格
    print("\n[2d] 危险序反转边界: risk = f1(conf) × f6 网格 "
          "(f1 = 0.5 + (base-0.5)×conf)")
    confs = [0.35, 0.65, 1.0]
    f6s = [0.25, 0.50, 0.70, 1.00]
    header = "     conf  f6  | " + " ".join(f"{s[:6]:>7}" for s in DESIGN_ORDER) + " | 实际排序(高->低)"
    print(header)
    inversions = []
    for c in confs:
        for f6 in f6s:
            scores = {}
            for s in DESIGN_ORDER:
                f1 = NEUTRAL_RISK + (bases[s] - NEUTRAL_RISK) * c
                scores[s] = round(f1 * f6, 3)
            order = sorted(DESIGN_ORDER, key=lambda s: -scores[s])
            mark = "OK " if order == DESIGN_ORDER else "INV"
            if order != DESIGN_ORDER:
                inversions.append((c, f6, order))
            print(f"     {c:.2f} {f6:.2f} | " + " ".join(f"{scores[s]:>7.3f}" for s in DESIGN_ORDER)
                  + f" | [{mark}] {', '.join(s[:4] for s in order)}")
    print(f"\n     反转组合数: {len(inversions)} / {len(confs)*len(f6s)}")
    for c, f6, order in inversions:
        print(f"       conf={c:.2f} f6={f6:.2f}: {', '.join(order)}")

    # 2e priority 分级可达性 (V0.1.5 Step5 标定后: HIGH>=0.60 / MEDIUM>=0.45)
    print("\n[2e] priority 分级可达性 (HIGH>=0.60 / MEDIUM>=0.45 / LOW 其余; Step5 标定后)")
    print(f"     {'state':<12} {'base':>5} {'risk上限(conf1,f6=1)':>20} "
          f"{'f6=0.85':>9} {'f6=0.80':>9} {'f6=0.70':>9}  可达分级")
    for s in DESIGN_ORDER:
        b = bases[s]
        r70, r60, r50 = b * 0.85, b * 0.80, b * 0.70
        bands = []
        if b >= 0.60:
            bands.append("HIGH(需f6>=%.2f)" % (0.60 / b))
        if b >= 0.45:
            bands.append("MEDIUM(需f6>=%.2f)" % (0.45 / b))
        bands.append("LOW")
        print(f"     {s:<12} {b:>5.2f} {b:>20.3f} {_priority(r70)+f' {r70:.3f}':>9} "
              f"{_priority(r60)+f' {r60:.3f}':>9} {_priority(r50)+f' {r50:.3f}':>9}  {'; '.join(bands)}")
    return ok


# ---------------------------------------------------------------------------
# Part 3 — Help-Waving 低风险归因
# ---------------------------------------------------------------------------
def part3_help_attribution(records, rk):
    print("\n" + "=" * 104)
    print("Part 3  Help-Waving(004) 低风险归因: f1/f6 分解与反事实阈值")
    print("=" * 104)
    rec = next((r for r in records if r["file"] == "004_help.jpg" and r["risk"] is not None), None)
    if rec is None:
        print("  004 无有效风险结果, 跳过")
        return
    base = rk.state_risk["Help-Waving"]
    print(f"\n  实测: state={rec['state']} conf={rec['conf']:.2f} det_conf={rec['det_conf']:.3f} "
          f"pose_vis={rec['pose_vis']:.3f}")
    print(f"        f1={rec['f1']:.3f}  f6={rec['f6']:.3f} = det_conf×pose_vis = "
          f"{rec['det_conf']:.3f}×{rec['pose_vis']:.3f}")
    print(f"        risk={rec['risk']:.3f}  priority={rec['priority']}  "
          f"(先验 base={base}, 若 conf=1 且 f6=1 风险应为 {base:.2f})")

    print(f"\n  反事实 A — 固定实测 f6={rec['f6']:.3f}, 仅升 state confidence:")
    print(f"     {'conf':>5} {'f1':>6} {'risk':>6} {'priority':>8}")
    for c in [0.35, 0.50, 0.65, 0.80, 1.0]:
        f1 = NEUTRAL_RISK + (base - NEUTRAL_RISK) * c
        risk = f1 * rec["f6"]
        print(f"     {c:>5.2f} {f1:>6.3f} {risk:>6.3f} {_priority(risk):>8}")

    print(f"\n  反事实 B — 固定 conf=1.0, 仅升 f6 (Step5 软化后 f6∈[0.5,1.0]):")
    print(f"     {'f6':>5} {'risk':>6} {'priority':>8}")
    for f6 in [0.50, 0.62, 0.70, 0.81, 0.90, 1.00]:
        risk = base * f6
        print(f"     {f6:>5.2f} {risk:>6.3f} {_priority(risk):>8}")

    f6_for_med = 0.45 / base
    f6_for_high = 0.60 / base
    conf_for_med = ((0.45 / rec["f6"]) - NEUTRAL_RISK) / (base - NEUTRAL_RISK)
    print(f"\n  阈值结论 (Help-Waving base={base}):")
    print(f"     conf=1 时: 进 MEDIUM 需 f6>={f6_for_med:.3f}; 进 HIGH 需 f6>={f6_for_high:.3f}"
          f"{' (HIGH 不可达)' if f6_for_high > 1 else ''}")
    print(f"     实测 f6={rec['f6']:.3f} 时: 进 MEDIUM 需 conf>={conf_for_med:.2f}"
          f"{' (不可达)' if conf_for_med > 1 else ''}")

    # 对照: Sitting
    rec_s = next((r for r in records if r["file"] == "005_multi_person.jpg" and r["risk"] is not None), None)
    if rec_s:
        bs = rk.state_risk["Sitting"]
        print(f"\n  对照 Sitting(005): base={bs}, conf={rec_s['conf']:.2f}, f6={rec_s['f6']:.3f}, "
              f"risk={rec_s['risk']:.3f} ({rec_s['priority']}); conf=1/f6=1 上限也仅 {bs:.2f} -> "
              f"Sitting 理论最高 priority={_priority(bs)}")


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(description="V0.1.5 Step4 DRRM 风险模型验证")
    parser.add_argument("--config", default=str(ROOT / "config" / "config.yaml"))
    parser.add_argument("--images", default=str(ROOT / "data" / "test_images"))
    parser.add_argument("--labels", default=str(ROOT / "tests" / "test_labels.json"))
    args = parser.parse_args()

    labels = ev.load_labels(Path(args.labels))
    images_dir = Path(args.images)
    det = Detector(args.config)
    pe = PoseEstimator(args.config)
    clf = StateClassifier()
    rk = Riskevaluator(args.config)
    print(f"[device] {det.device}")
    print(f"[config] state_risk={dict(rk.state_risk)}  w_min={rk.w_min}")

    records = part1_real(labels, images_dir, det, pe, clf, rk)
    design_ok = part2_design(rk)
    part3_help_attribution(records, rk)

    print("\n" + "=" * 104)
    print(f"设计符合性总结论: {'PASS — ranking/risk 逻辑与设计一致' if design_ok else 'FAIL — 见上方断言'}")
    print("=" * 104)
    return 0 if design_ok else 1


if __name__ == "__main__":
    sys.exit(main())
