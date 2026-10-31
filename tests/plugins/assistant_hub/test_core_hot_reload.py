# -*- coding: utf-8 -*-
"""test_core_hot_reload.py — core 内部加载器 mtime 自检（热重载防旧缓存）回归。

背景：assistant_hub 的 core 模块经 importlib 手动注册 sys.modules（前缀
assistant_hub_core.*），app 侧热重载的 purge 历史上不覆盖该前缀，命中即返回
会让热更新后的新代码不可见。现加载点（assistant_manager._load_core_module）带 mtime 自检：
磁盘 mtime 更新 → 重新加载替换缓存对象。
"""

import importlib.util
import os
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[3]
_CORE = _ROOT / "plugins" / "assistant_hub" / "core"

_CACHED_KEYS = {
    "assistant_hub_core.memory.prompts",
    "assistant_hub_core.memory.store",
    "assistant_hub_core.experience",
}

# 被 _bump_mtime 推过 mtime 的文件 → 原 (atime, mtime)，teardown 统一还原
_bumped: list = []


def _load_by_path(key: str, path: Path):
    spec = importlib.util.spec_from_file_location(key, str(path))
    mod = importlib.util.module_from_spec(spec)
    sys.modules[key] = mod
    spec.loader.exec_module(mod)
    return mod


def setup_function(fn):
    # 清掉相关缓存键，隔离全局 sys.modules（测试结束还原）
    fn._saved = {k: sys.modules.pop(k, None) for k in _CACHED_KEYS}
    _bumped.clear()


def teardown_function(fn):
    for k, v in fn._saved.items():
        if v is not None:
            sys.modules[k] = v
    # 还原被推到未来的源文件 mtime，避免影响后续真实编辑的重载判定
    while _bumped:
        p, times = _bumped.pop()
        os.utime(p, times)


def _bump_mtime(path: Path):
    """把源文件 mtime 推到未来（自检触发重载）；记录原值供还原。"""
    st = path.stat()
    _bumped.append((path, (st.st_atime, st.st_mtime)))
    future = st.st_mtime + 10.0
    os.utime(path, (future, future))


def test_manager_load_core_module_reload_on_mtime_change():
    """_load_core_module：store.py 更新后重新加载，mtime 未变则命中缓存。"""
    mpath = _ROOT / "plugins" / "assistant_hub" / "assistant_manager.py"
    spec = importlib.util.spec_from_file_location("test_hot_manager_mod", str(mpath))
    mod = importlib.util.module_from_spec(spec)
    sys.modules["test_hot_manager_mod"] = mod
    spec.loader.exec_module(mod)
    s1 = mod._load_core_module("memory.store", "memory/store.py")
    assert getattr(s1, "_source_mtime", -1.0) >= 0.0
    _bump_mtime(_CORE / "memory" / "store.py")
    s2 = mod._load_core_module("memory.store", "memory/store.py")
    assert s1 is not s2
    assert s2._source_mtime > s1._source_mtime
    # mtime 未再变化 → 命中缓存
    assert mod._load_core_module("memory.store", "memory/store.py") is s2




def test_purge_module_prefixes():
    """PluginHostService._purge_module_prefixes：按声明前缀清理 sys.modules。"""
    fake = _load_by_path("assistant_hub_core.fake_mod", _CORE / "memory" / "prompts.py")
    sys.modules["assistant_hub_core.fake_mod.sub"] = fake  # 同对象占位，验证前缀匹配
    from app.core.services.plugin_host_service import PluginHostService

    removed = PluginHostService._purge_module_prefixes(["assistant_hub_core."])
    assert "assistant_hub_core.fake_mod" in removed
    assert "assistant_hub_core.fake_mod" not in sys.modules
    assert "assistant_hub_core.fake_mod.sub" not in sys.modules
