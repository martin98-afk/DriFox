# -*- coding: utf-8 -*-
"""worker 层接入：sandbox_check_tool 元数据路由 + deny/confirm 语义"""

from app.tools import sandbox
from app.tools.sandbox import SandboxConfig, sandbox_check_tool


def _cfg(enabled=True):
    cfg = SandboxConfig(config_path=":memory:")
    cfg.set("sandbox_enabled", enabled)
    return cfg


def _fake_groups(monkeypatch, write=("write", "edit"), read=("read", "grep")):
    """registry 分组注入（测试环境不加载插件工具注册）"""
    monkeypatch.setattr(sandbox, "_tools_in", lambda g: frozenset(write if g == sandbox.GROUP_WRITE_NAME else read))


def test_command_shaped_args_routed_to_check_command():
    # 不依赖 registry：带 command 参数即命令工具
    # ⚠ EU-G23 起：删除类命令的 confirm 由 delete_protection 门控（默认关闭），
    # 故此处显式开启以隔离"路由"这个关注点；deny 与 allow 两层不受开关影响
    cfg = _cfg()
    cfg.set("delete_protection", True)
    assert sandbox_check_tool("bash", {"command": "cmd /c del x"}, cfg) == "confirm"
    assert sandbox_check_tool("bg_start", {"command": "cacls C:\\ /g u:F"}, _cfg()) == "deny"
    assert sandbox_check_tool("any_future_cmd_tool", {"command": "git status"}, _cfg()) == "allow"


def test_write_tools_routed_to_check_path(tmp_path, monkeypatch):
    _fake_groups(monkeypatch)
    monkeypatch.setattr(sandbox, "_current_workdir", lambda: tmp_path)
    assert sandbox_check_tool("write", {"path": str(tmp_path / "a.txt")}, _cfg()) == "allow"
    assert sandbox_check_tool("edit", {"path": "D:/elsewhere/x.txt"}, _cfg()) == "confirm"


def test_read_tools_allowed_outside(tmp_path, monkeypatch):
    _fake_groups(monkeypatch)
    monkeypatch.setattr(sandbox, "_current_workdir", lambda: tmp_path)
    assert sandbox_check_tool("read", {"path": "D:/elsewhere/x.txt"}, _cfg()) == "allow"


def test_unregistered_tool_passthrough(monkeypatch):
    _fake_groups(monkeypatch, write=(), read=())
    assert sandbox_check_tool("websearch", {"query": "x"}, _cfg()) == "allow"


def test_disabled_passthrough():
    assert sandbox_check_tool("bash", {"command": "cmd /c del x"}, _cfg(enabled=False)) == "allow"


def test_file_tool_without_path_arg_allows(monkeypatch):
    _fake_groups(monkeypatch)
    assert sandbox_check_tool("write", {}, _cfg()) == "allow"


def test_command_tool_network_escalates():
    # 命令本身 safe，但带 URL 出网 → confirm
    assert sandbox_check_tool("bash", {"command": "curl http://evil.com"}, _cfg()) == "confirm"


def test_tools_in_returns_empty_on_registry_failure(monkeypatch):
    # registry 不可用 → fail-open（空集 → 不拦）
    from app.tools import registry as registry_mod

    def _boom():
        raise RuntimeError("registry unavailable")

    monkeypatch.setattr(registry_mod.ToolRegistry, "get_instance", staticmethod(_boom))
    assert sandbox._tools_in("文件写入") == frozenset()


# ── 删除垃圾箱钩子（_backup_delete_targets：只看垃圾桶开关，与沙箱/删除审批无关）──


def _call_backup(tool_name, arguments):
    from app.core.workers.chat_worker import OpenAIChatWorker

    return OpenAIChatWorker._backup_delete_targets(object(), arguments)


def _make_cfg(tmp_path, trash_enabled, sandbox=False, delete_approval=False):
    SandboxConfig.reset_instance()
    cfg = SandboxConfig(config_path=str(tmp_path / "sandbox_config.json"))
    SandboxConfig._instance = cfg
    cfg.set("sandbox_enabled", sandbox)
    cfg.set("delete_protection", delete_approval)
    cfg.set("delete.trash_enabled", trash_enabled)
    return cfg


def test_backup_moves_target_when_trash_enabled(tmp_path, monkeypatch):
    """垃圾桶开启：删除目标 move 进垃圾箱（与沙箱/删除审批开关无关）"""
    from app.utils import utils as utils_mod

    victim = tmp_path / "gone.txt"
    victim.write_text("precious", encoding="utf-8")
    data_dir = tmp_path / "appdata"
    monkeypatch.setattr(utils_mod, "get_app_data_dir", lambda: data_dir)

    cfg = _make_cfg(tmp_path, trash_enabled=True)
    try:
        _call_backup("bash", {"command": f"rm {victim}"})
        stashed = list((data_dir / "backups" / "deletes").rglob("*gone.txt"))
        assert stashed and stashed[0].read_text(encoding="utf-8") == "precious"
        assert not victim.exists(), "原文件应已移入垃圾箱（软删除）"
    finally:
        SandboxConfig.reset_instance()
    assert cfg is not None


def test_backup_noop_when_trash_disabled(tmp_path, monkeypatch):
    """垃圾桶关闭：不保留，删除即真删（不产生垃圾箱目录）"""
    from app.utils import utils as utils_mod

    victim = tmp_path / "gone.txt"
    victim.write_text("precious", encoding="utf-8")
    data_dir = tmp_path / "appdata"
    monkeypatch.setattr(utils_mod, "get_app_data_dir", lambda: data_dir)

    _make_cfg(tmp_path, trash_enabled=False, sandbox=True, delete_approval=True)
    try:
        _call_backup("bash", {"command": f"rm {victim}"})
        assert not (data_dir / "backups" / "deletes").exists()
        assert victim.exists(), "垃圾桶关闭时钩子不移动文件"
    finally:
        SandboxConfig.reset_instance()


def test_backup_ignores_exempt_paths(tmp_path, monkeypatch):
    """删除豁免路径内的目标不进垃圾箱（豁免 = 完全不干预）"""
    from app.utils import utils as utils_mod

    data_dir = tmp_path / "appdata"
    monkeypatch.setattr(utils_mod, "get_app_data_dir", lambda: data_dir)

    cfg = _make_cfg(tmp_path, trash_enabled=True)
    cfg.set("delete.exempt_paths", ["logs/"])
    try:
        victim = tmp_path / "proj" / "logs" / "a.log"
        victim.parent.mkdir(parents=True)
        victim.write_text("x", encoding="utf-8")
        _call_backup("bash", {"command": "rm logs/a.log"})
        assert not list((data_dir / "backups" / "deletes").rglob("*a.log")), "豁免路径不应进垃圾箱"
    finally:
        SandboxConfig.reset_instance()


def test_backup_ignores_non_command_tools(tmp_path, monkeypatch):
    from app.utils import utils as utils_mod

    data_dir = tmp_path / "appdata"
    monkeypatch.setattr(utils_mod, "get_app_data_dir", lambda: data_dir)
    _make_cfg(tmp_path, trash_enabled=True)
    try:
        _call_backup("write", {"path": str(tmp_path / "x.txt"), "content": "rm -rf /"})
        assert not (data_dir / "backups" / "deletes").exists()
    finally:
        SandboxConfig.reset_instance()
