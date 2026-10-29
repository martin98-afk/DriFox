# -*- coding: utf-8 -*-
"""回归守卫：已升 api_version=2 的插件必须走能力池，不得自建池或越界摸宿主。

背景（2026-10-29 全量收敛）：
多个插件各自复制了同一段 `_caps()` 样板——建 `host=None` 的池、并且**自报**
`declared={cap_id, "state.kv", "plugin.log"}`。这有两个后果：

1. 授权形同虚设：插件自己决定自己有哪些能力，manifest 的 `capabilities`
   声明永远不参与裁剪，审核侧看到的清单与实际能拿到的能力是两回事。
2. `window.*` 这类吃 host 的能力恒为空（`host=None` 建池拿不到窗口），
   插件于是再各自写 `ctx["main_widget"]` 之类的兜底，绕过声明。

现约定：插件一律 `from app.plugins.sdk import caps_for`（host 由宿主按插件名
解析）；service 类需要「绑定到调用方传入的那个窗口」时用 `sdk.pool_for`
显式传 host。

本测试扫全部 `api_version >= 2` 的插件，守住四条：
1. 不得再出现 `build_pool(` + `declared=` 的自建池
2. 不得再直连 `HistoryManager` 等主程序单例
3. manifest 声明必须覆盖源码里实际用到的每个 `caps_for("x")` / `_caps("x")`
4. 声明里不得有 catalog 不认识的 ID（typo 在 CI 就炸，不到运行时静默失效）
"""

import ast
import json
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
PLUGINS = REPO / "plugins"

# 能力取用的函数名（含各插件自定义的薄封装）
_CAP_FUNCS = ("caps_for", "_caps", "pool_for", "_cap")


def _manifests():
    """所有声明了 api_version >= 2 的插件目录。"""
    for mf in sorted(PLUGINS.glob("*/.drifox-plugin/plugin.json")):
        try:
            data = json.loads(mf.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if int(data.get("api_version") or 1) >= 2:
            yield mf.parent.parent, data


def _plugin_sources(plugin_dir: Path):
    for p in sorted(plugin_dir.rglob("*.py")):
        if "__pycache__" in p.parts or "deps" in p.parts:
            continue
        yield p, p.read_text(encoding="utf-8", errors="ignore")


def _declared_usage(plugin_dir: Path) -> set:
    """源码里通过能力入口字面量取到的能力 ID 集合。

    只取 **第一个位置参数**；``pool_for("worktree-manager", surface="ui", host=..)``
    这类调用的首参是插件名不是能力 ID，故按「含点」过滤——catalog 的 ID 一律
    形如 ``<group>.<name>``，插件名不含点。
    """
    used = set()
    for _path, src in _plugin_sources(plugin_dir):
        try:
            tree = ast.parse(src)
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id in _CAP_FUNCS
                and node.args
                and isinstance(node.args[0], ast.Constant)
                and isinstance(node.args[0].value, str)
            ):
                cap_id = node.args[0].value
                if "." in cap_id:
                    used.add(cap_id)
    return used


def _all_plugins():
    return list(_manifests())


def test_plugins_are_discovered():
    """哨兵：确认扫描确实命中已迁移插件（防止 glob 写错导致测试空跑）。"""
    names = {d.name for d, _ in _all_plugins()}
    assert {"history-manager", "artifacts-manager", "welcome_changelog", "worktree-manager"} <= names


@pytest.mark.parametrize("plugin_dir,manifest", _all_plugins(), ids=lambda v: getattr(v, "name", ""))
def test_no_self_granted_capability_pool(plugin_dir, manifest):
    """不得自建 host=None 池并自报 declared（等于插件自己给自己发权限）。"""
    bad = []
    for path, src in _plugin_sources(plugin_dir):
        if "build_pool(" in src and "declared=" in src:
            bad.append(path.relative_to(plugin_dir).as_posix())
    assert not bad, f"{plugin_dir.name} 仍自建能力池自报声明: {bad}"


