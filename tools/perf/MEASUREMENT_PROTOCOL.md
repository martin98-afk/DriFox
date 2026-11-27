# DriFox 测量规程（MEASUREMENT PROTOCOL）

> 目的：让「优化前后差多少」这件事可信。CPU 敏感指标的噪声可以**大于被测目标本身**
> （实测 load 8.48 时，一个 41.9ms 的优化目标噪声超过 100ms）。本规程优先于任何
> 「看起来更快」的结论。

---

## 索引

1 何时能测 · 2 环境字段 · 3 统计量 · 4 何时写「测不出来」 · 5 已知的坑（含崩溃假阴性）
6 标准流程（复制即用） · 7 崩溃类固定动作 · 8 Qt 销毁期约束 · 9 统计口径与工具误用

---

## 1. 什么时候能测

先跑门禁，用退出码决定：

```bash
.venv/bin/python tools/perf/measure_gate.py check
.venv/bin/python tools/perf/measure_gate.py check --wait 300   # 轮询到 GO，最多 5 分钟
.venv/bin/python tools/perf/measure_gate.py check --report env.json
```

| 退出码 | 判定 | 依据 | 该怎么做 |
|---|---|---|---|
| 0 | GO | load ≤ 核数×0.8、main.py 实例 ≤1、idle ≥40% | 正常测 |
| 1 | CAUTION | load > 核数×0.8 / 实例 == 2 / idle < 40% | 可测，但重复 ≥5 次取中位，报告标注环境 |
| 2 | STOP | load > 核数×1.5 / main.py 实例 > 2 | **不要测**，等或清理后重跑 |

阈值依据：
- **load > 核数 × 1.5**：CPU 密集测量的排队延迟在 load 超过核数后非线性上升；
  实测 15 核机器 load 8.48（≈0.56×核数）时启动耗时抖动已 >20%，1.5 倍以上无意义。
- **main.py 实例 > 2**：多实例争 WindowServer / WebEngine profile / SQLite 锁，
  启动与渲染数据互相污染（实测 6 实例时 load 从 4.6 涨到 8.48）。
- **idle < 40%**：CPU 已被别人占掉一半以上，任何 wall-clock 都在排队。

门禁**只判定、不 kill**。疑似测试实例会列出来（pid / cpu% / 存活时长 / 命令行），
由人决定是否清理；用户自己打开的 `Drifox.app` 永远不动。

---

## 2. 每次报告必须记录的环境信息

| 字段 | 从哪来 | 为什么必须 |
|---|---|---|
| `load_avg`（1/5/15 分钟） | `measure_gate` / `os.getloadavg()` | 判断数字是否可比 |
| `main_py_instances` | `measure_gate` | 并发实例是最强污染源 |
| `cpu_idle_pct` | `measure_gate` | 与 load 交叉验证 |
| `git_commit` + `git_dirty` | `git rev-parse --short HEAD` + `git status --porcelain` | 跨轮对比的唯一身份 |
| pyc 状态（冷/热） | 冷 = 清项目 `__pycache__`（`--cold-mode project`，默认） | 冷热差约 2.2×（1705ms vs 770ms）；旧口径 `all` 连第三方 pyc 一起清，多算 652ms |
| 数据目录 | 临时目录路径 | 空库与有历史数据的启动不可比 |
| **`__pycache__` 是否已清（每轮）** | 每轮跑前 `find . -name __pycache__ -not -path "./.venv/*" -exec rm -rf {} +` | **崩溃类验证必填**。残留 pyc 会让「必崩」的代码连跑 8 次全不崩（见 §5 第一条），是假阴性最强来源 |

`tools/perf/benchmark_core.py` 已自动写入 `env_gate`、`git_commit`、`git_dirty`。

---

## 3. 报告必须包含的统计量

- **样本数 n**（≥3；CAUTION 环境下 ≥5）
- **p50（中位数）**：主指标，单次数字一律不作结论
- **极差或 max**：暴露抖动
- **测量口径**：起点/终点定义（例：`Popen → [StartupMark] first_chat_window`）
- **不可加性说明**：不同口径的数字不能相加（见 §5 采样归因）
- **信噪比（SNR）**：`< 2` 一律不可定、不写进交付，定义与算法见 **§4**

