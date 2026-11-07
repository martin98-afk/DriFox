# -*- coding: utf-8 -*-
"""外观页「主题音乐」卡

当前主题的 `backgrounds.music` 决定这张卡存不存在：

- 没有 music 配置 / enabled=False / 曲目文件全部无效 → 整张卡隐藏。
  以前是无条件显示一条调不出声的音量条，用户看不出「这主题到底有没有音乐」。
- 折叠态：标题 + 「主题名 · 正在播放 xxx」+ 音量滑块（随手可调，与旧行为一致）
- 展开态：当前主题歌单，逐曲开关，正在播放的那首高亮

单曲开关按主题记在 `plugin_data/<插件>/theme_music.json`（见
app.utils.theme_music_store），跟随插件数据走，不占主程序配置。

高度沿用 `DynamicHeightExpandCardMixin`：歌单是动态增删的，基类滚动动画在
内容高度变化时会失配。
"""

from __future__ import annotations

import os

from loguru import logger
from PyQt5.QtCore import Qt, pyqtSignal
from PyQt5.QtWidgets import QHBoxLayout, QLabel, QWidget
from qfluentwidgets import ExpandSettingCard, FluentIcon, Slider, SwitchButton

from app.utils import theme_music_store
from app.utils.config import Settings
from app.utils.design_tokens import Colors, SwitchStyles, font_size_css, scale_font_size
from app.utils.theme_manager import theme_manager
from app.utils.utils import get_font_family_css
from app.widgets.cards.settings.expand_height_mixin import DynamicHeightExpandCardMixin
from app.widgets.elided_label import _ElidedLabel

_SLIDER_WIDTH = 160
_INDEX_WIDTH = 22
_STATE_WIDTH = 46
_ROW_HEIGHT = 32


class _TrackRow(QWidget):
    """一行曲目：序号 | 曲名 | 播放标记 | 单曲开关"""

    toggled = pyqtSignal(bool)

    def __init__(self, index: int, path: str, name: str, enabled: bool, playing: bool, parent=None):
        super().__init__(parent)
        self.path = path
        self._enabled = bool(enabled)
        self._playing = bool(playing)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(12, 2, 12, 2)
        layout.setSpacing(8)

        self.indexLabel = QLabel(f"{index:02d}", self)
        self.indexLabel.setFixedWidth(scale_font_size(_INDEX_WIDTH))

        self.nameLabel = _ElidedLabel(name, self)
        self.nameLabel.setMinimumWidth(80)
        self.nameLabel.setToolTip(path)

        self.stateLabel = QLabel(self)
        self.stateLabel.setFixedWidth(scale_font_size(_STATE_WIDTH))
        self.stateLabel.setAlignment(Qt.AlignRight | Qt.AlignVCenter)

        self.switch = SwitchButton(self)
        SwitchStyles.configure(self.switch)
        self.switch.setChecked(self._enabled)
        self.switch.checkedChanged.connect(self._on_checked)

        layout.addWidget(self.indexLabel)
        layout.addWidget(self.nameLabel, 1)
        layout.addWidget(self.stateLabel)
        layout.addWidget(self.switch)
        self.setFixedHeight(scale_font_size(_ROW_HEIGHT))
        self.refresh_style()

    def _on_checked(self, on: bool) -> None:
        self._enabled = bool(on)
        if not on:
            self._playing = False
        self.refresh_style()
        self.toggled.emit(bool(on))

    def set_playing(self, playing: bool) -> None:
        if self._playing == playing:
            return
        self._playing = playing
        self.refresh_style()

    def refresh_style(self) -> None:
        """主题/字号刷新（label 样式串必须显式带字号，否则盖掉全局字号规则）"""
        self.indexLabel.setStyleSheet(
            f"color: {Colors.TEXT_MUTED}; background: transparent; "
            f"{get_font_family_css()} {font_size_css(11)}"
        )
        name_color = Colors.TEXT_PRIMARY if self._enabled else Colors.TEXT_MUTED
        self.nameLabel.setStyleSheet(
            f"color: {name_color}; background: transparent; "
            f"{get_font_family_css()} {font_size_css(12)}"
        )
        self.stateLabel.setText("播放中" if self._playing and self._enabled else "")
        self.stateLabel.setStyleSheet(
            f"color: {Colors.TEXT_ACCENT}; background: transparent; "
            f"{get_font_family_css()} {font_size_css(11)}"
        )


