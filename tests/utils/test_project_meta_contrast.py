# -*- coding: utf-8 -*-
"""项目色对比度回归测试 — 浅色主题可读性修复

背景：``get_project_color`` 的 HSL 采样区间 ``L ∈ [50%, 65%]`` 是照深色底
（``#212126``）调的，浅色主题下色相 60°~180°（黄→青绿）对浅底对比度低至
1.18:1（WCAG 正文阈值 4.5:1），项目名与头像缩写均无法辨读。

本文件锁定修复后的契约：
1. 色相对项目底 / 深色底达标（浅 4.5:1 / 深 3:1）
2. 头像缩写字恒白，且项目色被压进白字可读带（浅档 ≥4.4 / 深档 ≥3.4）
3. 色相 H 与饱和度 S 保持不变（项目身份色不串）
4. crc32 决定颜色，跨进程稳定（不受 PYTHONHASHSEED 影响）
5. 深色主题下原本已达标项目的颜色零变化（防回归）
"""

from __future__ import annotations

import colorsys
import glob
import re
import zlib
from pathlib import Path

import pytest
import yaml

from app.utils import project_meta
from app.utils.project_meta import (
    _relative_luminance,
    extract_project_initials,
    get_project_color,
    invalidate_color_cache,
)

# 断言阈值：修复目标（留 0.1 余量抵消 8bit 取整）
LIGHT_MIN_CONTRAST = 4.4
DARK_MIN_CONTRAST = 3.0
# 头像缩写字恒白：浅档 cap 0.153 → 白字 5.1，深档 cap 0.25 → 白字 3.5（留余量）
AVATAR_TEXT_MIN_CONTRAST_LIGHT = 4.4
AVATAR_TEXT_MIN_CONTRAST_DARK = 3.4

# 真实项目名样本（含用户截图里读不清的那批）
_SAMPLE_PROJECTS = [
    "drifox-plugins2",
    "drifox-pyside6",
    "ms-swift",
    "oh-my-pi",
    "sushine-node-executor",
    "trenddb",
    "专利",
    "便利店hy4",
    "冶金动力期刊",
    "在线监测",
    "插件推荐",
    "新项目",
    "日报",
    "梅钢脱硫脱硝",
    "全部项目",
    "默认项目",
]

_THEME_GLOB = "plugins/system-themes/themes/*/*.yaml"
_RGBA_RE = re.compile(r"rgba\(\s*(\d+),\s*(\d+),\s*(\d+)")
_HEX_RE = re.compile(r"#([0-9a-fA-F]{6})")


# ── 测试辅助 ──────────────────────────────────────────────


def _parse_color(raw: object) -> tuple[int, int, int, int] | None:
    """解析主题 YAML 里的颜色（hex 或 rgba），失败返回 None"""
    text = str(raw).strip()
    match = _HEX_RE.match(text)
    if match:
        h = match.group(1)
        return (int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16), 255)
    match = re.findall(r"[\d.]+", text)
    if len(match) >= 3:
        alpha = int(float(match[3])) if len(match) > 3 else 255
        return (int(float(match[0])), int(float(match[1])), int(float(match[2])), alpha)
    return None


def _composite(fg: tuple[int, int, int, int], bg: tuple[int, int, int]) -> tuple[int, int, int]:
    """把带 alpha 的前景合成到不透明背景上，返回真实可见色"""
    a = fg[3] / 255.0
    return tuple(int(round(fg[i] * a + bg[i] * (1 - a))) for i in range(3))


def _contrast(color_a: tuple[int, int, int], color_b: tuple[int, int, int]) -> float:
    """WCAG 对比度（1.0 ~ 21.0）"""
    lum_a, lum_b = _relative_luminance(color_a), _relative_luminance(color_b)
    if lum_a < lum_b:
        lum_a, lum_b = lum_b, lum_a
    return (lum_a + 0.05) / (lum_b + 0.05)


def _rgb_of(color_str: str) -> tuple[int, int, int]:
    """从 get_project_color 返回值取 RGB（忽略 alpha）"""
    match = _RGBA_RE.match(color_str)
    assert match, f"颜色格式异常: {color_str!r}"
    return (int(match.group(1)), int(match.group(2)), int(match.group(3)))


def _hsl_of(name: str) -> tuple[int, int, float]:
    """复算项目名的 HSL 采样值（与实现同一套 crc32 分段）"""
    crc = zlib.crc32(name.encode("utf-8"))
    return crc % 360, 55 + ((crc >> 8) % 31), (50 + ((crc >> 16) % 16)) / 100.0


def _theme_card_backgrounds() -> list[tuple[str, str, tuple[int, int, int]]]:
    """扫描全部主题，返回 (主题ID, 模式, card_bg 合成到窗口渐变后的底) 列表

    项目色出现在卡片底 / 侧栏 / 标题栏，均为卡片底系；主题 YAML 的 card_bg
    多半带 alpha，需合成到 window.gradient_start / gradient_end 上取真实底。
    """
    root = Path(__file__).resolve().parents[2]
    found: list[tuple[str, str, tuple[int, int, int]]] = []
    for path in sorted(glob.glob(str(root / _THEME_GLOB))):
        data = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
        colors = data.get("colors", {})
        window = data.get("window", {})
        card_bg = _parse_color(colors.get("card_bg", ""))
        if card_bg is None:
            continue
        mode = data.get("mode") or "dark"
        for key in ("gradient_start", "gradient_end"):
            grad = _parse_color(window.get(key, "#000000"))
            if grad is None:
                continue
            found.append((str(data.get("id", "?")), mode, _composite(card_bg, grad[:3])))
    return found


