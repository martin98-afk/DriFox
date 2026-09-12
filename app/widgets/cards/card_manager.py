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
    """卡片容器类型（全项目唯一权威定义）

    ⚠️ 任何模块都必须从本处导入，不要再定义第二份同名枚举：Enum 成员按 `is`
    比较，两份枚举的同名成员互不相等，而 CardManager 内部用容器类型作 dict 键
    —— 混用时注册与查询会静默落在不同桶里（详见 app/widgets/cards/__init__.py）。
    """

    TOP = "top"  # chatscroll 上方
    BOTTOM = "bottom"  # chatscroll 下方
    # 输入补全浮层（命令卡片 / 文件提及卡片）——紧贴输入框上方、位于 BOTTOM 之下。
    # 与 BOTTOM 分离的理由：补全卡是"输入的延伸"（跟光标绑定），而 BOTTOM 里的
    # 是"系统状态"（子智能体/排队/撤销）与"系统模态"。同容器时两者争抢同一段
    # 高度预算，实测排队卡会被压在命令卡参数行上重叠；拆层后互不干扰。
    COMPLETION = "completion"
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
                # 分层通道模型元数据：card_id -> {
                #     "layer": 语义层标识（"completion"/"status"/"system"/"default"）,
                #     "stackable": 层内是否允许多卡共存,
                #     "visible_when": 状态谓词 Callable[[], bool] | None,
                #     "order_hint": 层内排序权重（小值靠前）,
                # }
                "card_meta": {},
                # 多卡共存可见集：ContainerType -> list[card_id]（按 order_hint 排序）
                # ★ 与 visible_cards（单值/栈顶）并存：栈顶写单值兼容旧调用，
                #   完整可见集写这里，避免"同层非栈顶卡无法被 is_card_visible 识别"。
                "multi_visible": {},
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

    def register_window(self, window_id: str):
        """注册窗口到管理器（窗口创建时调用）"""
        self._ensure_window_initialized(window_id)

    def unregister_window(self, window_id: str):
        """注销窗口及其所有卡片数据（窗口关闭时调用）"""
        # ★ 泄漏修复（P1-E）：先 pop 出窗口数据，再显式 deleteLater 仍存活的
        # 卡片 widget，释放 C++ 对象树——否则卡片 widget 易被全局单例 / 回调
        # 残留引用长期持有，反复开关窗口时对象树堆积。
        win_data = self._window_data.pop(window_id, None)
        if win_data is not None:
            for _ct_cards in win_data.get("cards", {}).values():
                for _card_widget in _ct_cards.values():
                    try:
                        if _card_widget is not None:
                            _card_widget.deleteLater()
                    except (RuntimeError, TypeError):
                        pass
        self._coexist_containers.pop(window_id, None)

    def register_card(
        self,
        window_id: str,
        container_type: ContainerType,
        card_id: str,
        card_widget,
        system_card: bool = False,
        suppress_others: list = None,
        layer: str = "default",
        stackable: bool = False,
        visible_when: Callable[[], bool] = None,
        order_hint: int = 100,
    ):
        """注册卡片到管理器

        Args:
            window_id: 窗口标识
            container_type: 容器类型
            card_id: 卡片标识
            card_widget: 控件
            system_card: 是否为系统卡片（系统卡片窗口内互斥）
            suppress_others: 该卡片显示时需要压制的其他卡片 ID 列表
            layer: 语义层标识（"completion"/"status"/"system"/"default"）。
                同层卡片由 refresh_layer() 统一重算可见集。
            stackable: 层内是否允许多卡共存。为 True 时该卡
                ① 豁免同容器互斥（不被普通卡片挤掉）、
                ② 豁免其他卡片的 suppress_others 压制、
                ③ 显隐改由 visible_when 谓词驱动（见 refresh_layer）。
            visible_when: 状态谓词，返回该卡是否"应该可见"。为 None 表示
                仅由显式 show_card/hide_card 驱动，不参与谓词重算。
            order_hint: 层内排序权重，小值靠前
        """
        self._ensure_window_initialized(window_id)

        win_data = self._window_data[window_id]
        if container_type not in win_data["cards"]:
            win_data["cards"][container_type] = {}

        if card_id in win_data["containers"]:
            logger.warning(f"[CardManager] 窗口 {window_id} 的卡片 {card_id} 已注册，将被覆盖")

        win_data["cards"][container_type][card_id] = card_widget
        win_data["containers"][card_id] = container_type
        win_data["card_meta"][card_id] = {
            "layer": layer,
            "stackable": bool(stackable),
            "visible_when": visible_when,
            "order_hint": order_hint,
        }
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
        win_data["card_meta"].pop(card_id, None)
        for _ct, _mv in win_data.get("multi_visible", {}).items():
            if card_id in _mv:
                _mv.remove(card_id)
                if win_data["visible_cards"].get(_ct) == card_id:
                    win_data["visible_cards"][_ct] = _mv[0] if _mv else None
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

        # ★ L2 状态层：可堆叠卡片走"层可见集重算"路径
        # 不与同层其他卡互斥（子智能体运行中打 / 不再吞掉状态卡），
        # 且必须在"已可见"早退之前判定 —— 否则栈顶卡可见时会跳过整层重算。
        if self._is_declared_stackable(card_id, window_id):
            self.refresh_layer(window_id, win_data["card_meta"][card_id]["layer"])
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

        # ---- 输入补全卡（command/file_mention）的保护已上移到容器结构 ----
        # 旧实现把 card_id 字面量 {"command", "file_mention"} 硬编码在本类里，用于
        # "补全卡可见时不许其他卡覆盖"。L1 拆层后补全卡独占 ContainerType.COMPLETION，
        # 与其他卡的互斥由容器天然保证（它们根本不在同一个桶里），该硬编码已无必要
        # ——留在 Manager 里等于让主程序承载业务语义，与本层职责相悖，故删除。

        # 系统卡片：窗口内互斥（隐藏所有其他系统卡片）
        # 注意：覆盖层（TOP 系统卡片）打开时不关闭共存容器（LEFT/RIGHT/BOTTOM）
        # 的卡片，仅通过 QStackedWidget 视觉覆盖
        if card_id in win_data["system_cards"]:
            self._hide_system_cards(window_id, exclude_card_id=card_id, exclude_containers=coexist_cts)
            # 系统模态层覆盖：连 L2 状态层一并压制（关闭时由宿主 refresh_layer 恢复）
            self._hide_same_container_cards(window_id, container_type, exclude_card_id=card_id, exempt_stackable=False)
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
            # ★ L2 状态层豁免：状态卡表达的是"系统正在发生的事"（排队/撤销/子智能体），
            #   与输入补全卡（command/file_mention）语义正交，不应被后者压掉——
            #   旧实现里这正是"子智能体运行中打 / 后状态卡再也不回来"的根因。
            suppress_map = win_data.get("suppress_others_map", {})
            suppressed_ids = suppress_map.get(card_id, set())
            for suppressed_id in suppressed_ids:
                if self._is_declared_stackable(suppressed_id, window_id):
                    continue
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

        # ★ 多卡共存可见集：非栈顶卡不在 visible_cards 单值里，需按列表单独摘除
        multi = win_data.get("multi_visible", {}).get(container_type)
        if multi and card_id in multi:
            self._set_card_visible_raw(card_id, window_id, False)
            multi.remove(card_id)
            win_data["visible_cards"][container_type] = multi[0] if multi else None
            if card_id in win_data["system_cards"]:
                if not any(self.is_card_visible(sc, window_id) for sc in win_data["system_cards"]):
                    win_data["suppressed_by_system"] = False
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
        """隐藏窗口内所有卡片（停靠区 LEFT/RIGHT 与共存容器豁免）

        Question 强制覆盖路径：L2 状态层一并压制（exempt_stackable=False），
        关闭后由宿主 refresh_layer 按谓词恢复。
        """
        if window_id not in self._window_data:
            return
        coexist_cts = self._coexist_containers.get(window_id, frozenset())
        for container_type in ContainerType:
            if container_type in DOCK_CONTAINER_TYPES or container_type in coexist_cts:
                continue
            self._hide_same_container_cards(window_id, container_type, exempt_stackable=False)

    def _hide_same_container_cards(
        self,
        window_id: str,
        container_type: ContainerType,
        exclude_card_id: str = None,
        exempt_stackable: bool = True,
    ):
        """隐藏同容器的所有卡片

        Args:
            exclude_card_id: 不隐藏的卡片 ID
            exempt_stackable: 是否豁免 L2 可堆叠状态卡。
                普通卡片显示引起的同容器互斥应为 True（状态层独立共存）；
                系统模态卡 / question 的强制覆盖应为 False（压制一切）。
        """
        if window_id not in self._window_data:
            return
        win_data = self._window_data[window_id]

        # ★ 多卡共存可见集：仅在"强制覆盖"路径（exempt_stackable=False ——
        #   系统模态卡 / question）才逐卡压制；普通卡片显示时 L2 状态层保持共存，
        #   不受同容器互斥波及（这正是"打 / 吞掉子智能体卡"的修复点）。
        if not exempt_stackable:
            multi = win_data.get("multi_visible", {}).get(container_type)
            if multi:
                for cid in list(multi):
                    if cid == exclude_card_id:
                        continue
                    self._set_card_visible_raw(cid, window_id, False)
                if exclude_card_id in multi:
                    win_data["multi_visible"][container_type] = [exclude_card_id]
                    win_data["visible_cards"][container_type] = exclude_card_id
                else:
                    win_data["multi_visible"][container_type] = []
                    if win_data["visible_cards"].get(container_type) in multi:
                        win_data["visible_cards"][container_type] = None

        for card_id in list(win_data["cards"].get(container_type, {}).keys()):
            if card_id == exclude_card_id:
                continue
            if exempt_stackable and self._is_declared_stackable(card_id, window_id):
                continue
            if win_data["visible_cards"].get(container_type) == card_id:
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

    def is_card_visible(self, card_id: str, window_id: str) -> bool:
        if window_id not in self._window_data:
            return False
        win_data = self._window_data[window_id]
        if card_id not in win_data["containers"]:
            return False
        container_type = win_data["containers"][card_id]
        # ★ 多卡共存容器：L2 卡在完整可见集里；普通卡仍走栈顶单值 ——
        #   两者都在同一容器中真实可见，故任一路命中即为可见。
        multi = win_data.get("multi_visible", {}).get(container_type)
        if multi and card_id in multi:
            return True
        return win_data["visible_cards"].get(container_type) == card_id

    # ── 分层通道：多卡共存可见集（L2 状态层）──

    def _is_declared_stackable(self, card_id: str, window_id: str) -> bool:
        """卡片是否在注册时声明了层内堆叠（L2 状态层）

        与 is_card_stackable 的区别：后者是停靠区（LEFT/RIGHT）分流语义，
        本方法服务于分层通道模型，不限容器类型。
        """
        win_data = self._window_data.get(window_id)
        if not win_data:
            return False
        return bool(win_data.get("card_meta", {}).get(card_id, {}).get("stackable"))

    def refresh_layer(self, window_id: str, layer: str) -> None:
        """重算某层可见集：内容 = 谓词为真的卡片，按 order_hint 排序

        这是分层通道显隐的唯一真源 —— 调用方只更新数据（队列/回退栈/任务表）
        后调一次本方法，不再手写 show_card/hide_card 组合。

        从机制上消灭"卡片被挤掉后没人负责恢复"：谓词为真 → 必然回到可见集，
        不需要任何调用方"记得把卡片显示回来"。
        """
        win_data = self._window_data.get(window_id)
        if not win_data:
            return
        meta = win_data.get("card_meta", {})
        members = [
            cid for cid, m in meta.items() if m["layer"] == layer and m["stackable"] and cid in win_data["containers"]
        ]
        if not members:
            return
        container_type = win_data["containers"][members[0]]
        current = list(win_data.get("multi_visible", {}).get(container_type, []))

        desired: List[str] = []
        for cid in members:
            pred = meta[cid].get("visible_when")
            if pred is None:
                # 无谓词：仅由显式 show/hide 驱动，保持当前可见性
                if cid in current:
                    desired.append(cid)
                continue
            try:
                if pred():
                    desired.append(cid)
            except Exception as e:
                # 谓词可能持有已销毁 widget 的引用（窗口关闭竞态）
                logger.debug(f"[CardManager] 层 {layer} 卡片 {cid} 谓词求值失败，按不可见处理: {e}")

        desired.sort(key=lambda cid: meta[cid]["order_hint"])
        self._apply_visible_set(window_id, container_type, desired)

    def _apply_visible_set(self, window_id: str, container_type: ContainerType, desired: List[str]) -> None:
        """按目标可见列表差分应用：show 新增 / hide 移除 / 重排布局顺序"""
        win_data = self._window_data.get(window_id)
        if not win_data:
            return
        multi = win_data.setdefault("multi_visible", {}).setdefault(container_type, [])
        current = list(multi)
        for cid in current:
            if cid not in desired:
                self._set_card_visible_raw(cid, window_id, False)
        for cid in desired:
            if cid not in current:
                self._set_card_visible_raw(cid, window_id, True)
        win_data["multi_visible"][container_type] = list(desired)
        # 栈顶单值仅作旧调用兼容（is_card_visible 已优先查完整可见集）
        win_data["visible_cards"][container_type] = desired[0] if desired else None
        self._reorder_in_layout(window_id, container_type, desired)

    def _set_card_visible_raw(self, card_id: str, window_id: str, visible: bool) -> None:
        """底层显隐（不做互斥/压制决策）：驱动 widget + 回调 + 显隐事件"""
        win_data = self._window_data.get(window_id)
        if not win_data:
            return
        container_type = win_data["containers"].get(card_id)
        if container_type is None:
            return
        card_widget = win_data["cards"].get(container_type, {}).get(card_id)
        if card_widget is None:
            return
        if self._check_and_remove_deleted_card(window_id, card_id, container_type, card_widget):
            return
        try:
            if visible:
                if hasattr(card_widget, "show_card"):
                    card_widget.show_card()
                else:
                    card_widget.setVisible(True)
            elif hasattr(card_widget, "hide_card"):
                card_widget.hide_card()
            else:
                card_widget.setVisible(False)
        except RuntimeError:
            self._check_and_remove_deleted_card(window_id, card_id, container_type, card_widget)
            return
        for cb in win_data["shown_callbacks" if visible else "hidden_callbacks"].get(card_id, []):
            cb(card_id)
        self._publish_card_visibility(card_id, window_id, visible)

    @staticmethod
    def _publish_card_visibility(card_id: str, window_id: str, visible: bool) -> None:
        """发布卡片显隐事件（Phase E）"""
        try:
            from app.core.ui_event_bus import EV_CARD_VISIBILITY_CHANGED, UIEventBus

            UIEventBus.get_instance().publish(
                EV_CARD_VISIBILITY_CHANGED, card_id=card_id, window_id=window_id, visible=visible
            )
        except Exception:
            pass

    def _reorder_in_layout(self, window_id: str, container_type: ContainerType, ordered_ids: List[str]) -> None:
        """按 order_hint 顺序重排容器布局内的可见卡（仅调下标，不改 widget 归属）

        CardContainer.add_card 是注册期一次性 addWidget，布局顺序 = 注册顺序，
        与 order_hint 不一定一致；这里在每次可见集变化后校正。
        任何异常都静默放弃 —— 重排失败不应阻断显隐本身。
        """
        win_data = self._window_data.get(window_id)
        if not win_data or len(ordered_ids) < 2:
            return
        cards = win_data["cards"].get(container_type, {})
        widgets = [cards.get(cid) for cid in ordered_ids]
        widgets = [w for w in widgets if w is not None]
        if len(widgets) < 2:
            return
        try:
            container = widgets[0].parentWidget()
            layout = container.layout() if container is not None else None
            if layout is None:
                return
            indices = [
                i
                for i in range(layout.count())
                if layout.itemAt(i) is not None and layout.itemAt(i).widget() in widgets
            ]
            if len(indices) < 2:
                return
            base = min(indices)
            if [layout.itemAt(i).widget() for i in sorted(indices)] == widgets:
                return
            for w in widgets:
                layout.removeWidget(w)
            for idx, w in enumerate(widgets):
                layout.insertWidget(base + idx, w)
        except (RuntimeError, AttributeError, TypeError):
            pass

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

