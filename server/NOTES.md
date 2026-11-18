# server/ M1 骨架：Qt 耦合点核查与 headless 守卫结论

对应架构文档（docs/tauri/server-arch.md §一 耦合点清单）M1 验收项。
核查日期：2026-11-18。解释器：.venv/bin/python（3.14.6，uv sync）。

## 一、hook_manager 主线程判断（清单第 7 行）

**位置**：app/core/hooks/hook_manager.py:229-239 `_is_ui_thread()`

```python
229: def _is_ui_thread() -> bool:
230:     """检测当前是否运行在 Qt 主线程（UI 线程）上"""
231:     try:
232:         from PyQt5.QtWidgets import QApplication
234:         app = QApplication.instance()
235:         if app is None:
236:             return False
237:         return QThread.currentThread() == app.thread()
238:     except ImportError:
239:         return False
```

**结论（语义兼容，零改动）**：
- 架构文档推测「instance() 即 QCoreApplication.instance()」经实读成立：
  PyQt5 中 `QApplication.instance()` 静态方法底层返回当前 QCoreApplication
  实例（无论子类），headless 下返回 QCoreApplication 实例而非 None。
- 但实装走了另一条路径：启动器 import 守卫拦截
  `PyQt5.QtWidgets`（见 server/runtime/import_guard.py），232 行 import 抛
  ImportError → 238 行 except 分支 → **恒返回 False**。
- 语义影响：所有 hook 一律按「非 UI 线程」调度。headless 下安全：服务中不存在
  UI，无 hook 需要主线程 UI 通道；风险点仅当某 hook 依赖「UI 线程存在性」做
  资源初始化——M1 的 ChatBackend 初始化路径与 M2 计划触达的 hook 均无此模式。
- 兜底：若后续出现需按「逻辑主线程」判断的 hook，用
  DRIFOX_HEADLESS=1 + monkeypatch 替换 _is_ui_thread 为
  `QThread.currentThread() == QCoreApplication.instance().thread()`，不改 app/。

## 二、tool_executor 的 Qt 引用（清单第 13 行）

**位置**：app/core/tools/tool_executor.py:182（`is_valid()` 内部）

```python
179: def is_valid(self) -> bool:
182:     from PyQt5 import sip
183:     if sip.isdeleted(self._builtin_tools):
184:         return False
185:   except Exception:
186:       pass
```

**结论（语义退化，安全）**：
- `from PyQt5 import sip` 不在拦截清单（sip 无窗口依赖），import 成功，
  is_valid 保持完整语义（QObject 存活检查可用）。
- 退一步：即便 sip 引发异常，185 行 `except Exception: pass` 吞掉后
  仅退化为「_builtin_tools 非 None 即有效」——headless 无 UI 销毁场景，等价。
- **拦截触发点实测**：QtWidgets 在服务启动链中被守卫拦截 4 次（全部命中
  except ImportError 兼容分支），详见下文「启动链触达面」。

## 三、tool_permission_controller 的 Qt 依赖

**位置**：app/core/tools/tool_permission_controller.py:19
`from PyQt5.QtCore import QObject, pyqtSignal`（模块级）

**结论（保留 QtCore 是关键设计）**：
- 守卫只禁运 QtWidgets/QtGui，QtCore 正常加载 → 该模块零改动可用，
  实测构造成功（含 Settings 全局偏好加载、插件工具全量注册链）。
- pyqtSignal 依赖 QCoreApplication 事件循环投递跨线程信号——服务主线程
  QCoreApplication.exec() 在跑，QueuedConnection 语义完整。
- 注入顺序照搬主程序：构造 ChatBackend 后
  set_tool_permission_controller(ToolPermissionController())（backend.py:319
  契约「必须在 initialize 之前调用」）。

## 四、启动链触达面实测（guard 日志留痕）

实装守卫：server/runtime/import_guard.py（三层）：
1. meta_path 拦截器：PyQt5.QtWidgets / PyQt5.QtGui 直接 import 即 ImportError
   （日志标记 `[qt-block] 拦截 import: <模块名>`）
2. qfluentwidgets stub 父包：短路顶层 __init__ 全量组件拉取，仅保留
   common/config.py 真实现（QConfig 等 9 个配置类 + qconfig，
   恰好覆盖 app/utils/config.py:26 的导入面，语义零漂移）
