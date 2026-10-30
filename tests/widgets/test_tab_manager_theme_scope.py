# -*- coding: utf-8 -*-
"""[T30] 回归：tab_manager 主题串拆分顶层三容器可见区刷新。

源码断言：静态串零色值、三补刷点接线、_WorkbenchFrame 类级置脏默认。
行为断言：未 show 三 frame 置脏 + 静态串非空；show 后补刷清脏且串非空；
切色值 → 可见容器即时更新 / 隐藏容器仅置脏。
"""

import sys

from PyQt5.QtCore import Qt
from PyQt5.QtWidgets import QApplication, QFrame

QApplication.setAttribute(Qt.AA_ShareOpenGLContexts, True)
_APP = QApplication.instance() or QApplication(sys.argv)

from types import SimpleNamespace  # noqa: E402

import pytest  # noqa: E402

from app.utils.design_tokens import Colors  # noqa: E402
from app.widgets.tab_manager_window import TabManagerWindow, _ThemedPanel, _WorkbenchFrame  # noqa: E402

_SRC = None


def _src() -> str:
    global _SRC
    if _SRC is None:
        from pathlib import Path

        # 行尾归一（文件混合行尾，锚匹配统一 LF 口径）
        _SRC = (Path(__file__).resolve().parents[2] / "app" / "widgets" / "tab_manager_window.py").read_text(
            encoding="utf-8"
        ).replace("\r\n", "\n")
    return _SRC


class _TMStub(QFrame):
    """绑定 TabManagerWindow 真实主题方法的轻量窗口桩（不建 1500+ 子树）。"""

    _apply_theme_stylesheet = TabManagerWindow._apply_theme_stylesheet
    _refresh_theme_dirty_frames = TabManagerWindow._refresh_theme_dirty_frames
    _qss_tab_frame = TabManagerWindow._qss_tab_frame
    _qss_chat_frame = TabManagerWindow._qss_chat_frame
    _qss_workbench_frame = TabManagerWindow._qss_workbench_frame

    def _reapply_splitter_handle_styles(self):
        pass

    def __init__(self):
        super().__init__()
        self._tab_frame = _ThemedPanel(self)
        self._tab_frame.setObjectName("tabFrame")
        # chat/workbench 已改自绘容器（颜色在 paintEvent，不在 QSS）
        self._chat_frame = _ThemedPanel(self)
        self._chat_frame.setObjectName("chatFrame")
        self._workbench_frame = _WorkbenchFrame(self)
        self._workbench_frame.setObjectName("workbenchFrame")
        self._splitter = None


# ── 源码断言 ──


def test_src_window_qss_has_no_theme_colors():
    """窗口串零色值（普通三引号，非 f-string）：底色改 _WindowBaseLayer 自绘

    2026-10-30 契约变更：窗口本体底色原走 f-string 三引号串（内含 Colors.CONTENT_BG），
    主题一变整串变 → setStyleSheet 触发整窗 1500+ 控件 repolish（实测 600~800ms）。
    改为 `background: transparent` 恒串 + `_WindowBaseLayer.paintEvent` 自绘。
    """
    s = _src().find('qss = """')
    assert s != -1, "窗口串应为普通三引号（恒串，无 f-string 插值）"
    body = _src()[s : _src().find('"""', s + 10)]
    assert "CARD_BG" not in body, "静态串不得含 CARD_BG 色值（拆到容器串）"
    assert "BORDER" not in body, "静态串不得含 BORDER 色值"
    assert "CONTENT_BG" not in body, "窗口底色不得写回窗口串（应走 _WindowBaseLayer 自绘）"
    assert "background: transparent" in body
    # 自绘层仍承担窗口底色
    assert "class _WindowBaseLayer" in _src(), "窗口底色层类缺失"
    base_body = _src()[_src().find("class _WindowBaseLayer") : _src().find("class _WorkbenchFrame")]
    assert "Colors.CONTENT_BG" in base_body, "底色层应绘制 Colors.CONTENT_BG"
    assert "WA_TransparentForMouseEvents" in base_body, "底色层必须鼠标穿透"


def test_src_window_qss_not_fstring():
    """窗口串为普通三引号：无需 {{ 转义，花括号保持字面单括号形态"""
    s = _src().find('qss = """')
    assert s != -1, "窗口串应为普通三引号（恒串，无 f-string 插值）"
    body = _src()[s : _src().find('"""', s + 10)]
    assert "#tabManagerWindow {" in body, "普通串里花括号应为单括号"
    assert "{{" not in body, "普通串里不得残留 f-string 双花括号转义"


def test_src_apply_has_visibility_gating():
    """_apply_theme_stylesheet 含 isVisible 门控 + 不可见置脏"""
    body = _src()[_src().find("def _apply_theme_stylesheet") : _src().find("def _apply_bg_from_theme")]
    assert "frame.isVisible()" in body
    assert "frame._theme_needs_refresh = True" in body
    assert "frame._theme_needs_refresh = False" in body


def test_src_three_refresh_hooks():
    """三个可见性恢复点都调 _refresh_theme_dirty_frames"""
    src = _src()
    for anchor in (
        "self._set_windows_resize_preview_suppressed(False)",
        'self._wb_visible_target = None  # 回挂收起后回到"看 isVisible"，关闭态 False',
        # showEvent 尾：_refresh_mac_fullsize_content 调用后紧跟补刷（if _IS_MAC 多处出现，用组合串）
        "            self._refresh_mac_fullsize_content()\n        # [T30] 窗口重新显示可见性恢复点",
    ):
        at = src.find(anchor)
        assert at != -1, f"锚缺失: {anchor[:60]}"
        tail = src[at : at + 300]
        assert "self._refresh_theme_dirty_frames()" in tail, f"锚后未接入补刷: {anchor[:60]}"


