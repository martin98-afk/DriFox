# -*- coding: utf-8 -*-
"""项目元数据纯函数 — 名称缩写提取 / 哈希配色 / 同色系淡染

从原 ``app/widgets/cards/settings/project_selector_card.py`` 拆出（2026-10：
项目选择卡片 UI 本体迁入 history-manager 插件，纯函数保留在宿主公共层，
供主窗 / 标签页 / 标题栏 / 插件共同使用）。
"""

import colorsys
import re
import zlib


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


def get_project_color(name: str, alpha: int = 255) -> str:
    """根据项目名计算固定颜色（HSL 全空间哈希）

    旧实现：仅在 12 色调色板中取色 → 项目超过 ~30 个即发生明显撞色
    （生日悖论），尤其是首字母相同或前缀相近的项目。

    新实现：在 HSL 圆柱上从完整字符串 zlib.crc32 直接采样：
      - H ∈ [0°, 360°)   → 色相覆盖整圈，永不局限于 12 离散点
      - S ∈ [55%, 85%]   → 足够鲜艳，避免灰色
      - L ∈ [50%, 65%]   → 在 #212126 深色背景上清晰；白字仍可读

    采样自同一 CRC32 的不同 bit 段（H 低位、S 中位、L 高位），
    保证首字母相同 / 前缀相近的项目也能得到不同颜色。

    使用 zlib.crc32 替代内置 hash()，避免 Python 的
    进程间随机化种子（PYTHONHASHSEED）导致每次启动颜色不一致。

    Args:
        name: 项目名
        alpha: 透明度 0-255

    Returns:
        RGBA 颜色字符串，如 "rgba(33, 139, 255, 255)"
    """
    crc = zlib.crc32(name.encode("utf-8"))
    # 三个分量取自 CRC32 不同 bit 段，相关性低
    h = crc % 360  # 0-359°  色相
    s = 55 + ((crc >> 8) % 31)  # 55-85%  饱和度
    l = 50 + ((crc >> 16) % 16)  # 50-65%  亮度

    r, g, b = colorsys.hls_to_rgb(h / 360.0, l / 100.0, s / 100.0)
    return f"rgba({int(round(r * 255))}, {int(round(g * 255))}, {int(round(b * 255))}, {alpha})"


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
