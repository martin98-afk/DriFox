# -*- coding: utf-8 -*-
"""插件加载链路冒烟回归（2026-11-27）。

动机（实证）：`_ast_guard.py` 的 P0 修复过程中出现过一次**不可运行中间态**
（该时刻文件 hash `9fabfe2be330`）：关闭磁盘缓存后，8 个用户插件的工具模块
全部抛 `UnboundLocalError: cannot access local variable 'result'`，即门禁主
函数在某个分支上使用了未赋值的局部变量。当时没有任何测试能挡住它。

本文件只做**加载冒烟**，不验收具体判定语义（那属
`tests/plugins/test_ast_guard_cache_safety.py`）：
1. 磁盘缓存关 / 开 两种模式，插件工具模块都必须能加载，不得有
   非预期的拒载或执行异常；
2. 两种模式的「模块 -> 加载结果」集合必须完全一致 —— 即缓存不得改变判定
   （防缓存漂移：磁盘缓存只允许复用纯源码分析产物，放行结论必须每次重算）。

隔离：显式把 `DRIFOX_DATA_DIR` 指向 tmp_path。
为什么要显式：`_ast_guard` 与 `_code_cache` 的落盘路径都走
`get_app_data_dir()`，这是**隐式依赖**——测试本身是纯 Python 进程内加载、
不需要 Qt，很容易被误判为「不必隔离」而写穿到真实 `.drifox`
（2026-11-27 审查中实际发生过一次）。
"""

from __future__ import annotations

import importlib
import sys
from pathlib import Path
from typing import Dict, List, Tuple

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
_PLUGINS_ROOT = _REPO_ROOT / "plugins"

# 与 plugin_tool_loader._iter_tool_modules 保持同口径的跳过规则：
# 下划线开头的辅助模块（_path_utils / __init__ 等）没有 register 入口，
# 生产扫描也跳过，不属于「应加载」集合。
_SKIP_PREFIX = "_"


def _expected_modules() -> List[Tuple[str, Path]]:
    """枚举 plugins/ 下应被扫描到的工具模块（(插件名, 路径)）。"""
    out: List[Tuple[str, Path]] = []
    if not _PLUGINS_ROOT.is_dir():
        return out
    for plugin_dir in sorted(_PLUGINS_ROOT.iterdir()):
        if not plugin_dir.is_dir():
            continue
        tools_dir = plugin_dir / "tools"
        if not tools_dir.is_dir():
            continue
        for py in sorted(tools_dir.glob("*.py")):
            if py.name.startswith(_SKIP_PREFIX):
                continue
            out.append((plugin_dir.name, py))
    return out


def _load_all(loader) -> Dict[str, Dict[str, object]]:
    """用生产 loader 逐个加载，返回 {相对路径: 结果}。"""
    results: Dict[str, Dict[str, object]] = {}
    for plugin_name, py in _expected_modules():
        rel = str(py.relative_to(_REPO_ROOT))
        try:
            module = loader._load_module(plugin_name, py, root_kind="smoke")
        except BaseException as exc:  # noqa: BLE001 — 冒烟要把任何异常都记下来
            results[rel] = {"status": "EXEC_FAIL", "error": f"{type(exc).__name__}: {exc}"}
            continue
        if module is None:
            # loader 返回 None：门禁拒载 / 缺少 register 入口。两者都属非预期，
            # 因为这些文件按生产口径本就在「应加载」集合内。
            results[rel] = {"status": "REJECTED"}
            continue
        results[rel] = {
            "status": "LOADED",
            "has_register": callable(getattr(module, "register", None)),
            "file": Path(getattr(module, "__file__", "") or "").name,
        }
    return results


