# -*- coding: utf-8 -*-
"""[T22-C1] 回归：主题验证器免 theme_manager 加载 + 三态 saved 值不被重置。

三态语义（原实现行为保持）：
- 系统主题（内置 id）→ 白名单常量覆盖
- 插件主题（非内置自定义 id）→ saved_theme 直读追加
- 未知主题（任意字符串）→ saved_theme 直读追加
验证走真实 Settings.get_instance() 全链（patch 数据目录）：load 后
ui_theme_style.value 不被 correct 重置。
"""

import json
import sys

from PyQt5.QtCore import Qt
from PyQt5.QtWidgets import QApplication

QApplication.setAttribute(Qt.AA_ShareOpenGLContexts, True)
_APP = QApplication.instance() or QApplication(sys.argv)

import pytest  # noqa: E402

from app.utils import config as config_mod  # noqa: E402
from app.utils.config import Settings  # noqa: E402
from app.utils import utils as utils_mod  # noqa: E402


@pytest.fixture()
def fresh_settings(tmp_path, monkeypatch):
    """单例复位 + 数据目录指向 tmp_path（含预写的 app.config）。"""
    Settings._instance = None
    monkeypatch.setattr(utils_mod, "get_app_data_dir", lambda: tmp_path)
    return tmp_path


def _write_config(tmp_path, saved_theme):
    (tmp_path / "app.config").write_text(
        json.dumps({"UI": {"ThemeStyle": saved_theme}}, ensure_ascii=False),
        encoding="utf-8",
    )


@pytest.mark.parametrize(
    "saved",
    [
        "sakura",  # 系统主题（内置白名单）
        "my-plugin-theme",  # 插件主题（非内置自定义）
        "garbage-xyz-404",  # 未知主题（任意字符串，与原行为一致放行）
    ],
)
def test_saved_theme_not_reset(fresh_settings, saved):
    """load 后 ui_theme_style 保持 saved 值（validator 白名单放行，不重置为默认）。"""
    tmp_path = fresh_settings
    _write_config(tmp_path, saved)

    inst = Settings.get_instance()

    assert inst.ui_theme_style.value == saved, (
        f"saved 主题 {saved!r} 被 load 重置为 {inst.ui_theme_style.value!r}（验证器白名单未放行）"
    )
    assert saved in set(inst.ui_theme_style.validator.options)


def test_no_theme_manager_in_config_import_chain():
    """config 模块导入链不得拉起 theme_manager/yaml（C1 瘦身核心判据）。"""
    import app.utils.config  # noqa: F401 已导入

    assert "app.utils.theme_manager" not in sys.modules, "config 导入链不得前置加载 theme_manager"
    assert "yaml" not in sys.modules, "config 导入链不得前置加载 yaml"
