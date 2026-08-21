# -*- coding: utf-8 -*-
"""E2E：engines 插件全链路 — 临时插件目录 → loader 扫描 → EngineRegistry →
create_engine_for_slot 产出派生类实例 → 卸载清理后回退内置。

不触 Qt：fallback 用替身基类（backend 实际传 UIEngine，逻辑同一入口）。
"""

import shutil
import sys
import types


def test_engine_plugin_end_to_end(tmp_path, plugin_enabled, monkeypatch):
    from app.plugins.loaders.runtime_component_loader import RuntimeComponentLoader
    from app.plugins.registries.engine_registry import EngineRegistry, create_engine_for_slot

    # 替身基类（模拟内置 UIEngine）：插件引擎须继承它才能通过 isinstance 校验。
    # 插件里 ReplacementEngine 不继承 → create 必须回退替身基类（安全网生效路径）。
    class _FakeBuiltinEngine:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

    # 兼容基类（step 4 用）：测试本地定义 + 经 sys.modules 注入到插件 import 路径
    # —— 插件 exec_module 时只能靠 import 拿到基类，故用 sys.modules 共享同一类对象。
    class _Base:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

    sys.modules["_e2e_engine_base"] = types.SimpleNamespace(_Base=_Base)

    # 1) 造插件：engines/ui_replacement.py 注册替身引擎工厂（不继承 _FakeBuiltinEngine）
    plugin_dir = tmp_path / "my-engine-plugin"
    (plugin_dir / "engines").mkdir(parents=True)
    (plugin_dir / "engines" / "ui_replacement.py").write_text(
        "# -*- coding: utf-8 -*-\n"
        "from app.plugins.contracts.dialogue_engine import ENGINE_SLOT_UI, ClassEngineFactory\n\n\n"
        "class ReplacementEngine:\n"
        "    def __init__(self, **kwargs):\n"
        "        self.kwargs = kwargs\n\n\n"
        "def register(registry):\n"
        "    registry.register(ClassEngineFactory(ENGINE_SLOT_UI, ReplacementEngine))\n",
        encoding="utf-8",
    )
    restore = plugin_enabled("my-engine-plugin")

    reg = EngineRegistry()
    loader = RuntimeComponentLoader(comp_dir="engines", registry=reg)
    monkeypatch.setattr(EngineRegistry, "get_instance", staticmethod(lambda: reg))

    # 2) 扫描注册
    loaded = loader.scan_roots([tmp_path])
    assert loaded == {"my-engine-plugin"}
    assert reg.get_factory("ui") is not None

    # 3) 创建：不兼容（未继承 _FakeBuiltinEngine）→ 安全网回退
    engine = create_engine_for_slot("ui", _FakeBuiltinEngine, session_manager="s")
    assert type(engine) is _FakeBuiltinEngine

    # 4) 换成兼容实现（继承 _Base）→ 替换生效
    (plugin_dir / "engines" / "ui_replacement.py").write_text(
        "# -*- coding: utf-8 -*-\n"
        "from _e2e_engine_base import _Base\n"
        "from app.plugins.contracts.dialogue_engine import ENGINE_SLOT_UI, ClassEngineFactory\n\n\n"
        "class CompatibleEngine(_Base):\n"
        "    pass\n\n\n"
        "def register(registry):\n"
        "    registry.register(ClassEngineFactory(ENGINE_SLOT_UI, CompatibleEngine))\n",
        encoding="utf-8",
    )
    loader.reload_plugin("my-engine-plugin")
    engine2 = create_engine_for_slot("ui", _Base, session_manager="s")
    # CompatibleEngine 定义在插件模块，loader 用 drifox_rt_engines_xxx_yyy 命名加载
    plugin_mod_name = "drifox_rt_engines_my-engine-plugin_ui_replacement"
    assert plugin_mod_name in sys.modules, f"插件模块应加载到 sys.modules，实际：{[m for m in sys.modules if 'engines_my-engine' in m]}"
    CompatibleEngine = getattr(sys.modules[plugin_mod_name], "CompatibleEngine")
    assert type(engine2) is CompatibleEngine

    # 5) 卸载：目录删除 + 重扫 → 回退内置
    shutil.rmtree(plugin_dir)
    loader.scan_roots([tmp_path])
    assert reg.get_factory("ui") is None
    engine3 = create_engine_for_slot("ui", _Base, session_manager="s")
    assert type(engine3) is _Base
    restore()
    sys.modules.pop("_e2e_engine_base", None)