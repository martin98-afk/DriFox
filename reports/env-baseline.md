# 环境基线快照（子任务 #4.1）

> 采集时间：2026-08-22　分支：`dev`（工作树干净）
> 解释器：项目内置 `.venv/Scripts/python.exe`（Python 3.14.2）
> 包管理：uv（存在 `uv.lock`；基准脚本以 `uv run python ...` 运行）

## 1. 运行时版本

| 组件 | 版本 | 来源 |
|---|---|---|
| Python | **3.14.2** | `.venv` |
| PyQt5 | **5.15.11** | runtime dep（`pyproject.toml` 约束 `<5.15.16`） |
| PyQt5-Qt5 | 5.15.2 | win32 wheel（仅此版本提供 win_amd64） |
| PyQtWebEngine | **5.15.6** | runtime dep（`<5.15.7`） |
| PyQtWebEngine-Qt5 | 5.15.2 | win32 wheel |
| PyQt-Fluent-Widgets | 已安装（无显式版本号） | runtime dep |
| PyQt5-Frameless-Window | 0.8.1 | 传递依赖 |
| PyQt5_sip | 12.18.0 | 传递依赖 |

## 2. 测试框架

| 组件 | 版本 | 说明 |
|---|---|---|
| pytest | **9.1.1** | 主测试框架，`testpaths=["tests"]` |
| pytest-asyncio | 1.4.0 | `asyncio_mode="auto"` |
| pytest-qt | 4.5.0 | GUI 测试（`conftest.py` 自动建 `QApplication`） |

配置（`pyproject.toml` `[tool.pytest.ini_options]`）：
- `markers`: `perf`（性能基准）、`stress`（压力测试）
- 性能测试默认跳过，需 `PERF_TESTS=1` 才运行（`tests/test_perf_regression.py`）
- 泄漏阈值 env：`PERF_LEAK_THRESHOLD_MB`（缺省 10MB，待 #2 校准）

## 3. Lint / Format 工具

| 组件 | 版本 | 状态 |
|---|---|---|
| ruff | **0.15.20** | 实际激活（lint + format），`line-length=120`，`target-version=py314` |
| black | **26.5.1** | dev 组，历史格式化 |
| mypy | 2.1.0 | dev 组声明，已安装但当前未作为 CI 强制门禁 |
| flake8 | 未安装 | 未被采用 |

> ruff 当前 `select=["E","F"]` 且大量 `ignore`（E501/E402/.../F821 等），属过渡期配置。

## 4. Profiling / 可观测性工具

| 工具 | 类型 | 可用性 | 用途 |
|---|---|---|---|
| `tracemalloc` | 标准库 | ✅ 内置 | 内存分配追踪（Benchmarks 全面使用，`MEM_TRACE=1` 触发） |
| `time.perf_counter` | 标准库 | ✅ 内置 | 高精度耗时测量（产品代码 25+ 处、Benchmarks 使用） |
| `QElapsedTimer` | PyQt5 | ✅ 内置 | 帧/交互计时（`app/widgets/pixel_pet.py`） |
| `psutil` | runtime dep | ✅ 7.2.2 | RSS/进程内存采样（Benchmarks 使用） |
| `pympler` | dev 组 | ✅ 1.1 | 对象计数（Benchmarks 可选，未实际使用） |
| `py-spy` | 第三方 | ❌ **未安装** | 采样 profiler（需另行 `uv add`） |
| `memory_profiler` | 第三方 | ❌ **未安装** | 行级内存 profiler（未采用） |
| `pyinstrument` | 第三方 | ❌ **未安装** | 调用栈 profiler（未采用） |
| `timeit` | 标准库 | ✅ 内置 | 微基准（当前代码未直接调用） |

### 现有产品内置 profiler（值得复用）
- `app/utils/drag_stall_profiler.py`：拖拽卡顿采样器，`perf_counter` 心跳 + 后台采样线程，低开销。
- `app/core/workers/chat_worker.py`：tracemalloc 深度追踪（`MEM_TRACE=1` 时对单步大分配拍快照）。
- `main.py` 顶部：`tracemalloc` 深度追踪段（导入期分配追踪）。

## 5. 关键结论（对 build 的影响）
- **内存测量**：优先 `tracemalloc` + `psutil RSS`，无需新依赖。
- **CPU/耗时**：`time.perf_counter` / `QElapsedTimer`，无需新依赖。
- **采样 profiler**：当前缺 `py-spy`/`memory_profiler`，如需线程级火焰图须先 `uv add`（超出本任务“不引入新三方依赖”约束，留给后续决策）。
- 所有测量可用标准库 + 现有依赖完成，满足“不引入新三方依赖”要求。
