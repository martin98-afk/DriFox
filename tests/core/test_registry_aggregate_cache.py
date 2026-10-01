# -*- coding: utf-8 -*-
"""聚合缓存契约测试（PERF T32）

registry 的六个聚合查询（team_only_tools / provides_image_tools /
tools_in_group / keep_in_content_tools / dangerous_tools / safe_tools）
原为每次 O(N) 全表扫描，权限链/渲染链在流式循环里反复调用 → O(N²)。
T32 引入 _version 驱动的惰性聚合缓存，本测试锁定其契约：

- register / unregister 后缓存必须失效（不得返回陈旧集合）
- notify_batch 内的多次变更不得吞掉 version（批后读必须见新状态）
- 返回不可变 frozenset（调用方无法污染缓存）
- 同一 version 下多次调用返回恒等对象（真缓存，非每调重建）
- 热重载场景：注销后重注册归属切换（danger 变化）必须即时生效
- ToolNameMapper 别名缓存：registry 变化失效 + 公开面拷贝隔离

运行: python -m pytest tests/core/test_registry_aggregate_cache.py -v
"""

import os
import sys
import threading
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from app.tools.registry import DANGER_DANGEROUS, DANGER_SAFE, ToolRegistry  # noqa: E402


@pytest.fixture(autouse=True)
def fresh_registry():
    ToolRegistry.reset_instance()
    yield
    ToolRegistry.reset_instance()


def _schema(name: str) -> dict:
    return {"type": "function", "function": {"name": name, "parameters": {"type": "object", "properties": {}}}}


def _reg(name: str, **kwargs):
    """注册一个测试工具（插件源，danger 显式声明）"""
    kwargs.setdefault("danger", DANGER_SAFE)
    kwargs.setdefault("source", "plugin:test")
    return ToolRegistry.get_instance().register(name, _schema(name), **kwargs)


class TestAggregateInvalidation:
    """register / unregister 必须让聚合缓存失效"""

    def test_register_invalidates_team_only(self):
        reg = ToolRegistry.get_instance()
        assert reg.team_only_tools() == frozenset()
        _reg("agg_team_a", team_only=True)
        assert reg.team_only_tools() == frozenset({"agg_team_a"})

    def test_unregister_invalidates_team_only(self):
        reg = ToolRegistry.get_instance()
        _reg("agg_team_b", team_only=True)
        assert "agg_team_b" in reg.team_only_tools()
        reg.unregister("agg_team_b")
        assert "agg_team_b" not in reg.team_only_tools(), "unregister 后聚合缓存未失效"

    def test_register_invalidates_danger_split(self):
        reg = ToolRegistry.get_instance()
        _reg("agg_safe_x", danger=DANGER_SAFE)
        assert "agg_safe_x" in reg.safe_tools()
        assert "agg_safe_x" not in reg.dangerous_tools()
        _reg("agg_danger_y", danger=DANGER_DANGEROUS)
        assert "agg_danger_y" in reg.dangerous_tools()
        assert "agg_danger_y" not in reg.safe_tools()

    def test_register_invalidates_group_and_keep_and_image(self):
        reg = ToolRegistry.get_instance()
        _reg("agg_g_one", group="测试组", keep_in_content=True, metadata={"provides_image": True})
        assert reg.tools_in_group("测试组") == frozenset({"agg_g_one"})
        assert "agg_g_one" in reg.keep_in_content_tools()
        assert "agg_g_one" in reg.provides_image_tools()
        reg.unregister("agg_g_one")
        assert reg.tools_in_group("测试组") == frozenset(), "组聚合未失效"
        assert "agg_g_one" not in reg.keep_in_content_tools()
        assert "agg_g_one" not in reg.provides_image_tools()


class TestNotifyBatchVersion:
    """notify_batch 期间的多次变更不得吞掉 version（否则批后读陈旧）"""

    def test_batch_registrations_visible_after_batch(self):
        reg = ToolRegistry.get_instance()
        reg.team_only_tools()  # 预热缓存（version=N）
        with reg.notify_batch():
            _reg("agg_batch_1", team_only=True)
            _reg("agg_batch_2", team_only=True)
            _reg("agg_batch_3", team_only=True)
        got = reg.team_only_tools()
        assert got == frozenset({"agg_batch_1", "agg_batch_2", "agg_batch_3"}), "notify_batch 后聚合缓存陈旧"

    def test_batch_unregister_visible_after_batch(self):
        reg = ToolRegistry.get_instance()
        _reg("agg_del_1", team_only=True)
        _reg("agg_del_2", team_only=True)
        reg.team_only_tools()  # 预热
        with reg.notify_batch():
            reg.unregister("agg_del_1")
            reg.unregister("agg_del_2")
        assert reg.team_only_tools() == frozenset(), "notify_batch 内 unregister 未反映"

    def test_version_monotonic_across_batch(self):
        """version 单调性：批内多次变更逐次自增（不得被 batch 压成一次）"""
        reg = ToolRegistry.get_instance()
        v0 = reg.version()
        with reg.notify_batch():
            _reg("agg_v_1")
            v1 = reg.version()
            _reg("agg_v_2")
            v2 = reg.version()
        assert v1 == v0 + 1 and v2 == v0 + 2, f"version 单调性被破坏: {v0} → {v1} → {v2}"