禁止只报「平均值」：分布长尾（GC、首次渲染、磁盘）会被均值抹平。

---

## 4. 什么情况下必须写「测不出来」而不是给数字

1. 门禁为 STOP，且等不到 GO → 报告写「环境不可测（load X / 实例 Y），未取数」。
2. 同一版本连续 3 次极差 > 30% → 写「噪声大于信号，测不出来」，并给出 3 次原值。
   > 这与下方第 5 条是**同一判据的两种写法**：极差 > 30% ≈ 目标 < 2×噪声。
3. 首帧 / JS 就绪这类异步指标拿不到回调（例如无显示环境、WebEngine 未就绪）
   → 写 `ok=false` + `reason`，**不许用估算补齐**。
4. 优化目标 < 环境噪声（如目标 20ms、噪声 50ms）→ 只能给方向性判断，不能给百分比。
5. **信噪比 < 2 → 一律不可定**，不写进交付、不写改善百分比。定义：

   ```
   信噪比 = |A 组中心 − B 组中心| / 噪声
   A 组中心 = 该组「各轮中位数的算术平均」    ← 抗单点尖刺，不用全样本均值
   噪声     = max(A 组极差, B 组极差)         ← 必须用「组内极差」
   极差取值 = 该组「全部轮次 × 每轮 repeats」的样本 max − min
   ```

   **分子用「中位数的均值」而不是「全样本均值」**：单轮出现一次尖刺会把全样本均值
   拽偏，中位数天然抗尖刺；「先每轮取中位、再跨轮取均值」与 §3「p50 为主指标、
   单次数字不作结论」同源。两者实测差异不大（热启动 `1.58` vs `1.54`，结论不变），
   但落在 `1.95~2.05` 区间时会给相反结论，故必须事先固定。

   **分母必须用组内极差，不能用四值极差（全体样本 max−min）**：后者把组间差本身
   也算进了噪声，会**系统性低估** SNR。实例：首帧 `Δ=87.4ms`，
   用四值极差 `104.1` 算出 SNR `0.84`（判不可定），用组内极差 `21.7` 算出 `4.03`
   （判可下结论）——**同一组数据、两种算法结论相反**。

   与第 2 条的关系：`极差 > 30%` ≈ `目标 < 2×噪声` ≈ `SNR < 2`，三者是同一判据的不同写法。
   本算法的完整实现与十项指标实例见 `PERF_FALSIFIED.md` §8.3。

---

## 5. 已知的坑（都踩过并量化过）

> ⚠️ **下表第一条是所有「崩溃 / 段错误 / 间歇性失败」类验证的前置条件。**
> 不清 `__pycache__` 就下「N/N 不崩」的结论，得到的往往不是代码性质，而是缓存状态。

