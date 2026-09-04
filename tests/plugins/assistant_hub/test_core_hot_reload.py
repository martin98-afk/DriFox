# -*- coding: utf-8 -*-
"""test_core_hot_reload.py — core 内部加载器 mtime 自检（热重载防旧缓存）回归。

背景：assistant_hub 的 core 模块经 importlib 手动注册 sys.modules（前缀
assistant_hub_core.*），app 侧热重载的 purge 历史上不覆盖该前缀，命中即返回
会让热更新后的新代码不可见（如 prompts 新增 build_dream_forget）。现四处加载点
（dream._load / compile._core / experience._prompts / ticker._load_session_store）
均带 mtime 自检：磁盘 mtime 更新 → 重新加载替换缓存对象。
"""

import importlib.util
import os
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[3]
_CORE = _ROOT / "plugins" / "assistant_hub" / "core"

_CACHED_KEYS = {
    "assistant_hub_core.memory.prompts",
    "assistant_hub_core.memory.dream",
    "assistant_hub_core.memory.compile",
    "assistant_hub_core.session_store",
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


def test_dream_load_reload_on_mtime_change():
    m = _load_by_path("assistant_hub_core.memory.dream", _CORE / "memory" / "dream.py")
    prompts1 = m._load("memory.prompts", "memory/prompts.py")
    assert getattr(prompts1, "_source_mtime", -1.0) >= 0.0
    _bump_mtime(_CORE / "memory" / "prompts.py")
    prompts2 = m._load("memory.prompts", "memory/prompts.py")
    assert prompts1 is not prompts2
    assert prompts2._source_mtime > prompts1._source_mtime
    # mtime 未再变化 → 命中缓存
    assert m._load("memory.prompts", "memory/prompts.py") is prompts2


def test_compile_core_reload_on_mtime_change():
    m = _load_by_path("assistant_hub_core.memory.compile", _CORE / "memory" / "compile.py")
    store1 = m._core("session_store")
    assert getattr(store1, "_source_mtime", -1.0) >= 0.0
    _bump_mtime(_CORE / "session_store.py")
    store2 = m._core("session_store")
    assert store1 is not store2


def test_experience_prompts_reload_on_mtime_change():
    m = _load_by_path("assistant_hub_core.experience", _CORE / "experience.py")
    p1 = m._prompts()
    assert getattr(p1, "_source_mtime", -1.0) >= 0.0
    _bump_mtime(_CORE / "memory" / "prompts.py")
    p2 = m._prompts("build_consolidate")
    assert p1 is not p2
    # required 兜底：函数缺失时即使 mtime 未变也重载
    del p2.build_consolidate  # 模拟函数缺失（hasattr 对 None 值仍为 True，须真删）
    p3 = m._prompts("build_consolidate")
    assert p3 is not p2


def test_ticker_session_store_reload_on_mtime_change():
    m = _load_by_path("assistant_hub_core.memory.ticker", _CORE / "memory" / "ticker.py")
    s1 = m._load_session_store()
    assert getattr(s1, "_source_mtime", -1.0) >= 0.0
    _bump_mtime(_CORE / "session_store.py")
    s2 = m._load_session_store()
    assert s1 is not s2


def test_purge_module_prefixes():
    """PluginHostService._purge_module_prefixes：按声明前缀清理 sys.modules。"""
    fake = _load_by_path("assistant_hub_core.fake_mod", _CORE / "memory" / "prompts.py")
    sys.modules["assistant_hub_core.fake_mod.sub"] = fake  # 同对象占位，验证前缀匹配
    from app.core.plugin_host_service import PluginHostService

    removed = PluginHostService._purge_module_prefixes(["assistant_hub_core."])
    assert "assistant_hub_core.fake_mod" in removed
    assert "assistant_hub_core.fake_mod" not in sys.modules
    assert "assistant_hub_core.fake_mod.sub" not in sys.modules