class TestImmutableContract:
    """返回不可变 frozenset（调用方无法污染缓存）"""

    def test_returned_sets_are_frozenset(self):
        reg = ToolRegistry.get_instance()
        _reg("agg_frozen_a", team_only=True, keep_in_content=True, group="冻组")
        for name, got in (
            ("team_only_tools", reg.team_only_tools()),
            ("safe_tools", reg.safe_tools()),
            ("dangerous_tools", reg.dangerous_tools()),
            ("keep_in_content_tools", reg.keep_in_content_tools()),
            ("provides_image_tools", reg.provides_image_tools()),
            ("tools_in_group", reg.tools_in_group("冻组")),
        ):
            assert isinstance(got, frozenset), f"{name} 应返回 frozenset，实为 {type(got)}"

    def test_caller_cannot_mutate_cache(self):
        reg = ToolRegistry.get_instance()
        _reg("agg_mut_a", team_only=True)
        got = reg.team_only_tools()
        with pytest.raises(AttributeError):
            got.add("agg_injected")  # type: ignore[attr-defined]
        assert "agg_injected" not in reg.team_only_tools()

    def test_tools_in_group_unknown_returns_empty_frozenset(self):
        reg = ToolRegistry.get_instance()
        _reg("agg_known", group="已知组")
        assert reg.tools_in_group("不存在组") == frozenset()


class TestCacheIdentity:
    """同一 version 下多次调用返回恒等对象（真缓存，非每调重建）"""

    def test_same_version_returns_identical_object(self):
        reg = ToolRegistry.get_instance()
        _reg("agg_ident_a", team_only=True, keep_in_content=True, group="恒等组")
        assert reg.team_only_tools() is reg.team_only_tools()
        assert reg.keep_in_content_tools() is reg.keep_in_content_tools()
        assert reg.safe_tools() is reg.safe_tools()
        assert reg.dangerous_tools() is reg.dangerous_tools()
        assert reg.provides_image_tools() is reg.provides_image_tools()
        assert reg.tools_in_group("恒等组") is reg.tools_in_group("恒等组")

    def test_version_change_returns_new_object(self):
        reg = ToolRegistry.get_instance()
        _reg("agg_ident_b", team_only=True)
        first = reg.team_only_tools()
        _reg("agg_ident_c", team_only=True)
        second = reg.team_only_tools()
        assert first is not second, "version 变化后应重建（不得复用旧对象）"
        assert second == frozenset({"agg_ident_b", "agg_ident_c"})


class TestHotReloadDangerSwitch:
    """热重载：注销后重注册同一工具、danger 翻转必须即时生效"""

    def test_danger_flip_after_reregister(self):
        reg = ToolRegistry.get_instance()
        _reg("agg_flip", danger=DANGER_SAFE)
        assert "agg_flip" in reg.safe_tools()
        reg.unregister("agg_flip")
        _reg("agg_flip", danger=DANGER_DANGEROUS)
        assert "agg_flip" in reg.dangerous_tools(), "重注册后 danger 翻转未生效（聚合缓存陈旧）"
        assert "agg_flip" not in reg.safe_tools()

    def test_clear_resets_aggregates(self):
        reg = ToolRegistry.get_instance()
        _reg("agg_clear_a", team_only=True)
        _reg("agg_clear_b", team_only=True)
        assert len(reg.team_only_tools()) == 2
        reg.clear()
        assert reg.team_only_tools() == frozenset(), "clear 后聚合缓存未失效"


class TestAliasMapCache:
    """ToolNameMapper 别名缓存：失效正确 + 公开面拷贝隔离"""

    def test_alias_cache_invalidated_on_registry_change(self):
        from app.tools.tool_name_mapper import ToolNameMapper

        _reg("agg_alias_new", aliases=["AggAliasNew"])
        assert ToolNameMapper.to_native("AggAliasNew") == "agg_alias_new"
        # 注销后别名不应再映射
        ToolRegistry.get_instance().unregister("agg_alias_new")
        assert ToolNameMapper.to_native("AggAliasNew") == "AggAliasNew", "registry 变化后别名缓存未失效"

    def test_alias_map_public_copy_isolation(self):
        """ALIAS_MAP 公开面为拷贝：调用方改写不得污染内部缓存"""
        from app.tools.tool_name_mapper import ToolNameMapper

        _reg("agg_alias_iso", aliases=["AggAliasIso"])
        public = ToolNameMapper.ALIAS_MAP
        public["agg_alias_iso"].append("InjectedAlias")
        public["agg_ghost"] = ["Ghost"]
        # 再次通过映射器查询：注入的别名不得生效
        assert ToolNameMapper.to_native("InjectedAlias") == "InjectedAlias"
        assert "agg_ghost" not in ToolNameMapper.ALIAS_MAP
        assert ToolNameMapper.ALIAS_MAP["agg_alias_iso"] == ["AggAliasIso"]

    def test_register_alias_invalidates_cache(self):
        from app.tools.tool_name_mapper import ToolNameMapper

        # 先建立缓存
        ToolNameMapper.to_native("Read")
        ToolNameMapper.register_alias("_agg_extra_target", "AggExtraAlias")
        assert ToolNameMapper.to_native("AggExtraAlias") == "_agg_extra_target"
        ToolNameMapper._extra_aliases.pop("_agg_extra_target", None)


class TestConcurrentReadSmoke:
    """并发读 smoke：多线程同版本读不得抛异常、结果一致"""

    def test_concurrent_reads_consistent(self):
        reg = ToolRegistry.get_instance()
        for i in range(20):
            _reg(f"agg_conc_{i}", team_only=(i % 2 == 0), group="并发组")
        errors = []
        results = []

        def reader():
            try:
                results.append((reg.team_only_tools(), reg.tools_in_group("并发组")))
            except Exception as e:  # noqa: BLE001
                errors.append(e)

        threads = [threading.Thread(target=reader) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5.0)

        assert not errors, f"并发读异常: {errors}"
        assert len(results) == 8
        first, first_group = results[0]
        assert all(r[0] == first and r[1] == first_group for r in results), "并发读结果不一致"
        assert len(first) == 10 and len(first_group) == 20
