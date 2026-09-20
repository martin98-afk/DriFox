# -*- coding: utf-8 -*-
"""UI 驱动查询：按 selector 定位控件 / 输出浅层控件树（token 友好）。

selector 字段（均可选，组合为 AND）：
- objectName: QWidget.objectName() 精确匹配
- cls: 类名（含基类链，isinstance 语义）
- text: text()/title()/placeholderText() 等常用文本槽包含匹配
- role: 动态属性 ``role`` 精确匹配（MessageCard 等业务控件有此属性）
"""

from __future__ import annotations

from typing import Any, Optional

from PyQt5.QtWidgets import QApplication, QWidget

from .bus import guard, invoke


def _text_of(w: QWidget) -> str:
    """提取控件常用文本（多槽位尝试，取到即止）。"""
    for attr in ("text", "title", "placeholderText", "toPlainText", "currentText"):
        getter = getattr(w, attr, None)
        if callable(getter):
            try:
                value = getter()
                if isinstance(value, str):
                    return value
            except Exception:  # noqa: BLE001 — 单槽失败跳过
                pass
    return ""


def _matches(w: QWidget, selector: dict[str, Any]) -> bool:
    name = selector.get("objectName")
    if name is not None and w.objectName() != name:
        return False
    cls = selector.get("cls")
    if cls is not None:
        names = {c.__name__ for c in type(w).__mro__}
        if cls not in names:
            return False
    role = selector.get("role")
    if role is not None and w.property("role") != role:
        return False
    text = selector.get("text")
    if text is not None and text not in _text_of(w):
        return False
    return True


def _iter_descendants(root: QWidget):
    yield root
    for child in root.findChildren(QWidget):
        yield child


def find(selector: dict[str, Any], root: Optional[QWidget] = None) -> Optional[QWidget]:
    """按 selector 查找第一个匹配控件；root=None 时扫全部顶层 widgets。

    线程安全：非主线程调用自动投递主线程执行。
    """
    guard("query.find")

    def _run() -> Optional[QWidget]:
        pool: list[QWidget]
        if root is not None:
            pool = list(_iter_descendants(root))
        else:
            pool = list(QApplication.allWidgets())
        for w in pool:
            if _matches(w, selector):
                return w
        return None

    return invoke(_run)


def tree(root: Optional[QWidget] = None, depth: int = 2) -> Optional[dict[str, Any]]:
    """输出 root 的浅层控件树（默认深度 2），token 友好。

    节点字段：cls / objectName / text / children_count / children（仅 depth>1）。
    """
    guard("query.tree")

    def _run() -> Optional[dict[str, Any]]:
        target = root
        if target is None:
            widgets = [w for w in QApplication.allWidgets() if w.isWindow()]
            if not widgets:
                return None
            target = widgets[0]

        def build(w: QWidget, d: int) -> dict[str, Any]:
            children = [c for c in w.findChildren(QWidget) if c.parent() is w]
            node: dict[str, Any] = {
                "cls": type(w).__name__,
                "objectName": w.objectName(),
                "text": _text_of(w)[:60],
                "children_count": len(children),
            }
            if d > 1:
                node["children"] = [build(c, d - 1) for c in children[:12]]
            return node

        return build(target, max(1, depth))

    return invoke(_run)


__all__ = ["find", "tree"]
