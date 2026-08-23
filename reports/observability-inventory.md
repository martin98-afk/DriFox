# 可观测性与性能代码盘点（子任务 #4.2）

> 采集时间：2026-08-22　分支：`dev`（工作树干净）
> 扫描范围：`app/`、`tests/`、`benchmarks/`、`scripts/`、`plugins/`（排除 `.venv`/`.git`/缓存）
> 说明：产品代码中大量出现的 `elapsed` 字段是**聊天卡片的业务数据**（流式耗时展示），非性能日志，已排除。

## A. 性能测试 / 基准 / Benchmark 相关文件

### `tests/perf/`（静态源码分析回归测试，不实例化 GUI）
| 文件 | 对应瓶颈 | 要点 |
|---|---|---|
| `test_message_card_paint_throttle.py` | Top① 消息卡片动画高频绘制 | 动画定时器 50ms；渐变/裁剪路径缓存；每帧仍建渐变 |
| `test_lazy_batch_webengineview.py` | Top② WebEngineView 一次性实例化 | 懒加载分批 + LRU 回收 |
| `test_share_card_upload_nonblocking.py` | Top③ 分享上传阻塞主线程 | 后台线程上传 + 按钮禁用 |
| `test_startup_init_lazy.py` | Top④ 启动同步初始化链 | `initialize`→同步直调插件/agent 系统 |
| `test_memory_timer_and_branch_cache.py` | Top⑤ `_branch_cache` 淘汰/上限 | 固化「存在 `pop` + `_MAX_BRANCH*` 上限」断言 |
| `README.md` | — | 运行方式 + Top① `lerp_color` 计数口径说明 |

### `benchmarks/`（真实 GUI/导入测量，`uv run python benchmarks/...`）
| 文件 | 测量项 |
|---|---|
| `bench_startup.py` | GUI 全链路启动耗时 + 稳态 RSS/tracemalloc（中位 6.865s / 607MB） |
| `bench_importtime.py` | `-X importtime` 导入耗时 Top20（openai 占 34%） |
| `bench_session_leak.py` | 会话「新建→关闭」N 轮内存斜率 |
| `bench_chat_pipeline.py` | 对话管线一轮 RSS 斜率（每轮 +556~576KB，Qt C++ 侧累积） |
| `bench_longrun.py` | 渲染累积线性度 + 会话切换泄漏 |
| `bench_common.py` | 公共工具（隔离/tracemalloc 采样/斜率/判定） |
| `bench_report.py` | 汇总 `results/*.json` → `summary.txt` |
| `results/*.json` | 7 份基线 JSON（startup/importtime/session_leak/chat_pipeline/longrun/t8_isolate） |

### `scripts/`（测量/分析脚本）
| 文件 | 用途 |
|---|---|
| `scripts/perf_regression.py` | 开 tab 耗时 / 内存曲线 / 用量请求回归（真实 GUI，需桌面会话） |
| `scripts/action_timer.py` | 动作计时工具 |
| `scripts/mem_track.py` | 内存追踪工具 |
| `scripts/tab_cycle_bench.py` | tab 切换基准 |
| `scripts/README.md` | 脚本说明 |

### 其他
- `tests/test_perf_regression.py`：泄漏回归测试（`PERF_TESTS=1` 门禁，`PERF_LEAK_THRESHOLD_MB` 缺省 10MB）
- `tests/test_message_card_compact_perf.py`：`time.perf_counter` 微基准
- `app/utils/drag_stall_profiler.py`：**产品内置**拖拽卡顿采样器（perf_counter 心跳）

## B. TODO / FIXME / HACK / XXX / OPTIMIZE 注释（file:line）

| 文件 | 行号 | 标记 | 内容 / 对优化的含义 |
|---|---|---|---|
| `app/main_widget.py` | 969 | TODO | 根据用户机器性能动态调整此值（动画/批量阈值硬编码，可做成自适应） |
| `app/main_widget.py` | 8545 | TODO (deprecated) | remove with TestBuildTeamGroups（死代码清理，非性能） |
| `app/main_widget.py` | 17838 | TODO | 实现用户消息添加时的回调处理（功能占位） |
| `app/main_widget.py` | 18028 | TODO | 实现智能体切换时的状态同步（功能占位） |
| `app/core/backend.py` | 277-278 | TODO | 状态消息字段无 UI 订阅，待接入（可观测性缺口） |
| `app/widgets/cards/settings/history_card.py` | 1214 | TODO (deprecated) | remove with TestBuildTeamGroups（死代码清理） |