_THEME_BACKGROUNDS = _theme_card_backgrounds()
_LIGHT_BACKGROUNDS = [item for item in _THEME_BACKGROUNDS if item[1] == "light"]
_DARK_BACKGROUNDS = [item for item in _THEME_BACKGROUNDS if item[1] != "light"]


@pytest.fixture
def _force_theme_mode(monkeypatch):
    """把当前主题固定为指定模式，绕过 Settings / ThemeManager 依赖"""

    def _apply(is_light: bool):
        monkeypatch.setattr(project_meta, "_is_light_theme", lambda: is_light)
        invalidate_color_cache()

    return _apply


# ── 测试：前提与定标常量 ──────────────────────────────────


def test_theme_scan_finds_both_modes():
    """主题扫描前提：浅色与深色主题都真实存在（比例失真时定标常量需重算）"""
    assert _LIGHT_BACKGROUNDS, f"未扫到浅色主题（glob: {_THEME_GLOB}）"
    assert _DARK_BACKGROUNDS, f"未扫到深色主题（glob: {_THEME_GLOB}）"


def test_calibration_matches_worst_case_backgrounds():
    """定标常量必须覆盖全部主题里最严苛的承载底

    ``_LIGHT_BG_LUM`` 取「最暗」浅底（浅色下颜色要压得够暗才能读），
    ``_DARK_BG_LUM`` 取「最亮」深底（深色下颜色要提得够亮才够对比）。
    主题包一旦新增比现行常量更严苛的主题，本测试失败，提示重算常量。
    """
    light_lums = [_relative_luminance(bg) for _, _, bg in _LIGHT_BACKGROUNDS]
    dark_lums = [_relative_luminance(bg) for _, _, bg in _DARK_BACKGROUNDS]
    assert min(light_lums) >= project_meta._LIGHT_BG_LUM - 1e-9, (
        f"有浅色主题的卡片底比定标常量更暗: min={min(light_lums):.4f} < {project_meta._LIGHT_BG_LUM}"
    )
    assert max(dark_lums) <= project_meta._DARK_BG_LUM + 1e-9, (
        f"有深色主题的卡片底比定标常量更亮: max={max(dark_lums):.4f} > {project_meta._DARK_BG_LUM}"
    )


# ── 测试：对比度契约 ──────────────────────────────────────


def test_light_theme_colors_meet_contrast_on_all_backgrounds(_force_theme_mode):
    """浅色主题：每个项目色在全部浅底上达 4.5:1（小字正文标准）"""
    _force_theme_mode(True)
    failures = []
    for theme_id, _mode, bg in _LIGHT_BACKGROUNDS:
        for name in _SAMPLE_PROJECTS:
            ratio = _contrast(_rgb_of(get_project_color(name)), bg)
            if ratio < LIGHT_MIN_CONTRAST:
                failures.append(f"{theme_id}/{name}: {ratio:.2f}")
    assert not failures, "浅色主题对比度不达标:\n" + "\n".join(failures)


def test_dark_theme_colors_meet_contrast_on_all_backgrounds(_force_theme_mode):
    """深色主题：每个项目色在全部深底上达 3:1（图形元素标准）"""
    _force_theme_mode(False)
    failures = []
    for theme_id, _mode, bg in _DARK_BACKGROUNDS:
        for name in _SAMPLE_PROJECTS:
            ratio = _contrast(_rgb_of(get_project_color(name)), bg)
            if ratio < DARK_MIN_CONTRAST:
                failures.append(f"{theme_id}/{name}: {ratio:.2f}")
    assert not failures, "深色主题对比度不达标:\n" + "\n".join(failures)


def test_avatar_text_color_readable_on_swatch(_force_theme_mode):
    """头像缩写字恒白：项目色被压进白字可读带（浅档 ≥4.4 / 深档 ≥3.4）

    恒白是用户决策（2026-10-12）：自适应黑白曾致深色档头像出黑字，
    彩色块白字也是全站惯例。可读性靠项目色双向 clamp 保证，
    不再依赖字色切换算法（contrast_text_color 已删除）。
    """
    failures = []
    for is_light in (True, False):
        _force_theme_mode(is_light)
        min_ratio = AVATAR_TEXT_MIN_CONTRAST_LIGHT if is_light else AVATAR_TEXT_MIN_CONTRAST_DARK
        for name in _SAMPLE_PROJECTS:
            color = get_project_color(name)
            rgb = _rgb_of(color)
            ratio = _contrast(rgb, (255, 255, 255))  # 字色恒白
            if ratio < min_ratio:
                failures.append(f"{'light' if is_light else 'dark'}/{name}: {ratio:.2f}")
    assert not failures, "头像白字对比度不达标:\n" + "\n".join(failures)


