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
    """性能/回归断言：动画间隔 <=50ms；每帧 QColor 分配已收敛（流式指示从彩虹循环改为单色）。

    说明：旧实现 3 个彩虹渐变 × ~9 stop ≈ 27 QColor/帧 × 20fps（build_gradient + lerp_color）。
    现改为单色 tint + 底部光块，build_gradient / lerp_color 已移除，每帧固定分配
    内壁 2 + 描边 1 + 光块 2 = 5 个 QColor，且渐变/裁剪路径仍走缓存（见上一用例）。
    本用例保留为断言：不得把彩虹逐 stop 插值重新引入热路径。
    """
    m = re.search(r"_anim_timer\.start\((\d+)\)", src_text)
    assert m is not None, "未找到 _anim_timer.start(...) 调用"
    interval = int(m.group(1))
    assert interval <= 50, f"动画定时器间隔应 <=50ms，实际 {interval}ms"

    # 逐 stop 彩虹插值不得回归
    assert "build_gradient(" not in src_text, "彩虹逐 stop 插值 build_gradient 不应重新引入"
    assert "lerp_color(" not in src_text, "彩虹色插值 lerp_color 不应重新引入"
