# -*- coding: utf-8 -*-
"""插件组件/细项开关单元测试（D9 组件级 + D10 细项级）

覆盖范围：
1. 三段式 key 语义：`plugin:component`（整类）优先于 `plugin:component:item`（细项）
2. set_item_enabled / disabled_items 的幂等与列举
3. 禁用集缓存与 invalidate_component_cache
4. 细项枚举：hooks / team_templates / tools 各自的条目来源
5. 回归：团队模板的 system / user 源必须受组件级停用约束
   （此前这两路按硬编码路径读取，绕过了 PluginManager，导致开关关不掉）

设计说明：
- 全部走内存态 mock，不写真实 Settings（避免污染用户配置）
- 不创建任何 QWidget——细项枚举与过滤判定都是纯逻辑
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.plugins.component_items import ComponentItem, list_component_items, supports_items
from app.plugins.managers.plugin_manager import PluginManager

# 细项枚举在单测里不依赖 PluginManager 初始化——显式给出插件根目录，
# 既避开单例状态，也让用例意图更清晰
PROJECT_ROOT = Path(__file__).resolve().parents[2]
SYSTEM_PLUGIN_DIR = PROJECT_ROOT / "plugins" / "system"


@pytest.fixture
def pm_memory():
    """PluginManager + 内存态禁用集（不落盘）

    返回 (pm, store)：store 是真实的 set，测试可直接断言其内容。
    """
    pm = PluginManager.get_instance()
    store: set = set()
    pm.invalidate_component_cache()
    pm._disabled_components_cache = None
    # 旁路 Settings 读写：_get 走内存，_save 写回内存
    pm.__dict__["_get_disabled_components"] = lambda: frozenset(store)
    pm.__dict__["_save_disabled_components"] = lambda d: (store.clear(), store.update(d))
    yield pm, store
    # 还原实例级遮蔽，避免影响其他用例
    pm.__dict__.pop("_get_disabled_components", None)
    pm.__dict__.pop("_save_disabled_components", None)
    pm.invalidate_component_cache()


# ── 1. 三段式 key 语义 ──────────────────────────────


def test_item_key_helpers():
    assert PluginManager.component_key("demo", "tools") == "demo:tools"
    assert PluginManager.item_key("demo", "tools", "read_file") == "demo:tools:read_file"


def test_component_off_disables_all_items(pm_memory):
    pm, store = pm_memory
    pm.set_component_enabled("demo", "tools", False)
    assert store == {"demo:tools"}
    # 整类停用 ⇒ 其下任何细项都不可用
    assert pm.is_item_enabled("demo", "tools", "read_file") is False
    assert pm.is_item_enabled("demo", "tools", "anything") is False


def test_item_off_leaves_siblings_on(pm_memory):
    pm, store = pm_memory
    pm.set_item_enabled("demo", "tools", "read_file", False)
    assert store == {"demo:tools:read_file"}
    assert pm.is_item_enabled("demo", "tools", "read_file") is False
    # 兄弟条目不受影响
    assert pm.is_item_enabled("demo", "tools", "write_file") is True
    # 组件本身仍是启用态
    assert pm.is_component_enabled("demo", "tools") is True


def test_item_off_then_component_off_then_item_back_on(pm_memory):
    """整类停用期间打开某个细项，整类恢复后该细项是启用的"""
    pm, store = pm_memory
    pm.set_item_enabled("demo", "tools", "read_file", False)
    pm.set_component_enabled("demo", "tools", False)
    pm.set_item_enabled("demo", "tools", "read_file", True)  # 清掉细项标记
    assert store == {"demo:tools"}
    pm.set_component_enabled("demo", "tools", True)
    assert pm.is_item_enabled("demo", "tools", "read_file") is True


def test_toggle_idempotent(pm_memory):
    pm, store = pm_memory
    pm.set_item_enabled("demo", "hooks", "h1", False)
    pm.set_item_enabled("demo", "hooks", "h1", False)  # 重复停用：不应产生重复 key
    assert store == {"demo:hooks:h1"}
    pm.set_item_enabled("demo", "hooks", "h1", True)
    pm.set_item_enabled("demo", "hooks", "h1", True)
    assert store == set()


def test_disabled_items(pm_memory):
    pm, _store = pm_memory
    pm.set_item_enabled("demo", "tools", "a", False)
    pm.set_item_enabled("demo", "tools", "b", False)
    pm.set_item_enabled("demo", "hooks", "h", False)  # 其他组件不应被列出
    assert sorted(pm.disabled_items("demo", "tools")) == ["a", "b"]
    assert pm.disabled_items("demo", "commands") == []


def test_invalidate_component_cache():
    """缓存命中返回同一对象；invalidate 后下次读取重新构造

    不走 pm_memory fixture——本用例验证的正是「真实读取 + 缓存」这一层。
    只读取 Settings、不写入，因此不会污染用户配置。
    """
    pm = PluginManager.get_instance()
    pm.invalidate_component_cache()
    first = pm._get_disabled_components()
    assert pm._disabled_components_cache is not None, "首次读取后缓存应被填充"
    assert pm._get_disabled_components() is first, "缓存命中时必须复用同一对象"
    pm.invalidate_component_cache()
    assert pm._disabled_components_cache is None


# ── 2. 细项枚举 ────────────────────────────────────


def test_supports_items():
    assert supports_items("tools") is True
    assert supports_items("hooks") is True
    assert supports_items("ui") is False  # ui 是插件入口，无稳定细项 id


def test_enumerate_system_hooks():
    """系统插件的 hooks.json 里每条带 id 的 hook 都应被枚举出来"""
    items = list_component_items("system", "hooks", SYSTEM_PLUGIN_DIR)
    assert items, "系统插件应当有 hook 细项"
    ids = [it.id for it in items]
    assert len(ids) == len(set(ids)), "细项 id 必须唯一（会被写进配置做寻址）"
    for it in items:
        assert isinstance(it, ComponentItem)
        assert it.id


def test_enumerate_team_templates():
    items = list_component_items("system", "team_templates", SYSTEM_PLUGIN_DIR)
    assert items, "系统插件应当有团队模板"
    assert all(it.id for it in items)


def test_enumerate_unknown_component_returns_empty():
    """未知组件不应抛异常（设置界面不能因一个怪异目录整体崩掉）"""
    assert list_component_items("system", "no_such_component", SYSTEM_PLUGIN_DIR) == []


def test_enumerate_without_plugin_path_is_safe():
    """插件路径解析失败时返回空列表，不抛异常"""
    assert list_component_items("__no_such_plugin__", "hooks") == []


# ── 3. 团队模板来源过滤（回归） ─────────────────────


def test_template_sources_honor_component_switch(pm_memory, monkeypatch):
    """回归：system / user 源此前绕过 PluginManager，关掉 team_templates 仍会列出"""
    from app.core.team.template_manager import TemplateManager

    pm, _store = pm_memory
    # 让 TemplateManager 的插件存在性检查成立
    monkeypatch.setattr(pm, "is_initialized", lambda: True)
    monkeypatch.setattr(pm, "has_plugin", lambda name: True)

    tm = TemplateManager.get_instance()
    baseline = tm._template_sources()
    assert baseline, "应当至少解析出一路模板来源"

    # 整类停用 system 插件 → system 源消失
    pm.set_component_enabled("system", "team_templates", False)
    sources = tm._template_sources()
    assert all(src != tm.SOURCE_SYSTEM for _d, src, _p in sources), "system 源未被停用开关过滤"

    # 整类停用 user-custom → user 源消失
    pm.set_component_enabled("user-custom", "team_templates", False)
    sources = tm._template_sources()
    assert all(src != tm.SOURCE_USER for _d, src, _p in sources), "user 源未被停用开关过滤"


def test_template_source_carries_plugin_name(pm_memory):
    """细项过滤靠 plugin_name 拼 key，三路来源都必须带上归属插件名"""
    from app.core.team.template_manager import TemplateManager

    tm = TemplateManager.get_instance()
    for _d, src, plugin in tm._template_sources():
        assert plugin, f"来源 {src} 缺少归属插件名，细项过滤会失效"


# ── 4. 信号签名回归（曾因 lambda 形参顺序写反抛 TypeError）──


def test_component_row_signal_carries_component_then_enabled(qapp):
    """回归：ComponentRow.toggled 的 lambda 形参曾写成 (enabled, component)"""
    from app.plugins.component_items import ComponentItem
    from app.widgets.cards.settings.plugin_components_card import ComponentRow, ItemRow

    row = ComponentRow("tools", True)
    got = []
    row.toggled.connect(lambda component, enabled: got.append((component, enabled)))
    row.switch.setChecked(False)
    assert got == [("tools", False)], "组件开关信号必须按 (component, enabled) 传递"

    irow = ItemRow(ComponentItem(id="read_file", label="读取文件"), True)
    got = []
    irow.toggled.connect(lambda item_id, enabled: got.append((item_id, enabled)))
    irow.switch.setChecked(False)
    assert got == [("read_file", False)], "细项开关信号必须按 (item_id, enabled) 传递"


def test_section_forwards_plugin_name(qapp):
    """回归：PluginSectionWidget 转发时同样要带对插件名与组件名"""
    from app.widgets.cards.settings.plugin_components_card import PluginSectionWidget

    section = PluginSectionWidget("demo", "", True, ["tools"])
    got = []
    section.component_toggled.connect(lambda plugin, component, enabled: got.append((plugin, component, enabled)))
    section.component_row("tools").switch.setChecked(False)
    assert got == [("demo", "tools", False)]


@pytest.fixture
def qapp():
    """最小 QApplication（细项行是 QWidget，实例化必须有 app）"""
    from PyQt5.QtWidgets import QApplication

    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    yield app
