# -*- coding: utf-8 -*-
"""主题背景音乐播放器（backgrounds.music）

主窗口级单例：多个 TabManagerWindow 共享同一个 QMediaPlayer，避免多窗叠音。

设计要点：
- 列表身份 = 解析后绝对路径的元组。切主题时 key 相同就完全不动播放器，
  多主题共用同一列表时来回切换不打断当前曲目。
- 换列表走「淡出 → 换 → 淡入」，总时长 fade_ms，单 player 实现，
  中间有一瞬静音（真交叉淡入需要双 player，已评估否决）。
- 音量：用户设置（UI/MusicVolume）一旦被调过就恒优先于主题推荐音量。
  用 _user_volume 是否为 None 区分，不用哨兵数值。
- 焦点：ApplicationInactive → pause；回焦时只在「因失焦暂停」的情况下续播，
  用户主动 stop 后不自动复活。
- 失效曲（InvalidMedia）跳过继续下一首，全列表失效则停止，只记 warning。
- 单曲开关：主题列表是全量身份，实际进 playlist 的是「未被用户关掉」的子集。
  关掉正在播放的曲目 → 跳到下一条启用曲；全部关掉 → 停止但保留列表身份，
  重新勾上可立即恢复。开关状态由 theme_music_store 按插件分组持久化。

QtMultimedia 限制：QMediaPlayer 后端读不到 Qt 资源系统，
`:qrc` 路径的音频会 ResourceError，音乐只支持文件路径。
"""
import logging
import os
from typing import Callable, Iterable, Optional

from PySide6.QtCore import QObject, QUrl, Signal
from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer

logger = logging.getLogger(__name__)

_DEFAULT_VOLUME = 0.6
_DEFAULT_FADE_MS = 300

