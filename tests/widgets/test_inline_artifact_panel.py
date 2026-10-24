# -*- coding: utf-8 -*-
"""成果栏（InlineArtifactPanel）与 artifacts 字段驱动测试

覆盖：条目归一化 / kind 推断 / 去重 / 上限截断 / 空列表隐藏 / 坏数据容错 /
ToolResult.artifacts 透传 / catalog 登记。端到端（面板挂卡）依赖 QApplication，
走 tests/widgets 的既有 fixtures 形态，能起 QApplication 则跑，否则跳过。
"""

import pytest

from app.tools.result import ToolResult


# ── ToolResult.artifacts 透传 ──


def test_tool_result_artifacts_passthrough():
    items = [{"path": "/tmp/a.png", "kind": "image"}]
    r = ToolResult(True, content="ok", artifacts=items)
    assert r.artifacts == items
    assert r.to_dict()["artifacts"] == items


def test_tool_result_artifacts_none_omitted():
    r = ToolResult(True, content="ok")
    assert r.artifacts is None
    assert "artifacts" not in r.to_dict()


def test_tool_result_artifacts_kwargs_only():
    """关键字参数形态（插件侧 _result_with_artifacts 用法）不炸"""
    r = ToolResult(success=True, content="ok", artifacts=[{"path": "p"}], data={"k": 1})
    assert r.data == {"k": 1}
    assert r.artifacts == [{"path": "p"}]


# ── 面板（需要 QApplication）──

pytest.importorskip("PySide6.QtWidgets")


@pytest.fixture(scope="module")
def qapp():
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([])
    yield app


@pytest.fixture()
def panel(qapp):
    from app.widgets.inline_artifact_panel import InlineArtifactPanel

    p = InlineArtifactPanel()
    yield p
    p.deleteLater()


def _img_items(n, prefix="a"):
    return [{"path": f"/tmp/{prefix}{i}.png", "kind": "image", "name": f"{prefix}{i}.png"} for i in range(n)]


def test_panel_hidden_without_items(panel):
    panel.update_artifacts([])
    assert not panel.isVisible()


def test_panel_hidden_without_items_via_clear(panel):
    panel.update_artifacts(_img_items(2))
    panel.clear_artifacts()
    assert not panel.isVisible()
    assert panel._items == []


def test_panel_signature_skips_rebuild(panel):
    """同签名重复推送不重建（幂等）"""
    items = _img_items(2)
    panel.update_artifacts(items)
    n_items = panel._list_layout.count()
    panel.update_artifacts([dict(x) for x in items])  # 内容相同的新列表
    assert panel._list_layout.count() == n_items


def test_panel_dedupes_same_path(panel):
    """同路径重复条目去重保序"""
    items = [{"path": "/tmp/a.png"}, {"path": "/tmp/a.png"}, {"path": "/tmp/b.png"}]
    panel.update_artifacts(items)
    assert len(panel._items) == 2


def test_panel_drops_invalid_items(panel):
    """坏条目（非 dict / 无 path）静默丢弃"""
    panel.update_artifacts(["not-a-dict", 42, {"nope": 1}, {"path": "/tmp/a.png"}])
    assert len(panel._items) == 1
    assert panel._items[0]["path"] == "/tmp/a.png"


@pytest.mark.parametrize(
    "path, expected",
    [
        ("/x/a.png", "image"),
        ("/x/a.JPG", "image"),
        ("/x/a.mp4", "video"),
        ("/x/a.html", "html"),
        ("/x/a.md", "text"),
        ("/x/a.bin", "file"),
    ],
)
def test_panel_kind_from_ext(panel, path, expected):
    """kind 缺省按扩展名推断"""
    panel.update_artifacts([{"path": path}])
    assert panel._items[0]["kind"] == expected


def test_panel_kind_explicit_wins(panel):
    """显式 kind 优先于扩展名推断"""
    panel.update_artifacts([{"path": "/x/a.png", "kind": "video"}])
    assert panel._items[0]["kind"] == "video"


def test_panel_overflow_capped(panel):
    """超上限截断 + 「还有 N 项」提示"""
    panel.update_artifacts(_img_items(25))
    assert len(panel._items) == 25
    # 可见行 = 上限 20 + 提示行；_clear_items 后 stretch 也占一个 layout 项
    widgets = [panel._list_layout.itemAt(i).widget() for i in range(panel._list_layout.count())]
    labels = [w for w in widgets if w is not None and w.metaObject().className() == "QLabel"]
    assert any("还有 5 项" in w.text() for w in labels), "应有溢出提示"


