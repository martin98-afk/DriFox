# -*- coding: utf-8 -*-
"""project-dashboard UI 组件入口 — 欢迎卡片「📊 项目看板」tab + function 命令

- register_welcome_tab：欢迎卡片新增 tab，iframe 展示 .drifox/reports/project-dashboard.html
- FunctionCommandHandlers：/project-dashboard 生成 HTML，生成后刷新欢迎卡片
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


def _report_path() -> str:
    """报告文件绝对路径（不存在返回空串）"""
    root = _get_project_root()
    try:
        from dashboard import find_git_root

        git_root = find_git_root(root)
        if not git_root:
            return ""
        p = Path(git_root) / ".drifox" / "reports" / "project-dashboard.html"
        return str(p) if p.exists() else ""
    except Exception:
        return ""


def _render_welcome_tab(ctx: dict = None) -> str:
    """welcome tab render_func：概要行 + iframe + 追问按钮

    iframe src 带 ?t=<mtime> 时间戳，生成后强制重新加载最新文件。
    """
    is_dark = ctx.get("is_dark") if isinstance(ctx, dict) else None
    light = "--text: #333; --muted: #999;"
    dark = "--text: #e6e6e6; --muted: #8a8a8a;"
    if is_dark is not None:
        root_css = f":root {{ {dark if is_dark else light} }}"
    else:
        root_css = (
            f":root {{ {light} }}"
            f"@media (prefers-color-scheme: dark) {{ :root {{ {dark} }} }}"
        )

    rp = _report_path()
    if not rp:
        body = '<div class="pd-empty">📊 尚未生成项目看板，点击下方按钮生成</div>'
    else:
        try:
            mtime = int(Path(rp).stat().st_mtime)
        except OSError:
            mtime = 0
        body = (
            '<iframe class="pd-frame" '
            f'src="file:///{Path(rp).as_posix()}?t={mtime}" '
            'loading="lazy"></iframe>'
        )

    return f"""<div class="pd-wrap">
  {body}
  <span class="context-tag pd-refresh" data-action="ask"
        data-content="/project-dashboard">🔄 重新生成</span>
</div>
<style>
.pd-wrap {{ max-width: 640px; margin: 0 auto; }}
.pd-frame {{ width: 100%; height: 460px; border: 1px solid var(--text-muted, rgba(128,128,128,0.2));
             border-radius: 10px; background: transparent; }}
.pd-empty {{ padding: 18px 4px; color: var(--text); }}
.pd-refresh {{ display: inline-block; margin-top: 10px; cursor: pointer;
               color: var(--text, #333); }}
{root_css}
</style>
"""


def _handler(args: str) -> None:
    """/project-dashboard 命令 handler：生成 HTML + 刷新欢迎卡片"""
    root = _get_project_root()
    try:
        from dashboard import write_report

        # is_dark：跟随 Qt 主题（生成时状态）
        is_dark = True
        try:
            from app.utils.theme_manager import theme_manager

            is_dark = not theme_manager.is_light_theme()
        except Exception:
            pass
        path = write_report(root, is_dark)
        if path:
            logger.info(f"[project-dashboard] report generated: {path}")
        else:
            logger.warning("[project-dashboard] report generation failed (not a git repo?)")
    except Exception as e:
        logger.error(f"[project-dashboard] generate failed: {e}")
    # 刷新欢迎卡片（重新渲染 iframe，加载最新文件）
    try:
        from app.core.ui_plugin_registry import UIPluginRegistry

        UIPluginRegistry.get_instance()._refresh_welcome_cards()
    except Exception:
        pass


def register_ui(registry):
    """注册 project-dashboard 的 UI 组件"""
    # 清理旧子模块缓存（热重载兼容）
    prefix = "ui_plugin_project_dashboard."
    stale = [k for k in sys.modules if k.startswith(prefix)]
    for k in stale:
        del sys.modules[k]

    # 确保 ui 目录在 sys.path（相对导入 dashboard）
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

    # 注册 function 命令（命令定义已由 commands/*.md 提供）
    try:
        from app.core.builtin_commands import FunctionCommandHandlers
        from app.core.command_manager import CommandManager, CommandType

        cmd_mgr = CommandManager.get_instance()
        if not cmd_mgr.has_command("project-dashboard"):
            cmd_mgr.register(
                name="project-dashboard",
                command_type=CommandType.FUNCTION,
                description="生成项目信息看板 HTML（.drifox/reports/）",
            )
        FunctionCommandHandlers.register("project-dashboard", _handler)
    except Exception as e:
        logger.error(f"[project-dashboard] command register failed: {e}")

    logger.info("[project-dashboard] UI components registered")
