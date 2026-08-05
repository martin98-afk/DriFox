# -*- coding: utf-8 -*-
"""验证 #ui 类别过滤新增功能

测试 CommandCard 的 _parse_type_filter 和 _matches_type_filter 是否正确识别 #ui。
"""
from app.widgets.cards.floating.command_card import CommandCard


def test_parse_type_filter_ui():
    """#ui 应被识别为 {'ui'} 类别过滤器"""
    tests = [
        ("#ui", ({"ui"}, "")),
        ("#ui search", ({"ui"}, "search")),
        ("#skill #ui", ({"skill", "ui"}, "")),
        ("#skill #ui foo", ({"skill", "ui"}, "foo")),
        ("#skill tdd", ({"skill"}, "tdd")),
        ("type:ui", ({"ui"}, "")),
        ("type:skill|type:ui", ({"skill", "ui"}, "")),
        ("#ui|tdd", ({"ui"}, "tdd")),  # #ui 是类型，后面是搜索关键字
        ("#agent", ({"agent"}, "")),
        ("find", (None, "find")),
    ]
    for query, expected in tests:
        result = CommandCard._parse_type_filter(query)
        assert result == expected, f"_parse_type_filter({query!r}) = {result}, expected {expected}"
        print(f"  [OK] _parse_type_filter({query!r}) = {result}")


def test_matches_type_filter_ui():
    """#ui 应匹配 type=command 且 subtype=ui_plugin 的项"""
    ui_plugin = {"name": "foo", "type": "command", "subtype": "ui_plugin", "description": "a ui command"}
    normal_cmd = {"name": "bar", "type": "command", "description": "a normal command"}
    skill = {"name": "baz", "type": "skill", "description": "a skill"}
    agent = {"name": "qux", "type": "agent", "description": "an agent"}
    prompt = {"name": "quux", "type": "prompt", "description": "a prompt"}

    all_items = [ui_plugin, normal_cmd, skill, agent, prompt]

    # #ui 单独 → 只匹配 UI 插件
    matched = [it for it in all_items if CommandCard._matches_type_filter(it, {"ui"})]
    assert matched == [ui_plugin], f"#ui 应只匹配 UI 插件，实际: {matched}"
    print(f"  [OK] #ui 匹配: {[it['name'] for it in matched]}")

    # #cmd → 同时匹配 UI 插件和普通命令
    matched = [it for it in all_items if CommandCard._matches_type_filter(it, {"command"})]
    assert matched == [ui_plugin, normal_cmd], f"#cmd 期望匹配 UI 插件+普通命令，实际: {matched}"
    print(f"  [OK] #cmd 匹配: {[it['name'] for it in matched]}")

    # #cmd 加上普通命令不带 ui_plugin subtype 时仍可被 #cmd 匹配
    assert CommandCard._matches_type_filter(normal_cmd, {"command"})
    assert CommandCard._matches_type_filter(ui_plugin, {"command"})
    assert not CommandCard._matches_type_filter(skill, {"command"})
    print("  [OK] #cmd 类型匹配逻辑正常")

    # None → 不过滤
    matched = [it for it in all_items if CommandCard._matches_type_filter(it, None)]
    assert len(matched) == len(all_items)
    print(f"  [OK] None 过滤: {len(matched)} 项（全部）")

    # 空集合 → 不过滤
    matched = [it for it in all_items if CommandCard._matches_type_filter(it, set())]
    assert len(matched) == len(all_items)
    print(f"  [OK] 空 set 过滤: {len(matched)} 项（全部）")

    # #ui|#skill 多类过滤
    matched = [it for it in all_items if CommandCard._matches_type_filter(it, {"ui", "skill"})]
    assert matched == [ui_plugin, skill], f"#ui|#skill 期望 [ui_plugin, skill]，实际: {matched}"
    print(f"  [OK] #ui|#skill 匹配: {[it['name'] for it in matched]}")


def test_parse_type_filter_does_not_break_existing():
    """确保添加 #ui 没有破坏现有的过滤功能"""
    tests = [
        ("#cmd find", ({"command"}, "find")),
        ("#cmd", ({"command"}, "")),
        ("#skill", ({"skill"}, "")),
        ("#agent", ({"agent"}, "")),
        ("#prompt", ({"prompt"}, "")),
        ("type:cmd", ({"command"}, "")),
    ]
    for query, expected in tests:
        result = CommandCard._parse_type_filter(query)
        assert result == expected, f"已存在语法回归: {query!r} = {result}, expected {expected}"
        print(f"  [OK] 回归 {query!r} = {result}")


if __name__ == "__main__":
    print("=== test_parse_type_filter_ui ===")
    test_parse_type_filter_ui()
    print()
    print("=== test_matches_type_filter_ui ===")
    test_matches_type_filter_ui()
    print()
    print("=== test_parse_type_filter_does_not_break_existing ===")
    test_parse_type_filter_does_not_break_existing()
    print()
    print("所有测试通过 ✓")