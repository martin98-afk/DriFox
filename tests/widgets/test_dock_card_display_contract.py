# -*- coding: utf-8 -*-
"""回归测试：停靠区浮动卡 / 工作台插件页的显示契约

== 背景（de49617c 交换位置回归）==
「历史会话」从右侧工作台页签迁为对话区左侧停靠区浮动卡（``container="left"``），
「文件树」反向迁为工作台页签。三处契约随后被破坏，本测试静态盯住：

1. **历史会话打不开**：``HistoryPage.show_card()`` 只调 ``refresh()``，缺
   ``setVisible(True)``。``CardContainer.add_card`` 挂载时显式
   ``card_widget.setVisible(False)``，容器靠 ``not isHidden()`` 判定有无可见
   卡片才展开；``CardManager.show_card`` 只在卡片**没有** ``show_card`` 方法时
   才兜底 ``setVisible(True)``。本页有该方法 → 显示动作只能由自己完成，缺失即
   「点侧栏按钮无任何可见反馈」。
2. **文件树卡在「正在加载文件树...」**：``FileTreeCard.showEvent`` 被定义两次，
   后定义（浮动卡时代的窗口尺寸跟随）覆盖前定义（工作台页首次显示 → show_card）
   → 首次显示从不加载，``_project_root`` 也从未由 ``_apply_latest_theme`` 赋值，
   点刷新只报「项目目录不存在，请先设置工作目录」。
3. **宿主入口死链**：会话历史入口仍按 ``page_id="history-manager"`` 定位工作台
   页签，而该页签已不存在 → ``set_current_tab_by_id`` 恒返回 False（静默无反应）。

== 测试策略 ==
纯 AST 静态检查：本机无头环境实例化 Qt 控件 / 加载插件包会原生崩溃
（QThread、QWebEngine 相关），故只锁定源码结构契约。
"""

import ast
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]


def _parse(rel_path: str) -> ast.Module:
    path = _REPO_ROOT / rel_path
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def _get_class(tree: ast.Module, class_name: str) -> ast.ClassDef:
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == class_name:
            return node
    raise AssertionError(f"未找到类 {class_name}")


def _method_names(cls: ast.ClassDef) -> list:
    """类体内**实际生效**的方法名（同名后者覆盖前者，故按出现顺序全列）"""
    return [n.name for n in cls.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]


def _get_method(cls: ast.ClassDef, name: str) -> ast.FunctionDef:
    for node in cls.body:
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(f"未找到方法 {cls.name}.{name}")


def _find_calls(node: ast.AST, name: str) -> list:
    """收集体内所有 ``<name>(...)`` 调用（兼容 self.xxx / 模块级 xxx）"""
    calls = []
    for sub in ast.walk(node):
        if not isinstance(sub, ast.Call):
            continue
        func = sub.func
        if isinstance(func, ast.Attribute) and func.attr == name:
            calls.append(sub)
        elif isinstance(func, ast.Name) and func.id == name:
            calls.append(sub)
    return calls


# ═══════════════════════════════════════════════════════════════
# 1. 浮动卡 show_card 契约（历史会话打不开）
# ═══════════════════════════════════════════════════════════════


class TestFloatingCardShowContract:
    """停靠区浮动卡的 ``show_card()`` 必须自己把卡片显示出来"""

    def test_history_page_show_card_calls_set_visible_true(self):
        tree = _parse("plugins/history-manager/ui/history_page.py")
        cls = _get_class(tree, "HistoryPage")
        method = _get_method(cls, "show_card")

        calls = _find_calls(method, "setVisible")
        assert calls, (
            "HistoryPage.show_card 必须调用 self.setVisible(True)："
            "CardContainer.add_card 挂载时显式 setVisible(False)，"
            "CardManager.show_card 对本卡不兜底 setVisible（卡片自带 show_card 方法），"
            "缺失即「历史会话点不开」。"
        )
        assert any(isinstance(c.args[0], ast.Constant) and c.args[0].value is True for c in calls if c.args), (
            "setVisible 的入参必须是 True（False 会继续隐藏卡片）"
        )

    def test_history_page_show_card_refreshes(self):
        tree = _parse("plugins/history-manager/ui/history_page.py")
        cls = _get_class(tree, "HistoryPage")
        method = _get_method(cls, "show_card")
        assert _find_calls(method, "refresh"), "HistoryPage.show_card 需在显示时刷新列表数据"


# ═══════════════════════════════════════════════════════════════
# 2. 工作台插件页 showEvent 契约（文件树卡在加载中）
# ═══════════════════════════════════════════════════════════════