| 坑 | 实测表现 | 规避 |
|---|---|---|
| **`__pycache__` 造成崩溃假阴性**（最高优先，前置条件） | 同一份**有崩溃缺陷**的代码：首次运行 `EXIT=139`（段错误）；**后续连跑 8 次 `8/8 EXIT=1` 不崩**；每轮清 `__pycache__` + 数据目录后再跑 → **`5/5 EXIT=139`** 稳定复现。本轮因此产生了多起互相矛盾的结论（两个「几乎什么都不做」的槽函数，一个崩一个不崩），多人一度以为是自己搞错了 | 崩溃类验证**每轮**都清缓存，见下方「崩溃类验证的固定动作」。判据：只要出现「首次崩、复跑不崩」或「同代码不同人结论相反」，第一件事是清缓存复测，**不要先怀疑自己的实验设计** |
| **pyc 冷热口径** | 只清项目 pyc：冷 1705ms / 热 770~827ms（差 ~0.9s，其中项目 pyc 重编 ~509ms、全新数据目录 ~71ms）；旧口径 `all`（`PYTHONPYCACHEPREFIX` 清空）2357ms 是**合成值** | 冷启动用 `--cold-mode project`（默认），只删项目 `__pycache__`，保留 `.venv` 下第三方 pyc；`all` 仅用于口径对比。打包版（onedir+PYZ）不写 pyc，不受影响 |
| **抓栈采样放大等待** | 8ms 心跳把 `_connect_viewer_signals` 报成 197.9ms，wall-clock 实测 0.1ms（差 3 个数量级） | 函数级热点一律用 wall-clock 包一层复核；采样只用来找方向 |
| **多 viewer SIGSEGV** | 同进程创建多个 WebEngine viewer 后偶发崩溃（macOS 离屏/绘制路径） | 渲染测量独立子进程串行跑，失败重试；崩溃样本不进统计 |
| **首次渲染污染** | 首张卡 506ms vs 第 2 张 210ms（类/字体/骨架首建） | 预热 10 帧/1 张卡后丢弃，或明确单列「首建」指标 |
| **`deleteLater` 后不泵 `DeferredDelete` → 误判为 renderer 不回收** | 20 张卡 → 12 个 renderer，`deleteLater()` 后计数不回落，据此判「泄漏 1.1GB」；显式 `sendPostedEvents(None, QEvent.DeferredDelete)` 后立刻回落 | `processEvents()` **不消费** `DeferredDelete`。判 renderer 未回收前先显式泵一次；**泵完仍不降才是真泄漏**。内存测量仍须分 `main` / `renderer` / `tree` 三列 |
| **os._exit 型退出** | 探针强制退出会跳过 atexit | 采样数据边采边落盘（open/close 直写），不依赖退出钩子 |
| **内存 RSS 虚高** | macOS RSS 包含未触及页 | 只认 `proc_pid_rusage.phys_footprint`（缓冲 1KB + 显式 argtypes） |

---

## 6. 标准流程（复制即用）

```bash
# 1) 门禁
.venv/bin/python tools/perf/measure_gate.py check --wait 300 || echo "环境不适合测量，停"

# 2) 冷热启动 + 三列内存 + import + 首帧 + 渲染（一把跑，约 2 分钟）
.venv/bin/python tools/perf/benchmark_core.py all --repeats 3

# 3) 只跑渲染（约 11 秒）
.venv/bin/python tools/perf/benchmark_core.py render

# 4) 结果
#    tools/perf/results/benchmark_core_<ts>/report.json
#    必看：env_gate / git_commit / git_dirty / startup.cold_startup_ms / startup.warm_startup_ms
#          memory.main_phys_footprint_mb / renderer_total_mb / tree_total_mb
```

跨轮对比时，`env_gate.level` 不同的两组数字**不可直接比较**；level 相同但 `load_avg`
相差 > 20% 时也只作方向参考。

---

## 7. 崩溃类验证的固定动作（段错误 / SIGSEGV / 间歇性失败）

性能数字可以容忍一次性的缓存状态差异，**崩溃结论不行**：它只有「崩 / 不崩」两个值，
任何假阴性都会直接翻转结论。故本节动作是强制的，不是建议。

### 每一轮都必须做

```bash
# 1) 清项目 pyc（保留 .venv 下第三方 pyc）
#    ⚠ 必须在仓库根目录执行：命令起点是 "."，在 app/ 或 tools/ 下执行只会清
#    该子树，静默漏清其余部分 → 崩溃类验证会拿到假阴性（正是本节要防的事）。
find . -name "__pycache__" \
     -not -path "./.venv/*" -not -path "./.worktrees/*" \
     -exec rm -rf {} +

# 2) 每轮用独立的数据目录，且先删后跑（不复用上一轮状态）
rm -rf /tmp/crashcheck_$i
DRIFOX_DATA_DIR=/tmp/crashcheck_$i .venv/bin/python -m pytest <target> -q
echo "EXIT=$?"
```

**四条判据：**

1. **只有 `EXIT=139`（或 `-11`）才算崩溃。** `EXIT=1` 是测试失败（`failed`），
   不是段错误。二者混为一谈会把「断言挂了」当成「进程崩了」。
2. **「首次崩、复跑不崩」= 缓存污染，不是代码正常。** 必须清缓存重测，
   直到连续 N 次结果一致才下结论。
3. **N ≥ 3 且每轮清缓存。** 不清缓存的连跑只能证明「第一个进程跑了什么」，
   不能证明代码性质。
