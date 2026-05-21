# 大模型输入框
import os
import re

from PyQt5.QtCore import Qt, pyqtSignal, QTimer, QRect, QPoint, QSize
from PyQt5.QtGui import QKeyEvent, QKeySequence, QTextCursor, QColor, QTextCharFormat
from PyQt5.QtWidgets import QGraphicsDropShadowEffect
from PyQt5.QtWidgets import QShortcut, QWidget, QVBoxLayout, QListWidget, QListWidgetItem, QApplication, QLabel
from qfluentwidgets import FluentIcon, ComboBox
from qfluentwidgets import TextEdit, TransparentToolButton

from app.utils.utils import get_font_family_css, get_local_skills
from app.utils.design_tokens import Colors, font_size_css

# 预编译正则表达式
_FILE_PREFIX_PATTERN = re.compile(r'^file:/{1,3}')


class SkillListItem(QWidget):
    """带高亮的技能列表项"""
    def __init__(self, skill_name: str, query: str, parent=None):
        super().__init__(parent)
        self.setFixedHeight(36)
        self._skill_name = skill_name
        self._query = query
        self.setStyleSheet("""
            SkillListItem {
                background: transparent;
                padding: 4px 12px;
                border-radius: 6px;
            }
            SkillListItem:hover {
                background: rgba(255, 255, 255, 0.08);
            }
        """)
        
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        
        self._label = QLabel()
        self._update_highlight()
        layout.addWidget(self._label)

    def _update_highlight(self):
        """更新高亮显示"""
        name = self._skill_name
        query = self._query
        
        if query:
            # 高亮匹配的字母
            html = ''
            lower_name = name.lower()
            lower_query = query.lower()
            last_end = 0
            for i in range(len(lower_query)):
                idx = lower_name.find(lower_query[i], last_end)
                if idx >= 0:
                    html += name[last_end:idx]
                    html += f'<span style="color: #C9A85C; font-weight: bold;">{name[idx]}</span>'
                    last_end = idx + 1
                else:
                    break
            html += name[last_end:]
            self._label.setText(html)
        else:
            self._label.setText(name)
            
        self._label.setStyleSheet(f"""
            QLabel {{
                color: #EAF2FF;
                {get_font_family_css()} {font_size_css(13)}
            }}
        """)


