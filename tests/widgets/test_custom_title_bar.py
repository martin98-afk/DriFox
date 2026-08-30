"""CustomTitleBar 单元测试：tab 增删/激活/信号/主题刷新/mac 分支"""

import sys

import pytest
from PyQt5.QtWidgets import QWidget

from app.widgets.custom_title_bar import CustomTitleBar

# 内置常驻「聊天」tab id（与 tab_manager_window.CHAT_TAB_ID 一致）
CHAT = "chat"


@pytest.fixture
def container(qtbot):
    """顶栏宿主（模拟窗口）"""
    w = QWidget()
    qtbot.addWidget(w)
    return w


def test_instantiates_with_system_buttons(qtbot, container):
    """实例化：高 38，Windows 下三系统按钮存在（基类内置）"""
    tb = CustomTitleBar(container)
    qtbot.addWidget(tb)
    assert CustomTitleBar.HEIGHT == 38
    assert tb.minimumHeight() == 38
    assert tb.minBtn is not None and tb.maxBtn is not None and tb.closeBtn is not None
    assert tb._is_mac is False
    # 顶栏走极简：不再显示品牌（DriFox + 版本号）
    assert not hasattr(tb, "_brand_title")
    assert not hasattr(tb, "_brand_version")


def test_sidebar_button_transparent_style(qtbot, container):
    """侧栏折叠按钮：透明背景无边框，refresh_style 后样式非空"""
    tb = CustomTitleBar(container)
    qtbot.addWidget(tb)
    tb.refresh_style()
    qss = tb._sidebar_btn.styleSheet()
    assert qss != ""
    assert "background: transparent" in qss
    assert "border: none" in qss


def test_add_tab_sets_active_and_emits_signal(qtbot, container):
    """add_tab 后首个 tab 自动激活；点击发射 tab_clicked 并切激活态"""
    tb = CustomTitleBar(container)
    qtbot.addWidget(tb)
    tb.add_tab("chat", "聊天")
    tb.add_tab("channel", "频道")

    # 首个 tab 自动激活
    assert tb._active_id == "chat"

    received = []
    tb.tab_clicked.connect(received.append)
    tb._tabs["channel"].clicked.emit("channel")
    assert received == ["channel"]
    assert tb._active_id == "channel"
    assert tb._tabs["channel"]._active is True
    assert tb._tabs["chat"]._active is False


def test_add_tab_with_callback(qtbot, container):
    """add_tab 的 on_click 回调随点击触发"""
    tb = CustomTitleBar(container)
    qtbot.addWidget(tb)
    hits = []
    tb.add_tab("chat", "聊天", on_click=lambda: hits.append(1))
    tb._tabs["chat"].clicked.emit("chat")
    assert hits == [1]


def test_remove_tab_reactivates_remaining(qtbot, container):
    """移除激活 tab 后自动激活剩余第一个；移除不存在的 id 不崩"""
    tb = CustomTitleBar(container)
    qtbot.addWidget(tb)
    tb.add_tab("a", "A")
    tb.add_tab("b", "B")
    tb.remove_tab("a")
    assert "a" not in tb._tabs
    assert tb._active_id == "b"
    tb.remove_tab("nonexistent")  # 不抛异常
    tb.remove_tab("b")
    assert tb._active_id is None


def test_closable_tab_emits_close_signal(qtbot, container):
    """closable=True 的 tab 有 × 钮：点击只发 tab_close_clicked，不切换 tab

    full 卡片 tab 为非常驻可关闭形态；常驻 tab（聊天/插件页）无 × 钮。"""
    tb = CustomTitleBar(container)
    qtbot.addWidget(tb)
    tb.add_tab(CHAT, "聊天")
    tb.add_tab("usage", "用量统计", closable=True)

    # 形态差异：× 钮仅 closable tab 存在
    assert tb._tabs[CHAT]._close_btn is None
    assert tb._tabs["usage"]._close_btn is not None

    closes, clicks = [], []
    tb.tab_close_clicked.connect(closes.append)
    tb.tab_clicked.connect(clicks.append)
    tb._tabs["usage"]._close_btn.click()
    assert closes == ["usage"]
    # × 点击不触发整卡切换（子按钮事件不传播给父 widget）
    assert clicks == []
    assert tb._active_id == CHAT


def test_refresh_style_applies_qss(qtbot, container):
    """refresh_style 后激活 tab 的文字样式已应用

    tab 的底色/指示条是自绘（paintEvent 按动画进度插值），文字颜色仍走
    stylesheet，挂在内部 _label 上。
    """
    tb = CustomTitleBar(container)
    qtbot.addWidget(tb)
    tb.add_tab("chat", "聊天")
    tb.refresh_style()
    assert tb._tabs["chat"]._label.styleSheet() != ""


def test_tab_active_animates_progress(qtbot, container):
    """选中态切换：_active 立即翻转，底色进度由动画从 0 走向 1"""
    tb = CustomTitleBar(container)
    qtbot.addWidget(tb)
    tb.add_tab("chat", "聊天")
    tb.add_tab("usage", "用量")
    btn = tb._tabs["usage"]

    assert btn._active is False
    assert btn._active_t == 0.0

    tb.set_active_tab("usage")
    assert btn._active is True  # 状态立即翻转
    assert btn._anim_active.state() == btn._anim_active.Running  # 进度走动画

    # 动画结束后进度收拢到 1
    btn._anim_active.stop()
    btn._anim_active.setCurrentTime(btn._anim_active.duration())
    assert btn._active_t == 1.0


