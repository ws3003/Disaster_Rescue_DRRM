"""
Disaster_Rescue_DRRM V0.2-M0 — 冒烟测试视频合成
================================================
用 data/test_images/ 现有 5 张测试图合成一个短视频, 作为视频回放框架的
测试素材 (当前无真实灾场视频; 合成视频每帧内容与某张测试图完全相同,
因此视频模式的感知结果必须与图片模式逐位一致 — 见 test_video_replay.py)。

合成方式:
  5 张图按文件名排序, 每张图重复 FRAMES_PER_IMAGE 帧;
  所有帧统一 resize 到固定尺寸 SMOKE_SIZE;
  HFYU (HuffYUV) 无损编码 .avi, fps=12 -> 共 60 帧 / 5 秒, 每张图占 1 秒一个段落。
  无损编码使解码帧与输入像素逐位一致 (实测 maxdiff=0), golden 测试可严格比对;
  mp4v 等有损编码会引入压缩噪声 (实测 maxdiff≈59), 不用于框架级 golden。

输出: outputs/test_assets/smoke_video.avi  (outputs/ 已 gitignore, 不入库)

运行: venv\\Scripts\\python.exe tests\\make_smoke_video.py
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import cv2  # noqa: E402
import numpy as np  # noqa: E402

IMAGES_DIR = ROOT / "data" / "test_images"
OUT_VIDEO = ROOT / "outputs" / "test_assets" / "smoke_video.avi"
FRAMES_PER_IMAGE = 12
FPS = 12
CODEC = "HFYU"          # HuffYUV 无损 (OpenCV ffmpeg 构建自带; roundtrip 像素逐位一致)
SMOKE_SIZE = (720, 960)  # (w, h) 统一画布尺寸, 控制无损视频体积


def letterbox(img, size=SMOKE_SIZE, pad_color=(0, 0, 0)):
    """
    等比缩放 + 黑边填充到目标画布 (保持长宽比, 不拉伸人体几何)。
    测试图混有竖图(1440x1920)与横图(708x531), 直接 resize 会破坏姿态几何。
    纵向【底部对齐】: 人物脚部贴近画布底边, 黑边只在上方 —
    pose.py 的脚部方向 ROI 扩边(3x)因此只落在真实画面内, 不伸进黑边;
    (居中填充曾导致 005 坐姿图脚下大片黑边, MediaPipe 在 ROI 内找不到关键点)。
    视频写入与 golden 参考帧必须使用同一变换, 保证像素一致。
    """
    w, h = size
    ih, iw = img.shape[:2]
    scale = min(w / iw, h / ih)
    nw, nh = int(round(iw * scale)), int(round(ih * scale))
    resized = cv2.resize(img, (nw, nh))
    canvas = np.full((h, w, 3), pad_color, dtype=img.dtype)
    x0 = (w - nw) // 2
    y0 = h - nh  # 底部对齐
    canvas[y0:y0 + nh, x0:x0 + nw] = resized
    return canvas


def build_smoke_video(out_path: Path = OUT_VIDEO,
                      images_dir: Path = IMAGES_DIR,
                      frames_per_image: int = FRAMES_PER_IMAGE,
                      fps: int = FPS):
    """
    合成冒烟视频。返回 (out_path, segments):
      segments = [{"image": 图片名, "stem": 主名, "start": 起始帧, "end": 结束帧(不含)}, ...]
    """
    img_paths = sorted(p for p in images_dir.iterdir()
                       if p.suffix.lower() in (".jpg", ".jpeg", ".png", ".bmp"))
    if not img_paths:
        raise FileNotFoundError(f"未找到测试图片: {images_dir}")

    frames = []
    for p in img_paths:
        img = cv2.imread(str(p))
        if img is None:
            print(f"[skip] 无法读取: {p}")
            continue
        frames.append((p, img))
    if not frames:
        raise RuntimeError("没有可用的测试图片帧")

    # 所有帧统一 resize 到固定尺寸 (VideoWriter 要求全帧同尺寸; 也控制无损视频体积)
    size = SMOKE_SIZE

    out_path.parent.mkdir(parents=True, exist_ok=True)
    writer = cv2.VideoWriter(str(out_path), cv2.VideoWriter_fourcc(*CODEC), fps, size)
    if not writer.isOpened():
        raise RuntimeError(f"无法创建 VideoWriter: {out_path} (codec={CODEC})")

    segments = []
    cursor = 0
    for p, img in frames:
        img = letterbox(img, size)  # 等比缩放+黑边, 保持长宽比
        for _ in range(frames_per_image):
            writer.write(img)
        segments.append({"image": p.name, "stem": p.stem,
                         "start": cursor, "end": cursor + frames_per_image})
        cursor += frames_per_image
    writer.release()

    print(f"[saved] {out_path}")
    print(f"        {len(frames)} 张图 x {frames_per_image} 帧 = {cursor} 帧, "
          f"{fps} fps, 尺寸 {size[0]}x{size[1]}, codec={CODEC} (无损)")
    for seg in segments:
        print(f"        帧 [{seg['start']:>3}, {seg['end']:>3}) <- {seg['image']}")
    return out_path, segments


if __name__ == "__main__":
    build_smoke_video()
