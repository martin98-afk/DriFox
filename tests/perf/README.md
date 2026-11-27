# tests/perf — 性能瓶颈回归测试

本目录对 **#1 性能瓶颈报告 Top5** 提供最小复现 / 现象确认 + 修复前后量化回归基线。

> 所有测试均为 **静态源码分析**：用 `pathlib` 读取 `app/` 下源码文本 + `re` 匹配。
> **未修改任何业务代码**，不 `import PyQt5`、不实例化任何 GUI 对象。
> `tests/conftest.py` 会自动建 `QApplication`，但本目录测试逻辑不依赖它。

## 文件清单

| 文件 | 对应瓶颈 | 测试要点 |
|---|---|---|
| `test_message_card_paint_throttle.py` | Top① 消息卡片动画高频绘制 | 动画定时器 50ms；paintEvent 已缓存渐变 `self._grad_*` / 裁剪路径 `self._clip_*`；每帧颜色分配已收敛（断言 `build_gradient` / `lerp_color` 彩虹逐 stop 插值不再回归） |
| `test_lazy_batch_webengineview.py` | Top② WebEngineView 一次性实例化 | 懒加载分批 `_process_next_lazy_batch` + `ensure_rendered()` + `singleShot(80,...)`；Chromium 实例上限 `_max_rendered_cards` + LRU 回收 `_recycle_lru_batches` |
| `test_share_card_upload_nonblocking.py` | Top③ 分享上传阻塞主线程 | 上传入口 `_on_upload` + `uploader.upload_file(`；后台线程 `_ShareUploadThread(QThread)` + 按钮禁用 / "上传中"；底层 `requests.post(..., timeout=30)` |
| `test_startup_init_lazy.py` | Top④ 启动同步初始化链 | `self.backend.initialize(` -> `self._init_plugin_system()` 同步直调（前 6 行无 `singleShot`）-> `self._agent_manager.reload_agents()` 同步触发 |
| `test_memory_timer_and_branch_cache.py` | Top⑤ `_branch_cache` 淘汰/上限保护 | 断言存在 `_branch_cache.pop` 淘汰 + `_MAX_BRANCH*` 上限常量。说明：原 `_memory_timer`（标题栏 RSS 内存标签）已按需求下线，相关用例移除。 |

## 运行方式

```bash
# 全部性能测试
cd D:/work/DriFox && python -m pytest tests/perf/ -v

# 单文件
cd D:/work/DriFox && python -m pytest tests/perf/test_message_card_paint_throttle.py -v
```

## 环境要求

- pytest >= 7
- Python 3
- 对 `app/` 源码有读权限
- 无需显示器（测试不实例化 GUI）
- 无新三方依赖（仅标准库 `pathlib` / `re`）
- 跨平台，Windows 优先

## 说明

- 测试未修改任何业务代码，仅静态分析源码文本。
- **Top⑤ 修复后回归保护**：`test_memory_timer_and_branch_cache.py` 已固化为「
  存在 `_branch_cache.pop` + `_MAX_BRANCH*` 上限常量」断言（防止后续重构误删性能修复）。
  注：原 `_memory_timer`（标题栏 RSS 内存标签）已按需求下线，相关用例同步移除。
- **Top① `lerp_color` 计数偏差说明**：#1 报告估算「3 渐变 x ~9 lerp_color 约 27 QColor/帧」。
  实际源码中 `lerp_color` 定义为 helper 并在 `build_gradient` 的 `stops` 循环内调用 1 次
  （文本仅出现 2 处），运行时循环展开为每帧 ~27 次分配。因此测试断言改为
  `lerp_color(` 文本出现 **>=2**（证明 helper 定义并被调用），而非报告中的 >=20（文本计数口径不符）。

---

# 性能预算门（护栏）

上面那批是**瓶颈复现/静态分析**，它们证明"修复还在"，但**不能阻止性能变慢**：
判据是源码文本或采集链路是否存活，改慢不改结构则全绿。

本节的两个文件是**真预算门**：测真实墙钟与真实内存，超限即红。

## test_startup_budget.py — 启动预算门

口径：父进程墙钟 `Popen` → stderr `[StartupMark] first_chat_window`
（含解释器冷启动与全部顶层 import，真端到端）。预热 1 轮丢弃，正式 3 轮取中位。

```bash
.venv/bin/python -m pytest tests/perf/test_startup_budget.py -q -s
```

阈值 `1500ms`（当前实测中位 ~830ms）。覆盖变量：
`DRIFOX_STARTUP_BUDGET_MS`、`DRIFOX_STARTUP_ROUNDS`。

**口径边界（必读）**：本门是**热启动**口径——预热 1 轮丢弃后才计时，pyc 已就绪。
**冷启动（每轮清项目 `__pycache__`）实测 1705ms（project-only 口径），不在本门范围内**，
用 1500ms 去卡冷启动必然误红。冷启动数据见 `tools/perf/MEASUREMENT_PROTOCOL.md` §5。

另含 `test_startup_probe_emits_marks`：探针链路自检。若有人删掉 `main.py` 的
`_smark("first_chat_window")` 或改动 `_startup_probe`，这条会红 —— 防止
「探针没了 → 预算门永远测不到 → 静默变成无效护栏」。

## test_memory_budget.py — 内存预算门

