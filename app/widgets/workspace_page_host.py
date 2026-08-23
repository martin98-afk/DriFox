# -*- coding: utf-8 -*-
"""WorkspacePageHost — 工作区页面宿主：懒创建 / 激活 / 插件卸载清理

接入方式：TabManagerWindow._setup_ui 尾部 attach_to(self)；
刷新触发点：tab_manager_window._update_shared_launcher（plugin 热重载后），
内部对比已加载 vs 注册集，销毁被卸载的页面 + 重建 sidebar 入口 + 重注册命令。
"""

from typing import Any, Dict, List, Optional, Tuple

from loguru import logger


class WorkspacePageHost:
    def __init__(self):
        self._tab_window: Optional[Any] = None
        self._loaded: Dict[str, Tuple[Any, Any]] = {}  # page_id -> (info, widget)
        self._page_indexes: Dict[str, int] = {}  # page_id -> content_area index
        self._sidebar_item_ids: List[str] = []
        self._command_names: List[str] = []  # 已注册的 FUNCTION 命令名（用于卸载清理）

    # ── 接入 ──

    def attach_to(self, tab_window: Any) -> None:
        self._tab_window = tab_window
        self.refresh_pages()

    # ── 刷新与生命周期 ──

    def refresh_pages(self) -> None:
        """按注册集重建侧边栏入口 + 销毁被卸载页面 + 重注册命令

        幂等：可重复调用；重复注册 sidebar item / command 会自动覆盖或跳过。
        """
        if self._tab_window is None:
            return
        from app.plugins.registries.ui_plugin_registry import UIPluginRegistry

        reg = UIPluginRegistry.get_instance()
        # 1. 清旧 sidebar 入口（同时清 _sidebar_items 与 _regions["sidebar"]["entries"]）
        for item_id in self._sidebar_item_ids:
            self._remove_sidebar_item(reg, item_id)
        self._sidebar_item_ids = []
        # 2. 对比销毁：已加载但 registry 中已不存在的页
        current_page_ids = {i.page_id for i in reg.get_workspace_pages()}
        for page_id in list(self._loaded.keys()):
            if page_id not in current_page_ids:
                self._destroy_page(page_id)
        # 3. 注销旧命令（避免被卸载页面残留命令可调用）
        self._unregister_page_commands()
        # 4. 注册新 sidebar 入口（hide_sidebar=True 不进入口）
        for info in reg.get_workspace_pages():
            if info.metadata.get("hide_sidebar"):
                continue
            item_id = f"wp:{info.page_id}"
            try:
                reg.register_sidebar_item(
                    info.plugin_name,
                    item_id,
                    info.title,
                    icon_path=info.icon_path,
                    group="custom",
                    on_click=lambda ctx, pid=info.page_id: self.show_page(pid),
                )
                self._sidebar_item_ids.append(item_id)
            except Exception as e:
                logger.warning(f"[WorkspacePageHost] sidebar register failed ({item_id}): {e}")
        # 5. 注册命令
        self._register_page_commands(reg)

    def show_page(self, page_id: str) -> None:
        """激活页面（首访懒创建）"""
        if self._tab_window is None:
            return
        from app.plugins.registries.ui_plugin_registry import UIPluginRegistry

        info = UIPluginRegistry.get_instance().get_workspace_page(page_id)
        if info is None:
            logger.warning(f"[WorkspacePageHost] unknown page {page_id}")
            return
        if page_id not in self._loaded:
            context = self._tab_window._build_ui_context()
            widget = info.widget_class(parent=self._tab_window._content_area, context=context)
            idx = self._tab_window._content_area.addWidget(widget)
            self._page_indexes[page_id] = idx
            self._loaded[page_id] = (info, widget)
        self._tab_window._content_area.setCurrentIndex(self._page_indexes[page_id])

    def teardown_plugin(self, plugin_name: str) -> None:
        """插件卸载：销毁其页面 + 移除 content_area 占位 + 清入口"""
        if self._tab_window is None:
            return
        for page_id, (info, _w) in list(self._loaded.items()):
            if info.plugin_name != plugin_name:
                continue
            self._destroy_page(page_id)
        self.refresh_pages()

    # ── 查询（测试/诊断）──

    def get_loaded_page_ids(self) -> List[str]:
        return list(self._loaded.keys())

    def get_known_page_ids(self) -> List[str]:
        if self._tab_window is None:
            return []
        from app.plugins.registries.ui_plugin_registry import UIPluginRegistry

        return [i.page_id for i in UIPluginRegistry.get_instance().get_workspace_pages()]

    # ── 内部 ──

    def _remove_sidebar_item(self, reg: Any, item_id: str) -> None:
        try:
            reg._sidebar_items.pop(item_id, None)
        except Exception:
            pass
        try:
            region = reg._regions.get("sidebar")
            if region is not None:
                region["entries"].pop(item_id, None)
        except Exception:
            pass

    def _destroy_page(self, page_id: str) -> None:
        pair = self._loaded.pop(page_id, None)
        if pair is None:
            return
        _info, widget = pair
        try:
            self._tab_window._content_area.removeWidget(widget)
            widget.deleteLater()
        except Exception:
            pass
        self._page_indexes.pop(page_id, None)

    def _register_page_commands(self, reg: Any) -> None:
        try:
            from app.core.builtin_commands import FunctionCommandHandlers
            from app.core.command_manager import CommandManager, CommandType
        except Exception:
            return
        for info in reg.get_workspace_pages():
            cmd_name = (
                info.page_id
                if ":" in info.page_id or info.plugin_name == "system"
                else f"{info.plugin_name}:{info.page_id}"
            )
            mgr = CommandManager.get_instance()
            try:
                mgr.register(
                    name=cmd_name,
                    command_type=CommandType.FUNCTION,
                    description=f"打开页面 {info.title}",
                    argument_hint="",
                )
                FunctionCommandHandlers.register(cmd_name, lambda args, pid=info.page_id: self.show_page(pid))
                if cmd_name not in self._command_names:
                    self._command_names.append(cmd_name)
            except Exception as e:
                logger.warning(f"[WorkspacePageHost] command register failed ({cmd_name}): {e}")

    def _unregister_page_commands(self) -> None:
        try:
            from app.core.command_manager import CommandManager
            from app.core.builtin_commands import FunctionCommandHandlers
        except Exception:
            return
        for name in self._command_names:
            try:
                CommandManager.get_instance().unregister(name)
                FunctionCommandHandlers._handlers.pop(name, None)
            except Exception:
                pass
        self._command_names = []