> 非性能类（已排除）：`app/widgets/cards/floating/todo_floating_widget.py`（UI 组件 docstring）、`plugins/system/hooks/read_project_notes.py`（文档模板文案）。
> **关键观察**：产品代码中真正的“待优化”意图主要靠 **65 处 `# [PERF]` 内联注释**（见 C 节），而非 TODO/FIXME。这些 `[PERF]` 注释集中在 `app/main_widget.py`（~30 处）、`app/core/backend.py`、`app/core/config_sync.py`、`app/core/workers/chat_worker.py`，是优化点的高价值地图。

## C. 显式性能日志（logger/print 输出耗时）

| 文件 | 行号 | 语句 | 语义 |
|---|---|---|---|
| `app/core/backend.py` | 460 | `logger.info(f"[ChatBackend-Perf] SessionManager 创建完成 (...ms)")` | 后端会话管理器构造耗时 |
| `app/main_widget.py` | 10582 | `logger.info(f"[Perf-CreateSession] auto_save=..ms cache_cards=..ms backend_create=..ms ui_cleanup=..ms total=..ms")` | 新建会话各阶段分段耗时（**最完整的内置分段计时**） |
| `app/widgets/pixel_pet.py` | 862 | `logger.debug(f"[PixelPet] 空闲行为: {chosen} ({duration_ms}ms)")` | 桌宠空闲行为耗时 |
| `benchmarks/bench_importtime.py` | 77-81 | `print(...总耗时 / Top 累计 / Top 自身 ...)` | 导入耗时报告 |
| `tests/test_codegraph_tools.py` | 389-391 | `print(...耗时 + 阈值 ...)` | 工具耗时断言输出 |
| `tests/widgets/test_tab_drag_perf.py` | 74,129 | `print(...超过 33ms 次数 / 总耗时 ...)` | 拖拽帧耗时统计 |

> 覆盖度：产品代码仅 **3 处**显式耗时日志（backend / main_widget / pixel_pet），且 main_widget 的 `[Perf-CreateSession]` 是最有价值的端到端分段计时。后续优化建议：把 `[PERF]` 注释旁的关键路径统一接入分段计时日志（参考 10582 模式）。

## D. 计时 / Profiling 调用（timeit / QElapsedTimer / perf_counter / tracemalloc）

### `time.perf_counter`（25+ 处，高精度耗时）
| 文件 | 行号 | 用途 |
|---|---|---|
| `app/main_widget.py` | 10464,10536,10539,10559,10579 | `_create_session` 分段计时（配合 10582 日志） |
| `app/core/backend.py` | 448,460 | SessionManager 构造计时 |
| `app/core/builtin_commands.py` | 536-539,617-620,723-725 | 命令执行耗时 |
| `app/tray_manager.py` | 1111 | 托盘事件计时 |
| `app/utils/drag_stall_profiler.py` | 81,122,130,173,220 | 拖拽卡顿心跳/采样 |
| `app/utils/theme_refresh.py` | 168,183 | 主题刷新阶段计时 |
| `benchmarks/*` | 多处 | 启动/泄漏/管线计时 |
| `tests/test_message_card_compact_perf.py` | 64,66 | 卡片压缩微基准 |

### `QElapsedTimer`（PyQt5，帧/交互计时）
| 文件 | 行号 | 用途 |
|---|---|---|
| `app/widgets/pixel_pet.py` | 245,306,887 | 桌宠帧间隔 / 交互计时 / 空闲计时 |

### `tracemalloc`（标准库，内存分配追踪）
| 文件 | 行号 | 用途 |
|---|---|---|
| `main.py` | 27 | 导入期深度追踪段 |
| `app/core/workers/chat_worker.py` | 285,321,325-328,330,440 | 单步大分配快照（`MEM_TRACE=1`） |
| `benchmarks/bench_chat_pipeline.py` | 28,80-160 | 每轮 tracemalloc 斜率 + Top 分配点 |
| `benchmarks/bench_common.py` | 22,90-111,159-162 | 公共采样/Top-N/差值工具 |
| `benchmarks/bench_startup.py` | 46 | 导入前开启追踪 |
| `scripts/perf_regression.py` | 顶部 | `MEM_TRACE` 清理（实际未启用） |

### `timeit`
- **产品代码与基准脚本均未直接使用** `timeit`（改用 `perf_counter`）。标准库可用，但当前无调用点。

## E. 对 build 的结论
1. **计时基础设施齐备**：`perf_counter` 已遍布关键路径，新增分段计时直接复用即可。
2. **内存基础设施齐备**：`tracemalloc` + `psutil` 可直接用于优化前后对照，无需新依赖。
3. **可观测性缺口**：仅 3 处显式耗时日志；`backend.py:277` 的状态消息无 UI 订阅（TODO），是诊断链路断点。
4. **优化点地图**：以 **65 处 `[PERF]` 注释** + `tests/perf/`（Top5 回归保护）为主，待 #1/#2/#3 诊断报告细化后，build 可直接按注释定位改动点。
