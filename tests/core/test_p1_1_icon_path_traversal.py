# -*- coding: utf-8 -*-
"""P1-1：icon 路径逃逸校验（str/dict 两形态，越界条目丢弃 + warning）。

四用例：绝对路径拒 / 相对穿越拒 / 根内合法图标过 / 打包版 ../../_internal 变体拒。
全部 tmp_path，零写入真实插件目录。
"""
import pytest
from loguru import logger

from app.plugins.managers.plugin_manager import PluginInfo


@pytest.fixture()
def log_capture():
    records = []
    sink_id = logger.add(lambda m: records.append(str(m)), level="WARNING")
    yield records
    logger.remove(sink_id)


def _info(tmp_path, icon_raw):
    return PluginInfo(name="icon-plug", manifest={"icon": icon_raw}, path=tmp_path)


def test_absolute_path_rejected(tmp_path, log_capture):
    """绝对路径（拼接后被绝对值覆盖）→ 越界丢弃。"""
    raw = "C:/Windows/system32/evil.svg" if tmp_path.drive != "C:" else "/etc/passwd"
    info = _info(tmp_path, raw)
    assert info.icon_config is None
    assert any("icon 路径越界" in r for r in log_capture)


def test_relative_traversal_rejected(tmp_path, log_capture):
    """相对路径穿越（..）出插件根 → 丢弃；dict 形态越界子项同样丢弃。"""
    outside = tmp_path.parent / "outside.svg"
    outside.write_text("<svg/>", encoding="utf-8")
    info = _info(tmp_path, "../../outside.svg")
    assert info.icon_config is None
    # dict 形态：越界主题丢弃，合法主题保留（若两个都越界则整体 None）
    info2 = _info(tmp_path, {"light": "ok.svg", "dark": "../outside.svg"})
    (tmp_path / "ok.svg").write_text("<svg/>", encoding="utf-8")
    cfg = info2.icon_config
    # 越界 dark 被丢弃后触发既有「单主题补齐」语义：dark 回填为 light
    assert cfg is not None and cfg["dark"] == cfg["light"] == tmp_path / "ok.svg"
    assert sum(1 for r in log_capture if "icon 路径越界" in r) >= 2


def test_valid_icon_inside_root_passes(tmp_path, log_capture):
    """根内合法图标（str/dict 两形态）正常返回且无越界告警。"""
    (tmp_path / "icon.svg").write_text("<svg/>", encoding="utf-8")
    (tmp_path / "dark.svg").write_text("<svg/>", encoding="utf-8")
    info = _info(tmp_path, "icon.svg")
    cfg = info.icon_config
    assert cfg is not None and cfg["light"] == tmp_path / "icon.svg" and cfg["dark"] == cfg["light"]
    info2 = _info(tmp_path, {"light": "icon.svg", "dark": "dark.svg"})
    cfg2 = info2.icon_config
    assert cfg2["light"] == tmp_path / "icon.svg" and cfg2["dark"] == tmp_path / "dark.svg"
    assert not any("icon 路径越界" in r for r in log_capture)


def test_packaged_internal_variant_rejected(tmp_path, log_capture):
    """打包版 ../../_internal 变体（穿越后落 _internal）→ 越界丢弃。"""
    internal = tmp_path.parent.parent / "_internal" / "plugins" / "x.svg"
    internal.parent.mkdir(parents=True, exist_ok=True)
    internal.write_text("<svg/>", encoding="utf-8")
    info = _info(tmp_path, "../../_internal/plugins/x.svg")
    assert info.icon_config is None
    assert any("icon 路径越界" in r for r in log_capture)
