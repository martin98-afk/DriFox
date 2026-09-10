# zero — Python 版可逆插件内核

对标 [cordis](https://cordis.moe)：**所有对上下文的变更都归结为一个原语，
因此"通过上下文做的任何操作都自动可追踪、可恢复"是结构事实，不是约定。**

zero 是独立包，零第三方依赖，不依赖 DriFox 任何模块（适配层另建，不动主程序）。

## 核心原语

```
ctx.effect(callback)     # 执行可逆副作用，callback 返回 disposer 则登记销毁时调用
ctx.add_disposer(fn)     # 手动登记撤销（连接 / 文件 / 线程 / Qt 对象）
ctx.dispose()            # 逆序回滚：子上下文先销毁，再回滚自身副作用
```

提供服务、注册监听、挂载插件、派生子上下文，全部走这条路径，因此它们的
撤销都是自动的。插件作者只需记住一条铁律：**zero 不管的资源，包进 `effect`。**

## 快速开始

```python
from zero import Service, create_context, inject

class Database(Service):
    name = "db"

    def start(self):
        self.conn = connect()

    def stop(self):
        self.conn.close()

root = create_context()

def database(ctx, config):
    ctx.provide()(Database)           # 注册服务，start 立即调用

@inject(required=["db"])              # 依赖缺失则插件不加载
def feature(ctx):
    db = ctx.require("db")
    ctx.on("tick", db.ping)           # 监听随上下文销毁自动退订

root.use(database)
fork = root.use(feature)

fork.dispose()   # 监听退订、副作用回滚，root 不受影响
root.dispose()   # 子上下文先销毁，最后 stop 服务
```

## 服务与依赖

- `@inject(required=[…], optional=[…])` 声明依赖。**required 缺失时插件压根不加载**，
  因此不会注册一半监听、建一半连接
- `ctx.provide("db", impl)` 或 `@ctx.provide()` 注册服务，自动托管 `start` / `stop`
- 服务被替换 → 旧实例 `stop`、依赖它的插件自动 dispose 并重新加载（拿到新实例）
- 提供方上下文销毁 → 依赖者一起卸载；重启失败即停用，不抛异常
- 依赖**向上解析**（子可见父，兄弟不可见）；事件**向上冒泡**，两者方向相反

## 插件注册表

```python
@root.registry.add(name="database", provides=("db",))
def database(ctx):
    ctx.provide()(Database)

@root.registry.add(name="feature")
@inject(required=["db"])
def feature(ctx):
    ...

root.registry.load_all()      # 按服务依赖拓扑排序加载，循环依赖直接报错
root.registry.delete("database")  # 停用全部 fork，服务撤销，依赖者自动停用
```

- 加载顺序由 `provides` 与 `@inject(required)` 的对应关系推导（Kahn 拓扑排序），
  调用方不需要手工排
- 无人提供的服务视为外部已有（根上下文手工 `set`），不参与排序
- 可重用插件可 `load(name)` 多次产生多个 fork，`delete(name)` 一并停用

## 解析域隔离（多窗口 / 多会话）

```python
win1 = root.isolate("db", "w1")   # 声明：win1 子树内的 db 解析到 w1 域
win2 = root.isolate("db", "w2")

win1.provide("db", Database(win1))   # 服务落在 w1 域（而非全局）
win1.get("db")                       # → w1 实例
win2.get("db")                       # → w2 实例（另一份）
root.get("db")                       # → None（域外不可见）
```

- **两级解析**：先沿父链找本域槽 `(realm, key)`，未命中回落全局槽 `("", key)`
- **域随子树继承**：`win1.fork(...)` 里的插件同样解析到 w1 实例
- **未 isolate 的 key 不受影响**：全局共享一份
- **关闭窗口只动本域**：`win1.dispose()` 撤销 w1 服务、停用 w1 插件，
  w2 与全局无感；替换 w1 服务也只重启 w1 的依赖者
- 依赖图按域标注服务节点：`db@w1` / `db@w2`

## 语义约定

| 能力 | 行为 |
|---|---|
| 依赖解析 | `get` 沿上下文树**向上**查找，子可覆盖父，销毁自动恢复 |
| 事件传播 | **应用级共享总线**：任何 `ctx.emit` 全局可见，`ctx.on` 随上下文销毁自动退订 |
| 插件加载 | `use` 在子上下文执行；抛异常则整棵子树回滚，不留半截副作用 |
| 解析域 | `isolate(key, realm)` 改变 key 的解析域，用于多窗口 / 多会话隔离 |
| 生命周期 | `fork`（派生）→ `ready`（加载完成）→ `dispose`（销毁前） |

事件四种语义：`emit` 通知、`waterfall` 链式改写（不调 `next` 即短路）、
`parallel` 并发 await、`serial` 按序 await。监听器异常一律隔离。

## 与 cordis 的差异（Python 化取舍）

| cordis (TS) | zero (Python) | 原因 |
|---|---|---|
| 副作用栈 | 自建 disposer 列表逆序回滚（非 `ExitStack`） | 需要异常隔离 + 可观测 pending 数 |
| 隐式 ctx 捕获 | `contextvars` 记录当前作用域 | 显式传 ctx 为主，隐式兜底 |
| `parallel` / `serial` | `asyncio.gather` / 顺序 await | 同步监听器兼容，不强制 async |
| 类型系统 | `Protocol` + 运行时检查 | Python 无编译期类型体操 |
| 核心路径 | **同步**（仅 parallel/serial 是 async） | 副作用回滚必须确定性，异步回滚在热重载会出竞态 |

## 路线

| 阶段 | 内容 | 状态 |
|---|---|---|
| M0 | effect 原语 + Context + 生命周期事件 | ✅ |
| M1 | Service 基类 + `@inject` 依赖声明 + 服务变更触发依赖者回滚重启 | ✅ |
| M2 | 插件注册表（provides/required 推导加载顺序、delete 整体卸载、可重用插件） | ✅ |
| M3 | isolate 解析域（两级解析、域随子树继承、按域回滚） | ✅ |
| M4 | DriFox 适配层（独立包，主程序不动） | ⬜ |

## 测试

```bash
py -3 -m pytest tests/zero -q
py -3 -m ruff check zero tests/zero
```
