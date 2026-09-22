# -*- coding: utf-8 -*-
"""安全中心卡 smoke 测试：实例化 / 配置读写联动 / 名单编辑回调"""

import sys

import pytest
from PyQt5.QtWidgets import QApplication


def _ensure_qapp():
    return QApplication.instance() or QApplication(sys.argv)


@pytest.fixture(scope="session")
def _app_keepalive():
    """进程级持有 QApplication 引用（防 GC 销毁后重建损坏 Qt 全局状态，见 test_command_card_pin.py）"""
    app = _ensure_qapp()
    yield app


@pytest.fixture(autouse=True)
def _qapp(_app_keepalive):
    yield


@pytest.fixture
def isolated_cfg(tmp_path, monkeypatch):
    """隔离 SandboxConfig：单例指向 tmp_path 配置文件（沙箱默认显式开启）"""
    from app.tools.sandbox import SandboxConfig

    SandboxConfig.reset_instance()
    cfg = SandboxConfig(config_path=str(tmp_path / "sandbox_config.json"))
    cfg.set("sandbox_enabled", True)  # 测试关注开启态行为，不依赖全局默认值
    monkeypatch.setattr(SandboxConfig, "_instance", cfg)
    yield cfg
    SandboxConfig.reset_instance()


@pytest.fixture
def data_dir(tmp_path, monkeypatch):
    """隔离 app_data_dir（快照统计/清理都基于它）"""
    from app.utils import utils as utils_mod

    d = tmp_path / "appdata"
    d.mkdir()
    monkeypatch.setattr(utils_mod, "get_app_data_dir", lambda: d)
    return d


def _make_card():
    from app.widgets.cards.settings.security_center_card import SecurityCenterCard

    return SecurityCenterCard()


def test_card_instantiates_and_binds_config(isolated_cfg):
    """总开关反映配置；切开关写回配置"""
    from app.widgets.cards.settings.security_center_card import SecurityCenterCard

    card = SecurityCenterCard()
    # 总开关反映配置（fixture 显式开启）
    assert card.sandbox_switch.isChecked() is True
    # 切开关写回配置
    card.sandbox_switch.setChecked(False)
    assert isolated_cfg.get("sandbox_enabled") is False
    # 删除保护与系统豁免同理
    card.delete_switch.setChecked(False)
    assert isolated_cfg.get("delete_protection") is False
    card.sys_switch.setChecked(True)
    assert isolated_cfg.get("sys_tools_bypass") is True


def test_path_list_editor_roundtrip(isolated_cfg):
    """名单增删回调写回配置（card 级接口按 key 路由）"""
    from app.widgets.cards.settings.security_center_card import SecurityCenterCard

    card = SecurityCenterCard()
    card._add_path_entry("whitelist", "D:/other_proj")
    assert "D:/other_proj" in isolated_cfg.get("path.whitelist")
    card._remove_path_entry("whitelist", 0)
    assert isolated_cfg.get("path.whitelist") == []


def test_command_and_network_lists_route_to_config(isolated_cfg):
    """命令/网络名单编辑路由到正确的配置键"""
    from app.widgets.cards.settings.security_center_card import SecurityCenterCard

    card = SecurityCenterCard()
    card._add_path_entry("allow_prefixes", "git")
    card._add_path_entry("confirm_prefixes", "git push")
    card._add_path_entry("blacklist_domains", "evil.com")
    assert isolated_cfg.get("command.allow_prefixes") == ["git"]
    assert isolated_cfg.get("command.confirm_prefixes") == ["git push"]
    assert isolated_cfg.get("network.blacklist_domains") == ["evil.com"]


def test_duplicate_add_is_idempotent(isolated_cfg):
    """重复添加同一名单条目不产生重复项"""
    from app.widgets.cards.settings.security_center_card import SecurityCenterCard

    card = SecurityCenterCard()
    card._add_path_entry("blacklist", "D:/secrets")
    card._add_path_entry("blacklist", "D:/secrets")
    assert isolated_cfg.get("path.blacklist") == ["D:/secrets"]


# ══════════════ EU-G12 网络外传检测开关 ══════════════


def test_network_enabled_toggle(isolated_cfg):
    """切网络开关 → 配置写回 network.enabled"""
    card = _make_card()
    assert card.net_switch.isChecked() is True  # 默认 True
    card.net_switch.setChecked(False)
    assert isolated_cfg.get("network.enabled") is False
    card.net_switch.setChecked(True)
    assert isolated_cfg.get("network.enabled") is True