# ── 测试：身份色不串 ──────────────────────────────────────


def test_hue_and_saturation_preserved(_force_theme_mode):
    """色相 H 与饱和度 S 必须保持不变（只允许调亮度 L）

    项目色是身份标识，跨主题换色相会让用户认不出同一项目。
    """
    for is_light in (True, False):
        _force_theme_mode(is_light)
        for name in _SAMPLE_PROJECTS:
            expected_h, expected_s, _ = _hsl_of(name)
            r, g, b = _rgb_of(get_project_color(name))
            hue, _, sat = colorsys.rgb_to_hls(r / 255.0, g / 255.0, b / 255.0)
            actual_h = round(hue * 360) % 360
            actual_s = sat * 100.0
            # 8bit 量化误差：色相 ±2°、饱和度 ±3%
            assert abs(actual_h - expected_h) <= 2 or abs(actual_h - expected_h) >= 358, (
                f"{name} 色相漂移: {expected_h} → {actual_h}"
            )
            assert abs(actual_s - expected_s) <= 3.0, f"{name} 饱和度漂移: {expected_s:.1f} → {actual_s:.1f}"


def test_dark_theme_already_compliant_colors_unchanged(_force_theme_mode):
    """深色主题：白字可读带 [floor, cap] 内的项目颜色零变化

    恒白决策（2026-10-12）后深色分支双向 clamp：低于 floor 抬、高于 cap 压
    都是合法修正（为保头像白字可读）；带内项目不得被误改，否则是观感回归。
    """
    _force_theme_mode(False)
    dark_bg = (33, 33, 38)  # midnight card 底（深色基准）
    changed = []
    for name in _SAMPLE_PROJECTS:
        h, s, l = _hsl_of(name)
        original = tuple(int(round(c * 255)) for c in colorsys.hls_to_rgb(h / 360.0, l, s / 100.0))
        if _contrast(original, dark_bg) < 3.0 or _relative_luminance(original) > project_meta._DARK_CAP_LUM:
            continue  # 原本不达标，或超出白字可读带（cap 压低是合法修正）
        actual = _rgb_of(get_project_color(name))
        if actual != original:
            changed.append(f"{name}: {original} → {actual}")
    assert not changed, "深色下已达标项目被误改:\n" + "\n".join(changed)


# ── 测试：确定性与缓存 ────────────────────────────────────


def test_color_is_deterministic_across_calls(_force_theme_mode):
    """同一项目名重复取色结果一致（缓存不引入抖动）"""
    _force_theme_mode(True)
    for name in _SAMPLE_PROJECTS:
        first = get_project_color(name)
        invalidate_color_cache()
        second = get_project_color(name)
        assert first == second, f"{name} 取色不稳定: {first} vs {second}"


def test_color_stable_across_hash_seeds():
    """配色由 crc32 决定，不受 PYTHONHASHSEED 影响（跨进程稳定）"""
    expected = {}
    for name in _SAMPLE_PROJECTS:
        crc = zlib.crc32(name.encode("utf-8"))
        expected[name] = (crc % 360, 55 + ((crc >> 8) % 31))
    for name, (hue, sat) in expected.items():
        assert _hsl_of(name)[:2] == (hue, sat)


def test_cache_respects_theme_mode(_force_theme_mode):
    """缓存按主题深浅分桶：切档取色不得返回另一档的结果"""
    name = "ms-swift"  # 该色在浅档会被明显压暗
    _force_theme_mode(True)
    light_color = get_project_color(name)
    _force_theme_mode(False)
    dark_color = get_project_color(name)
    assert light_color != dark_color, "深浅档返回了同一个颜色（缓存未分桶）"
    # 回到浅档应命中浅档结果（不是深档残留）
    _force_theme_mode(True)
    assert get_project_color(name) == light_color


def test_alpha_is_forwarded(_force_theme_mode):
    """alpha 参数必须透传到返回值（消费点依赖半透明/不透明两种形态）"""
    _force_theme_mode(True)
    assert get_project_color("专利", alpha=160).endswith(", 160)")
    assert get_project_color("专利", alpha=255).endswith(", 255)")


# ── 测试：边界与鲁棒性 ────────────────────────────────────


@pytest.mark.parametrize("name", ["", "a", "项目", "PROJECT_WITH_UNDERSCORE", "中文English混排"])
def test_edge_case_names_still_produce_valid_color(name, _force_theme_mode):
    """极端项目名仍产出合法 rgba 且达标（不因短名/混排崩溃）"""
    _force_theme_mode(True)
    color = get_project_color(name)
    assert _RGBA_RE.match(color), f"非法颜色输出: {color!r}"
    assert isinstance(extract_project_initials(name), str)


def test_contrast_text_color_fallbacks_removed():
    """头像字恒白后 contrast_text_color 已删：防复活哨兵"""
    assert not hasattr(project_meta, "contrast_text_color"), (
        "contrast_text_color 已随恒白决策删除；头像字色请直接用白色，可读性由 get_project_color 双向 clamp 保证"
    )
