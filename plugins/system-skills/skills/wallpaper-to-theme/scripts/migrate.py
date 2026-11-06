# -*- coding: utf-8 -*-
"""Wallpaper Engine → DriFox 主题插件 一键迁移

支持三种壁纸类型：
  video     直接取 project.json 里的 file 字段（mp4），转成 overlay 素材
  scene     解析 scene.pkg：抽音频（mp3/ogg）与主视觉图（PNG/JPEG）
  web       不支持（需要 Web 渲染，DriFox 主题无对应载体）

设计原则：
- 只依赖标准库 + Pillow（PIL 已是 DriFox 运行期依赖）
- 任何一步失败都给出可读原因，不静默产出半成品
- 产出前先 dry-run 打印计划，用户确认后才落盘

用法：
    python migrate.py <workshop_dir> --out <plugin_dir> [--dry-run]
    python migrate.py --list <workshop_root>
"""
from __future__ import annotations

import argparse
import io
import json
import re
import struct
import sys
from dataclasses import dataclass, field
from pathlib import Path

try:
    from PIL import Image
except ImportError:  # pragma: no cover
    Image = None

PNG_SIG = b"\x89PNG\r\n\x1a\n"
JPEG_SIG = b"\xff\xd8\xff"
AUDIO_EXT = {".mp3", ".ogg", ".wav", ".flac", ".m4a"}
VIDEO_EXT = {".mp4", ".webm", ".avi", ".mkv"}

# 明显不是主视觉的贴图路径
SKIP_TEX_HINTS = (
    "mask",
    "spritesheet",
    "shaders/",
    "/presets/",
    "effects/",
    "normal",
    "reflection",
    "_depth",
)

_ID_SAFE = re.compile(r"[^a-z0-9-]+")


# ── 数据结构 ─────────────────────────────────────────

@dataclass
class ThemePlan:
    """一次迁移的完整计划"""

    workshop_id: str
    title: str
    wall_type: str
    source_dir: Path
    out_dir: Path
    theme_id: str
    scene_image: Path | None = None       # 已落盘的主背景图
    decoration_image: Path | None = None  # 已落盘的角色/前景装饰件
    video_path: Path | None = None        # video 类型的 mp4
    audio_files: list[Path] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    def ok(self) -> bool:
        return not self.errors and bool(
            self.scene_image or self.video_path or self.audio_files
        )


def safe_id(text: str) -> str:
    s = _ID_SAFE.sub("-", text.lower()).strip("-")
    s = re.sub(r"-{2,}", "-", s)
    return s or "wallpaper-theme"


# ── scene.pkg 读取 ───────────────────────────────────

def read_pkg_entries(data: bytes) -> tuple[list[tuple[str, int, int]], int]:
    """解析 PKG 索引（PKGV0018~0023 实测同一布局）

    返回 ([(name, rel_offset, size)], data_area_base)
    rel_offset 相对 data_area_base。
    """
    if len(data) < 16 or not data[4:12].startswith(b"PKGV"):
        raise ValueError("不是 Wallpaper Engine PKG（缺 PKGV 签名）")
    count = struct.unpack_from("<I", data, 12)[0]
    if count <= 0 or count > 100_000:
        raise ValueError(f"条目数异常: {count}")
    off = 16
    entries = []
    for _ in range(count):
        nlen = struct.unpack_from("<I", data, off)[0]
        off += 4
        if nlen <= 0 or nlen > 4096:
            raise ValueError(f"条目名长度异常: {nlen}")
        name = data[off : off + nlen].decode("utf-8", "replace")
        off += nlen
        rel, sz = struct.unpack_from("<II", data, off)
        off += 8
        entries.append((name, rel, sz))
    base = off
    # 自检：条目首尾相接且填满数据区，否则结构假设不成立
    prev = 0
    for _n, rel, sz in sorted(entries, key=lambda e: e[1]):
        if rel != prev:
            raise ValueError("条目偏移不连续，该 PKG 版本结构不同")
        prev = rel + sz
    if prev != len(data) - base:
        raise ValueError("条目长度与文件大小不符")
    return entries, base


def image_from_tex(blob: bytes):
    """从 .tex 取出可用图像

    优先签名扫描（对 WE 各版本的 TEXB 头字段差异免疫），
    取不到就返回 None（原始像素 / DXT 压缩路径未实现）。
    """
    if Image is None:
        return None
    if blob[:8] == PNG_SIG or blob[:3] == JPEG_SIG:
        try:
            im = Image.open(io.BytesIO(blob))
            im.load()
            return im
        except Exception:
            return None
    j = blob.find(b"TEXB")
    tail = blob[j:] if j >= 0 else blob
    pos = [p for p in (tail.find(PNG_SIG), tail.find(JPEG_SIG)) if p >= 0]
    if not pos:
        return None
    try:
        im = Image.open(io.BytesIO(tail[min(pos):]))
        im.load()
        return im
    except Exception:
        return None


