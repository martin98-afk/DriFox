# -*- coding: utf-8 -*-
"""项目元数据纯函数 — 名称缩写提取 / 哈希配色 / 同色系淡染

从原 ``app/widgets/cards/settings/project_selector_card.py`` 拆出（2026-10：
项目选择卡片 UI 本体迁入 history-manager 插件，纯函数保留在宿主公共层，
供主窗 / 标签页 / 标题栏 / 插件共同使用）。

配色主题感知（2026-10）：原 ``L ∈ [50%, 65%]`` 采样区间是照深色底
（``#212126``）调的，浅色主题下色相 60°~180°（黄→青绿）对浅底对比度低至
1.18:1（WCAG 正文阈值 4.5:1），项目名与头像缩写均无法辨读。现固定 H/S
（项目身份色相）只解 L，按当前主题求解到目标对比度；深色主题下原本已
达标项目的颜色零变化。
"""

import colorsys
import re
import zlib

# ── 主题感知配色的定标常量 ──────────────────────────────────
# 定标来源：扫描 plugins/system-themes/themes/*/*.yaml 全部主题，把 card_bg
# 按自身 alpha 合成到 window.gradient_start / gradient_end 上取真实承载底
# 亮度（项目色出现在卡片底 / 侧栏 / 标题栏，均为卡片底系）。
#   浅色主题最暗 card_bg lum = 0.904272...（laven）
#   深色主题最亮 card_bg lum = 0.021511...（amber）
# 倍率留余量（4.7 / 3.2）抵消 8bit 取整误差，避免刚好踩线不达标。
# ⚠️ 主题包新增更严苛的底时，tests/utils/test_project_meta_contrast.py::
#    test_calibration_matches_worst_case_backgrounds 会失败，届时重算本常量。
_LIGHT_BG_LUM = 0.904272
_DARK_BG_LUM = 0.021511
_LIGHT_TARGET_CONTRAST = 4.7  # 小字正文：WCAG AA 4.5:1，留 0.2 余量
_DARK_TARGET_CONTRAST = 3.2  # 图形元素（色块）：WCAG AA 3:1，留 0.2 余量

# 头像缩写字恒白（用户决策 2026-10-12）：两种主题下项目色都被压进白字可读带
#   浅色档 cap_lum=0.153（4.7:1 定标）→ 白字 ≥5.1:1
#   深色档 cap 到 _DARK_CAP_LUM → 白字 ≥3.5:1（粗体大字，饱和彩底观感可读）
_DARK_CAP_LUM = 0.25



def extract_project_initials(name: str) -> str:
    """从项目名提取最多 2 个字符的缩写

    优先级：
    1. 中文 → 首个汉字
    2. 分隔符（_/-/空格）→ 首段首字母 + 末段首字母
    3. 驼峰/帕斯卡 → 首个词首字母 + 末个词首字母
    4. 其他 → 前 2 个字母大写

    Args:
        name: 项目名

    Returns:
        1-2 个字符的缩写字符串
    """
    if not name:
        return "??"

    # ── 中文：首个汉字 ──
    has_cjk = any("\u4e00" <= c <= "\u9fff" for c in name)
    if has_cjk:
        for c in name:
            if "\u4e00" <= c <= "\u9fff":
                return c
        return name[0]

    # ── 分隔符拆分（下划线/中划线/空格）──
    # 取第一个段的第一个字母 + 最后一个段的第一个字母
    for delim in ("_", "-", " "):
        if delim in name:
            parts = [p for p in name.split(delim) if p]
            if len(parts) >= 2:
                return (parts[0][0] + parts[-1][0]).upper()
            # 只有一个段，退化到后续逻辑
            name = parts[0]
            break

    # ── 驼峰/帕斯卡：拆分为词 ──
    # 处理连写缩写：OpenAIChat → Open|AI|Chat
    s = re.sub(r"([a-z0-9])([A-Z])", r"\1|\2", name)
    s = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1|\2", s)
    words = [w for w in s.split("|") if w]

    if len(words) >= 2:
        # 取首词首字母 + 末词首字母
        return (words[0][0] + words[-1][0]).upper()

    # ── 普通单词：前 2 字母大写 ──
    if len(name) >= 2:
        return name[:2].upper()
    return name.upper()


def _relative_luminance(rgb: tuple[int, int, int]) -> float:
    """WCAG 相对亮度（sRGB 线性化后按感知权重加权）

    Args:
        rgb: (r, g, b) 8bit 三元组

    Returns:
        0.0（纯黑）~ 1.0（纯白）
    """

    def _linearize(c: float) -> float:
        c = c / 255.0
        return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4

    r, g, b = (_linearize(float(c)) for c in rgb)
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def _hsl_to_rgb8(h: int, s: int, l: float) -> tuple[int, int, int]:
    """HSL → 8bit RGB 三元组（h 度、s 百分比、l 0-1）"""
    r, g, b = colorsys.hls_to_rgb(h / 360.0, l, s / 100.0)
    return (int(round(r * 255)), int(round(g * 255)), int(round(b * 255)))


def _l_for_target_luminance(h: int, s: int, target_lum: float) -> float:
    """固定 H/S，二分求相对亮度恰达 target_lum 的 L

    HSL 中固定 H/S 时相对亮度随 L 单调递增，故可二分。
    """
    lo, hi = 0.0, 1.0
    for _ in range(28):
        mid = (lo + hi) / 2.0
        if _relative_luminance(_hsl_to_rgb8(h, s, mid)) < target_lum:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2.0