def test_network_switch_reflects_config(isolated_cfg):
    """开关初值反映配置（非硬编码）"""
    isolated_cfg.set("network.enabled", False)
    card = _make_card()
    assert card.net_switch.isChecked() is False


# ══════════════ EU-G9 job_limits 三项 ══════════════


def test_job_limits_spin_reflects_config(isolated_cfg):
    """三个 SpinBox 初值反映配置"""
    card = _make_card()
    assert card.job_spins["memory_mb"].value() == 2048
    assert card.job_spins["active_process"].value() == 64
    assert card.job_spins["cpu_time_ms"].value() == 0


def test_job_limits_spin_writes_config(isolated_cfg):
    """改 SpinBox → 写回 job_limits 三键（且不丢其它键）"""
    card = _make_card()
    card.job_spins["memory_mb"].setValue(4096)
    card.job_spins["active_process"].setValue(128)
    card.job_spins["cpu_time_ms"].setValue(30000)

    limits = isolated_cfg.get("job_limits")
    assert limits["memory_mb"] == 4096
    assert limits["active_process"] == 128
    assert limits["cpu_time_ms"] == 30000


def test_job_limits_ranges(isolated_cfg):
    """范围与步进符合规格（防溢出 SIZE_T）"""
    card = _make_card()
    assert card.job_spins["memory_mb"].minimum() == 0
    assert card.job_spins["memory_mb"].maximum() == 65536
    assert card.job_spins["active_process"].maximum() == 1024
    assert card.job_spins["cpu_time_ms"].maximum() == 3600000


def test_job_limits_change_persists_without_restart(isolated_cfg):
    """改动即时落盘：create_quota_job 下次读取即可见（无需重启）"""
    card = _make_card()
    card.job_spins["memory_mb"].setValue(512)
    from app.tools.sandbox import SandboxConfig

    assert SandboxConfig.get_instance().get("job_limits.memory_mb") == 512


# ══════════════ EU-G10 备份上限 + 立即清理 ══════════════


def test_backup_limit_spin_writes_config(isolated_cfg):
    card = _make_card()
    card.backup_limit_spin.setValue(5000)
    assert isolated_cfg.get("backup_limit_mb") == 5000


def test_backup_limit_spin_reflects_config(isolated_cfg):
    isolated_cfg.set("backup_limit_mb", 1234)
    card = _make_card()
    assert card.backup_limit_spin.value() == 1234


def test_clean_btn_disabled_when_limit_zero(isolated_cfg):
    """0 = 不限 → 清理按钮禁用 + tooltip 说明（UI 层消歧）"""
    card = _make_card()
    card.backup_limit_spin.setValue(0)
    assert card.clean_btn.isEnabled() is False
    assert "不限" in card.clean_btn.toolTip()

    card.backup_limit_spin.setValue(500)
    assert card.clean_btn.isEnabled() is True
    assert card.clean_btn.toolTip() == ""


def test_clean_btn_noop_when_limit_zero(isolated_cfg, data_dir, monkeypatch):
    """limit=0 时点击不触发清理（双保险，防止绕过禁用态调用）"""
    calls = []
    import app.utils.file_operation_recorder as rec_mod

    monkeypatch.setattr(rec_mod, "cleanup_backups_partitioned", lambda *a, **k: calls.append((a, k)) or {})
    card = _make_card()
    card.backup_limit_spin.setValue(0)
    card._on_clean_backups()
    assert calls == [], "0 = 不限时不得执行清理"


def test_clean_btn_calls_cleanup_partitioned(isolated_cfg, data_dir, monkeypatch):
    """点清理 → 调 cleanup_backups_partitioned 且路径指向 backups 根、limit 传当前值"""
    calls = []
    import app.utils.file_operation_recorder as rec_mod

    def _fake(backups_dir, limit_mb):
        calls.append((backups_dir, limit_mb))
        return {"file_backups_removed": [], "snapshots_removed": [], "snapshot_limit_mb": 0}

    monkeypatch.setattr(rec_mod, "cleanup_backups_partitioned", _fake)
    card = _make_card()
    card.backup_limit_spin.setValue(777)
    card._on_clean_backups()
    assert len(calls) == 1
    path, limit = calls[0]
    assert str(path).endswith("backups")
    assert limit == 777


def test_backup_hint_text_reflects_limit(isolated_cfg):
    card = _make_card()
    card.backup_limit_spin.setValue(0)
    assert "未设上限" in card.backup_hint.text()
    card.backup_limit_spin.setValue(3000)
    assert "3000 MB" in card.backup_hint.text()


# ══════════════ EU-G11 删除快照 ══════════════


