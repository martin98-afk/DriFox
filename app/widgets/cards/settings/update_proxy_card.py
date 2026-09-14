# -*- coding: utf-8 -*-
"""更新下载代理设置卡（四选一模式 + 按模式显隐输入框 + 连通性测试）

挂载位置：设置 → 更新页，手动检查更新卡片下方。

模式语义：
- 直连    ：不使用代理
- 跟随系统：读系统代理设置（环境变量 + Windows 注册表）
- 加速前缀：经第三方镜像下载（仅作用于安装包下载，检查更新仍直连 API）
- 手动代理：指定 HTTP 代理地址（不支持 SOCKS5）

沿用项目既有折叠卡套路：`DynamicHeightExpandCardMixin` + `ExpandSettingCard`
（同 `secret_mode_card` / `render_advanced_card`）——基类那套滚动动画在内容
动态挂载时会失配，高度由 mixin 接管。

坑位备忘：
- 给 label 设 stylesheet 会盖掉 `apply_font_size_to_widget` 写的字号规则 →
  本文件所有 label 样式**必须显式带 font_size_css**
- 折叠卡的内容控件父对象要传 `self.view`，否则高度接管失效
- 测试必须后台线程发起（实测网络受限时 ConnectTimeout 要 21s 才落地，
  同步跑会冻住 UI），结果经信号回主线程
"""

from __future__ import annotations

import threading

from loguru import logger
from PyQt5.QtCore import Qt, pyqtSignal
from PyQt5.QtWidgets import QFrame, QHBoxLayout, QLabel, QVBoxLayout, QWidget
from qfluentwidgets import (
    ExpandSettingCard,
    FluentIcon,
    InfoBar,
    InfoBarPosition,
    LineEdit,
    PushButton,
)

from app.utils import update_proxy
from app.utils.config import Settings
from app.utils.design_tokens import Colors, font_size_css, scale_font_size
from app.utils.utils import get_font_family_css
from app.widgets.cards.settings.expand_height_mixin import DynamicHeightExpandCardMixin
from app.widgets.elided_label import _ElidedLabel

# 模式清单：(模式常量, 标题, 说明文案)
# 「跟随系统」的说明在 _refresh 里动态生成（含实时探测结果）
MODES = (
    (update_proxy.MODE_DIRECT, "直连", "不使用代理，直接访问 GitHub"),
    (update_proxy.MODE_SYSTEM, "跟随系统", "读取系统代理设置"),
    (update_proxy.MODE_PREFIX, "加速前缀", "经第三方镜像加速下载，适用于国内网络"),
    (update_proxy.MODE_HTTP, "手动代理", "指定 HTTP 代理地址（不支持 SOCKS5）"),
)

_MODE_LABEL = {m: t for m, t, _ in MODES}
# 需要地址输入框的模式
_ADDR_MODES = (update_proxy.MODE_PREFIX, update_proxy.MODE_HTTP)


def _hint(text: str, parent) -> _ElidedLabel:
    """说明文字（单行省略）

    ⚠️ 用 `_ElidedLabel` 而非 `BodyLabel(wordWrap=True)`：后者是自动换行的
    QLabel，其 C++ sizeHint 按**理想宽度**算换行高度，实测虚高 12px 以上；
    多行累加后卡片底部多出一大块空白。而 PyQt5 里布局对 QWidgetItem 的
    sizeHint 调用不派发 Python override，覆写 sizeHint() 无效。
    项目内 `render_advanced_card` 已经在用本方案（同源坑）。
    """
    label = _ElidedLabel(text, parent)
    label.setMinimumWidth(40)
    # _ElidedLabel 构造时会把全文设成 tooltip；本行已有整体 tooltip，清掉避免重复
    label.setToolTip("")
    label.setStyleSheet(
        f"color: {Colors.TEXT_MUTED}; background: transparent; {get_font_family_css()} {font_size_css(11)}"
    )
    return label


