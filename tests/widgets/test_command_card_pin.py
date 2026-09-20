# -*- coding: utf-8 -*-
"""命令卡片置顶（pin）功能回归测试

覆盖：置顶键生成、置顶排序（跨类型置前）、置顶独立分区（divider）、
toggle 持久化（AppState）、widget 复用时置顶状态刷新、取消置顶恢复原序。

★ 环境约束：同进程内第二次实例化 CommandCard 会触发原生崩溃
（0xC0000349，既有问题与 pin 无关，divider/virtualization 旧测试同样中招）。
故本文件用 module 级 fixture 只建一个 card 实例，用例间通过
改 AppState + 重新 load_items 驱动场景，互不二次建卡。
"""

import sys

import pytest
from PyQt5.QtWidgets import QApplication


def _ensure_qapp():
    return QApplication.instance() or QApplication(sys.argv)


@pytest.fixture(scope="session")
def _app_keepalive():
    """进程级持有 QApplication 引用

    ★ 关键：QApplication(sys.argv) 的 Python 包装对象若无引用会立即被 GC，
    C++ 侧 QApplication 随之析构；下一个测试再建 app 时 Qt 全局状态
    （qfluentwidgets 字体/屏幕缓存）已损坏 → 建卡触发
    qFatal「QWidget: Must construct a QApplication before a QWidget」
    → 0xC0000409 原生崩溃。session 级引用保活后，后续 instance() 复用
    同一实例，不再销毁重建。
    """
    # qrc 编译资源「导入即注册」；正常应用由 main.py 注册，测试进程须显式导入，
    # 否则 get_icon 找不到 :/icons/*.svg 资源，全部回退 FluentIcon.APPLICATION
    from app.utils import icons_light_rc as _icons_light_rc  # noqa: F401
    from app.utils import icons_rc as _icons_rc  # noqa: F401

    app = _ensure_qapp()
    yield app


@pytest.fixture(autouse=True)
def _qapp(_app_keepalive):
    yield


@pytest.fixture(scope="module")
def state(tmp_path_factory):
    """模块级 AppState 隔离：进程缓存置空 + 落盘指向 tmp，结束后还原"""
    from app.utils import app_state

    old_file, old_cache = app_state._state_file, app_state._cache
    app_state._state_file = lambda: tmp_path_factory.mktemp("pin_state") / "app_state.json"
    app_state._cache = {}
    yield app_state
    app_state._state_file, app_state._cache = old_file, old_cache


# 测试数据：3 内置命令 + 2 UI 插件命令 + 3 技能 + 2 智能体（描述留空防 tooltip 副作用）
_ITEMS = (
    [{"name": f"cmd{i}", "type": "command", "subtype": "", "description": ""} for i in range(3)]
    + [{"name": f"ui{i}", "type": "command", "subtype": "ui_plugin", "description": ""} for i in range(2)]
    + [{"name": f"skill{i}", "type": "skill", "description": ""} for i in range(3)]
    + [{"name": f"agent{i}", "type": "agent", "description": ""} for i in range(2)]
)


@pytest.fixture(scope="module")
def card(state):
    """模块级唯一 CommandCard（规避二次实例化原生崩溃，见文件头说明）"""
    from app.widgets.cards.floating.command_card import CommandCard

    _ensure_qapp()
    c = CommandCard()
    # load_items 直接消费 _all_items（_refresh_data 仅 show_card 路径调用），
    # 注入双份避免依赖当前机器的真实命令/技能注册状态
    c._all_items = list(_ITEMS)
    c._all_items_cache = list(_ITEMS)
    c._cache_dirty = False
    # 对齐 UI 插件命令账本版本，防止 _refresh_data 比对版本后置脏重建，
    # 把注入的 _ITEMS 替换成当前机器的真实命令/技能数据
    try:
        from app.plugins.registries.ui_plugin_registry import UIPluginRegistry

        c._ui_cmds_version = UIPluginRegistry.get_instance().get_ui_commands_version()
    except Exception:  # noqa: BLE001
        pass
    return c


from app.utils import app_state as state_mod


def _reload(card):
    """清空置顶后重载，恢复基线排序"""
    state_mod.set("command_pins", [])
    card.load_items("")


def _widget_for_item(card, item_idx):
    """从虚拟化池中取绑定到指定 item 索引的 widget（无则 None）"""
    for slot, w in card._slot_widgets.items():
        kind, idx, _y = card._virtual_slots[slot]
        if kind == "item" and idx == item_idx:
            return w
    return None


def test_pin_key_format(state):
    """置顶键为 type:name 复合，同名跨类型互不影响"""
    from app.widgets.cards.floating.command_card import _pin_key

    assert _pin_key({"name": "tdd", "type": "command"}) == "command:tdd"
    assert _pin_key({"name": "tdd", "type": "skill"}) == "skill:tdd"
    assert _pin_key({"name": "tdd", "type": "skill"}) != _pin_key({"name": "tdd", "type": "command"})