4. **崩溃在测试序列中间时，单跑该测试往往不崩。** 这是前序测试累积状态
   （对象未正确销毁）的信号，本身是有价值线索，不是「无法复现」。
   记录累积条件：哪个测试开始崩、前序测试创建了什么。

### 相关环境字段

报告里除 §2 的通用字段外，崩溃类验证**还需**写明下列两项（§2 已要求的
`__pycache__` 状态与数据目录在崩溃场景下属**必填**，不得省略）：
- `__pycache__` 是否每轮已清（是/否）
- 数据目录是否每轮独立（是/否）
- 每轮的 `EXIT` 原值序列（例：`139, 139, 139`）
- 崩溃时 stderr 尾部（含 exit code 与栈顶符号）

---

## 8. Qt 对象销毁期间的回调约束

**背景（实测 2026-11-27，`app/widgets/scene_layer.py`）**：给 `QWidget` 的 `destroyed`
信号挂一个槽，槽里读 `self._anim_source` 并归还引用计数，累积 15 个 UI 用例后必崩
（`EXIT=139`）；单跑该用例不崩。崩溃栈顶：

```
_PyFunction_Vectorcall
PyQtSlot::call → PyQtSlot::invoke → PyQtSlotProxy::unislot
QMetaObject::activate
QObject::destroyed            ← 此时对象已在析构中
QWidget::~QWidget → QObjectPrivate::deleteChildren → sipQWidgetD0Ev
```

### 可复核标识（为什么能确认「确实存在两个版本」）

两个快照（工作树完整副本）**除 `scene_layer.py` 外逐字节一致**（`diff -rq` 仅此一处
差异，其余仅 `.drifox-test/` 测试缓存）。即崩溃与非崩溃的唯一变量是下面这一行：

> 注：下表两个 md5 对应 2026-11-27 取证时的两份工作树快照，**快照已删除**，
> **不可用于核对当前文件**。当前 `scene_layer.py`（`955648b3`，839 行）已回退钩子、
> 仅保留历史注释，不属于上表任一行。
>
> 三个 md5 的来历（同一文件的三次状态，勿混用）：
> `ac97b7b6` = 崩溃版（含 `_release_anim_source` 钩子）→ 已回退
> `6f5832f0` = 回退后 +7 行注释（中间态）
> `955648b3` = 再改注释后 +18 行（当前，839 行）

| 版本 | `scene_layer.py` md5 | 行数 | 结果 |
|---|---|---|---|
| 崩溃版 | `ac97b7b6d64289903d83e33ccd764849` | 845 | `EXIT=139`，3/3 |
| 不崩版 | `3d203883191afe5331926e6d6a6e3079` | 846 | `1 failed, 15 passed`，3/3 |

唯一差异（崩溃版 `:400` 存在，不崩版该处为两行注释）：
```python
        self.destroyed.connect(_release_anim_source)   # ← 崩溃版有这行
```
它连接的槽体：
```python
        def _release_anim_source(_obj=None):
            layer = _layer_ref()                          # weakref 解引用
            src = getattr(layer, "_anim_source", None)    # ← 属性查找即 use-after-free
            if src is not None:
                AnimationRegistry.instance().release(src.path)
```
复现命令（快照已删，需重建时按上表 md5 校验）：
```bash
pytest tests/widgets/test_tab_manager_window.py -q --no-header -p no:cacheprovider
# 崩溃态：..............Fatal Python error: Segmentation fault（前 14 过，第 15 崩）
# 正常态：1 failed, 15 passed
```

### 机制

`QObject::destroyed` 是在 `QObjectPrivate::deleteChildren` 链**内部**发射的。
发射时该对象的 C++ 部分（含子对象、sip 包装）**仍在析构过程中**，不是「已死但可安全访问」
的状态。此刻任何重新进入 Python 求值并触碰该对象的操作都是 use-after-free。

### 禁止事项（not negotiable）

在 `destroyed` 槽里**不要做任何真实工作**：

