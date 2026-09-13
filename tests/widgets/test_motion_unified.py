# -*- coding: utf-8 -*-
"""统一动效层（app/utils/motion.py + Animations token）回归测试

覆盖三类守卫：

1. **行为**：``retarget`` 从当前实测值续接（连点/反向不跳变）、亚像素不启动、
   ``HeightAnimator`` 终态成对恢复 min/max、``LoopTimer`` 门控拦截。
2. **静态**：改造过的文件不得退回硬编码魔法时长 / 缺少无障碍检查。
3. **规范**：``Animations`` 方向语义 token 存在且关系正确。

行为用例依赖系统「减少动态效果」为**关闭**状态；开启时自动 skip（那些路径
的语义是"直接落终值"，由静态用例保证其存在）。
"""

import pytest

from app.utils.design_tokens import Animations

pytestmark = pytest.mark.usefixtures("qapp")

_MOTION_ON = pytest.mark.skipif(
    not Animations.motion_enabled(),
    reason="系统已开启「减少动态效果」，动画类断言不适用",
)


# ── 规范：token 关系 ──────────────────────────────────────────────


def test_animations_directional_tokens_ordered():
    """方向语义时长必须满足 按下 < hover < 退出 < 进入 < 大重排

    这组序关系是"手感规则"的载体：进入比退出长（展开让人看清）、退出比
    进入短（不拖沓）、hover 最短（快速划过不发钝）。被改乱就等于手感规则失效。
    """
    assert (
        Animations.PRESS_MS
        < Animations.HOVER_MS
        < Animations.EXIT_MS
        < Animations.ENTER_MS
        < Animations.SLOW_MS
    )


def test_animations_has_directional_curves():
    """存在方向语义曲线：进场/离场分开，且离场不用 OutCubic

    OutCubic 在轴向尺寸收起的尾段拖尾明显（后 12.5% 距离磨掉一半时间 →
    "收到一半顿一下"），所以离场必须是 OutQuad。
    """
    from PyQt5.QtCore import QEasingCurve

    assert Animations.EASE_ENTER == QEasingCurve.OutCubic
    assert Animations.EASE_EXIT == QEasingCurve.OutQuad
    assert Animations.EASE_HOVER == QEasingCurve.OutQuad


# ── 行为：retarget ────────────────────────────────────────────────


@_MOTION_ON
def test_retarget_starts_from_current_value(qapp):
    """起点必须是**当前实测值**，不是 0 / 上次终点（反向跳变的根因）"""
    from PyQt5.QtCore import QVariantAnimation
    from PyQt5.QtWidgets import QWidget

    from app.utils.motion import retarget

    host = QWidget()
    anim = QVariantAnimation(host)
    assert retarget(anim, 100.0, 300.0, duration=200) is True
    assert abs(float(anim.startValue()) - 100.0) < 0.5
    assert abs(float(anim.endValue()) - 300.0) < 0.5


@_MOTION_ON
def test_retarget_interrupt_resumes_without_jump(qapp):
    """动画中途反向：起点 = 中断时的实际值（不先跳到终值再动）"""
    from PyQt5.QtCore import QVariantAnimation
    from PyQt5.QtWidgets import QWidget

    from app.utils.motion import retarget

    host = QWidget()
    anim = QVariantAnimation(host)
    retarget(anim, 0.0, 400.0, duration=200)
    anim.setCurrentTime(100)
    mid = float(anim.currentValue())
    assert 0.0 < mid < 400.0
    assert retarget(anim, mid, 50.0, duration=200) is True
    assert abs(float(anim.startValue()) - mid) < 0.5, "反向起点必须续接当前值"


def test_retarget_skips_subpixel_change(qapp):
    """<1px 的变化不值得一次完整动画（否则是无意义的抖动）"""
    from PyQt5.QtCore import QVariantAnimation
    from PyQt5.QtWidgets import QWidget

    from app.utils.motion import retarget

    host = QWidget()
    anim = QVariantAnimation(host)
    assert retarget(anim, 50.0, 50.2, duration=200) is False
    assert retarget(anim, 50.0, 50.0, duration=200) is False


def test_retarget_returns_false_when_motion_disabled(qapp, monkeypatch):
    """系统「减少动态效果」时必须返回 False，由调用方直接落终值"""
    from PyQt5.QtCore import QVariantAnimation
    from PyQt5.QtWidgets import QWidget

    from app.utils import motion as motion_mod

    monkeypatch.setattr(motion_mod.Animations, "motion_enabled", classmethod(lambda cls: False))
    host = QWidget()
    anim = QVariantAnimation(host)
    assert motion_mod.retarget(anim, 0.0, 300.0, duration=200) is False


# ── 行为：HeightAnimator ──────────────────────────────────────────


@_MOTION_ON
def test_height_animator_releases_min_and_max(qapp):
    """动画结束必须**成对**恢复 min/max

    只放开 max 会让控件永久卡死在动画末值高度（这是改造前的一个真实坑）。
    """
    from PyQt5.QtTest import QTest
    from PyQt5.QtWidgets import QWidget

    from app.utils.motion import HeightAnimator

    box = QWidget()
    box.resize(200, 0)
    box.show()
    animator = HeightAnimator(box)
    assert animator.animate_to(300, duration=60) is True
    QTest.qWait(160)
    assert abs(box.height() - 300) <= 2, f"高度未到位: {box.height()}"
    assert box.minimumHeight() == 0
    assert box.maximumHeight() == HeightAnimator.UNLIMITED