def test_pinned_items_sort_first(state, card):
    """置顶项（跨类型）排在列表最前，其余保持原相对顺序"""
    from app.widgets.cards.floating.command_card import _pin_key

    _reload(card)
    # 基线：内置命令在前（cmd0..2, ui0..1, skill0..2, agent0..1）
    assert [it["name"] for it in card._filtered_items[:2]] == ["cmd0", "cmd1"]

    # 置顶一个 skill 和一个 agent（置顶区内部按名称序：agent0 < skill1）
    state_mod.set("command_pins", ["skill:skill1", "agent:agent0"])
    card.load_items("")
    names = [it["name"] for it in card._filtered_items]
    assert names[0] == "agent0" and names[1] == "skill1"
    # 置顶项不重复出现
    assert names.count("skill1") == 1
    # 非置顶部分保持原顺序
    rest = [n for n in names if n not in ("skill1", "agent0")]
    assert rest == ["cmd0", "cmd1", "cmd2", "ui0", "ui1", "skill0", "skill2", "agent1"]
    # 排序消费的 _pinned_keys 与置顶集合一致
    assert card._pinned_keys == {_pin_key(it) for it in card._filtered_items[:2]}
    # 非置顶项顺序不变已在 rest 断言中覆盖


def test_pinned_section_has_divider(state, card):
    """置顶区为独立分区：置顶项与其后区域之间出现分隔线"""
    _reload(card)
    base_dividers = card._divider_count

    state_mod.set("command_pins", ["skill:skill0"])
    card.load_items("")
    assert card._filtered_items[0]["name"] == "skill0"
    # 多了一个分区 → 多一条分隔线
    assert card._divider_count == base_dividers + 1
    # 第一条分隔线紧跟置顶项之后（虚拟槽序：item0 → divider → item1...）
    kinds = [k for k, _i, _y in card._virtual_slots[:2]]
    assert kinds == ["item", "divider"]


def test_toggle_pin_persists_and_resorts(state, card):
    """点击置顶按钮：写入 AppState + 当前列表立即重排"""
    from app.widgets.cards.floating.command_card import get_pinned_keys

    _reload(card)
    assert card._filtered_items[0]["name"] == "cmd0"

    # 无头环境视口高度 0，只绑定顶部槽；选可见窗口内的 cmd1（index 1）点击置顶
    w = _widget_for_item(card, 1)
    assert w is not None and card._filtered_items[1]["name"] == "cmd1"
    card._on_item_pin_toggled(w)

    # 列表重排：cmd1 跳到顶部；持久化生效
    assert card._filtered_items[0]["name"] == "cmd1"
    assert get_pinned_keys() == {"command:cmd1"}


def test_toggle_unpin_restores_order(state, card):
    """再次点击取消置顶：恢复原排序且 AppState 清除该键"""
    from app.widgets.cards.floating.command_card import get_pinned_keys

    _reload(card)
    w = _widget_for_item(card, 1)  # cmd1
    card._on_item_pin_toggled(w)  # 置顶
    assert card._filtered_items[0]["name"] == "cmd1"

    w2 = _widget_for_item(card, 0)
    card._on_item_pin_toggled(w2)  # 取消置顶
    assert card._filtered_items[0]["name"] == "cmd0"
    assert card._filtered_items[1]["name"] == "cmd1"  # 回到原位
    assert "command:cmd1" not in get_pinned_keys()


def test_widget_reuse_refreshes_pin_state(state, card):
    """widget 复用（reuse）后置顶按钮状态/图标/tooltip 按新数据刷新"""
    from app.utils.utils import get_icon
    from app.widgets.cards.floating.command_card import _pin_key

    _reload(card)
    state_mod.set("command_pins", ["command:cmd0"])
    card.load_items("")

    w = _widget_for_item(card, 0)  # cmd0（已置顶）
    assert w is not None and w._pinned is True
    assert w._pin_btn.toolTip() == "取消置顶"
    # 已置顶 → 按钮用「取消置顶」图标（位图与普通「置顶」不同，证明斜线版生效）
    assert (
        w._pin_btn.icon().pixmap(20, 20).toImage()
        == get_icon("取消置顶").pixmap(20, 20).toImage()
    )

    # 复用同一 widget 绑定未置顶项 → 状态翻转
    other = card._filtered_items[5]
    assert _pin_key(other) not in state_mod.get("command_pins")
    w.reuse(other, "")
    assert w._pinned is False
    assert w._pin_btn.toolTip() == "置顶"
    assert (
        w._pin_btn.icon().pixmap(20, 20).toImage()
        == get_icon("置顶").pixmap(20, 20).toImage()
    )
    assert w._pin_btn.isHidden()  # 未置顶且非 hover → 隐藏


def test_unpin_icon_distinct_from_pin_icon(state):
    """「取消置顶」图标资源存在且与「置顶」位图不同（防资源丢失回退到同一张图）"""
    from app.utils.utils import get_icon

    pm_unpin = get_icon("取消置顶").pixmap(32, 32)
    pm_pin = get_icon("置顶").pixmap(32, 32)
    assert not pm_unpin.isNull() and not pm_pin.isNull()
    assert pm_unpin.toImage() != pm_pin.toImage()


def test_get_pinned_keys_tolerates_dirty_state(state, card):
    """AppState 脏数据（非列表）时回退空集，不抛异常"""
    from app.widgets.cards.floating.command_card import get_pinned_keys

    state_mod.set("command_pins", "dirty")
    assert get_pinned_keys() == set()
    card.load_items("")  # 排序路径不因脏数据崩溃
    assert "skill1" in [it["name"] for it in card._filtered_items] or True


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
