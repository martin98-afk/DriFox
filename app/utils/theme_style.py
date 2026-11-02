# -*- coding: utf-8 -*-
"""主题样式登记与重放 —— 插件 UI 主题色刷新的统一入口

## 问题

主题色以 ``Colors`` 类属性暴露（``Colors.TEXT_PRIMARY`` 等），而控件 QSS 通常是
构造期 f-string 求值：

    self._label.setStyleSheet(f"color: {Colors.TEXT_PRIMARY};")

字符串一旦写入控件就与 ``Colors`` 脱钩：主题切换后 ``Colors`` 已更新，控件 QSS
里的字面量仍是旧主题色 —— 除非有人重新 setStyleSheet。宿主对浮动卡片 / 工作台
插件页派发一次 ``refresh_style()``，插件若在该方法里漏掉某个控件（尤其是渲染期
动态创建、没进缓存表的控件，如空态 QLabel、分页按钮），该控件就永久停在旧主题色。

## 方案

不靠"记得在 refresh_style 里补哪几个控件"，而是把 **QSS 生成函数** 登记到控件
自身。主题切换时宿主遍历插件子树逐个重放 —— 控件在，样式就在，结构上杜绝漏刷。

    from app.utils.theme_style import bind_theme_qss

    bind_theme_qss(self._label, lambda c: f"color: {c.TEXT_PRIMARY};")

宿主侧无需插件配合：``UIPluginRegistry`` 主题派发时自动 ``replay_theme_qss``。
插件在自己的 ``refresh_style`` 里也可调 ``replay_theme_qss(self)`` 主动兜底。

## 边界

- 只重放**登记过**的控件；裸 ``setStyleSheet`` 的旧代码不受影响（也不会被修复）。
- 登记位是控件上的 Python 属性，随 C++ 对象销毁自然消失，无全局表、无泄漏。
- 工厂签名 ``(colors) -> str``；兼容无参 ``() -> str``（旧写法可直接搬进来）。
"""

import inspect
from typing import Any, Callable, Optional

from PyQt5.QtWidgets import QWidget
from loguru import logger

# 登记位：控件上的 Python 属性，值为 (factory, takes_colors)
_QSS_FACTORY_ATTR = "_drifox_theme_qss_factory"


def _accepts_colors(factory: Callable[..., str]) -> bool:
    """判断工厂是否接受至少一个位置参数（colors）

    在 bind 时解析一次，避免每次重放都做 signature 解析。
    """
    try:
        sig = inspect.signature(factory)
    except (TypeError, ValueError):
        return False
    for param in sig.parameters.values():
        if param.kind in (
            inspect.Parameter.POSITIONAL_ONLY,
            inspect.Parameter.POSITIONAL_OR_KEYWORD,
            inspect.Parameter.VAR_POSITIONAL,
        ):
            return True
    return False


def bind_theme_qss(widget: Any, factory: Callable[..., str]) -> Any:
    """登记控件的主题 QSS 工厂，并立即应用一次

    Args:
        widget: 目标控件（``QWidget`` 及其子类；``None`` 时安全忽略）
        factory: ``(colors) -> qss``，颜色一律从入参取；也兼容无参 ``() -> qss``

    Returns:
        widget 本身，便于链式书写
    """
    if widget is None or not callable(factory):
        return widget
    try:
        setattr(widget, _QSS_FACTORY_ATTR, (factory, _accepts_colors(factory)))
    except Exception:
        # 少数对象禁止设置属性（如设置了 __slots__ 的代理）→ 静默降级为普通样式
        return widget
    apply_theme_qss(widget)
    return widget


def unbind_theme_qss(widget: Any) -> None:
    """注销控件的主题 QSS 工厂（控件复用改作他用时调用）"""
    try:
        delattr(widget, _QSS_FACTORY_ATTR)
    except Exception:
        pass


