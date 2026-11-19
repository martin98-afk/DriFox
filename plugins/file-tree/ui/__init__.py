# -*- coding: utf-8 -*-
"""file-tree UI 组件入口"""

import sys

from loguru import logger

# 本插件接管的是哪个工作台页（也是 register_workbench_tab 的 page_id）
_PAGE_ID = "file-tree"

# 宿主在 register_ui 里传进来的注册表实例。仅作能力面的兜底通路：
# 能力池瞬时不可用（declared 解析失败 / provider 未注册）时，插件自己的页
# 仍能取到，不让「预览打开」因为一条旁路能力失效而整个掉到系统默认程序。
_HOST_REGISTRY = None


def register_ui(registry):
    """注册 file-tree 的 UI 组件

    热重载兼容：
    清理 sys.modules 中残留的子模块缓存，确保 Python 重新从 .py 源文件编译，
    避免旧的 __pycache__/.pyc 导致 NameError 等异常。
    """
    global _HOST_REGISTRY
    _HOST_REGISTRY = registry
    # 清理旧子模块缓存（避免热重载时 Python 用旧 sys.modules 缓存）
    prefix = "ui_plugin_file_tree."
    stale = [k for k in sys.modules if k.startswith(prefix)]
    for k in stale:
        del sys.modules[k]

    from .cards import FileTreeCard

    # 注册工作台页签（右侧边栏常驻；自动注册对应命令 /file-tree）
    # order_hint=15 只决定页签排列位置（排在最后）；default_landing=True 才是
    # “初次打开工作台默认展示本页”的声明（消费点见 WorkbenchPanel.sync_plugin_pages）。
    registry.register_workbench_tab(
        plugin_name="file-tree",
        page_id=_PAGE_ID,
        label="文件树",
        widget_class=FileTreeCard,
        priority=10,
        metadata={"source": "system", "order_hint": 15, "default_landing": True},
    )
    # 文件打开接管项走能力池 file.open（授权按 manifest 声明裁剪，与宿主调用链
    # 同一口径）。能力不可用时只跳过接管，其余 UI 组件照常注册。
    try:
        from app.plugins.sdk import caps_for

        api = caps_for("file.open", plugin="file-tree", surface="ui")
        if api is None:
            logger.warning("[file-tree] file.open 能力不可用（未声明或未注册），文件打开接管未登记")
        elif api["register_opener"]("preview", _can_open, _open_file, priority=10):
            logger.info("[file-tree] 已注册本地文件打开接管项: preview")
        else:
            logger.warning("[file-tree] 文件打开接管项注册失败（能力调用返回 False）")
    except Exception as e:
        logger.warning(f"[file-tree] 文件打开接管项注册异常: {e}")
    logger.info("[file-tree] UI components registered")


def _can_open(path: str) -> bool:
    """能否在本插件预览（图片 / markdown / 文本类代码与配置）

    判定复用预览页的后缀白名单：能预览才接管，否则交系统默认程序。
    ⚠️ 判定表依赖 Qt（图片后缀来自 ``QImageReader.supportedImageFormats()``），
    只做主线程只读查询；宿主的接管链本身就在主线程调用。
    """
    from .preview import detect_kind

    try:
        return bool(detect_kind(path))
    except Exception:
        return False


def _workbench_page():
    """取文件树页实例：能力池 window.workbench 优先，宿主注册表兜底

    ⚠️ 能力面是首选通路（授权与卸载清理都归能力池）；但页面访问是插件
    自己的页，能力面瞬时不可用时不该让预览功能完全失效——register_ui 传入
    的注册表是宿主显式交给插件的对象，不是绕过授权的私有 import。
    """
    try:
        from app.plugins.sdk import caps_for

        workbench = caps_for("window.workbench", plugin="file-tree", surface="ui")
        if workbench is not None:
            page = workbench["page"](_PAGE_ID)
            if page is not None:
                return page
            logger.debug("[file-tree] window.workbench 未返回页面，尝试注册表兜底")
        else:
            logger.warning("[file-tree] window.workbench 能力不可用，走注册表兜底")
    except Exception as e:
        logger.warning(f"[file-tree] window.workbench 取页异常，走注册表兜底: {e}")
    registry = _HOST_REGISTRY
    opener = getattr(registry, "open_workbench_page", None) if registry is not None else None
    if not callable(opener):
        return None
    try:
        return opener(_PAGE_ID)
    except Exception as e:
        logger.warning(f"[file-tree] 注册表兜底取页失败: {e}")
        return None


def _open_file(path: str) -> bool:
    """把文件开在文件树页的预览区

    Returns:
        True 表示已展示；False 交给宿主继续降级（系统默认程序）。
        工作台不可用 / 页面没拿到 / 预览失败都返回 False，不静默失败。
    """
    try:
        page = _workbench_page()
        if page is None:
            logger.debug("[file-tree] 工作台页不可用，放弃接管打开")
            return False
        preview = getattr(page, "preview_path", None)
        if not callable(preview):
            return False
        return bool(preview(path))
    except Exception as e:
        logger.warning(f"[file-tree] 接管打开失败，降级系统程序: {e}")
        return False