def test_panel_size_not_duplicated_on_repeat(panel):
    """重复推送不同 size 的同路径 → 视为新数据重建（文件可能已更新）"""
    panel.update_artifacts([{"path": "/tmp/a.png", "size": 1}])
    panel.update_artifacts([{"path": "/tmp/a.png", "size": 2}])
    assert len(panel._items) == 1
    assert panel._items[0]["size"] == 2


# ── catalog 登记 ──


def test_catalog_has_artifacts_show():
    from app.plugins.capabilities import catalog
    from app.plugins.contracts.capabilities import CapabilityLevel

    spec = catalog.get("ui.artifacts.show")
    assert spec is not None
    assert spec.level is CapabilityLevel.MEDIUM
    assert spec.surfaces == frozenset({"ui", "tool"})


def test_capability_registered_both_surfaces():
    """两 surface 都注册了 ui.artifacts.show（经捕获型 registry 验证）"""
    from app.plugins.capabilities.providers import tool_core, ui_core

    class CaptureRegistry:
        def __init__(self):
            self.calls = {}

        def register(self, cap_id, provider, **kwargs):
            self.calls[(cap_id, kwargs.get("surface", "ui"))] = provider

        def has(self, cap_id, surface):
            return (cap_id, surface) in self.calls

    reg = CaptureRegistry()
    ui_core.register(reg)
    tool_core.register(reg)
    assert reg.has("ui.artifacts.show", "ui")
    assert reg.has("ui.artifacts.show", "tool")


def test_tool_surface_show_without_window_returns_zero():
    """tool surface 无窗口时返回恒 0 空实现（不炸）

    ``_artifacts_show`` 是 ``tool_core.register`` 内的嵌套工厂（该模块惯例），
    经 register 捕获取用。
    """
    from app.plugins.capabilities.providers import tool_core

    holder = {}

    class CaptureRegistry:
        def register(self, cap_id, provider, **kwargs):
            if cap_id == "ui.artifacts.show":
                holder["provider"] = provider

    tool_core.register(CaptureRegistry())
    assert "provider" in holder, "ui.artifacts.show 未在 tool surface 注册"
    provider = holder["provider"](None)
    assert provider([{"path": "/tmp/a.png"}]) == 0
    assert provider([]) == 0


# ── ui surface provider（主线程直跑，不依赖窗口就返回 0）──


def test_ui_provider_bad_items_zero(qapp, monkeypatch):
    from app.plugins.capabilities.providers import ui_core

    provider = ui_core._ui_artifacts_show(object())  # host 无卡片属性
    assert provider([]) == 0
    assert provider("not-a-list") == 0


def test_ui_provider_delivers_on_main_thread(qapp):
    """主线程调用直接落到 update_artifact_list（用假卡片验证）"""
    from app.plugins.capabilities.providers import ui_core

    delivered = []

    class FakeCard:
        def update_artifact_list(self, items):
            delivered.extend(items)

    class FakeHost:
        _current_assistant_card = FakeCard()
        _tool_card_map = {}

    provider = ui_core._ui_artifacts_show(FakeHost())
    n = provider([{"path": "/tmp/a.png"}, {"path": "/tmp/b.png"}])
    assert n == 2
    assert len(delivered) == 2


def test_ui_provider_call_id_targeting(qapp):
    """target="call" 经 _tool_card_map 定位原卡"""
    from app.plugins.capabilities.providers import ui_core

    delivered = []

    class FakeCard:
        def __init__(self, tag):
            self.tag = tag

        def update_artifact_list(self, items):
            delivered.append(self.tag)

    old_card, origin_card = FakeCard("latest"), FakeCard("origin")
    host = type("H", (), {})()
    host._current_assistant_card = old_card
    host._tool_card_map = {"call-1": origin_card}

    provider = ui_core._ui_artifacts_show(host)
    provider([{"path": "/tmp/a.png"}], target="call", call_id="call-1")
    assert delivered == ["origin"], "应挂到 call_id 对应的发起卡，而非最新卡"


def test_ui_provider_missing_card_zero(qapp):
    from app.plugins.capabilities.providers import ui_core

    host = type("H", (), {})()  # 无 _current_assistant_card / _find_latest_assistant_card
    provider = ui_core._ui_artifacts_show(host)
    assert provider([{"path": "/tmp/a.png"}]) == 0


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
