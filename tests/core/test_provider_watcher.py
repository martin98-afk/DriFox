# -*- coding: utf-8 -*-
"""
ProviderWatcher 服务商插件热重载测试（文件删除残留清理回归）

覆盖：
1. warmup 直接注册（不经 watcher）后删除插件文件 → scan_now → 服务商注销
   —— 回归点：scan_now 卸载依赖 watcher._loaded 记忆，warmup 注册的不在其中，
      且重扫时 registry 同名保护拒绝导致 _loaded 永远为空，删除永不生效
2. scan_now 幂等：重复重扫不重复注册
3. 删除后重扫：现存服务商保留、被删服务商消失

运行: python -m pytest tests/core/test_provider_watcher.py -v
"""
import os
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from app.plugins.loaders.provider_loader import (
    ProviderWatcher,
    load_providers,
)
from app.plugins.registries.provider_registry import ProviderRegistry


_TEST_PLUGIN = "watcher-provider-test"


@pytest.fixture(autouse=True)
def _enable_test_plugin(plugin_enabled):
    """把测试插件名临时加入 Settings.enabled_plugins（加载过滤适配）。"""
    restore = plugin_enabled(_TEST_PLUGIN)
    yield
    restore()


def _make_provider(root: Path, provider_name: str) -> Path:
    """构造 providers/<name>.py 插件文件，返回路径。"""
    providers_dir = root / _TEST_PLUGIN / "providers"
    providers_dir.mkdir(parents=True, exist_ok=True)
    py = providers_dir / f"{provider_name}.py"
    py.write_text(
        "from app.plugins.registries.provider_registry import ProviderDef\n"
        f"def register(registry):\n"
        f"    registry.register(ProviderDef(name={provider_name!r}, api_url='https://x/v1'))\n",
        encoding="utf-8",
    )
    return py


def _make_watcher(root: Path, registry: ProviderRegistry) -> ProviderWatcher:
    return ProviderWatcher(registry=registry, roots=[root])


class TestScanRemovesDeletedProvider:
    """删除插件文件 → 重扫 → 残留清理"""

    def test_scan_now_removes_provider_deleted_after_warmup(self, tmp_path):
        """回归：warmup 直接注册（不经 watcher）后删除文件，scan_now 应注销对应服务商。

        真实 bug：启动链 warmup_providers() 直接 load_providers 注册，watcher._loaded 空；
        删除文件后 scan_now 第一步遍历空 _loaded 什么都不注销，且重扫同名保护拒绝
        使 _loaded 永远为空 → 被删文件的服务商永久残留 registry。
        """
        _make_provider(tmp_path, "Alpha")
        _make_provider(tmp_path, "Beta")
        reg = ProviderRegistry()

        # 模拟启动链：warmup 直接注册（不经过 watcher）
        load_providers(registry=reg, plugin_roots=[tmp_path])
        assert "Alpha" in reg.names()
        assert "Beta" in reg.names()

        # 删除 Alpha 插件文件
        (tmp_path / _TEST_PLUGIN / "providers" / "Alpha.py").unlink()

        # watcher 检测到变更后全量重扫
        watcher = _make_watcher(tmp_path, reg)
        watcher.scan_now()

        assert reg.get("Alpha") is None, f"已删除插件的服务商应被注销，残留: {reg.names()}"
        assert reg.get("Beta") is not None

    def test_scan_now_idempotent_no_residue(self, tmp_path):
        """重扫幂等：多次 scan_now 不重复注册、不误删现存服务商。"""
        _make_provider(tmp_path, "Alpha")
        reg = ProviderRegistry()
        load_providers(registry=reg, plugin_roots=[tmp_path])

        watcher = _make_watcher(tmp_path, reg)
        watcher.scan_now()
        watcher.scan_now()
        watcher.scan_now()
        assert reg.names() == ["Alpha"], f"重复重扫应保持唯一注册，实际: {reg.names()}"


class TestPreciseUnloadReload:
    """unload_plugin / reload_plugin 精准路径：只处理目标插件，不波及他插件"""

    def test_unload_plugin_precise_no_other_plugin_touched(self, tmp_path):
        """卸载单插件：只注销其服务商，他插件服务商保留"""
        _make_provider(tmp_path, "Alpha")
        _make_provider(tmp_path, "Beta")
        reg = ProviderRegistry()
        watcher = _make_watcher(tmp_path, reg)
        watcher.scan_now()
        assert reg.names() == ["Alpha", "Beta"]

        watcher.unload_plugin(_TEST_PLUGIN)
        assert reg.names() == [], f"该插件服务商应全部注销，实际: {reg.names()}"

    def test_reload_plugin_updates_and_preserves_others(self, tmp_path):
        """重载单插件：新内容生效，他插件服务商不被注销重注册（以 source 验证）"""
        _make_provider(tmp_path, "Alpha")
        _make_provider(tmp_path, "Beta")
        reg = ProviderRegistry()
        watcher = _make_watcher(tmp_path, reg)
        watcher.scan_now()
        assert "Alpha" in reg.names()

        # 热更新：Alpha 改名 Alpha2
        (tmp_path / _TEST_PLUGIN / "providers" / "Alpha.py").unlink()
        _make_provider(tmp_path, "Alpha2")

        watcher.reload_plugin(_TEST_PLUGIN)
        assert reg.get("Alpha") is None, "旧服务商应被注销"
        assert reg.get("Alpha2") is not None, "新服务商应注册"
        assert reg.get("Beta") is not None, "他插件服务商应保留"
        assert reg.get("Beta").source == f"plugin:{_TEST_PLUGIN}", "Beta source 应不变"
