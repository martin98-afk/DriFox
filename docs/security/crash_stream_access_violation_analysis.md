# 崩溃分析：流式响应取消导致的 Windows access violation

> 对应崩溃报告：Windows fatal exception: access violation
> 栈顶 `openai/_streaming.py:53 _iter_events` → `chat_worker.py:3424 _process_response`
> → `chat_worker.py:3149 _make_api_call` → `chat_worker.py:1853 run`
> 分析日期：2026-09-07

## 1. 结论先行

崩溃不是 openai 库的 bug，而是**本项目对流式响应对象的生命周期管理存在跨线程裂缝**：

`cancel()` 在 GUI 线程调用 `self._current_response.close()`，而 worker 线程此刻正阻塞在
`for chunk in response` 的 C 层读取里。`close()` 会释放 httpcore 连接与 SSL 对象，
读线程随后访问已被释放的对象 → use-after-free → Windows access violation。

栈顶点恰好落在 `openai/_streaming.py:_iter_events`（迭代推进入口），并且报告同时给出
`ssl.py read` 帧，是这条链路的典型特征。

## 2. 调用链还原

```
GUI 线程                              worker 线程
-----------------------------------   ------------------------------------------
用户点「停止」
ConversationExecutor.cancel()
  executor.py:362 / :487
    worker.cancel()
      chat_worker.py:1151-1157
        response.close()  ───────────►  for chunk in response:              (3424)
                                          openai/_streaming.py __iter__      (49)
                                          openai/_streaming.py __stream__    (62)
                                          openai/_streaming.py _iter_events  (53)
                                            httpx.Response.iter_bytes
                                              httpcore stream read
                                                ssl.SSLObject.read()   ← 阻塞中
        ↑ 释放 httpcore 连接 + SSL 对象      ↓ 继续使用已释放对象
        └────────────── access violation ───┘
```

`_current_response` 的赋值全在 worker 线程（`_make_api_call` / `_process_response` /
`_process_responses_stream`），读取与 `close()` 全在 GUI 线程 —— 一个没有任何同步的裸共享变量。

## 3. 缝隙清单

| # | 级别 | 位置 | 问题 |
|---|---|---|---|
| 1 | P0 | `cancel()` 1151-1157 | 跨线程 `close()` 正在被读取的流式响应 → use-after-free |
| 2 | P0 | `_process_response` / `_process_responses_stream` / `_make_api_call` | 流式响应**从不 close**，连接不归还 httpx 池 |
| 3 | P1 | `_current_response` | 无锁共享，cancel 可能 close 掉新一轮刚建立的响应 |
| 4 | P1 | `cleanup()` 1399-1402 | 主线程清理与尚未完全退出的 worker 线程竞争 |
| 5 | P1 | 取消时的异常类型 | 只 catch `httpx.ReadError/httpcore.ReadError`，实际可能是 `ssl.SSLEOFError`/`OSError` |
| 6 | P2 | `httpx.Timeout(600.0)` | 兜底唤醒失败时，取消后最长阻塞 600s |
| 7 | 诊断 | 线程名 `tool_parallel_0` | 与栈内容矛盾，需下次 dump 复核 |

### 2.1 P0-1 跨线程 close（主因）

旧代码：

```python
# cancel()
if self._current_response is not None:
    try:
        self._current_response.close()   # GUI 线程
    except Exception:
        pass
    self._current_response = None
```

`close()` 会一路释放 httpx Response → httpcore 连接 → SSL 对象，而 worker 线程正处在
`ssl.SSLObject.read()` 内部。这不是 Python 层异常能兜住的，是进程级崩溃。

### 2.2 P0-2 流式响应从不 close（连接泄漏）

所有退出路径（正常 return、取消 return、抛 `StreamInterruptedError`、重试前 `continue`）
都只是把 `_current_response` 置 None，**没有一次调用过 `response.close()`**。

后果：

- 未消费完/未关闭的流式响应不会把连接归还 httpx 连接池；
- 每轮工具迭代泄漏一条，`max_retries=15` 的重试成倍放大；
- 池耗尽后新请求挂起 → 用户以为卡死 → 点「停止」→ 触发 P0-1。

即：泄漏是崩溃的**放大器**，也是崩溃多发生在"工具并行迭代中"的原因。

### 2.3 P1-1 无锁共享 `_current_response`

worker 线程在 `3397 / 3988` 赋新值，GUI 线程在 `1152` 读并 close，主线程在 `1402` 清 None。
无锁 → cancel 拿到的可能是上一轮已完成的响应（无害），也可能是新一轮刚建立、正在读的响应（致命）。

### 2.4 P1-2 cleanup 与线程退出窗口

`ConversationExecutor._finalize_worker_cleanup` 用 `worker.isRunning()` 判断后才调 `cleanup()`；
但 `isRunning()` 转 False 与 `run()` 真正结束之间仍有窗口，期间主线程执行
`_http_client = None` / `_current_response = None`，与 worker 线程形成数据竞争。

### 2.5 P1-3 取消时的异常类型不确定