- ❌ 不读 `self` 的任何属性（含 `weakref.ref(self)` 解引用后取属性）
- ❌ 不调用持有 Qt 对象的方法（含 `stop_prefetch`、`deleteLater`、`hide`）
- ❌ 不同步调用需要遍历 Qt 对象的单例方法

### 正确形态

1. **需要的信息提前抽成纯 Python 值**（`str` / `int` / `dict`），槽里只读这些值。
2. **实际动作延后到 next-tick**：`QTimer.singleShot(0, lambda: do_work(saved_value))`，
   等 `deleteChildren` 整条链走完、回到事件循环再执行。
3. **幂等由「取完即置 None」保证**，不依赖对象存活状态判断。
4. 更稳的做法：把归还逻辑放到 `closeEvent` / 宿主显式清理入口，**根本不在
   `destroyed` 上挂副作用钩子**。本轮最终采用的就是这个方案。

### 一条必须写死的认识

> 「空槽 / `pass` / `return` 不崩」**不能**作为「空槽是安全的」的依据。

实测中 `pass`、`return`、`_x = 1`、`lambda *a: None` 都不崩，而
`AnimationRegistry.instance()`（只调一个纯 Python 单例、不碰 `self`）必崩。
差异来自 CPython 帧的可折叠优化——常量级函数体不会真正进入求值循环。
这是**实现细节**，不是契约：在「空槽」里加一行 `x = self.foo` 就会立刻崩。

结论按风险写：**`destroyed` 槽只允许做「无副作用的值搬运 + 延后派发」**，
不要基于「这样写没崩」去推断安全性。

---
## 9. 统计口径与环境判据的坑（2026-11-27 补充）
> 第 5 节讲「数字为什么失真」，本节讲「尺子本身错了」。两类区分：前者是测量技术的局限，
> 后者是**用错了工具，得到的数字看起来完全正常**。

| 坑 | 实测表现 | 规避 |
|---|---|---|
| **`pgrep -c -f "main.py"` 漏报测量实例** | `pgrep` 返回 **0**（看着像干净窗口），`measure_gate check` 报 **5~6 个实例**、退出码 2（STOP） | 门禁还认 `/tmp/run_app_*.py` 宿主（`measure_gate.py:98`），`pgrep -f main.py` 抓不到。**判环境只认 `measure_gate check` 的退出码**，不要用 `pgrep` 自证干净 |
| **renderer 只数子进程会漏孤儿** | 本机 12 个 `QtWebEngineProcess` **全部不在** `ME.children(recursive=True)` 里（reparent 到 launchd），`children` 口径数到 0 | 统计 renderer 必须**双口径**：`children` + `psutil.process_iter()` 全局扫，两者差值 = 孤儿数。只报 `children` 会把「reparent」误算成「已退出」，凭空得出几十万 MB 的「释放」 |
| **非本进程子进程 ≠ 泄漏** | 12 个 renderer 被当成残留，实际是用户正式版 app（`PPID=1` 是 Finder 启动的正常状态） | 判「是不是我的」看 `PPID` 链和启动时间；用户已装的 app 与测量进程无关，不是泄漏证据 |
| **抓栈失真不止一例**（补充第 5 节） | 除 197.9ms→0.1ms 外，同批还有：nav 样式批量化报 **12~25ms** 实测 **0.87ms**；`scroll_bar.__initWidget` 报 **216ms** 实测 **0.02ms** | 同一批采样数据里出现多个「百毫秒级但代码看不出重活」的热点，**优先怀疑采样器自耗时**，不是挨个优化 |
| **工具报的「文件坏了」多半是工具自己的问题** | 两例同型：① `awk`/`sed` 打印含中文的 `.md` 全是乱码，`grep -c` 查不到刚写进去的字符串；② 带 BOM 的 `.py` 让 `ast.parse` 报 `invalid non-printable character U+FEFF`。两例文件本身都完好 | 判「文件是否损坏」**只用一条基准**：`.venv/bin/python -c "import pathlib; print(pathlib.Path('<f>').read_text(encoding='utf-8')[:200])"`。`read_text` 成功 = 编码没问题。① 是终端 locale 问题，换 Python 复核即可；② 是 BOM，读用 `utf-8-sig`。**不要基于工具输出就重写文件** |