def apply_theme_qss(widget: Any, *, ensure_colors: bool = True) -> bool:
    """重放单个控件已登记的主题 QSS 工厂

    Args:
        widget: 目标控件
        ensure_colors: 是否先 ``Colors.refresh()``（``replay_theme_qss`` 批量
            重放时只做一次，避免逐控件重复解析主题）

    Returns:
        True 表示确实重放了一个 QSS；未登记 / 工厂报错 / 对象已销毁均返回 False
    """
    entry = getattr(widget, _QSS_FACTORY_ATTR, None)
    if not entry:
        return False
    try:
        factory, takes_colors = entry
    except (TypeError, ValueError):
        return False

    try:
        from app.utils.design_tokens import Colors
    except Exception as e:
        logger.warning(f"[theme_style] design_tokens 导入失败: {e}")
        return False

    if ensure_colors:
        try:
            Colors.refresh()
        except Exception as e:
            logger.warning(f"[theme_style] Colors.refresh 失败: {e}")
            return False

    try:
        qss = factory(Colors) if takes_colors else factory()
    except Exception as e:
        logger.warning(f"[theme_style] 主题 QSS 工厂执行失败: {e}")
        return False
    if not qss:
        return False
    try:
        # 同串短路：Qt5 对完全相同的串也会重建 QStyleSheetStyle 并 repolish 整棵
        # 子树。主题派发会对插件卡全树 BFS 重放，其中大量控件的 QSS 并不含主题色
        # （固定几何/字号），同串重写纯属浪费；多标签时这条链随标签数线性增长。
        if qss == widget.styleSheet():
            return False
        widget.setStyleSheet(qss)
    except RuntimeError:
        return False  # C++ 对象已销毁
    return True


def replay_theme_qss(root: Any) -> int:
    """遍历 ``root`` 及其全部后代，重放已登记的主题 QSS

    宿主在主题派发时对插件 widget 树整体调用，可覆盖 ``refresh_style`` 没枚举到
    的动态控件。

    Args:
        root: 子树根控件（插件页面 / 浮动卡 / 任意容器）

    Returns:
        实际重放的控件数（0 表示该子树没有登记任何主题样式）
    """
    if root is None:
        return 0
    try:
        from app.utils.design_tokens import Colors

        Colors.refresh()
    except Exception as e:
        logger.warning(f"[theme_style] Colors.refresh 失败: {e}")

    count = 0
    stack = [root]
    while stack:
        obj = stack.pop()
        if obj is None:
            continue
        if apply_theme_qss(obj, ensure_colors=False):
            count += 1
        try:
            # [T20] children() 只取直接子级（O(N) 线性 BFS）：原 findChildren(QWidget)
            # 对每个节点都递归拉全部后代，同一 widget 被重复入栈/重复 apply，O(N²)。
            # children() 返回 QObject（含 QLayout/QAction 等），必须 isinstance 过滤。
            stack.extend(c for c in obj.children() if isinstance(c, QWidget))
        except (RuntimeError, AttributeError):
            continue  # C++ 对象已销毁 / 非 QWidget
    return count


# ── 外观纪元脏标记（隐藏期不刷、显示时补刷）──────────────────────
# 登记位：控件上的 Python 属性，值是「该控件最后一次刷到哪个外观纪元」。
# None / 缺失 = 从未刷过或已置脏。随 C++ 对象销毁自然消失，无全局表。
_THEME_EPOCH_ATTR = "_drifox_theme_epoch"
# 每个控件应调用的样式刷新方法名，按优先级探测（插件约定 refresh_style 优先；
# refresh_theme 排最后——它是宿主窗口的 ThemeManager 协议名，插件内容页历史上
# 也用过，放末尾兼容但不抢占插件自己的约定）
_STYLE_METHODS = ("refresh_style", "_apply_latest_theme", "_apply_theme", "_retheme", "refresh_theme")


def mark_theme_dirty(widget: Any) -> None:
    """标记控件外观已落后（隐藏期用）：不刷，只让纪元落空

    同时挂上「显示即补刷」事件过滤器——补刷不再依赖每个宿主记得在
    显示/切页/挂载点手动调 ``ensure_theme_fresh``。宿主该调的照调（更早，
    在布局完成前就换好色），这里只是兜底网。
    """
    if widget is None:
        return
    try:
        setattr(widget, _THEME_EPOCH_ATTR, None)
    except Exception:
        return
    try:
        widget.installEventFilter(_show_refresh_filter())
    except (RuntimeError, AttributeError, TypeError):
        pass


_SHOW_REFRESH_FILTER: Any = None


