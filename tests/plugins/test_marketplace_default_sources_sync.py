# -*- coding: utf-8 -*-
"""builtin 市场源跟随版本默认值同步（_ensure_defaults 回归）。

背景：默认源 URL 随主程序线调整（pyside6 线切 drifox-plugins/pyside6 分支）后，
旧版 _ensure_defaults 只按 name 追加缺失 builtin 源、不覆盖已有条目，存量用户的
sources.json 永远锁死旧 main URL，市场清单/图标/安装全部走错分支。

四用例：存量旧 URL 同步更新、用户自定义源不动、已是新值幂等不重写、
文件不存在写入全部默认源。全程 tmp_path，零网络。
"""
import json
import sys
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
PLUGIN_MARKETPLACE = ROOT / "plugins" / "plugin-marketplace"
if str(PLUGIN_MARKETPLACE) not in sys.path:
    sys.path.insert(0, str(PLUGIN_MARKETPLACE))

# 唯一包名加载 plugin-marketplace/ui（避免与其他插件的 ui 包抢占 sys.modules["ui"]）
if "pm_ui_sync" not in sys.modules:
    _pkg = types.ModuleType("pm_ui_sync")
    _pkg.__path__ = [str(PLUGIN_MARKETPLACE / "ui")]
    _pkg.__package__ = "pm_ui_sync"
    sys.modules["pm_ui_sync"] = _pkg

from pm_ui_sync import marketplace_manager as mm  # noqa: E402

MarketplaceSourceManager = mm.MarketplaceSourceManager

# 官方源默认 URL（当前版本应指向 pyside6 分支）
OFFICIAL_DEFAULT_URL = "https://raw.githubusercontent.com/martin98-afk/drifox-plugins/pyside6/marketplace.json"


def _make_mgr(tmp_path: Path) -> MarketplaceSourceManager:
    """__new__ 绕过 __init__，手动挂最小依赖（同 test_c1_marketplace_source_allowlist 模式）"""
    mgr = MarketplaceSourceManager.__new__(MarketplaceSourceManager)
    sources_dir = tmp_path / "user-custom" / "marketplaces"
    sources_dir.mkdir(parents=True, exist_ok=True)
    mgr._sources_file = sources_dir / "sources.json"
    mgr._cache_dir = tmp_path / "cache" / "marketplaces"
    mgr._status_file = mgr._cache_dir / "status.json"
    mgr._fetch_status = {}
    return mgr


def _read_sources(mgr: MarketplaceSourceManager) -> list:
    return json.loads(mgr._sources_file.read_text(encoding="utf-8-sig"))


def _url_of(sources: list, name: str) -> str:
    for s in sources:
        if s.get("name") == name:
            return s["source"].get("url", "")
    return ""


def test_stale_builtin_source_synced_to_default(tmp_path):
    """存量 sources.json 里 builtin 源锁死旧 main URL → 启动即同步为当前默认。"""
    mgr = _make_mgr(tmp_path)
    legacy = [
        {
            "name": "drifox-official",
            "source": {"source": "url", "url": "https://raw.githubusercontent.com/martin98-afk/drifox-plugins/main/marketplace.json"},
            "auto_update": True,
            "builtin": True,
        },
    ]
    mgr._sources_file.write_text(json.dumps(legacy, ensure_ascii=False, indent=2), encoding="utf-8")

    mgr._ensure_defaults()

    sources = _read_sources(mgr)
    assert _url_of(sources, "drifox-official") == OFFICIAL_DEFAULT_URL
    assert sources[0]["name"] == "drifox-official"  # 顺序保留


def test_user_custom_source_untouched(tmp_path):
    """用户自定义源（builtin 非真）即使同名默认源存在也不被覆盖；缺失默认源照常追加。"""
    mgr = _make_mgr(tmp_path)
    legacy = [
        {
            "name": "drifox-official",
            "source": {"source": "url", "url": "https://example.com/my-own-market.json"},
            "auto_update": False,
            "builtin": False,
        },
    ]
    mgr._sources_file.write_text(json.dumps(legacy, ensure_ascii=False, indent=2), encoding="utf-8")

    mgr._ensure_defaults()

    sources = _read_sources(mgr)
    assert _url_of(sources, "drifox-official") == "https://example.com/my-own-market.json"
    names = [s["name"] for s in sources]
    # 缺失的 builtin 默认源仍被补齐
    assert "drifox-system" in names


def test_idempotent_when_already_current(tmp_path):
    """源已是当前默认值 → 幂等，不产生多余写回（文件内容不变）。"""
    mgr = _make_mgr(tmp_path)
    current = [
        {
            "name": "drifox-official",
            "source": {"source": "url", "url": OFFICIAL_DEFAULT_URL},
            "auto_update": True,
            "builtin": True,
        },
        *mm._DEFAULT_SOURCES[1:],
    ]
    mgr._sources_file.write_text(json.dumps(current, ensure_ascii=False, indent=2), encoding="utf-8")
    before = mgr._sources_file.read_text(encoding="utf-8-sig")

    mgr._ensure_defaults()

    assert mgr._sources_file.read_text(encoding="utf-8-sig") == before


def test_missing_file_writes_all_defaults(tmp_path):
    """文件不存在 → 写入全部默认源（含 pyside6 官方源）。"""
    mgr = _make_mgr(tmp_path)

    mgr._ensure_defaults()

    sources = _read_sources(mgr)
    assert _url_of(sources, "drifox-official") == OFFICIAL_DEFAULT_URL
    assert len(sources) == len(mm._DEFAULT_SOURCES)