---
## 10. 不可引用的历史数据（2026-11-27 复核）

> 本节的用途：**读到以下文件时先查这里**，避免把不可比的数字当结论引用。
> 判据来自 #92 的三方交叉核验（工具源码 + report.json env 字段 + 文件系统实体检查）。

### 10.1 `tools/perf/results/baseline_before_selfcheck.md` —— **整份不可引用**

这三条理由各自独立成立，任意一条都足以否掉它：

| # | 问题 | 证据原文 | 后果 |
|---|---|---|---|
| 1 | **自比对** | `- before: tools\perf\results\baseline_before.json`<br>`- after:  tools\perf\results\baseline_before.json` —— **同一个文件** | 所有 `Δ` 与 `Δ%` **恒为 0**，`判定` 列（✅/⚠️/❌）建立在 Δ 上，**全部无意义** |
| 2 | **Windows 产物** | 路径分隔符是**反斜杠** `tools\perf\results\`（macOS 不产生）；且 `find /tmp /Users/... -name "baseline_before*.json"` 在本机**无结果** | 原始数据不在本机，无法复核；平台口径未知（Windows vs macOS 的启动/RSS 不可混比） |
| 3 | **工具版本不明** | 同轮 checklist 已自述：`/tmp/wt_base` 的旧版 `e9220121`「没有 `--cold-mode project`，冷启动口径不对，数字不可混比」 | 该文件的绝对值（`to_visible_s 2.7556s` / `app_init_s 2.3356s` / `rss_visible_mb 228.84MB`）来源版本无法判定 |

**明确不可引用的内容**：
- ❌ 该文件**所有** `Δ` / `Δ%`
- ❌ 该文件的 `判定` 列（✅ OK / ⚠️ WARN / ❌ FAIL）
- ⚠️ 该文件的绝对值：不建议；若必须引用，须同时标注「工具版本与平台均未确认」

### 10.2 可引用的部分：`report.json` 的 `env.*`

每次 `benchmark_core.py` 运行都会产出 `results/benchmark_core_<ts>/report.json`，其中 `env` 段是**可直接核验的原始字段**：

| 字段 | 用途 | 已验证值样例 |
|---|---|---|
| `env.cwd_isolated` | 该轮是否用了隔离 cwd | `True` |
| `env.data_dir` | 该轮的数据目录；**配合 `os.path.isdir()` 判实体是否存在**，存在即强证据 | `/var/folders/.../T/drifox_bench_nr0n7l5n`（`isdir=True`） |
| `env.entry` | 该轮实际测的是哪个仓库 | `/private/tmp/wt_base/main.py` vs `/Users/.../DriFox/main.py` |

**测量值本身**：只在**同一工具版本、同一轮次内**可比。
跨轮/跨版本/跨平台引用必须先核 `env.entry` + 工具 md5，否则不成立。

### 10.3 隔离机制：不依赖调用者 shell

`tools/perf/benchmark_core.py:386-393`（源码原文）：
```python
# ⚠️ 必须显式覆盖：DRIFOX_DATA_DIR 在 get_app_data_dir() 里优先级最高，
# （实测：export DRIFOX_DATA_DIR=/tmp/leak_test 后，cwd 隔离目录为空，...
app_data_dir = (data_dir / ".drifox").resolve()
app_data_dir.mkdir(parents=True, exist_ok=True)
env.pop("DRIFOX_DATA_DIR", None)              # ← 先删掉继承来的
env["DRIFOX_DATA_DIR"] = str(app_data_dir)    # ← 再强制设成自己的隔离目录
```

**结论**：工具层已 `pop` 掉继承值再强制覆盖，**调用者 shell 里预设的 `DRIFOX_DATA_DIR` 不会污染隔离**。
这条是内建保证，不靠使用者自觉——但**前提是跑的就是这份工具**（旧版是否含此逻辑需单独核）。

### 10.4 一句话判据

> 看到一个「历史测量数据」想引用它之前，先问三句：
> **它的 before/after 是两份不同数据吗？它的原始 json 在本机吗？它知道自己是哪版工具跑的吗？**
> 三句有一句不能答，就不要引用。
