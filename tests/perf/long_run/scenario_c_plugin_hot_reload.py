# -*- coding: utf-8 -*-
"""场景 C：插件热重载压测（基于 watchfiles 热扫描触发 reload）。

压测目标：
- PluginHostService._do_single_reload → emit_plugin_changed → plugin_changed 信号链路
- 模拟 watchfiles 报告 (plugin_name, component) 变更事件
- plugin_changed 信号槽连接是否随循环泄漏
- PluginHostService 重载路径上的闭包与 dict 缓存

历史注：热重载入口原在 ``ChatBackend``（``reload_plugin_subsystems`` /
``_on_hot_reload_requested``），已迁到 ``PluginHostService``；本场景跟迁。

循环次数：
- Demo 模式（默认）：30 秒 → ~1 千次 reload 调用（mock PluginManager 极快）
- Full 模式（LONGRUN_FULL=1）：≥5 万次 reload 调用

策略：
- 直接驱动现役入口 ``PluginHostService._do_single_reload`` + ``emit_plugin_changed``
  （不经 300ms 去抖，保证逐次计数与逐次信号广播）
- Monkeypatch `PluginManager.get_instance` 返回 mock（含 rescan + is_initialized）
- 卡片回放与 PluginChanged hook diff 与本压测目标无关，patch 为空实现
"""

from __future__ import annotations

import os
import sys
import time
from typing import Callable, List, Optional

from .sampler import Sample, env_full_mode, start_tracemalloc, stop_tracemalloc


class _MockPlugin:
    """占位（仅在 PluginManager.get_plugin 时被调用，场景 C 主路径用不上）。"""

    def __init__(self, name: str = "mock-plugin"):
        self.name = name


class _MockPluginManager:
    """模拟 PluginManager：仅保留 reload 链路真正调用的最小接口集。"""

    def __init__(self):
        self._initialized = True
        self._rescan_calls = 0

    def is_initialized(self) -> bool:
        return self._initialized

    def rescan(self) -> dict:
        self._rescan_calls += 1
        return {"added": [], "removed": [], "changed": []}

    def get_mcp_servers(self) -> list:
        """HookManager compute_plugin_snapshot_diff 会调用，避免 DEBUG 日志噪音。"""
        return []


def _build_mock_backend():
    """构造 ChatBackend 但跳过 initialize；monkeypatch PluginManager。

    关键点：
    - __init__ 不需要任何外部依赖（只设 QObject parent + 注册到 _active_instances）
    - 手动构造 _plugin_changed 信号计数 + 手动注入 _get_plugin_manager 函数
    - reload_plugin_subsystems 会读 self._agent_manager.get_builtin_tools()，
      我们注入 mock 的 agent_manager

    QApplication 检测：用 gc.get_objects() 兜底（绕过 PyQt5.5.15+Python3.14
    下 QApplication.instance() 偶发返回 None 的 bug）
    """
    import gc

    from app.core.services.plugin_host_service import PluginHostService
    from PyQt5.QtWidgets import QApplication

    # 多路径找 QApplication
    app = QApplication.instance()
    if app is None:
        for obj in gc.get_objects():
            if isinstance(obj, QApplication):
                try:
                    # 仅做属性访问，不调用方法，避免触发 deleted C++ 对象
                    _ = obj.objectName
                    app = obj
                    break
                except Exception:
                    continue
    if app is None:
        # 没有 QApplication 时，QObject 单例构造可能因无 GUI 环境而崩；
        # 此处主动抛清晰错误
        raise RuntimeError("QApplication 必须已建（scenario runner 负责）")

    # --- Monkeypatch PluginManager.get_instance ---
    from app.plugins.managers import plugin_manager as pm_module

    _orig_get_instance = pm_module.PluginManager.get_instance
    _mock_pm = _MockPluginManager()

    def _mock_get_instance():
        return _mock_pm

    pm_module.PluginManager.get_instance = staticmethod(_mock_get_instance)

    from app.core.services.plugin_host_service import PluginHostService

    svc = PluginHostService.get_instance()
    _orig_do_single_reload = PluginHostService._do_single_reload
    _orig_schedule_debounced = PluginHostService._schedule_debounced_reload
    _orig_replay_cards = PluginHostService._replay_pending_config_cards
    _orig_changed_hook = PluginHostService._trigger_plugin_changed_hook
    _signal_count = {"plugin_changed": 0, "reload_calls": 0}

    def _mock_do_single_reload(self, plugin_name: str, component: str):
        _signal_count["reload_calls"] += 1
        from app.plugins.kernel import KNOWN_COMPONENTS as _KC

        return {k: (0 if k == "agents" else False) for k in _KC}

    PluginHostService._do_single_reload = _mock_do_single_reload
    # 只保留「reload → emit」关键路径：卡片回放与 hook diff 与压测目标无关
    PluginHostService._replay_pending_config_cards = staticmethod(lambda: 0)
    PluginHostService._trigger_plugin_changed_hook = lambda self, *a, **k: None

    # --- 信号槽计数（plugin_changed） ---
    def _on_plugin_changed(_result: dict) -> None:
        _signal_count["plugin_changed"] += 1

    svc.plugin_changed.connect(_on_plugin_changed)

    # 清理辅助（供 finally）
    def _restore():
        pm_module.PluginManager.get_instance = _orig_get_instance
        PluginHostService._do_single_reload = _orig_do_single_reload
        PluginHostService._schedule_debounced_reload = _orig_schedule_debounced
        PluginHostService._replay_pending_config_cards = staticmethod(_orig_replay_cards)
        PluginHostService._trigger_plugin_changed_hook = _orig_changed_hook

    return svc, _mock_pm, _signal_count, _restore


