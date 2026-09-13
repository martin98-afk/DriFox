# 崩溃风险审计（续）

> 范围：全仓排查「进程级崩溃」风险（access violation / abort），不含普通 Python 异常与功能 bug。
> 方法：分四路并行静态审计（Qt 跨线程与对象生命周期 / QWebEngine 与渲染 / 线程·子进程·资源 / 数据层与 C 扩展）
> + 关键结论人工复核。日期：2026-09-07
> 前情：`reports/crash_stream_access_violation_analysis.md`（流式响应跨线程 close，已修）

## 摘要

| 级别 | 数量 | 说明 |
|---|---|---|
| P0 | 2 | 其中 1 条（worker 线程 HeapCompact）**本次已直接修复** |
| P1 | 6 | 需逐个确认，多数为条件触发 |
| P2 | 4 | 资源泄漏 / 无界增长，长期运行才显现 |
| 已排查安全 | 6 类 | 见文末 |
| 误报澄清 | 1 条 | `except A, B:` 不是语法错误 |

---

## P0

### P0-1 worker 线程里调用 `HeapCompact` —— 已被项目实测确认 100% 抛 access violation ✅ **已修复**

**位置**：`app/core/workers/chat_worker.py:3886-3898`（流式读取循环内）

原代码在 RSS 增量 > 200MB 时执行：

```python
msvcrt = ctypes.CDLL("msvcrt.dll")
msvcrt._heapmin()
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
heap = kernel32.GetProcessHeap()
kernel32.HeapCompact(heap, 0)
```

**为什么危险**：项目自己的注释（`chat_worker.py:5184`、`main_widget.py:21348`）已经写明——
> T10 实测：Python 3.14 下 HeapCompact **100% 抛 access violation**（被 except 吞掉）

关键在于「被 except 吞掉」**不等于安全**。Windows 的结构化异常被 Python 的 `try/except`
接住后进程继续跑，但进程堆已经被触碰过，进程从此处于不可信状态；之后任何一次
分配/释放都可能随机崩溃，而**崩溃点会落在完全无关的代码上**——比如
`openai/_streaming.py`。这正是本次崩溃「栈点离奇、难归因」的最合理解释之一。

叠加因素：这段代码跑在 worker 线程，与主线程 / Qt 的并发堆操作同时进行。

**触发条件**：`MEM_DIAG=1` 环境变量 + 流式期间 RSS 增量 > 200MB（默认不开，但排查内存问题时常会开）。

**已修复**：删除 `_heapmin()` 与 `HeapCompact()`，只保留 `gc.collect()`，并就地写明原因注释。
回归测试 44 passed。

**同类残留**：`plugins/system-cleaner/ui/scanner.py:317` 有同样的 `HeapCompact`
（在「内存清理」功能里，用户手动触发，有 `except: pass`）。建议同样删除，
保留 `SetProcessWorkingSetSize` 即可。**本次未改**，等你确认。

### P0-2 DirectConnection 把 13 个信号直连给插件回调，回调在 worker 线程同步执行

**位置**：`app/core/conversation/executor.py:436`（`_connect_callbacks`）
调用方：`app/core/conversation/engine_session.py:215`（`direct_signals=True`）

`content_received / tool_call_started / question_asked / permission_approval_requested ...`
共 13 个信号被 `Qt.DirectConnection` 连接，回调**在 ChatWorker 的 OS 线程内同步执行**。
EngineSession 只把 `finished / error / messages_updated` 换成线程安全的 `_SyncAdapter`，
**其余 key 原样透传插件传入的回调**。

真实调用方是插件 daemon 线程，例如：
- `.drifox/plugins/cron-tasks/crontasks_core/executor.py:100-115`（`threading.Thread(daemon=True, name="cron-turn")`）
- `plugins/assistant_hub/core/memory/ticker.py:80`

**崩溃条件**：任何插件往这些 key 传一个会操作 QWidget / Qt 对象的回调 →
非 GUI 线程写 C++ QWidget → access violation。

**为什么还没大规模爆**：目前使用的插件回调大多只做数据收集，没碰 UI。但这是**架构上没有护栏**，
任何插件作者写错一行就会打挂主程序。建议：核心信号的插件回调统一包一层
「异常隔离 + 线程断言」，或在引擎会话侧把回调派发回主线程。

---

## P1

### P1-1 `QTimer.singleShot` 回调持有已销毁卡片

`app/widgets/cards/settings/sub_agent_session_card.py:324-332`
`_auto_scroll_latest` 用 `QTimer.singleShot(100, lambda: self._web_view.page().runJavaScript(...))`，
lambda 强捕获 `self` 且**无 try/except**（同文件 306-309 的同类代码有 `except RuntimeError`）。
停止轮询时 `_stop_polling()` 只停 `_poll_timer`，**不取消已排队的 singleShot**；
100ms 内卡片被 `deleteLater` 后回调触发 → 触碰已销毁 C++ 对象。
PyQt5 对槽中未捕获的 Python 异常会 `qFatal()` → 进程 abort（不是普通报错）。

### P1-2 嵌套 `QEventLoop` 期间对象被回收

`app/widgets/message_card.py:9868-9888`（`_run_js_sync`）与 `10073-10120`（`_capture_full_content_1x`）
`QEventLoop.exec_()` 外层还套了 `stable_loop.exec_()` 与 `QApplication.processEvents()`（10113）。
嵌套循环期间事件继续派发，用户关 tab / 触发虚拟回收 → viewer 走 `deleteLater`；
循环返回后 `self.setFixedHeight()`（10111）、`self._grab_render_widget()`（10121）触碰已销毁对象。
导出 PNG 路径可达。

### P1-3 归属 daemon 线程的 QThread 调 `deleteLater`