def test_src_workbench_frame_class_default():
    """_WorkbenchFrame 类级置脏默认 False（getattr 防御的兜底基线）"""
    assert "_theme_needs_refresh: bool = False" in _src()


# ── 行为断言 ──


def _make_stub():
    return _TMStub()


def test_behavior_hidden_frames_dirty_until_shown():
    """未 show：三 frame 置脏；show 后补刷清脏

    2026-10-30 契约变更：chat/workbench 改 _ThemedPanel 自绘后，主题色不再走
    QSS，补刷后这两个容器的串应为**空**；tabFrame 仍走 QSS，串含主题色。
    """
    stub = _make_stub()
    stub._apply_theme_stylesheet()
    # 静态串非空且不含色值
    assert stub.styleSheet().strip() != ""
    assert "CARD_BG" not in stub.styleSheet()
    # 未 show（isVisible False）→ 三 frame 全部置脏
    for f in (stub._tab_frame, stub._chat_frame, stub._workbench_frame):
        assert f._theme_needs_refresh is True, "不可见容器应置脏跳过重刷"

    # show 模拟（真实可见）→ 补刷清脏 + 各容器按新契约校验
    stub.show()  # 父窗口可见后子 isVisible 才为 True
    for f in (stub._tab_frame, stub._chat_frame, stub._workbench_frame):
        f.show()
    stub._refresh_theme_dirty_frames()
    for f in (stub._tab_frame, stub._chat_frame, stub._workbench_frame):
        assert f._theme_needs_refresh is False, "补刷后应清脏"
    # 三容器均已自绘：主题色不进 QSS（否则每次切主题都要整子树 repolish）
    for f in (stub._tab_frame, stub._chat_frame, stub._workbench_frame):
        assert "rgba(" not in f.styleSheet(), "自绘容器不得把主题色写回 QSS"


def test_behavior_construction_phase_no_frames_attr():
    """[T30-P0] 构造期未创建 frame 属性：直接调 _apply_theme_stylesheet 不得炸

    真机崩溃根因：_setup_ui 早期调用本方法时 _workbench_frame 尚未创建，
    直接属性访问抛 AttributeError。getattr 防御后未创建属正常态（跳过，
    静态串照常设置），后续主题切换/showEvent 补刷兜住。
    """
    stub = _TMStub.__new__(_TMStub)  # 不跑 __init__：模拟 frame 属性未创建
    QFrame.__init__(stub)
    assert not hasattr(stub, "_tab_frame")
    stub._apply_theme_stylesheet()  # 不抛即通过
    assert stub.styleSheet().strip() != "", "静态串应照常设置（几何/透明规则与 frame 无关）"

    # 随后创建 frame → 再调用 → 正常门控逻辑（未 show 全部置脏）
    stub._tab_frame = QFrame(stub)
    stub._chat_frame = QFrame(stub)
    stub._workbench_frame = _WorkbenchFrame(stub)
    stub._apply_theme_stylesheet()
    for f in (stub._tab_frame, stub._chat_frame, stub._workbench_frame):
        assert f._theme_needs_refresh is True, "构造后未 show 的 frame 应置脏"


def test_behavior_window_qss_invariant_under_theme_change():
    """窗口串对主题 token 不敏感：切 CONTENT_BG 后串不变（同串短路恒命中）"""
    stub = _make_stub()
    stub._apply_theme_stylesheet()
    base_before = stub.styleSheet()
    assert "transparent" in base_before

    real_bg = Colors.CONTENT_BG
    try:
        Colors.CONTENT_BG = "#0A0A0F"
        stub._apply_theme_stylesheet()
        assert stub.styleSheet() == base_before, "窗口串必须恒定，否则每轮切主题都整窗 repolish"
    finally:
        Colors.CONTENT_BG = real_bg


def test_behavior_color_switch_updates_visible_and_dirties_hidden():
    """切色值：可见容器即时更新为新色；隐藏容器仅置脏不更新"""
    stub = _make_stub()
    stub._apply_theme_stylesheet()
    stub.show()  # 父窗口可见后子 isVisible 才为 True
    stub._tab_frame.show()  # tab 可见
    stub._chat_frame.hide()  # 显式隐藏：模拟置脏场景
    stub._workbench_frame.hide()
    # chat/workbench 隐藏
    stub._refresh_theme_dirty_frames()
    assert stub._tab_frame._theme_needs_refresh is False

    real_card_bg = Colors.CARD_BG
    try:
        Colors.CARD_BG = SimpleNamespace(format=lambda alpha: "#123ABC")
        Colors.BORDER = "#456DEF"
        stub._apply_theme_stylesheet()
        # 2026-10-30：三容器改自绘后，切主题不再写 QSS（零 repolish）；
        # 可见容器靠 paintEvent 重绘取新色，所以串里不得出现新色值。
        for f in (stub._tab_frame, stub._chat_frame, stub._workbench_frame):
            assert "#123ABC" not in f.styleSheet(), "自绘容器切主题不得重写 QSS"
        assert stub._tab_frame._theme_needs_refresh is False
        # 隐藏的 chatFrame 只置脏
        assert stub._chat_frame._theme_needs_refresh is True, "隐藏容器应置脏"
    finally:
        Colors.CARD_BG = real_card_bg
        Colors.BORDER = "#456DEF" if not hasattr(real_card_bg, "format") else Colors.BORDER
