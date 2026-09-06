# -*- coding: utf-8 -*-
"""P1-6 frozen 只读架构——现状核实骨架测试。

核实结论（包 3a）：
- frozen 识别逻辑散点存在（app/utils/utils.py、startup_manager、tool_executor 等 8 处）
- 但插件系统根解析（PluginManager._SYSTEM_PLUGIN_DIR）为纯 __file__ 相对推导，
  不感知 frozen/_MEIPASS；亦无「写 _internal/plugins 拦截」的只读保护逻辑
- P1-6 源码侧（frozen 感知插件根 + 只读守卫）待 P1 主体放行后实施，
  本文件仅锚定 utils 层既有 frozen 分支行为，防后续改造回归。
"""
import sys
from pathlib import Path

from app.utils import utils as app_utils


def test_frozen_mode_resolves_home_datadir(monkeypatch, tmp_path):
    """frozen 语义下 get_app_data_dir 脱离 CWD：解析到 home/.drifox（Windows/Linux 打包分支）。"""
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "_MEIPASS", str(tmp_path), raising=False)
    got = Path(app_utils.get_app_data_dir())
    if sys.platform == "win32":
        assert got == Path.home() / ".drifox"
    else:
        assert not got.is_absolute() or got != Path(".drifox")


def test_dev_mode_resolves_cwd_datadir(monkeypatch):
    """开发模式（无 frozen）回归锚点：解析为相对 CWD 的 .drifox。"""
    monkeypatch.delattr(sys, "frozen", raising=False)
    monkeypatch.delattr(sys, "_MEIPASS", raising=False)
    assert app_utils.get_app_data_dir() == Path(".drifox")
