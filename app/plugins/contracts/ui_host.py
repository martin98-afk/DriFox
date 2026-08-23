# -*- coding: utf-8 -*-
"""IWindowHost 协议 — UI 插件宿主窗口的显式契约（收敛鸭子属性耦合）

现状（附录 B3）：registry 靠 `getattr(obj, "_card_manager")` / `"_window_id"`
鸭子探测宿主，新宿主必须手抄私有属性名。本协议提供唯一显式入口：
宿主实现 `as_ui_host()` 返回自身；registry 优先走协议，鸭子属性仅兜底。
"""

from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

if TYPE_CHECKING:
    from app.widgets.cards.card_manager import CardManager


@runtime_checkable
class IWindowHost(Protocol):
    """UI 宿主窗口协议（Tab 窗口 / 独立窗口 / 测试 stub 均可实现）"""

    @property
    def window_id(self) -> str:
        """窗口唯一 ID（多窗口隔离键）"""
        ...

    @property
    def card_manager(self) -> "CardManager":
        """中央卡片管理器（四向容器注册/显隐/互斥）"""
        ...

    def as_ui_host(self) -> "IWindowHost":
        """自描述入口 — registry 探测宿主的优先路径（返回 self）"""
        ...


def is_ui_host(obj: Any) -> bool:
    """runtime 探测：对象是否实现 IWindowHost 协议"""
    if obj is None:
        return False
    return all(hasattr(obj, attr) for attr in ("window_id", "card_manager", "as_ui_host"))