`app/core/conversation/executor.py:197`、`app/core/conversation/engine_session.py:289-293`
QObject 的 thread affinity 是那个**没有事件循环的 daemon 线程**，`DeferredDelete` 永不投递
→ C++ QThread 对象泄漏；daemon 线程退出后再从主线程调用，等于往已释放的 `QThreadData` 投递事件。
`engine_session.py:290` 的注释已承认这一点，但仍在调。

### P1-4 线程守卫在 daemon 线程场景静默失效

`app/utils/thread_guard.py:73`
`original_init(self, _thread_anchor)` 强制把 QThread 的 parent 设成主线程对象。
QThread 若创建于 daemon 线程，Qt5 的 `check_parent_thread()` 判定跨线程，
打印 `QObject: Cannot create children for a parent that is in a different thread.` 后**把 parent 置空**。
守卫恰好在最需要它的场景（cron-tasks / assistant_hub 后台线程 new ChatWorker）失效，只留下强引用。

### P1-5 类级 QTimer 的跨线程 stop / deleteLater

`app/widgets/cards/settings/../main_widget.py:9124`（`_theme_batch_timer`，无 parent，类变量跨窗口持有）
`_on_settings_config_changed` 缺少线程断言；一旦从非 GUI 线程进入，timer 归属该线程且永不触发，
而主线程下一轮会执行 `stop()`（9120）/ `deleteLater()`（9143）——`QTimer::stop()` 触碰**归属线程**的
event dispatcher，是标准的跨线程 C++ 竞争。条件触发（需非 GUI 线程进该函数）。

### P1-6 提问等待循环里未判空

`app/core/workers/chat_worker.py:2179-2195`
`q = self._question_pending` 提前捕获后进入 `while ... _answer_event.wait(1.0)`。
期间 GUI 线程 `cleanup()` 会把 `_question_pending` 置 None 并复位 `_is_cancelled=False`；
循环退出后 2191 `q.get(...)` / 2195 `q["tool_call_id"]` 未判 `q is None` → 抛 `AttributeError/TypeError`。
属 Python 异常（不会直接崩进程），但发生在 `QThread.run()` 内，可能中断正常的收尾流程。

---

## P2（资源泄漏 / 无界增长，长期运行显现）

1. **`app/tools/bg_manager.py:207-243`**：`stdin=subprocess.PIPE` 全程不写不关，任务自然结束后
   从不 `process.wait()`（只有 `stop()` 路径调）；`_tasks` 无条数上限。读 stdin 的命令会永久挂起，
   Popen 与 OS 句柄长期累积。
2. **`app/tools/bg_manager.py:202, 245-246`**：`ProcessJob()` 先建，随后启动异常被 except 吞掉返回，
   **该分支从不 `job.close()`** → Job 句柄泄漏，kill-on-close 失效。
3. **线程池无界队列且从不 shutdown**：`message_card.py:185`（`_RENDER_POOL`）、
   `chat_worker.py:58`（`_SHARED_TOOL_POOL`）、`hook_manager.py:37-45`（`_PARALLEL_EXECUTOR`）；
   `_get_parallel_executor()` 无锁，并发首次调用可能创建两个池。
4. **缓存无上限**：`message_card.py:4035` `_tool_md_cache` 无条数/字节上限；
   `app/utils/diff_viewer.py:1793, 1824-1827` 每次 `load_html` 建临时文件，只有 dispose/close 才清理
   → `%TEMP%` 下 HTML 累积。

---

## 已排查，确认安全

- **WebEngine 初始化顺序**：`main.py:142` 在 `145` 行创建 `QApplication` **之前**导入
  `QtWebEngineWidgets`（顺序错误会导致启动即崩）—— 正确。
- **跨线程触碰 WebEngine**：46 处 `runJavaScript` 全在 GUI 线程；`_RENDER_POOL` worker 只做纯 Python
  Markdown 渲染，不碰 Qt；`init_shared_web_profile` 仅启动期主线程调用。
- **runJavaScript 回调悬垂**：关键路径已有 `weakref` + `except RuntimeError` + `sip.isdeleted()` 保护。
- **跨线程 QPixmap / QPainter**：worker 侧只用 `QImage.fromData`（QImage 允许非 GUI 线程），未发现违规。
- **`moveToThread`**：`app/` 内 0 处；QThread 子类的线程归属未见经典错误用法。
- **WebEngine 缓存配额**：`_skeleton_cache` LRU 上限 48，`_MAX_PAYLOAD_B64` 8MB，均有配额。

## 误报澄清

审计过程中有一条「**10 个核心模块存在 `except A, B:` 语法错误**」的 P0 判读，**是误报**：
`except A, B:` 是 **PEP 758**（Python 3.14 新增的无括号多异常语法），本项目运行的就是 3.14.2。
用 <3.14 的解释器做 `compileall` 会误报。**自检务必用项目的 `.venv/Scripts/python.exe`（3.14）**，
反向也一样：注解缺 import 之类只在 <3.14 才暴露的问题，别只在 3.14 上跑自检。

## 建议处置顺序

1. ✅ **已完成**：worker 线程 HeapCompact（P0-1）
2. **建议一起删**：`plugins/system-cleaner/ui/scanner.py:317`（同一 API，同一风险，1 行改动）
3. **加护栏**：P0-2 的插件回调异常隔离（改动小、收益高，能挡住一整类「插件写崩主程序」）
4. **补保护**：P1-1、P1-2（都是给回调加 `RuntimeError` 保护 / 取消排队定时器，各几行）
5. **观察项**：P1-3 ~ P1-6（需结合真机日志判断实际触发频率）
6. **长周期**：P2 的资源泄漏项，建议排进常规迭代

需要我接着修 2~4 项吗？2 和 4 都是几行的改动，风险很低。