class TestWorkbenchPageShowEventContract:
    """插件页只允许一个 showEvent，且首次显示必须触发数据加载"""

    _FILE_TREE_UI = "plugins/file-tree/ui/cards.py"

    def test_file_tree_card_show_event_defined_once(self):
        tree = _parse(self._FILE_TREE_UI)
        cls = _get_class(tree, "FileTreeCard")
        names = _method_names(cls)
        assert names.count("showEvent") == 1, (
            "FileTreeCard.showEvent 被定义了 %d 次：Python 类体内后定义覆盖先定义，"
            "早先的「首次显示 → show_card()」会被末尾的窗口尺寸跟随实现吃掉，"
            "页面永远停在「正在加载文件树...」占位。" % names.count("showEvent")
        )

    def test_file_tree_card_show_event_triggers_card_show(self):
        tree = _parse(self._FILE_TREE_UI)
        cls = _get_class(tree, "FileTreeCard")
        method = _get_method(cls, "showEvent")
        assert _find_calls(method, "show_card"), (
            "FileTreeCard.showEvent 必须调用 self.show_card()（首次显示应用主题 + 懒加载目录树），"
            "否则 _project_root 恒为空，点刷新只报「项目目录不存在，请先设置工作目录」。"
        )


# ═══════════════════════════════════════════════════════════════
# 3. 工作台残缺 context 自愈判据（按值而非按键）
# ═══════════════════════════════════════════════════════════════


class TestWorkbenchContextHeal:
    """``_build_ui_context()`` 恒含 backend 键，判据必须看值是否有效"""

    def test_incomplete_checks_backend_value(self):
        tree = _parse("app/widgets/workbench_panel.py")
        cls = _get_class(tree, "WorkbenchPanel")
        method = _get_method(cls, "_page_context_incomplete")

        # 方法内必须出现对 ctx 的 .get("backend") 取值（而非 "backend" in ctx）
        has_value_check = False
        for sub in ast.walk(method):
            if not isinstance(sub, ast.Call):
                continue
            func = sub.func
            if not isinstance(func, ast.Attribute) or func.attr != "get":
                continue
            if any(isinstance(a, ast.Constant) and a.value == "backend" for a in sub.args):
                has_value_check = True
        assert has_value_check, (
            '_page_context_incomplete 需按值判定 backend（ctx.get("backend")）：'
            "启动早期 backend 未就绪时该键存在但值为 None，只查键会把坏页误判为完整，"
            "自愈不再触发 → 文件树/工作树页拿不到工作目录。"
        )


# ═══════════════════════════════════════════════════════════════
# 4. 会话历史入口（活链，不能指向已卸载的工作台页签）
# ═══════════════════════════════════════════════════════════════


class TestHistoryEntrypoints:
    """历史会话入口必须走浮动卡通道，不得再按 page_id 定位工作台页签"""

    def test_open_workbench_history_uses_floating_card(self):
        tree = _parse("app/widgets/tab_manager_window.py")
        cls = _get_class(tree, "TabManagerWindow")
        method = _get_method(cls, "open_workbench_history")

        assert _find_calls(method, "toggle_floating_card"), (
            "TabManagerWindow.open_workbench_history 必须走 toggle_floating_card"
            '（历史会话已迁左侧停靠区浮动卡 card_id="history-manager"）。'
        )
        assert not _find_calls(method, "set_current_tab_by_id"), (
            '历史会话页签已从工作台移除，继续 set_current_tab_by_id("history-manager") '
            "只会静默返回 False（历史会话打不开的直接原因）。"
        )


# ═══════════════════════════════════════════════════════════════
# 5. 项目切换联动（工作台页刷新协议 + 可选协议 on_project_changed）
# ═══════════════════════════════════════════════════════════════


class TestFileTreeProjectLinkage:
    """文件树必须接入工作台页刷新协议与项目变更协议"""

    _FILE_TREE_UI = "plugins/file-tree/ui/cards.py"

    def test_file_tree_card_has_refresh_protocols(self):
        tree = _parse(self._FILE_TREE_UI)
        cls = _get_class(tree, "FileTreeCard")
        names = _method_names(cls)
        assert "refresh_data" in names, "FileTreeCard 缺 refresh_data（切页/项目联动都走它）"
        assert "on_project_changed" in names, "FileTreeCard 缺 on_project_changed（项目变更即时刷新）"

    def test_on_project_changed_delegates_to_refresh_data(self):
        tree = _parse(self._FILE_TREE_UI)
        cls = _get_class(tree, "FileTreeCard")
        method = _get_method(cls, "on_project_changed")
        assert _find_calls(method, "refresh_data"), "on_project_changed 需转调 refresh_data（单一刷新实现）"

    def test_refresh_data_reloads_tree_from_fresh_context(self):
        tree = _parse(self._FILE_TREE_UI)
        cls = _get_class(tree, "FileTreeCard")
        method = _get_method(cls, "refresh_data")
        assert _find_calls(method, "_refresh_host_context"), "refresh_data 必须先拉最新宿主上下文"
        assert _find_calls(method, "_apply_latest_theme"), "refresh_data 需应用最新主题/project_root"
        assert _find_calls(method, "_async_load_tree"), "refresh_data 需重载目录树"