class _ModeRow(QFrame):
    """模式可选行：整行点击选中，选中态高亮边框；带输入框的模式附输入区"""

    selected = pyqtSignal()

    def __init__(self, mode: str, title: str, desc: str, with_input: bool, parent=None):
        super().__init__(parent)
        self.mode = mode
        self._selected = False
        self.setCursor(Qt.PointingHandCursor)

        v = QVBoxLayout(self)
        self._vbox = v
        v.setContentsMargins(12, 8, 12, 8)
        v.setSpacing(2)

        head = QHBoxLayout()
        head.setContentsMargins(0, 0, 0, 0)
        head.setSpacing(8)
        self._title = QLabel(title, self)
        head.addWidget(self._title)
        head.addStretch(1)
        v.addLayout(head)

        self._desc = _hint(desc, self)
        v.addWidget(self._desc)

        # 输入区（仅 prefix / http 模式创建）
        self._input_row: QWidget | None = None
        self.edit: LineEdit | None = None
        self.test_btn: PushButton | None = None
        if with_input:
            self._input_row = QWidget(self)
            h = QHBoxLayout(self._input_row)
            h.setContentsMargins(0, 4, 0, 0)
            h.setSpacing(8)
            self.edit = LineEdit(self._input_row)
            self.edit.setClearButtonEnabled(True)
            self.test_btn = PushButton("测试", self._input_row)
            self.test_btn.setFixedWidth(64)
            h.addWidget(self.edit, 1)
            h.addWidget(self.test_btn)
            # 初始未选中：不挂进布局，并且必须显式隐藏 —— 否则父控件 show 时
            # 它会以“无布局子控件”的身份在 (0,0) 浮出来，压在自己的标题上
            self._input_row.setVisible(False)

        self.refresh_style()
        self._apply_style()

    def set_desc(self, text: str) -> None:
        self._desc.setText(text)

    def set_input_visible(self, visible: bool) -> None:
        """显隐输入区

        ⚠️ 用「从布局里摘掉 / 挂回」而非只 setVisible——后者只隐藏控件，
        但行的 sizeHint 仍含输入区高度（QVBoxLayout 对嵌套子布局的隐藏
        子项算不准，PyQt5 又不派发 Python 的 sizeHint override），
        表现为卡片底部多出一条输入框高的大片空白。

        ⚠️ 两条路都必须调 setVisible：只 removeWidget 的话控件仍留在父控件
        可见性体系里，父控件 show 时会以「无布局子控件」身份在 (0,0)
        浮出来，直接压在自己的标题上。
        """
        if self._input_row is None:
            return
        in_layout = self._vbox.indexOf(self._input_row) != -1
        if visible and not in_layout:
            self._vbox.addWidget(self._input_row)
            self._input_row.setVisible(True)
        elif not visible:
            if in_layout:
                self._vbox.removeWidget(self._input_row)
            self._input_row.setVisible(False)
        self.updateGeometry()

    def set_selected(self, selected: bool):
        if self._selected == selected:
            return
        self._selected = selected
        self._apply_style()

    def _apply_style(self):
        border = Colors.BORDER_ACCENT if self._selected else Colors.BORDER
        bg = Colors.HOVER_BG if self._selected else "transparent"
        self.setStyleSheet(f"""
            QFrame {{
                background-color: {bg};
                border: 1px solid {border};
                border-radius: 6px;
            }}
            QFrame:hover {{
                border-color: {Colors.BORDER_ACCENT};
            }}
            QLabel {{
                border: none;
                background: transparent;
            }}
        """)

    def mouseReleaseEvent(self, e):
        if e.button() == Qt.LeftButton:
            # 点输入区不该触发选中。输入框/按钮的 mouseRelease 会冒泡到这里，
            # 所以按坐标排除 —— 输入区已挂载时是本行的子控件，e.pos() 与
            # geometry() 同以本行为原点，可直接比较。
            ir = self._input_row
            if ir is not None and ir.isVisible() and ir.geometry().contains(e.pos()):
                super().mouseReleaseEvent(e)
                return
            self.selected.emit()
        super().mouseReleaseEvent(e)

    def refresh_style(self) -> None:
        """主题/字号刷新：label 样式必须显式带 font_size_css（见模块文档）"""
        self._title.setStyleSheet(
            f"color: {Colors.TEXT_PRIMARY}; background: transparent;"
            f"{get_font_family_css()} font-size: {scale_font_size(13)}px; font-weight: 600;"
        )
        self._desc.setStyleSheet(
            f"color: {Colors.TEXT_MUTED}; background: transparent;"
            f"{get_font_family_css()} font-size: {scale_font_size(11)}px;"
        )


