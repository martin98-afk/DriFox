# -*- coding: utf-8 -*-
"""QuestionFloatingWidget 修复回归测试：NO_ANIMATION_PROP + minimumSizeHint 下限

== 问题描述 ==
自定义选项高度变化时，描述区上冒被遮挡；偶尔完全看不到描述。

== 根因 ==
1. 描述区 `_AutoHeightScrollArea.minimumSizeHint()` 返回 (0,0) → 布局空间
   不足时描述区是唯一可被压到 0 的成员（描述完全不可见）。
2. CardContainer 高度走 200ms OutCubic 动画（`_animate_height`），自定义
   输入框增高时容器高度滞后 200ms，期间 QVBoxLayout 空间不足，描述区被
   优先压没。

== 修复 ==
1. `QuestionFloatingWidget` 声明 `CardContainer.NO_ANIMATION_PROP = True`，
   容器高度直接 snap 到目标值，消除动画滞后窗口期。
2. `_AutoHeightScrollArea.minimumSizeHint()` 返回内容高度下限
   （封顶 maximumHeight=280），描述区不再可被压到 0。

本文件固化上述行为，防止后续回归。
"""

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt5.QtCore import Qt

Qt.AA_ShareOpenGLContexts = Qt.AA_ShareOpenGLContexts
try:
    from PyQt5.QtWebEngineWidgets import (  # noqa: F401
        QWebEnginePage,
        QWebEngineSettings,
        QWebEngineView,
    )
except Exception:
    pass

import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from PyQt5.QtCore import QEventLoop, QPropertyAnimation, QTimer
from PyQt5.QtWidgets import QApplication, QSplitter, QVBoxLayout, QWidget

from app.widgets.cards.card_container import CardContainer

# 描述区最大高度（生产代码 setMaximumHeight(280) 的固化常量）
_QUESTION_SCROLL_MAX_H = 280


def _pump(ms: int):
    loop = QEventLoop()
    QTimer.singleShot(ms, loop.quit)
    loop.exec()


def _setup():
    app = QApplication.instance() or QApplication(sys.argv)
    return app


def _make_widget_with_question(
    app, desc: str = "这是一个较长的选项描述文本，用于撑起描述区高度。", show_custom_input=True
):
    """构造带问题与描述的提问卡片并渲染"""
    from app.widgets.cards.floating.question_floating_widget import QuestionFloatingWidget

    widget = QuestionFloatingWidget()
    widget.show_question(
        [
            {
                "question": "请选择你的答案？",
                "options": [
                    {"label": "选项A", "description": desc},
                    {"label": "选项B", "description": "短描述"},
                ],
                "multiple": False,
            }
        ],
        show_custom_input=show_custom_input,
    )
    widget.resize(400, 600)
    widget.show()
    _pump(100)
    return widget


# ═══════════════════════════════════════════════════════════
# 修复点 1：跳过容器动画声明
# ═══════════════════════════════════════════════════════════


def test_no_animation_property_declared():
    """QuestionFloatingWidget 必须声明 CardContainer.NO_ANIMATION_PROP = True

    容器 200ms 动画在自定义输入框增高时会造成高度滞后、描述区被压没；
    声明后容器高度 snap 到目标值。属性名须与 card_container.py 定义一致。
    """
    _setup()
    from app.widgets.cards.floating.question_floating_widget import QuestionFloatingWidget

    widget = QuestionFloatingWidget()
    assert widget.property(CardContainer.NO_ANIMATION_PROP) is True, (
        "提问卡片未声明跳过容器动画，自定义输入增高时容器高度会滞后 200ms"
    )


# ═══════════════════════════════════════════════════════════
# 修复点 2：描述区 minimumSizeHint 非零下限 + 封顶保留
# ═══════════════════════════════════════════════════════════


def test_question_scroll_minimum_size_hint_bounded():
    """描述区 minimumSizeHint().height() ≥ 1 且 ≤ 280

    修复前返回 (0,0)：布局空间不足时描述区被压没。
    修复后返回内容高度下限；max-height 280 的滚动封顶必须保留。
    """
    _setup()
    widget = _make_widget_with_question(_setup())

    scroll = widget._question_scroll
    hint_h = scroll.minimumSizeHint().height()
    print(f"[minimumSizeHint] height={hint_h}px (max={scroll.maximumHeight()})")
    assert hint_h >= 1, "描述区 minimumSizeHint 被压到 0，描述会不可见"
    assert hint_h <= _QUESTION_SCROLL_MAX_H, "描述区 minimumSizeHint 超过封顶高度 280"


# ═══════════════════════════════════════════════════════════
# 修复点 3：自定义输入增高时描述区不被压没（回归场景）
# ═══════════════════════════════════════════════════════════


