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

pytest.importorskip("PyQt5.QtWidgets")


@pytest.fixture(scope="module")
def qapp():
    from PyQt5.QtWidgets import QApplication

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


# ── 合并语义（修覆盖丢条目）──

def test_card_artifact_list_merges_instead_of_replacing(qapp):
    """同轮多次推送必须累积：旧实现整体替换，后到的会把先到的顶掉"""
    from app.widgets.message_card import MessageCard

    card = MessageCard(role="assistant")
    try:
        card.update_artifact_list([{"path": "/tmp/a.png"}, {"path": "/tmp/b.png"}])
        card.update_artifact_list([{"path": "/tmp/c.png"}])
        assert [it["path"] for it in card._artifact_panel_items] == ["/tmp/a.png", "/tmp/b.png", "/tmp/c.png"]
    finally:
        card.deleteLater()

def test_card_artifact_list_updates_same_path_in_place(qapp):
    """同路径重复推送原位更新，不改变顺序"""
    from app.widgets.message_card import MessageCard

    card = MessageCard(role="assistant")
    try:
        card.update_artifact_list([{"path": "/tmp/a.png", "size": 1}, {"path": "/tmp/b.png"}])
        card.update_artifact_list([{"path": "/tmp/a.png", "size": 2}])
        items = card._artifact_panel_items
        assert [it["path"] for it in items] == ["/tmp/a.png", "/tmp/b.png"]
        assert items[0]["size"] == 2
    finally:
        card.deleteLater()

def test_card_artifact_list_ignores_empty_push(qapp):
    """空列表不再隐含清空（显式清理走 clear_artifact_list）"""
    from app.widgets.message_card import MessageCard

    card = MessageCard(role="assistant")
    try:
        card.update_artifact_list([{"path": "/tmp/a.png"}])
        card.update_artifact_list([])
        assert len(card._artifact_panel_items) == 1
        card.clear_artifact_list()
        assert card._artifact_panel_items == []
    finally:
        card.deleteLater()

# ── 折叠态缩略图带 / 溢出展开 / 拖拽 ──

def test_panel_collapsed_strip_lists_only_images(panel):
    panel.update_artifacts([{"path": "/tmp/a.png"}, {"path": "/tmp/b.png"}, {"path": "/tmp/c.html"}])
    widgets = [panel._strip_layout.itemAt(i).widget() for i in range(panel._strip_layout.count())]
    thumbs = [w for w in widgets if w is not None and w.metaObject().className() == "QLabel"]
    assert len(thumbs) == 2, "折叠带只放图片条目"
    assert not panel._thumb_strip.isHidden(), "折叠态缩略图带应可见"

def test_panel_collapsed_strip_hidden_after_expand(panel):
    panel.update_artifacts([{"path": "/tmp/a.png"}])
    panel._on_collapse_clicked()
    assert panel._thumb_strip.isHidden()

def test_panel_overflow_expand_all(panel):
    from app.widgets.inline_artifact_panel import MAX_VISIBLE_ITEMS

    panel.update_artifacts(_img_items(25))
    rows = [panel._list_layout.itemAt(i).widget() for i in range(panel._list_layout.count())]
    assert len([w for w in rows if w is not None]) == MAX_VISIBLE_ITEMS + 1, "20 行 + 溢出提示"
    panel._expand_all()
    rows = [panel._list_layout.itemAt(i).widget() for i in range(panel._list_layout.count())]
    assert len([w for w in rows if w is not None]) == 25

def test_panel_make_drag_carries_file_url(panel, tmp_path):
    from PyQt5.QtWidgets import QFrame

    f = tmp_path / "a.png"
    f.write_bytes(b"x")
    drag = panel._make_drag(str(f), QFrame())
    assert drag is not None
    from pathlib import Path

    assert [Path(u.toLocalFile()) for u in drag.mimeData().urls()] == [f]

def test_panel_make_drag_missing_file_returns_none(panel, tmp_path):
    from PyQt5.QtWidgets import QFrame

    assert panel._make_drag(str(tmp_path / "nope.png"), QFrame()) is None

# ── 落库归一化与历史恢复 ──

def test_normalize_message_keeps_artifacts():
    from app.core.conversation.message_content import normalize_message

    out = normalize_message(
        {
            "role": "tool",
            "tool_call_id": "c1",
            "name": "gen",
            "content": "ok",
            "artifacts": [
                {
                    "path": "/tmp/a.png",
                    "kind": "image",
                    "width": 1024,
                    "height": 768,
                    "meta": {"duration": 3, "note": "x" * 500, "bad": ["list"]},
                },
                {"path": "", "kind": "image"},
                "not-a-dict",
            ],
        }
    )
    assert out is not None
    assert len(out["artifacts"]) == 1
    art = out["artifacts"][0]
    assert art["path"] == "/tmp/a.png"
    assert art["kind"] == "image"
    assert art["width"] == 1024
    assert len(art["meta"]["note"]) == 200, "meta 长字符串截断"
    assert "bad" not in art["meta"], "非标量 meta 丢弃"

def test_normalize_artifacts_capped():
    from app.core.conversation.message_content import normalize_artifact_items

    items = [{"path": f"/tmp/{i}.png"} for i in range(80)]
    assert len(normalize_artifact_items(items)) == 50

def test_kind_badge_shows_letter(qapp):
    """徽标必须有字：构造期漏 setText 会渲成空白色块"""
    from app.widgets.inline_artifact_panel import _KindBadge

    assert _KindBadge("text").text() == "T"
    assert _KindBadge("file").text() == "文"

def test_render_batch_restores_artifacts():
    """历史渲染收集 batch 内所有 tool 消息的成果（空 content 的也要收）"""
    from app.widgets.ui_helpers import render_batch_to_assistant_card

    calls = {}

    class FakeCard:
        def append_text(self, text, immediate_render=True):
            calls.setdefault("text", []).append(text)

        def append_tool_result(self, **kwargs):
            calls.setdefault("tools", []).append(kwargs)

        def update_artifact_list(self, items):
            calls["artifacts"] = list(items)

        def finish_streaming(self, history=False, immediate=True):
            calls["finished"] = True

    batch = [
        {"role": "assistant", "content": "done"},
        {"role": "tool", "name": "gen", "content": "ok", "tool_call_id": "c1", "artifacts": [{"path": "/tmp/a.png"}]},
        {"role": "tool", "name": "gen2", "content": "", "tool_call_id": "c2", "artifacts": [{"path": "/tmp/b.png"}]},
    ]
    render_batch_to_assistant_card(FakeCard(), batch, immediate_render=False)
    assert [it["path"] for it in calls["artifacts"]] == ["/tmp/a.png", "/tmp/b.png"]
    assert len(calls["tools"]) == 1, "空 content 的 tool 消息不渲染工具块"
    assert calls["finished"] is True

if __name__ == "__main__":
    pytest.main([__file__, "-v"])
