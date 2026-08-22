# 实施蓝图 / 实施任务卡（子任务 #4.5 · P0 预备）

> 生成时间：2026-08-22　分支：`dev`（工作树干净）
> 依据：#4.2 已识别的 **65 处 `[PERF]` 注释 + 6 处 TODO** + benchmarks 结论
> （对话渲染链每轮 **+556~576KB RSS**，R²=0.99，Qt C++ 侧累积：样式/字体/tooltip 缓存；渲染管线本身仅 ~20KB/轮）
> 状态：**PRE-诊断蓝图**。等 #1/#2/#3 诊断报告后，build 按本卡逐条派单实施。
> 约束：不修改任何产品代码（本文件仅为蓝图）；不引入新三方依赖；推荐方案须可执行。

## 使用说明
- 每条候选含 ①②③④⑤⑥⑦⑧ 八项。
- ① 文件:行号（D:/work/DriFox 下路径）　② 现状代码片段（≤15 行）　③ 推荐代码片段（≤20 行）
- ④ 预期收益类别：CPU / 内存 / 帧率 / 启动　⑤ 实施难度 1-5　⑥ 风险等级 1-5
- ⑦ 验证：用 `scripts/measure_perf.py` + `tests/perf/` 回归　⑧ 回滚：`git revert <commit>`
- **重要**：多数 `[PERF]` 注释记录的是**已落地**优化（"已改为/已足够/已移入"）。本蓝图的候选把它转为
  **"参数化/自适应/扩展/补回归护城河"** 的可执行动作；真正的净新增优化集中在 **内存渲染链（Top 优先）** 与 **TODO**。

---

## Top-15 实施候选清单

### C01 · 对话渲染链 Qt C++ 侧内存累积治理（Top 优先，来自 benchmarks 结论）
- ① `app/widgets/message_card.py`（`MessageCard` / `CodeWebViewer` 构造与 `deleteLater` 路径，参考 `bench_chat_pipeline.py` 结论）
- ② 现状：每轮 `MessageCard` 构造→`deleteLater` 后 RSS 仍 +556~576KB（R²=0.99），tracemalloc 仅 +3.4KB/轮 → 泄漏在 Qt C++ 侧（样式表/字体/tooltip 缓存未随卡片销毁释放）。
  ```python
  # bench_chat_pipeline.py 结论片段
  # 每轮 RSS +556~576 KB（R²=0.99），Python 堆仅 3.4KB/轮
  # → Qt C++ 侧累积（疑样式/字体/tooltip 缓存）
  ```
- ③ 推荐：在卡片 `deleteLater` 前显式清理可释放的 Qt 资源，并复用单例 `QTextDocument`/`QWebEnginePage` 池，避免每卡新建：
  ```python
  def _dispose_resources(self):
      # 1) 解绑信号，断开对 viewer 的强引用（已有 B4 回收层，此处补 C++ 侧）
      self._content_data = None
      viewer = getattr(self, "viewer", None)
      if viewer is not None:
          viewer.page().setHtml("")          # 释放上一页 DOM/tooltip 缓存
          viewer.setStyleSheet("")            # 清空 per-card QSS，避免样式表缓存累积
      # 2) 归还 viewer 到池（若 CodeWebViewer 已实现对象池）
      _viewer_pool.release(viewer)
  ```
- ④ 内存（最高优先）　⑤ 4　⑥ 4（需 reviewer 参与，误清会崩）
- ⑦ `uv run python benchmarks/bench_chat_pipeline.py --rounds 30 --chunks 40` 看 RSS 斜率是否降到 ~20KB/轮；`scripts/measure_perf.py --scenario memory` 对比基线；`tests/perf/test_memory_timer_and_branch_cache.py` 护体。
- ⑧ `git revert <实施 commit>`；保留 B4 回收层不删。

### C02 · 机器性能自适应阈值（落地 TODO #969）
- ① `app/main_widget.py:967-969`（`_TEMPLATE_JOIN_DELAY_MS = 300`）
- ② 现状：延迟/批量/定时器阈值硬编码，无机器分级。
  ```python
  # TODO: 根据用户机器性能动态调整此值
  _TEMPLATE_JOIN_DELAY_MS: int = 300
  ```
- ③ 推荐：引入机器档位（CPU 核数/内存），档位→系数映射，统一常量集中管理。
  ```python
  def _perf_tier() -> float:
      import psutil
      mem_gb = psutil.virtual_memory().total / 1e9
      return 0.6 if mem_gb < 8 else 1.0 if mem_gb < 16 else 1.4
  _PERF_TIER = _perf_tier()
  _TEMPLATE_JOIN_DELAY_MS: int = int(300 * (2 - _PERF_TIER))  # 弱机更长缓冲
  ```