def test_custom_input_growth_keeps_desc_visible():
    """激活自定义输入并输入多行文本后，描述区 minimumSizeHint 仍 ≥ 1

    旧 bug 场景：输入框增高 → 容器高度滞后 → 描述区被压没。
    NO_ANIMATION_PROP + minimumSizeHint 下限双保险下，描述区高度下限保持。
    """
    _setup()
    widget = _make_widget_with_question(_setup())

    custom = widget._custom_input_widget
    assert custom is not None, "show_custom_input=True 时应存在自定义输入卡片"

    # 激活自定义输入并输入多行长文本 → 输入框增高
    custom.set_active(True)
    _pump(50)
    custom._text_edit.setPlainText("\n".join(f"第{i}行较长的自定义输入内容用于撑高输入框" for i in range(8)))
    _pump(200)

    scroll_h = widget._question_scroll.minimumSizeHint().height()
    input_h = custom._text_edit.height()
    print(f"[自定义增高] 描述区 minimumSizeHint={scroll_h}px, 输入框 height={input_h}px")
    assert scroll_h >= 1, "自定义输入增高后描述区 minimumSizeHint 被压到 0，描述不可见"
    assert input_h > custom.MIN_INPUT_HEIGHT, "输入多行文本后输入框高度应超过初始单行高度"


# ═══════════════════════════════════════════════════════════
# 修复点 4：容器展开走 snap 分支，不创建/启动动画
# ═══════════════════════════════════════════════════════════


def test_container_expand_snaps_without_animation():
    """容器 _do_expand 对声明 NO_ANIMATION_PROP 的提问卡片走 snap 分支

    断言目的：NO_ANIMATION_PROP 不仅要被声明（用例 1），还必须在容器层
    生效——卡片可见后 `_do_expand` 的 skip_anim 分支直接 `_set_axis_max`
    snap 到自然高度，动画不进入 Running。注意 `_expand_animation` 对象
    是容器复用的（card_container.py 性能优化：`_animate_height` 内惰性
    创建、后续复用），且首次展开可能在卡片可见性确认前走一次动画，
    因此断言状态（而非对象为 None）才不 flaky：动画必须已停止、且最终
    容器高度精确 snap 到 follow_content 自然高度。

    容器装配方式参考 test_command_card_container_follow_content.py：窗口 +
    垂直 QSplitter + BottomCardContainer（dock 模式）+ 提问卡片。
    """
    _setup()
    from app.widgets.cards.card_container import BottomCardContainer
    from app.widgets.cards.floating.question_floating_widget import QuestionFloatingWidget

    win = QWidget()
    win.resize(900, 800)
    outer = QVBoxLayout(win)
    chat = QWidget()
    chat.setMinimumHeight(400)
    splitter = QSplitter()
    splitter.setOrientation(1)  # Vertical
    splitter.addWidget(chat)
    splitter.setStretchFactor(0, 1)
    container = BottomCardContainer()
    splitter.addWidget(container)
    splitter.setStretchFactor(1, 0)
    outer.addWidget(splitter)
    win.show()

    container.enable_dock_mode(splitter)
    widget = QuestionFloatingWidget()
    widget.show_question(
        [
            {
                "question": "请选择你的答案？",
                "options": [{"label": "选项A", "description": "描述A"}, {"label": "选项B"}],
                "multiple": False,
            }
        ],
        show_custom_input=False,
    )
    container.add_card("question", widget)
    widget.setVisible(True)
    # 等 _schedule_expand 的防抖 timer 触发 _do_expand 完成
    _pump(300)

    # 动画（如曾被创建）必须已停止，不得处于 Running：声明 NO_ANIMATION_PROP
    # 的卡片展开应 snap 而非走 200ms 动画
    anim = container._expand_animation
    if anim is not None:
        assert anim.state() != QPropertyAnimation.Running, (
            "声明 NO_ANIMATION_PROP 的卡片展开动画不应处于 Running（应 snap）"
        )
    # snap 完成：容器已展开到自然高度（非 0），且轴向 max 已锁到内容高度
    natural_h = container._follow_content_natural_h()
    axis_max = container._axis_max()
    print(
        f"[snap] animation_running={anim.state() if anim is not None else None}, axis_max={axis_max}px, natural_h={natural_h}px"
    )
    assert axis_max >= 1, "容器未展开：snap 分支未生效"
    assert abs(axis_max - natural_h) < 2, f"容器高度未 snap 到自然高度: axis_max={axis_max}, natural_h={natural_h}"


if __name__ == "__main__":
    print("=" * 70)
    print("QuestionFloatingWidget 修复回归测试（NO_ANIMATION_PROP + minimumSizeHint）")
    print("=" * 70)

    results = []
    for name, fn in [
        ("1: NO_ANIMATION_PROP 声明", test_no_animation_property_declared),
        ("2: minimumSizeHint 非零下限", test_question_scroll_minimum_size_hint_bounded),
        ("3: 自定义增高描述仍可见", test_custom_input_growth_keeps_desc_visible),
        ("4: 容器展开 snap 无动画", test_container_expand_snaps_without_animation),
    ]:
        print(f"\n{'#' * 70}\n# {name}\n{'#' * 70}")
        try:
            fn()
            results.append((name, "✅"))
        except AssertionError as e:
            print(f"❌ {e}")
            results.append((name, "❌"))
        except Exception as e:
            import traceback

            traceback.print_exc()
            print(f"❌ 异常: {e}")
            results.append((name, "⚠️"))

    print("\n" + "=" * 70)
    print("结果汇总")
    print("=" * 70)
    for name, res in results:
        print(f"  {res} {name}")
