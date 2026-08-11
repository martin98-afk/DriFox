# -*- coding: utf-8 -*-
"""
GlobalCardController — Tab 级全局卡片控制器（单例）

负责系统配置卡片 / 服务商编辑 / Hook 编辑 / MCP 编辑四张全局卡片的
创建、注册、显隐与保存逻辑。这些卡片不再绑定单个对话窗口，统一托管在
TabManagerWindow 的全局卡片容器（GLOBAL_WINDOW_ID 作用域）中：

- 多窗口（Tab）下只存在一份，所有对话窗口共享；
- 打开时不再隐藏任何对话窗口的输入区（不再接入 per-window 的
  _on_system_card_opened/closed 输入隐藏回调）；
- 卡片内部互斥（system_card=True）仍保留，确保设置/编辑卡一次只显示一张。

per-window 的派生状态（如当前窗口选中的服务商、模型列表）由 _active_window()
指向当前激活对话窗口处理，保存后广播刷新所有窗口。
"""

import copy
from typing import List, Optional

from loguru import logger
from PyQt5.QtCore import QTimer, Qt

from app.utils.config import Settings
from app.widgets.cards.card_manager import GLOBAL_WINDOW_ID, CardManager, ContainerType


_controller: Optional["GlobalCardController"] = None


def get_global_card_controller() -> Optional["GlobalCardController"]:
    """获取全局卡片控制器单例（依赖 TabManagerWindow 已创建）"""
    global _controller
    if _controller is None:
        from app.widgets.tab_manager_window import TabManagerWindow

        tm = TabManagerWindow.get_instance()
        if tm is None:
            return None
        _controller = GlobalCardController(tm, tm._global_card_container)
    return _controller


