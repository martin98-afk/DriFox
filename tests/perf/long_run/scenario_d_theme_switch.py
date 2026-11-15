# -*- coding: utf-8 -*-
"""场景 D：主题切换压测（cfg.set + broadcast_theme_refresh + 活事件循环泵）。

压测目标：
- ``cfg.set(cfg.ui_theme_style, id, save=False)`` → ``broadcast_theme_refresh()``
  → 全窗口 ``_on_settings_config_changed`` 的完整链路（theme_mode.py:186-218）
- QSS 全量重排 + 分帧补渲依赖事件循环：本场景是**首个需要活事件循环的场景**，
  每 5s 切一次主题，切换后 pump 2.0s（batched 30ms 防抖与补渲全靠 QEventLoop）

暴露标注（判读时已知噪音源，非缺陷）：
- **M4**：QSS 全量重排会污染心跳判据（本场景采样不含心跳；8h 档判读注意）
- **C2**：CodeWebViewer.refresh_theme 全量重渲染是主题刷新主成本源
- **K2**：主题资源重解码；测试用固定主题对，避免引入额外重解码变量

实现说明：
- runner 进程内无 OpenAIChatToolWindow（不构造宿主主窗），broadcast 的 alive
  windows 为空；QSS 重排由本场景自建窗口承接——与 ``_on_settings_config_changed``
  同源的「styleSheet 全量重设」路径，重排成本一致
- OpenAIChatToolWindow 侧的完整刷新在宿主共存场景（冒烟档）由真实链路覆盖
"""

from __future__ import annotations

import os
import sys
import time
from typing import Callable, List

from .sampler import Sample, env_full_mode, start_tracemalloc, stop_tracemalloc


def _pump_events(seconds: float) -> None:
    """活事件循环泵：QSS 30ms 防抖与分帧补渲都在这段时间内被派发。"""
    from PyQt5.QtCore import QEventLoop, QTimer

    loop = QEventLoop()
    QTimer.singleShot(int(seconds * 1000), loop.quit)
    loop.exec_()


def _pick_theme_pair() -> List[str]:
    """任选一深一浅组成切换对；不可分（全深/全浅）时退回前两个主题。"""
    from app.utils.theme_manager import theme_manager

    ids = list(theme_manager.list_themes())
    dark = [x for x in ids if not theme_manager.is_light_theme(x)]
    light = [x for x in ids if theme_manager.is_light_theme(x)]
    if dark and light:
        return [dark[0], light[0]]
    return ids[:2] if len(ids) >= 2 else ids


def _theme_qss(theme_id: str) -> str:
    """按深浅生成 QSS（与 _on_settings_config_changed 同源的 styleSheet 全量重设）。"""
    from app.utils.theme_manager import theme_manager

    light = bool(theme_manager.is_light_theme(theme_id))
    bg = "#f5f7fa" if light else "#1a1d24"
    fg = "#1f2933" if light else "#e6eaf0"
    border = "#d9e1ea" if light else "#2a3038"
    return (
        f"QWidget {{ background: {bg}; color: {fg}; }}"
        f"QLabel {{ color: {fg}; border: 1px solid {border}; border-radius: 6px; padding: 6px; }}"
    )


