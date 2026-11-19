# -*- coding: utf-8 -*-
"""[E7-A] cfg() 共享 store 实例 + 读盘次数改造对照。

背景数字（素材卡 A）：tier 4 层 + budget 每投影 10~20 次同步读盘，30 轮工具
迭代数百次。改造两刀：① PluginConfigStore._read_raw 加 (path, mtime_ns) 进程级
缓存；② cfg() 从每次 new PluginConfigStore 改为模块级共享实例。
"""

import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.plugins.contracts.plugin_config import parse_config_schema  # noqa: E402
from app.plugins.registries.plugin_config_registry import PluginConfigRegistry  # noqa: E402

@pytest.fixture()
def cfg_env(tmp_path, monkeypatch):
    """隔离 app_data_dir + 注册 system-context 测试 schema + 清模块缓存。"""
    monkeypatch.setattr("app.utils.utils.get_app_data_dir", lambda: str(tmp_path))
    import app.core.context.config as cfg_mod
    import app.plugins.managers.plugin_config_store as pcs

    pcs._read_cache.clear()
    cfg_mod._cfg_store = None  # 复位共享实例（防其它测试污染）
    reg = PluginConfigRegistry.get_instance()
    fields = {f"key{i}": {"key": f"key{i}", "label": f"k{i}", "type": "text", "default": f"d{i}"} for i in range(10)}
    reg.register(
        parse_config_schema(
            "system-context",
            {"title": "ctx", "fields": list(fields.values())},
        )
    )
    yield tmp_path
    reg.unregister_plugin("system-context")
    pcs._read_cache.clear()
    cfg_mod._cfg_store = None

def test_cfg_uses_shared_store_instance(cfg_env):
    """cfg() 连续调用复用同一 store 实例（不再每次 new）。"""
    import app.core.context.config as cfg_mod
    from app.core.context.config import cfg

    cfg("key0")
    s1 = cfg_mod._cfg_store
    assert s1 is not None
    cfg("key1")
    assert cfg_mod._cfg_store is s1
    assert cfg_mod._get_store() is s1

def test_cfg_read_count_before_after(tmp_path, monkeypatch):
    """读盘次数对照：同 store 10 个 key × 2 轮，无缓存 20 次 parse，有缓存 10 次。

    读盘口径 = PluginConfigStore._read_raw 内 json.loads 调用数（read_text+parse
    是读盘主成本；mtime stat 本身不 parse）。改造前（每次读都 parse）与改造后
    （命中走缓存）在同一进程内对照：先跑一轮禁用缓存，再跑一轮启用缓存。
    """
    import json as json_mod

    import app.plugins.managers.plugin_config_store as pcs
    from app.core.context.config import cfg

    monkeypatch.setattr("app.utils.utils.get_app_data_dir", lambda: str(tmp_path))
    reg = PluginConfigRegistry.get_instance()
    reg.register(
        parse_config_schema(
            "system-context",
            {
                "title": "ctx",
                "fields": [
                    {"key": f"key{i}", "label": f"k{i}", "type": "text", "default": f"d{i}"}
                    for i in range(10)
                ],
            },
        )
    )
    try:
        keys = [f"key{i}" for i in range(10)]
        calls = {"n": 0}
        real_loads = json_mod.loads

        def counting(*a, **k):
            calls["n"] += 1
            return real_loads(*a, **k)

        monkeypatch.setattr(pcs.json, "loads", counting)

        # 先写入实际数据：文件不存在时 get 走 schema 默认兜底，不读盘
        store = pcs.PluginConfigStore()
        store.set_values("system-context", {f"key{i}": f"v{i}" for i in range(10)})

        # 改造前口径：缓存条目禁用（每次 _read_raw 强制 miss）→ 每次读都 parse
        monkeypatch.setattr(pcs, "_read_cache", _AlwaysMissCache())
        for _ in range(2):
            for k in keys:
                cfg(k)
        before = calls["n"]
        assert before == 20, f"无缓存口径 10 key × 2 轮应 20 次读盘，实测 {before}"

        # 改造后口径：真实缓存。同一插件 10 个 key 共读同一 config.json，
        # 首轮首读 parse 1 次填缓存，其余 19 次 cfg 全命中 → 1 次。
        monkeypatch.setattr(pcs, "_read_cache", {})
        calls["n"] = 0
        for _ in range(2):
            for k in keys:
                cfg(k)
        after = calls["n"]
        assert after == 1, f"同插件 10 key × 2 轮应仅首读 1 次 parse，实测 {after}"
        print(f"\n[E7-A 读盘对照] 10 key × 2 轮投影：改造前 {before} 次 parse → 改造后 {after} 次（省 {before - after} 次）")
    finally:
        reg.unregister_plugin("system-context")

class _AlwaysMissCache(dict):
    """模拟改造前：get 永远 miss（读盘计数对照用）。"""

    def get(self, key, default=None):
        return default

if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