def _is_light_theme() -> bool:
    """当前主题是否浅色（延迟导入 theme_manager）

    本模块被 tab_panel / square_avatar 等热路径引用，其注释明确要求保持
    "无 Qt / 无重依赖"（为列表渲染省掉 mcp SDK 等重链）。theme_manager 会
    拉起 plugin_manager 等重型依赖（实测模块加载约 50ms），故只在实际取色时
    函数内导入。异常时回退深色，与 Colors 类默认值一致。
    """
    try:
        from app.utils.theme_manager import theme_manager

        return bool(theme_manager.is_light_theme())
    except Exception:
        return False




# 配色结果缓存：key = (项目名, alpha, 是否浅色主题)
# 主题求解含二分（28 轮色彩空间换算），列表滚动 / 自绘 paintEvent 会反复
# 命中同一名字；项目数有限，超限整体清空即可。
#
# 注：目标亮度取自「全部主题的最严苛承载底」固定常量，故结果只依赖
# is_light 两档 —— 同档内换主题（crema → lumia）颜色不变，切主题无需重算。
_COLOR_CACHE: dict[tuple[str, int, bool], str] = {}
_COLOR_CACHE_MAX = 512


def get_project_color(name: str, alpha: int = 255) -> str:
    """根据项目名计算固定颜色（HSL 全空间哈希 + 主题感知明度）

    基础实现：在 HSL 圆柱上从完整字符串 zlib.crc32 直接采样：
      - H ∈ [0°, 360°)   → 色相覆盖整圈，永不局限于 12 离散点
        （旧实现的 12 色调色板在项目超 ~30 个时明显撞色，生日悖论）
      - S ∈ [55%, 85%]   → 足够鲜艳，避免灰色
      - L ∈ [50%, 65%]   → 采样初值

    采样自同一 CRC32 的不同 bit 段（H 低位、S 中位、L 高位），
    保证首字母相同 / 前缀相近的项目也能得到不同颜色。

    主题感知（2026-10）：固定 H/S 只调 L，使结果在当前主题承载底上达标——
      - 浅色主题 → 亮度上限，保证小字正文达 4.5:1（黄绿系原低至 1.18:1）
      - 深色主题 → 亮度下限，保证色块达 3:1（原本已达标的不动）
    深色主题下多数项目 L 不变，观感与旧版一致。

    使用 zlib.crc32 替代内置 hash()，避免 Python 的
    进程间随机化种子（PYTHONHASHSEED）导致每次启动颜色不一致。

    Args:
        name: 项目名
        alpha: 透明度 0-255

    Returns:
        RGBA 颜色字符串，如 "rgba(33, 139, 255, 255)"
    """
    is_light = _is_light_theme()
    cache_key = (name, alpha, is_light)
    cached = _COLOR_CACHE.get(cache_key)
    if cached is not None:
        return cached

    crc = zlib.crc32(name.encode("utf-8"))
    # 三个分量取自 CRC32 不同 bit 段，相关性低
    h = crc % 360  # 0-359°  色相
    s = 55 + ((crc >> 8) % 31)  # 55-85%  饱和度
    l = (50 + ((crc >> 16) % 16)) / 100.0  # 0.50-0.65  亮度初值

    if is_light:
        cap_lum = (_LIGHT_BG_LUM + 0.05) / _LIGHT_TARGET_CONTRAST - 0.05
        if _relative_luminance(_hsl_to_rgb8(h, s, l)) > cap_lum:
            l = _l_for_target_luminance(h, s, cap_lum)
    else:
        # 双向 clamp 进白字可读带：低于 floor 抬（对深底 3.2:1），高于 cap 压
        # （0.30+ 的亮色版配白字掉到 3:1 以下，且曾触发头像字自适应黑字）
        floor_lum = _DARK_TARGET_CONTRAST * (_DARK_BG_LUM + 0.05) - 0.05
        lum = _relative_luminance(_hsl_to_rgb8(h, s, l))
        if lum < floor_lum:
            l = _l_for_target_luminance(h, s, floor_lum)
        elif lum > _DARK_CAP_LUM:
            l = _l_for_target_luminance(h, s, _DARK_CAP_LUM)

    r, g, b = _hsl_to_rgb8(h, s, l)
    result = f"rgba({r}, {g}, {b}, {alpha})"

    if len(_COLOR_CACHE) >= _COLOR_CACHE_MAX:
        _COLOR_CACHE.clear()
    _COLOR_CACHE[cache_key] = result
    return result


def invalidate_color_cache() -> None:
    """清空配色缓存（主题切换后调用，防御性）

    缓存 key 含 ``is_light`` 分桶，且目标亮度是「全部主题最严苛底」的固定
    常量（同档内换主题颜色不变），故正常路径下无需清空。保留本入口供
    未来调整定标常量或强制重算使用。
    """
    _COLOR_CACHE.clear()


def _alpha_tint(rgba: str, ratio: float, fallback: str = "transparent") -> str:
    """把 ``rgba(r, g, b, a)`` 色按比例压成淡染底色（保持同色系关联）

    用于「当前项目」行静止态底纹：取项目强调色的一层极淡底色，与头像/色条
    同色系。解析失败（主题 token 占位符等）回退 ``fallback``。
    """
    try:
        inner = rgba[rgba.index("(") + 1 : rgba.rindex(")")]
        parts = [p.strip() for p in inner.split(",")]
        r, g, b = (int(float(parts[0])), int(float(parts[1])), int(float(parts[2])))
        return f"rgba({r}, {g}, {b}, {int(round(255 * ratio))})"
    except Exception:
        return fallback
