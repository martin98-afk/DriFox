# baseline.py — DriFox 性能基线测试脚本

无侵入式采集 PyQt5 GUI 的四大性能维度：**启动耗时 / 内存占用 / 帧率 / 长时运行内存增长**。
所有插桩通过运行时 monkey-patch（`QWidget.showEvent` 等）完成，**不修改任何业务代码**。
仅依赖标准库 + `psutil`（已是项目运行时依赖），**不引入任何新三方依赖**。

---

## 1. 快速开始

```bash
# 进入项目根目录（脚本靠自身位置推导项目根，任意 cwd 均可）
cd D:/work/DriFox

# 用项目虚拟环境运行
.venv/Scripts/python.exe tools/perf/baseline.py all --synthetic \
    --out tools/perf/results/baseline_<日期>.json
```

> 默认 Qt 平台为 `offscreen`（无显示器即可运行）。真实带显示环境可用
> `--platform windows`（Windows 桌面）或 Linux 下 `xvfb-run -a python tools/perf/baseline.py ...`。

---

## 2. 子命令

| 命令 | 作用 | 说明 |
|------|------|------|
| `all` | 跑全部维度并汇总 | 启动(×repeats) + 内存 + 帧率 + 长时，输出一份 JSON 报告 |
| `startup` | 仅冷启动耗时 | 多次独立子进程采样，得到 import→首屏可见的方差 |
| `mem` | 仅内存基线 | RSS 周期采样 + tracemalloc Top 分配者 + 对象数 |
| `fps` | 仅帧率 | `QTimer(16ms)` 实际间隔分布 → 均值帧率 |
| `longevity` | 仅长时内存增长 | 模拟定时操作 N 次，记录 RSS/对象数增长曲线 |
| `_once` | 内部单次采样 | 供 `subprocess` 拉起，输出 JSON 到 stdout（一般不直接用） |

---

## 3. 常用参数

| 参数 | 默认 | 说明 |
|------|------|------|
| `--entry` | `main` | 入口模块。`main` = 仓库根 `main.py`（即真实 GUI 入口）。`app.main` 不存在。 |
| `--platform` | `offscreen` | Qt 平台：`offscreen` / `windows` / `xcb` 等 |
| `--duration-ms` | `3000` | 首屏可见后保持运行的时长（内存/帧率采样窗口） |
| `--synthetic` | 关 | 用合成轻量窗口（无 WebEngine）替代真实 app，便于无显示/CI 环境复现 |
| `--no-tracemalloc` | 关 | 关闭 tracemalloc 快照（某些大堆进程快照可能很慢/崩溃时可用） |
| `--retries` | `3` | 子进程原生崩溃时的重试次数（真实 app 在 offscreen 下偶发崩溃） |
| `--repeats` | `3` | `startup` 的独立采样次数 |
| `--frame-ms` | `16` | 帧率采样器目标间隔 |
| `--mem-interval-ms` | `100` | 内存周期采样间隔 |
| `--ops` | `1000` | 长时模式模拟操作次数（24h 长跑可设极大值或外层守护） |
| `--op-interval-ms` | `20` | 长时模式每次操作间隔 |
| `--sample-every` | `50` | 长时模式每 N 次操作记录一次样本 |
| `--out` | 空 | `all` 命令的结果 JSON 输出路径 |

示例：
```bash
# 真实 GUI 启动耗时（需带显示环境）
.venv/Scripts/python.exe tools/perf/baseline.py startup --repeats 5 --platform windows

# 长时运行内存增长（1000 次模拟操作）
.venv/Scripts/python.exe tools/perf/baseline.py longevity --ops 1000 --op-interval-ms 20

# 无显示环境快速全量基线
.venv/Scripts/python.exe tools/perf/baseline.py all --synthetic --out tools/perf/results/baseline_now.json
```

---

## 4. 输出格式

`all` 命令输出 JSON（同时写 `--out` 指定文件），结构：