- ④ 启动 / 帧率（弱机感知提升）　⑤ 2　⑥ 2
- ⑦ `scripts/measure_perf.py --scenario startup`；`tests/perf/test_startup_init_lazy.py` 断言延迟段存在。
- ⑧ `git revert <实施 commit>`。

### C03 · 延迟段错峰间隔参数化（backend.py:659-680）
- ① `app/core/backend.py:658-680`
- ② 现状：错峰间隔 0/200/400/600ms 硬编码。
  ```python
  QTimer.singleShot(0, self._deferred_create_memory_manager)
  QTimer.singleShot(200, self._deferred_create_tool_executor)
  QTimer.singleShot(400, self._deferred_create_engines)
  QTimer.singleShot(600, self._deferred_create_sub_agent_and_misc)
  ```
- ③ 推荐：抽出为类常量并按 C02 的 `_PERF_TIER` 缩放。
  ```python
  _DEFER_MS = (0, 200, 400, 600)
  def _schedule_deferred(self):
      for i, m in enumerate(self._DEFER_MS):
          QTimer.singleShot(int(m * (2 - _PERF_TIER)), self._deferred_fns[i])
  ```
- ④ 启动　⑤ 1　⑥ 1
- ⑦ `scripts/measure_perf.py --scenario startup`；`tests/perf/test_startup_init_lazy.py`。
- ⑧ `git revert <实施 commit>`。

### C04 · 首帧后非关键初始化窗口参数化（main_widget.py:2324-2332）
- ① `app/main_widget.py:2324-2332`
- ② 现状：1500/2000/2500ms 硬编码分散。
  ```python
  QTimer.singleShot(1500, lambda: self._safe_timer_call(self._load_model_configs))
  QTimer.singleShot(2000, lambda: self._safe_timer_call(self._sync_working_directory))
  QTimer.singleShot(2500, lambda: self._safe_timer_call(self._on_initialization_complete))
  ```
- ③ 推荐：集中为常量，随档位缩放（强机更早完成）。
  ```python
  _INIT_SPREAD = (1500, 2000, 2500)
  for i, ms in enumerate(_INIT_SPREAD):
      QTimer.singleShot(int(ms * (2 - _PERF_TIER)), ...)
  ```
- ④ 启动 / 帧率　⑤ 1　⑥ 1
- ⑦ `tests/perf/test_startup_init_lazy.py`（断言首 6 行无同步直调）。
- ⑧ `git revert <实施 commit>`。

### C05 · 时间片预算常量化 + 自适应（main_widget.py:12101-12121）
- ① `app/main_widget.py:12109`（`TIME_SLICE_MS = 16`）
- ② 现状：16ms 时间片、80ms 批次间隔硬编码（见 12106/12165 注释）。
  ```python
  TIME_SLICE_MS = 16
  ...
  QTimer.singleShot(80, self._process_next_lazy_batch)
  ```
- ③ 推荐：提为模块常量，按档位缩放（弱机更小时间片避免卡顿）。
  ```python
  LAZY_TIME_SLICE_MS = int(16 * (2 - _PERF_TIER))
  LAZY_BATCH_GAP_MS = int(80 * (2 - _PERF_TIER))
  ```
- ④ 帧率 / CPU　⑤ 1　⑥ 1
- ⑦ `scripts/measure_perf.py --scenario animation`；`tests/perf/test_message_card_paint_throttle.py`（断言 50ms 定时器 + 缓存渐变）。
- ⑧ `git revert <实施 commit>`。

### C06 · processEvents 限量阈值参数化（main_widget.py:2170-2173）
- ① `app/main_widget.py:2173`（`QApplication.processEvents(QEventLoop.AllEvents, 5)`）
- ② 现状：5ms 全量事件处理上限硬编码。
  ```python
  # [PERF] 限量版：最多处理 5ms 事件（全量 processEvents 会无界扫描 pending 事件队列...）
  QApplication.processEvents(QEventLoop.AllEvents, 5)
  ```
- ③ 推荐：提为常量，强机可稍放宽以更快 flush 文本。
  ```python
  TOOL_SYNC_PROCESS_MS = int(5 * (2 - _PERF_TIER))
  QApplication.processEvents(QEventLoop.AllEvents, TOOL_SYNC_PROCESS_MS)
  ```
