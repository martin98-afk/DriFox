# zero-bridge —— zero 插件引擎与 DriFox 的桥

zero（`zero/`，仓库顶层）是宿主无关的可逆插件引擎；本插件把它接入 DriFox。
**适配现有架构：监听、发现、变更识别全部复用 PluginHostService 现有链路，
zero 只是新增的一种组件类型**，主系统改动仅 kernel 登记两行。

## zero 组件（新组件类型）

任何 DriFox 插件都可以带 zero 组件：插件目录下放 `zero/` 子目录，
每个 `*.py` 文件是一个 zero 插件（`_` 前缀忽略）：

```python
NAME = "my_component"         # 可选，缺省用文件名
PROVIDES = ("svc",)           # 可选，提供的服务（参与拓扑排序）
# from zero import inject
# @inject(required=["db"])    # 可选，依赖缺失则不加载
def apply(ctx):
    ctx.provide("svc", ...)   # 提供服务（注册到根）
    ctx.on("host.theme_changed", ...)  # 宿主事件（UIEventBus 桥入）
    ctx.add_disposer(...)     # 外部资源手动接管
```

plugin.json 声明 `"components": {"zero": true}`。

## 热重载链路（全部复用现有基建）

```
watchfiles（PluginHostService 现有 watcher）
  → 识别 <插件>/zero/*.py 变更（现有 _identify_* 链路）
  → ComponentReloaderRegistry.reload（现有分派表）
  → zero-bridge 注册的 reloader（本插件运行时注册）
  → zero 语义：旧 fork 全部副作用逆序回滚 → 模块 purge → 重装
```

主系统改动清单：`kernel.py` KNOWN_COMPONENTS/COMPONENT_ORDER 登记 `"zero"`
（两行）、`plugin_manager.py` `_COMPONENT_PROBES` 加一条探测谓词（一行）。
reloader 本体由本插件注册，主系统零逻辑。

## 已知坑（已防）

- pyc 字节码缓存（mtime 秒级 + size 校验）：快速保存同秒同大小会执行过期
  bytecode → 装载器 `compile(source)` 直载，绕开 pyc 全部机制
- 跨插件同名组件 → 登记名 `<plugin_name>.<stem>` 前缀隔离
- 坏组件（import 异常 / 缺 apply / 依赖缺失）→ 只停用自身，不拖垮其他组件

## 测试

```bash
py -3 -m pytest tests/plugins/test_zero_plugin_loader.py tests/zero -q
```