class ThemeMusicPlayer(QObject):
    """主题背景音乐播放器（主窗口级单例）"""

    # 当前曲目变化（绝对路径；空串 = 没有在播的曲目）
    trackChanged = Signal(str)
    # 主题列表整体换了（设置卡据此重建歌单行）
    tracksChanged = Signal()

    _instance: Optional["ThemeMusicPlayer"] = None

    @classmethod
    def instance(cls) -> "ThemeMusicPlayer":
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    def __init__(self, parent: Optional[QObject] = None):
        super().__init__(parent)
        self._player = QMediaPlayer(self)
        self._audio = QAudioOutput(self)
        self._player.setAudioOutput(self._audio)
        self._player.mediaStatusChanged.connect(self._on_media_status)
        self._tracks: tuple = ()  # 主题全量曲目（列表身份）
        self._enabled: tuple = ()  # 实际进播放队列的启用曲目
        self._index: int = -1  # 当前曲目下标（Qt6 移除 QMediaPlaylist，自维护队列）
        self._loop = True  # True=整列表循环（原 QMediaPlaylist.Loop）；
        #                    False=播完当前即停（原 QMediaPlaylist.CurrentItemOnce）
        self._disabled: frozenset = frozenset()  # 用户关掉的曲目绝对路径
        self._theme_volume = _DEFAULT_VOLUME
        self._user_volume: Optional[float] = None
        self._app_active = True
        self._paused_by_focus = False
        # 调用记录：生产代码不读，测试用于断言「播放器被怎么调用了」
        self._calls: list = []
        self._apply_volume()

    # ── 对外接口 ─────────────────────────────────────

    def apply_config(
        self,
        cfg: Optional[dict],
        resolver: Callable[[str], str],
        disabled: Iterable[str] = (),
    ) -> None:
        """应用主题 music 配置；None / enabled=False / 无有效曲目 → 停止并清空

        Args:
            disabled: 用户在该主题里关掉的曲目（绝对路径）。只影响播放子集，
                不改变列表身份——同一主题反复进出不会因开关而重启播放。
        """
        if not cfg or not cfg.get("enabled", True):
            self.stop()
            return

        tracks = self._resolve_tracks(cfg.get("tracks") or [], resolver)
        if not tracks:
            self.stop()
            return

        fade_ms = max(0, int(cfg.get("fade_ms", _DEFAULT_FADE_MS)))
        loop = bool(cfg.get("loop", True))
        self._theme_volume = max(0.0, min(float(cfg.get("volume", _DEFAULT_VOLUME)), 1.0))
        new_disabled = frozenset(str(p) for p in (disabled or ())) & set(tracks)

        if tracks == self._tracks:
            # 同列表：只同步可能在变的部分，不重启播放
            self._playlist_set_loop(loop)
            self._apply_volume()
            if new_disabled != self._disabled:
                self._disabled = new_disabled
                self._rebuild_enabled_playlist()
                if self._enabled and not self._paused_by_focus:
                    self._player.play()
            return

        self._disabled = new_disabled
        self._switch_tracks(tracks, loop, fade_ms)

    def set_volume(self, volume: float) -> None:
        """用户音量设置（0.0~1.0），调用后恒优先于主题推荐音量"""
        self._user_volume = max(0.0, min(float(volume), 1.0))
        self._apply_volume()

    def set_disabled_tracks(self, disabled: Iterable[str]) -> None:
        """更新「用户关掉的曲目」并即时生效（设置卡勾选单曲时调用）"""
        norm = frozenset(str(p) for p in (disabled or ())) & set(self._tracks)
        if norm == self._disabled:
            return
        self._disabled = norm
        if not self._tracks:
            return
        self._rebuild_enabled_playlist()
        if self._enabled and not self._paused_by_focus:
            self._player.play()
        self._apply_volume()

    def current_track(self) -> str:
        """当前正在播放/待播的曲目绝对路径（无则空串）"""
        if not self._enabled:
            return ""
        if 0 <= self._index < len(self._enabled):
            return self._enabled[self._index]
        return ""

    def set_app_active(self, active: bool) -> None:
        """应用焦点变化：失焦暂停，回焦续播（仅续播失焦导致的暂停）

        不读 _player.state()：无音频后端（CI / 无设备）时它恒为 StoppedState，
        「正在播放才 pause」会让失焦暂停永远不生效。改为按「有曲目与否」
        与 _paused_by_focus 标记决定，语义等价且不依赖后端。
        """
        if active == self._app_active:
            return
        self._app_active = active
        if not self._enabled:
            return
        if not active:
            self._player.pause()
            self._paused_by_focus = True
        else:
            if self._paused_by_focus:
                self._paused_by_focus = False
                self._player.play()

    def stop(self) -> None:
        """停止播放并清空当前列表身份"""
        # 先清状态再碰后端：DirectShow 在 stop()/clear() 期间会同步上报
        # InvalidMedia，状态未清时 _on_media_status 会再次调 stop() 无限递归。
        self._tracks = ()
        self._enabled = ()
        self._disabled = frozenset()
        self._paused_by_focus = False
        self._index = -1
        self._player.stop()
        self._player.setSource(QUrl())  # 卸载当前媒体（对应原 playlist.clear()）
        self.trackChanged.emit("")

    def current_tracks(self) -> tuple:
        return self._tracks

    def playlist_mode_name(self) -> str:
        if self._loop:
            return "Loop"
        return "CurrentItemOnce"

    # ── 内部 ─────────────────────────────────────────

    @staticmethod
    def _resolve_tracks(raw_tracks, resolver: Callable[[str], str]) -> tuple:
        """逐个解析并过滤不存在的文件，返回绝对路径元组（列表身份）"""
        out = []
        for item in raw_tracks:
            if not item:
                continue
            try:
                path = resolver(str(item)) if resolver else str(item)
            except Exception as e:
                logger.warning(f"[ThemeMusic] 解析音乐路径失败 {item}: {e}")
                continue
            if path.startswith(":"):
                # QtMultimedia 读不到 qrc，明确跳过而不是静默失败
                logger.warning(f"[ThemeMusic] 不支持 qrc 路径音乐，已跳过: {item}")
                continue
            try:
                if not os.path.exists(path):
                    logger.warning(f"[ThemeMusic] 音乐文件不存在，已跳过: {path}")
                    continue
                out.append(os.path.abspath(path))
            except OSError:
                continue
        # 保序去重
        seen = set()
        uniq = []
        for p in out:
            if p not in seen:
                seen.add(p)
                uniq.append(p)
        return tuple(uniq)

    def _switch_tracks(self, tracks: tuple, loop: bool, fade_ms: int) -> None:
        """淡出 → 换列表 → 淡入"""
        half = max(1, fade_ms // 2) if fade_ms > 0 else 0
        if fade_ms > 0 and self._tracks:
            self._fade_to(0.0, half)
        self._tracks = tracks
        self._paused_by_focus = False
        # 从第一条启用曲开始（保持「换主题从头听」的既有行为）
        first = next((p for p in tracks if p not in self._disabled), "")
        self._rebuild_enabled_playlist(keep_path=first)
        self._playlist_set_loop(loop)
        self.tracksChanged.emit()
        if not self._enabled:
            # 主题曲目被用户全部关掉：保留列表身份，静默不播
            return
        self._audio.setVolume(0 if fade_ms > 0 else self._effective_volume() / 100.0)
        self._player.play()
        if fade_ms > 0:
            self._fade_to(self._effective_volume() / 100.0, half)
        else:
            self._apply_volume()

    def _rebuild_enabled_playlist(self, keep_path: str = "") -> None:
        """按禁用集重建 playlist 内容（不负责 play，由调用方决定）

        尽量保持当前曲目：仍在启用集里就续播它，否则回落到第一条启用曲。
        """
        previous = keep_path or self.current_track()
        enabled = tuple(p for p in self._tracks if p not in self._disabled)
        self._enabled = enabled
        self._player.stop()
        self._index = -1
        if enabled:
            index = enabled.index(previous) if previous in enabled else 0
            # 仅加载不播放（对应原 setCurrentIndex 激活待播），是否 play 由调用方决定
            self._index = index
            self._player.setSource(QUrl.fromLocalFile(enabled[index]))
            self.trackChanged.emit(enabled[index])
        else:
            self._player.setSource(QUrl())  # 卸载当前媒体（对应原 playlist.clear()）
            self.trackChanged.emit("")

    def _effective_volume(self) -> int:
        """有效音量（0~100 int，内部统一口径；写入 QAudioOutput 时换算 0.0~1.0）"""
        v = self._user_volume if self._user_volume is not None else self._theme_volume
        return max(0, min(100, int(round(v * 100))))

    def _apply_volume(self) -> None:
        self._audio.setVolume(self._effective_volume() / 100.0)

    def _fade_to(self, target: float, duration_ms: int) -> None:
        """阶梯式音量渐变（target 为 0.0~1.0 比例，内部换算成 0~100 int）

        duration_ms 很小（半程 ≤150ms），分 5 步同步 setVolume 已足够听感自然。
        不用 QPropertyAnimation：异步动画在窗口销毁时更容易留下悬挂引用。
        """
        target_i = max(0, min(100, int(round(target * 100))))
        if duration_ms <= 0:
            self._audio.setVolume(target_i / 100.0)
            return
        start = round(self._audio.volume() * 100)
        steps = 5
        for i in range(1, steps + 1):
            self._audio.setVolume(round(start + (target_i - start) * i / steps) / 100.0)

    def _on_media_status(self, status) -> None:
        """失效曲跳过；EndOfMedia 按循环模式前进/回头（Qt6 自维护队列，无 QMediaPlaylist）"""
        if status == QMediaPlayer.InvalidMedia:
            # 全禁用态（有列表身份但无启用曲目）不属于「播放失败」：清空队列
            # 期间后端同步上报 InvalidMedia 时直接早退，保留身份供重新勾选恢复。
            if not self._tracks or not self._enabled:
                return
            # index 在播放开始前可能为 -1，用 max(0, ...) 归一；
            # 只要还有下一首就前进，最后一首失效则停止
            nxt = max(0, self._index) + 1
            if nxt >= len(self._enabled):
                logger.warning("[ThemeMusic] 全部曲目无法播放，停止主题音乐")
                self.stop()
                return
            self._play_at(nxt)
            return
        if status == QMediaPlayer.EndOfMedia:
            if not self._loop:
                return  # CurrentItemOnce：播完当前即停（Qt6 播完已自动停止）
            nxt = self._index + 1
            if nxt >= len(self._enabled):
                nxt = 0  # Loop：整列表循环，最后一首回第一首
            self._play_at(nxt)

    def _play_at(self, index: int) -> None:
        """加载并播放启用队列第 index 首，同步对外曲目状态

        同一曲目（单曲循环回头）无需重载：EndOfMedia 后 play() 即从头播放。
        """
        self._index = index
        path = self._enabled[index]
        if self._player.source() == QUrl.fromLocalFile(path):
            self._player.play()
        else:
            self._player.setSource(QUrl.fromLocalFile(path))
            self._player.play()
        self.trackChanged.emit(path)

    def _playlist_set_loop(self, loop: bool) -> None:
        self._loop = bool(loop)
