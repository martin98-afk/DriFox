# 大模型输入框
import os
import re

from PyQt5.QtCore import Qt, pyqtSignal, QTimer
from PyQt5.QtGui import QKeyEvent, QKeySequence, QTextCursor, QColor, QTextCharFormat
from PyQt5.QtWidgets import QGraphicsDropShadowEffect
from PyQt5.QtWidgets import QShortcut, QWidget, QVBoxLayout
from qfluentwidgets import FluentIcon, ComboBox
from qfluentwidgets import TextEdit, TransparentToolButton

from app.utils.utils import get_font_family_css
from app.utils.design_tokens import Colors, font_size_css

# 预编译正则表达式
_FILE_PREFIX_PATTERN = re.compile(r'^file:/{1,3}')


class SendableTextEdit(TextEdit):
    sendMessageRequested = pyqtSignal()
    stopMessageRequested = pyqtSignal()
    clearRequested = pyqtSignal()
    newSessionRequested = pyqtSignal()
    historyUpRequested = pyqtSignal()
    historyDownRequested = pyqtSignal()
    agentChanged = pyqtSignal(str)
    slashTriggered = pyqtSignal(str)     # 检测到 / 触发，携带查询文本
    slashDismissed = pyqtSignal()        # / 触发结束

    def __init__(self, parent=None):
        super().__init__(parent)
        self._initializing = True
        self._glow_effect = None

        self._setup_glow_effect()
        self._apply_input_style()
        self.setPlaceholderText("给 DriFox 发送消息，Enter 发送，Shift+Enter 换行")
        self.setAcceptRichText(False)
        self.setLineWrapMode(TextEdit.WidgetWidth)
        self.setAcceptDrops(True)
        self.setMinimumHeight(52)
        self.setMaximumHeight(180)
        self.setFixedHeight(52)

        self._agent_combo = ComboBox(self)
        self._agent_combo.setFixedSize(75, 28)
        self._agent_combo.setStyleSheet(self._build_combo_style())
        self._agent_combo.currentTextChanged.connect(self._on_agent_changed)

        self.send_btn = TransparentToolButton(FluentIcon.SEND, self)
        self.send_btn.setFixedSize(34, 34)
        self.send_btn.setToolTip("发送（Enter）")
        self.send_btn.clicked.connect(self._on_send_click)
        self.send_btn.setDisabled(True)
        self._apply_send_btn_style()
        self.textChanged.connect(self._on_text_changed)
        self.textChanged.connect(self._on_slash_trigger_check)

        # 关闭 qfluentwidgets TextEdit 焦点时的底部高亮
        if hasattr(self, 'layer'):
            self.layer.hide()

        self._setup_keyboard_shortcuts()

        # 命令卡片引用（由 main_widget 注入）
        self._command_card_ref = None
        self._slash_trigger_pos = -1  # / 触发位置

        # 节流相关
        self._slash_throttle_timer = QTimer(self)
        self._slash_throttle_timer.setSingleShot(True)
        self._slash_throttle_timer.timeout.connect(self._on_slash_throttle_timeout)
        self._pending_slash_query = ""
        self._last_slash_trigger_time = 0  # 上次触发时间（毫秒）
        self._slash_trigger_count = 0  # 快速触发计数

        # 输入历史浏览
        self._history_list: list = []          # 最近输入历史（最新在前）
        self._history_index: int = -1          # -1 = 不在浏览模式
        self._suppress_slash_trigger: bool = False  # 切换历史时临时阻止 / 触发

        # 使用 QTimer.singleShot(0, ...) 在事件循环启动后重置初始化标志
        QTimer.singleShot(0, self._finish_initialization)

    def _apply_send_btn_style(self):
        """从 Colors 应用发送按钮样式"""
        from app.utils.design_tokens import Colors
        Colors.refresh()
        self.send_btn.setStyleSheet(f"""
            TransparentToolButton {{
                background: qlineargradient(x1:0, y1:0, x2:1, y2:1,
                    stop:0 {Colors.SEND_BTN_START}, stop:1 {Colors.SEND_BTN_END});
                border: none;
                border-radius: 17px;
                color: white;
            }}
            TransparentToolButton:hover {{
                background: qlineargradient(x1:0, y1:0, x2:1, y2:1,
                    stop:0 {Colors.SEND_BTN_HOVER_START}, stop:1 {Colors.SEND_BTN_HOVER_END});
            }}
            TransparentToolButton:disabled {{
                background: rgba(255, 255, 255, 0.10);
                color: rgba(255, 255, 255, 0.45);
            }}
        """)

    def _finish_initialization(self):
        """初始化完成后重置标志，允许高度调整"""

    def set_command_card(self, card):
        """注入命令卡片引用（由 main_widget 创建并注册）"""
        self._command_card_ref = card
        card.commandSelected.connect(self._on_command_selected)
        card.dismissed.connect(self._on_card_dismissed)

    def _get_card(self):
        """获取命令卡片引用"""
        return self._command_card_ref

    def _on_slash_trigger_check(self):
        """检测 / 触发——仅在开头（位置0）的 / 触发命令卡片，支持节流"""
        # 历史浏览模式下，如果当前历史项以 / 开头，阻止命令卡片触发
        if self._suppress_slash_trigger:
            self._suppress_slash_trigger = False
            card = self._get_card()
            if card and card.is_card_visible:
                card.dismiss()
                self.slashDismissed.emit()
            return

        card = self._get_card()
        try:
            cursor = self.textCursor()
            text = self.toPlainText()
            cursor_pos = cursor.position()

            if cursor_pos < 0 or cursor_pos > len(text):
                return

            # 仅当 / 在文本开头（位置0）时触发
            text_before_cursor = text[:cursor_pos]
            
            # 检查开头是否有 /
            if text.startswith("/"):
                query = text[1:cursor_pos] if cursor_pos > 1 else ""
                # 如果有空格或换行，说明 / 触发已结束
                if " " in query or "\n" in query:
                    self._cancel_slash_throttle()
                    if card and card.is_card_visible:
                        self.slashDismissed.emit()
                    self._slash_trigger_pos = -1
                    return

                # 在开头触发 - 使用节流
                self._slash_trigger_pos = 0
                self._apply_slash_throttle(query)
            else:
                # 没有在开头
                self._cancel_slash_throttle()
                if card and card.is_card_visible:
                    self.slashDismissed.emit()
                self._slash_trigger_pos = -1
        except Exception:
            pass

    def _apply_slash_throttle(self, query: str):
        """应用节流逻辑：快速输入时降低触发频率"""
        import time
        # 计算时间间隔（毫秒）
        current_ms = int(time.time() * 1000)
        time_delta = current_ms - self._last_slash_trigger_time if self._last_slash_trigger_time > 0 else 1000
        self._last_slash_trigger_time = current_ms
        
        # 判断输入速度：小于 150ms 认为快速输入
        is_fast_input = time_delta < 150 and self._slash_trigger_count > 0
        
        if is_fast_input:
            self._slash_trigger_count += 1
            # 快速输入模式：更新待发送的 query，延长计时器
            self._pending_slash_query = query
            # 节流延迟：20ms（数据缓存后渲染仅需 ~1ms，可以降延迟提升响应速度）
            throttle_delay = 20
            self._slash_throttle_timer.stop()
            self._slash_throttle_timer.start(throttle_delay)
        else:
            self._slash_trigger_count = 0
            # 正常速度：直接发射信号
            self._cancel_slash_throttle()
            self.slashTriggered.emit(query)
    
    def _on_slash_throttle_timeout(self):
        """节流定时器超时：发射最终的 query"""
        if self._slash_trigger_pos >= 0:
            self.slashTriggered.emit(self._pending_slash_query)
    
    def _cancel_slash_throttle(self):
        """取消节流定时器"""
        self._slash_throttle_timer.stop()
        self._pending_slash_query = ""
        self._slash_trigger_count = 0

    def insert_command_text(self, item_name: str):
        """将选中的命令/技能文本插入输入框（由 main_widget 调用）"""
        cursor = self.textCursor()
        text = self.toPlainText()
        cursor_pos = cursor.position()

        trigger_pos = self._slash_trigger_pos

        if trigger_pos >= 0:
            cursor.setPosition(trigger_pos)
            cursor.setPosition(cursor_pos, QTextCursor.KeepAnchor)

            # 确定插入格式：命令用 /xxx，技能用 @xxx
            from app.core.command_manager import CommandManager
            is_command = CommandManager.get_instance().is_known_command_name(item_name)
            insert_prefix = "/" if is_command else "@"

            insert_text = f"{insert_prefix}{item_name} "
            cursor.insertText(insert_text)

            cursor.setPosition(trigger_pos + len(insert_text))
            self.setTextCursor(cursor)

        self._slash_trigger_pos = -1
        self.setFocus(Qt.OtherFocusReason)

    def _on_command_selected(self, item_name: str):
        """命令/技能被选中（由 CommandCard.commandSelected 触发）"""
        card = self._get_card()
        self.insert_command_text(item_name)
        if card:
            card.dismiss()
        self.slashDismissed.emit()

    def _on_card_dismissed(self):
        """卡片被关闭时的清理"""
        self._slash_trigger_pos = -1

    # ==================== 输入历史浏览 ====================

    def load_history(self, history_list: list):
        """从外部加载输入历史列表"""
        self._history_list = list(history_list)
        self._history_index = -1

    def _enter_history_mode(self):
        """进入历史浏览模式：加载最新一条"""
        if not self._history_list:
            return
        # 进入历史模式时，隐藏命令卡片
        card = self._get_card()
        if card and card.is_card_visible:
            card.dismiss()
            self.slashDismissed.emit()
        self._suppress_slash_trigger = False
        self._history_index = 0
        self._set_history_text()

    def _set_history_text(self):
        """根据当前 history_index 设置输入框文本"""
        if 0 <= self._history_index < len(self._history_list):
            text = self._history_list[self._history_index]
            # ⚠️ 必须在 setPlainText 之前设置抑制标志！
            # 否则 textChanged → _on_slash_trigger_check 先执行，标志还没设上
            # 导致：首次用户按键被错误抑制（标志延后生效），卡片延迟一个按键才出现
            self._suppress_slash_trigger = text.strip().startswith("/")
            self.setPlainText(text)
            # 选中全部文本，方便继续编辑
            cursor = self.textCursor()
            cursor.movePosition(QTextCursor.End)
            cursor.movePosition(QTextCursor.Start, QTextCursor.KeepAnchor)
            self.setTextCursor(cursor)

    def _navigate_history(self, direction: int):
        """方向导航：1 = 更旧（Up），-1 = 更新（Down）"""
        if not self._history_list:
            return

        if self._history_index < 0:
            # 不在浏览模式
            if direction == 1:  # Up → 进入模式
                self._enter_history_mode()
            return

        new_index = self._history_index + direction

        if new_index >= len(self._history_list):
            # 超过最旧条目，停留在最旧
            return

        if new_index < 0:
            # 超过最新条目，退出浏览模式
            self._history_index = -1
            self.clear()
            return

        self._history_index = new_index
        self._set_history_text()

    def _reset_history_mode(self):
        """退出历史浏览模式"""
        self._history_index = -1

    def _tab_complete_if_card_visible(self):
        """Tab 补全：卡片可见时选中当前项"""
        card = self._get_card()
        if card and card.is_card_visible:
            card.select_current()
        self._slash_trigger_pos = -1

    def _on_agent_changed(self, text: str):
        self.agentChanged.emit(text)

    def _setup_keyboard_shortcuts(self):
        self._shortcut_clear = QShortcut(QKeySequence("Ctrl+L"), self)
        self._shortcut_clear.activated.connect(self._on_clear_shortcut)

        self._shortcut_new = QShortcut(QKeySequence("Ctrl+N"), self)
        self._shortcut_new.activated.connect(self._on_new_session_shortcut)

    def _on_clear_shortcut(self):
        self.clearRequested.emit()

    def _on_new_session_shortcut(self):
        self.newSessionRequested.emit()

    def _on_text_changed(self):
        has_text = bool(self.toPlainText().strip())
        # 在停止模式下，按钮应该始终可用（用于停止正在进行的请求）
        # 只在发送模式下才根据文本内容决定是否启用
        if not getattr(self, '_is_stop_mode', False):
            self.send_btn.setDisabled(not has_text)
        # 文本变化时总是需要调整高度，不管是否在停止模式
        if not getattr(self, '_initializing', False):
            self._adjust_height_to_content()

    def _adjust_height_to_content(self):
        """根据内容自动调整高度"""
        if getattr(self, '_initializing', False):
            return
        
        doc = self.document()
        content_height = int(doc.size().height()) + 24
        new_height = max(44, min(160, content_height))

        if self.height() != new_height:
            self.setFixedHeight(new_height)
            if self.parent():
                self.parent().updateGeometry()
                self.updateGeometry()

    def _rebind_send_btn(self, handler):
        try:
            self.send_btn.clicked.disconnect()
        except TypeError:
            pass
        self.send_btn.clicked.connect(handler)

    def toggle_send_button(self, enable: bool):
        """启用/禁用发送按钮"""
        if enable:
            self._is_stop_mode = False
            self.send_btn.setIcon(FluentIcon.SEND)
            self.send_btn.setToolTip("发送（Enter）")
            self._rebind_send_btn(self._on_send_click)
            self._on_text_changed()
            # 发送完成后，确保输入框高度重置（即使在停止模式下也可能需要调整高度）
            self._adjust_height_to_content()
        else:
            self._is_stop_mode = True
            self.send_btn.setIcon(FluentIcon.PAUSE)
            self.send_btn.setToolTip("停止")
            self.send_btn.setDisabled(False)  # 停止模式下按钮应该始终可用
            self._rebind_send_btn(self._on_stop_click)

        # 同步到外部工具栏按钮（如果有的话）
        self._sync_external_send_btn()

    def _sync_external_send_btn(self):
        """不再需要外部同步，发送按钮在输入框内部"""
        pass

    def _on_send_click(self):
        """发送按钮点击事件"""
        if not self.toPlainText().strip():
            return
        self.toggle_send_button(False)
        self.sendMessageRequested.emit()

    def _on_stop_click(self):
        """停止按钮点击事件"""
        self.toggle_send_button(True)
        self.stopMessageRequested.emit()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._position_send_button()

    def _position_send_button(self):
        """定位发送按钮到输入框右下角"""
        if self.send_btn:
            btn_size = self.send_btn.size()
            send_btn_x = self.width() - btn_size.width() - 10
            send_btn_y = self.height() - btn_size.height() - 8
            self.send_btn.move(max(0, send_btn_x), max(0, send_btn_y))

    def keyPressEvent(self, event: QKeyEvent):
        # 历史浏览模式下，↑↓ 始终导航历史，不受命令卡片影响
        in_history_mode = self._history_index >= 0

        card = self._get_card()
        # 先检查命令卡片是否可见（但历史浏览模式时跳过）
        if card and card.is_card_visible and not in_history_mode:
            if event.key() == Qt.Key_Down:
                card.select_next()
                event.accept()
                return
            elif event.key() == Qt.Key_Up:
                card.select_prev()
                event.accept()
                return
            elif event.key() in (Qt.Key_Return, Qt.Key_Enter, Qt.Key_Tab):
                card.select_current()
                event.accept()
                return
            elif event.key() == Qt.Key_Escape:
                card.dismiss()
                self.slashDismissed.emit()
                event.accept()
                return

        # Tab 键：开头有 / 时触发补全
        if event.key() == Qt.Key_Tab:
            text = self.toPlainText()
            if text.startswith("/"):
                # 模拟 / 触发，然后选择当前项
                self._slash_trigger_pos = 0
                self.slashTriggered.emit(text[1:] if len(text) > 1 else "")
                # 延迟选中（等待卡片加载）
                QTimer.singleShot(10, lambda: self._tab_complete_if_card_visible())
                event.accept()
                return

        if event.key() in (Qt.Key_Return, Qt.Key_Enter):
            if event.modifiers() & Qt.ShiftModifier:
                super().keyPressEvent(event)  # 换行
            else:
                self._on_send_click()
                event.accept()
        elif event.key() == Qt.Key_Up:
            if self._history_index >= 0 or not self.toPlainText():
                # 历史浏览模式，或在空输入框按↑
                self._navigate_history(1)
                event.accept()
            elif event.modifiers() & Qt.ControlModifier:
                self.historyUpRequested.emit()
                event.accept()
            else:
                super().keyPressEvent(event)
        elif event.key() == Qt.Key_Down:
            if self._history_index >= 0:
                # 历史浏览模式
                self._navigate_history(-1)
                event.accept()
            elif event.modifiers() & Qt.ControlModifier:
                self.historyDownRequested.emit()
                event.accept()
            else:
                super().keyPressEvent(event)
        else:
            super().keyPressEvent(event)

    def insertFromMimeData(self, source):
        """重写以处理拖放的文本格式化和高亮"""
        try:
            # 首先检查是否是真正的文件拖拽（通过 URLs）
            is_file_drop = False
            file_paths = []  # 收集所有拖入的文件路径
            
            if source.hasUrls():
                urls = source.urls()
                if urls:
                    file_paths = [url.toLocalFile() for url in urls if url.toLocalFile()]
                    is_file_drop = True
            elif source.hasText():
                text = source.text()
                
                # 文本内容拖入：逐行解析文件路径
                # 只有实际存在的路径才被认为是文件
                if "file:/" in text:
                    try:
                        lines = text.split("\n")
                        for line in lines:
                            path = _FILE_PREFIX_PATTERN.sub('', line)
                            if path and os.path.exists(path):
                                file_paths.append(path)
                        if file_paths:
                            is_file_drop = True
                    except Exception:
                        pass
                elif "\n" in text:
                    try:
                        lines = text.split("\n")
                        for line in lines:
                            if line and os.path.isabs(line) and os.path.exists(line):
                                file_paths.append(line)
                        if file_paths:
                            is_file_drop = True
                    except Exception:
                        pass
            
            if is_file_drop and file_paths:
                try:
                    # 保存默认格式
                    cursor = self.textCursor()
                    default_format = QTextCharFormat()  # 创建干净的默认格式
                    
                    # 先插入一个空格占位符，用默认格式
                    cursor.insertText(" ", default_format)
                    
                    # 准备要插入的文件路径文本——所有文件
                    insert_text = "\n".join([f"路径: {p}" for p in file_paths])
                    
                    # 记录文件路径的起始位置
                    path_start = cursor.position()
                    
                    # 插入文件路径文本
                    cursor.insertText(insert_text)
                    
                    # 记录文件路径的结束位置
                    path_end = cursor.position()
                    
                    # 高亮显示拖入的文件路径
                    cursor.setPosition(path_start)
                    cursor.setPosition(path_end, QTextCursor.KeepAnchor)
                    
                    # 创建高亮格式 - 使用和技能一样的金色
                    highlight_format = QTextCharFormat()
                    highlight_format.setForeground(QColor("#C9A85C"))
                    highlight_format.setFontWeight(700)
                    cursor.setCharFormat(highlight_format)
                    
                    # 最后再插入一个空格，用默认格式
                    cursor.setPosition(path_end)
                    cursor.clearSelection()
                    cursor.insertText(" ", default_format)
                    
                    # 确保光标在最后，使用默认格式
                    final_pos = cursor.position()
                    cursor.setPosition(final_pos)
                    cursor.setCharFormat(default_format)
                    self.setTextCursor(cursor)
                    
                    # 确保输入框有焦点
                    self.setFocus(Qt.OtherFocusReason)
                    
                    return
                except Exception:
                    # 如果文件路径插入失败，回退到默认处理
                    pass
            
            # 其他情况使用默认处理
            super().insertFromMimeData(source)
            
        except Exception as e:
            # 捕获所有异常，确保应用不会崩溃
            try:
                # 发生任何错误时，回退到默认处理
                super().insertFromMimeData(source)
            except Exception:
                # 最后的保障
                pass

    def _setup_glow_effect(self):
        """设置输入卡片发光效果 — 挂载到父卡片而非输入框自身"""
        self._glow_effect = QGraphicsDropShadowEffect(self)
        self._glow_effect.setBlurRadius(0)
        self._glow_effect.setColor(QColor(201, 168, 92, 0))
        self._glow_effect.setOffset(0, 0)
        # 延迟挂载：等 input_area 加入 _input_card 后再设置
        self._glow_target = None

    def _apply_input_style(self):
        """应用输入框样式 - 融入卡片，无边框"""
        Colors.refresh()
        self.setStyleSheet(f"""
            QTextEdit {{
                background: transparent;
                color: {Colors.INPUT_TEXT};
                border: none;
                border-radius: 16px 16px 0 0;
                padding: 12px 52px 12px 20px;
                selection-background-color: rgba(201, 168, 92, 0.28);
                {get_font_family_css()} {font_size_css(15)};
            }}
            QTextEdit:focus {{
                border: none;
                color: {Colors.INPUT_FOCUS_TEXT};
            }}
            QTextEdit QScrollBar:vertical {{
                background: transparent;
                width: 0px;
                margin: 0;
            }}
            QTextEdit QScrollBar::handle:vertical {{
                background: rgba(255, 255, 255, 0.15);
                border-radius: 3px;
                min-height: 20px;
            }}
            QTextEdit QScrollBar::handle:vertical:hover {{
                background: rgba(255, 255, 255, 0.25);
            }}
            QTextEdit QScrollBar::add-line:vertical,
            QTextEdit QScrollBar::sub-line:vertical {{
                height: 0px;
            }}
            QTextEdit QScrollBar::add-page:vertical,
            QTextEdit QScrollBar::sub-page:vertical {{
                background: none;
            }}
        """)

    def _build_combo_style(self) -> str:
        """构建智能体下拉框样式"""
        Colors.refresh()
        return f"""
            ComboBox {{
                background-color: rgba(255, 255, 255, 0.05);
                color: {Colors.INPUT_TEXT};
                border: 1px solid {Colors.INPUT_BORDER};
                border-radius: 10px;
                padding: 3px 10px;
                {get_font_family_css()} {font_size_css(12)};
            }}
            ComboBox:hover {{
                background-color: rgba(255, 255, 255, 0.08);
                border-color: {Colors.INPUT_FOCUS_BORDER};
            }}
            ComboBox::drop-down {{
                border: none;
                width: 16px;
            }}
            ComboBox::down-arrow {{
                border-left: 4px solid transparent;
                border-right: 4px solid transparent;
                border-top: 5px solid {Colors.INPUT_TEXT};
                margin-right: 2px;
            }}
            ComboBox AbstractItemView {{
                background-color: {Colors.CONTENT_BG};
                color: {Colors.INPUT_TEXT};
                selection-background-color: {Colors.TEXT_ACCENT};
                border: 1px solid {Colors.INPUT_BORDER};
                border-radius: 10px;
                padding: 4px;
            }}
        """

    def refresh_style(self):
        """刷新样式（响应主题切换）"""
        self._apply_input_style()
        if hasattr(self, '_agent_combo') and self._agent_combo:
            self._agent_combo.setStyleSheet(self._build_combo_style())
        
    def _animate_glow(self, target_blur, target_alpha, duration=300):
        """动画发光效果 - 作用到父级 _input_card 的边框"""
        if not self._glow_effect:
            return
        try:
            # 延迟挂载发光效果到父卡片
            if self._glow_target is None:
                card = self.parent()
                while card and not hasattr(card, '_input_card'):
                    card = card.parent()
                if card and hasattr(card, '_input_card'):
                    self._glow_target = card._input_card
                    self._glow_target.setGraphicsEffect(self._glow_effect)
            if self._glow_target:
                self._glow_effect.setBlurRadius(target_blur)
                color = QColor(201, 168, 92, target_alpha)
                self._glow_effect.setColor(color)
                # 焦点时高亮边框颜色
                if target_alpha > 0:
                    self._glow_target.setStyleSheet(f"""
                        QWidget {{
                            background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
                                stop:0 {Colors.INPUT_FOCUS_BG_START},
                                stop:1 {Colors.INPUT_FOCUS_BG_END});
                            border: 2px solid {Colors.INPUT_FOCUS_BORDER};
                            border-radius: 16px;
                        }}
                    """)
                else:
                    self._glow_target.setStyleSheet(f"""
                        QWidget {{
                            background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
                                stop:0 {Colors.INPUT_BG_START},
                                stop:1 {Colors.INPUT_BG_END});
                            border: 1px solid {Colors.INPUT_BORDER};
                            border-radius: 16px;
                        }}
                    """)
        except Exception:
            pass

    def focusInEvent(self, event):
        try:
            super().focusInEvent(event)
            self._animate_glow(25, 180, 250)
            QTimer.singleShot(0, self._ensure_cursor_visible)
        except Exception:
            pass
        
    def focusOutEvent(self, event):
        try:
            super().focusOutEvent(event)
            self._animate_glow(0, 0, 200)
        except Exception:
            pass

    def _ensure_cursor_visible(self):
        cursor = self.textCursor()
        if cursor.position() > 0:
            self.ensureCursorVisible()

    def mousePressEvent(self, event):
        # 点击时退出历史浏览模式
        if self._history_index >= 0:
            self._reset_history_mode()
        # 点击时隐藏命令卡片
        card = self._get_card()
        if card and card.is_card_visible:
            card.dismiss()
            self.slashDismissed.emit()
        super().mousePressEvent(event)

    def wheelEvent(self, event):
        # 滚轮时隐藏命令卡片
        card = self._get_card()
        if card and card.is_card_visible:
            card.dismiss()
            self.slashDismissed.emit()
        super().wheelEvent(event)

    def clear(self):
        """重写 clear 方法，清空输入时退出历史浏览模式"""
        self._reset_history_mode()
        super().clear()
