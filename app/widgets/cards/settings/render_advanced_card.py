# -*- coding: utf-8 -*-
"""渲染页「高级配置」折叠卡 —— 一条一项，右侧 SwitchButton 开关。

不再分组（不区分 Features / Chromium 开关），也不再用 chip：每一项一行，
左边是「开启后会发生什么」的动作文案，右边一个开关，语义唯一：

    禁用网页翻译                                    [ ○——]
    关闭垂直同步                                    [ ——○]

底层仍写回两个配置项：
- `DisabledFeatures`（feature 类，逗号分隔）
- `ExtraChromiumFlags`（flag 类，空白分隔）
预设之外的旧值提交时原样保留，不会静默丢配置。

沿用项目既有折叠卡套路：`DynamicHeightExpandCardMixin` + `ExpandSettingCard`
（同 `list_setting_card.SkillListSettingCard`）——基类那套滚动动画在内容动态挂载
时会失配，高度由 mixin 接管。

坑位备忘（与 render_restart_card / render_status_card 同源）：
- 给 label 设 stylesheet 会盖掉 `apply_font_size_to_widget` 写的字号规则 →
  本文件所有 label 样式**必须显式带 font_size_css**
- 折叠卡的内容控件父对象要传 `self.view`，否则高度接管失效
"""

from __future__ import annotations

import re

from PyQt5.QtCore import Qt, pyqtSignal
from PyQt5.QtWidgets import QHBoxLayout, QLabel, QVBoxLayout, QWidget
from qfluentwidgets import ConfigItem, ExpandSettingCard, FluentIcon, SwitchButton

from app.utils.config import Settings
from app.utils.design_tokens import Colors, SwitchStyles, font_size_css, scale_font_size
from app.utils.utils import get_font_family_css
from app.widgets.cards.settings.expand_height_mixin import DynamicHeightExpandCardMixin
from app.widgets.elided_label import _ElidedLabel

# ── 高级项清单：(归属, 写入值, 名称, 描述, mode)
# 归属："feature" → 写进 DisabledFeatures；"flag" → 写进 ExtraChromiumFlags
# mode 决定「开关打开」对应什么：
#   "disable" —— 该能力默认开，开关**关掉**才写入值（--disable-x / feature 名）
#   "allow"   —— 该能力默认关，开关**打开**才写入值（--allow-x 之类）
# 开关一律读作「这个能力开不开」，名称用能力本身，描述说明开/关的后果 ──
ADVANCED_ITEMS = (
    ("flag", "--disable-lcd-text", "LCD 文字渲染", "关闭可解决 Windows 文字发虚/彩边", "disable"),
    ("flag", "--disable-gpu-vsync", "垂直同步", "关闭后流式重绘更跟手（可能画面撕裂）", "disable"),
    ("flag", "--disable-partial-raster", "部分光栅", "关闭后大面积滚动更稳，首屏略慢", "disable"),
    ("flag", "--mute-audio", "声音", "关闭后卡片内音视频不发声", "disable"),
    ("flag", "--allow-file-access-from-files", "本地文件访问", "开启后插件页的 file:// 资源才能加载", "allow"),
    ("flag", "--autoplay-policy=no-user-gesture-required", "自动播放", "开启后音视频无需点击即可播放", "allow"),
    ("flag", "--force-color-profile=srgb", "sRGB 色彩统一", "开启后校正远程图片偏色", "allow"),
    ("feature", "Translate", "网页翻译", "关闭可去掉 Chromium 自带的翻译条", "disable"),
    ("feature", "MediaRouter", "投屏", "关闭可去掉媒体路由发现", "disable"),
    ("feature", "optimizeHints", "加载优化提示", "关闭可省一点内存", "disable"),
    ("feature", "CalculateNativeWinOcclusion", "窗口遮挡计算", "关闭可省 CPU", "disable"),
    ("feature", "BackForwardCache", "前进后退缓存", "关闭省内存，回退稍慢", "disable"),
    ("feature", "AudioServiceOutOfProcess", "音频独立进程", "关闭后音频并入主进程，少一个进程", "disable"),
)

# 名称列宽 / 描述最小宽：与「工具配置」ItemRow 保持一致
_NAME_WIDTH = 150

_KIND_SEP = {"feature": ",", "flags": " "}


def _split(value, kind: str) -> list[str]:
    """按归属切分配置值并去重保序（忽略空串）"""
    text = "" if value is None else str(value)
    parts = re.split(r"[,\n]", text) if kind == "feature" else text.split()
    seen: set[str] = set()
    out: list[str] = []
    for p in parts:
        p = p.strip()
        if p and p not in seen:
            seen.add(p)
            out.append(p)
    return out