class SkillCompleterPopup(QWidget):
    """技能补全弹窗"""

    skillSelected = pyqtSignal(str)  # 选择技能信号

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowFlags(Qt.FramelessWindowHint | Qt.Tool | Qt.NoDropShadowWindowHint)
        self.setAttribute(Qt.WA_TranslucentBackground)
        # macOS 上顶层 Tool 窗口更容易抢占输入焦点，补全窗只负责展示和鼠标选择，
        # 键盘事件仍由文本框统一处理。
        self.setAttribute(Qt.WA_ShowWithoutActivating, True)
        self.setFocusPolicy(Qt.NoFocus)
        self.setFixedWidth(220)
        self.setMinimumHeight(40)
        self.setMaximumHeight(280)

        self._list_widget = QListWidget(self)
        self._list_widget.setFrameShape(QListWidget.NoFrame)  # 去掉默认 frame，尺寸完全由样式表控制
        self._list_widget.setFocusPolicy(Qt.NoFocus)
        self._list_widget.setStyleSheet(f"""
            QListWidget {{
                background: rgba(25, 34, 50, 245);
                border: 1px solid #2B3850;
                border-radius: 10px;
                padding: 4px;
            }}
            QListWidget::item {{
                color: #EAF2FF;
                background: transparent;
            }}
            QListWidget::item:selected {{
                background: rgba(139, 115, 85, 0.6);
            }}
            QListWidget::item:hover {{
                background: rgba(255, 255, 255, 0.08);
            }}
        """)
        self._list_widget.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self._list_widget.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self._list_widget.itemClicked.connect(self._on_item_clicked)
        self._list_widget.itemDoubleClicked.connect(self._on_item_double_clicked)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)
        layout.addWidget(self._list_widget)

        self._skills = []
        self._current_query = ""

    def load_skills(self, skills: list, query: str = ""):
        """加载技能列表——只添加匹配的项，不再操作隐藏项"""
        self._skills = skills
        self._current_query = query
        self._list_widget.clear()

        matched_count = 0
        for skill in skills:
            name = skill["name"]
            if query and query.lower() not in name.lower():
                continue  # 不匹配直接跳过，不创建 item
            matched_count += 1
            item = QListWidgetItem()
            item.setData(Qt.UserRole, name)
            widget = SkillListItem(name, query)
            item.setSizeHint(QSize(212, 36))
            self._list_widget.addItem(item)
            self._list_widget.setItemWidget(item, widget)

        # 没有匹配项则隐藏弹窗
        if matched_count == 0:
            self.hide()
            return

        self._list_widget.setCurrentRow(0)
        self._adjust_height()

    def _adjust_height(self):
        """精确计算弹窗高度，基于已知像素值，避免 QListWidget 内部坐标系的歧义"""
        count = self._list_widget.count()
        if count == 0:
            self.hide()
            return

        # 各层空间消耗（像素值均来自样式表/布局设置）：
        #   SkillCompleterPopup 布局 margin:    4 top + 4 bottom = 8
        #   QListWidget 样式表 border:          1 top + 1 bottom = 2
        #   QListWidget 样式表 padding:         4 top + 4 bottom = 8
        #   items (每个 36px):                  count * 36
        #   总计: count * 36 + 18
        item_area = count * 36
        list_border_and_padding = 10  # 1(border-top) + 1(border-bottom) + 4(padding-top) + 4(padding-bottom)
        layout_margins = self.layout().contentsMargins()
        margin_vertical = layout_margins.top() + layout_margins.bottom()

        outer_height = item_area + list_border_and_padding + margin_vertical
        outer_height = min(outer_height, 280)
        self.setFixedHeight(max(40, outer_height))

    def _on_item_clicked(self, item):
        self._select_current()

    def _on_item_double_clicked(self, item):
        self._select_current()

    def _select_current(self):
        """选择当前行（所有行都是可见的）"""
        current_row = self._list_widget.currentRow()
        if current_row < 0 or current_row >= self._list_widget.count():
            return
        skill_name = self._list_widget.item(current_row).data(Qt.UserRole)
        self.skillSelected.emit(skill_name)
        self.hide()

    def key_event(self, event: QKeyEvent):
        """处理键盘事件"""
        key = event.key()
        if key == Qt.Key_Down:
            self._move_selection(1)
            event.accept()
            return True
        elif key == Qt.Key_Up:
            self._move_selection(-1)
            event.accept()
            return True
        elif key in (Qt.Key_Return, Qt.Key_Enter, Qt.Key_Tab):
            self._select_current()
            event.accept()
            return True
        elif key == Qt.Key_Escape:
            self.hide()
            event.accept()
            return True
        return False

    def _move_selection(self, delta: int):
        """移动选择（所有项都是可见的，简化循环逻辑）"""
        count = self._list_widget.count()
        if count == 0:
            return

        current = self._list_widget.currentRow()
        new_idx = current + delta
        if new_idx < 0:
            new_idx = count - 1
        elif new_idx >= count:
            new_idx = 0

        self._list_widget.setCurrentRow(new_idx)

    def show_at_cursor(self, text_edit, cursor_top_global: QPoint, prefer_below: bool = True):
        """在光标位置显示，自动换向避免超出屏幕
        
        Args:
            text_edit: 文本编辑控件
            cursor_top_global: 光标顶部在全局坐标系中的位置
            prefer_below: 是否优先显示在下方
        """
        screen = QApplication.screenAt(cursor_top_global) or QApplication.primaryScreen()
        if screen:
            screen_geo = screen.geometry()
        else:
            screen_geo = QRect(0, 0, 1920, 1080)
        
        popup_width = self.width()
        popup_height = self.height()
        
        cursor_x = cursor_top_global.x()
        cursor_y = cursor_top_global.y()
        
        # 检查右边界
        if cursor_x + popup_width > screen_geo.right():
            cursor_x = screen_geo.right() - popup_width - 10
        
        x = cursor_x
        self._show_below = True
        
        # 判断应该在下方还是上方
        space_below = screen_geo.bottom() - cursor_y
        space_above = cursor_y - screen_geo.top()
        
        # 优先下方，但下方空间不够时用上方
        if prefer_below and space_below >= popup_height:
            # 下方足够，弹窗顶部对齐光标顶部
            y = cursor_y + 2
            self._show_below = True
        elif space_above >= popup_height:
            # 上方足够，弹窗底部对齐光标顶部
            y = cursor_y - popup_height - 2
            self._show_below = False
        elif space_below >= popup_height:
            # 回退到下方
            y = cursor_y + 2
            self._show_below = True
        elif space_above >= popup_height:
            # 回退到上方
            y = cursor_y - popup_height - 2
            self._show_below = False
        else:
            # 空间都不够，选择较大的
            if space_above > space_below:
                y = cursor_y - popup_height - 2
                self._show_below = False
            else:
                y = cursor_y + 2
                self._show_below = True
        
        # 确保不超出边界
        if x < screen_geo.left():
            x = screen_geo.left() + 10
        if y < screen_geo.top():
            y = screen_geo.top() + 10
            
        self.move(x, y)
        self.show()
        if text_edit is not None:
            text_edit.setFocus(Qt.OtherFocusReason)