@pytest.fixture()
def isolated_app_data(monkeypatch, tmp_path):
    """把 app data 指到 tmp_path，并重置两个缓存的进程内状态。

    同时 monkeypatch 两个模块的落盘路径做双保险：模块可能已被其它测试
    先 import 过，单靠环境变量未必生效。
    """
    data_dir = tmp_path / "appdata"
    (data_dir / "cache").mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("DRIFOX_DATA_DIR", str(data_dir))

    guard = importlib.import_module("app.plugins.loaders._ast_guard")
    code_cache = importlib.import_module("app.plugins.loaders._code_cache")

    monkeypatch.setattr(guard, "_disk_cache_path", lambda: data_dir / "cache" / "ast_guard_cache.json")
    monkeypatch.setattr(code_cache, "_cache_path", lambda: data_dir / "cache" / "plugin_tool_code_cache.json")

    # 进程内状态清零：避免其它用例留下的条目让本用例「假命中」
    monkeypatch.setattr(guard, "_guard_cache", {})
    monkeypatch.setitem(guard._disk_cache_state, "loaded", False)
    monkeypatch.setitem(guard._disk_cache_state, "dirty", False)
    monkeypatch.setitem(guard._disk_cache_state, "flush_pending", False)
    monkeypatch.setitem(guard._disk_cache_state, "entries", {})
    monkeypatch.setitem(code_cache._state, "loaded", False)
    monkeypatch.setitem(code_cache._state, "dirty", False)
    monkeypatch.setitem(code_cache._state, "scheduled", False)
    monkeypatch.setitem(code_cache._state, "entries", {})

    yield data_dir


@pytest.fixture()
def loader():
    return importlib.import_module("app.plugins.loaders.plugin_tool_loader")


def _set_disk_guard(monkeypatch, enabled: bool):
    guard = importlib.import_module("app.plugins.loaders._ast_guard")
    monkeypatch.setitem(guard._disk_cache_state, "enabled", enabled)


def test_all_plugin_tools_load_without_disk_guard_cache(isolated_app_data, loader, monkeypatch):
    """磁盘缓存关闭时，plugins/ 下全部工具模块都必须正常加载。

    这条是 P0 中间态（hash 9fabfe2be330）的护栏：那个版本会让多个模块
    在门禁里抛 UnboundLocalError，此处会直接 EXEC_FAIL。
    """
    _set_disk_guard(monkeypatch, False)

    expected = _expected_modules()
    # 前置自检：枚举结果不能为空，否则「全绿」是假绿
    assert len(expected) >= 15, f"应扫描到至少 15 个工具模块，实际 {len(expected)}"

    results = _load_all(loader)

    failures = {k: v for k, v in results.items() if v["status"] != "LOADED"}
    assert not failures, "以下模块未正常加载：\n" + "\n".join(
        f"  [{v['status']}] {k} {v.get('error', '')}" for k, v in sorted(failures.items())
    )

    # 每个加载成功的模块都必须有 register 入口（生产 loader 的约定）
    missing_entry = {k: v for k, v in results.items() if not v.get("has_register")}
    assert not missing_entry, f"以下模块缺少 register 入口：{sorted(missing_entry)}"


def test_all_plugin_tools_load_with_disk_guard_cache(isolated_app_data, loader, monkeypatch):
    """磁盘缓存开启时同样必须全部加载成功（缓存不得引入新的拒载）。"""
    _set_disk_guard(monkeypatch, True)

    expected = _expected_modules()
    assert len(expected) >= 15, f"应扫描到至少 15 个工具模块，实际 {len(expected)}"

    results = _load_all(loader)

    failures = {k: v for k, v in results.items() if v["status"] != "LOADED"}
    assert not failures, "以下模块未正常加载：\n" + "\n".join(
        f"  [{v['status']}] {k} {v.get('error', '')}" for k, v in sorted(failures.items())
    )


def test_guard_disk_cache_does_not_change_verdicts(isolated_app_data, loader, monkeypatch):
    """防缓存漂移：磁盘缓存开 / 关 两种模式的加载结果必须逐模块一致。

    磁盘缓存只允许复用「纯源码分析产物」（writes / has_register /
    dangerous / syntax_error）；放行结论依赖运行时的 sys.modules 与
    find_spec，必须每次现场重算。若有人把 ok 也缓存了，本用例会红。
    """
    _set_disk_guard(monkeypatch, False)
    without_cache = _load_all(loader)

    # 清掉进程内与磁盘状态，模拟「另一个进程」的首次加载
    guard = importlib.import_module("app.plugins.loaders._ast_guard")
    monkeypatch.setattr(guard, "_guard_cache", {})
    monkeypatch.setitem(guard._disk_cache_state, "entries", {})
    monkeypatch.setitem(guard._disk_cache_state, "loaded", False)
    _set_disk_guard(monkeypatch, True)
    with_cache = _load_all(loader)

    assert set(with_cache) == set(without_cache), "两次枚举到的模块集合不一致（枚举不稳定）"

    drifted = {
        rel: (without_cache[rel], with_cache[rel])
        for rel in without_cache
        if without_cache[rel]["status"] != with_cache[rel]["status"]
    }
    assert not drifted, "磁盘缓存改变了加载判定（疑似缓存了放行结论）：\n" + "\n".join(
        f"  {rel}: 无缓存={a['status']} 有缓存={b['status']}" for rel, (a, b) in sorted(drifted.items())
    )