- ④ 帧率 / CPU　⑤ 1　⑥ 2（改错值会拖慢主线程，需回归）
- ⑦ `scripts/measure_perf.py --scenario animation`；`tests/widgets/test_tab_drag_perf.py` 帧耗时。
- ⑧ `git revert <实施 commit>`。

### C07 · 流式批量 emit 阈值参数化（chat_worker.py:3417-3439）
- ① `app/core/workers/chat_worker.py:3423,3439`
- ② 现状：20 字符/80ms（reasoning）、30 字符/80ms（content）硬编码。
  ```python
  if len(_reasoning_batch) >= 20 or (now - _reasoning_batch_time) > 0.08:
  if len(_content_batch) >= 30 or (now - _content_batch_time) > 0.08:
  ```
- ③ 推荐：提为常量，按网络/机器档位调（弱网更长批量减少渲染）。
  ```python
  _REASON_BATCH_CH = 20; _CONTENT_BATCH_CH = 30
  _EMIT_INTERVAL_S = 0.08 * (2 - _PERF_TIER)
  ```
- ④ 帧率 / CPU（减少 WebEngine 渲染次数）　⑤ 1　⑥ 2
- ⑦ `scripts/measure_perf.py --scenario animation`；`tests/test_message_card_compact_perf.py`。
- ⑧ `git revert <实施 commit>`。

### C08 · 主题刷新 findChildren 短路再前置（main_widget.py:9855-9865）
- ① `app/main_widget.py:9857`（已短路，但 findChildren 全树扫描仍发生）
- ② 现状：仅当 `not is_color and not is_font` 才早退；否则仍全树 `findChildren`。
  ```python
  if not is_color and not is_font:
      ThemeRefreshCoordinator.timer_end("total")
      return
  ```
- ③ 推荐：在更外层（调用前）用「脏标记位图」跳过整段刷新；对单卡刷新用缓存的子树引用而非全树扫。
  ```python
  # 仅当本窗口确有该 scope 的脏标记才进入扫描
  if not self._theme_dirty_mask & scope_bit:
      return
  # 单卡刷新走 card.refresh_style() 而非 main_widget 全树
  for card in changed_cards:
      card.refresh_style()
  ```
- ④ CPU（主题切换/热重载卡顿）　⑤ 3　⑥ 3（需与 ThemeRefreshCoordinator 协同）
- ⑦ `benchmarks/bench_startup.py` 启动段；`tests/perf/test_message_card_paint_throttle.py` 断言缓存 `_grad_*`/`_clip_*`。
- ⑧ `git revert <实施 commit>`。

### C09 · 持久化 flush 批处理/合并（main_widget.py:17110-17122）
- ① `app/main_widget.py:17115-17122`（save 即时、flush 延迟）
- ② 现状：每次流式结束都 save + 延迟 flush，flush 未去重。
  ```python
  self._save_current_session_to_history()
  # flush 延迟到 _do_post_stream_cleanup
  QTimer.singleShot(0, self._do_post_stream_cleanup)
  ```
- ③ 推荐：对高频 flush 做合并且用 `QTimer.singleShot` 去重（已有 `_session_dirty` 标记可复用）；flush 合并窗口 ~200ms。
  ```python
  if not self._flush_pending:
      self._flush_pending = True
      QTimer.singleShot(200, self._flush_once)  # 合并多次 flush
  ```
- ④ CPU / IO（减少 fsync 次数）　⑤ 2　⑥ 2
- ⑦ `scripts/measure_perf.py --scenario memory`；`tests/test_perf_regression.py`（泄漏阈值）。
- ⑧ `git revert <实施 commit>`；保留 `_session_dirty` 守卫。

### C10 · Hook 状态消息接入 UI（落地 TODO backend.py:277）
- ① `app/core/backend.py:275-278`
- ② 现状：`hook_status_changed` 信号已发但无 UI 订阅，状态消息（`statusMessage`）不可见 → 可观测性断点。
  ```python
  # TODO: 当前没有 UI 订阅此信号。状态消息字段 (statusMessage) 已可解析但尚未展示。
  hook_status_changed = pyqtSignal(str, str, bool)
  ```
- ③ 推荐：状态栏/通知组件订阅并渲染；无 UI 时不崩溃（已有 try/except 守卫）。
  ```python
  # 在 main_widget 初始化处连接（仅状态栏存在时）
  if hasattr(self, "_status_bar"):
      self.backend.hook_status_changed.connect(self._status_bar.show_hook_status)
  ```
- ④ 可观测性（非直接性能，但补齐诊断链路）　⑤ 2　⑥ 2
- ⑦ `tests/test_perf_regression.py` 不回归；手动验证状态栏显示。
- ⑧ `git revert <实施 commit>`。