def pick_scene_images(entries, data: bytes, base: int):
    """挑主背景图与装饰件

    主背景：非 mask 贴图里面积最大的那张。
    装饰件：在剩余贴图里，取「大部分像素透明」且宽度 >= 800 的那张
            （角色立绘/前景元素的典型特征）。
    """
    candidates = []
    for name, rel, sz in entries:
        low = name.lower()
        if not low.endswith(".tex"):
            continue
        if any(h in low for h in SKIP_TEX_HINTS):
            continue
        blob = data[base + rel : base + rel + sz]
        im = image_from_tex(blob)
        if im is None:
            continue
        candidates.append((im.width * im.height, name, im))

    if not candidates:
        return None, None

    candidates.sort(key=lambda c: -c[0])
    _area, _name, scene = candidates[0]

    decoration = None
    for _area2, _n2, im in candidates[1:]:
        if im.mode != "RGBA":
            continue
        if im.width < 800:
            continue
        alpha = im.getchannel("A")
        hist = alpha.histogram()
        total = im.width * im.height
        transparent_ratio = hist[0] / total if total else 0.0
        if transparent_ratio >= 0.25:
            decoration = im
            break
    return scene, decoration


# ── 素材落盘 ─────────────────────────────────────────

def save_image(im, dest: Path, max_width: int, as_jpeg: bool) -> Path:
    """按目标宽度等比缩放并保存（过大图直接原样落盘会很占体积）"""
    if im.width > max_width:
        ratio = max_width / im.width
        im = im.resize((max_width, max(1, int(im.height * ratio))), Image.LANCZOS)
    if as_jpeg:
        im.convert("RGB").save(dest, quality=88, optimize=True)
    else:
        im.save(dest, optimize=True)
    return dest


def extract_audio(entries, data: bytes, base: int, out_dir: Path) -> list[Path]:
    out = []
    out_dir.mkdir(parents=True, exist_ok=True)
    idx = 0
    for name, rel, sz in entries:
        ext = Path(name).suffix.lower()
        if ext not in AUDIO_EXT:
            continue
        blob = data[base + rel : base + rel + sz]
        if ext == ".mp3" and not blob.startswith(b"ID3") and blob[:2] != b"\xff\xfb":
            # 可能是压缩流或非标准头，放弃而不是产出坏文件
            continue
        idx += 1
        dest = out_dir / f"{idx:02d}-{safe_id(Path(name).stem)[:40]}{ext}"
        dest.write_bytes(blob)
        out.append(dest)
    return out


# ── 主流程 ───────────────────────────────────────────

def read_project(wall_dir: Path) -> dict:
    pj = wall_dir / "project.json"
    if not pj.exists():
        raise ValueError(f"缺少 project.json：{wall_dir}")
    raw = pj.read_text(encoding="utf-8", errors="replace")
    return json.loads(raw)


def plan_migration(wall_dir: Path, out_dir: Path) -> ThemePlan:
    proj = read_project(wall_dir)
    wall_type = str(proj.get("type", "")).lower()
    title = proj.get("title") or wall_dir.name
    plan = ThemePlan(
        workshop_id=wall_dir.name,
        title=title,
        wall_type=wall_type,
        source_dir=wall_dir,
        out_dir=out_dir,
        theme_id=safe_id(title) or f"we-{wall_dir.name}",
    )

    if wall_type == "video":
        media = proj.get("file") or ""
        src = wall_dir / media
        if not src.exists():
            plan.errors.append(f"project.json 指向的视频不存在: {media}")
            return plan
        if src.suffix.lower() not in VIDEO_EXT:
            plan.errors.append(f"视频格式不支持: {src.suffix}")
            return plan
        plan.video_path = src
        return plan

    if wall_type in ("scene", "web"):
        if wall_type == "web":
            plan.errors.append("web 类型壁纸需要 Web 渲染，DriFox 主题无对应载体，无法迁移")
            return plan
        pkg = wall_dir / "scene.pkg"
        if not pkg.exists():
            # 部分 scene 壁纸用 gifscene.pkg（结构、索引格式与 scene.pkg 完全一致）
            pkg = wall_dir / "gifscene.pkg"
        if not pkg.exists():
            plan.errors.append("scene 类型但缺少 scene.pkg / gifscene.pkg")
            return plan
        data = pkg.read_bytes()
        try:
            entries, base = read_pkg_entries(data)
        except ValueError as e:
            plan.errors.append(f"scene.pkg 解析失败：{e}")
            return plan

        scene, decoration = pick_scene_images(entries, data, base)
        if scene is None:
            plan.errors.append(
                "scene.pkg 里没有可直接取用的图片（可能是 DXT 压缩贴图，当前未支持）"
            )
        else:
            plan.scene_image = scene  # 暂存 PIL.Image，落盘在 apply 阶段
        if decoration is not None:
            plan.decoration_image = decoration

        # 音频：pkg 内嵌的音频
        loose_audio = [
            p for p in wall_dir.iterdir() if p.suffix.lower() in AUDIO_EXT
        ]
        if loose_audio:
            plan.audio_files = loose_audio
        else:
            plan.audio_files = extract_audio(entries, data, base, out_dir / "assets")
        return plan

    plan.errors.append(f"未知壁纸类型: {wall_type or '(空)'}")
    return plan


