# -*- coding: utf-8 -*-
"""enable/disable 移动插件的健壮性回归测试

复现问题：gateway 插件启用/禁用时目录 move 偶发 [WinError 5] 拒绝访问
（deps 中的 .pyd 被 backend watcher reload 抢跑 import 占用句柄），
旧逻辑用裸 ``shutil.move`` 会立即失败、需用户点第二次才成功。

修复后 enable/disable 走 ``_robust_move``（带锁重试）+ 抑制 watcher +
移动前 purge 模块缓存，第一次点击即可成功。
"""

import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PLUGIN_MARKETPLACE = ROOT / "plugins" / "plugin-marketplace"
if str(PLUGIN_MARKETPLACE) not in sys.path:
    sys.path.insert(0, str(PLUGIN_MARKETPLACE))


def _build_fake_plugin(base: Path, name: str) -> Path:
    plugin = base / name
    plugin.mkdir(parents=True, exist_ok=True)
    (plugin / "__init__.py").write_text("# fake")
    # 模拟 vendor 的 deps（含会被锁的 .pyd）
    deps = plugin / "deps" / "Crypto" / "Cipher"
    deps.mkdir(parents=True, exist_ok=True)
    (deps / "_raw_aes.pyd").write_text("fake-pyd")
    return plugin


def _make_installer(tmp_path: Path):
    plugins_dir = tmp_path / "plugins"
    disabled_dir = tmp_path / "plugins-disabled"
    plugins_dir.mkdir()
    disabled_dir.mkdir()
    installer = object.__new__(__import__("ui.installer", fromlist=["PluginInstaller"]).PluginInstaller)
    installer._plugins_dir = plugins_dir
    installer._disabled_dir = disabled_dir
    installer._cache_dir = tmp_path / "cache"
    installer._system_dir = tmp_path / "system-plugins"
    installer._inst_map_cache = None
    installer._inst_map_ts = 0.0
    installer._status_map_cache = None
    installer._status_map_ts = 0.0
    installer._manifest_cache = {}
    installer.last_error = ""
    return installer


def test_enable_retries_on_locked_move(tmp_path, monkeypatch):
    """启用时首次 move 被锁（PermissionError）应重试成功，而非立即失败"""
    from ui.installer import PluginInstaller

    installer = _make_installer(tmp_path)
    _build_fake_plugin(installer._disabled_dir, "gateway-feishu")

    real_move = shutil.move
    calls = {"n": 0}

    def fake_move(src, dst, *a, **k):
        calls["n"] += 1
        if calls["n"] == 1:
            # 模拟 Windows 下 .pyd 被占用触发的 WinError 5
            raise PermissionError("[WinError 5] 拒绝访问。: '..._raw_aes.pyd'")
        return real_move(src, dst)

    monkeypatch.setattr("ui.installer.shutil.move", fake_move)
    monkeypatch.setattr("ui.installer.time.sleep", lambda *_: None)

    ok = installer.enable("gateway-feishu")
    assert ok is True, "首次锁失败后重试应成功，无需用户再次点击"
    assert (installer._plugins_dir / "gateway-feishu").exists()
    assert not (installer._disabled_dir / "gateway-feishu").exists()
    assert calls["n"] == 2, "应恰好重试一次（共 2 次 move 调用）"


def test_disable_retries_on_locked_move(tmp_path, monkeypatch):
    """禁用时首次 move 被锁应重试成功"""
    from ui.installer import PluginInstaller

    installer = _make_installer(tmp_path)
    _build_fake_plugin(installer._plugins_dir, "gateway-feishu")

    real_move = shutil.move
    calls = {"n": 0}

    def fake_move(src, dst, *a, **k):
        calls["n"] += 1
        if calls["n"] == 1:
            raise PermissionError("[WinError 5] 拒绝访问。: '..._raw_aes.pyd'")
        return real_move(src, dst)

    monkeypatch.setattr("ui.installer.shutil.move", fake_move)
    monkeypatch.setattr("ui.installer.time.sleep", lambda *_: None)

    ok = installer.disable("gateway-feishu")
    assert ok is True, "首次锁失败后重试应成功"
    assert (installer._disabled_dir / "gateway-feishu").exists()
    assert not (installer._plugins_dir / "gateway-feishu").exists()
    assert calls["n"] == 2


def test_enable_uses_robust_move_after_fix(tmp_path, monkeypatch):
    """确认 enable 实际调用的是 _robust_move（而非裸 shutil.move）"""
    from ui.installer import PluginInstaller

    installer = _make_installer(tmp_path)
    _build_fake_plugin(installer._disabled_dir, "gateway-feishu")

    called = {"robust": False}

    real_robust = installer._robust_move

    def spy_robust(src, dst, **kw):
        called["robust"] = True
        return real_robust(src, dst, **kw)

    monkeypatch.setattr(installer, "_robust_move", spy_robust)
    monkeypatch.setattr("ui.installer.time.sleep", lambda *_: None)

    ok = installer.enable("gateway-feishu")
    assert ok is True
    assert called["robust"] is True, "enable 必须走 _robust_move"