def run_theme_switch_scenario(
    *,
    progress_cb: Callable[[int, Sample], None],
    duration_sec: float,
) -> dict:
    """执行主题切换压测；返回基线摘要 dict。"""
    from PyQt5.QtWidgets import QApplication, QLabel, QPushButton, QWidget

    from app.utils.config import Settings
    from app.utils.theme_manager import theme_manager
    from app.utils.theme_mode import broadcast_theme_refresh

    app = QApplication.instance() or QApplication(sys.argv)
    cfg = Settings.get_instance()

    pair = _pick_theme_pair()
    start_tracemalloc()
    t0 = time.time()

    # 自建窗口承接 QSS 全量重排（重排成本与宿主 _on_settings_config_changed 同源）
    window = QWidget()
    window.setWindowTitle("perf-scenario-D")
    window.resize(480, 320)
    layout_buttons: List[QPushButton] = []
    labels: List[QLabel] = []
    for i in range(4):
        btn = QPushButton(f"tab-{i} 占位按钮")
        window_layout_label = QLabel(f"block-{i}\n" + "x" * 400)
        layout_buttons.append(btn)
        labels.append(window_layout_label)
    from PyQt5.QtWidgets import QVBoxLayout

    lay = QVBoxLayout(window)
    for btn in layout_buttons:
        lay.addWidget(btn)
    for lab in labels:
        lay.addWidget(lab)
    window.show()
    app.processEvents()

    initial_theme = cfg.ui_theme_style.value
    window.setStyleSheet(_theme_qss(str(initial_theme or pair[0])))
    app.processEvents()

    samples: List[Sample] = []
    last_sample_at = t0
    switches = 0

    def _maybe_sample(now: float, force: bool = False) -> None:
        nonlocal last_sample_at
        if force or now - last_sample_at >= max(5.0, float(os.environ.get("PERF_SAMPLE_INTERVAL", "60"))):
            from .sampler import take_sample

            s = take_sample("D_theme_switch", switches, t0)
            samples.append(s)
            progress_cb(switches, s)
            last_sample_at = now

    try:
        while True:
            now = time.time()
            if now - t0 >= duration_sec:
                break
            theme_id = pair[switches % len(pair)]
            # 规格入口链：cfg.set(save=False) -> broadcast_theme_refresh()
            cfg.set(cfg.ui_theme_style, theme_id, save=False)
            broadcast_theme_refresh()
            # 活事件循环泵：30ms 防抖 + 分帧补渲在此派发（必须，不能只 sleep）
            _pump_events(2.0)
            # 自建窗口补一次同源 QSS 全量重设（重排成本即 M4/C2 所指）
            window.setStyleSheet(_theme_qss(str(cfg.ui_theme_style.value)))
            app.processEvents()
            switches += 1
            print(
                f"  [d] switch={switches} theme={theme_id} rss={samples[-1].rss_mb:.1f}MB"
                if samples
                else f"  [d] switch={switches} theme={theme_id}"
            )
            _maybe_sample(time.time())
    finally:
        window.setStyleSheet("")
        window.close()
        _maybe_sample(time.time(), force=True)

    elapsed = time.time() - t0
    rss_delta_mb = samples[-1].rss_mb - samples[0].rss_mb
    summary = {
        "scenario": "D_theme_switch",
        "iterations": switches,
        "elapsed_sec": elapsed,
        "theme_pair": pair,
        "switch_count": switches,
        "current_theme": str(cfg.ui_theme_style.value),
        "rss_mb_first": samples[0].rss_mb,
        "rss_mb_last": samples[-1].rss_mb,
        "rss_delta_mb": rss_delta_mb,
        "qobject_first": samples[0].qobject_count,
        "qobject_last": samples[-1].qobject_count,
        "qobject_delta": samples[-1].qobject_count - samples[0].qobject_count,
        "samples": samples,
    }
    stop_tracemalloc()
    return summary


if __name__ == "__main__":
    from PyQt5.QtWidgets import QApplication

    _app = QApplication.instance() or QApplication(sys.argv)

    duration = 30.0
    if env_full_mode():
        duration = 1800.0
    print(f"[scenario_d] 启动，时长 {duration}s")

    def _cb(i: int, s: Sample) -> None:
        print(
            f"  iter={i}  rss={s.rss_mb:.1f}MB  qobj={s.qobject_count}  "
            f"elapsed={s.elapsed_sec:.0f}s  tm_cur={s.tracemalloc_current_mb:.2f}MB"
        )

    summary = run_theme_switch_scenario(progress_cb=_cb, duration_sec=duration)
    print(
        f"[scenario_d] 完成：switches={summary['switch_count']} "
        f"pair={summary['theme_pair']} current={summary['current_theme']} "
        f"RSS Δ={summary['rss_delta_mb']:.1f}MB"
    )
    sys.exit(0)