def theme_yaml(plan: ThemePlan, has_decoration: bool, has_music: bool) -> str:
    lines = [
        f"name: {plan.title}",
        f"id: {plan.theme_id}",
        "mode: dark",
        "",
        "window:",
        "  gradient_start: rgba(16, 14, 22, 255)",
        "  gradient_end: rgba(26, 18, 26, 255)",
        "",
        "backgrounds:",
    ]
    if plan.video_path is not None:
        lines += [
            "  overlay:",
            "    target: chat_area",
            f"    video: ../../assets/{plan.video_path.name}",
            "    opacity: 0.35",
            "    position: center",
            "    enabled: true",
        ]
    else:
        lines += [
            "  scene:",
            "    image: ../../assets/scene.png",
            "    opacity: 1.0",
            "    dim: rgba(8, 6, 12, 0.42)",
            "    enabled: true",
        ]
        if has_decoration:
            lines += [
                "",
                "  decorations:",
                "    - id: foreground",
                "      image: ../../assets/decoration.png",
                "      anchor: scene_bottom",
                "      offset: [0, 40]",
                "      opacity: 0.55",
                "      fit_width: true",
                "      fit_height: true",
            ]
    if has_music:
        lines += ["", "  music:", "    tracks:"]
        for f in plan.audio_files:
            lines.append(f"      - ../../assets/{f.name}")
        lines += ["    volume: 0.45", "    loop: true", "    fade_ms: 420", "    enabled: true"]

    lines += [
        "",
        "colors:",
        "  card_bg: rgba(22, 18, 28, 232)",
        "  card_bg_solid: rgba(22, 18, 28, 248)",
        "  content_bg: '#16121c'",
        "  border: '#3a2a44'",
        "  border_accent: '#e8467c'",
        "  text_primary: '#ece6f2'",
        "  text_secondary: rgba(236, 230, 242, 0.66)",
        "  text_muted: '#8a80a0'",
        "  accent: '#e8467c'",
        "  accent_warm: '#38d6e0'",
        "  hover_bg: rgba(232, 70, 124, 0.12)",
        "  selected_bg: rgba(232, 70, 124, 0.26)",
        "  input_bg_start: rgba(24, 20, 32, 200)",
        "  input_bg_end: rgba(32, 24, 40, 200)",
        "  input_text: '#ece6f2'",
        "  input_border: '#3a2a44'",
        "  input_focus_border: '#e8467c'",
        "  send_btn_start: '#e8467c'",
        "  send_btn_end: '#b8325f'",
    ]
    return "\n".join(lines) + "\n"


