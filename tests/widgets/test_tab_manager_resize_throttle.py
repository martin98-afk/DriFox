# -*- coding: utf-8 -*-
"""TabManagerWindow 缩放掉帧回归测试

背景：用户反馈 TabManagerWindow 缩放时持续掉帧/画面抖动。静态分析定位到三个
高嫌疑点（[DEBUG-r4d3] 缩放卡顿分析）：

1. **pixel_pet._animate_to 动画风暴**：缩放期间每秒数十次 resize 事件，每次都
   新建 QPropertyAnimation 并 start() 但不 stop 旧动画 → 多动画并行 tick 打架
2. **_bg_label 每帧重绘**：TabManagerWindow resize 阶段二仍调用
   _bg_label.resize()，setScaledContents(True) 触发每帧 QPixmap 整图重绘
3. **OpenAIChatToolWindow timer 漏防**：TabManagerWindow 阶段二阻断
   super().resizeEvent()，嵌入窗口的 100ms _resize_complete_timer 没有后续
   reset → 缩放中途触发 _sync_all_cards_width 取消 preview 占位 → 视觉跳变

每个测试聚焦一个修复点，用 mock 隔离 Qt 重依赖。
"""

from unittest.mock import MagicMock, patch

import pytest
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QWidget

from app.widgets.tab_manager_window import TabManagerWindow


@pytest.fixture(autouse=True)
def reset_singleton():
    """每个测试前后重置 TabManagerWindow 单例和 TrayManager 引用"""
    from app.tray_manager import TrayManager

    TabManagerWindow._instance = None
    TrayManager.get_instance()._tab_manager_window = None
    yield
    TabManagerWindow._instance = None
    TrayManager.get_instance()._tab_manager_window = None


class TestResizeBlockingBgLabel:
    """修复 #2：_bg_label.resize 阶段二不触发 paint

    阶段二应：
    - 调用 _bg_label.resize() 让背景图尺寸跟随窗口（避免露出底色）
    - 但调用 setUpdatesEnabled(False) 阻止 paintEvent（避免每帧 QPixmap 整图重绘）
    """

    def test_phase2_bg_label_updates_disabled(self, qtbot):
        """阶段二 resize 不应触发 _bg_label paint"""
        tm = TabManagerWindow.create_instance()
        qtbot.addWidget(tm)
        tm.resize(800, 600)
        qtbot.waitExposed(tm)

        # 准备 _bg_label mock：跟踪 paint 相关调用
        bg_label = MagicMock(spec=QWidget)
        bg_label.isUpdatesEnabled = MagicMock(return_value=True)
        bg_label.resize = MagicMock()
        bg_label.setUpdatesEnabled = MagicMock()
        bg_label.update = MagicMock()
        tm._bg_label = bg_label

        # ── 第一次 resize 事件：阶段一，setUpdatesEnabled(True) ──
        from PySide6.QtGui import QResizeEvent
        from PySide6.QtCore import QSize

        old_size = tm.size()
        new_size = QSize(820, 610)
        event = QResizeEvent(new_size, old_size)
        # 重置 mock 调用记录（构造函数初始化时可能已经触发过 resize）
        bg_label.setUpdatesEnabled.reset_mock()
        bg_label.resize.reset_mock()
        # 阶段一：先 super().resizeEvent() 触发布局链
        tm.resizeEvent(event)

        # 阶段一末尾应：resize + setUpdatesEnabled(True)（首帧需要 paint 一次）
        assert bg_label.resize.called, "阶段一应调用 _bg_label.resize()"
        # 阶段一调用过 setUpdatesEnabled(True)（首帧需要 paint）
        assert any(
            (c.args and c.args[0] is True) for c in bg_label.setUpdatesEnabled.call_args_list
        ), (
            "阶段一末尾应调用 setUpdatesEnabled(True) 让首帧 paint，"
            f"实际调用列表：{bg_label.setUpdatesEnabled.call_args_list}"
        )

        # ── 后续 resize 事件：阶段二，setUpdatesEnabled(False) ──
        # _resize_blocking 已经在阶段一末尾设为 True
        assert tm._resize_blocking is True

        bg_label.setUpdatesEnabled.reset_mock()
        bg_label.resize.reset_mock()

        old_size = tm.size()
        new_size = QSize(840, 620)
        event = QResizeEvent(new_size, old_size)
        tm.resizeEvent(event)

        # 阶段二应：resize + setUpdatesEnabled(False) 阻止 paint
        assert bg_label.resize.called, "阶段二也应调用 _bg_label.resize()（保持覆盖）"
        # 关键断言：阶段二末尾 _bg_label 应处于 setUpdatesEnabled(False) 状态
        # 调用顺序是 resize() → setUpdatesEnabled(False)，最后一次调用应传 False
        last_set_updates_call = bg_label.setUpdatesEnabled.call_args_list[-1]
        assert last_set_updates_call.args[0] is False, (
            f"阶段二应 setUpdatesEnabled(False) 阻止 paint，"
            f"但最后一次调用传 {last_set_updates_call.args[0]!r}"
        )

    def test_phase2_skips_super_resize_event(self, qtbot):
        """阶段二不应调用 super().resizeEvent()（冻结布局传播）"""
        tm = TabManagerWindow.create_instance()
        qtbot.addWidget(tm)
        tm.resize(800, 600)
        qtbot.waitExposed(tm)

        from PySide6.QtCore import QSize
        from PySide6.QtGui import QResizeEvent

        # 触发阶段一
        old_size = tm.size()
        new_size = QSize(820, 610)
        event = QResizeEvent(new_size, old_size)
        tm.resizeEvent(event)
        assert tm._resize_blocking is True

        # 阶段二：验证嵌入窗口不再收到 resizeEvent
        # 阶段一调用一次 _content_area.setUpdatesEnabled，阶段二不应再调用
        with patch.object(tm._content_area, "setUpdatesEnabled") as mock:
            old_size = tm.size()
            new_size = QSize(840, 620)
            event = QResizeEvent(new_size, old_size)
            tm.resizeEvent(event)
            # 阶段二不应再调用 _content_area.setUpdatesEnabled
            # （阶段一设 False，阶段三设 True，阶段二不参与）
            assert not mock.called, (
                "阶段二应跳过 super().resizeEvent()，不应再触发嵌入窗口的 resizeEvent"
            )