def test_smoke_does_not_write_repo_drifox(isolated_app_data, loader, monkeypatch):
    """冒烟加载不得在仓库内写入真实 .drifox（隔离有效性自检）。"""
    _set_disk_guard(monkeypatch, True)
    _load_all(loader)

    guard = importlib.import_module("app.plugins.loaders._ast_guard")
    code_cache = importlib.import_module("app.plugins.loaders._code_cache")

    # 两个缓存模块的落点必须都指向 tmp_path
    guard_path = guard._disk_cache_path()
    code_path = code_cache._cache_path()
    assert guard_path is not None and str(isolated_app_data) in str(guard_path), guard_path
    assert code_path is not None and str(isolated_app_data) in str(code_path), code_path

    # 仓库根下的 .drifox 不应因本用例新增缓存文件（存在即为写穿）
    repo_cache = _REPO_ROOT / ".drifox" / "cache"
    for name in ("ast_guard_cache.json", "plugin_tool_code_cache.json"):
        candidate = repo_cache / name
        assert not candidate.exists(), f"测试写穿了真实数据目录：{candidate}"


def _modules_writing_sys_modules() -> List[Tuple[str, Path]]:
    """挑出真实会写 sys.modules 的工具模块（它们才受「声明式放行」影响）。

    这类模块是缓存漂移的最佳探针：allowed_sys_modules 变化时，判定必须
    随之变化；若门禁把放行结论缓存了，第二次会复用第一次的 ok。
    """
    guard = importlib.import_module("app.plugins.loaders._ast_guard")
    out: List[Tuple[str, Path]] = []
    for plugin_name, py in _expected_modules():
        try:
            source = py.read_text(encoding="utf-8")
        except OSError:
            continue
        if "sys.modules" not in source:
            continue
        try:
            _key, analysis = guard._analyze_source(source, ("register",))
        except Exception:
            continue
        if analysis.writes:
            out.append((plugin_name, py))
    return out


def test_in_process_cache_drift_on_real_plugins(isolated_app_data, monkeypatch):
    """真实插件上的进程内缓存漂移：声明放行 → 撤销声明，必须立刻改判拒载。

    与 test_guard_disk_cache_does_not_change_verdicts 互补：那条比的是
    「磁盘缓存开/关」，本条比的是「同一进程内、同一文件的第二次调用命中
    进程内缓存」—— 这正是 2026-11-27 P0 的形态（缓存了 GuardResult 而非
    纯分析产物）。
    """
    guard = importlib.import_module("app.plugins.loaders._ast_guard")
    from app.plugins.contracts.manifest_schema import read_manifest_module_prefixes

    probes = _modules_writing_sys_modules()
    # 前置自检：仓库里必须存在这类模块，否则本用例退化成空跑
    assert probes, "未找到任何写 sys.modules 的工具模块，用例失去意义"

    monkeypatch.setattr(guard, "_guard_cache", {})
    monkeypatch.setitem(guard._disk_cache_state, "enabled", False)

    drifted = []
    for plugin_name, py in probes:
        source = py.read_text(encoding="utf-8")
        plugin_dir = py.parent.parent
        declared = read_manifest_module_prefixes(plugin_dir)

        # 第 1 次：按 plugin.json 声明放行（应通过）
        first = guard.guard_plugin_module_once(
            source, py, require_register=True, component="SmokeDrift",
            entry_names=("register",), allowed_sys_modules=declared, plugin_dir=plugin_dir,
        )
        # 第 2 次：撤销声明（应拒载）。此时进程内缓存已有该文件的条目。
        second = guard.guard_plugin_module_once(
            source, py, require_register=True, component="SmokeDrift",
            entry_names=("register",), allowed_sys_modules=(), plugin_dir=plugin_dir,
        )
        if first.ok and second.ok:
            drifted.append(str(py.relative_to(_REPO_ROOT)))

    assert not drifted, (
        "以下模块在撤销 sys.modules 声明后仍被放行（进程内缓存泄漏了放行结论）：\n"
        + "\n".join(f"  {p}" for p in drifted)
    )