中断底层 socket 后，读操作可能抛 `httpx.ReadError`、`httpcore.ReadError`、
`ssl.SSLEOFError`、`OSError`、`ValueError` 中的任意一种。旧代码只认前两种，
其余会掉进通用 `except` → 判为不可重试 → 弹网络错误，或误走 15 次 ×5s 重试。

### 2.6 P2 600s 读超时

`_get_http_client()` 用 `httpx.Timeout(600.0, connect=60.0)`。若兜底的 socket shutdown
没挖到底层 socket，取消后 worker 最长要等 600s 才从阻塞读返回。
（未改默认值：调小会让长思考模型断流。）

### 2.7 诊断：线程名与栈不一致

报告标注崩溃线程为 `tool_parallel_0`，该前缀来自 `chat_worker.py:57` 的
`_SHARED_TOOL_POOL`（并行工具线程池）；但栈底是 `chat_worker.py:1853 run`（QThread）。
全仓检索未发现把 `run()` 提交进该线程池的调用点。

判断：应以 faulthandler dump 中 `Current thread` 标记的那一段为准，报告里的线程名大概率是
人工/模型整理时的张冠李戴。为便于下次确认，已在流式读取入口加一行取证日志：

```
[Stream] 开始流式读取 thread=<线程名> cancelled=<bool>
```

若下次 dump 确认真的是 `tool_parallel_*`，则说明有工具/插件在池线程内发起了 LLM 流式调用，
需要把该路径也纳入本节的保护范围（同样禁止跨线程 close）。

## 4. 已落地的修复

补丁脚本：`tools/apply_stream_av_hardening_patch.py`（幂等，支持 `--revert`）
目标文件：`app/core/workers/chat_worker.py`

1. **`_abort_current_stream()`**（替代 cancel 里的裸 close）
   - worker 不在底层读（Python 层安全点）→ 调用线程直接 `close()`；
   - worker 正在底层读 → 只置 `_stream_abort_pending` 并 `shutdown(SHUT_RDWR)`
     底层 socket 唤醒它，**释放动作留给 worker 线程自己完成**。
2. **`_find_stream_socket()` / `_shutdown_stream_socket()`**
   - 从 openai Stream → httpx.Response → httpcore stream 递归挖出 socket，
     优先走 httpcore 的 `get_extra_info("socket")`，失败降级到 `_connection/_socket/_sock`；
     挖不到就返回 False，降级为等待 worker 自行退出（绝不强行 close）。
3. **`_guarded_stream_iter()`**
   - 包裹两处流式迭代（`_process_response`、`_process_responses_stream`）；
   - 用 `_stream_in_read` 精确标注"是否处在 C 层读"，供 cancel 决策；
   - 取消引发的读异常一律按流结束处理，不再误判成网络故障。
4. **`_finish_stream_response()` + `try/finally`**
   - 在 `_make_api_call` 中统一收尾：正常 / 取消 / 异常 / 重试前丢弃四条路径全覆盖，
     同线程 close，彻底消除连接泄漏。
5. **`_stream_lock`（RLock）**
   - 保护 `_current_response` 的读/写/清理，覆盖 `cancel()`、`_make_api_call` 开头、`cleanup()`。
6. **取证日志**：流式读取入口记录真实线程名。
7. **类级默认值**（踩坑）：`_stream_lock` / `_stream_in_read` / `_stream_abort_pending` /
   `_current_response` 必须声明为**类属性**。`tests/core/test_retry_queue_full_503.py`
   等用 `OpenAIChatWorker.__new__()` 构造测试桩，不走 `__init__`；QObject 未初始化时
   读取缺失的实例属性抛的是 `RuntimeError: super-class __init__() ... was never called`
   （不是 AttributeError），首轮补丁因此打挂了 4 个用例。类里已有 `_loop_policy_id`
   的同类约定，本次沿用同一写法。

### 验证

- `py_compile` 通过；`ruff check` 通过；行尾守卫 `eol_guard check` 通过（无整文件翻转）。
- 补丁 `--revert` / 再应用往返后文件与备份逐字节一致（幂等已验证）。
- `pytest` 相关用例 44 passed（含 `test_retry_queue_full_503.py` 的 4 个 `_make_api_call` 用例）。
- `tests/core` 全量：54 failed / 1075 passed / 5 errors，与改动前的基线（58 / 1071 / 5，
  其中 4 个失败正是上一节第 7 条引入）相比无新增失败，`_PreSendWorker` 等失败为预存项。

## 5. 遗留项与后续建议

1. **兜底唤醒失败时仍可能长阻塞**（P2）：如需更强保证，可在"取消且 3s 内 worker 未退出"
   时由 worker 线程自身的定时器 close，而不是让 GUI 线程 close。
2. **`subagent_worker.SubAgentExecutor`** 是独立 QThread，未复用 `_current_response` 保护；
   若它也有独立流式取消路径，建议按同一模式加固。
3. **复现建议**：崩溃属竞态，本地难稳定复现。建议保留 `logs/crash/` 下的 dump 与
   `Report.wer`（含崩溃模块签名 P4/P7），下次复现时用它们确认崩溃模块到底是
   `_ssl.pyd` / `Qt5Core.dll` 还是 `libcrypto`，可进一步坐实本节结论。