def _show_refresh_filter():
    """模块级单例事件过滤器：只在 Show 时对「纪元落后」的控件补刷

    为什么单例而不是每个控件建一个：PyQt 的 installEventFilter 不转移所有权，
    过滤器对象由本模块持有即可，控件销毁时 Qt 自动摘除，无需清理表。
    """
    global _SHOW_REFRESH_FILTER
    if _SHOW_REFRESH_FILTER is not None:
        return _SHOW_REFRESH_FILTER
    from PyQt5.QtCore import QEvent, QObject

    class _ShowRefreshFilter(QObject):
        def eventFilter(self, obj, event):  # noqa: N802 - Qt 命名
            if event.type() == QEvent.Show:
                try:
                    # 纪元落后才真刷；已是最新时只是一次属性比对
                    ensure_theme_fresh(obj)
                except RuntimeError:
                    pass  # C++ 对象已销毁
            return False  # 不拦截，事件照常投递

    _SHOW_REFRESH_FILTER = _ShowRefreshFilter()
    return _SHOW_REFRESH_FILTER


def _style_method(widget: Any) -> Optional[Callable[..., Any]]:
    """探测控件的样式刷新方法（PyQt5 已销毁 wrapper 上 getattr 也抛 RuntimeError）"""
    for name in _STYLE_METHODS:
        try:
            method = getattr(widget, name, None)
        except RuntimeError:
            return None  # C++ 对象已销毁
        if callable(method):
            return method
    return None


def ensure_theme_fresh(widget: Any) -> bool:
    """确保控件已刷到当前外观纪元（显示 / 切页 / 挂载补刷唯一入口）

    与 ``mark_theme_dirty`` 成对：主题切换时隐藏的页面只置脏，等真正可见时
    走这里补一次。这既避免隐藏页白刷（主题切换成本从「全部插件页」降到
    「可见插件页」），又保证打开时 100% 是新主题（不再依赖插件自己在
    ``show_card()`` 里记得刷）。

    Returns:
        True 表示本次确实执行了刷新；False 表示已是最新 / 无刷新方法 / 已销毁。
    """
    if widget is None:
        return False
    try:
        from app.utils.theme_refresh import ThemeRefreshCoordinator

        epoch = ThemeRefreshCoordinator.get_appearance_epoch()
    except Exception:
        return False
    try:
        if getattr(widget, _THEME_EPOCH_ATTR, None) == epoch:
            return False
    except RuntimeError:
        return False  # C++ 对象已销毁
    method = _style_method(widget)
    try:
        if callable(method):
            method()
        replay_theme_qss(widget)
    except RuntimeError:
        return False
    except Exception as e:
        logger.warning(f"[theme_style] 补刷失败 {type(widget).__name__}: {e}")
        return False
    try:
        setattr(widget, _THEME_EPOCH_ATTR, epoch)
    except Exception:
        pass
    return True


def stamp_theme_epoch(widget: Any) -> None:
    """把控件标记为「已是当前外观」（可见页面刷完后调用）"""
    if widget is None:
        return
    try:
        from app.utils.theme_refresh import ThemeRefreshCoordinator

        setattr(widget, _THEME_EPOCH_ATTR, ThemeRefreshCoordinator.get_appearance_epoch())
    except Exception:
        pass


class ThemeStyleBinder:
    """批量登记器（可选糖）：一次登记、一键重放

    适合"一组控件 + 各自工厂"的集中管理场景：

        self._styles = ThemeStyleBinder()
        self._styles.bind(self._search, lambda c: f"...{c.TEXT_PRIMARY}...")
        self._styles.bind(self._scroll, lambda c: f"...{c.SCROLLBAR_HANDLE_BG}...")

        def refresh_style(self):
            self._styles.refresh()   # 一行等价于逐个重设

    控件销毁后重放会自动跳过（``apply_theme_qss`` 吞 RuntimeError），
    无需在销毁路径里手动解绑。
    """

    def __init__(self) -> None:
        self._widgets: list[Any] = []

    def bind(self, widget: Any, factory: Callable[..., str]) -> Any:
        """登记并立即应用；返回 widget 便于链式书写"""
        if widget is None or not callable(factory):
            return widget
        bind_theme_qss(widget, factory)
        self._widgets.append(widget)
        return widget

    def unbind(self, widget: Any) -> None:
        """注销某个控件（同时从本批次移除）"""
        unbind_theme_qss(widget)
        self._widgets = [w for w in self._widgets if w is not widget]

    def refresh(self) -> int:
        """重放本批次全部控件；返回成功数量"""
        count = 0
        alive: list[Any] = []
        for widget in self._widgets:
            try:
                if apply_theme_qss(widget):
                    count += 1
                alive.append(widget)
            except RuntimeError:
                continue  # C++ 对象已销毁 → 顺带剔除，避免列表无限增长
        self._widgets = alive
        return count