@pytest.mark.parametrize("plugin_dir,manifest", _all_plugins(), ids=lambda v: getattr(v, "name", ""))
def test_no_direct_history_manager_singleton(plugin_dir, manifest):
    """不得直连持久化历史单例（数据面必须走 session.history 能力）。"""
    bad = []
    for path, src in _plugin_sources(plugin_dir):
        if "HistoryManager.get_instance" in src or "history_manager import HistoryManager" in src:
            bad.append(path.relative_to(plugin_dir).as_posix())
    assert not bad, f"{plugin_dir.name} 仍直连 HistoryManager: {bad}"


@pytest.mark.parametrize("plugin_dir,manifest", _all_plugins(), ids=lambda v: getattr(v, "name", ""))
def test_declared_capabilities_cover_usage(plugin_dir, manifest):
    """manifest 声明必须覆盖源码里用到的全部能力 ID。"""
    declared = set(manifest.get("capabilities") or [])
    used = _declared_usage(plugin_dir)
    missing = used - declared
    assert not missing, f"{plugin_dir.name} 用到但未在 manifest 声明: {sorted(missing)}"


@pytest.mark.parametrize("plugin_dir,manifest", _all_plugins(), ids=lambda v: getattr(v, "name", ""))
def test_declared_capabilities_exist_in_catalog(plugin_dir, manifest):
    """声明里的每个 ID 都必须是 catalog 真有的（typo 在 CI 阶段就炸）。"""
    from app.plugins.capabilities import catalog

    declared = manifest.get("capabilities")
    if declared is None:
        return
    unknown = catalog.unknown_declared(declared)
    assert not unknown, f"{plugin_dir.name} 声明了未知能力: {unknown}"


@pytest.mark.parametrize(
    "cap_id",
    ["project.workdir", "session.history", "session.history.write", "project.read", "project.write", "team.session"],
)
def test_new_capabilities_registered(cap_id):
    """本次新增的能力都必须在 catalog 登记、ui surface 注册且 provider 可构造。"""
    from unittest.mock import MagicMock

    from app.plugins.capabilities import bootstrap, catalog
    from app.plugins.capabilities.registry import CapabilityRegistry

    CapabilityRegistry.reset_instance()
    bootstrap.reset_bootstrap()
    try:
        reg = CapabilityRegistry.get_instance()
        bootstrap.ensure_registered("ui")
        assert catalog.get(cap_id) is not None, f"{cap_id} 未在 catalog 登记"
        assert reg.has(cap_id, "ui"), f"{cap_id} 未在 ui surface 注册"
        pool = reg.build_pool(surface="ui", host=MagicMock(), plugin="probe", declared={cap_id})
        assert pool[cap_id] is not None, f"{cap_id} provider 返回 None"
    finally:
        CapabilityRegistry.reset_instance()
        bootstrap.reset_bootstrap()


def test_project_workdir_restores_root_mark_only_for_non_worktree():
    """project.workdir.switch 的根目录标记恢复只针对非 worktree 旧目录。

    这是从 worktree-manager 抽出来的宿主语义：旧工作目录若本身是 worktree，
    给它恢复 is_working_dir 标记会把记忆卡片的根目录指到 worktree 上。
    """
    from unittest.mock import MagicMock

    from app.plugins.capabilities import bootstrap
    from app.plugins.capabilities.registry import CapabilityRegistry

    CapabilityRegistry.reset_instance()
    bootstrap.reset_bootstrap()
    try:
        reg = CapabilityRegistry.get_instance()
        bootstrap.ensure_registered("ui")

        def _api(prev: str, docs: list):
            mm = MagicMock()
            mm.get_working_directory.return_value = prev
            mm.get_key_documents.return_value = docs
            host = MagicMock()
            host._current_project = "P"
            host.backend.memory_manager = mm
            pool = reg.build_pool(surface="ui", host=host, plugin="probe", declared={"project.workdir"})
            return pool["project.workdir"], mm

        # 旧目录是普通根目录 → 恢复标记
        api, mm = _api("/root", [])
        assert api["switch"]("P", "/wt/a") is True
        mm.restore_working_directory_mark.assert_called_once_with("P", "/root")

        # 旧目录本身是 worktree → 不恢复标记
        api, mm = _api("/wt/old", [{"file_path": "/wt/old", "added_by": "git_worktree"}])
        assert api["switch"]("P", "/wt/b") is True
        mm.restore_working_directory_mark.assert_not_called()
    finally:
        CapabilityRegistry.reset_instance()
        bootstrap.reset_bootstrap()