### C11 · 共享 httpx.Client 连接池调优（config_sync.py:1106-1108）
- ① `app/core/config_sync.py:1108`
- ② 现状：三文件共享一个 `httpx.Client(timeout=30)` 但无连接池上限配置。
  ```python
  with httpx.Client(timeout=httpx.Timeout(30.0)) as client:
  ```
- ③ 推荐：提升为模块级复用 client + 显式连接池，避免每次 sync 重建握手。
  ```python
  _SYNC_CLIENT = httpx.Client(timeout=httpx.Timeout(30.0),
                              limits=httpx.Limits(max_connections=4, max_keepalive_connections=2))
  with _SYNC_CLIENT:   # 复用 keep-alive 池
  ```
- ④ 启动 / 网络（减少 TLS 握手）　⑤ 1　⑥ 1
- ⑦ `scripts/measure_perf.py --scenario upload`；`benchmarks/bench_importtime.py` 不回归。
- ⑧ `git revert <实施 commit>`。

### C12 · 远端 SHA 缓存 TTL / 负缓存（config_sync.py:1257-1261）
- ① `app/core/config_sync.py:1261`
- ② 现状：优先本地缓存远端 SHA 跳过 GET，但缓存无 TTL / 无负缓存。
  ```python
  existing_sha = self._get_cached_remote_sha(label) or self._get_remote_file_sha(remote_path, client)
  ```
- ③ 推荐：缓存带 TTL（如 60s）+ 404 负缓存，减少重复 GET 往返。
  ```python
  _sha = self._get_cached_remote_sha(label)
  if _sha is None or self._sha_cache_age(label) > 60:
      _sha = self._get_remote_file_sha(remote_path, client)
      self._cache_remote_sha(label, _sha)   # 含 404 负缓存
  ```
- ④ 网络 / CPU　⑤ 2　⑥ 2
- ⑦ `scripts/measure_perf.py --scenario upload`；`tests/perf/` 不回归。
- ⑧ `git revert <实施 commit>`。

### C13 · MCP 工具超时按工具分级（mcp_tools.py:1113）
- ① `app/tools/mcp_tools.py:1107`（默认 60s）
- ② 现状：所有 MCP 工具统一 60s 超时（已从 120s 降）。
  ```python
  def call_tool_sync(self, prefixed_name: str, arguments: dict, timeout: float = 60) -> ToolResult:
  ```
- ③ 推荐：读 registry 的工具级 `timeout_hint`，无则回退默认；避免快工具被长超时拖住。
  ```python
  hint = ToolRegistry.get_instance().get_timeout_hint(prefixed_name)
  timeout = hint or 60
  ```
- ④ CPU / 响应（长任务不阻塞、快任务早失败）　⑤ 2　⑥ 2
- ⑦ `scripts/measure_perf.py --scenario upload`；`tests/perf/` 不回归。
- ⑧ `git revert <实施 commit>`。

### C14 · 桌宠帧计时死代码清理 + FPS 上限（pixel_pet.py:884-894）
- ① `app/widgets/pixel_pet.py:886-887`
- ② 现状：`QElapsedTimer()` 创建并 `start()` 但从未读取（死代码）；帧推进完全靠 `step_interval`，无上限保护。
  ```python
  elapsed = QElapsedTimer()
  elapsed.start()
  def state_step():
      if sip.isdeleted(self): return
  ```
- ③ 推荐：删除死代码；引入全局 FPS 上限（如 30fps）避免高刷屏空转。
  ```python
  # 删除 elapsed = QElapsedTimer(); elapsed.start()
  step_interval = max(self._get_interval(state_name), 1000 // MAX_PET_FPS)
  ```
- ④ CPU（桌宠空闲能耗）　⑤ 1　⑥ 1
- ⑦ `scripts/measure_perf.py --scenario animation`；`tests/widgets/test_tab_drag_perf.py` 不回归。
- ⑧ `git revert <实施 commit>`。

### C15 · 托盘切换防抖阈值参数化（tray_manager.py:1110-1113）
- ① `app/tray_manager.py:1111`（`if now - self._last_toggle_time < 0.5: return`）
- ② 现状：0.5s 防抖硬编码。
  ```python
  now = time.perf_counter()
  if now - self._last_toggle_time < 0.5:
      return
  self._last_toggle_time = now
  ```
