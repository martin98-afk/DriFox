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
from typing import Dict, List, Optional, Callable, Any
from loguru import logger


class ContainerType(Enum):
    TOP = "top"      # chatscroll 上方
    BOTTOM = "bottom"  # chatscroll 下方


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
    
    def _ensure_window_initialized(self, window_id: str):
        """确保窗口数据已初始化"""
        if window_id not in self._window_data:
            self._window_data[window_id] = {
                "cards": {
                    ContainerType.TOP: {},
                    ContainerType.BOTTOM: {},
                },
                "containers": {},  # card_id -> ContainerType
                "system_cards": set(),
                "visible_cards": {
                    ContainerType.TOP: None,
                    ContainerType.BOTTOM: None,
                },
                "shown_callbacks": {},
                "hidden_callbacks": {},
                "suppress_others_map": {},  # card_id -> set of suppressed card_ids
                "suppressed_by_others": set(),  # 被其他卡片压制的 card_id 集合
            }
    
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
    
    def register_card(self, window_id: str, container_type: ContainerType, card_id: str, card_widget, system_card: bool = False, suppress_others: list = None):
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
        if card_id in win_data["system_cards"]:
            self._hide_system_cards(window_id, exclude_card_id=card_id)
            self._hide_same_container_cards(window_id, container_type, exclude_card_id=card_id)
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
            if hasattr(card_widget, 'show_card'):
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
            if hasattr(card_widget, 'hide_card'):
                card_widget.hide_card()
            else:
                card_widget.setVisible(False)
        except RuntimeError:
            self._check_and_remove_deleted_card(window_id, card_id, container_type, card_widget)
            return
        
        win_data["visible_cards"][container_type] = None
        
        # 如果隐藏的是系统卡片，检查是否还有系统卡片可见，没有则解除压制
        if card_id in win_data["system_cards"]:
            has_visible_system = any(
                self.is_card_visible(sc_id, window_id)
                for sc_id in win_data["system_cards"]
            )
            if not has_visible_system:
                win_data["suppressed_by_system"] = False
        
        # 触发回调
        if card_id in win_data["hidden_callbacks"]:
            for cb in win_data["hidden_callbacks"][card_id]:
                cb(card_id)

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

    def _check_and_remove_deleted_card(self, window_id: str, card_id: str, container_type: ContainerType, card_widget) -> bool:
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

    def _hide_system_cards(self, window_id: str, exclude_card_id: str = None):
        """隐藏窗口内所有系统卡片"""
        if window_id not in self._window_data:
            return
        win_data = self._window_data[window_id]
        for card_id in list(win_data["system_cards"]):
            if card_id != exclude_card_id and self.is_card_visible(card_id, window_id):
                self.hide_card(card_id, window_id)

    def _hide_all_cards(self, window_id: str):
        """隐藏窗口内所有卡片"""
        if window_id not in self._window_data:
            return
        for container_type in ContainerType:
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
                    if hasattr(card_widget, 'hide_card'):
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
    
    def get_all_windows(self) -> List[str]:
        """获取所有已注册的窗口ID"""
        return list(self._window_data.keys())