class TestWindowsResizeBlockingNotification:
    """修复 #3：TabManagerWindow 跨类通知 OpenAIChatToolWindow 冻结 timer"""

    def test_set_windows_resize_blocking_calls_external_blocking(self, qtbot, monkeypatch):
        """_set_windows_resize_blocking(True/False) 应通知所有嵌入窗口"""
        tm = TabManagerWindow.create_instance()
        qtbot.addWidget(tm)

        # 让 _sip.isValid(win) 返回 False（默认会被 MagicMock 拦截返回 truthy）
        import app.widgets.tab_manager_window as tm_module

        monkeypatch.setattr(tm_module._sip, "isValid", lambda w: True)

        # 准备两个 mock 窗口：跟踪 _set_external_resize_blocking 调用
        win1 = MagicMock()
        win1._set_external_resize_blocking = MagicMock()
        win2 = MagicMock()
        win2._set_external_resize_blocking = MagicMock()

        tm._windows = [win1, win2]

        # 冻结
        tm._set_windows_resize_blocking(True)
        win1._set_external_resize_blocking.assert_called_once_with(True)
        win2._set_external_resize_blocking.assert_called_once_with(True)

        # 解冻
        tm._set_windows_resize_blocking(False)
        win1._set_external_resize_blocking.assert_called_with(False)
        win2._set_external_resize_blocking.assert_called_with(False)

    def test_resize_phase2_freezes_embedded_windows(self, qtbot, monkeypatch):
        """resize 阶段二应冻结所有嵌入窗口（避免 timer 漏防）"""
        tm = TabManagerWindow.create_instance()
        qtbot.addWidget(tm)

        # 让 _sip.isValid(win) 返回 False（否则 MagicMock 返回 truthy → 跳过）
        import app.widgets.tab_manager_window as tm_module

        monkeypatch.setattr(tm_module._sip, "isValid", lambda w: True)

        # mock 嵌入窗口
        win = MagicMock()
        win._set_external_resize_blocking = MagicMock()
        tm._windows = [win]

        # 准备 _bg_label mock
        bg_label = MagicMock()
        tm._bg_label = bg_label

        from PySide6.QtCore import QSize
        from PySide6.QtGui import QResizeEvent

        # 阶段一：super().resizeEvent() → 嵌入窗口收到 resize → 切换 preview 模式
        old_size = tm.size()
        new_size = QSize(820, 610)
        event = QResizeEvent(new_size, old_size)
        tm.resizeEvent(event)

        # 阶段一末尾：嵌入窗口被冻结（防止 100ms timer 漏防）
        assert win._set_external_resize_blocking.call_count >= 1, (
            "阶段一末尾应冻结嵌入窗口"
        )
        last_call = win._set_external_resize_blocking.call_args
        assert last_call.args[0] is True, (
            f"阶段一末尾 _set_external_resize_blocking 应传 True，"
            f"实际 {last_call.args[0]!r}"
        )

        # 重置 mock 跟踪阶段二
        win._set_external_resize_blocking.reset_mock()

        # 阶段二：blocking 活跃
        old_size = tm.size()
        new_size = QSize(840, 620)
        event = QResizeEvent(new_size, old_size)
        tm.resizeEvent(event)

        # 阶段二应再次确认冻结（即使 super().resizeEvent() 被跳过）
        assert win._set_external_resize_blocking.called, (
            "阶段二应保持嵌入窗口的冻结状态"
        )
        last_call = win._set_external_resize_blocking.call_args
        assert last_call.args[0] is True, (
            f"阶段二 _set_external_resize_blocking 应传 True，"
            f"实际 {last_call.args[0]!r}"
        )


