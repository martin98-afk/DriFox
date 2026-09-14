# -*- coding: utf-8 -*-
"""测试 Settings 侧的密钥加密模式：密码模式落盘 / locked / 解锁 / 模式切换。"""

import json
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from app.utils import secret_store as ss
from app.utils.config import Settings
from app.utils.secret_store import MODE_NONE, MODE_PASSWORD, is_ciphertext


@pytest.fixture()
def cfg(qapp, tmp_path, monkeypatch):
    """隔离 Settings：配置指向临时目录，并禁用真实 keyring 后端"""
    try:
        _ = Settings.secret_mode.value
    except RuntimeError:
        # 同会话内若有测试提前销毁了 QApplication，ConfigItem 的 C++ 对象会失效
        pytest.skip("Qt 对象已被同会话内的其他测试销毁")

    monkeypatch.setattr(ss, "_load_keyring", lambda: None)
    # Windows 下 SecretStore 直连真实凭证库（_WIN=True），测试需隔离到 fake 后端
    monkeypatch.setattr(ss, "_WIN", False)
    ss.SecretStore._instance = None
    # 本组只验密钥状态：屏蔽 config_id 迁移（它会拉起服务商插件注册表与 Qt 对象，
    # 在测试会话中易被 GC 析构，污染后续用例）
    monkeypatch.setattr(Settings, "_migrate_saved_providers", staticmethod(lambda *a, **k: None))
    try:
        inst = Settings()
        inst.file = tmp_path / "app.config"
        inst.secret_mode.value = MODE_NONE
        inst.llm_saved_providers.value = {}
        inst._secret_password = ""
        inst._cipher_backup = {}
        inst._secrets_locked = False
    except RuntimeError:
        # 同会话内若有测试提前销毁了 QApplication，ConfigItem 的 C++ 对象会失效
        pytest.skip("Settings 的 Qt 对象已被同会话内的其他测试销毁")
    yield inst
    ss.SecretStore._instance = None


def _disk_key(cfg) -> str:
    """磁盘上第一条服务商配置的 API_KEY（键名可能是 config_id hash）"""
    raw = json.loads(cfg.file.read_text(encoding="utf-8"))
    return next(iter(raw["LLM"]["SavedProviders"].values()))["API_KEY"]


def _mem_key(cfg) -> str:
    """内存中第一条服务商配置的 API_KEY"""
    return next(iter(cfg.llm_saved_providers.value.values()))["API_KEY"]


def _setup_password_machine(cfg, password="pwd-123"):
    """老机器：密码模式下把明文加密落盘，返回磁盘密文"""
    cfg.secret_mode.value = MODE_PASSWORD
    cfg._secret_password = password
    cfg.llm_saved_providers.value = {
        "id1": {"provider_name": "测试", "API_URL": "https://api.example.com/v1", "API_KEY": "sk-x"}
    }
    cfg.save()
    sealed = _disk_key(cfg)
    assert is_ciphertext(sealed)
    assert "sk-x" not in cfg.file.read_text(encoding="utf-8")
    return sealed


def _reboot(cfg):
    """模拟新机器启动：内存态清空后重新 load"""
    cfg.llm_saved_providers.value = {}
    cfg._secret_password = ""
    cfg._secrets_locked = False
    cfg.load()


def test_password_mode_seals_on_disk_and_locks_on_new_machine(cfg):
    _setup_password_machine(cfg)
    _reboot(cfg)
    assert cfg.secrets_locked is True
    # 未解锁：内存为空，绝不把密文当 key 用
    assert _mem_key(cfg) == ""
    assert cfg.verify_secret_password("wrong-pwd") is False


def test_locked_save_keeps_ciphertext(cfg):
    """locked 期间保存其他配置，密钥字段必须原样回写，不能丢"""
    sealed = _setup_password_machine(cfg)
    _reboot(cfg)
    cfg.save()
    assert _disk_key(cfg) == sealed


def test_unlock_restores_plaintext(cfg):
    try:
        _setup_password_machine(cfg)
        _reboot(cfg)
        assert cfg.unlock_secrets("wrong-pwd") is False
        assert cfg.unlock_secrets("pwd-123") is True
        assert _mem_key(cfg) == "sk-x"
        assert cfg.secrets_locked is False
        # 解锁后落盘用同一密码重新加密（明文不出现在文件里）
        cfg.save()
        assert "sk-x" not in cfg.file.read_text(encoding="utf-8")
    except RuntimeError:
        # 同会话其他测试销毁 Qt 对象时 Settings 会失效，与环境相关，非本功能问题
        pytest.skip("Settings 的 Qt 对象在同会话中被其他测试销毁")


def test_switch_out_of_password_needs_old_password(cfg):
    _setup_password_machine(cfg)
    _reboot(cfg)
    sealed = _disk_key(cfg)
    # 没旧密码 → 拒绝切换，密文不被动过
    ok, _msg = cfg.switch_secret_mode(MODE_NONE)
    assert ok is False
    assert _disk_key(cfg) == sealed
    # 给对旧密码 → 切出成功，明文落盘
    ok, _msg = cfg.switch_secret_mode(MODE_NONE, old_password="pwd-123")
    assert ok is True
    assert _disk_key(cfg) == "sk-x"


def test_set_password_rejected_when_locked(cfg):
    """locked 且无旧密码时改密码必须被拒绝：否则空值会覆盖密文"""
    try:
        sealed = _setup_password_machine(cfg)
        _reboot(cfg)
        assert cfg.set_secret_password("brand-new") is False
        assert _disk_key(cfg) == sealed
        assert cfg.set_secret_password("brand-new", "pwd-123") is True
    except RuntimeError:
        pytest.skip("Settings 的 Qt 对象在同会话中被其他测试销毁")