def test_set_active_heals_stuck_progress(qtbot, container):
    """进度卡在中途时，重复 set_active 必须补一次收尾（早期版本是 no-op）

    场景：动画被新事件打断后 ``_active_t`` 停在 0.4，此时若因为「意图没变」
    就直接 return，高亮会永久停在半亮状态、再也无法自愈——这就是
    「选中效果卡住」的成因。
    """
    tb = CustomTitleBar(container)
    qtbot.addWidget(tb)
    tb.add_tab("chat", "聊天")
    btn = tb._tabs["chat"]

    # 人为把进度卡在中途（模拟动画被打断）
    btn._anim_active.stop()
    btn._active_t = 0.4

    tb.set_active_tab("chat")  # 意图未变，但进度没到位 → 必须重启
    assert btn._anim_active.state() == btn._anim_active.Running


def test_set_active_is_noop_when_settled(qtbot, container):
    """进度已到位时重复 set_active 不重启动画

    反向保险：若每次都无条件重启，缓动曲线会被反复重置，进度永远走不到
    1.0（表现为 hover / 选中淡入永远差一口气）。
    """
    tb = CustomTitleBar(container)
    qtbot.addWidget(tb)
    tb.add_tab("chat", "聊天")
    btn = tb._tabs["chat"]

    btn._anim_active.stop()
    btn._active_t = 1.0
    tb.set_active_tab("chat")
    assert btn._anim_active.state() != btn._anim_active.Running


def test_sync_tab_hover_picks_the_tab_under_cursor(qtbot, container, monkeypatch):
    """hover 由光标位置仲裁：至多一个 tab 处于 hover，且是光标下那一个

    ★ 覆盖的是「增删 tab / 居中重排让整组 tab 在光标静止时平移，Qt 不补发
    enter/leave」这一类顽疾——单靠事件边沿无法自愈，必须按 QCursor 重算。
    """
    from PyQt5.QtCore import QPoint

    from app.widgets import custom_title_bar

    tb = CustomTitleBar(container)
    qtbot.addWidget(tb)
    tb.add_tab("chat", "聊天")
    tb.add_tab("usage", "用量")
    container.resize(900, 400)
    container.show()
    qtbot.wait(30)  # 等布局跑完，否则 rect 还是默认几何

    target = tb._tabs["usage"]
    gp = target.mapToGlobal(target.rect().center())
    monkeypatch.setattr(custom_title_bar.QCursor, "pos", staticmethod(lambda: QPoint(gp)))

    tb.sync_tab_hover()
    assert [tid for tid, b in tb._tabs.items() if b._hovered] == ["usage"]

    # 光标移出 tab 区：所有 hover 清空（leaveEvent 可能收不到，靠这层兜底）
    monkeypatch.setattr(
        custom_title_bar.QCursor, "pos", staticmethod(lambda: QPoint(-500, -500))
    )
    tb.sync_tab_hover()
    assert not any(b._hovered for b in tb._tabs.values())


def test_add_and_remove_tab_schedule_hover_resync(qtbot, container):
    """增删 tab 会排一次 hover 重算（合并到同一事件循环，只排一次）"""
    tb = CustomTitleBar(container)
    qtbot.addWidget(tb)
    tb._hover_sync_pending = False

    tb.add_tab("chat", "聊天")
    assert tb._hover_sync_pending is True

    tb._hover_sync_pending = False
    tb.add_tab("usage", "用量")
    tb.remove_tab("usage")
    assert tb._hover_sync_pending is True


def test_label_color_qss_is_cached(qtbot, container):
    """同一进度的 _apply_label_color 不重复 setStyleSheet

    ``setStyleSheet`` 会触发整棵子树的 QSS 重解析 + polish，在 180ms 动画里
    逐帧调用是标题栏掉帧的主要来源；量化缓存把次数从 ~11 降到 ~4。
    """
    tb = CustomTitleBar(container)
    qtbot.addWidget(tb)
    tb.add_tab("chat", "聊天")
    btn = tb._tabs["chat"]

    calls = []
    real = btn._label.setStyleSheet
    btn._label.setStyleSheet = lambda s: (calls.append(s), real(s))

    btn._active_t = 0.5
    btn._apply_label_color()
    assert len(calls) == 1
    btn._apply_label_color()  # 进度未变 → 命中缓存
    assert len(calls) == 1
    btn._active_t = 0.9  # 量化档位变化 → 必须重新应用
    btn._apply_label_color()
    assert len(calls) == 2


def test_mac_branch_hides_system_buttons(qtbot, container, monkeypatch):
    """mac 分支：隐藏三系统按钮，左区留白 ≥70px"""
    monkeypatch.setattr(sys, "platform", "darwin")
    tb = CustomTitleBar(container)
    qtbot.addWidget(tb)
    assert tb._is_mac is True
    assert tb.minBtn.isHidden() and tb.maxBtn.isHidden() and tb.closeBtn.isHidden()
    m = tb.layout().contentsMargins()
    assert m.left() >= 70