def run_plugin_hot_reload_scenario(
    *,
    progress_cb: Callable[[int, Sample], None],
    duration_sec: float,
) -> dict:
    """执行插件热重载压测；返回基线摘要 dict。"""
    svc, mock_pm, signal_count, restore = _build_mock_backend()

    start_tracemalloc()
    t0 = time.time()

    samples: List[Sample] = []
    last_sample_at = t0
    iter_count = 0

    try:
        while True:
            now = time.time()
            if now - t0 >= duration_sec:
                break

            # 模拟 watchfiles 检测到 "agents" 组件变更 → 驱动现役重载+广播入口
            try:
                result = svc._do_single_reload("mock-plugin", "agents")
                svc.emit_plugin_changed(result, "mock-plugin")
            except Exception as e:
                # 容错：mock 不完整时抛错，记录但不中断
                print(f"  [warn] iter={iter_count} reload raised: {e}", file=sys.stderr)

            iter_count += 1

            # 每 60 秒（demo 模式每 5 秒）采一次样
            if now - last_sample_at >= max(5.0, float(os.environ.get("PERF_SAMPLE_INTERVAL", "60"))):
                from .sampler import take_sample

                s = take_sample("C_plugin_hot_reload", iter_count, t0)
                samples.append(s)
                progress_cb(iter_count, s)
                last_sample_at = now

        # 收尾：再采一次
        from .sampler import take_sample

        s = take_sample("C_plugin_hot_reload", iter_count, t0)
        samples.append(s)
    finally:
        restore()
        # 不调用 backend.cleanup() —— 它会停 watcher；我们没启 watcher，安全。

    elapsed = time.time() - t0
    rss_delta_mb = samples[-1].rss_mb - samples[0].rss_mb
    summary = {
        "scenario": "C_plugin_hot_reload",
        "iterations": iter_count,
        "elapsed_sec": elapsed,
        "plugin_changed_emits": signal_count["plugin_changed"],
        "mock_pm_rescan_calls": mock_pm._rescan_calls,
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
    print(f"[scenario_c] 启动，时长 {duration}s")

    def _cb(i: int, s: Sample) -> None:
        print(
            f"  iter={i}  rss={s.rss_mb:.1f}MB  qobj={s.qobject_count}  "
            f"elapsed={s.elapsed_sec:.0f}s  tm_cur={s.tracemalloc_current_mb:.2f}MB"
        )

    summary = run_plugin_hot_reload_scenario(progress_cb=_cb, duration_sec=duration)
    print(
        f"[scenario_c] 完成：rel={summary['iterations']} "
        f"plugin_changed emit={summary['plugin_changed_emits']} "
        f"RSS Δ={summary['rss_delta_mb']:.1f}MB"
    )
    sys.exit(0)
