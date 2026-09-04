"""
Disaster_Rescue_DRRM V0.1.5 Step5 — DRRM 风险评分标定实验台
============================================================
职责: 在不修改任何源码的前提下, 用真实 pipeline 输出 + 合成证据, 比较:
  实验A  F6 基线   f6 = clip(det_conf*pose_vis, w_min, 1)
         F6 软化   f6 = 0.5 + 0.5*(det_conf*pose_vis)          (clip 到 [.,1])
         聚合保持设计: R = f1 × f6, f1 = 0.5 + (base-0.5)*conf (risk.py 公式复现)
  实验B  priority 阈值方案:
         S0 当前  HIGH>=0.75 / MEDIUM>=0.50
         S1       HIGH>=0.60 / MEDIUM>=0.40
         S2       HIGH>=0.65 / MEDIUM>=0.45

Part A  真实 5 图: 逐 civilian 框两种 F6 下的 risk 与 priority(S0/S2)
Part B  五状态合成网格: Fall/Lying 无真实样本, 用代表证据
        (raw=det*vis: 弱0.45/中0.60/清晰0.75; conf: 0.35/0.65/1.0)
Part C  阈值方案语义检查汇总

运行: venv\\Scripts\\python.exe tests\\calibrate_risk.py   (只读, 不写文件)
"""

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))

import cv2  # noqa: E402

import evaluate_states as ev  # noqa: E402
from detector import Detector  # noqa: E402
from pose import PoseEstimator  # noqa: E402
from risk import NEUTRAL_RISK, Riskevaluator  # noqa: E402
from state import StateClassifier  # noqa: E402

DESIGN_ORDER = ["Fall", "Lying", "Help-Waving", "Sitting", "Standing"]
W_MIN = 0.25

# priority 阈值方案 (high, medium)
SCHEMES = {
    "S0(当前 0.75/0.50)": (0.75, 0.50),
    "S1(0.60/0.40)": (0.60, 0.40),
    "S2(0.65/0.45)": (0.65, 0.45),
    "S3(0.60/0.45)": (0.60, 0.45),
}


def f1_of(base, conf):
    return NEUTRAL_RISK + (base - NEUTRAL_RISK) * conf


def f6_baseline(raw):
    return max(W_MIN, min(1.0, raw))


def f6_soft(raw):
    return min(1.0, 0.5 + 0.5 * raw)  # 用户指定实验公式; 下限自然为 0.5


def priority(score, hi, med):
    return "HIGH" if score >= hi else ("MEDIUM" if score >= med else "LOW")


def collect_real(labels, images_dir, det, pe, clf, rk):
    """跑真实 pipeline, 返回逐框 [(file, state, conf, raw_evidence, f1, gt)]。"""
    rows = []
    for item in labels:
        fname, gt = item["file"], item["gt_state"]
        img = cv2.imread(str(images_dir / fname))
        dets = det.detect_frame(img)
        civilians = [d for d in dets if d.is_person and d.class_name == "civilian"]
        poses = pe.estimate_persons(img, civilians)
        for d, p in zip(civilians, poses):
            if not (p and p.is_ok):
                continue
            sr = clf.classify(p)
            if sr is None:
                continue
            raw = float(d.confidence) * float(getattr(p, "visibility_mean", 0.0) or 0.0)
            base = rk.state_risk.get(sr.state, NEUTRAL_RISK)
            rows.append({
                "file": fname, "gt": gt, "state": sr.state,
                "conf": float(sr.confidence), "raw": raw,
                "f1": f1_of(base, float(sr.confidence)),
            })
    return rows


def part_a(rows):
    print("=" * 118)
    print("Part A  真实图片: F6 基线 vs F6 软化 (R = f1 × f6)")
    print("=" * 118)
    h2, m2 = SCHEMES["S3(0.60/0.45)"]
    print(f"  {'图片':<22}{'state':<12}{'conf':>5}{'raw':>6} | "
          f"{'f6基':>5}{'R基':>6}{'prio(S0)':>9} | {'f6软':>5}{'R软':>6}{'prio(S0)':>9}{'prio(S3)':>9}")
    base_risks, soft_risks = [], []
    for r in rows:
        f6b, f6s = f6_baseline(r["raw"]), f6_soft(r["raw"])
        rb, rs = r["f1"] * f6b, r["f1"] * f6s
        base_risks.append(rb)
        soft_risks.append(rs)
        print(f"  {r['file']:<22}{r['state']:<12}{r['conf']:>5.2f}{r['raw']:>6.3f} | "
              f"{f6b:>5.3f}{rb:>6.3f}{priority(rb, 0.75, 0.50):>9} | "
              f"{f6s:>5.3f}{rs:>6.3f}{priority(rs, 0.75, 0.50):>9}{priority(rs, h2, m2):>9}")

    def dist(xs):
        return f"min={min(xs):.3f} max={max(xs):.3f} mean={sum(xs)/len(xs):.3f}"

    print(f"\n  风险分布  基线 F6 : {dist(base_risks)}")
    print(f"            软化 F6 : {dist(soft_risks)}")
    for tag, xs, (hi, me) in [("基线+S0", base_risks, (0.75, 0.50)),
                              ("软化+S0", soft_risks, (0.75, 0.50)),
                              ("软化+S3", soft_risks, (h2, m2))]:
        bands = {"HIGH": 0, "MEDIUM": 0, "LOW": 0}
        for x in xs:
            bands[priority(x, hi, me)] += 1
        print(f"  priority 计数 [{tag}]: HIGH={bands['HIGH']} MEDIUM={bands['MEDIUM']} LOW={bands['LOW']}")