3. QtGui 占位类工厂：QFont/QIcon/QIconEngine/QPixmap/QCloseEvent/
   QResizeEvent 等值对象名按需生成空壳类并留痕（服务路径无调用方）；
   QColor 用带语义的最小替身（common/config.py:10 模块级 import 需要）

实测触发（ChatBackend headless 初始化 + ToolPermissionController 全链）：
- QtWidgets 拦截 4 次（含 hook_manager.py:232、插件 UI 模块链）
- QtGui 占位 6 个名字（见上）
- ChatBackend.initialize 同步段完成（SessionManager/HookManager/AgentManager/
  HistoryManager），事件桥接通 19 个 pyqtSignal（含 stream_chunk/
  tool_executor_ready/permission_requested/message_received 等）
- engines/base.py 的「1 处 Qt 引用」：初始化路径未触达（无报错日志）

## 五、M1 验收证据（verify_m1.sh exit=0）

- 握手：stdout 独占一行 `{"drifox_server":{"port":N,"pid":P,"version":"0.1.0"}}`
  （端口就绪即打，backend_ready 走 /api/health）
- GET /api/health → 200，drifox_server 嵌套体
- WS /ws 首帧 hello：
  `{"v":1,"type":"event","name":"hello","seq":2,"ts":...,"sid":"","payload":
  {"server_version":"0.1.0","seq":1,"active_sessions":0}}`
- 命令通路闭环：WS client_cmd ping → command_queue → Qt 主线程
  CommandPump(10ms) → EventBridge.emit_custom(pong) → WS 广播
  `{"cid":"c1"}`（双队列转发路径全通）
- 拦截证据：LOG 含 `[qt-block]` 标记；反向断言（无未拦截 Qt import）通过

## 六、已知限制（M2 待办）

1. get_model_config 返回空 dict（main.py TODO），真模型配置 M2 接 Settings
2. 命令仅支持 ping，ChatBackend 方法命令分发与回执 M2 做
3. 重连缓冲/seq 重放未实装（架构文档 §三，M2 范围）
4. handshake 先于 backend_ready：壳若需等待引擎就绪应轮询 /api/health 的
   backend_ready 字段

## 七、🟡3 审计结论（M2b，2026-11-18）

**问题确认**：QtGui stub 的占位类让 `from PyQt5.QtGui import X` 成功拿到占位类，
后续使用抛 AttributeError（非 ImportError）→ app/ 侧 `except ImportError` 降级
分支全部失效。

**触达面审计**：
- app/ 内 `from PyQt5.QtGui import` 共 105 处（grep 实测）；**零处**包裹在
  except ImportError 降级分支内（grep except ImportError 附近无 QtGui 命中）
  ——即「import 成功但运行时 AttributeError」在当前 app/ 代码上并不触发任何
  降级分支，风险面是**未来代码**与「占位类被实例化/调用」的静默行为漂移。
- M2b 触达路径（ChatBackend 初始化链 + 会话/发送链）实测仅 import 不调用
  （QFont/QIcon/QIconEngine/QPixmap/QCloseEvent/QResizeEvent 六个名字来自
  utils.py:27 与插件 UI 模块链的模块级 import）。
- M2c 前瞻：插件 register_ui 构造 widget 时占位类将被 ui_shim 路径 B 的假
  QWidget 桩取代，占位类语义届时退役。

**加固已实施**（server/runtime/import_guard.py）：占位类 `__init__` 与实例
`__getattr__` 改抛 **ImportError**（带 DRIFOX_HEADLESS=1 前缀）——import 成功
但首次构造/属性访问即恢复「模块缺失」原语义，app/ 侧 except ImportError 兜底
重新生效；QColor 保持 _FakeColor（config.py:10 模块级 import 的硬依赖，无窗口
依赖）。回归：M1 + 场景 1-4 全绿，插件加载链无新增失败（占位类当前只被引用
未被调用）。

**残留风险登记**：① `isinstance(x, QIcon)` 类检查语义偏移（无异常，M2c shim
落地时复核）；② 若未来插件在模块级实例化 QtGui 对象，加载将从「静默占位」变
「显式 ImportError」——属预期禁运行为，插件加载失败日志可见。