口径：`proc_pid_rusage` 的 `ri_phys_footprint`（offset 72），**不是 RSS**。
macOS RSS 含共享页会虚高，与系统 `footprint -p <pid>` 工具口径不一致；
本文件的读法已与该工具交叉验证同值。

两个视角分别断言（以下为**同一次运行的一对读数**，不可拼不同轮次）：
- **主进程** < 800MB（样例 `results/benchmark_core_20261127_122717`：348.8MB）
- **全树**（主进程 + 递归全部子进程，含 QtWebEngineProcess）< 1200MB（同次运行：404.1MB，其中 renderer 55.4MB）

多轮成对采样（11 轮中排除 1 轮短跑异常）的中位：主进程 **355.1MB** / 全树 **410.3MB** /
差额 **55.4MB**（范围 55.2~74.4MB，取决于同时有几个 renderer）。

```bash
.venv/bin/python -m pytest tests/perf/test_memory_budget.py -q -s
```

覆盖变量：`DRIFOX_MEM_MAIN_BUDGET_MB`、`DRIFOX_MEM_TREE_BUDGET_MB`、
`DRIFOX_MEM_STEADY_WAIT_S`。

**为什么必须分「主进程 / 全树」两个数**：renderer 子进程不进主进程 footprint，
只看主进程会漏掉 **55.4MB（空载单 renderer）到 74.4MB（3 个 renderer）**，多卡场景可达 1GB+。

## 环境门：skip 不是 bug

测量型测试在脏环境下没有意义。两个文件都会前置检查：

| 检查 | 默认行为 | 严格模式 |
|---|---|---|
| `load1 > ncpu * 0.8` | **skip**（测量值不可信） | skip |
| 有其它 `main.py` 实例在跑 | 打印 `[warn]` 后继续 | **skip** |

`DRIFOX_PERF_STRICT_ENV=1` 切严格模式，适合采集基线/CI。

默认宽松的理由：开发机上残留实例（尤其 PPID=1 的孤儿）很常见，
一律 skip 会让护栏长期静默 —— 静默的护栏等于没有护栏。
负载门仍会拦住真正不可信的环境。

## 默认收集范围

`pyproject.toml` 的 `addopts` 不再整体 `--ignore=tests/perf`，改为逐个排除
已知不可用的文件（合跑 segfault / 单跑分钟级 / 纯基线无阈值）。
所以 `pytest` 默认会跑这两个预算门。

需要人工跑被排除的那些时，显式传路径：

```bash
.venv/bin/python -m pytest tests/perf/test_new_tab_baseline.py -q -s   # 25 轮真窗口
.venv/bin/python -m pytest tests/perf/long_run -q -s                   # ≥30s/场景
```

## 改阈值的正确姿势

**不要为了让测试变绿而放宽阈值。** 顺序必须是：

1. 先确认变慢是真的（复测 3 次，排除环境噪声）
2. 用 `-s` 看实测数字，与历史值比较，判断是回归还是可接受的变化
3. 若是**有意**的性能取舍（例如为了正确性牺牲 50ms），在提交信息里写明理由
4. 再调阈值，并把新基线数字写进本 README

阈值宁松不紧。频繁误红 → 没人看 → 等于没有护栏。
先立起来，稳定运行一段时间后再逐步收紧。

## 坑：`DRIFOX_STARTUP_PROBE_EXIT_MS` 不完全生效

写性能测试起异步子进程时**不要依赖 `DRIFOX_STARTUP_PROBE_EXIT_MS` 收尾**。

实测：该环境变量的定时器挂在 `_probe_dump()` 之后（见
`app/utils/_startup_probe.py` 的 `_maybe_schedule_exit`），部分路径上不会启动。
以 `EXIT_MS=1500` 启动的子进程曾跑满 6 分钟仍未退出，
`sample <pid>` 显示停在 Qt 事件循环（`pymain_run_file` → 主线程）。

后果不只是测试挂住：残留实例会进入下一次测量的环境门，而且 PPID=1 的孤儿
清理时容易被安全策略拒绝，最终变成长期噪声。

正确做法（两个预算门都是这么写的）：

```python
proc = subprocess.Popen(..., start_new_session=True)   # 独立进程组
try:
    ...  # 读 stderr 到目标打点即 break，不要等 EOF
finally:
    # SIGTERM → 等 15s → SIGKILL，失败静默
    _stop_child(proc)
```

要点：
- **读到目标打点就 break**，不要 `for line in proc.stderr` 等 EOF（进程可能不退）
- `start_new_session=True` + 主动信号，别指望被测程序自杀
- 收尾逻辑必须幂等且失败静默，不能让清理异常盖住测量结果

## 已知存量失败（不属于预算门，看到红不必自查）

| 用例 | 状态 | 原因 |
|---|---|---|
| `test_message_card_streaming_resize_throttle.py::test_perf_preview_exit_applies_pending_height` | 已知失败 | 静态源码断言写的是 `self._commit_viewer_height(pending_h)`，当前实现是 `self.viewer.setFixedHeight(pending_h)`（`app/widgets/message_card.py` 退出占位分支，注释明确写「绕开 HeightCommitBatch」）。属断言与实现漂移，需要产品语义判断「哪个才对」，不在护栏范围。 |

在默认收集下不带 `--ignore` 跑 `tests/perf` 会看到这一条红，
基线是 `1 failed, 41 passed`。这不是你改坏的。