def part_b(rk):
    print("\n" + "=" * 118)
    print("Part B  五状态合成证据网格 (Fall/Lying 无真实样本; raw=det_conf*pose_vis)")
    print("=" * 118)
    confs = [0.35, 0.65, 1.0]
    raws = [("弱证据", 0.45), ("中证据", 0.60), ("清晰", 0.75)]
    h2, m2 = SCHEMES["S3(0.60/0.45)"]

    for f6_name, f6_fn in [("F6 基线", f6_baseline), ("F6 软化", f6_soft)]:
        print(f"\n  --- {f6_name} (R=f1×f6; 括号内为 priority: S0当前 / S3提案) ---")
        for ev_name, raw in raws:
            f6 = f6_fn(raw)
            print(f"\n  [{ev_name} raw={raw:.2f} -> f6={f6:.3f}]")
            print(f"    conf | " + " | ".join(f"{s:<10}" for s in DESIGN_ORDER))
            for c in confs:
                cells = []
                for s in DESIGN_ORDER:
                    r = f1_of(rk.state_risk[s], c) * f6
                    cells.append(f"{r:6.3f} {priority(r,0.75,0.50)[0]}/{priority(r,h2,m2)[0]}")
                print(f"    {c:>5.2f} | " + " |".join(cells))
            print("    (单元格: R值 S0等级/S3等级; H=HIGH M=MEDIUM L=LOW)")


def part_c(rk):
    print("\n" + "=" * 118)
    print("Part C  阈值方案语义检查 (代表场景: conf=0.65, 中证据 raw=0.60; F6 软化)")
    print("=" * 118)
    h2, m2 = SCHEMES["S3(0.60/0.45)"]
    f6 = f6_soft(0.60)
    expect = {
        "Fall": "HIGH/MEDIUM (最危急)",
        "Lying": "HIGH/MEDIUM (失去行动能力)",
        "Help-Waving": "MEDIUM+ (主动求救)",
        "Sitting": "LOW/MEDIUM (低危)",
        "Standing": "LOW (安全)",
    }
    print(f"  {'state':<12}{'base':>5}{'f1':>6}{'R':>7} | " +
          " ".join(f"{name:>18}" for name in SCHEMES) + f" | 救援语义期望")
    for s in DESIGN_ORDER:
        f1 = f1_of(rk.state_risk[s], 0.65)
        r = f1 * f6
        labels = [f"{priority(r, hi, me):>18}" for hi, me in SCHEMES.values()]
        print(f"  {s:<12}{rk.state_risk[s]:>5.2f}{f1:>6.3f}{r:>7.3f} | "
              + " ".join(labels) + f" | {expect[s]}")

    # 边界可达性 (F6 软化, conf=1)
    print("\n  分级可达性 (F6 软化, conf=1, f6 随证据):")
    print(f"  {'state':<12}{'R上限(f6=1)':>12}{'清晰raw.75':>11}{'中raw.60':>10}{'弱raw.45':>10}  S3下最高级")
    for s in DESIGN_ORDER:
        b = rk.state_risk[s]
        r_clear, r_mid, r_weak = b * f6_soft(0.75), b * f6_soft(0.60), b * f6_soft(0.45)
        rank_of = {"HIGH": 2, "MEDIUM": 1, "LOW": 0}
        top = max((priority(r, h2, m2) for r in (r_clear, r_mid, r_weak)), key=lambda x: rank_of[x])
        print(f"  {s:<12}{b:>12.3f}{priority(r_clear,h2,m2)+f' {r_clear:.3f}':>11}"
              f"{priority(r_mid,h2,m2)+f' {r_mid:.3f}':>10}{priority(r_weak,h2,m2)+f' {r_weak:.3f}':>10}  S3 可达最高级: {top}")


def main():
    parser = argparse.ArgumentParser(description="V0.1.5 Step5 DRRM 风险标定实验台")
    parser.add_argument("--config", default=str(ROOT / "config" / "config.yaml"))
    parser.add_argument("--images", default=str(ROOT / "data" / "test_images"))
    parser.add_argument("--labels", default=str(ROOT / "tests" / "test_labels.json"))
    args = parser.parse_args()

    labels = ev.load_labels(Path(args.labels))
    det = Detector(args.config)
    pe = PoseEstimator(args.config)
    clf = StateClassifier()
    rk = Riskevaluator(args.config)
    print(f"[device] {det.device}  state_risk={dict(rk.state_risk)}")

    rows = collect_real(labels, Path(args.images), det, pe, clf, rk)
    part_a(rows)
    part_b(rk)
    part_c(rk)
    print("\n[done] 实验结束, 未修改任何源码")


if __name__ == "__main__":
    main()