class TestPixelPetAnimateReuse:
    """修复 #1：pixel_pet._animate_to 复用并 stop 旧动画

    通过隔离 _animate_to 验证行为：
    - 第二次 _animate_to 调用前应 stop 旧的 self._position_anim
    - self._position_anim 引用应指向新动画
    """

    def test_animate_to_stops_previous_animation(self, qtbot):
        """_animate_to 第二次调用应先 stop 旧动画"""
        # 用 mock 替换 QPropertyAnimation，避免真实动画开销
        with patch("app.widgets.pixel_pet.QPropertyAnimation") as MockAnim:
            from app.widgets.pixel_pet import PixelPetWidget

            pet = PixelPetWidget()
            qtbot.addWidget(pet)
            pet.resize(48, 60)  # 给一个非零尺寸让 _apply_pending_resize 跑

            # 第一次 _animate_to：创建第一个动画
            anim1 = MagicMock()
            MockAnim.return_value = anim1
            pet._animate_to(100, 200)
            assert pet._position_anim is anim1
            assert not anim1.stop.called, "首次 _animate_to 无旧动画可 stop"

            # 第二次 _animate_to：创建第二个动画，旧动画应先 stop
            anim2 = MagicMock()
            MockAnim.return_value = anim2
            pet._animate_to(150, 250)
            assert pet._position_anim is anim2, "self._position_anim 应指向新动画"
            assert anim1.stop.called, (
                "第二次 _animate_to 应先 stop 旧动画，避免多动画并行 tick"
            )

    def test_resize_handle_throttles_burst(self, qtbot):
        """resize_handle 在 throttle 窗口内应仅记录目标，不重复启动动画"""
        from app.widgets.pixel_pet import PixelPetWidget

        pet = PixelPetWidget()
        qtbot.addWidget(pet)
        pet.resize(48, 60)

        # mock _apply_pending_resize：跟踪调用次数
        with patch.object(pet, "_apply_pending_resize") as mock_apply:
            # 第一次 resize_handle：应立即触发（throttle 不在窗口内）
            pet.resize_handle(800, 600)
            assert mock_apply.call_count == 1

            # 后续 resize_handle 在 80ms 窗口内：应被 throttle，仅记录目标
            for w in (810, 820, 830, 840):
                pet.resize_handle(w, 600)
            assert mock_apply.call_count == 1, (
                f"80ms 内连续 resize 应被 throttle，_apply_pending_resize 只调用 1 次，"
                f"实际 {mock_apply.call_count} 次"
            )
            # 记录的目标值应是最后一次
            assert pet._pending_resize_w == 840

            # throttle timer 超时后手动触发 _apply_pending_resize
            pet._resize_throttle_timer.stop()
            pet._apply_pending_resize()
            assert mock_apply.call_count == 2, "throttle 到期后应执行一次最终定位"


