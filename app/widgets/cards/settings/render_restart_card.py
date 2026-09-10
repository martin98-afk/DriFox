# -*- coding: utf-8 -*-
"""渲染与性能页首项：手动重启卡。

Chromium / QtWebEngine 只在进程启动时读一次环境变量 → 本页（Render 组）所有
配置都只能重启生效。本卡把这件事摆在最上面：

- 右侧「立即重启」按钮：直接拉起新进程（app.utils.app_restart）
- 左侧文案实时反映「有没有改动还没生效」：挂载时记下各配置基线，任一配置
  valueChanged 后与基线比对，有差异即进入待生效态（计数 + accent 配色）
"""

from __future__ import annotations

from PyQt5.QtCore import Qt
from qfluentwidgets import ConfigItem, FluentIcon, PrimaryPushButton, SettingCard

from app.utils.app_restart import restart_application
from app.utils.design_tokens import ButtonStyles, Colors, font_size_css
from app.utils.utils import get_font_family_css


class RenderRestartCard(SettingCard):
    """手动重启 + 待生效变更计数（渲染配置页首项）"""

    _IDLE_TEXT = "本页所有变更需重启应用后生效"

    def __init__(self, render_items, parent=None):
        """
        Args:
            render_items: Render 组 ConfigItem 列表（变更计数与基线比对的监控对象）
            parent: 父控件
        """
        super().__init__(FluentIcon.ROTATE, "手动重启", self._IDLE_TEXT, parent)
        self._items: list[ConfigItem] = list(render_items)
        self._baseline = [item.value for item in self._items]

        self.restartBtn = PrimaryPushButton("立即重启", self)
        self.restartBtn.setFixedWidth(100)
        self.restartBtn.setStyleSheet(ButtonStyles.primary_action())
        # 同 ManualUpdateCard：不参与焦点链，防禁用时焦点转移导致滚动跳转
        self.restartBtn.setFocusPolicy(Qt.NoFocus)
        self.restartBtn.clicked.connect(self._on_restart)
        self.hBoxLayout.addWidget(self.restartBtn, 0, Qt.AlignRight)

        for item in self._items:
            item.valueChanged.connect(self._refresh_pending)
        self._refresh_pending()

    # ------------------------------------------------------------------
    # 待生效状态
    # ------------------------------------------------------------------
    @property
    def pending_count(self) -> int:
        """与基线不一致的配置项数量（= 尚未生效的变更数）"""
        return sum(1 for item, base in zip(self._items, self._baseline) if item.value != base)

    def reset_baseline(self) -> None:
        """以当前值重设基线（配置被外部重载 / 已确认生效时调用）"""
        self._baseline = [item.value for item in self._items]
        self._refresh_pending()

    def _refresh_pending(self, *_args) -> None:
        """任一 Render 配置变更 → 刷新文案与配色"""
        pending = self.pending_count
        self.setContent(f"有 {pending} 项变更待重启生效" if pending else self._IDLE_TEXT)
        self._apply_content_style(bool(pending))

    def _apply_content_style(self, pending: bool) -> None:
        """待生效用 accent 高亮，否则用弱化灰（Colors 令牌随主题刷新）。

        字号必须显式写出：给 contentLabel 设 stylesheet 会盖掉
        apply_font_size_to_widget 为它写的 `QLabel#contentLabel {...}` 规则，
        漏写 font-size 就退回应用默认字号（在大字号档下明显偏小）。
        11 与 design_tokens.apply_font_size_to_widget 的 content 档保持一致。
        """
        color = Colors.TEXT_ACCENT if pending else Colors.TEXT_MUTED
        self.contentLabel.setStyleSheet(
            f"QLabel#contentLabel {{ color: {color}; background: transparent; "
            f"{get_font_family_css()} {font_size_css(11)} }}"
        )

    def refresh_style(self) -> None:
        """主题/字体刷新后重刷文案与按钮（由 LLMSettingsCard 刷新链调用）"""
        self._apply_content_style(bool(self.pending_count))
        # ButtonStyles 内嵌 font_size_css / 字体族，字号变更后需重建才跟得上
        self.restartBtn.setStyleSheet(ButtonStyles.primary_action())

    # ------------------------------------------------------------------
    # 重启
    # ------------------------------------------------------------------
    def _on_restart(self) -> None:
        self.restartBtn.setEnabled(False)
        self.restartBtn.setText("重启中...")
        if not restart_application():
            self.restartBtn.setEnabled(True)
            self.restartBtn.setText("立即重启")