class UpdateProxyCard(DynamicHeightExpandCardMixin, ExpandSettingCard):
    """更新下载代理：折叠一行摘要，展开后四选一 + 按需输入框"""

    testFinished = pyqtSignal(bool, str)

    def __init__(self, parent=None):
        super().__init__(
            FluentIcon.GLOBE,
            "更新下载代理",
            "决定安装包下载走哪条网络路径",
            parent,
        )
        self.cfg = Settings.get_instance()
        self._rows: dict[str, _ModeRow] = {}
        self._syncing = False
        self._testing = False
        self._build_content()
        self._connect_signals()
        self.testFinished.connect(self._on_test_finished)
        self._refresh()
        self._adjust_view_size()

    # ── 构建 ──

    def _build_content(self):
        self.viewLayout.setSpacing(6)
        self.viewLayout.setAlignment(Qt.AlignTop)
        self.viewLayout.setContentsMargins(8, 4, 8, 4)

        for mode, title, desc in MODES:
            with_input = mode in _ADDR_MODES
            row = _ModeRow(mode, title, desc, with_input, parent=self.view)
            row.selected.connect(lambda m=mode: self._on_row_selected(m))
            if with_input and row.edit is not None and row.test_btn is not None:
                row.edit.editingFinished.connect(lambda m=mode: self._on_addr_committed(m))
                row.test_btn.clicked.connect(lambda _checked=False, m=mode: self._on_test(m))
            self.viewLayout.addWidget(row)
            self._rows[mode] = row

        # 输入框预填与占位符
        prefix_row = self._rows[update_proxy.MODE_PREFIX]
        if prefix_row.edit is not None:
            prefix_row.edit.setText(str(self.cfg.update_proxy_prefix.value or ""))
            prefix_row.edit.setPlaceholderText("https://ghfast.top/")
        http_row = self._rows[update_proxy.MODE_HTTP]
        if http_row.edit is not None:
            http_row.edit.setText(str(self.cfg.update_proxy_url.value or ""))
            http_row.edit.setPlaceholderText("http://127.0.0.1:7890")

    def _connect_signals(self):
        self.cfg.update_proxy_mode.valueChanged.connect(self._on_mode_changed_external)
        self.cfg.update_proxy_prefix.valueChanged.connect(self._on_prefix_changed_external)
        self.cfg.update_proxy_url.valueChanged.connect(self._on_url_changed_external)

    # ── 状态刷新 ──

    def _current_mode(self) -> str:
        value = str(self.cfg.update_proxy_mode.value or update_proxy.MODE_DIRECT)
        return value if value in _MODE_LABEL else update_proxy.MODE_DIRECT

    def _refresh(self) -> None:
        mode = self._current_mode()
        self._syncing = True
        try:
            for m, row in self._rows.items():
                row.set_selected(m == mode)
            # 「跟随系统」说明带实时探测结果：系统可能填了地址但开关关着
            self._rows[update_proxy.MODE_SYSTEM].set_desc(update_proxy.probe_system_proxy())
            # 输入框只在选中对应模式时显示
            for m in _ADDR_MODES:
                self._rows[m].set_input_visible(mode == m)
        finally:
            self._syncing = False
        self.card.setContent(f"当前：{_MODE_LABEL[mode]}")
        # 输入框显隐会改变内容高度：展开态下必须让 mixin 重算，否则底部被裁
        if getattr(self, "isExpand", False):
            self._adjust_view_size()

    # ── 交互 ──

    def _on_row_selected(self, mode: str) -> None:
        if self._syncing or mode == self._current_mode():
            return
        self.cfg.update_proxy_mode.value = mode
        self._refresh()

    def _on_addr_committed(self, mode: str) -> None:
        """输入框失焦/回车：格式校验 → 通过则落盘（立即生效）"""
        if self._syncing:
            return
        row = self._rows[mode]
        if row.edit is None:
            return
        addr = row.edit.text().strip()
        item = self.cfg.update_proxy_prefix if mode == update_proxy.MODE_PREFIX else self.cfg.update_proxy_url
        if not addr:
            # 空地址允许：运行时退化为直连（校验层只在用户填了东西时才拦）
            item.value = ""
            return
        ok, msg = update_proxy.validate(mode, addr)
        if not ok:
            self._toast_error("地址无效", msg)
            return
        item.value = addr

    def _on_test(self, mode: str) -> None:
        """连通性测试：后台线程发起真实请求，严格校验响应形态"""
        if self._testing:
            return
        row = self._rows[mode]
        if row.edit is None or row.test_btn is None:
            return
        addr = row.edit.text().strip()
        ok, msg = update_proxy.validate(mode, addr)
        if not ok:
            self._toast_error("无法测试", msg)
            return

        self._testing = True
        row.test_btn.setText("测试中")
        row.test_btn.setEnabled(False)

        def _worker():
            try:
                passed, message = update_proxy.test_connection(mode, addr)
            except Exception as e:  # 探测失败不该打死线程
                logger.warning(f"[UpdateProxy] 连通性测试异常: {e}")
                passed, message = False, f"失败: {e}"
            # 跨线程回主线程（工作线程不能直接碰 widget）
            self.testFinished.emit(passed, f"{_MODE_LABEL[mode]}：{message}")

        threading.Thread(target=_worker, daemon=True, name="update-proxy-test").start()

    def _on_test_finished(self, passed: bool, message: str) -> None:
        self._testing = False
        for row in self._rows.values():
            if row.test_btn is not None:
                row.test_btn.setText("测试")
                row.test_btn.setEnabled(True)
        if passed:
            InfoBar.success(
                title="连通性正常",
                content=message,
                position=InfoBarPosition.BOTTOM,
                duration=3000,
                parent=self.window() or self,
            ).show()
        else:
            self._toast_error("连通性测试失败", message)

    def _toast_error(self, title: str, content: str) -> None:
        InfoBar.error(
            title=title,
            content=content,
            position=InfoBarPosition.BOTTOM,
            duration=4000,
            parent=self.window() or self,
        ).show()

    # ── 外部变更同步 ──

    def _on_mode_changed_external(self, _value=None) -> None:
        self._refresh()

    def _on_prefix_changed_external(self, value=None) -> None:
        self._sync_edit(update_proxy.MODE_PREFIX, value)

    def _on_url_changed_external(self, value=None) -> None:
        self._sync_edit(update_proxy.MODE_HTTP, value)

    def _sync_edit(self, mode: str, value) -> None:
        """配置 → 输入框（外部改写如换机同步；避免回环）"""
        if self._syncing:
            return
        row = self._rows.get(mode)
        if row is None or row.edit is None:
            return
        self._syncing = True
        try:
            row.edit.setText(str(value or ""))
        finally:
            self._syncing = False

    # ── 主题刷新 ──

    def refresh_style(self) -> None:
        """主题/字号变更：重绘各行 label 与 header 摘要"""
        for row in self._rows.values():
            row.refresh_style()
        self._refresh()
