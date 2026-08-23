# -*- coding: utf-8 -*-
"""
中央卡片管理器 - 按窗口隔离管理所有卡片的显示状态

设计原则：
- 每个窗口独立管理自己的卡片（通过 window_id 隔离）
- 系统卡片组窗口内互斥：同一窗口内所有标记为 system 的卡片，一次只能显示一张
- 非系统卡片同容器互斥：Tool/SubAgent 等同容器内互斥
- 不同容器可共存（如 Top 的 Todo + Bottom 的 Tool）
- Question 强制覆盖所有
- 系统卡片活跃时（question 除外）：压制所有非系统卡片

优先级层级：
  1. Question（强制覆盖一切）
  2. 命令卡片（压制 tool/sub_agent）
  3. 系统卡片（settings/history/memory 等）
  4. 实时卡片（todo/tool/sub_agent）—— 系统卡片存在时被压制
"""

from enum import Enum
from typing import Any, Callable, Dict, List, Optional

from loguru import logger


class ContainerType(Enum):
    TOP = "top"  # chatscroll 上方
    BOTTOM = "bottom"  # chatscroll 下方
    LEFT = "left"  # 内容区左侧停靠区（Tab 级全局卡片 / UI 插件卡片）
    RIGHT = "right"  # 内容区右侧停靠区（Tab 级全局卡片 / UI 插件卡片）


# ── 停靠区容器 ──
# LEFT/RIGHT 作为独立停靠区：
# - 仅同容器互斥（同一侧一次显示一张卡片）
# - 不参与系统卡片的跨容器压制（打开设置卡片不会关掉左右停靠面板）
# - 不被 question 卡片强制关闭
#
# BOTTOM 在 Tab 模式下通过 mark_coexist_containers() 加入共存集合，
# 与 LEFT/RIGHT 共存（TabManagerWindow._setup_ui 中配置）。
# 覆盖层（TOP）通过 QStackedWidget 仅替换对话区，与 LEFT/RIGHT/BOTTOM
# 无互斥关系：四向区域可同时存在、互不关闭。
DOCK_CONTAINER_TYPES = frozenset({ContainerType.LEFT, ContainerType.RIGHT})


# ── 全局卡片作用域 ──
# Tab 管理器级别的卡片（系统配置/服务商编辑/Hook 编辑/MCP 编辑等）
# 不再绑定单个对话窗口，统一注册在该保留 window_id 下。
# 对话级卡片（项目/会话/模型选择等）仍使用各窗口自己的 window_id。
GLOBAL_WINDOW_ID = "__global__"