def test_deleted_snapshot_stats_empty(isolated_cfg, data_dir):
    """目录不存在 → (0, 0)"""
    from app.widgets.cards.settings.security_center_card import SecurityCenterCard

    assert SecurityCenterCard._deleted_dir_stats() == (0, 0)


def test_deleted_snapshot_stats_counts(isolated_cfg, data_dir):
    """造 tmp 目录 + 文件 → (N, bytes)"""
    from app.widgets.cards.settings.security_center_card import SecurityCenterCard

    d = data_dir / "backups" / "deleted"
    d.mkdir(parents=True)
    (d / "20260921_120000_a.txt").write_bytes(b"x" * 100)
    (d / "20260921_120001_b.txt").write_bytes(b"y" * 250)
    count, size = SecurityCenterCard._deleted_dir_stats()
    assert count == 2
    assert size == 350


def test_deleted_snapshot_stats_recurses(isolated_cfg, data_dir):
    """目录型快照（copytree 产物）应递归统计"""
    from app.widgets.cards.settings.security_center_card import SecurityCenterCard

    sub = data_dir / "backups" / "deleted" / "20260921_120000_dir" / "inner"
    sub.mkdir(parents=True)
    (sub / "f.txt").write_bytes(b"z" * 10)
    count, size = SecurityCenterCard._deleted_dir_stats()
    assert count == 1
    assert size == 10


def test_snapshot_label_shows_stats(isolated_cfg, data_dir):
    d = data_dir / "backups" / "deleted"
    d.mkdir(parents=True)
    (d / "a.txt").write_bytes(b"x" * 1024)
    card = _make_card()
    text = card.snapshot_label.text()
    assert "1 项" in text
    assert "MB" in text
    assert "恢复" in text, "应提示快照的恢复价值"


def test_snapshot_clear_btn_disabled_when_empty(isolated_cfg, data_dir):
    card = _make_card()
    assert card.snapshot_clear_btn.isEnabled() is False
    assert "没有快照" in card.snapshot_clear_btn.toolTip()


def test_snapshot_clear_requires_confirmation(isolated_cfg, data_dir, monkeypatch):
    """清空必须走二次确认：用户取消 → 文件保留"""
    d = data_dir / "backups" / "deleted"
    d.mkdir(parents=True)
    target = d / "keep.txt"
    target.write_bytes(b"important")

    card = _make_card()
    assert card.snapshot_clear_btn.isEnabled() is True

    import app.widgets.common_dialogs as cd

    class _FakeDialog:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

        def exec_(self):
            return 0  # 用户取消

    monkeypatch.setattr(cd, "ConfirmDialog", _FakeDialog)
    card._on_clear_snapshots()
    assert target.exists(), "用户取消后快照必须保留"


def test_snapshot_clear_removes_when_confirmed(isolated_cfg, data_dir, monkeypatch):
    """确认后清空"""
    d = data_dir / "backups" / "deleted"
    d.mkdir(parents=True)
    (d / "gone.txt").write_bytes(b"x")

    card = _make_card()
    import app.widgets.common_dialogs as cd

    class _FakeDialog:
        def __init__(self, **kwargs):
            pass

        def exec_(self):
            return 1  # 确认

    monkeypatch.setattr(cd, "ConfirmDialog", _FakeDialog)
    card._on_clear_snapshots()
    assert not d.exists() or not any(d.rglob("*")), "确认后应清空"
    assert card.snapshot_clear_btn.isEnabled() is False


def test_snapshot_clear_noop_when_empty(isolated_cfg, data_dir, monkeypatch):
    """无快照时不弹确认框（避免空操作打扰）"""
    shown = []
    import app.widgets.common_dialogs as cd

    class _FakeDialog:
        def __init__(self, **kwargs):
            shown.append(True)

        def exec_(self):
            return 1

    monkeypatch.setattr(cd, "ConfirmDialog", _FakeDialog)
    card = _make_card()
    card._on_clear_snapshots()
    assert shown == []


# ══════════════ 读路径文案澄清（追加裁定）══════════════


def _security_card_source() -> str:
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent.parent
    return (root / "app" / "widgets" / "cards" / "settings" / "security_center_card.py").read_text(encoding="utf-8")


def test_whitelist_copy_clarifies_write_only(isolated_cfg):
    """白名单文案必须点明"仅写入"，消除读场景的虚假安全感

    源码级断言：qfluentwidgets 的 ExpandGroupSettingCard 标题属性名随版本变化，
    直接查构造文案字符串更稳，且直指真源。
    """
    src = _security_card_source()
    assert "写入白名单" in src, "白名单标题未改为「写入白名单」"
    assert "读操作不受此限制" in src, "白名单文案未点明读操作不受限"


