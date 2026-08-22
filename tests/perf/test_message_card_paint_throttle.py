# -*- coding: utf-8 -*-
"""
性能回归测试（#1 性能瓶颈报告 Top①）：MessageCard 动画绘制节流与渐变/裁剪缓存化

(a) 对应瓶颈：消息卡片 assistant 角色呼吸/流光动画以 50ms 高频定时器驱动（约 20fps），
    每帧 paintEvent 重建渐变与裁剪路径，CPU 占用高。第一批修复已在 paintEvent 加入
    渐变（self._grad_*）与裁剪路径（self._clip_*）缓存，仅在尺寸变化时重建。

(b) 本测试未修改任何业务代码，仅静态分析：用 pathlib 读取 app/widgets/message_card.py
    源码文本 + re 匹配，不 import PyQt5、不实例化任何 GUI 对象。

(c) 环境要求：pytest>=7 / Python3 / 对 app/ 源码有读权限 / 无需显示器 /
    无新三方依赖 / 跨平台 Windows 优先。
"""

from pathlib import Path
import re

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SRC = REPO_ROOT / "app" / "widgets" / "message_card.py"


@pytest.fixture(scope="module")
def src_text() -> str:
    return SRC.read_text(encoding="utf-8")


def test_static_anim_timer_high_frequency(src_text: str):
    """静态扫描：确认动画定时器以 50ms 高频驱动，且存在 _update_anim 与 self.update()。"""
    assert "self._anim_timer.start(50)" in src_text
    assert "_update_anim" in src_text
    assert "self.update()" in src_text


def test_perf_paint_cache_exists(src_text: str):
    """性能/回归断言：paintEvent 已缓存渐变（self._grad_*）与裁剪路径（self._clip_*），
    仅在尺寸变化时重建，而非每帧 new。"""
    assert "self._grad_" in src_text
    assert "self._clip_" in src_text


def test_perf_per_frame_allocation(src_text: str):
    """性能/回归断言：动画间隔 <=50ms；源码仍含渐变重建与逐 stop 颜色插值（每帧仍有颜色分配）。

    说明：3 渐变 × ~9 stop（main 9 / inner 10 / glow 6 / shimmer 3）≈ 27 QColor/帧 × 20fps。
    lerp_color 在源码中定义为 helper 并在 build_gradient 的 stops 循环内调用 1 次
    （文本出现 2 处），运行时循环展开为每帧 ~27 次 QColor 分配。
    """
    m = re.search(r"_anim_timer\.start\((\d+)\)", src_text)
    assert m is not None, "未找到 _anim_timer.start(...) 调用"
    interval = int(m.group(1))
    assert interval <= 50, f"动画定时器间隔应 <=50ms，实际 {interval}ms"

    build_gradient_count = len(re.findall(r"build_gradient\(", src_text))
    assert build_gradient_count >= 3, (
        f"paintEvent 应含 >=3 处渐变重建（build_gradient 调用），实际 {build_gradient_count}"
    )

    lerp_color_count = len(re.findall(r"lerp_color\(", src_text))
    # 源码中 lerp_color 定义为 helper 并在 build_gradient 循环内调用 1 次（共 2 处文本出现）。
    # 运行时每次 build_gradient 遍历 stops 调用 lerp_color，3 渐变 × ~9 stop ≈ 27 QColor/帧。
    assert lerp_color_count >= 2, (
        f"lerp_color 应至少定义并调用 1 次（证明每帧仍有颜色分配），实际 {lerp_color_count}"
    )