```jsonc
{
  "meta": {                          // 运行环境：python/qt 版本、platform、entry、machine、时间
    "tool": "DriFox baseline.py",
    "python": "3.14.2",
    "qt_version": "5.15.2",
    "platform": "offscreen",
    "entry": "main",
    "machine": "win32"
  },
  "dimensions": {
    "startup": {                     // 冷启动：多次样本 + 统计 + 热启动近似
      "repeats": 2,
      "samples": [ { "import_s":.., "to_visible_s":.., "app_init_s":.., "visible_cls":.. } ],
      "cold":   { "import_s":{min,max,mean,p95,stdev}, "to_visible_s":{...}, "app_init_s":{...} },
      "hot_approx_min": { "import_s":.., "to_visible_s":.. }   // 取多次最小近似热启动
    },
    "memory": {                      // 内存基线
      "rss_visible_mb": 37.2, "obj_visible": 29355,
      "rss_peak_mb": 41.6, "rss_min_mb": 41.4,
      "mem_series": [ [elapsed_s, rss_bytes, obj_count], ... ],
      "trace_top": [ {size_kb, size_diff_kb, count, file}, ... ]  // tracemalloc Top 分配者
    },
    "fps": {                         // 帧率
      "frame_samples": 185, "frame_mean_ms": 16.03, "frame_p50_ms": 16.03,
      "frame_p95_ms": 16.88, "frame_p99_ms": 18.26, "frame_min_ms": 12.7, "frame_max_ms": 22.7,
      "fps_mean": 62.4
    },
    "longevity": {                   // 长时运行内存增长
      "ops_total": 300, "rss_start_mb": 42.1, "rss_end_mb": 43.0, "rss_delta_mb": 0.02,
      "obj_start": 29543, "obj_end": 28613, "obj_delta": -930, "leak_suspected": false,
      "longevity_series": [ [op_index, rss_bytes, obj_count], ... ]
    }
  },
  "errors": { }                      // 某维度子进程全重试失败时的错误信息
}
```

各维度单独运行（`startup`/`mem`/`fps`/`longevity`）输出对应 `{"meta":..,"dimensions":{<维度>:..}}`。

---

## 5. 关键说明与已知限制

### 5.1 真实 GUI 入口
- 真实 GUI 入口是仓库根 `main.py`（即模块 `main`），**`app.main` 不存在**。
- 脚本默认 `--entry main`，会 `import main` 后调用 `main.main()`。

### 5.2 offscreen 与 WebEngine（重要）
- 本环境以 `offscreen` 平台运行 GUI，可无显示器复现。**但真实 app 含 `QWebEngineView`（Chromium）**，
  在 offscreen 下初始化不稳定，**可能偶发原生崩溃（`0xC0000005` 访问冲突）**，且崩溃无法被 Python 捕获。
- 因此：**真实 `main` 入口的基线测量需要在带显示的环境进行**（Windows 桌面会话，或 Linux
  `xvfb-run -a python tools/perf/baseline.py ...` 配软件 GL）。
- 无显示/CI 环境请用 `--synthetic`：脚本自建轻量 `QMainWindow`（无 WebEngine），
  完整跑通「启动/内存/帧率/长时」采集逻辑，作为可复现的基线验证与回归锚点。
- 脚本对子进程原生崩溃内置 `--retries` 重试；若真实 app 在 offscreen 下持续崩溃，
  `all` 会在 `errors` 字段记录该维度失败，不影响其它维度。

### 5.3 帧率含义
- offscreen 下帧率采样（`QTimer(16ms)`）反映**事件循环定时器精度**，非真实 GPU 渲染帧率。
- 真实渲染帧率请在带显示平台运行，此时帧间隔更贴近真实 paint 节奏。

### 5.4 tracemalloc
- 内存基线含 tracemalloc 快照（首屏可见 + 结束，取 Top 分配者）。
- 超大堆进程（如完整 DriFox）在 offscreen 下若快照导致崩溃，可用 `--no-tracemalloc`。

### 5.5 启动耗时方差来源
- 真实启动含应用自身网络 IO（版本检查、插件加载、models.dev 同步等），会引入耗时方差；
  对比基线时建议固定网络条件，或记录网络波动为已知方差源。

---

## 6. 与方案文档对应
- 测试维度、工具选型、对比表格模板见 `docs/perf/baseline_plan.md`。
- 复现命令：`python tools/perf/baseline.py all --out tools/perf/results/baseline_<ts>.json`
