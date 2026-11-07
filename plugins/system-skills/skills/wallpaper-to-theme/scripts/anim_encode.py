# -*- coding: utf-8 -*-
"""把视频/动图转成主题可用的动画 WebP

为什么需要这一步：DriFox 的动态背景走动画 WebP（Qt 内置 qwebp 解码），
不依赖系统媒体栈。原素材（Wallpaper Engine 的 mp4）必须离线转一次，
客户端才能在任何平台播放。

只在主题制作端需要 ffmpeg；客户端零依赖。

用法：
    python anim_encode.py in.mp4 out.webp
    python anim_encode.py in.mp4 out.webp --mode mask --blur 1.0
    python anim_encode.py in.mp4 out.webp --width 800 --fps 12 --start 2 --duration 8

两种模式：
    normal  画面原样，适合当区域背景图
    mask    黑底 + 反相亮度 alpha，复刻 backgrounds.overlay 的光影压暗效果
            （白色/亮部透明，暗部压暗，叠在背景上形成流动光影）
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

DEFAULT_WIDTH = 800
DEFAULT_FPS = 12
DEFAULT_QUALITY = 55
# 客户端 fps 上限（theme_manager._ANIM_FPS_CAP / anim_background.FPS_CAP）
FPS_CAP = 15

def find_ffmpeg() -> str | None:
    env = os.environ.get("DRIFOX_FFMPEG")
    if env and Path(env).exists():
        return env
    return shutil.which("ffmpeg")

def find_ffprobe(ffmpeg: str) -> str | None:
    sibling = Path(ffmpeg).with_name("ffprobe.exe" if os.name == "nt" else "ffprobe")
    if sibling.exists():
        return str(sibling)
    return shutil.which("ffprobe")

def source_size(ffprobe: str | None, path: Path) -> tuple[int, int] | None:
    """读源视频尺寸（竖版素材需要先按目标比例裁切）"""
    if not ffprobe:
        return None
    try:
        out = subprocess.check_output(
            [
                ffprobe,
                "-v",
                "error",
                "-select_streams",
                "v:0",
                "-show_entries",
                "stream=width,height",
                "-of",
                "csv=p=0:s=x",
                str(path),
            ],
            text=True,
        ).strip()
        w, h = out.split("x")[:2]
        return int(w), int(h)
    except (subprocess.CalledProcessError, ValueError):
        return None

def crop_filter(args, src_size: tuple[int, int] | None) -> str | None:
    """按 --aspect 从源裁出一块（竖版素材裁成横版，只保留可见段）"""
    if not args.aspect or not src_size:
        return None
    try:
        aw, ah = (int(x) for x in args.aspect.replace("/", ":").split(":")[:2])
    except ValueError:
        return None
    if aw <= 0 or ah <= 0:
        return None
    sw, sh = src_size
    target = aw / ah
    if sw / sh > target:
        cw, ch = int(sh * target), sh
        x, y = (sw - cw) // 2, 0
    else:
        cw, ch = sw, int(sw / target)
        x = 0
        if args.align == "top":
            y = 0
        elif args.align == "bottom":
            y = sh - ch
        else:
            y = (sh - ch) // 2
    cw, ch = max(2, cw - cw % 2), max(2, ch - ch % 2)  # yuv420 要求偶数
    return f"crop={cw}:{ch}:{x}:{y}"

def build_filter(args, src_size: tuple[int, int] | None = None) -> str:
    parts = []
    crop = crop_filter(args, src_size)
    if crop:
        parts.append(crop)
    parts.append(f"fps={args.fps}")
    parts.append(f"scale={args.width}:-2:flags=lanczos")
    base = ",".join(parts)
    if args.mode == "mask":
        # 黑底 + 反相亮度作 alpha：亮部透明、暗部压暗。
        # geq 的 lum() 在部分 ffmpeg 构建里不可用，改用 split+alphamerge。
        chain = (
            f"{base},format=rgb24,split[bg][mask];"
            "[mask]format=gray,negate[m];"
            "[bg]colorchannelmixer=rr=0:gg=0:bb=0[blk];"
            "[blk][m]alphamerge"
        )
    else:
        chain = base
    if args.blur and args.blur > 0:
        chain += f",gblur=sigma={args.blur:.2f}"
    return chain

def run_ffmpeg(ffmpeg: str, src: Path, dst: Path, args, src_size=None) -> None:
    cmd = [
        ffmpeg,
        "-y",
        "-loglevel",
        "error",
    ]
    if args.start:
        cmd += ["-ss", str(args.start)]
    cmd += ["-i", str(src)]
    if args.duration:
        cmd += ["-t", str(args.duration)]
    cmd += [
        "-vf",
        build_filter(args, src_size),
        "-c:v",
        "libwebp_anim",
        "-lossless",
        "0",
        "-q:v",
        str(args.quality),
        "-loop",
        "0",
        "-an",
        str(dst),
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError((proc.stderr or proc.stdout or "").strip() or f"ffmpeg 退出码 {proc.returncode}")

def probe(ffprobe: str | None, path: Path) -> dict:
    """读输出规格；无 ffprobe 时退化为只看体积"""
    info = {"bytes": path.stat().st_size if path.exists() else 0}
    if not ffprobe:
        return info
    try:
        out = subprocess.check_output(
            [
                ffprobe,
                "-v",
                "error",
                "-select_streams",
                "v:0",
                "-count_frames",
                "-show_entries",
                "stream=width,height,nb_read_frames,r_frame_rate",
                "-of",
                "json",
                str(path),
            ],
            text=True,
        )
        stream = (json.loads(out).get("streams") or [{}])[0]
        info.update(
            {
                "width": stream.get("width"),
                "height": stream.get("height"),
                "frames": stream.get("nb_read_frames"),
                "rate": stream.get("r_frame_rate"),
            }
        )
    except (subprocess.CalledProcessError, ValueError):
        pass
    return info

def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="视频/动图 → 主题动画 WebP")
    parser.add_argument("input", help="源文件（mp4/gif/webm 等 ffmpeg 可读的格式）")
    parser.add_argument("output", help="输出 .webp 路径")
    parser.add_argument("--width", type=int, default=DEFAULT_WIDTH, help="输出宽度，高度按比例（默认 800）")
    parser.add_argument("--fps", type=int, default=DEFAULT_FPS, help=f"帧率，上限 {FPS_CAP}（默认 12）")
    parser.add_argument("--quality", type=int, default=DEFAULT_QUALITY, help="质量 0~100（默认 55）")
    parser.add_argument("--mode", choices=("normal", "mask"), default="normal", help="normal=原画面 / mask=光影遮罩")
    parser.add_argument("--blur", type=float, default=0.0, help="预烘模糊 sigma（默认 0，建议 0.4~0.8）")
    parser.add_argument("--start", type=float, default=0.0, help="起始秒（默认 0）")
    parser.add_argument("--duration", type=float, default=0.0, help="截取时长秒（默认整段）")
    parser.add_argument("--aspect", help="按目标比例先从源裁切再缩放，如 16:9（竖版素材必用）")
    parser.add_argument(
        "--align", choices=("top", "center", "bottom"), default="center", help="裁切对齐（配合 --aspect）"
    )
    args = parser.parse_args(argv)

    if args.fps > FPS_CAP:
        print(f"[warn] fps {args.fps} 超过上限，按 {FPS_CAP} 处理")
        args.fps = FPS_CAP

    src = Path(args.input)
    dst = Path(args.output)
    if not src.exists():
        print(f"[error] 源文件不存在: {src}")
        return 2

    ffmpeg = find_ffmpeg()
    if not ffmpeg:
        print("[error] 找不到 ffmpeg。装一个并把 ffmpeg 加进 PATH，或设 DRIFOX_FFMPEG 指向可执行文件")
        return 2

    try:
        src_size = source_size(find_ffprobe(ffmpeg), src) if args.aspect else None
        run_ffmpeg(ffmpeg, src, dst, args, src_size)
    except RuntimeError as e:
        print(f"[error] 转码失败: {e}")
        return 1

    info = probe(find_ffprobe(ffmpeg), dst)
    size_mb = info["bytes"] / 1024 / 1024
    print(f"[ok] {dst}")
    print(
        f"     {info.get('width', '?')}x{info.get('height', '?')} "
        f"{info.get('frames', '?')} 帧 {size_mb:.2f} MB"
    )
    if size_mb > 3:
        print("[warn] 体积偏大：缩短时长、降 --width 或降 --quality")
    if info.get("frames") and int(info["frames"]) < 12:
        print("[warn] 帧数过少，循环会明显跳动：加长 --duration")
    return 0

if __name__ == "__main__":
    sys.exit(main())