class GlobalCardController:
    """Tab 级全局卡片控制器"""

    def __init__(self, tab_manager, global_card_container):
        self._tab_manager = tab_manager
        self._global_card_container = global_card_container
        self._card_manager = CardManager.get_instance()
        self.cfg = Settings.get_instance()

        # ── 懒构建的全局卡片实例 ──
        self._settings_popup = None
        # 构建中标志：LLMSettingsCard 构造链（GiteeCard/Hook/MCP 子卡刷新）含
        # QCoreApplication.processEvents()，事件重入可能再次进入 ensure_settings_popup。
        # 若不加保护，会在 _settings_popup 赋值前递归构建多张设置卡（P024：重叠显示）。
        self._settings_popup_building = False
        self._provider_edit_card = None
        self._hook_edit_card = None
        self._mcp_edit_card = None
        self._provider_edit_popup = None
        self._hook_edit_popup = None
        self._mcp_edit_popup = None
        self._diff_viewer_card = None
        self._file_undo_card = None
        self._sub_agent_session_card = None
        # Gitee 绑定提醒去重标记：仅 tab 管理器初始化后提示一次，不随每个对话窗口重复弹出
        self._gitee_reminder_shown = False
        # Gitee token 失效提醒去重标记：与 _gitee_reminder_shown 独立并存，
        # 失效事件（syncDone 含"已失效"）进程级只弹一次
        self._gitee_token_invalid_reminder_shown = False

    # ───────────────────────────────────────────────────────────
    # 窗口辅助
    # ───────────────────────────────────────────────────────────

    def _active_window(self):
        """当前激活的对话窗口（per-window 状态的读写目标）"""
        from app.widgets.tab_manager_window import TabManagerWindow

        tm = TabManagerWindow.get_instance()
        if tm is not None:
            w = tm.get_current_window()
            if w is not None and not getattr(w, "_is_destroyed", False):
                return w
        from app.main_widget import OpenAIChatToolWindow

        for w in OpenAIChatToolWindow._instances:
            if not getattr(w, "_is_destroyed", False):
                return w
        return None

    def _all_windows(self) -> List:
        """所有未销毁的对话窗口"""
        from app.main_widget import OpenAIChatToolWindow

        result = []
        for w in OpenAIChatToolWindow._instances:
            if not getattr(w, "_is_destroyed", False):
                result.append(w)
        return result

    # ───────────────────────────────────────────────────────────
    # 懒构建：系统设置大卡
    # ───────────────────────────────────────────────────────────

    def ensure_settings_popup(self):
        """性能优化：懒构建设置弹窗（重型，隐藏构件），仅构建一次

        重入保护：LLMSettingsCard 构造链（子卡 _refresh 含 QCoreApplication.processEvents）
        可能事件重入再次进入本方法；若 `_settings_popup` 尚未赋值（先构造后赋值），
        会递归构建多张设置卡 → 容器内多卡重叠（P024）。构建中标志直接短路重入调用。
        """
        if self._settings_popup is not None:
            return
        if self._settings_popup_building:
            # 事件重入：正在构建中，直接返回（外层构建完成后 _settings_popup 已赋值）
            return
        self._settings_popup_building = True
        try:
            from app.core.hook_manager import HookManager
            from app.widgets.cards.settings.llm_settings_card import LLMSettingsCard

            self._settings_popup = LLMSettingsCard(self._tab_manager)
            self._settings_popup.setVisible(False)
            self._settings_popup.configChanged.connect(self.on_settings_config_changed)
            self._settings_popup.closed.connect(lambda: self._card_manager.hide_card("settings", GLOBAL_WINDOW_ID))

            # 全局 Hook 列表：HookManager 使用类级共享状态，单例即可跨窗口共用
            try:
                self._settings_popup.hookListCard._hook_manager = HookManager()
                # 重新加载一次（构建时 manager 可能为 None 导致列表为空）
                self._settings_popup.hookListCard._refresh(reload=True)
            except Exception as e:
                logger.warning(f"[GlobalCard] 设置 HookManager 注入失败: {e}")

            # 连接服务商添加/编辑信号
            self._settings_popup.llmProviderCard.showAddProviderCard.connect(self._show_provider_add_card)
            self._settings_popup.llmProviderCard.showEditProviderCard.connect(self._show_provider_edit_card)
            # 连接 Hook 添加/编辑信号
            self._settings_popup.hookListCard.showAddHookCard.connect(self._show_hook_add_card)
            self._settings_popup.hookListCard.showEditHookCard.connect(self._show_hook_edit_card)
            self._settings_popup.hookListCard.hooksChanged.connect(self._on_hook_toggled)
            self._settings_popup.hookListCard.hookToggled.connect(self._on_hook_toggled_light)
            # 连接 MCP 添加/编辑信号
            self._settings_popup.mcpListCard.showAddCard.connect(self._show_mcp_add_card)
            self._settings_popup.mcpListCard.showEditCard.connect(self._show_mcp_edit_card)
            # 注意：MCP 服务器开关/增删改均在各操作点（MCPListSettingCard）内自行完成 UI
            # 更新（开关=行级更新，增删改=局部 _refresh），此处不再把 serversChanged 接到
            # _on_mcp_servers_toggled 做全量重建，否则开关 MCP 时整卡闪烁/列表重建。
            # 跨窗口同步由 .mcp.json 热重载广播负责（见 main_widget HotReload 的 mcp 分支）。
            self._settings_popup.gatewayCard.gatewayToggled.connect(self._on_gateway_toggled)

            # 注册到 CardManager 与全局容器（system_card=True 仅做全局互斥，不隐藏输入区）
            mgr = self._card_manager
            mgr.register_card(GLOBAL_WINDOW_ID, ContainerType.TOP, "settings", self._settings_popup, system_card=True)
            self._global_card_container.add_card("settings", self._settings_popup)
        finally:
            self._settings_popup_building = False

    # ───────────────────────────────────────────────────────────
    # 显示入口
    # ───────────────────────────────────────────────────────────

    def toggle_settings(self):
        """切换设置卡片的显示"""
        self.ensure_settings_popup()
        self._card_manager.toggle_card("settings", GLOBAL_WINDOW_ID)

    def open_settings(self):
        """打开设置卡片并确保宿主窗口前置"""
        self.ensure_settings_popup()
        self._card_manager.toggle_card("settings", GLOBAL_WINDOW_ID)
        if self._card_manager.is_card_visible("settings", GLOBAL_WINDOW_ID):
            tm = self._tab_manager
            if tm.isMinimized():
                tm.showNormal()
            tm.activateWindow()
            tm.raise_()
            self._settings_popup.raise_()
            self._settings_popup.activateWindow()

    def on_settings_config_changed(self):
        """外观设置变更 → 刷新所有窗口的模型列表 + 批量主题刷新"""

        for win in self._all_windows():
            try:
                win._on_settings_config_changed()
            except Exception as e:
                logger.warning(f"[GlobalCard] 窗口配置刷新失败: {e}")

    # ───────────────────────────────────────────────────────────
    # 服务商编辑卡片
    # ───────────────────────────────────────────────────────────

    def _ensure_provider_edit_card(self):
        """确保服务商编辑卡片已创建并注册到全局作用域"""
        if self._provider_edit_card is not None:
            return
        from app.widgets.cards.settings.base_settings_card import BaseSettingsCard
        from app.widgets.cards.settings.provider_edit_card import ProviderEditCard

        self._provider_edit_card = BaseSettingsCard("服务商配置", "⚙️", parent=self._tab_manager)
        self._provider_edit_card.setMinimumHeight(300)
        self._provider_edit_card.set_height_mode("content")
        self._provider_edit_popup = ProviderEditCard(parent=self._provider_edit_card)
        self._provider_edit_popup.saved.connect(
            lambda name, info: self._on_provider_edit_saved(name, info, is_new=True)
        )
        self._provider_edit_popup.closed.connect(self._on_provider_edit_closed)
        self._provider_edit_card.content_layout.addWidget(self._provider_edit_popup)
        self._provider_edit_card.set_save_button_handler(self._provider_edit_popup._on_save)
        self._provider_edit_card.setVisible(False)
        self._provider_edit_card.closed.connect(self._on_provider_edit_card_closed)
        mgr = self._card_manager
        mgr.register_card(
            GLOBAL_WINDOW_ID, ContainerType.TOP, "provider_edit", self._provider_edit_card, system_card=True
        )
        self._global_card_container.add_card("provider_edit", self._provider_edit_card)

    def _show_provider_add_card(self):
        """显示添加服务商卡片"""
        from app.widgets.cards.settings.provider_edit_card import ProviderEditCard

        self._ensure_provider_edit_card()
        self._card_manager.hide_card("settings", GLOBAL_WINDOW_ID)
        self._provider_edit_card.set_title("⚙️ 添加服务商")
        self._provider_edit_popup = ProviderEditCard(
            provider_name="", provider_info={}, is_new=True, parent=self._provider_edit_card
        )
        self._provider_edit_popup.saved.connect(
            lambda name, info: self._on_provider_edit_saved(name, info, is_new=True)
        )
        self._provider_edit_popup.closed.connect(self._on_provider_edit_closed)
        while self._provider_edit_card.content_layout.count():
            item = self._provider_edit_card.content_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        self._provider_edit_card.content_layout.addWidget(self._provider_edit_popup)
        self._provider_edit_card.set_save_button_handler(lambda: self._provider_edit_popup._on_save())
        self._card_manager.show_card("provider_edit", GLOBAL_WINDOW_ID)

    def _show_provider_edit_card(self, config_id: str, provider_info: dict):
        """显示编辑服务商卡片"""
        from app.widgets.cards.settings.provider_edit_card import ProviderEditCard

        self._ensure_provider_edit_card()
        self._card_manager.hide_card("settings", GLOBAL_WINDOW_ID)
        display_name = provider_info.get("name", "") or provider_info.get("provider_name", config_id)
        self._provider_edit_card.set_title(f"⚙️ 编辑: {display_name}")
        if "provider_name" not in provider_info:
            provider_info["provider_name"] = display_name
        self._provider_edit_popup = ProviderEditCard(
            provider_name=provider_info["provider_name"],
            provider_info=provider_info,
            is_new=False,
            parent=self._provider_edit_card,
        )
        self._provider_edit_popup.saved.connect(
            lambda name, info: self._on_provider_edit_saved(name, info, is_new=False)
        )
        self._provider_edit_popup.closed.connect(self._on_provider_edit_closed)
        while self._provider_edit_card.content_layout.count():
            item = self._provider_edit_card.content_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        self._provider_edit_card.content_layout.addWidget(self._provider_edit_popup)
        self._provider_edit_card.set_save_button_handler(lambda: self._provider_edit_popup._on_save())
        self._card_manager.show_card("provider_edit", GLOBAL_WINDOW_ID)

    def _on_provider_edit_saved(self, provider_name: str, provider_info: dict, is_new: bool = False):
        """服务商编辑保存后的回调（全局配置落盘 + 广播所有窗口刷新）"""
        saved_providers = copy.deepcopy(self.cfg.llm_saved_providers.value) or {}
        from app.core.provider_profile import ProviderConfigCollision, apply_provider_save

        try:
            new_config_id = apply_provider_save(saved_providers, provider_info, provider_name, is_new=is_new)
        except ProviderConfigCollision:
            from app.widgets.common_dialogs import InfoDialog

            _dialog = InfoDialog(
                title="配置冲突",
                content="该 (API_URL, API_KEY) 组合已被其他配置占用，请修改后重试。\n\n"
                "同名服务商可以使用不同 base_url 分别配置（如 coding plan / 普通 plan），\n"
                "但 (URL, KEY) 必须唯一。",
                confirm_text="知道了",
                parent=self._tab_manager,
            )
            _dialog.exec_()
            return

        self.cfg.set(self.cfg.llm_saved_providers, saved_providers, save=True)

        # 各窗口选中服务商随 config_id 迁移
        for win in self._all_windows():
            try:
                old = win._current_provider_name
                if old and old != new_config_id and win._valid_configs.get(old):
                    if saved_providers.get(new_config_id) is provider_info and old not in saved_providers:
                        win._current_provider_name = new_config_id
                        if win.cfg.llm_selected_model.value == old:
                            win.cfg.set(win.cfg.llm_selected_model, new_config_id, save=True)
            except Exception:
                pass

        # 隐藏编辑卡，显示设置卡
        self._card_manager.hide_card("provider_edit", GLOBAL_WINDOW_ID)
        self._card_manager.show_card("settings", GLOBAL_WINDOW_ID)

        # 模型选择卡片数据将在下次打开时自动刷新
        for win in self._all_windows():
            try:
                win._load_model_configs()
            except Exception:
                pass

        from qfluentwidgets import InfoBar, InfoBarPosition

        InfoBar.success(
            "已保存",
            f"服务商 '{provider_name}' 已保存",
            parent=self._tab_manager,
            duration=2000,
            position=InfoBarPosition.BOTTOM,
        )

    def _on_provider_edit_closed(self):
        """服务商编辑关闭后的回调"""
        self._card_manager.hide_card("provider_edit", GLOBAL_WINDOW_ID)
        self._card_manager.show_card("settings", GLOBAL_WINDOW_ID)

    def _on_provider_edit_card_closed(self):
        """服务商编辑卡片（SystemCardFrame）关闭回调 → 回到设置面板"""
        self._card_manager.hide_card("provider_edit", GLOBAL_WINDOW_ID)
        self._card_manager.show_card("settings", GLOBAL_WINDOW_ID)

    # ───────────────────────────────────────────────────────────
    # Hook 编辑卡片
    # ───────────────────────────────────────────────────────────

    def _ensure_hook_edit_card(self):
        """确保 Hook 编辑卡片已创建并注册到全局作用域"""
        if self._hook_edit_card is not None:
            return
        from app.widgets.cards.settings.base_settings_card import BaseSettingsCard
        from app.widgets.cards.settings.hook_setting_card import HookEditCard

        self._hook_edit_card = BaseSettingsCard("Hook 配置", "⚙️", parent=self._tab_manager)
        self._hook_edit_card.setMinimumHeight(200)
        self._hook_edit_card.set_height_mode("proportional")
        self._hook_edit_popup = HookEditCard(parent=self._hook_edit_card)
        self._hook_edit_popup.saved.connect(self._on_hook_edit_saved)
        self._hook_edit_popup.closed.connect(self._on_hook_edit_closed)
        self._hook_edit_card.content_layout.addWidget(self._hook_edit_popup)
        self._hook_edit_card.set_save_button_handler(self._hook_edit_popup._on_save)
        self._hook_edit_card.setVisible(False)
        self._hook_edit_card.closed.connect(self._on_hook_edit_card_closed)
        mgr = self._card_manager
        mgr.register_card(GLOBAL_WINDOW_ID, ContainerType.TOP, "hook_edit", self._hook_edit_card, system_card=True)
        self._global_card_container.add_card("hook_edit", self._hook_edit_card)

    def _show_hook_add_card(self):
        """显示添加 Hook 卡片"""
        self._ensure_hook_edit_card()
        from app.widgets.cards.settings.hook_setting_card import HookEditCard

        self._card_manager.hide_card("settings", GLOBAL_WINDOW_ID)
        self._hook_edit_card.set_title("➕ 添加 Hook")
        hm = None
        if self._settings_popup is not None:
            hm = self._settings_popup.hookListCard._hook_manager
        self._hook_edit_popup = HookEditCard(parent=self._hook_edit_card, hook_manager=hm)
        self._hook_edit_popup.saved.connect(self._on_hook_edit_saved)
        self._hook_edit_popup.closed.connect(self._on_hook_edit_closed)
        while self._hook_edit_card.content_layout.count():
            item = self._hook_edit_card.content_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        self._hook_edit_card.content_layout.addWidget(self._hook_edit_popup)
        self._hook_edit_card.set_save_button_handler(lambda: self._hook_edit_popup._on_save())
        self._hook_edit_card.set_header_sticky("")
        self._card_manager.show_card("hook_edit", GLOBAL_WINDOW_ID)

    def _show_hook_edit_card(self, hook_id: str, hook_data: dict):
        """显示编辑 Hook 卡片"""
        self._ensure_hook_edit_card()
        from app.widgets.cards.settings.hook_setting_card import HookEditCard

        self._card_manager.hide_card("settings", GLOBAL_WINDOW_ID)
        self._hook_edit_card.set_title("✏️ 编辑 Hook")
        hm = None
        if self._settings_popup is not None:
            hm = self._settings_popup.hookListCard._hook_manager
        self._hook_edit_popup = HookEditCard(hook_data=hook_data, parent=self._hook_edit_card, hook_manager=hm)
        self._hook_edit_popup.saved.connect(self._on_hook_edit_saved)
        self._hook_edit_popup.closed.connect(self._on_hook_edit_closed)
        while self._hook_edit_card.content_layout.count():
            item = self._hook_edit_card.content_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        self._hook_edit_card.content_layout.addWidget(self._hook_edit_popup)
        self._hook_edit_card.set_save_button_handler(lambda: self._hook_edit_popup._on_save())
        self._hook_edit_card.set_header_sticky(self._hook_edit_popup.get_source_display())
        self._card_manager.show_card("hook_edit", GLOBAL_WINDOW_ID)

    def _on_hook_edit_saved(self, values: dict):
        """Hook 保存回调（全局生效 + 广播所有窗口后端重载）"""
        self._card_manager.hide_card("hook_edit", GLOBAL_WINDOW_ID)
        self._card_manager.show_card("settings", GLOBAL_WINDOW_ID)

        if self._settings_popup is None:
            return
        hook_card = self._settings_popup.hookListCard
        original = self._hook_edit_popup.get_original_data()
        hook_id = original.get("id", "") if original else ""
        hm = hook_card._hook_manager

        # ── inject_agent_identity hook：同步下拉框选择到 Settings.llm_primary_agent ──
        if hook_id == "builtin_inject_agent_identity" and "agent" in values:
            try:
                selected_agent = values["agent"]
                self.cfg.llm_primary_agent.value = selected_agent
                for win in self._all_windows():
                    try:
                        if win.backend and win.backend.chat_engine:
                            win.backend.chat_engine._invalidate_session_system_prompt_cache()
                    except Exception:
                        pass
                logger.info(f"[GlobalCard] llm_primary_agent = {selected_agent}, session cache invalidated")
            except Exception as e:
                logger.warning(f"[GlobalCard] Failed to sync llm_primary_agent: {e}")

        if hook_id and hm:
            hm.edit_hook_by_id(hook_id, values)
            hm.reload_global_hooks(str(hook_card._hooks_config_file))
            hook_card._refresh(reload=True)
        elif hm:
            add_kwargs = dict(
                event=values["event"],
                command=values["command"],
                matcher=values["matcher"],
                hook_type=values["type"],
                enabled=values["enabled"],
            )
            if "commandWindows" in values:
                add_kwargs["commandWindows"] = values["commandWindows"]
            hook_card._add_hook(**add_kwargs)

        # 广播给所有窗口后端重载 Hook（全局配置已落盘）
        hooks_file = str(hook_card._hooks_config_file)
        for win in self._all_windows():
            try:
                if win.backend and win.backend.hook_manager:
                    win.backend.hook_manager.reload_global_hooks(hooks_file)
            except Exception:
                pass

    def _on_hook_edit_closed(self):
        """Hook 编辑关闭回调"""
        self._card_manager.hide_card("hook_edit", GLOBAL_WINDOW_ID)
        self._card_manager.show_card("settings", GLOBAL_WINDOW_ID)

    def _on_hook_edit_card_closed(self):
        """Hook 编辑卡片（SystemCardFrame）关闭回调 → 回到设置面板"""
        self._card_manager.hide_card("hook_edit", GLOBAL_WINDOW_ID)
        self._card_manager.show_card("settings", GLOBAL_WINDOW_ID)

    # ───────────────────────────────────────────────────────────
    # MCP 编辑卡片
    # ───────────────────────────────────────────────────────────

    def _ensure_mcp_edit_card(self):
        """确保 MCP 编辑卡片已创建并注册到全局作用域"""
        if self._mcp_edit_card is not None:
            return
        from app.widgets.cards.settings.base_settings_card import BaseSettingsCard

        self._mcp_edit_card = BaseSettingsCard("MCP 服务器", "🔌", parent=self._tab_manager)
        self._mcp_edit_card.setMinimumHeight(200)
        self._mcp_edit_card.set_height_mode("content")
        self._mcp_edit_popup = None
        self._mcp_edit_card.setVisible(False)
        self._mcp_edit_card.closed.connect(self._on_mcp_edit_card_closed)
        mgr = self._card_manager
        mgr.register_card(GLOBAL_WINDOW_ID, ContainerType.TOP, "mcp_edit", self._mcp_edit_card, system_card=True)
        self._global_card_container.add_card("mcp_edit", self._mcp_edit_card)

    def _show_mcp_add_card(self):
        """显示添加 MCP 服务器卡片"""
        from app.widgets.cards.settings.mcp_setting_card import MCPEditCard

        self._ensure_mcp_edit_card()
        self._card_manager.hide_card("settings", GLOBAL_WINDOW_ID)
        self._mcp_edit_card.set_title("🔌 添加 MCP 服务器")
        self._mcp_edit_popup = MCPEditCard(server_data=None, parent=self._mcp_edit_card)
        self._mcp_edit_popup.saved.connect(self._on_mcp_edit_saved)
        self._mcp_edit_popup.closed.connect(self._on_mcp_edit_closed)
        while self._mcp_edit_card.content_layout.count():
            item = self._mcp_edit_card.content_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        self._mcp_edit_card.content_layout.addWidget(self._mcp_edit_popup)
        self._mcp_edit_card.set_save_button_handler(lambda: self._mcp_edit_popup._on_save())
        self._setup_mcp_edit_mode_buttons()
        from app.utils.design_tokens import apply_font_size_to_widget

        apply_font_size_to_widget(self._mcp_edit_popup, 14)
        self._card_manager.show_card("mcp_edit", GLOBAL_WINDOW_ID)

    def _show_mcp_edit_card(self, name: str, server_data: dict):
        """显示编辑 MCP 服务器卡片"""
        from app.widgets.cards.settings.mcp_setting_card import MCPEditCard

        self._ensure_mcp_edit_card()
        self._card_manager.hide_card("settings", GLOBAL_WINDOW_ID)
        self._mcp_edit_card.set_title(f"🌐 编辑: {name}")
        self._mcp_edit_popup = MCPEditCard(server_data=server_data, parent=self._mcp_edit_card)
        self._mcp_edit_popup.saved.connect(self._on_mcp_edit_saved)
        self._mcp_edit_popup.closed.connect(self._on_mcp_edit_closed)
        while self._mcp_edit_card.content_layout.count():
            item = self._mcp_edit_card.content_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        self._mcp_edit_card.content_layout.addWidget(self._mcp_edit_popup)
        self._mcp_edit_card.set_save_button_handler(lambda: self._mcp_edit_popup._on_save())
        self._setup_mcp_edit_mode_buttons()
        from app.utils.design_tokens import apply_font_size_to_widget

        apply_font_size_to_widget(self._mcp_edit_popup, 14)
        self._card_manager.show_card("mcp_edit", GLOBAL_WINDOW_ID)

    def _setup_mcp_edit_mode_buttons(self):
        """设置 MCP 编辑卡头的模式切换按钮"""
        self._mcp_edit_popup.modeChanged.connect(self._refresh_mcp_mode_buttons)
        self._refresh_mcp_mode_buttons(self._mcp_edit_popup._json_mode)

    def _refresh_mcp_mode_buttons(self, is_json: bool):
        """刷新 MCP 编辑卡头的模式切换按钮状态"""
        self._mcp_edit_card.set_mode_buttons(
            [
                {
                    "label": "表单",
                    "active": not is_json,
                    "handler": lambda: self._try_toggle_to_form(),
                },
                {
                    "label": "JSON",
                    "active": is_json,
                    "handler": lambda: self._try_toggle_to_json(),
                },
            ]
        )

    def _try_toggle_to_form(self):
        if self._mcp_edit_popup and not self._mcp_edit_popup._json_mode:
            return
        self._mcp_edit_popup._toggle_mode()

    def _try_toggle_to_json(self):
        if self._mcp_edit_popup and self._mcp_edit_popup._json_mode:
            return
        self._mcp_edit_popup._toggle_mode()

    def _on_mcp_edit_saved(self, server_data: dict):
        """MCP 编辑保存回调（全局 PluginManager 落盘 + 刷新全局列表）"""
        self._card_manager.hide_card("mcp_edit", GLOBAL_WINDOW_ID)
        self._card_manager.show_card("settings", GLOBAL_WINDOW_ID)
        if self._settings_popup is None:
            return
        mcp_card = self._settings_popup.mcpListCard
        from app.core.plugin_manager import PluginManager

        pm = PluginManager.get_instance()
        new_name = server_data.get("name", "")
        original_name = getattr(self._mcp_edit_popup, "_original_name", None)
        lookup_name = original_name if original_name else new_name

        servers = mcp_card._get_servers()
        is_edit = any(s.get("name") == lookup_name for s in servers)
        if is_edit:
            pm.update_mcp_server(lookup_name, server_data)
        else:
            pm.add_mcp_server(new_name, server_data)

        mcp_card._refresh()
        QTimer.singleShot(500, mcp_card.refresh_connections)

    def _on_mcp_edit_closed(self):
        """MCP 编辑关闭回调"""
        self._card_manager.hide_card("mcp_edit", GLOBAL_WINDOW_ID)
        self._card_manager.show_card("settings", GLOBAL_WINDOW_ID)
        if self._settings_popup is not None:
            self._settings_popup.mcpListCard.refresh_connections()

    def _on_mcp_edit_card_closed(self):
        """MCP 编辑卡片（SystemCardFrame）关闭回调 → 回到设置面板"""
        self._card_manager.hide_card("mcp_edit", GLOBAL_WINDOW_ID)
        self._card_manager.show_card("settings", GLOBAL_WINDOW_ID)

    # ───────────────────────────────────────────────────────────
    # 内嵌差异对比卡片（替代弹窗 DiffViewerWindow，覆盖对话区域）
    # ───────────────────────────────────────────────────────────

    def ensure_diff_viewer(self):
        """懒构建内嵌差异对比卡片（重型，隐藏构件），仅构建一次"""
        if self._diff_viewer_card is not None:
            return
        from app.widgets.cards.settings.diff_viewer_card import DiffViewerCard

        self._diff_viewer_card = DiffViewerCard(self._tab_manager)
        self._diff_viewer_card.setVisible(False)
        self._diff_viewer_card.closed.connect(lambda: self._card_manager.hide_card("diff_viewer", GLOBAL_WINDOW_ID))

        mgr = self._card_manager
        mgr.register_card(GLOBAL_WINDOW_ID, ContainerType.TOP, "diff_viewer", self._diff_viewer_card, system_card=True)
        self._global_card_container.add_card("diff_viewer", self._diff_viewer_card)

    def show_diff_viewer(self, html: str, title: str = "文件差异对比"):
        """内嵌显示差异对比面板

        Args:
            html: DiffHtmlGenerator 生成的完整 HTML 报告
            title: 卡片标题
        """
        self.ensure_diff_viewer()
        card = self._diff_viewer_card
        card.load_html(html, title)
        # 隐藏其他全局系统卡片（如设置），避免覆盖层堆叠
        self._card_manager.hide_card("settings", GLOBAL_WINDOW_ID)
        self._card_manager.show_card("diff_viewer", GLOBAL_WINDOW_ID)

    def show_file_undo(self, operations, file_recorder, on_finished):
        """显示文件撤销卡片；差异关闭后返回此卡片。"""
        from app.widgets.cards.settings.file_undo_card import FileUndoCard

        if self._file_undo_card is not None:
            self._card_manager.hide_card("file_undo", GLOBAL_WINDOW_ID)
            self._global_card_container.remove_card("file_undo")
            self._file_undo_card.deleteLater()
        self._file_undo_card = FileUndoCard(operations, file_recorder, self._tab_manager)
        self._file_undo_card.finished.connect(
            lambda result, selected: self._finish_file_undo(on_finished, result, selected)
        )
        self._file_undo_card.diffRequested.connect(self._show_file_undo_diff)
        self._file_undo_card.closed.connect(lambda: self._finish_file_undo(on_finished, FileUndoCard.CANCEL, []))
        self._card_manager.register_card(
            GLOBAL_WINDOW_ID, ContainerType.TOP, "file_undo", self._file_undo_card, system_card=True
        )
        self._global_card_container.add_card("file_undo", self._file_undo_card)
        self._card_manager.show_card("file_undo", GLOBAL_WINDOW_ID)

    def _finish_file_undo(self, on_finished, result, selected):
        """关闭撤销卡片并把用户选择交回原撤销流程。"""
        self._card_manager.hide_card("file_undo", GLOBAL_WINDOW_ID)
        on_finished(result, selected)

    def _show_file_undo_diff(self, html, title):
        self.show_diff_viewer(html, title)
        self._diff_viewer_card.closed.connect(self._return_to_file_undo, type=Qt.UniqueConnection)

    def _return_to_file_undo(self):
        self._card_manager.hide_card("diff_viewer", GLOBAL_WINDOW_ID)
        if self._file_undo_card is not None:
            self._card_manager.show_card("file_undo", GLOBAL_WINDOW_ID)

    def hide_file_undo(self):
        self._card_manager.hide_card("file_undo", GLOBAL_WINDOW_ID)

    def hide_diff_viewer(self):
        """隐藏内嵌差异对比面板"""
        if self._diff_viewer_card is None:
            return
        self._card_manager.hide_card("diff_viewer", GLOBAL_WINDOW_ID)

    def toggle_diff_viewer(self):
        """切换内嵌差异对比面板显隐"""
        self.ensure_diff_viewer()
        if self._card_manager.is_card_visible("diff_viewer", GLOBAL_WINDOW_ID):
            self.hide_diff_viewer()
        else:
            # 无内容时重新构建（如当前 HTML 已失效）
            self._card_manager.show_card("diff_viewer", GLOBAL_WINDOW_ID)

    def invalidate_diff_viewer(self):
        """销毁内嵌差异卡片，下次打开时重建（配置/会话切换后调用）"""
        card = self._diff_viewer_card
        if card is None:
            return
        if card.isVisible():
            self._card_manager.hide_card("diff_viewer", GLOBAL_WINDOW_ID)
        self._card_manager.unregister_card("diff_viewer", GLOBAL_WINDOW_ID)
        self._global_card_container.remove_card("diff_viewer")
        try:
            card.setParent(None)
            card.deleteLater()
        except RuntimeError:
            pass
        self._diff_viewer_card = None

    # ───────────────────────────────────────────────────────────
    # 内嵌子智能体会话卡片（替代弹窗 SubAgentSessionDialog，覆盖对话区域）
    # ───────────────────────────────────────────────────────────

    def ensure_sub_agent_session(self):
        """懒构建内嵌子智能体会话卡片（重型，隐藏构件），仅构建一次"""
        if self._sub_agent_session_card is not None:
            return
        from app.widgets.cards.settings.sub_agent_session_card import SubAgentSessionCard

        self._sub_agent_session_card = SubAgentSessionCard(self._tab_manager)
        self._sub_agent_session_card.setVisible(False)
        self._sub_agent_session_card.closed.connect(
            lambda: self._card_manager.hide_card("sub_agent_session", GLOBAL_WINDOW_ID)
        )

        mgr = self._card_manager
        mgr.register_card(
            GLOBAL_WINDOW_ID,
            ContainerType.TOP,
            "sub_agent_session",
            self._sub_agent_session_card,
            system_card=True,
        )
        self._global_card_container.add_card("sub_agent_session", self._sub_agent_session_card)

    def show_sub_agent_session(self, task_id, agent_name, logs, summary=None, logs_provider=None):
        """内嵌显示子智能体会话面板

        Args:
            task_id: 任务ID
            agent_name: 智能体名称
            logs: 初始日志列表
            summary: 任务摘要
            logs_provider: 可选。获取最新日志的回调（运行中自动轮询刷新）
        """
        self.ensure_sub_agent_session()
        card = self._sub_agent_session_card
        card.load(task_id, agent_name, logs, summary, logs_provider)
        # 隐藏其他全局系统卡片（如设置），避免覆盖层堆叠
        self._card_manager.hide_card("settings", GLOBAL_WINDOW_ID)
        self._card_manager.show_card("sub_agent_session", GLOBAL_WINDOW_ID)

    def hide_sub_agent_session(self):
        """隐藏内嵌子智能体会话面板"""
        if self._sub_agent_session_card is None:
            return
        self._card_manager.hide_card("sub_agent_session", GLOBAL_WINDOW_ID)

    # ───────────────────────────────────────────────────────────
    # 列表变更广播（Hook/MCP/Gateway 开关 → 刷新全局列表 + 各窗口后端）
    # ───────────────────────────────────────────────────────────

    def _on_hook_toggled(self):
        """Hook 开关/增删 → 刷新全局列表 + 各窗口后端重载"""
        if self._settings_popup is None:
            return
        hook_card = self._settings_popup.hookListCard
        hm = hook_card._hook_manager
        hooks_file = str(hook_card._hooks_config_file)
        if hm:
            hm.reload_global_hooks(hooks_file)
            hook_card._refresh(reload=True)
        for win in self._all_windows():
            try:
                if win.backend and win.backend.hook_manager:
                    win.backend.hook_manager.reload_global_hooks(hooks_file)
            except Exception:
                pass

    def _on_hook_toggled_light(self, hook_id: str, enabled: bool):
        """Hook 单项开关同步 → 仅更新全局列表 Switch 状态"""
        if self._settings_popup is not None:
            self._settings_popup.hookListCard.update_toggle_state(hook_id, enabled)

    def _on_mcp_servers_toggled(self):
        """MCP 服务器开关变更 → 刷新全局列表

        已不再做全量 _refresh()：settings popup 是全局唯一共享卡片，且各操作点
        （MCPListSettingCard 的开关/增删改）已自行完成 UI 更新。全量重建只由
        .mcp.json 热重载广播触发（main_widget HotReload mcp 分支）。
        """
        return

    def _on_gateway_toggled(self):
        """Gateway 平台开关/配置变更 → 刷新全局列表"""
        if self._settings_popup is not None:
            self._settings_popup.gatewayCard._refresh()

    # ───────────────────────────────────────────────────────────
    # Gitee 绑定提醒（原 main_widget 逻辑，迁移到全局作用域）
    # ───────────────────────────────────────────────────────────

    def check_gitee_sync_reminder(self):
        """启动后检查：未绑定 Gitee 且提醒开启时，弹 InfoBar 引导绑定

        全局去重：多个对话窗口都会在初始化后延迟调用本方法，
        仅首次真正弹出一次，后续窗口调用直接跳过。
        """
        if self._gitee_reminder_shown:
            return
        if self._settings_popup is None:
            # 设置弹窗改为按需构建（不再 3500ms 预构建）——此处兜底构建，
            # 保证 gitee 绑定提醒不被延迟构建策略影响（两处必须同改）。
            self.ensure_settings_popup()
        if self._settings_popup is None:
            return
        if self.cfg.gitee_bound.value:
            return
        if not self.cfg.gitee_sync_remind.value:
            return

        from PyQt5.QtCore import Qt
        from PyQt5.QtWidgets import QWidget, QHBoxLayout
        from qfluentwidgets import InfoBar, InfoBarIcon, InfoBarPosition, PrimaryPushButton, PushButton

        infobar = InfoBar(
            icon=InfoBarIcon.INFORMATION,
            title="绑定 Gitee 账号",
            content=("• 配置与自定义插件自动备份，仅自己可见\n• 会话记录与项目文件分享可选择公开或私有仓库\n"),
            orient=Qt.Vertical,
            isClosable=True,
            duration=-1,
            position=InfoBarPosition.BOTTOM,
            parent=self._tab_manager,
        )

        btn_container = QWidget()
        btn_layout = QHBoxLayout(btn_container)
        btn_layout.setContentsMargins(0, 0, 0, 0)
        btn_layout.setSpacing(8)

        btn_bind = PrimaryPushButton("立即绑定")
        btn_bind.setFixedWidth(90)
        btn_bind.clicked.connect(lambda: self.open_gitee_bind_from_reminder(infobar))
        btn_layout.addWidget(btn_bind)

        btn_dismiss = PushButton("不再提醒")
        btn_dismiss.setFixedWidth(90)
        btn_dismiss.clicked.connect(lambda: self._dismiss_gitee_reminder(infobar))
        btn_layout.addWidget(btn_dismiss)

        infobar.widgetLayout.addWidget(btn_container, 0, Qt.AlignRight)
        infobar.show()
        self._gitee_reminder_shown = True

    def _dismiss_gitee_reminder(self, infobar):
        """提醒中点击「不再提醒」：持久化设置并关闭"""
        self.cfg.set(self.cfg.gitee_sync_remind, False, save=True)
        infobar.close()

    def check_gitee_token_invalid_reminder(self):
        """Gitee token 真失效（invalid_grant）时弹出「重新绑定」提醒

        触发源：ConfigSyncService.syncDone(False, "Gitee token 已失效，请重新绑定")。
        该事件仅在曾绑定过（进入过 token 刷新流程）时才会发出——未绑定时
        ConfigSync 只会发"未绑定 Gitee，跳过同步"，因此"曾绑定过"由事件本身
        保证，此处无需再检查 gitee_bound（失效时已被 ConfigSync 清为 False）。

        全局去重：多窗口都会收到 syncDone 并调用本方法，仅首次真正弹出。
        """
        if self._gitee_token_invalid_reminder_shown:
            return
        if self._settings_popup is None:
            return
        if not self.cfg.gitee_sync_remind.value:
            return

        from PyQt5.QtCore import Qt
        from PyQt5.QtWidgets import QWidget, QHBoxLayout
        from qfluentwidgets import InfoBar, InfoBarIcon, InfoBarPosition, PrimaryPushButton, PushButton

        infobar = InfoBar(
            icon=InfoBarIcon.ERROR,
            title="Gitee 绑定已失效",
            content="Gitee token 已失效，请重新绑定以恢复配置同步",
            orient=Qt.Vertical,
            isClosable=True,
            duration=-1,
            position=InfoBarPosition.BOTTOM,
            parent=self._tab_manager,
        )

        btn_container = QWidget()
        btn_layout = QHBoxLayout(btn_container)
        btn_layout.setContentsMargins(0, 0, 0, 0)
        btn_layout.setSpacing(8)

        btn_bind = PrimaryPushButton("重新绑定")
        btn_bind.setFixedWidth(90)
        btn_bind.clicked.connect(lambda: self.open_gitee_bind_from_reminder(infobar))
        btn_layout.addWidget(btn_bind)

        btn_dismiss = PushButton("不再提醒")
        btn_dismiss.setFixedWidth(90)
        btn_dismiss.clicked.connect(lambda: self._dismiss_gitee_reminder(infobar))
        btn_layout.addWidget(btn_dismiss)

        infobar.widgetLayout.addWidget(btn_container, 0, Qt.AlignRight)
        infobar.show()
        self._gitee_token_invalid_reminder_shown = True

    def open_gitee_bind_from_reminder(self, infobar):
        """提醒中点击「立即绑定」：关闭提醒，打开设置定位到 Gitee 卡片"""
        infobar.close()
        self.open_settings()
        if self._settings_popup is not None:
            scroll_bar = self._settings_popup.scroll_area.verticalScrollBar()
            scroll_bar.setValue(0)

    # ───────────────────────────────────────────────────────────
    # 其他窗口打开系统卡片时，隐藏全局卡片（避免层级叠加）
    # ───────────────────────────────────────────────────────────

    def hide_all_global_cards(self):
        """隐藏所有全局卡片（供 per-window 系统卡片互斥时调用）"""
        for cid in ("settings", "provider_edit", "hook_edit", "mcp_edit", "diff_viewer", "sub_agent_session"):
            self._card_manager.hide_card(cid, GLOBAL_WINDOW_ID)

    def invalidate_settings_popup(self):
        """销毁设置弹窗，下次打开时重建（配置从 Gitee 恢复后调用）

        所有子卡片（provider/MCP/gateway/font 等）会在重建时从 Settings
        读取最新值，无需逐卡片手动刷新。
        """
        popup = self._settings_popup
        if popup is None:
            return
        if popup.isVisible():
            self._card_manager.hide_card("settings", GLOBAL_WINDOW_ID)
        self._card_manager.unregister_card("settings", GLOBAL_WINDOW_ID)
        self._global_card_container.remove_card("settings")
        try:
            popup.setParent(None)
            popup.deleteLater()
        except RuntimeError:
            pass
        self._settings_popup = None