class CardManager:
    """
    中央卡片管理器 - 按窗口隔离

    数据结构：
    {
        "window_1": {
            "cards": {ContainerType.TOP: {}, ContainerType.BOTTOM: {}},
            "containers": {"settings": ContainerType.TOP, ...},
            "system_cards": set(),
            "visible_cards": {ContainerType.TOP: None, ContainerType.BOTTOM: None},
            "shown_callbacks": {"card_id": [cb1, cb2]},
            "hidden_callbacks": {"card_id": [cb1, cb2]},
        },
        ...
    }
    """

    _instance = None

    @classmethod
    def get_instance(cls) -> "CardManager":
        if cls._instance is None:
            cls._instance = object.__new__(cls)
            cls._instance.__init_state()
        return cls._instance

    @classmethod
    def reset_instance(cls):
        """重置单例（主要用于测试）"""
        cls._instance = None

    def __init__(self):
        pass

    def __init_state(self):
        # 按窗口隔离的数据
        # {
        #   "window_id": {
        #       "cards": {ContainerType.TOP: {}, ContainerType.BOTTOM: {}},
        #       "containers": {"card_id": ContainerType, ...},
        #       "system_cards": set(),
        #       "visible_cards": {ContainerType.TOP: None, ContainerType.BOTTOM: None},
        #       "shown_callbacks": {"card_id": [cb1, cb2]},
        #       "hidden_callbacks": {"card_id": [cb1, cb2]},
        #       "suppressed_by_system": False,  # 系统卡片活跃时压制非系统卡片
        #       "suppress_others_map": {},  # card_id -> set of suppressed card_ids
        #       "suppressed_by_others": set(),  # 被其他卡片压制的 card_id 集合
        #   }
        # }
        self._window_data: Dict[str, Dict[str, Any]] = {}
        # 共存容器：同一窗口内仅同容器互斥、不跨容器互斥的容器类型集合
        # （如 Tab 模式下 LEFT/RIGHT/BOTTOM 可同时显示、互不关闭）
        self._coexist_containers: Dict[str, "frozenset[ContainerType]"] = {}

    def _ensure_window_initialized(self, window_id: str):
        """确保窗口数据已初始化"""
        if window_id not in self._window_data:
            self._window_data[window_id] = {
                "cards": {ct: {} for ct in ContainerType},
                "containers": {},  # card_id -> ContainerType
                "system_cards": set(),
                "visible_cards": {ct: None for ct in ContainerType},
                "shown_callbacks": {},
                "hidden_callbacks": {},
                "suppress_others_map": {},  # card_id -> set of suppressed card_ids
                "suppressed_by_others": set(),  # 被其他卡片压制的 card_id 集合
                # Phase G：dock（LEFT/RIGHT）多卡堆叠数据模型
                "dock_visible_cards": {ct: [] for ct in DOCK_CONTAINER_TYPES},  # list[card_id]
                "dock_active_cards": {ct: None for ct in DOCK_CONTAINER_TYPES},  # 栈顶 card_id
            }

    def mark_coexist_containers(self, window_id: str, containers: "frozenset[ContainerType]"):
        """标记指定窗口中可共存的容器类型

        共存容器之间仅同容器互斥（同一侧一次显示一张卡片），不同容器可同时显示。
        覆盖层（TOP 容器）与共存容器无互斥关系：四向区域可同时存在、互不关闭。
        覆盖层通过 QStackedWidget 仅替换对话区，LEFT/RIGHT/BOTTOM 不受影响。

        Args:
            window_id: 窗口标识
            containers: 共存容器类型集合（如 frozenset({LEFT, RIGHT, BOTTOM})）
        """
        self._ensure_window_initialized(window_id)
        self._coexist_containers[window_id] = containers

    def _ensure_state_initialized(self):
        """兼容旧代码"""
        pass

    def register_window(self, window_id: str):
        """注册窗口到管理器（窗口创建时调用）"""
        self._ensure_window_initialized(window_id)

    def unregister_window(self, window_id: str):
        """注销窗口及其所有卡片数据（窗口关闭时调用）"""
        if window_id in self._window_data:
            del self._window_data[window_id]
        self._coexist_containers.pop(window_id, None)

    def register_card(
        self,
        window_id: str,
        container_type: ContainerType,
        card_id: str,
        card_widget,
        system_card: bool = False,
        suppress_others: list = None,
    ):
        """注册卡片到管理器

        Args:
            window_id: 窗口标识
            container_type: 容器类型
            card_id: 卡片标识
            card_widget: 控件
            system_card: 是否为系统卡片（系统卡片窗口内互斥）
            suppress_others: 该卡片显示时需要压制的其他卡片 ID 列表
        """
        self._ensure_window_initialized(window_id)

        win_data = self._window_data[window_id]
        if container_type not in win_data["cards"]:
            win_data["cards"][container_type] = {}

        if card_id in win_data["containers"]:
            logger.warning(f"[CardManager] 窗口 {window_id} 的卡片 {card_id} 已注册，将被覆盖")

        win_data["cards"][container_type][card_id] = card_widget
        win_data["containers"][card_id] = container_type
        if system_card:
            win_data["system_cards"].add(card_id)

        # 处理压制关系：注册时记录该卡片会压制哪些其他卡片
        if suppress_others:
            win_data["suppress_others_map"][card_id] = set(suppress_others)
            for suppressed_id in suppress_others:
                win_data["suppressed_by_others"].add(suppressed_id)

    def unregister_card(self, card_id: str, window_id: str):
        """注销单张卡片（卡片销毁重建前调用）

        清理 cards/containers/system_cards/visible_cards/压制关系中的所有痕迹，
        使同名 card_id 可被重新 register_card 而不触发覆盖警告。
        """
        win_data = self._window_data.get(window_id)
        if win_data is None:
            return
        container_type = win_data["containers"].pop(card_id, None)
        if container_type is not None:
            win_data["cards"].get(container_type, {}).pop(card_id, None)
            if win_data["visible_cards"].get(container_type) == card_id:
                win_data["visible_cards"][container_type] = None
        win_data["system_cards"].discard(card_id)
        win_data["shown_callbacks"].pop(card_id, None)
        win_data["hidden_callbacks"].pop(card_id, None)
        suppressed = win_data["suppress_others_map"].pop(card_id, None)
        if suppressed:
            # 重算被压制集合（其他卡片可能仍压制相同目标）
            still_suppressed = set()
            for ids in win_data["suppress_others_map"].values():
                still_suppressed |= ids
            win_data["suppressed_by_others"] &= still_suppressed

    def show_card(self, card_id: str, window_id: str):
        """显示指定窗口的指定卡片"""
        if window_id not in self._window_data:
            return

        win_data = self._window_data[window_id]

        if card_id not in win_data["containers"]:
            return

        container_type = win_data["containers"][card_id]
        card_widget = win_data["cards"].get(container_type, {}).get(card_id)
        if card_widget is None:
            return

        # 多窗口隔离：检查 widget 是否已被删除
        if self._check_and_remove_deleted_card(window_id, card_id, container_type, card_widget):
            return

        # 如果卡片已经可见，不做任何事
        if win_data["visible_cards"].get(container_type) == card_id:
            return

        # ── 共存 / 停靠区卡片（LEFT/RIGHT/BOTTOM）：独立于系统卡片压制体系 ──
        # 仅同容器互斥，不受 question / 系统卡片 / 优先卡片影响
        # 与覆盖层（TOP）无互斥关系，四向区域可同时存在
        coexist_cts = self._coexist_containers.get(window_id, frozenset())
        if container_type in DOCK_CONTAINER_TYPES or container_type in coexist_cts:
            # Phase G：dock 容器多卡堆叠——可堆叠卡片追加到 dock_visible_cards 列表；
            # 非堆叠卡片走原互斥路径（清空列表 + visible_cards 单值）。
            stackable = self.is_card_stackable(card_id, window_id) if container_type in DOCK_CONTAINER_TYPES else False
            if container_type in DOCK_CONTAINER_TYPES and stackable:
                # 堆叠模式：追加到可见列表，active 指向本卡；不隐藏同容器其他卡
                dock_list = win_data["dock_visible_cards"].setdefault(container_type, [])
                if card_id not in dock_list:
                    dock_list.append(card_id)
                win_data["dock_active_cards"][container_type] = card_id
                # visible_cards 单值保留为 active（兼容 is_card_visible 旧调用）
                win_data["visible_cards"][container_type] = card_id
            else:
                # 非堆叠：原互斥路径（清空 dock 列表 + 单值）
                self._hide_same_container_cards(window_id, container_type, exclude_card_id=card_id)
                if container_type in DOCK_CONTAINER_TYPES:
                    win_data["dock_visible_cards"][container_type] = [card_id]
                    win_data["dock_active_cards"][container_type] = card_id
                win_data["visible_cards"][container_type] = card_id
            try:
                if hasattr(card_widget, "show_card"):
                    card_widget.show_card()
                else:
                    card_widget.setVisible(True)
            except RuntimeError:
                self._check_and_remove_deleted_card(window_id, card_id, container_type, card_widget)
                return
            for cb in win_data["shown_callbacks"].get(card_id, []):
                cb(card_id)
            return

        # ── Question 最高优先级：如果 question 已显示，其他非 question 卡片不能打断 ──
        if card_id != "question" and self.is_card_visible("question", window_id):
            logger.debug(f"[CardManager] question 已显示，跳过显示 {card_id}（question 强制覆盖所有）")
            return

        # ---- 优先卡片保护：流式对话中，除 question/system 外不打断优先卡片 ----
        # command/file_mention 卡片正在显示时，其他非优先非 question 卡片不应将其覆盖
        priority_cards = {"command", "file_mention"}
        if card_id not in priority_cards | {"question"}:
            for pc in priority_cards:
                if self.is_card_visible(pc, window_id):
                    if card_id not in win_data.get("system_cards", set()):
                        logger.debug(f"[CardManager] {pc} 卡片可见，跳过显示 {card_id}（仅 question/系统卡片可打断）")
                        return

        # 系统卡片：窗口内互斥（隐藏所有其他系统卡片）
        # 注意：覆盖层（TOP 系统卡片）打开时不关闭共存容器（LEFT/RIGHT/BOTTOM）
        # 的卡片，仅通过 QStackedWidget 视觉覆盖
        if card_id in win_data["system_cards"]:
            self._hide_system_cards(window_id, exclude_card_id=card_id, exclude_containers=coexist_cts)
            self._hide_same_container_cards(window_id, container_type, exclude_card_id=card_id)
            # 系统卡片激活时，隐藏所有可见的非系统卡片（跨容器），
            # 例如 BOTTOM 容器的 command/file_mention 应随 TOP 容器 settings 打开而关闭
            # 停靠区（LEFT/RIGHT）与共存容器（BOTTOM）豁免：
            # 不随系统卡片关闭非系统卡片
            for ct in ContainerType:
                if ct in DOCK_CONTAINER_TYPES or ct in coexist_cts:
                    continue
                vid = win_data["visible_cards"].get(ct)
                if vid and vid not in win_data["system_cards"]:
                    self.hide_card(vid, window_id)
            # 系统卡片激活，压制非系统卡片
            win_data["suppressed_by_system"] = True
        else:
            # 非系统卡片：检查是否被系统卡片压制（question 除外）
            if card_id not in {"question"} and win_data.get("suppressed_by_system", False):
                return

            # 非系统卡片：同容器互斥
            self._hide_same_container_cards(window_id, container_type, exclude_card_id=card_id)

            # 处理压制关系：该卡片压制其他卡片
            suppress_map = win_data.get("suppress_others_map", {})
            suppressed_ids = suppress_map.get(card_id, set())
            for suppressed_id in suppressed_ids:
                if self.is_card_visible(suppressed_id, window_id):
                    self.hide_card(suppressed_id, window_id)

            # 非系统卡片显示时，如果系统卡片可见则隐藏（让系统卡片优先变成互斥）
            # 但 Question 特殊：强制关闭所有
            if card_id in {"question"}:
                self._hide_all_cards(window_id)
                # question 激活时不压制其他卡片（它自己会处理）
                win_data["suppressed_by_system"] = False

        # 显示卡片
        try:
            # 调用卡片的 show_card 方法（由卡片自己管理计时器）
            if hasattr(card_widget, "show_card"):
                card_widget.show_card()
            else:
                card_widget.setVisible(True)
        except RuntimeError:
            # 竞态条件：检测后 widget 被删除了
            self._check_and_remove_deleted_card(window_id, card_id, container_type, card_widget)
            return

        win_data["visible_cards"][container_type] = card_id

        # 触发回调
        if card_id in win_data["shown_callbacks"]:
            for cb in win_data["shown_callbacks"][card_id]:
                cb(card_id)

        # Phase E：发布卡片显隐事件
        try:
            from app.core.ui_event_bus import EV_CARD_VISIBILITY_CHANGED, UIEventBus

            UIEventBus.get_instance().publish(
                EV_CARD_VISIBILITY_CHANGED,
                card_id=card_id,
                window_id=window_id,
                visible=True,
            )
        except Exception:
            pass

    def hide_card(self, card_id: str, window_id: str):
        """隐藏指定窗口的指定卡片"""
        if window_id not in self._window_data:
            return

        win_data = self._window_data[window_id]

        if card_id not in win_data["containers"]:
            return

        container_type = win_data["containers"][card_id]
        card_widget = win_data["cards"].get(container_type, {}).get(card_id)
        if card_widget is None:
            return

        # 多窗口隔离：检查 widget 是否已被删除
        if self._check_and_remove_deleted_card(window_id, card_id, container_type, card_widget):
            return

        if win_data["visible_cards"].get(container_type) != card_id:
            return

        try:
            if hasattr(card_widget, "hide_card"):
                card_widget.hide_card()
            else:
                card_widget.setVisible(False)
        except RuntimeError:
            self._check_and_remove_deleted_card(window_id, card_id, container_type, card_widget)
            return

        win_data["visible_cards"][container_type] = None
        # Phase G：dock 容器多卡——从可见列表移除；active 若指向本卡则指向列表尾
        if container_type in DOCK_CONTAINER_TYPES:
            dock_list = win_data.get("dock_visible_cards", {}).get(container_type, [])
            if card_id in dock_list:
                dock_list.remove(card_id)
            if win_data.get("dock_active_cards", {}).get(container_type) == card_id:
                win_data["dock_active_cards"][container_type] = dock_list[-1] if dock_list else None

        # 如果隐藏的是系统卡片，检查是否还有系统卡片可见，没有则解除压制
        if card_id in win_data["system_cards"]:
            has_visible_system = any(self.is_card_visible(sc_id, window_id) for sc_id in win_data["system_cards"])
            if not has_visible_system:
                win_data["suppressed_by_system"] = False

        # 触发回调
        if card_id in win_data["hidden_callbacks"]:
            for cb in win_data["hidden_callbacks"][card_id]:
                cb(card_id)

        # Phase E：发布卡片显隐事件
        try:
            from app.core.ui_event_bus import EV_CARD_VISIBILITY_CHANGED, UIEventBus

            UIEventBus.get_instance().publish(
                EV_CARD_VISIBILITY_CHANGED,
                card_id=card_id,
                window_id=window_id,
                visible=False,
            )
        except Exception:
            pass

    # ========== 兼容旧 API（使用默认窗口）==========
    # 这些方法保留用于向后兼容，但新代码应使用带 window_id 的版本

    def toggle_card(self, card_id: str, window_id: str = None):
        """切换卡片显示状态（兼容旧 API）"""
        if window_id is None:
            logger.warning("[CardManager] toggle_card 需要 window_id 参数")
            return
        if window_id not in self._window_data:
            return
        if self.is_card_visible(card_id, window_id):
            self.hide_card(card_id, window_id)
        else:
            self.show_card(card_id, window_id)

    def show_card_by_id(self, card_id: str, window_id: str):
        """显示卡片（兼容旧 API）"""
        self.show_card(card_id, window_id)

    def hide_card_by_id(self, card_id: str, window_id: str):
        """隐藏卡片（兼容旧 API）"""
        self.hide_card(card_id, window_id)

    # ========== 回调管理 ==========

    def on_card_shown(self, window_id: str, card_id: str, callback: Callable):
        if window_id not in self._window_data:
            return
        win_data = self._window_data[window_id]
        if card_id not in win_data["shown_callbacks"]:
            win_data["shown_callbacks"][card_id] = []
        win_data["shown_callbacks"][card_id].append(callback)

    def on_card_hidden(self, window_id: str, card_id: str, callback: Callable):
        if window_id not in self._window_data:
            return
        win_data = self._window_data[window_id]
        if card_id not in win_data["hidden_callbacks"]:
            win_data["hidden_callbacks"][card_id] = []
        win_data["hidden_callbacks"][card_id].append(callback)

    # ========== 内部辅助方法 ==========

    def _check_and_remove_deleted_card(
        self, window_id: str, card_id: str, container_type: ContainerType, card_widget
    ) -> bool:
        """检查 widget 是否已删除，已删除则从管理器移除"""
        try:
            _ = card_widget.windowTitle()
            return False
        except RuntimeError:
            logger.warning(f"[CardManager] 窗口 {window_id} 的卡片 {card_id} 已被删除，从管理器移除")
            win_data = self._window_data.get(window_id)
            if win_data:
                if container_type in win_data["cards"] and card_id in win_data["cards"][container_type]:
                    del win_data["cards"][container_type][card_id]
                if win_data["visible_cards"].get(container_type) == card_id:
                    win_data["visible_cards"][container_type] = None
            return True

    def _hide_system_cards(
        self, window_id: str, exclude_card_id: str = None, exclude_containers: "frozenset[ContainerType]" = None
    ):
        """隐藏窗口内所有系统卡片

        Args:
            exclude_card_id: 不隐藏的卡片 ID
            exclude_containers: 不隐藏这些容器中的系统卡片（如共存容器 LEFT/RIGHT/BOTTOM）
        """
        if window_id not in self._window_data:
            return
        win_data = self._window_data[window_id]
        for card_id in list(win_data["system_cards"]):
            if card_id == exclude_card_id:
                continue
            if exclude_containers is not None:
                ct = win_data["containers"].get(card_id)
                if ct in exclude_containers:
                    continue
            if self.is_card_visible(card_id, window_id):
                self.hide_card(card_id, window_id)

    def _hide_all_cards(self, window_id: str):
        """隐藏窗口内所有卡片（停靠区 LEFT/RIGHT 与共存容器豁免）"""
        if window_id not in self._window_data:
            return
        coexist_cts = self._coexist_containers.get(window_id, frozenset())
        for container_type in ContainerType:
            if container_type in DOCK_CONTAINER_TYPES or container_type in coexist_cts:
                continue
            self._hide_same_container_cards(window_id, container_type)

    def _hide_same_container_cards(self, window_id: str, container_type: ContainerType, exclude_card_id: str = None):
        """隐藏同容器的所有卡片"""
        if window_id not in self._window_data:
            return
        win_data = self._window_data[window_id]

        for card_id in list(win_data["cards"].get(container_type, {}).keys()):
            if card_id != exclude_card_id and win_data["visible_cards"].get(container_type) == card_id:
                card_widget = win_data["cards"][container_type][card_id]
                # 检查 widget 是否已被删除
                if self._check_and_remove_deleted_card(window_id, card_id, container_type, card_widget):
                    continue
                try:
                    if hasattr(card_widget, "hide_card"):
                        card_widget.hide_card()
                    else:
                        card_widget.setVisible(False)
                except RuntimeError:
                    self._check_and_remove_deleted_card(window_id, card_id, container_type, card_widget)
                    continue
                win_data["visible_cards"][container_type] = None
                if card_id in win_data["hidden_callbacks"]:
                    for cb in win_data["hidden_callbacks"][card_id]:
                        cb(card_id)

    def get_visible_card(self, container_type: ContainerType, window_id: str) -> Optional[str]:
        if window_id not in self._window_data:
            return None
        return self._window_data[window_id]["visible_cards"].get(container_type)

    def is_card_visible(self, card_id: str, window_id: str) -> bool:
        if window_id not in self._window_data:
            return False
        win_data = self._window_data[window_id]
        if card_id not in win_data["containers"]:
            return False
        container_type = win_data["containers"][card_id]
        return win_data["visible_cards"].get(container_type) == card_id

    # ── Phase G：dock 多卡堆叠 API ──

    def is_card_stackable(self, card_id: str, window_id: str) -> bool:
        """卡片是否声明停靠区堆叠（widget 属性 stackInDock 优先）"""
        win_data = self._window_data.get(window_id)
        if not win_data:
            return False
        ct = win_data["containers"].get(card_id)
        if ct is None or ct not in DOCK_CONTAINER_TYPES:
            return False
        widget = win_data["cards"].get(ct, {}).get(card_id)
        if widget is not None:
            try:
                val = widget.property("stackInDock")
                if val is True:
                    return True
            except Exception:
                pass
        return False

    def get_visible_cards(self, window_id: str, container_type: ContainerType) -> List[str]:
        """dock 容器可见卡列表（多卡堆叠）；非 dock 容器返回空列表"""
        win_data = self._window_data.get(window_id)
        if not win_data:
            return []
        if container_type not in DOCK_CONTAINER_TYPES:
            return []
        return list(win_data.get("dock_visible_cards", {}).get(container_type, []))

    def set_active_card(self, card_id: str, window_id: str) -> None:
        """切换栈顶卡（仅状态标记，不触发 show/hide 回调）"""
        win_data = self._window_data.get(window_id)
        if not win_data:
            return
        ct = win_data["containers"].get(card_id)
        if ct is None or ct not in DOCK_CONTAINER_TYPES:
            return
        dock_list = win_data.get("dock_visible_cards", {}).get(ct, [])
        if card_id not in dock_list:
            return
        win_data["dock_active_cards"][ct] = card_id
        win_data["visible_cards"][ct] = card_id

    def get_all_windows(self) -> List[str]:
        """获取所有已注册的窗口ID"""
        return list(self._window_data.keys())

    # ============================================================
    # 外部卡片注册（由 UI 插件调用）
    # ============================================================

    def register_external_card(
        self,
        window_id: str,
        card_id: str,
        widget_class: type,
        container: "ContainerType",
        default_visible: bool = False,
    ) -> None:
        """注册外部卡片（由 UI 插件调用）

        Args:
            window_id: 窗口 ID（多窗口隔离）
            card_id: 卡片唯一 ID
            widget_class: QWidget 子类
            container: 容器位置
            default_visible: 默认是否可见
        """
        if not hasattr(self, "_external_cards"):
            self._external_cards: Dict[str, Dict[str, dict]] = {}
        if window_id not in self._external_cards:
            self._external_cards[window_id] = {}
        self._external_cards[window_id][card_id] = {
            "widget_class": widget_class,
            "container": container,
            "default_visible": default_visible,
        }

    def unregister_external_card(self, window_id: str, card_id: str) -> None:
        """注销外部卡片"""
        if not hasattr(self, "_external_cards"):
            return
        cards = self._external_cards.get(window_id, {})
        cards.pop(card_id, None)

    def get_external_card(self, window_id: str, card_id: str) -> Optional[dict]:
        """获取外部卡片信息"""
        if not hasattr(self, "_external_cards"):
            return None
        return self._external_cards.get(window_id, {}).get(card_id)

    def list_external_cards(self, window_id: str) -> Dict[str, dict]:
        """列出窗口的所有外部卡片"""
        if not hasattr(self, "_external_cards"):
            return {}
        return dict(self._external_cards.get(window_id, {}))