def test_blacklist_copy_mentions_both_modes(isolated_cfg):
    """黑名单文案必须点明"读写均拦截"（消除"只拦读"或"只拦写"的误解）"""
    src = _security_card_source()
    assert "读写均拦截" in src, "黑名单文案未体现读写都拦"


def test_note_explains_read_path_semantics(isolated_cfg):
    """说明区必须总述读路径语义（读除黑名单外不受约束）"""
    src = _security_card_source()
    assert "读操作除黑名单外不受路径约束" in src
    assert "白名单与 workdir 边界仅约束写入" in src


# ══════════════ 文案批次（H1/H2/C2/N1/N3~N7 + MCP + 跨平台）══════════════


def test_h1_allow_prefix_placeholder_accurate(isolated_cfg):
    """H1：放行前缀 placeholder 必须说明"破坏性子命令仍会确认"

    旧文案「如 git、npm run」引导用户以为填 git 就全放行，
    而实测 `git rm -rf .` 会连带走白名单（G24 已修，文案需同步为准确表述）。
    """
    src = _security_card_source()
    assert "如 git status、npm run build" in src
    assert "破坏性操作仍会确认" in src
    assert "如 git、npm run" not in src, "旧的高危 placeholder 应已替换"


def test_h2_env_placeholder_warns_exact_match(isolated_cfg):
    """H2：文件黑名单 placeholder 必须提示 `.env` 不拦 `.env.local`"""
    src = _security_card_source()
    assert ".env 不拦 .env.local" in src


def test_n1_note_mentions_default_off(isolated_cfg):
    """N1：说明区必须提示默认关闭（避免用户以为装了就有防护）"""
    src = _security_card_source()
    assert "默认关闭，需手动开启后才会拦截" in src


def test_n3_relative_path_semantics_documented(isolated_cfg):
    """N3：白名单与删除豁免都需说明相对路径按 workdir 解析

    注意：G5/G26 已统一两侧语义，故文案写"按工作目录解析"而非"不支持"。
    """
    src = _security_card_source()
    assert src.count("相对路径按工作目录解析") >= 2, "白名单与删除豁免都应注明"


def test_n4_delete_exempt_mentions_no_snapshot(isolated_cfg):
    """N4：删除豁免需说明"也不做快照" """
    src = _security_card_source()
    assert "不再弹审批，也不做快照" in src


def test_n5_job_quota_scope_documented(isolated_cfg):
    """N5：进程配额需说明仅约束 Bash 与后台命令"""
    src = _security_card_source()
    assert "仅约束 Bash 与后台命令" in src


def test_n6_snapshot_quota_rule_documented(isolated_cfg):
    """N6：快照需说明独立配额规则（避免用户以为清理备份会连带清快照）"""
    src = _security_card_source()
    assert "独立配额 = 备份上限的 1/4" in src
    assert "不受「清理备份文件」影响" in src


def test_n7_backup_restart_hint(isolated_cfg):
    """N7：备份上限需提示"改动后需重启才自动清理，可点立即清理" """
    src = _security_card_source()
    assert "改动上限后需重启才生效" in src


def test_note_mentions_mcp_and_upload_scope(isolated_cfg):
    """说明区需提示 MCP 工具与 upload_file/webfetch 不在本页约束内"""
    src = _security_card_source()
    assert "MCP 工具（mcp__*）" in src
    assert "upload_file" in src and "webfetch" in src


def test_c2_backup_card_has_no_stale_number(isolated_cfg):
    """C2：备份卡文案不得含构造期数值快照（避免同屏两处数字不一致）"""
    src = _security_card_source()
    assert '修改前自动备份原文件（FileRecorder）",' in src, "应改为不含数值的静态描述"
    assert "FileRecorder），上限 {" not in src, "构造期 f-string 数值快照应已移除"


def test_cross_platform_open_dir_branches(isolated_cfg):
    """跨平台：打开目录必须覆盖 win32 / darwin / 其他三分支"""
    src = _security_card_source()
    assert 'sys.platform == "win32"' in src
    assert 'sys.platform == "darwin"' in src
    assert '"xdg-open"' in src, "linux 分支缺失会导致点击静默无反应"


def test_open_backup_dir_reuses_cross_platform_helper(isolated_cfg):
    """_open_backup_dir 应复用跨平台实现（避免两套 explorer 调用漂移）"""
    src = _security_card_source()
    # 只应有一处 explorer 调用（在 _open_path_in_explorer 内）
    assert src.count('["explorer"') == 1, "explorer 调用应集中在一处"
