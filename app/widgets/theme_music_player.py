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

QtMultimedia 限制：QMediaPlayer 后端读不到 Qt 资源系统，
`:qrc` 路径的音频会 ResourceError，音乐只支持文件路径。
"""
import logging
import os
from typing import Callable, Optional

from PyQt5.QtCore import QObject, QUrl
from PyQt5.QtMultimedia import QMediaContent, QMediaPlayer, QMediaPlaylist

logger = logging.getLogger(__name__)

_DEFAULT_VOLUME = 0.6
_DEFAULT_FADE_MS = 300

class ThemeMusicPlayer(QObject):
    """主题背景音乐播放器（主窗口级单例）"""

    _instance: Optional["ThemeMusicPlayer"] = None

    @classmethod
    def instance(cls) -> "ThemeMusicPlayer":
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    def __init__(self, parent: Optional[QObject] = None):
        super().__init__(parent)
        self._player = QMediaPlayer(self)
        self._playlist = QMediaPlaylist(self)
        self._player.setPlaylist(self._playlist)
        self._player.mediaStatusChanged.connect(self._on_media_status)
        self._tracks: tuple = ()
        self._theme_volume = _DEFAULT_VOLUME
        self._user_volume: Optional[float] = None
        self._app_active = True
        self._paused_by_focus = False
        # 调用记录：生产代码不读，测试用于断言「播放器被怎么调用了」
        self._calls: list = []
        self._apply_volume()

    # ── 对外接口 ─────────────────────────────────────

    def apply_config(self, cfg: Optional[dict], resolver: Callable[[str], str]) -> None:
        """应用主题 music 配置；None / enabled=False / 无有效曲目 → 停止并清空"""
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

        if tracks == self._tracks:
            # 同列表：只同步可能在变的部分，不重启播放
            self._playlist_set_loop(loop)
            self._apply_volume()
            return

        self._switch_tracks(tracks, loop, fade_ms)

    def set_volume(self, volume: float) -> None:
        """用户音量设置（0.0~1.0），调用后恒优先于主题推荐音量"""
        self._user_volume = max(0.0, min(float(volume), 1.0))
        self._apply_volume()

    def set_app_active(self, active: bool) -> None:
        """应用焦点变化：失焦暂停，回焦续播（仅续播失焦导致的暂停）

        不读 _player.state()：无音频后端（CI / 无设备）时它恒为 StoppedState，
        「正在播放才 pause」会让失焦暂停永远不生效。改为按「有曲目与否」
        与 _paused_by_focus 标记决定，语义等价且不依赖后端。
        """
        if active == self._app_active:
            return
        self._app_active = active
        if not self._tracks:
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
        self._player.stop()
        self._playlist.clear()
        self._tracks = ()
        self._paused_by_focus = False

    def current_tracks(self) -> tuple:
        return self._tracks

    def playlist_mode_name(self) -> str:
        mode = self._playlist.playbackMode()
        if mode == QMediaPlaylist.Loop:
            return "Loop"
        if mode == QMediaPlaylist.CurrentItemOnce:
            return "CurrentItemOnce"
        return str(mode)

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
        self._player.stop()
        self._playlist.clear()
        for p in tracks:
            self._playlist.addMedia(QMediaContent(QUrl.fromLocalFile(p)))
        self._playlist_set_loop(loop)
        self._tracks = tracks
        self._paused_by_focus = False
        self._player.setVolume(0 if fade_ms > 0 else self._effective_volume())
        self._playlist.setCurrentIndex(0)
        self._player.play()
        if fade_ms > 0:
            self._fade_to(self._effective_volume() / 100.0, half)
        else:
            self._apply_volume()

    def _playlist_set_loop(self, loop: bool) -> None:
        self._playlist.setPlaybackMode(
            QMediaPlaylist.Loop if loop else QMediaPlaylist.CurrentItemOnce
        )

    def _effective_volume(self) -> int:
        """有效音量（0~100 int，QMediaPlayer 只收 int）"""
        v = self._user_volume if self._user_volume is not None else self._theme_volume
        return max(0, min(100, int(round(v * 100))))

    def _apply_volume(self) -> None:
        self._player.setVolume(self._effective_volume())

    def _fade_to(self, target: float, duration_ms: int) -> None:
        """阶梯式音量渐变（target 为 0.0~1.0 比例，内部换算成 0~100 int）

        duration_ms 很小（半程 ≤150ms），分 5 步同步 setVolume 已足够听感自然。
        不用 QPropertyAnimation：异步动画在窗口销毁时更容易留下悬挂引用。
        """
        target_i = max(0, min(100, int(round(target * 100))))
        if duration_ms <= 0:
            self._player.setVolume(target_i)
            return
        start = self._player.volume()
        steps = 5
        for i in range(1, steps + 1):
            self._player.setVolume(int(round(start + (target_i - start) * i / steps)))

    def _on_media_status(self, status) -> None:
        """失效曲跳过；列表播完由 playlist 模式决定，无需干预"""
        if status != QMediaPlayer.InvalidMedia:
            return
        if not self._tracks:
            return
        # currentIndex 在播放开始前可能为 -1，用 max(0, ...) 归一；
        # 只要还有下一首就前进，最后一首失效则停止
        nxt = max(0, self._playlist.currentIndex()) + 1
        if nxt >= self._playlist.mediaCount():
            logger.warning("[ThemeMusic] 全部曲目无法播放，停止主题音乐")
            self.stop()
            return
        self._playlist.setCurrentIndex(nxt)
        self._player.play()
