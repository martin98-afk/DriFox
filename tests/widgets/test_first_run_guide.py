"""首次设置：模型接入独立于可选项；配置变化与重启选择反映真实状态。"""

from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from PyQt5.QtCore import QObject, pyqtSignal
from PyQt5.QtTest import QSignalSpy
from qfluentwidgets import PushButton

from app.widgets import first_run_guide as onboarding


class _Item(QObject):
    valueChanged = pyqtSignal(object)

    def __init__(self, value):
        super().__init__()
        self.value = value

    def change(self, value):
        self.value = value
        self.valueChanged.emit(value)


@pytest.fixture
def guide(qtbot, monkeypatch):
    cfg = SimpleNamespace(secret_mode=_Item("none"), gitee_bound=_Item(False), gitee_user_owner=_Item(""))
    # 仅替代引导页读取的配置，保留全局主题与字体所用的 Settings。
    monkeypatch.setattr(onboarding, "Settings", SimpleNamespace(get_instance=lambda: cfg))
    widget = onboarding.FirstRunGuide()
    qtbot.addWidget(widget)
    return widget


def test_model_actions_and_skip(guide):
    add, choose, done = (QSignalSpy(signal) for signal in (guide.addProvider, guide.chooseModel, guide.finished))
    guide.primary.click()
    assert len(add) == 1
    guide.skip.click()
    assert len(done) == 1
    guide.update_progress(True, "")
    guide.primary.click()
    assert len(choose) == 1
    guide.update_progress(True, "test-model")
    assert "test-model" in guide.model_status.text()
    guide.primary.click()
    assert len(done) == 2


def test_preferences_follow_changes(guide):
    assert "明文" in guide.key_status.text()
    guide.cfg.secret_mode.change("password")
    assert "密码加密" in guide.key_status.text()
    guide.cfg.gitee_bound.change(True)
    guide.cfg.gitee_user_owner.change("test-owner")
    assert "test-owner" in guide.account_status.text()
    assert guide.account_button.text() == "管理账户"
    guide.cfg.gitee_bound.change(False)
    assert guide.account_button.text() == "绑定账户"
    assert "test-owner" not in guide.account_status.text()


@pytest.mark.parametrize("safe", [False, True])
def test_switch_mode_requires_explicit_restart(guide, monkeypatch, safe):
    from app.utils import app_restart
    from app.widgets.common_dialogs import ConfirmDialog

    monkeypatch.setattr(onboarding, "is_safe_mode", lambda: safe)
    restart = Mock(return_value=True)
    monkeypatch.setattr(app_restart, "restart_application", restart)
    button = next(b for b in guide.findChildren(PushButton) if b.accessibleName().startswith("插件启动模式"))
    monkeypatch.setattr(ConfirmDialog, "exec_", lambda dialog: dialog.close())
    button.click()
    restart.assert_not_called()
    monkeypatch.setattr(ConfirmDialog, "exec_", lambda dialog: dialog.confirmed.emit())
    button.click()
    restart.assert_called_once_with(**{("drop_args" if safe else "extra_args"): [onboarding.SAFE_MODE_FLAG]})