class SendableTextEdit(TextEdit):
    sendMessageRequested = pyqtSignal()
    stopMessageRequested = pyqtSignal()
    clearRequested = pyqtSignal()
    newSessionRequested = pyqtSignal()
    historyUpRequested = pyqtSignal()
    historyDownRequested = pyqtSignal()
    agentChanged = pyqtSignal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._initializing = True  # 初始化标志，防止早期高度调整
        self._glow_effect = None

        self._setup_glow_effect()
        self._apply_input_style()
        self.setPlaceholderText("给 DriFox 发送消息，Enter 发送，Shift+Enter 换行")
        self.setAcceptRichText(False)
        self.setLineWrapMode(TextEdit.WidgetWidth)
        self.setAcceptDrops(True)
        self.setMinimumHeight(72)
        self.setMaximumHeight(200)
        self.setFixedHeight(72)  # 初始化时设为最小高度

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
        self.textChanged.connect(self._on_at_trigger_check)

        # 关闭 qfluentwidgets TextEdit 焦点时的底部高亮
        if hasattr(self, 'layer'):
            self.layer.hide()

        self._setup_keyboard_shortcuts()

        # 技能补全弹窗
        self._completer_popup = SkillCompleterPopup(self)
        self._completer_popup.hide()
        self._completer_popup.skillSelected.connect(self._on_skill_selected)
        self._at_trigger_pos = -1  # @ 触发位置

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

    def _on_at_trigger_check(self):
        """检测 @ 触发——统一逻辑：始终检查光标前是否有 @，不再依赖弹窗可见性"""
        try:
            cursor = self.textCursor()
            text = self.toPlainText()
            cursor_pos = cursor.position()

            if cursor_pos < 0 or cursor_pos > len(text):
                return

            text_before_cursor = text[:cursor_pos]
            last_at = text_before_cursor.rfind("@")

            if last_at >= 0:
                query = text_before_cursor[last_at + 1:]
                # 如果有空格或换行，说明 @ 触发已结束
                if " " in query or "\n" in query:
                    if self._completer_popup.isVisible():
                        self._completer_popup.hide()
                    self._at_trigger_pos = -1
                    return

                # 还在 @ 触发中
                self._at_trigger_pos = last_at
                self._completer_popup.load_skills(get_local_skills(), query)

                # load_skills 只会在无匹配时隐藏弹窗，但不会主动显示（首次触发时弹窗处于隐藏状态）
                # 因此需要根据是否有匹配项来决定是否显示
                if self._completer_popup._list_widget.count() > 0:
                    self._show_completer_popup()
            else:
                # 没有 @ 符号
                if self._completer_popup.isVisible():
                    self._completer_popup.hide()
                self._at_trigger_pos = -1
        except Exception:
            pass

    def _show_completer_popup(self):
        """显示补全弹窗，位置紧贴光标"""
        rect = self.cursorRect()
        # 获取光标顶部在全局坐标系中的位置
        viewport_pos = self.viewport().mapToGlobal(QPoint(0, 0))
        cursor_x = viewport_pos.x() + rect.left()
        cursor_y = viewport_pos.y() + rect.top()
        
        global_pos = QPoint(cursor_x, cursor_y)
        self._completer_popup.show_at_cursor(self, global_pos)

    def _on_skill_selected(self, skill_name: str):
        """技能被选中"""
        cursor = self.textCursor()
        text = self.toPlainText()
        cursor_pos = cursor.position()

        # 用局部变量保存，防止后续 insertText() 触发 textChanged → _on_at_trigger_check
        # 递归修改 self._at_trigger_pos 导致定位错乱
        trigger_pos = self._at_trigger_pos

        if trigger_pos >= 0:
            # 删除 @ 符号和后面的内容
            cursor.setPosition(trigger_pos)
            cursor.setPosition(cursor_pos, QTextCursor.KeepAnchor)

            # 插入技能名，@符号也保留但给 @ 高亮
            insert_text = f"@{skill_name} "
            cursor.insertText(insert_text)

            # 高亮显示 @ 部分（用特殊颜色）
            cursor.setPosition(trigger_pos)
            cursor.setPosition(trigger_pos + 1 + len(skill_name), QTextCursor.KeepAnchor)

            # 创建高亮格式
            highlight_format = cursor.charFormat()
            highlight_format.setForeground(QColor("#C9A85C"))
            highlight_format.setFontWeight(700)
            cursor.setCharFormat(highlight_format)

            # 恢复光标到插入文本之后
            cursor.setPosition(trigger_pos + len(insert_text))
            self.setTextCursor(cursor)

        self._completer_popup.hide()
        self._at_trigger_pos = -1
        self.setFocus(Qt.OtherFocusReason)

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
        # 初始化期间不调整高度
        if getattr(self, '_initializing', False):
            return
        
        doc = self.document()
        # 计算文档高度 + padding
        content_height = int(doc.size().height()) + 28  # 上下 padding
        # 限制在最小和最大高度之间
        new_height = max(72, min(200, content_height))

        if self.height() != new_height:
            self.setFixedHeight(new_height)
            # 触发父布局重新计算
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
        # 定位发送按钮（智能体下拉已移到外部）
        self._position_send_button()

    def _position_send_button(self):
        """只定位发送按钮"""
        if self.send_btn:
            btn_size = self.send_btn.size()
            send_btn_x = self.width() - btn_size.width() - 12
            send_btn_y = self.height() - btn_size.height() - 10
            self.send_btn.move(max(0, send_btn_x), max(0, send_btn_y))

    def keyPressEvent(self, event: QKeyEvent):
        # 先检查补全弹窗是否可见
        if self._completer_popup.isVisible():
            if self._completer_popup.key_event(event):
                return

        if event.key() in (Qt.Key_Return, Qt.Key_Enter):
            if event.modifiers() & Qt.ShiftModifier:
                super().keyPressEvent(event)  # 换行
            else:
                self._on_send_click()
                event.accept()
        elif event.key() == Qt.Key_Up:
            if event.modifiers() & Qt.ControlModifier:
                self.historyUpRequested.emit()
                event.accept()
            else:
                super().keyPressEvent(event)
        elif event.key() == Qt.Key_Down:
            if event.modifiers() & Qt.ControlModifier:
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
        """设置发光效果"""
        try:
            self._glow_effect = QGraphicsDropShadowEffect(self)
            self._glow_effect.setBlurRadius(0)
            self._glow_effect.setColor(QColor(201, 168, 92, 0))
            self._glow_effect.setOffset(0, 0)
            self.setGraphicsEffect(self._glow_effect)
        except Exception:
            self._glow_effect = None

    def _apply_input_style(self):
        """应用输入框样式（动态从 Colors 读取）"""
        Colors.refresh()
        self.setStyleSheet(f"""
            QTextEdit {{
                background: qlineargradient(x1:0, y1:0, x2:1, y2:1,
                    stop:0 {Colors.INPUT_BG_START},
                    stop:1 {Colors.INPUT_BG_END});
                color: {Colors.INPUT_TEXT};
                border: 1px solid {Colors.INPUT_BORDER};
                border-radius: 18px;
                padding: 14px 50px 18px 16px;
                selection-background-color: rgba(201, 168, 92, 0.28);
                {get_font_family_css()} {font_size_css(14)};
            }}
            QTextEdit:focus {{
                border: 2px solid {Colors.INPUT_FOCUS_BORDER};
                border-radius: 18px;
                background: qlineargradient(x1:0, y1:0, x2:1, y2:1,
                    stop:0 {Colors.INPUT_FOCUS_BG_START},
                    stop:1 {Colors.INPUT_FOCUS_BG_END});
                color: {Colors.INPUT_FOCUS_TEXT};
                {font_size_css(14)};
            }}
            QTextEdit QScrollBar:vertical {{
                background: rgba(255, 255, 255, 0.05);
                width: 6px;
                margin: 2px 0 2px 0;
                border-radius: 3px;
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
        """动画发光效果"""
        if not self._glow_effect:
            return
        
        try:
            # 直接设置最终状态，避免复杂动画可能导致的问题
            self._glow_effect.setBlurRadius(target_blur)
            color = QColor(201, 168, 92, target_alpha)
            self._glow_effect.setColor(color)
        except Exception:
            # 发光效果失败时安全忽略
            pass

    def focusInEvent(self, event):
        try:
            super().focusInEvent(event)
            # 激活发光效果
            self._animate_glow(25, 180, 250)
            QTimer.singleShot(0, self._ensure_cursor_visible)
        except Exception:
            # 确保即使出错也不会崩溃
            pass
        
    def focusOutEvent(self, event):
        try:
            super().focusOutEvent(event)
            # 取消发光效果
            self._animate_glow(0, 0, 200)
        except Exception:
            # 确保即使出错也不会崩溃
            pass

    def _ensure_cursor_visible(self):
        cursor = self.textCursor()
        if cursor.position() > 0:
            self.ensureCursorVisible()

    def mousePressEvent(self, event):
        # 点击时隐藏补全弹窗
        self._completer_popup.hide()
        super().mousePressEvent(event)

    def wheelEvent(self, event):
        # 滚轮时隐藏补全弹窗
        self._completer_popup.hide()
        super().wheelEvent(event)
