"""headless import 守卫（架构文档 §一：qfluentwidgets/webengine 进程禁运）。

三层防线，全部落在 server/ 侧（不改 app/ 既有文件）：
1. meta_path 拦截器：PyQt5.QtWidgets / PyQt5.QtGui 直接 import 即 ImportError
   （macOS 无窗口服务器时 UI import 链即崩，app/ 侧 except ImportError 分支天然兼容）
2. qfluentwidgets stub 父包：短路顶层 __init__ 的全量组件拉取，
   仅加载 common/config.py 真实现——Settings(QConfig) 所需 9 个配置类 + qconfig
   语义零漂移；未导出的组件名经 __getattr__ 拒绝并留痕
3. QtGui stub：仅 QColor 值对象（config.py 模块级 import 需要，无窗口依赖）

用 headless 服务中 Settings 的全部触达面来自 app/utils/config.py:26 的 9 个名字
（见 server/NOTES.md），组件类（SettingsCard 等）在服务中不存在调用方。
"""
import logging
import os
import sys
import types

logger = logging.getLogger("drifox.server.guard")

_BLOCKED_MODULES = ("PyQt5.QtWidgets", "PyQt5.QtGui")

# app/utils/config.py:26 从 qfluentwidgets 导入的名字全集 + 函数内 qconfig
_CFG_EXPORTS = (
    "BoolValidator",
    "ConfigItem",
    "ConfigSerializer",
    "ConfigValidator",
    "OptionsConfigItem",
    "OptionsValidator",
    "QConfig",
    "RangeConfigItem",
    "RangeValidator",
    "qconfig",
)


class _FakeColor:
    """QColor 最小替身：仅 config.py 的 ColorValidator/serializer 签名面。"""

    HexArgb = 2  # 常见格式枚举占位

    def __init__(self, *args):
        self._args = args

    def isValid(self) -> bool:
        return False

    def name(self, fmt=None) -> str:
        return "#00000000"


def _find_qfluentwidgets_dir():
    for p in sys.path:
        if not p:
            continue
        cand = os.path.join(p, "qfluentwidgets")
        if os.path.isdir(cand):
            return cand
    return None


def _install_qtgui_stub() -> None:
    """QtGui：QColor 有语义替身，其余名字动态占位类 + 留痕。

    🟡3 加固（R3 审计）：占位类构造与实例属性访问抛 **ImportError**（而非
    任由后续 AttributeError）——恢复 app/ 侧 `except ImportError` 降级分支
    的原语义：import 成功但首次使用即走降级，与模块缺失时行为一致。
    QtWidgets 不做占位（窗口类创建会真崩），由拦截器硬拒。
    """
    if "PyQt5.QtGui" in sys.modules:
        return
    stub = types.ModuleType("PyQt5.QtGui")
    stub.QColor = _FakeColor

    def _make_placeholder(cls_name: str):
        msg = f"DRIFOX_HEADLESS=1: PyQt5.QtGui.{cls_name} 在 headless 服务中不可用"

        def _blocked_init(self, *args, **kwargs):
            raise ImportError(msg)

        def _blocked_getattr(self, attr: str):
            raise ImportError(f"{msg}: 属性 {attr} 访问被占位类拦截")

        return type(cls_name, (), {"__init__": _blocked_init, "__getattr__": _blocked_getattr})

    def _getattr(name):
        if name.startswith("__"):
            raise AttributeError(name)
        placeholder = _make_placeholder(name)
        setattr(stub, name, placeholder)
        logger.warning("[qt-block] QtGui.%s 使用占位类（headless，首次使用抛 ImportError）", name)
        return placeholder

    stub.__getattr__ = _getattr
    sys.modules["PyQt5.QtGui"] = stub


def _install_qfluentwidgets_stub() -> None:
    """短路 qfluentwidgets 顶层 __init__，仅保留 common/config.py 真实现。"""
    if "qfluentwidgets" in sys.modules:
        return
    pkg_dir = _find_qfluentwidgets_dir()
    if pkg_dir is None:
        logger.warning("[qt-block] 未找到 qfluentwidgets，跳过 stub（Settings 将不可用）")
        return

    qw = types.ModuleType("qfluentwidgets")
    qw.__path__ = [pkg_dir]

    def _qw_getattr(name):
        if name.startswith("__"):
            raise AttributeError(name)
        raise ImportError(f"[qt-block] headless 禁运 qfluentwidgets.{name}（UI 组件）")

    qw.__getattr__ = _qw_getattr
    sys.modules["qfluentwidgets"] = qw

    # common 父包同样短路（其 __init__ 拉 animation/font 等 UI 依赖）
    common = types.ModuleType("qfluentwidgets.common")
    common.__path__ = [os.path.join(pkg_dir, "common")]
    sys.modules["qfluentwidgets.common"] = common

    # 真实现 config 模块（内部 import QtCore 正常，QtGui 走 stub QColor）
    # 用 import_module 走完整 bootstrap 流程，避免逐级 getattr 快速路径
    import importlib

    _cfg = importlib.import_module("qfluentwidgets.common.config")
    # 显式绑定中间包属性：CPython 对 import a.b.c 的快速路径逐级 getattr，
    # 不绑定会在 getattr(qw, "common") 处误触发组件拒绝
    qw.common = common
    common.config = _cfg

    missing = [n for n in _CFG_EXPORTS if not hasattr(_cfg, n)]
    if missing:
        logger.warning("[qt-block] config 模块缺少导出: %s", missing)
    for n in _CFG_EXPORTS:
        if hasattr(_cfg, n):
            setattr(qw, n, getattr(_cfg, n))


class _HeadlessImportGuard:
    """meta_path 钩子：PyQt5.QtWidgets/QtGui 直接 import 即拒。"""

    def find_module(self, fullname, path=None):  # 兼容旧调用约定
        return self.find_spec(fullname, path)

    def find_spec(self, fullname, path=None, target=None):
        if fullname in _BLOCKED_MODULES or fullname.startswith(
            tuple(f"{m}." for m in _BLOCKED_MODULES)
        ):
            logger.warning("[qt-block] 拦截 import: %s", fullname)
            raise ImportError(
                f"DRIFOX_HEADLESS=1: 模块 {fullname} 在 headless 服务中被禁运"
            )
        return None


def install_headless_guard() -> None:
    """必须在任何 app/ 模块 import 之前调用。"""
    _install_qtgui_stub()
    _install_qfluentwidgets_stub()
    if not any(isinstance(f, _HeadlessImportGuard) for f in sys.meta_path):
        sys.meta_path.insert(0, _HeadlessImportGuard())
    os.environ["DRIFOX_HEADLESS"] = "1"
    logger.warning(
        "[qt-block] headless guard 已安装：QtWidgets/QtGui 禁运 + "
        "qfluentwidgets config-only stub"
    )
