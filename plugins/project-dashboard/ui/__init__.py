# -*- coding: utf-8 -*-
"""project-dashboard UI 组件入口 — 欢迎卡片「📊 项目看板」tab

render_func 返回 markdown 片段（概要行 + ```echarts 代码块），
经欢迎卡片 markdown → CodeWebViewer(QWebEngineView) 管线渲染，
与 context-stats 插件同款模式：无 iframe、无独立 HTML 文件。

数据采集全部异步（QThread）：首次打开显示「加载中」占位，
后台采集完成自动重渲染欢迎卡片，不阻塞 UI 主线程。
"""

import os
import sys
from pathlib import Path

from loguru import logger


def _get_project_root() -> str:
    """获取当前项目 git 根：优先 registry 活跃窗口 provider，兜底 os.getcwd()"""
    try:
        from app.core.ui_plugin_registry import UIPluginRegistry

        reg = UIPluginRegistry.get_instance()
        provider = reg._resolve_active_window_provider()
        if provider is not None:
            ctx = provider()
            root = ctx.get("project_root")
            if root:
                return root
    except Exception:
        pass
    return os.getcwd()


def _render_welcome_tab(ctx: dict = None) -> str:
    """welcome tab render_func：概要行 + echarts 代码块

    - 缓存命中：直接返回已采集数据的图表
    - 未命中：返回「加载中」占位 + 启动后台采集（完成后自动重渲染）
    """
    is_dark = ctx.get("is_dark") if isinstance(ctx, dict) else None
    if is_dark is None:
        is_dark = True  # DriFox 默认深色主题

    root = _get_project_root()

    from collector import build_cache_key, get_collector

    collector = get_collector()
    cache_key = build_cache_key(root)
    data = collector.get_cached(cache_key)

    if data is None:
        # 未命中：启动后台采集，先显示加载占位
        collector.start(root, is_dark, cache_key)
        return "📊 正在采集项目数据…\n\n*首次生成需数秒，完成后自动显示*"

    if data.get("error"):
        return f"> ⚠️ {data['error']}\n"

    # 概要行
    import json

    from dashboard import build_options

    repo = data.get("repo_name", "")
    branch = data.get("branch", "")
    generated = data.get("generated_at", "")
    total = data.get("total_commits", 0)
    summary = f"**{repo}** · `{branch}` · 生成于 {generated} · 近 30 天 **{total}** commits"

    # 2 个 echarts 代码块（双 grid 合并，各 400px）
    options = build_options(data, is_dark)
    parts = [summary, ""]
    for opt in options:
        parts.append("```echarts")
        parts.append(json.dumps(opt, ensure_ascii=False))
        parts.append("```")
        parts.append("")
    return "\n".join(parts)


def register_ui(registry):
    """注册 project-dashboard 的 UI 组件"""
    # 清理旧子模块缓存（热重载兼容）
    prefix = "ui_plugin_project_dashboard."
    stale = [k for k in sys.modules if k.startswith(prefix)]
    for k in stale:
        del sys.modules[k]

    # 确保 ui 目录在 sys.path（相对导入 dashboard / async）
    ui_dir = str(Path(__file__).resolve().parent)
    if ui_dir not in sys.path:
        sys.path.insert(0, ui_dir)

    registry.register_welcome_tab(
        plugin_name="project-dashboard",
        mode_key="project-dashboard",
        label="📊 项目看板",
        render_func=_render_welcome_tab,
        priority=0,
    )
    logger.info("[project-dashboard] UI components registered")
