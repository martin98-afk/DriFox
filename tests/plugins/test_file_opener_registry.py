# -*- coding: utf-8 -*-
"""本地文件打开接管链（file_opener）回归测试

主程序在「交系统默认程序打开本地文件」之前先问插件接管方。这些用例锁住：
- 优先级降序尝试；can_open 不命中则跳过
- open 返回 False 视为放弃接管，继续问下一家；全部放弃才交系统程序
- 单个接管项抛异常只让它自己出局（插件 bug 不能把打开动作变成完全失效）
- 插件卸载后接管项被清理（残留会拿旧模块闭包静默失败）
- 槽位声明表覆盖该扩展点（卸载幂等判据依赖它）
"""

import pytest

from app.plugins.registries.ui_plugin_registry import (
    FileOpenerInfo,
    UI_SLOT_DECLS,
    UIPluginRegistry,
)


class TestRegisterAndOrder:
    def test_register_and_get_sorted_by_priority(self, fresh_registry):
        fresh_registry.register_file_opener("demo", "low", lambda p: True, lambda p: True, priority=1)
        fresh_registry.register_file_opener("demo", "high", lambda p: True, lambda p: True, priority=9)
        openers = fresh_registry.get_file_openers()
        assert [i.opener_id for i in openers] == ["high", "low"]
        assert all(isinstance(i, FileOpenerInfo) for i in openers)

    def test_same_id_overwrites(self, fresh_registry):
        """同插件同 ID 重复注册即覆盖（热重载幂等，不叠出两条）"""
        fresh_registry.register_file_opener("demo", "x", lambda p: False, lambda p: False)
        fresh_registry.register_file_opener("demo", "x", lambda p: True, lambda p: True)
        assert len(fresh_registry.get_file_openers()) == 1
        assert fresh_registry.try_open_local_file("/tmp/a.txt") is True

    def test_slot_declared(self):
        """扩展点必须进声明表：卸载幂等判据与槽位查询都由它驱动"""
        assert "file_opener" in UI_SLOT_DECLS


class TestTryOpenChain:
    def test_priority_order_respected(self, fresh_registry):
        calls = []
        fresh_registry.register_file_opener(
            "demo", "first", lambda p: True, lambda p: calls.append("first") or True, priority=100
        )
        fresh_registry.register_file_opener(
            "demo", "second", lambda p: True, lambda p: calls.append("second") or True, priority=1
        )
        assert fresh_registry.try_open_local_file("a.py") is True
        assert calls == ["first"]  # 高优先级接管成功后不再问下一家

    def test_can_open_rejection_skips(self, fresh_registry):
        calls = []
        fresh_registry.register_file_opener(
            "demo", "py", lambda p: p.endswith(".py"), lambda p: calls.append(p) or True, priority=10
        )
        assert fresh_registry.try_open_local_file("a.txt") is False
        assert calls == []
        assert fresh_registry.try_open_local_file("a.py") is True
        assert calls == ["a.py"]

    def test_open_false_degrades_to_next(self, fresh_registry):
        """open 返回 False（如工作台不可用）→ 继续问下一家，最后交系统程序"""
        calls = []
        fresh_registry.register_file_opener(
            "demo", "fail", lambda p: True, lambda p: calls.append("fail") or False, priority=10
        )
        fresh_registry.register_file_opener(
            "demo", "ok", lambda p: True, lambda p: calls.append("ok") or True, priority=1
        )
        assert fresh_registry.try_open_local_file("a.py") is True
        assert calls == ["fail", "ok"]

    def test_all_decline_returns_false(self, fresh_registry):
        fresh_registry.register_file_opener("demo", "no", lambda p: True, lambda p: False)
        assert fresh_registry.try_open_local_file("a.py") is False

    def test_exception_in_can_open_isolated(self, fresh_registry):
        def boom(_p):
            raise RuntimeError("插件炸了")

        calls = []
        fresh_registry.register_file_opener("demo", "bad", boom, lambda p: calls.append("bad") or True, priority=99)
        fresh_registry.register_file_opener(
            "demo", "good", lambda p: True, lambda p: calls.append("good") or True, priority=1
        )
        assert fresh_registry.try_open_local_file("a.py") is True
        assert calls == ["good"]

    def test_exception_in_open_isolated(self, fresh_registry):
        def boom(_p):
            raise RuntimeError("渲染失败")

        calls = []
        fresh_registry.register_file_opener("demo", "bad", lambda p: True, boom, priority=99)
        fresh_registry.register_file_opener(
            "demo", "good", lambda p: True, lambda p: calls.append("good") or True, priority=1
        )
        assert fresh_registry.try_open_local_file("a.py") is True
        assert calls == ["good"]

    def test_empty_path_returns_false(self, fresh_registry):
        fresh_registry.register_file_opener("demo", "any", lambda p: True, lambda p: True)
        assert fresh_registry.try_open_local_file("") is False

    def test_no_openers_returns_false(self, fresh_registry):
        assert fresh_registry.try_open_local_file("a.py") is False