class _SwitchRow(QWidget):
    """一行高级项：名称 | 描述 | 开关（列布局对齐「工具配置」ItemRow）"""

    toggled = pyqtSignal(bool)

    def __init__(self, name: str, desc: str, token: str = "", checked: bool = False, parent=None):
        super().__init__(parent)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(12, 2, 12, 2)
        layout.setSpacing(8)

        self.nameLabel = QLabel(name, self)
        self.nameLabel.setFixedWidth(_NAME_WIDTH)

        # 描述超长自动省略（与技能卡 / 工具配置细项同款 muted 副标题）
        self.descLabel = _ElidedLabel(desc, self)
        self.descLabel.setMinimumWidth(40)
        # _ElidedLabel 构造时会把全文设成自己的 tooltip —— 这里清空，
        # 让整行只保留一个 tooltip（Qt 的子控件无 tooltip 时会向上冒泡到父控件）
        self.descLabel.setToolTip("")

        self.switch = SwitchButton(self)
        SwitchStyles.configure(self.switch)  # 无文字标签 + 固定宽度（项目统一约定）
        self.switch.setChecked(checked)
        self.switch.checkedChanged.connect(self.toggled)

        layout.addWidget(self.nameLabel)
        layout.addWidget(self.descLabel, 1)
        layout.addWidget(self.switch)
        self.setToolTip(f"{desc}（写入值：{token}）" if token else desc)
        self.refresh_style()

    def set_checked(self, on: bool) -> None:
        """外部同步（不触发 toggled 回写）"""
        if self.switch.isChecked() != on:
            self.switch.blockSignals(True)
            self.switch.setChecked(on)
            self.switch.blockSignals(False)

    def refresh_style(self) -> None:
        """主题/字号刷新：label 样式必须显式带 font_size_css（见模块文档）"""
        self.nameLabel.setStyleSheet(
            f"color: {Colors.TEXT_PRIMARY}; background: transparent; {get_font_family_css()} {font_size_css(11)}"
        )
        self.descLabel.setStyleSheet(
            f"color: {Colors.TEXT_MUTED}; background: transparent; "
            f"{get_font_family_css()} font-size: {scale_font_size(11)}px;"
        )


class RenderAdvancedCard(DynamicHeightExpandCardMixin, ExpandSettingCard):
    """渲染页高级配置：折叠一行摘要，展开后逐项开关"""

    def __init__(self, feature_item: ConfigItem, flag_item: ConfigItem, items=ADVANCED_ITEMS, parent=None):
        """
        Args:
            feature_item: DisabledFeatures 配置项
            flag_item: ExtraChromiumFlags 配置项
            items: 高级项清单，见 ADVANCED_ITEMS
            parent: 父控件
        """
        super().__init__(FluentIcon.DEVELOPER_TOOLS, "高级配置", "Chromium 高级开关", parent)
        self.cfg = Settings.get_instance()
        self._feature_item = feature_item
        self._flag_item = flag_item
        self._items = tuple(items)
        self._rows: list[tuple[str, str, str, _SwitchRow]] = []  # (归属, 写入值, mode, 行)
        self._syncing = False
        self.__initWidget()

    def __initWidget(self):  # noqa: N802 - 与项目既有折叠卡命名一致
        self.viewLayout.setSpacing(0)
        self.viewLayout.setAlignment(Qt.AlignTop)
        self.viewLayout.setContentsMargins(8, 4, 8, 4)

        for kind, token, name, desc, mode in self._items:
            row = _SwitchRow(name, desc, token, parent=self.view)
            # mode="disable" 的能力默认开 → 开关**关掉**才写入值；"allow" 反之
            row.toggled.connect(lambda on, k=kind, t=token, m=mode: self._on_row_toggled(k, t, m, on))
            self.viewLayout.addWidget(row)
            self._rows.append((kind, token, mode, row))

        self._feature_item.valueChanged.connect(self._sync_from_config)
        self._flag_item.valueChanged.connect(self._sync_from_config)
        self._sync_from_config()
        self._adjust_view_size()

    # ------------------------------------------------------------------
    # 同步
    # ------------------------------------------------------------------
    def _sync_from_config(self, *_args) -> None:
        """配置 → 开关状态（外部改写如「恢复默认」也走这里）"""
        self._syncing = True
        try:
            for kind, token, mode, row in self._rows:
                item = self._feature_item if kind == "feature" else self._flag_item
                present = token in _split(item.value, kind)
                # 开关语义 = 能力是否开启：disable 类「值在 = 关」，allow 类「值在 = 开」
                row.set_checked(present if mode == "allow" else not present)
        finally:
            self._syncing = False
        self._refresh_summary()

    def _on_row_toggled(self, kind: str, token: str, mode: str, on: bool) -> None:
        """开关 → 写回对应配置项（预设之外的旧值原样保留）"""
        if self._syncing:
            return
        item = self._feature_item if kind == "feature" else self._flag_item
        tokens = _split(item.value, kind)
        # 开关开 + allow / 开关关 + disable → 需要写入值；反之移除
        want_present = on if mode == "allow" else not on
        if want_present and token not in tokens:
            tokens.append(token)
        elif not want_present and token in tokens:
            tokens.remove(token)
        else:
            return
        sep = _KIND_SEP["feature"] if kind == "feature" else " "
        new_value = sep.join(tokens)
        if new_value != item.value:
            self.cfg.set(item, new_value, save=True)
        self._refresh_summary()

    # ------------------------------------------------------------------
    # 展示
    # ------------------------------------------------------------------
    def _adjusted_count(self) -> int:
        """与 Chromium 原生默认值不同的项数（= 配置里出现过的已知项）"""
        total = 0
        for kind, token, _mode, _row in self._rows:
            item = self._feature_item if kind == "feature" else self._flag_item
            if token in _split(item.value, kind):
                total += 1
        return total

    def _refresh_summary(self, *_args) -> None:
        """折叠态头部：相对默认改动了几项"""
        n = self._adjusted_count()
        self.card.setContent(f"已调整 {n} 项" if n else "全部保持默认")

    def refresh_style(self) -> None:
        """主题/字号刷新链（LLMSettingsCard 调用）"""
        for _kind, _token, _mode, row in self._rows:
            row.refresh_style()
        self._refresh_summary()