# ── 行为：LoopTimer ───────────────────────────────────────────────


def test_loop_timer_respects_visibility_gate(qapp):
    """宿主不可见时零回调（桌宠/面板隐藏后不再空转重绘）"""
    from PyQt5.QtTest import QTest
    from PyQt5.QtWidgets import QWidget

    from app.utils.motion import LoopTimer

    hidden = QWidget()  # 故意不 show()
    ticks = {"n": 0}
    clock = LoopTimer(hidden, 10, lambda: ticks.__setitem__("n", ticks["n"] + 1))
    clock.start()
    QTest.qWait(80)
    clock.stop()
    assert ticks["n"] == 0


def test_loop_timer_respects_custom_gate(qapp):
    """自定义门控为假时不回调，为真时恢复回调"""
    from PyQt5.QtTest import QTest
    from PyQt5.QtWidgets import QWidget

    from app.utils.motion import LoopTimer

    host = QWidget()
    host.show()
    ticks = {"n": 0}
    gate = {"on": False}
    clock = LoopTimer(host, 10, lambda: ticks.__setitem__("n", ticks["n"] + 1), gate=lambda: gate["on"])
    clock.start()
    QTest.qWait(80)
    assert ticks["n"] == 0
    gate["on"] = True
    QTest.qWait(80)
    clock.stop()
    assert ticks["n"] > 0
    assert clock.running is False


# ── 静态守卫：改造过的调用点不得退化 ──────────────────────────────

_SRC = {
    "motion": "app/utils/motion.py",
    "sidebar": "app/widgets/sidebar_hover_preview.py",
    "tree": "app/widgets/workspace_tree.py",
    "titlebar": "app/widgets/custom_title_bar.py",
    "tabwin": "app/widgets/tab_manager_window.py",
    "tabpanel": "app/widgets/tab_panel.py",
    "scrollbtn": "app/widgets/scroll_to_bottom_button.py",
    "viewer": "app/widgets/markdown_block_viewer.py",
    "card": "app/widgets/cards/card_container.py",
    "stop_btn": "app/widgets/stop_button.py",
    "pet": "app/widgets/pixel_pet.py",
    "arc": "plugins/assistant_hub/ui/arc_stack.py",
    "subagent": "app/widgets/cards/floating/sub_agent_compact_widget.py",
}


def _read(key: str) -> str:
    with open(_SRC[key], encoding="utf-8") as f:
        return f.read()


@pytest.mark.parametrize(
    "key,needle",
    [
        # hover / 箭头 / 树展开：连点不丢事件、从当前值续接
        ("sidebar", "retarget("),
        ("tree", "retarget("),
        ("tabpanel", "retarget("),
        # 无障碍：这些文件改造前 0 检查
        ("titlebar", "motion_enabled()"),
        ("tabwin", "motion_enabled()"),
        ("card", "motion_enabled()"),
        ("arc", "motion_enabled()"),
        # 循环动画门控
        ("stop_btn", "LoopTimer"),
        ("subagent", "LoopTimer"),
    ],
)
def test_static_guards(key, needle):
    """关键调用点必须保留统一动效层提供的机制（防止后续改回手写实现）"""
    assert needle in _read(key), f"{_SRC[key]} 丢失 {needle}"


@pytest.mark.parametrize(
    "key",
    ["sidebar", "tree", "titlebar", "tabpanel", "arc"],
)
def test_no_hardcoded_duration_regression(key):
    """不得再出现裸的 setDuration(<字面量>)，一律走 Animations token

    允许 ``setDuration(0)``（禁用插值的显式语义）。
    """
    import re

    src = _read(key)
    bad = [m for m in re.findall(r"setDuration\(\s*(\d+)\s*\)", src) if m != "0"]
    assert not bad, f"{_SRC[key]} 仍有硬编码时长: {bad}"


def test_pet_pauses_all_loops_on_hide():
    """桌宠隐藏时必须暂停**全部**循环定时器（改造前只停了 _raise_timer）"""
    src = _read("pet")
    hide = src[src.index("def hideEvent") : src.index("def _pause_loops")]
    assert "_pause_loops()" in hide, "hideEvent 必须调用 _pause_loops"
    # timer 清单是类级元组，_pause_loops 遍历它
    attrs = src[src.index("_LOOP_TIMER_ATTRS") : src.index("def _pause_loops")]
    for name in ('"_frame_timer"', '"_sleep_timer"', '"_idle_behavior_timer"', '"_attention_timer"'):
        assert name in attrs, f"隐藏时漏停 {name}"
    # 显示时按当前状态恢复
    assert "_resume_loops()" in src


def test_pet_tracks_animations_with_cleanup():
    """桌宠动画列表必须可回收（改造前只 append 不清理 = 稳定泄漏）"""
    src = _read("pet")
    assert "def _track_animation" in src
    assert "self._track_animation(" in src
    # 裸 append 只允许出现在 _track_animation 内部（那里是统一入口）；
    # 其余调用点必须走 _track_animation，且它会连 finished → 摘除。
    # 按整行匹配，避开 docstring 里对旧写法的引述。
    code_lines = [ln.strip() for ln in src.splitlines()]
    assert code_lines.count("self._animations.append(anim)") == 1, "调用点不得绕过 _track_animation"
    assert "self._animations.remove(anim)" in "\n".join(code_lines), "_track_animation 必须自动摘除"
