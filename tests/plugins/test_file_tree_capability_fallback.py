# -*- coding: utf-8 -*-
"""file-tree 取工作台页：能力池优先，宿主注册表兜底。

回归背景：`window.workbench` 在运行期一度解析为不可用（caps_for 返回 None），
旧实现直接 `return False` → 文件打开接管整体失效，消息卡片里点工具参数文件
不再走文件树预览，静默掉到系统默认程序。页面访问是插件自己的页，能力面瞬时
不可用不该让预览功能整体失效，故保留注册表兜底（register_ui 的宿主入参）。
"""

import importlib.util
from pathlib import Path

UI_INIT = Path(__file__).resolve().parents[2] / "plugins" / "file-tree" / "ui" / "__init__.py"


def _load_ui_module():
    """按文件路径加载插件 ui 入口（不调用 register_ui，避免触发 .cards 相对导入）。"""
    spec = importlib.util.spec_from_file_location("file_tree_ui_under_test", UI_INIT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class _Reg:
    def __init__(self, page):
        self._page = page
        self.calls = []

    def open_workbench_page(self, key):
        self.calls.append(key)
        return self._page if key == "file-tree" else None


def _patch_caps(monkeypatch, fake):
    """patch 能力入口。

    ⚠️ 必须打在 ``app.plugins.sdk`` 的模块字典上：sdk 是 PEP 562 懒代理，
    首次取用后把实现写进 globals() 缓存，patch 底层 resolve 模块对后续
    用例无效（实测合跑时第二例起吃到上一例的 fake）。
    """
    import app.plugins.sdk as sdk

    monkeypatch.setitem(vars(sdk), "caps_for", fake)


def test_uses_capability_when_available(monkeypatch):
    page = object()
    _patch_caps(
        monkeypatch,
        lambda *a, **k: {"page": lambda key: page if key == "file-tree" else None},
    )
    mod = _load_ui_module()
    reg = _Reg(object())
    mod._HOST_REGISTRY = reg
    assert mod._workbench_page() is page
    assert reg.calls == [], "能力可用时不应触碰兜底通路"


def test_falls_back_to_registry_when_capability_missing(monkeypatch):
    _patch_caps(monkeypatch, lambda *a, **k: None)
    mod = _load_ui_module()
    page = object()
    reg = _Reg(page)
    mod._HOST_REGISTRY = reg
    assert mod._workbench_page() is page
    assert reg.calls == ["file-tree"]


def test_falls_back_when_capability_raises(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("pool 构建失败")

    _patch_caps(monkeypatch, boom)
    mod = _load_ui_module()
    page = object()
    mod._HOST_REGISTRY = _Reg(page)
    assert mod._workbench_page() is page


def test_returns_none_without_any_route(monkeypatch):
    _patch_caps(monkeypatch, lambda *a, **k: None)
    mod = _load_ui_module()
    mod._HOST_REGISTRY = None
    assert mod._workbench_page() is None


def test_open_file_previews_via_fallback(monkeypatch):
    """端到端：能力不可用时，接管打开仍能落到页面的 preview_path。"""
    _patch_caps(monkeypatch, lambda *a, **k: None)
    mod = _load_ui_module()

    class _Page:
        def __init__(self):
            self.seen = []

        def preview_path(self, path):
            self.seen.append(path)
            return True

    page = _Page()
    mod._HOST_REGISTRY = _Reg(page)
    assert mod._open_file("/tmp/a.md") is True
    assert page.seen == ["/tmp/a.md"]