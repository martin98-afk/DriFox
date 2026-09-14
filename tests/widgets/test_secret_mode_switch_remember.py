# -*- coding: utf-8 -*-
"""设置卡切换到密码加密后的「记住本机密码」回归测试（2026-09-14 用户报告）

Bug 症状：重启软件偶尔要求重新输入加密密码。

根因：密码写进系统钥匙串这一步只在部分入口做了——
- 「设置密码」按钮 `_on_set_password` → 调 remember_secret_password；
- 解锁弹窗勾选「记住本机密码」→ 调 remember_secret_password；
- 设置卡列表行点「密码加密」`_switch_to` → **没有**。

走列表行切换后，本次运行内存里有密码（能正常解密），但进程退出即丢失；
下次启动 keyring 读不到密码 → `_secrets_locked=True` → 弹解锁窗。
用户来回切换模式时表现为「偶尔弹窗」。

本测试锁定：`_switch_to` 切到密码模式成功后，必须把新密码写入钥匙串
（钥匙串不可用时跳过，与 `_on_set_password` 同款判断）。
"""

import os
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from app.utils import secret_store as ss  # noqa: E402
from app.utils.config import Settings  # noqa: E402
from app.utils.secret_store import (  # noqa: E402
    MASTER_PASSWORD_ACCOUNT,
    MODE_KEYRING,
    MODE_PASSWORD,
)

_PWD = "pwd-123"


class _FakeKeyring:
    """内存版钥匙串后端（隔离真实系统凭证库）"""

    def __init__(self):
        self.data: dict[tuple[str, str], str] = {}

    def get_password(self, service, account):
        return self.data.get((service, account), "")

    def set_password(self, service, account, value):
        self.data[(service, account)] = value

    def delete_password(self, service, account):
        self.data.pop((service, account), None)


@pytest.fixture()
def cfg(qapp, tmp_path, monkeypatch):
    """隔离 Settings + 假钥匙串，返回 (cfg, fake_kr)"""
    fake = _FakeKeyring()
    # Windows 走 advapi32 直连（_WIN=True），测试需切到 keyring 分支以使用 fake 后端
    monkeypatch.setattr(ss, "_WIN", False)
    monkeypatch.setattr(ss, "_load_keyring", lambda: fake)
    ss.SecretStore._instance = None
    # 本组只验密码记忆：屏蔽 config_id 迁移（会拉起服务商插件注册表）
    monkeypatch.setattr(Settings, "_migrate_saved_providers", staticmethod(lambda *a, **k: None))

    inst = Settings()
    inst.file = tmp_path / "app.config"
    inst.secret_mode.value = MODE_KEYRING
    inst.llm_saved_providers.value = {
        "cid_1": {
            "provider_name": "测试服务商",
            "API_URL": "https://api.example.com/v1",
            "API_KEY": "sk-test-key",
            "config_id": "cid_1",
        }
    }
    inst._secret_password = ""
    inst._cipher_backup = {}
    inst._secrets_locked = False
    inst.save()
    yield inst, fake
    ss.SecretStore._instance = None


def _make_card(cfg, monkeypatch):
    """构造设置卡并屏蔽会阻塞的 UI 交互（弹窗 / 通知 / 高度重算）"""
    from app.widgets.cards.settings.secret_mode_card import SecretModeSettingCard

    card = SecretModeSettingCard(None)
    monkeypatch.setattr(card, "_prompt_setup_password", lambda require_old: ("", _PWD))
    monkeypatch.setattr(card, "_ensure_unlocked", lambda: True)
    monkeypatch.setattr(card, "_notify", lambda *a, **k: None)
    monkeypatch.setattr(card, "_refresh", lambda: None)
    return card


def _reboot(cfg):
    """模拟重启：清空内存态后重新 load（钥匙串内容保留）"""
    cfg.llm_saved_providers.value = {}
    cfg._secret_password = ""
    cfg._cipher_backup = {}
    cfg._secrets_locked = False
    cfg.load()


def test_switch_to_password_remembers_password(cfg, monkeypatch):
    """列表行切到密码加密 → 密码必须进钥匙串，重启后自动解锁不弹窗"""
    inst, fake = cfg
    card = _make_card(inst, monkeypatch)

    card._switch_to(MODE_PASSWORD)

    assert inst.secret_mode.value == MODE_PASSWORD
    assert card._current_mode() == MODE_PASSWORD
    assert fake.get_password(ss.SERVICE, MASTER_PASSWORD_ACCOUNT) == _PWD, (
        "切到密码模式后密码未写入钥匙串 → 重启必弹解锁窗"
    )

    # 重启：keyring 里有密码 → 自动解锁，不 locked
    _reboot(inst)
    assert inst.secrets_locked is False
    assert next(iter(inst.llm_saved_providers.value.values()))["API_KEY"] == "sk-test-key"


def test_switch_to_password_skips_remember_when_keyring_unavailable(cfg, monkeypatch):
    """钥匙串不可用时不得报错，也不能留下「以为记住了」的假象"""
    inst, fake = cfg
    monkeypatch.setattr(ss, "_load_keyring", lambda: None)
    ss.SecretStore._instance = None
    card = _make_card(inst, monkeypatch)

    card._switch_to(MODE_PASSWORD)

    assert inst.secret_mode.value == MODE_PASSWORD
    # 无可用钥匙串：没记住是预期行为，重启后需要用户手动输入
    _reboot(inst)
    assert inst.secrets_locked is True
