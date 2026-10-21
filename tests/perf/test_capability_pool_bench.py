"""能力池性能基准：测池构建、声明解析、单次能力取值的开销。

关注点：
1. build_pool 本身多贵（provider 惰性求值，构建时应只做 dict 推导）
2. resolve_declared 多贵（os.stat + PluginManager 查询 + 缓存命中）
3. 一次 UI 卡片 context 现在会建几个池（重复建池是浪费）
"""

import sys
import time
from pathlib import Path

sys.path.insert(0, '.')


def bench(label, fn, n=2000):
    # 预热
    for _ in range(50):
        fn()
    t0 = time.perf_counter()
    for _ in range(n):
        fn()
    dt = (time.perf_counter() - t0) / n
    print(f"  {label:46s} {dt * 1e6:8.2f} μs/次")
    return dt


def main():
    from app.plugins.capabilities.bootstrap import ensure_registered, reset_bootstrap
    from app.plugins.capabilities.registry import CapabilityRegistry

    reset_bootstrap()
    reg = CapabilityRegistry.get_instance()
    ensure_registered("ui")
    ensure_registered("tool")

    print("=== 池构建 ===")
    bench("build_pool(host=None, declared 显式)", lambda: reg.build_pool(
        surface="tool", host=None, plugin="demo", declared={"tool.read"}))
    bench("build_pool(host=object(), plugin='')", lambda: reg.build_pool(
        surface="ui", host=object(), plugin=""))
    bench("build_pool(host=object(), plugin='demo')", lambda: reg.build_pool(
        surface="ui", host=object(), plugin="demo"))

    print()
    print("=== 声明解析（resolve_declared）===")
    bench("resolve_declared('demo') 首次之后（命中缓存）", lambda: reg.resolve_declared("demo"))

    print()
    print("=== 能力取值 ===")
    pool = reg.build_pool(surface="tool", host=None, plugin="demo", declared={"tool.read"})
    api = pool["tool.read"]()
    bench("pool['tool.read'] 重复取值（缓存值）", lambda: pool["tool.read"])
    bench("api['names']() 调注册表", lambda: api["names"]())

    print()
    print("=== 单次 UI 卡片 context 的建池次数 ===")
    # 统计一次 _build_card_context 路径上会走几次 build_pool
    import app.plugins.registries.ui_plugin_registry as uir

    src_all = Path('app/plugins/registries/ui_plugin_registry.py').read_text(encoding='utf-8')
    for name in ('_capabilities_for', 'callback_context'):
        i = src_all.find(f'def {name}')
        seg = src_all[i:i + 1200]
        print(f"  {name}: build_pool 调用 {seg.count('build_pool(')} 次")
    mw = Path('app/main_widget.py').read_text(encoding='utf-8')
    i = mw.find('def _build_ui_capabilities')
    print(f"  _build_ui_capabilities: build_pool {mw[i:i+800].count('build_pool(')} 次")
    i = mw.find('def _build_ui_services')
    print(f"  _build_ui_services: _build_ui_capabilities {mw[i:i+900].count('_build_ui_capabilities')} 次")


if __name__ == '__main__':
    main()