class ThemeMusicCard(DynamicHeightExpandCardMixin, ExpandSettingCard):
    """主题音乐卡：无音乐隐藏 / 展开歌单 / 逐曲开关 / 当前曲高亮"""

    # 进外观页时不自动展开（外观页已有主题网格等多张卡，歌单默认收起）
    _skip_auto_expand = True

    def __init__(self, volume_item, parent=None):
        super().__init__(FluentIcon.MUSIC, "主题音乐", "当前主题没有背景音乐", parent)
        self._volume_item = volume_item
        self.cfg = Settings.get_instance()
        self._theme_id = ""
        self._tracks: tuple = ()
        self._rows: list = []  # [(绝对路径, _TrackRow)]

        # ── header 右侧：音量滑块 + 数值 ──
        self._volume_label = QLabel(self)
        self._volume_label.setFixedWidth(scale_font_size(30))
        self._volume_label.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        self.slider = Slider(Qt.Horizontal, self)
        self.slider.setRange(0, 100)
        self.slider.setFixedWidth(_SLIDER_WIDTH)
        self.slider.valueChanged.connect(self._on_slider_changed)
        self.addWidget(self._volume_label)
        self.addWidget(self.slider)

        # ── 展开区：说明 + 曲目行 ──
        self.viewLayout.setContentsMargins(48, 6, 18, 6)
        self.viewLayout.setSpacing(0)
        self._hintLabel = QLabel("关闭的曲目不会被播放，选择按插件记在 plugin_data", self.view)
        self.viewLayout.addWidget(self._hintLabel)

        # ── 数据源 ──
        self._volume_item.valueChanged.connect(self._sync_volume)
        self.cfg.ui_theme_style.valueChanged.connect(self._on_theme_style_changed)
        player = self._player()
        if player is not None:
            player.trackChanged.connect(self._on_track_changed)
            player.tracksChanged.connect(self._on_tracks_changed)

        self._sync_volume(self._volume_item.value)
        self.sync_from_theme()

    # ── 对外 ─────────────────────────────────────────

    def sync_from_theme(self) -> None:
        """按当前主题重建歌单；主题没有可用音乐时整张卡隐藏"""
        theme_id = theme_manager.get_current_theme_id()
        self._theme_id = theme_id
        self._tracks = theme_music_store.theme_tracks(theme_id)

        if not self._tracks:
            self._clear_rows()
            if self.isExpand:
                self.setExpand(False)
            self.setVisible(False)
            return

        self.setVisible(True)
        self._rebuild_rows()
        self._refresh_content()
        self._adjust_view_size()

    def refresh_style(self) -> None:
        """主题/字号刷新链（LLMSettingsCard 调用）"""
        self._volume_label.setStyleSheet(
            f"color: {Colors.TEXT_SECONDARY}; background: transparent; "
            f"{get_font_family_css()} {font_size_css(11)}"
        )
        self._hintLabel.setStyleSheet(
            f"color: {Colors.TEXT_MUTED}; background: transparent; "
            f"{get_font_family_css()} {font_size_css(11)}"
        )
        for _path, row in self._rows:
            row.refresh_style()

    # ── 内部：行构建 ──────────────────────────────────

    def _clear_rows(self) -> None:
        for _path, row in self._rows:
            self.viewLayout.removeWidget(row)
            row.hide()
            row.deleteLater()
        self._rows.clear()

    def _rebuild_rows(self) -> None:
        self._clear_rows()
        disabled = theme_music_store.load_disabled(self._theme_id)
        playing = self._current_track()
        for index, (path, name) in enumerate(self._tracks, 1):
            row = _TrackRow(index, path, name, path not in disabled, path == playing, self.view)
            row.toggled.connect(lambda on, p=path: self._on_row_toggled(p, on))
            self.viewLayout.addWidget(row)
            self._rows.append((path, row))

    def _on_row_toggled(self, path: str, on: bool) -> None:
        disabled = set(theme_music_store.load_disabled(self._theme_id))
        if on:
            disabled.discard(path)
        else:
            disabled.add(path)
        theme_music_store.save_disabled(self._theme_id, disabled)
        player = self._player()
        if player is not None:
            player.set_disabled_tracks(disabled)
        self._refresh_content()

    # ── 内部：播放态 / 音量 ───────────────────────────

    def _on_theme_style_changed(self, *_args) -> None:
        self.sync_from_theme()

    def _on_track_changed(self, path: str) -> None:
        for track_path, row in self._rows:
            row.set_playing(track_path == path)
        self._refresh_content()

    def _on_tracks_changed(self) -> None:
        # 主题列表换了但配置项没变（如主题文件热重载）时才需要重取
        if theme_manager.get_current_theme_id() != self._theme_id:
            self.sync_from_theme()

    def _refresh_content(self) -> None:
        if not self._tracks:
            return
        name = theme_manager.list_themes().get(self._theme_id, self._theme_id)
        playing = self._current_track()
        if playing:
            self.card.setContent(f"{name} · 正在播放 {os.path.basename(playing)}")
            return
        disabled = theme_music_store.load_disabled(self._theme_id)
        enabled_count = sum(1 for path, _ in self._tracks if path not in disabled)
        if enabled_count == 0:
            self.card.setContent(f"{name} · 已全部关闭")
        else:
            self.card.setContent(f"{name} · {enabled_count} 首曲目")

    def _on_slider_changed(self, value: int) -> None:
        if self._volume_item.value != value:
            self.cfg.set(self._volume_item, int(value), save=True)

    def _sync_volume(self, value) -> None:
        try:
            number = int(value)
        except (TypeError, ValueError):
            return
        self.slider.blockSignals(True)
        self.slider.setValue(number)
        self.slider.blockSignals(False)
        self._volume_label.setText(str(number))
        self._volume_label.setStyleSheet(
            f"color: {Colors.TEXT_SECONDARY}; background: transparent; "
            f"{get_font_family_css()} {font_size_css(11)}"
        )

    def _current_track(self) -> str:
        player = self._player()
        return player.current_track() if player is not None else ""

    @staticmethod
    def _player():
        try:
            from app.widgets.theme_music_player import ThemeMusicPlayer

            return ThemeMusicPlayer.instance()
        except Exception as e:  # noqa: BLE001 - 播放器不可用时卡片退化为纯展示
            logger.debug(f"[ThemeMusicCard] 播放器不可用: {e}")
            return None