- ③ 推荐：提为常量（与其他定时器策略统一），可后续按交互档位调。
  ```python
  TRAY_TOGGLE_DEBOUNCE_S = 0.5
  if now - self._last_toggle_time < TRAY_TOGGLE_DEBOUNCE_S:
      return
  ```
- ④ CPU（避免重复触发做全窗口显隐）　⑤ 1　⑥ 1
- ⑦ `scripts/measure_perf.py --scenario animation`；`tests/perf/` 不回归。
- ⑧ `git revert <实施 commit>`。

---

## 批次划分建议

### 批次 A — 低风险、立竿见影、<2h（~5 条）
| 候选 | 文件:行号 | 一句话 |
|---|---|---|
| C03 | backend.py:658-680 | 错峰间隔抽常量 + 档位缩放 |
| C04 | main_widget.py:2324-2332 | 首帧后初始化窗口抽常量 + 档位缩放 |
| C05 | main_widget.py:12109 | 懒渲染时间片/批次间隔抽常量 |
| C14 | pixel_pet.py:886-887 | 删 QElapsedTimer 死代码 + 桌宠 FPS 上限 |
| C15 | tray_manager.py:1110-1113 | 托盘防抖阈值抽常量 |

### 批次 B — 中风险、需回归验证、~3-5h（~5 条）
| 候选 | 文件:行号 | 一句话 |
|---|---|---|
| C02 | main_widget.py:967-969 | 落地 TODO：机器性能档位 → 阈值系数 |
| C06 | main_widget.py:2173 | processEvents 5ms 限量抽常量 + 档位 |
| C07 | chat_worker.py:3423,3439 | 流式批量 emit 阈值抽常量 |
| C09 | main_widget.py:17115-17122 | flush 合并去重（复用 _session_dirty） |
| C11 | config_sync.py:1108 | 共享 httpx.Client 提模块级 + 连接池 |

### 批次 C — 高风险、需 reviewer / 大改（~5 条）
| 候选 | 文件:行号 | 一句话 |
|---|---|---|
| C01 | message_card.py / CodeWebViewer | 渲染链 Qt C++ 侧内存治理（Top 优先， reviewer 必审） |
| C08 | main_widget.py:9857 | 主题刷新脏标记位图 + 单卡刷新替代全树扫 |
| C10 | backend.py:275-278 | Hook 状态消息接入状态栏（可观测性断点） |
| C12 | config_sync.py:1261 | 远端 SHA 缓存 TTL + 负缓存 |
| C13 | mcp_tools.py:1107 | MCP 超时按工具分级 |

---

## 依赖图（实施先后顺序）
```
C02(机器档位) ──┬──> C03(错峰间隔) ──┐
                ├──> C04(首帧初始化) ─┤
                ├──> C05(时间片)      ├──> C06(processEvents)
                └──> C07(批量emit)    ├──> C07
                                       └──> C14/C15(独立常量化，无依赖)

C01(渲染链内存) 独立最高优先 ── 需在 #1 诊断后最先做；
   做完再做 C09(flush合并) 才有意义（先止内存涨，再降 IO 频次）。

C08(主题刷新) ── 依赖 ThemeRefreshCoordinator 现状理解，独立但需 reviewer。
C10(状态消息) ── 依赖状态栏组件存在，独立。
C11/C12/C13(config_sync/mcp) ── 网络侧，彼此独立，可在 B 批并行。
```
关键顺序：**先 C01（内存泄漏根因）→ 再 C09（IO 降频）；先 C02（档位基础设施）→ 再 C03~C07（所有阈值消费档位）。**

---

## 附：6 处 TODO 处置说明
- `main_widget.py:969` → **C02**（性能相关，纳入）
- `backend.py:277` → **C10**（可观测性，纳入）
- `main_widget.py:8545` / `history_card.py:1214` → `deprecated, remove with TestBuildTeamGroups`：死代码清理，**非性能**，建议单独低频 PR，不在本性能批次。
- `main_widget.py:17838` / `18028` → 功能占位（用户消息回调 / 智能体切换同步），**非性能**，排除。

## 附：验证基线锚点
- 启动：benchmarks 中位 6.865s / 607MB（baseline 见 `benchmarks/results/startup.json`）
- 内存：渲染链每轮 +556~576KB（目标：降到 ~20KB/轮）
- 回归护城河：`tests/perf/`（Top5 静态断言）+ `benchmarks/`（真实 GUI/导入/泄漏）+ `scripts/measure_perf.py`（CI 门禁化 diff）
- 基线对比文件：`reports/perf-baseline-before.json`（由 #2 perf-tester 产出，本骨架已支持 `--baseline` 对比标红）
