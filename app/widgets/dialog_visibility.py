"""弹窗显隐广播总线 —— 替代 QApplication 级事件过滤器。

为什么不用 app 级 eventFilter（mac 闪退根因）：
PySide6/shiboken 在 `QObjectWrapper::sbk_o_eventFilter` 里会把事件接收者无条件包装成
Python 对象（`PySide::getWrapperForQObject`）。当接收者正处于析构过程中
（`QObject::d_ptr` 已被置空）时，该包装直接读地址 0x8 → SIGSEGV。崩溃发生在进入
Python 回调之前，Python 侧加 `shiboken6.isValid` 判活根本执行不到，无法防御；
PyQt5/sip 在同样场景抛的是可捕获的 RuntimeError，所以迁移到 PySide6 后潜伏的
生命周期问题才升级成硬崩。

本模块的做法：给已知弹窗类的 `showEvent`/`hideEvent` 各包一层广播（一次性 patch 基类，
其全部子类含插件自动生效），消费方（message_card 的 viewer 注册表）只收显/隐通知，
不再让 Python 代码进入 app 级事件分发链。
"""

import contextlib

from loguru import logger

# 广播安装标记（设在类上，随继承可见，避免子类重复包装导致一次显隐广播两遍）
_PATCH_FLAG = "_drifox_visibility_patched"

# 需要广播显隐的弹窗类。都是"会盖在内容之上、或需要压住 WebView"的一类：
# - MaskDialogBase：半透明遮罩对话框，必须让 WebView 让位（原生层穿透遮罩）
# - Flyout / InfoBar：贴附式弹层，与 WebView 争 z-order
# QMenu / QComboBox 下拉 / QToolTip 是 Qt.Popup|Qt.ToolTip 独立原生顶层窗口，
# 天然绘制在 Qt 合成内容之上，WebView 盖不住它们，无需广播。
_TARGET_MODULES = (
    ("qfluentwidgets.components.dialog_box.mask_dialog_base", "MaskDialogBase"),
    ("qfluentwidgets.components.widgets.flyout", "Flyout"),
    ("qfluentwidgets.components.widgets.flyout", "FlyoutViewBase"),
    ("qfluentwidgets.components.widgets.info_bar", "InfoBar"),
)

_listeners: list = []  # [(on_shown, on_hidden)]


def add_listener(on_shown, on_hidden) -> None:
    """登记显隐回调（各自 callable(widget)）。重复登记不去重，由调用方保证只装一次。"""
    _listeners.append((on_shown, on_hidden))


def install() -> bool:
    """给目标弹窗类装上显隐广播。幂等：重复调用不会二次包装。

    返回是否至少包装成功了一个类。qfluentwidgets 缺失/结构变更时返回 False，
    仅影响 WebView 让位这一项体验，不影响启动。
    """
    patched = 0
    for module_name, class_name in _TARGET_MODULES:
        cls = _resolve(module_name, class_name)
        if cls is None:
            continue
        if getattr(cls, _PATCH_FLAG, False):
            continue
        if _patch(cls):
            patched += 1
    if patched == 0:
        logger.warning("[DialogVisibility] 未能包装任何弹窗类，WebView 让位逻辑不生效")
    return patched > 0


def _resolve(module_name: str, class_name: str):
    try:
        import importlib

        return getattr(importlib.import_module(module_name), class_name, None)
    except Exception as e:  # pragma: no cover - 依赖缺失/改版时降级
        logger.debug(f"[DialogVisibility] 解析 {module_name}.{class_name} 失败: {e}")
        return None


def _patch(cls) -> bool:
    orig_show = getattr(cls, "showEvent", None)
    orig_hide = getattr(cls, "hideEvent", None)
    if orig_show is None or orig_hide is None:
        return False

    def showEvent(self, event):
        orig_show(self, event)
        _emit(_SHOWN, self)

    def hideEvent(self, event):
        orig_hide(self, event)
        _emit(_HIDDEN, self)

    cls.showEvent = showEvent
    cls.hideEvent = hideEvent
    setattr(cls, _PATCH_FLAG, True)
    return True


_SHOWN, _HIDDEN = "shown", "hidden"


def _emit(kind: str, widget) -> None:
    """广播一个显隐事件。任一监听者异常都不允许影响弹窗自身的事件处理。"""
    for on_shown, on_hidden in tuple(_listeners):
        cb = on_shown if kind == _SHOWN else on_hidden
        with contextlib.suppress(Exception):
            cb(widget)