def apply_plan(plan: ThemePlan) -> list[str]:
    """落盘插件；返回产出文件清单"""
    root = plan.out_dir
    assets = root / "assets"
    theme_dir = root / "themes" / plan.theme_id
    assets.mkdir(parents=True, exist_ok=True)
    theme_dir.mkdir(parents=True, exist_ok=True)

    made = []
    has_decoration = False
    has_music = bool(plan.audio_files)

    if plan.video_path is not None:
        dest = assets / plan.video_path.name
        if not dest.exists():
            dest.write_bytes(plan.video_path.read_bytes())
        made.append(str(dest))
    else:
        if isinstance(plan.scene_image, Image.Image):
            dest = assets / "scene.png"
            save_image(plan.scene_image, dest, max_width=2560, as_jpeg=False)
            made.append(str(dest))
        if isinstance(plan.decoration_image, Image.Image):
            dest = assets / "decoration.png"
            save_image(plan.decoration_image, dest, max_width=1600, as_jpeg=False)
            made.append(str(dest))
            has_decoration = True

    # 音频已由 extract_audio 直接落到 assets；loose 音频需要复制
    for f in plan.audio_files:
        dest = assets / f.name
        if f.resolve() != dest.resolve() and not dest.exists():
            dest.write_bytes(f.read_bytes())
        made.append(str(dest))

    yaml_path = theme_dir / f"{plan.theme_id}.yaml"
    yaml_path.write_text(
        theme_yaml(plan, has_decoration, has_music), encoding="utf-8"
    )
    made.append(str(yaml_path))

    manifest = {
        "name": plan.theme_id,
        "description": f"{plan.title} — 由 Wallpaper Engine 创意工坊 {plan.workshop_id} 迁移",
        "icon": {"light": "icon.png", "dark": "icon_dark.png"},
        "version": "1.0.0",
        "author": {"name": "DriFox Contributors"},
        "license": "MIT",
        "type": "user",
        "keywords": ["theme", "wallpaper-engine", "migrated", "主题"],
        "components": {"themes": True},
        "min_host_version": "0.6.15",
    }
    mpath = root / ".drifox-plugin" / "plugin.json"
    mpath.parent.mkdir(parents=True, exist_ok=True)
    mpath.write_text(json.dumps(manifest, ensure_ascii=False, indent=4), encoding="utf-8")
    made.append(str(mpath))

    # 图标：用主图裁方形
    src_img = plan.scene_image if isinstance(plan.scene_image, Image.Image) else None
    if src_img is None and plan.video_path is not None:
        preview = plan.source_dir / "preview.jpg"
        if preview.exists() and Image is not None:
            try:
                src_img = Image.open(preview)
            except Exception:
                src_img = None
    if isinstance(src_img, Image.Image):
        w, h = src_img.size
        side = min(w, h)
        sq = src_img.crop(((w - side) // 2, (h - side) // 2, (w + side) // 2, (h + side) // 2))
        for name in ("icon.png", "icon_dark.png"):
            p = root / name
            sq.resize((256, 256), Image.LANCZOS).save(p, optimize=True)
            made.append(str(p))

    return made


# ── CLI ──────────────────────────────────────────────

def cmd_list(root: Path) -> int:
    rows = []
    for d in sorted(root.iterdir()):
        if not d.is_dir() or not (d / "project.json").exists():
            continue
        try:
            proj = json.loads((d / "project.json").read_text(encoding="utf-8", errors="replace"))
        except Exception:
            continue
        rows.append(
            (
                d.name,
                str(proj.get("type", "?")).lower(),
                str(proj.get("title", "?"))[:48],
            )
        )
    if not rows:
        print(f"{root} 下没有找到壁纸项目")
        return 1
    for wid, t, title in rows:
        print(f"{wid:<14} {t:<8} {title}")
    print(f"\n共 {len(rows)} 个")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Wallpaper Engine → DriFox 主题插件")
    ap.add_argument("workshop", help="壁纸目录（含 project.json），或配合 --list 传根目录")
    ap.add_argument("--out", help="输出插件目录")
    ap.add_argument("--list", action="store_true", help="列出根目录下所有壁纸")
    ap.add_argument("--dry-run", action="store_true", help="只打印计划不落盘")
    args = ap.parse_args(argv)

    src = Path(args.workshop)
    if args.list:
        return cmd_list(src)

    if not args.out:
        print("缺少 --out", file=sys.stderr)
        return 2
    out = Path(args.out)

    plan = plan_migration(src, out)
    print(f"标题    : {plan.title}")
    print(f"类型    : {plan.wall_type}")
    print(f"主题 id : {plan.theme_id}")
    print(f"视频    : {plan.video_path.name if plan.video_path else '-'}")
    print(
        "背景图  : "
        + ("已取得" if isinstance(plan.scene_image, Image.Image) else "-")
    )
    print(
        "装饰件  : "
        + ("已取得" if isinstance(plan.decoration_image, Image.Image) else "-")
    )
    print(f"音乐    : {len(plan.audio_files)} 首")
    for w in plan.warnings:
        print(f"警告    : {w}")
    for e in plan.errors:
        print(f"错误    : {e}")

    if plan.errors:
        print("\n迁移中止：存在未解决的问题")
        return 1
    if args.dry_run:
        print("\n(dry-run) 未落盘")
        return 0

    made = apply_plan(plan)
    print(f"\n已生成 {len(made)} 个文件：")
    for m in made:
        print("  " + m)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