class TestUnregister:
    def test_unregister_removes_only_target_plugin(self, fresh_registry):
        fresh_registry.register_file_opener("a", "x", lambda p: True, lambda p: True)
        fresh_registry.register_file_opener("b", "y", lambda p: True, lambda p: True)
        fresh_registry.unregister_file_openers("a")
        assert [i.plugin_name for i in fresh_registry.get_file_openers()] == ["b"]
        assert fresh_registry.try_open_local_file("f.py") is True

    def test_unregister_idempotent(self, fresh_registry):
        fresh_registry.unregister_file_openers("missing")
        assert fresh_registry.get_file_openers() == []


class TestWorkbenchPageLookup:
    def test_get_workbench_page_returns_none_without_panel(self, fresh_registry, monkeypatch):
        """无 TabManagerWindow（headless / 早期启动）时安静返回 None"""
        monkeypatch.setattr(fresh_registry, "_workbench_panel", lambda: None)
        assert fresh_registry.get_workbench_page("file-tree") is None
        assert fresh_registry.open_workbench_page("file-tree") is None

    def test_get_workbench_page_delegates(self, fresh_registry, monkeypatch):
        sentinel = object()

        class _Panel:
            def get_plugin_page(self, page_id):
                return sentinel if page_id == "file-tree" else None

        monkeypatch.setattr(fresh_registry, "_workbench_panel", lambda: _Panel())
        assert fresh_registry.get_workbench_page("file-tree") is sentinel
        assert fresh_registry.get_workbench_page("other") is None

    def test_panel_exception_is_swallowed(self, fresh_registry, monkeypatch):
        class _Panel:
            def get_plugin_page(self, page_id):
                raise RuntimeError("wrapper 已销毁")

        monkeypatch.setattr(fresh_registry, "_workbench_panel", lambda: _Panel())
        assert fresh_registry.get_workbench_page("file-tree") is None


class TestOpenWorkbenchPageReconcile:
    """首次展开前必须先 reconcile：否则动画收尾才建页 → 调用方拿到 None 静默降级"""

    def test_reconciles_before_opening_when_page_missing(self, fresh_registry, monkeypatch):
        events = []
        page = object()

        class _Panel:
            built = False

            def get_plugin_page(self, page_id):
                events.append(("get", page_id))
                return page if self.built else None

            def sync_plugin_pages(self, tabs, force=False, force_plugin=""):
                events.append(("sync", len(list(tabs))))
                self.built = True

        panel = _Panel()
        monkeypatch.setattr(fresh_registry, "_workbench_panel", lambda: panel)

        class _TM:
            def open_workbench_tab(self, page_id):
                events.append(("open", page_id))
                return True

        monkeypatch.setattr("app.widgets.tab_manager_window.TabManagerWindow.get_instance", classmethod(lambda cls: _TM()))
        assert fresh_registry.open_workbench_page("file-tree") is page
        # reconcile 必须发生在展开之前（展开会走动画，页在收尾才建）
        assert [e[0] for e in events] == ["get", "sync", "open", "get"]

    def test_skips_reconcile_when_page_already_mounted(self, fresh_registry, monkeypatch):
        page = object()
        synced = []

        class _Panel:
            def get_plugin_page(self, page_id):
                return page

            def sync_plugin_pages(self, tabs, **kwargs):
                synced.append(True)

        monkeypatch.setattr(fresh_registry, "_workbench_panel", lambda: _Panel())

        class _TM:
            def open_workbench_tab(self, page_id):
                return True

        monkeypatch.setattr("app.widgets.tab_manager_window.TabManagerWindow.get_instance", classmethod(lambda cls: _TM()))
        assert fresh_registry.open_workbench_page("file-tree") is page
        assert synced == []  # 已挂载：不重复 reconcile（签名未变时虽幂等但没必要）

    def test_panel_none_returns_none(self, fresh_registry, monkeypatch):
        monkeypatch.setattr(fresh_registry, "_workbench_panel", lambda: None)
        assert fresh_registry.open_workbench_page("file-tree") is None


@pytest.fixture()
def fresh_registry(monkeypatch):
    """独立 UIPluginRegistry（本文件自备，避免依赖 plugins/ conftest 的路径解析）"""
    reg = UIPluginRegistry()
    monkeypatch.setattr(UIPluginRegistry, "_instance", reg)
    monkeypatch.setattr(UIPluginRegistry, "get_instance", classmethod(lambda cls: reg))
    return reg