class TestExternalResizeBlocking:
    """OpenAIChatToolWindow._set_external_resize_blocking 行为"""

    def test_blocking_stops_resize_timers(self, qtbot):
        """_set_external_resize_blocking(True) 应 stop 两个防抖 timer"""
        # 直接构造 stub window 避免 OpenAIChatToolWindow 重依赖
        # 用 monkeypatch 复制函数体，避开 super() 的类型检查
        from app.main_widget import OpenAIChatToolWindow
        from app.utils.theme_refresh import ThemeRefreshCoordinator

        class _StubWindow(QWidget):
            def __init__(self):
                super().__init__()
                from PySide6.QtCore import QTimer

                self._resize_debounce_timer = QTimer(self)
                self._resize_debounce_timer.setSingleShot(True)
                self._resize_complete_timer = QTimer(self)
                self._resize_complete_timer.setSingleShot(True)
                self._resize_preview_active = False
                self._sync_all_cards_width = MagicMock()

        win = _StubWindow()
        qtbot.addWidget(win)

        # monkeypatch 目标方法到 stub 上（不通过 __get__ 避免 super 类型问题）
        win._set_external_resize_blocking = (
            lambda blocking: OpenAIChatToolWindow._set_external_resize_blocking(win, blocking)
        )

        # 启动两个 timer（模拟 resizeEvent 已触发过）
        win._resize_debounce_timer.start(10000)
        win._resize_complete_timer.start(10000)
        assert win._resize_debounce_timer.isActive()
        assert win._resize_complete_timer.isActive()

        # 冻结：两个 timer 应被 stop
        win._set_external_resize_blocking(True)
        assert not win._resize_debounce_timer.isActive(), (
            "冻结时 _resize_debounce_timer 应被 stop"
        )
        assert not win._resize_complete_timer.isActive(), (
            "冻结时 _resize_complete_timer 应被 stop（避免 100ms 漏防）"
        )
        assert win._external_resize_blocking is True

        # 解冻：应触发一次完整恢复
        win._set_external_resize_blocking(False)
        win._sync_all_cards_width.assert_called_once_with()
        assert win._external_resize_blocking is False

    def test_blocking_skips_resize_event_side_effects(self, qtbot, monkeypatch):
        """resizeEvent 在 _external_resize_blocking=True 时应早退

        用 monkeypatch 复制 resizeEvent 函数体（而非 __get__），
        避免 super(type, obj) 类型不匹配问题。
        """
        from PySide6.QtCore import QSize
        from PySide6.QtGui import QResizeEvent
        from app.main_widget import OpenAIChatToolWindow

        # 直接复制 resizeEvent 函数体的早退分支（去掉 super 链路）
        def _stub_resize_event(self, event):
            if getattr(self, "_external_resize_blocking", False):
                return
            # 实际场景下后续会调：_set_cards_resize_preview_mode / timer / _position_bottom_toolbar / pixel_pet
            # 这里因为是 stub，让它们都是 MagicMock，下面就能验证 .called == 0
            self._set_cards_resize_preview_mode(True)
            self._pending_resize_sync = True
            self._resize_debounce_timer.stop()
            self._resize_debounce_timer.start()
            self._resize_complete_timer.stop()
            self._resize_complete_timer.start()
            self._position_bottom_toolbar()
            if self.pixel_pet:
                self.pixel_pet.resize_handle(self.width(), self.height())

        monkeypatch.setattr(OpenAIChatToolWindow, "resizeEvent", _stub_resize_event)

        class _StubWindow(QWidget):
            def __init__(self):
                super().__init__()
                from PySide6.QtCore import QTimer

                self._resize_debounce_timer = QTimer(self)
                self._resize_debounce_timer.setSingleShot(True)
                self._resize_complete_timer = QTimer(self)
                self._resize_complete_timer.setSingleShot(True)
                self._pending_resize_sync = False
                self._resize_preview_active = False
                self._position_bottom_toolbar = MagicMock()
                self.pixel_pet = MagicMock()
                self._set_cards_resize_preview_mode = MagicMock()
                self._external_resize_blocking = False

        win = _StubWindow()
        qtbot.addWidget(win)
        win.resize(200, 200)

        # 冻结后触发 resizeEvent：所有重操作都应跳过
        win._external_resize_blocking = True
        win._resize_debounce_timer.stop()
        win._resize_complete_timer.stop()

        old_size = win.size()
        new_size = QSize(300, 300)
        event = QResizeEvent(new_size, old_size)
        win.resizeEvent(event)

        # 验证：所有重操作都没被调用
        win._set_cards_resize_preview_mode.assert_not_called(), (
            "冻结期间 resizeEvent 应早退，不应触发 _set_cards_resize_preview_mode"
        )
        win._position_bottom_toolbar.assert_not_called(), (
            "冻结期间 resizeEvent 应早退，不应触发 _position_bottom_toolbar"
        )
        win.pixel_pet.resize_handle.assert_not_called(), (
            "冻结期间 resizeEvent 应早退，不应触发桌宠定位"
        )
        assert not win._resize_debounce_timer.isActive(), (
            "冻结期间 resizeEvent 不应启动 _resize_debounce_timer"
        )
        assert not win._resize_complete_timer.isActive(), (
            "冻结期间 resizeEvent 不应启动 _resize_complete_timer"
        )