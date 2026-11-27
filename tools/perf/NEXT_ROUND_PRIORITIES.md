# 下一轮优先项

> **本文件收录两类，性质不同、处置方式不同**：
> - **一~九节：性能优化机会**（来源：任务 #57 只读清单，6 轮真实启动拦截 `Popen.__init__` 精确计数）
> - **第十节：静默失败类缺陷**（来源：`SessionStore` DSN 守卫缺口，不是性能问题）
>
> **基线**：每轮 9 次 spawn / 约 152ms；其中首窗前仅 3 次 `defaults`（约 26ms，已由 B3 消除）
> **配套**：`tools/perf/PERF_FALSIFIED.md`（已证伪清单）、`tools/perf/MEASUREMENT_PROTOCOL.md`（测量规程）

## 〇、五条前置声明（读任何一条收益数字前先看这里）

1. **收益时间窗：全部在首窗之后**。P1/P2/P4/P5 作用于 `first_chat_window` 之后 1.5~2.5s，
   **不改善首窗时间**。收益形态是「启动后 CPU/IO 占用」与「多窗口线性放大」。
   —— 上一轮 B5 栽在「把位移当收益」、冷启动 2590ms 栽在「口径合成」，本次每条都带时间窗。
2. **P1 的性质是 bug，不是优化**。`_warm_git_cache` 的预热**从未命中过它的消费者**。
   这段代码从写下来那天起就没起过作用（详见 P1 与下方独立核验）。
3. **P3 已由 leader 裁决搁置**。30s TTL 会让分支标签在 `git checkout` 后最坏迟 30s 更新，
   属「用户能感知的信息滞后」，不能拿来换「用户感知不到的 19ms」。
4. **🔴 本文档除 P1 与 D1 测试侧止血外，其余条目均未实施；未实施条目的收益为预期值而非实测落地值。（P1 已于 `88e232a54`/2026-11-23 落地在 HEAD；D1 测试侧本轮新增 5 处）**
   本文是「待办清单」，不是「已完成的优化记录」。引用时勿当作已落地。
5. **一~九节是「优化机会」，第十节是「缺陷」**。前者可以讨论「值不值得做」；后者
   （`SessionStore` DSN 守卫）是**正确性问题**，不参与性价比讨论——它在把事情做错，
   而且不报错。排期时不要把两者混进同一个优先级序列。
5. **一~九节是「优化机会」，第十节是「缺陷」**。前者可以讨论「值不值得做」，
   后者（`SessionStore` DSN 守卫）是**正确性问题**，不参与性价比讨论：它在把事情做错，
   而且不报错。排期时不要把两者混在同一个优先级序列里。

## 一、优先项一览

| # | 位置 | 性质 | 收益 | 收益时间窗 | 风险 | 顺序 |
|---|---|---|---|---|---|---|
| **P1** | `backend.py:1433`（`_warm_git_cache`） | **bug（预热空转）** | 26ms | 首窗之后 | 低 | **1** |
| **P4** | `worktree_section.py:89/369`（`read_all_worktree_meta`） | 优化（延后非首屏数据） | 17.7ms | 首窗之后 | 低 | **2** |
| **P2** | `worktree_section.py:129/135`（`list_local_branches`） | 优化（合并两次 git） | 19ms | 首窗之后 | 中低 | **3** |
| **P5** | `workbench_panel.py:697`（插件页全量挂载） | 结构性改造 | ~131ms | 首窗之后 | 中 | **4（单独立项）** |
| **P6** | `backend.py:1409`（SessionStart 裸键读 + `ctx["worktree"]`） | **删死代码（零收益，消除误导）** | **0ms** | — | 低 | **5** |
| ~~P3~~ | ~~`branch_chip.py:65`~~ | ~~优化（复用缓存）~~ | ~~19ms × 窗口数~~ | — | — | **搁置** |

**执行顺序：P1 → P4 → P2 → P5。P3 搁置。**

---

## 二、P1（优先修，性质是 bug）

**问题**：`_warm_git_cache` 预热写 `(path, True)` 缓存，消费者读 `(path, False)` —— 两套独立缓存，预热永远打不中。

> **行号说明（两条都准，指的不是同一行）**：方法定义在 **:1423**（`def _warm_git_cache`），
> 要改的调用在 **:1433**（`GitWorktreeDetector.get_repo_info(project_root)`）。
> 汇总表与图景里的 `1433` 指后者；实施代码块里的 `1423` 指前者。

**独立核验（本文件核对，非转述）**：
```
缓存键  app/utils/git_worktree.py:608
        cache_key = (path, bool(with_counts))

预热    app/core/conversation/backend.py:1433
        GitWorktreeDetector.get_repo_info(project_root)          → with_counts 默认 True

消费者  app/core/conversation/backend.py:1351-1352
        get_repo_info_stale_while_revalidate(project_root, with_counts=False)
```
**结论：三处全对，断言成立。** 预热写 True、消费者读 False，两者不共享条目。

> 📌 **同一条链上还有第三个点**：`backend.py:1409`（SessionStart 路径）读的是 `(path)` **裸键**，同样恒不命中。
> 那一处不产生性能收益（因无人消费），处置是「删死代码」而非「修口径」，见 **P6**。

**实测（提案方 5 次清缓存采样）**：
```
with_counts=True   中位 25.9ms  [24.2, 25.6, 25.9, 26.7, 28.1]   子进程 2 个（worktree list + rev-list）
with_counts=False  中位  0.1ms  [0.1, 0.1, 0.1, 0.2, 0.3]        子进程 0 个（纯文件路径）
```
**差 260 倍**，且 worktree 越多差距越大（N×rev-list）。

**改法**：`get_repo_info(project_root, with_counts=False)`，与消费者对齐。

**为什么这条排第一**：
- 这是**修 bug**，不是拿性能换东西。改完只会有「原本该发生的事终于发生了」这一个效果。
- `with_counts=False` 是仓内已在生产使用的成熟路径（`backend.py:1352` 正在用），非新增设计。
- **与 `PERF_FALSIFIED` 的「位移当消除」不是同一类**：位移至少有效（成本挪到别处），
  这条是**完全没生效**——预热跑了 25.9ms 的子进程，却给了一份没人读的缓存。
  比位移更该修：位移是取舍问题，空转是缺陷。

**注意**：改后唯一语义变化是「工作台工作树页不再被预热」。该页默认不可见（`WorkbenchVisible` 默认 False），
且 P4 会进一步把它的数据拉取延后到真正可见时——两条互补。

---

#### 实施代码

**现状**（`app/core/conversation/backend.py:1423`，锚点 `_warm_git_cache` 内 `_warm()`）
```python
def _warm_git_cache(self, project_root: str):
    """后台线程预热 git 缓存，避免 create_session 时同步执行 git 子进程（~1.1s）"""
    if not project_root:
        return
    import threading
    def _warm():
        try:
            from app.utils.git_worktree import GitWorktreeDetector
            GitWorktreeDetector.get_repo_info(project_root)   # ← 默认 with_counts=True
        except Exception:
            pass
    threading.Thread(target=_warm, daemon=True).start()
```

**建议改法**（只改一行，加参数）
```python
            # 必须与消费者同口径：backend.py:1351 用 with_counts=False 读
            # (path, False) 缓存；不传则默认 True，写进 (path, True) 无人读。
            GitWorktreeDetector.get_repo_info(project_root, with_counts=False)
```

**验证**
```bash
# 1) 单点确认两种口径的子进程数与耗时
.venv/bin/python - <<'EOF'
import sys, time, subprocess
sys.path.insert(0, '.')
cmds = []
orig = subprocess.run
subprocess.run = lambda a, *x, **k: (cmds.append(a[1] if len(a) > 1 else a), orig(a, *x, **k))[1]
from app.utils.git_worktree import GitWorktreeDetector as D
for wc in (True, False):
    D._info_cache.clear(); D._worktrees_cache.clear()
    cmds.clear()
    t = time.perf_counter(); D.get_repo_info('.', with_counts=wc)
    print(f"with_counts={wc}: {(time.perf_counter()-t)*1000:.1f}ms, 子进程={cmds}")
EOF
# 期望：True → 2 个子进程(worktree/rev-list)；False → 0 个子进程

# 2) 端到端：预热线程不再起 git
#    用 sitecustomize 拦截 subprocess.Popen.__init__ 记录 cmd（见 MEASUREMENT_PROTOCOL.md），
#    过滤 "git worktree list"，改前每轮 1 次、改后应为 0 次（工作台不可见时）
```

**验证点**：改后 `git worktree list --porcelain` 在启动路径上消失；`_build_worktree_context_dict` 首次调用即命中缓存（无 `git-info-refresh` 线程冷启）。
**风险**：低。`with_counts=False` 是 `backend.py:1352` 在用的成熟路径。唯一语义变化是「工作台工作树页不再被预热」，该页默认不可见，且 P4 会进一步延后它的数据拉取。

## 三、P4（次优先，低风险）

**问题**：`read_all_worktree_meta` 只服务「工作台 → 工作树页」的行内标注，而该页**默认不可见**，
却因 `WorkbenchPanel` 构造时实例化所有插件页而必然执行。

**改法（单独做）**：从 `_fetch_repo_info_job` 拆出，改到页面首次 `showEvent` 或宿主真正 `refresh_data()` 时补齐；
标注回来后再 `_repopulate` 一次。

**为什么排在 P2 之前**：风险更低（唯一后果是行内标注晚一拍出现），且它与 P1 有一处呼应——
P1 让「预热不再为空转」省 26ms，P4 让「不必要的数据拉取不做」省 17.7ms，两条方向一致（都是「不该做的别做」）。

---

#### 实施代码

**现状**（真实触发点是 `plugins/worktree-manager/ui/worktree_page.py:71-74`，`_PageLoadJob.run()` 三路并行）
```python
with concurrent.futures.ThreadPoolExecutor(max_workers=3) as ex:
    f_info = ex.submit(GitWorktreeDetector.get_repo_info, self._workdir)     # ← 因 P1 的预热而命中
    f_branches = ex.submit(list_local_branches, self._workdir)
    f_desc = ex.submit(read_all_worktree_meta, self._workdir)                # ← 要延后的这一路
    info = f_info.result()
    branches = f_branches.result()
    descriptions, creators = f_desc.result()
```

> ⚠️ **修正文档正文的一处定位**：`read_all_worktree_meta` 在启动路径上是由 `worktree_page.py:74` 的 `_PageLoadJob` 触发的（线程名 `ThreadPoolExecutor-0`，实测吻合），**不是** `worktree_section.py:369` 的 `_fetch_repo_info_job`（后者走 `QThreadPool`，只在 `_repopulate()` 时跑）。实施时改 `_PageLoadJob` 这一路即可；`_fetch_repo_info_job` 里那份是工作台可见后的正常刷新，保留。

**建议改法**（四处小改动：页面构造时不可见就不拉 meta）

① `_PageLoadJob.__init__` 增参（`worktree_page.py:59`）
```python
def __init__(self, workdir: str, signals: _PageLoadSignals, with_meta: bool = True):
    super().__init__()
    self._workdir = workdir
    self._signals = signals
    self._with_meta = with_meta
```

② `run()` 里改为按需提交（`worktree_page.py:71-77`）
```python
with concurrent.futures.ThreadPoolExecutor(max_workers=3) as ex:
    f_info = ex.submit(GitWorktreeDetector.get_repo_info, self._workdir)
    f_branches = ex.submit(list_local_branches, self._workdir)
    f_desc = ex.submit(read_all_worktree_meta, self._workdir) if self._with_meta else None
    info = f_info.result()
    branches = f_branches.result()
    descriptions, creators = f_desc.result() if f_desc is not None else ({}, {})
```

③ `_rebuild()` 按可见性传参（`worktree_page.py:366`）
```python
# 首次构造时宿主尚未 show，isVisible() 为 False → 启动路径不拉 meta；
# 用户切到该页时 refresh_data() 会再走一次本方法，此时已可见 → 正常拉。
job = _PageLoadJob(workdir, signals, with_meta=self.isVisible())
```

④ 兜底：可见后补刷（`SystemWorktreePage` 内新增）
```python
def showEvent(self, event):
    super().showEvent(event)
    if not getattr(self, "_meta_loaded", False):
        self._meta_loaded = True
        self.refresh_data()      # 可见后补一次；刷新本身走后台线程，不阻塞
```

**验证**
```bash
# 1) 启动路径（工作台默认不可见）应无 git config 子进程
#    拦截 subprocess.Popen.__init__，过滤 "git config --get-regexp"，改前 1 次/轮、改后 0 次

# 2) 功能：打开工作台 → 工作树页，确认行内「说明/创建者」标注正常显示
DRIFOX_DATA_DIR=/tmp/wt_meta .venv/bin/python main.py
#    手动点开右侧工作台 → 工作树页 → 检查子工作树行的 description / AI 标签是否出现

# 3) 回归
.venv/bin/python -m pytest tests/widgets/ -q -k "worktree or tab_panel" 2>&1 | tail -5
```

**验证点**：启动后 `git config --get-regexp ^worktree\..*` 不再出现；打开工作树页后标注正常出现（允许晚一拍）。
**风险**：低。唯一后果是行内标注在页面首次可见后才出现（约几十 ms 后台延迟）。若 P5 实施，本条自然消失。

## 四、P2（第三，有一个未验证项）

**问题**：`list_local_branches` 起两次 git（`branch --format=...` 和 `branch --show-current`），后者可用 `%(HEAD)` 合并。

**实测格式可行**（提案方在本仓库验证）：
```
$ git branch --format='%(HEAD) %(refname:short)'
* dev
  devneq
  drifox/test
  master
```

**⚠️ 唯一未验证项**：`%(HEAD)` 在不同 git 版本上的输出稳定性（`*` / 空格）**未在 Windows 目标机验过**。
实施前必须先验，否则拿到的可能不是 `* dev` 而是别的格式。

**附带发现（既有 bug，不夹带本轮）**：detached HEAD 下 `git branch --format=%(refname:short)` 会输出
一行 `(no branch)`，原实现把它当成真分支塞进下拉列表。合并方案可顺手修（过滤 `(no branch)` / `(HEAD detached`），
但**按裁决不夹带进性能提交**——修 bug 应有独立提交与回归用例（详见文末附节）。

---

#### 实施代码

**现状**（`plugins/worktree-manager/ui/worktree_section.py:125-145`）
```python
def list_local_branches(repo_root: str) -> list:
    """列出本地分支（当前分支排最前）"""
    try:
        r = _git(["branch", "--format=%(refname:short)"], repo_root, timeout=8)   # 第 1 次
        if r.returncode != 0:
            return []
        branches = [ln.strip() for ln in (r.stdout or "").splitlines() if ln.strip()]
        current = ""
        try:
            cur = _git(["branch", "--show-current"], repo_root, timeout=5)         # 第 2 次
            current = (cur.stdout or "").strip()
        except Exception:
            pass
        if current and current in branches:
            branches.remove(current)
            branches.insert(0, current)
        return branches
    except Exception:
        return []
```

**建议改法**（合并为一次，用 `%(HEAD)` 标记当前分支）
```python
def list_local_branches(repo_root: str) -> list:
    """列出本地分支（当前分支排最前）；一次 git 拿全"""
    try:
        r = _git(["branch", "--format=%(HEAD) %(refname:short)"], repo_root, timeout=8)
        if r.returncode != 0:
            return []
        branches, current = [], ""
        for ln in (r.stdout or "").splitlines():
            if not ln.strip():
                continue
            mark, _, name = ln.partition(" ")
            name = name.strip()
            if not name:
                continue
            if mark == "*":
                current = name
            else:
                branches.append(name)
        if current:
            branches.insert(0, current)
        return branches
    except Exception:
        return []
```

**验证（第 1 步是硬性前置，Windows 未验）**
```bash
# 1) ⚠️ 必做：在目标 git 版本确认 %(HEAD) 输出形态（本仓 macOS 已验，Windows 未验）
git branch --format='%(HEAD) %(refname:short)'
# 期望：当前分支行以 "* " 开头，其余行以两个空格开头
# 若形态不同（无前导空格 / 其他标记），解析需相应调整，勿直接套用

# 2) 常规仓库：分支列表与当前分支排序
.venv/bin/python -c "
import sys; sys.path.insert(0,'.'); sys.path.insert(0,'plugins/worktree-manager')
from ui.worktree_section import list_local_branches
print(list_local_branches('.')[:5])"

# 3) 端到端：启动时 git branch 子进程从 2 个降到 1 个
#    拦截 subprocess.Popen.__init__，过滤 'git branch'
```

**验证点**：`git branch` 调用减半；下拉列表内容与当前分支排序不变。
**风险**：中低。调用方仅 `worktree_section.py:375`、`worktree_page.py:73`，都只当字符串列表渲染下拉。**`%(HEAD)` 跨 git 版本输出稳定性是唯一未验证项，实施前必须先跑验证第 1 步。**
**注**：本改法**不含** detached 伪分支过滤（那是文末附节的独立 bug，按裁决不夹带）。若要一并修，在 `mark == "*"` 分支前加 `if name == "(no branch)" or name.startswith("(HEAD detached"): continue`。

## 五、P5（最大一笔，但必须单独立项）

**收益 ~131ms**，是四条里最大的一笔。但它是**结构性改动**（页面挂载生命周期），需要覆盖：

- 切页能刷出数据
- 主题刷新后重建
- 多窗口隔离
- `_request_id` 防串数据

**前置条件**：**在干净环境（load < 2）复测 131ms 后再动手**。
理由：本轮已有多起「高负载下的数字不可信」案例（见 `MEASUREMENT_PROTOCOL.md` 第 1/9 节）。
131ms 若在 load 3~4.7 下采样，可能与真实收益差一倍以上。

---

#### 实施代码

**现状**（触发链：构造即挂载 → 挂载后立即拉数据）
```python
# app/widgets/workbench_panel.py:695-697  构造时实例化全部插件页（非懒加载）
for index, page_id in enumerate(order):
    if page_id not in self._plugin_widgets:
        self._mount_plugin_page(infos[page_id])
```
```python
# app/widgets/tab_manager_window.py:2204  构造完立即让当前页自拉数据
try:
    panel.refresh_current_page_data()
except Exception:
    logger.exception("[Workbench] 当前页数据刷新失败")
```
```python
# app/widgets/workbench_panel.py:749-759  页面自拉协议入口（无可见性判断）
def refresh_current_page_data(self) -> None:
    page = self._stack.currentWidget()
    fn = getattr(page, "refresh_data", None)
    if callable(fn):
        try:
            fn()
        except Exception:
            logger.exception(f"[WorkbenchPanel] 页面数据刷新失败: {type(page).__name__}")
```
工作树页是 `order_hint=0` 的首页，因此启动时必然走到它的 `refresh_data()` → `_rebuild()` → 后台 4 个 git。

**建议改法**（最小侵入：在协议入口加可见性闸门，不动挂载逻辑）

① `WorkbenchPanel.refresh_current_page_data` 加闸门
```python
def refresh_current_page_data(self) -> None:
    # 面板不可见时只置脏、不拉数据：工作台 frame 默认 hide()，
    # 启动期挂载的插件页（如工作树页）不应为此起 git 子进程。
    if not self.isVisible():
        self._pending_refresh = True
        return
    self._pending_refresh = False
    page = self._stack.currentWidget()
    fn = getattr(page, "refresh_data", None)
    if callable(fn):
        try:
            fn()
        except Exception:
            logger.exception(f"[WorkbenchPanel] 页面数据刷新失败: {type(page).__name__}")
```

② `WorkbenchPanel.__init__` 初始化标志位
```python
self._pending_refresh = False
```

③ `showEvent` 里补刷
```python
def showEvent(self, event):
    super().showEvent(event)
    if getattr(self, "_pending_refresh", False):
        self.refresh_current_page_data()
```

④ 切页路径（`set_current_tab(user=True)`）此时面板已可见，`refresh_current_page_data()` 照常生效，无需改动。

**验证**
```bash
# 1) 前置：干净环境复测收益（load < 2 才采信）
uptime                     # 要求 load averages 第一个数 < 2
DRIFOX_DATA_DIR=/tmp/wb_hidden DRIFOX_STARTUP_PROBE=1 DRIFOX_STARTUP_PROBE_EXIT_MS=8000 \
  .venv/bin/python main.py
#    拦截 subprocess.Popen.__init__，过滤 'git '，对比改动前后 spawn 次数与合计耗时
#    期望：工作台隐藏时 git spawn 从 6 次/轮 降到 0~1 次/轮

# 2) 功能：打开工作台后数据能刷出来
DRIFOX_DATA_DIR=/tmp/wb_show .venv/bin/python main.py
#    手动点标题栏「右侧边栏」按钮 → 确认工作树页有内容（主仓库卡片/分支/子工作树行）
#    切换页签 → 确认每页数据正常；切换主题 → 确认页面重建后仍有数据

# 3) 多窗口隔离：开两个对话窗口，各开一次工作台，确认两窗口数据不串

# 4) 回归
.venv/bin/python -m pytest tests/widgets/ -q -k "workbench or tab_panel or tab_manager" 2>&1 | tail -8
```

**验证点**：工作台隐藏时启动无 git 子进程；打开后一帧内数据到位（允许一次后台延迟）；切页 / 切主题 / 多窗口均正常。
**风险**：中。属结构性改动（页面数据生命周期）。必须覆盖：切页能刷出、主题刷新后重建、多窗口隔离、`_request_id` 防串数据（`worktree_page.py:339/368`）。
**前置条件（硬性）**：131ms 必须在 load < 2 的干净环境复测后再动手 —— 本轮基线 load 3.4~4.7，git 单次耗时极差 42.2ms > 中位 19.4ms，该数值精度有限。

## 五之二、P6（删死代码，零性能收益）

> 失败分类与完整图景（三处空转、三点三键零命中）见 `PERF_FALSIFIED.md` 的「实例 2」。
> 本条只讲**怎么处置**。

> ⚠️ **编号说明**：这里的 **P6** 与本文档里已搁置的 **P3**（`branch_chip` 复用缓存）**不是同一件事**。
> 两者都曾被称为「P3」，为避免混淆，此处改用 P6。

**位置**：`app/core/conversation/backend.py:1409`（`_build_session_context()` 内裸键读）+ `:1411-1417`（`ctx["worktree"]` 赋值）+ `:1424`（误导性 docstring）

**性质**：**不是性能优化，是消除误导。** 修了不改善任何指标，只删掉一段从未生效的代码和一句假的 docstring。

### 现状：同一条链上三个点、三个不同键、零命中

```
预热点    backend.py:1433  get_repo_info(project_root)                     → 写 (path, True)
消费点A   backend.py:1351  get_repo_info_stale_while_revalidate(...,False)  → 读 (path, False)   ← P1
消费点B   backend.py:1409  _cache_get(_info_cache, project_root)            → 读 (path) 裸键      ← P6
```

**整条链上没有任何一处命中。** 三个点分别由不同人加上，每一层单独看都「加了缓存」，看起来都在省时间——但没有人验证过端到端能命中。
（这句解读来自 build@win_798，Leader 认为最有警示价值，保留原话。）

### 为什么「删」而不是「修裸键」

1. **修了也没人读**：把 `cache.get(project_root)` 改成 `cache.get((project_root, False))` 后 `ctx["worktree"]` 确实有值了，但**全仓零读取** → 仍是无人消费的代码。
2. **留着有风险**：将来有人看到 ctx 里有 `worktree` 字段，会以为「这是官方契约」，然后在 hook 里开始读。而它的语义是「**SessionStart 时刻的快照**」，可能已过时。
3. **docstring 是假的**：`:1424` 写「避免 create_session 时同步执行 git 子进程（~1.1s）」，而这段代码从未生效 → 那 1.1s 从未被它省掉。删掉它等于承认「这个优化从来没做过」，而不是「做了一个假的优化」。

### 处置（拆成两步，依赖关系不同）

#### P6a · 删死代码（与 P1 无关，可独立执行）

```python
# 删除 backend.py:1409 的裸键读 + ctx["worktree"] 赋值（含 1411-1417 整段）
# 保留 :1355 的写入（PreUserMessage 路径正常，不受影响）
```

**理由**：裸键 `(path)` 永远读不到 `(path, True/False)` 的 tuple 键，无论 P1 修不修都是死代码。

#### P6b · docstring 复核（P1 已于 HEAD（`88e232a54`），**本项可执行**）

修完 P1 后，重新评估 `:1424` 与 `:768` 这两句 docstring。
**预期结论：不用动**（P1 修好后预热真的能命中，这句话从「假的」变成「真的」）。

**若 P1 未落地就单独执行 P6b，会删掉一句「修完就正确」的注释** —— 见下方依赖说明。

**⚠️ 行号核对（本文件 grep 实测）与 build@win_798 未提的一处：**

| 断言 | 实测 |
|---|---|
| `_cache_get(_info_cache, project_root)` 裸键读 | **:1409** ✓ |
| `ctx["worktree"] = {` 赋值起点 | **:1411** ✓（1411-1417 共 7 行） |
| `ctx["worktree"] = {` PreUserMessage 写入 | **:1355** ✓（保留） |
| 那句 docstring | **:1424** ✓，**但 `:768` 还有一句完全相同的**（`_deferred_warm_git_cache` 内） |

**⚠️ 依赖冲突（动手前必读）：那句 docstring 是否「误导」取决于 P1 是否落地。**

```
:1424  """后台线程预热 git 缓存，避免 create_session 时同步执行 git 子进程（~1.1s）"""
:768   # 后台预热 git 缓存，避免 create_session 时同步执行 git 子进程（~1.1s）
```

- **若 P1 未修**（预热仍写 `(path, True)`）：这句话是假的，应删。
- **若 P1 已修**（预热改 `with_counts=False`）：预热**真能**命中 `create_session` 走的 `(path, False)`，
  这句话就变**正确**了，不该删。

**执行顺序**：
1. **P6a**（删死代码）—— 与 P1 无关，随时可做
2. **P1**（修预热口径）
3. **P6b**（docstring 复核）—— 做完 P1 后再看，预期不用动

反过来（先做 P6b 再做 P1）会删掉一句修完就正确的注释。

### 影响面（两条独立路径，只有 SessionStart 受影响）

| 路径 | 触发时机 | 缓存键 | 命中 |
|---|---|---|---|
| `_build_worktree_context_dict()`（`:1314`） | PreUserMessage，**每条消息** | `(path, False)` | ✅ 正常 |
| `_build_session_context()`（`:1367`） | SessionStart，**仅会话启动** | `(path)` 裸键 | ❌ 恒不命中 |

`engine.py:512` 与 `chat_worker.py:1032` 走的都是前者，**主聊天链路完全不受影响**。

### 为什么定低优先级（四条依据，已由 build@win_798 调查）

1. **零读取点**：`ctx.get("worktree")` / `"worktree" in ctx` / `context.get("worktree")` 全仓均 0 命中
2. `assistant_hub` 的 hooks（`project_notes.py:306/341/393/639`）全从文件系统读 `.git` / `gitdir:` / `commondir` 自算 worktree 签名，**不依赖 ctx 注入**
3. `app/plugins/contracts/hook_policy.py` **未声明**该字段为 context 契约
4. 功能有替代：PreUserMessage 每条消息注入，比 SessionStart 更新更及时

**归属确认**：`git status --short app/core/conversation/backend.py` 无输出，`git show HEAD` 对照三行完全一致 → **HEAD 上就存在，不是本轮回归**。

### 与 P1 的区别（排期时别混）

| | P1（预热打不中） | P6（删死代码） |
|---|---|---|
| 现状 | 口径不匹配，预热白跑 25.9ms | 从未生效，读的键永远不存在 |
| 修了之后 | **能拿回 25.9ms** | **0ms**，只消除误导 |
| 性质 | 有性能收益的 bug | 无性能收益的清理 |

**混在一个条目里会让人误以为修 P6 也有性能收益。**

---

## 六、精度声明（写报告时必须带上）

所有 ms 来自 **load 3.4~4.7、并发 1~2** 的 6 轮采样。
**git 单次耗时极差 42.2ms > 中位 19.4ms** —— 单条 19ms 只能定性为「量级几十毫秒」，
不能当作精确值引用。P5 的 131ms 尤其要在干净环境复测。

## 七、与已证伪清单的关系

`PERF_FALSIFIED.md` 里「位移当消除」一类的判据是：**成本只是被挪走，净收益远小于账面**。
本次 P1 是**更强的一类**：成本没被挪走也没被消除，而是**产生了一份无人消费的结果**（空转）。
判据可加一条：

> **看到「预热 / 缓存 / 预取」类代码，先核对它的写入口径与消费口径是否一致。**
> 不一致时，这段代码的收益为 0（甚至为负），且不会有任何报错。
---

## 八、附：detached HEAD 假分支（独立 bug，**不夹带本轮**）

**位置**：`plugins/worktree-manager/ui/worktree_section.py:129`（`list_local_branches` 第 1 次 git）

**现象（实测）**
```
$ git branch --show-current
（空）
$ git branch --format=%(refname:short)
(no branch)      ← 原实现把这行当成一个真分支
dev
devneq
```
用户在 detached HEAD 的工作树里打开工作树页，分支下拉列表会多出一个叫 `(no branch)` 的假条目（不可选）。

**性质**：正确性问题，**现状即存在，不是 P2 方案引入的**。

**按裁决**：不夹带进性能提交——修 bug 应有独立提交记录与回归用例。

**建议修法**（若单独开单）
```python
if name == "(no branch)" or name.startswith("(HEAD detached"):
    continue
```

**回归用例建议**：造一个 detached HEAD 的临时仓库，断言 `list_local_branches()` 返回不含 `(no branch)`。

---

## 九、实施代码块内嵌状态

按裁决：提案原文（304 行，含实测过程与中间推理）**不进仓库**，改为在本文各条目末尾直接内嵌
「现状代码 + 建议改法 + 验证命令」。

- **填表责任**：build@win_793（提案作者）
- **结构已就位**：P1 / P4 / P2 / P5 各节末尾已预留 `#### 实施代码` 占位
- 占位填完后，本文即可独立支撑实施，不依赖任何 `/tmp` 路径

| 条目 | 内嵌内容 | 状态 |
|---|---|---|
| P1 | 现状 `_warm_git_cache._warm()` + 改法（加 `with_counts=False`）+ 验证 | ✅ 已填（见「二、P1」节末） |
| P4 | 现状 `_PageLoadJob.run()` 三路并行 + `with_meta` 按可见性传参 + 验证 | ✅ 已填（见「三、P4」节末） |
| P2 | 现状两次 `_git` + 合并后的 `%(HEAD)` 解析 + Windows 验证清单 | ✅ 已填（见「四、P2」节末） |
| P5 | 现状 `_mount_plugin_page` 循环 + 可见性闸门改造 + 复测命令 | ✅ 已填（见「五、P5」节末） |

**填写中的一处定位修正**（P4）：正文原写 `read_all_worktree_meta` 由 `worktree_section.py:369`
的 `_fetch_repo_info_job` 触发。实测线程名为 `ThreadPoolExecutor-0`，对应的是
`worktree_page.py:74` 的 `_PageLoadJob.run()` 三路并行；`_fetch_repo_info_job` 走 `QThreadPool`
且只在 `_repopulate()` 时跑。实施时改 `_PageLoadJob` 这一路。已在 P4 实施代码块内标注。

**P2 不含 detached 伪分支过滤**：按裁决该 bug 独立记录（第八节），P2 代码块未夹带；
需要一并修时按 P2 块末尾的 `注` 加两行即可。

---

# 第十节 · 静默失败类缺陷（性质与一~九节不同）

> **这一节不是性能优化。** 它是「代码在把事情做错，而且不报错」。
> 排期时不要和 P1~P5 放进同一个性价比序列——那几节可以讨论值不值得做，这一条不能。

## D1 · `SessionStore` 把 SQLite DSN 当目录名，静默建出真实库文件

**性质**：静默失败（silent failure）。无异常、无日志、无告警。

**位置**：`app/core/store/session_store.py:56`（`__init__`）

**现象**：调用方传 `SqliteStorageEngine(db_dir=":memory:")`——本意是「用内存库」，
但 `SessionStore` 把它当**目录名**拼路径 → `:memory:/sessions.db` → 在 **cwd 下真的建出目录并写盘**。

**证据（SQLite 自己给的，不是推断）**：
```
sqlite3 PRAGMA database_list
→ [(0, 'main', '/Users/dingma/work/DriFox/DriFox/:memory:/sessions.db')]
```
返回的是绝对路径，证明它打开的是一个真实的磁盘文件。

**影响面（主仓）**：
```
:memory: 用法总计 98 处
├─ 49 处正确（SandboxConfig 有专门处理）
└─  5 处错误（分布在 3 个测试文件）
```

**为什么测试隔离完全失效**：
`SqliteStorageEngine(db_dir=...)` **绕过** `get_app_data_dir()`，直接用自己的 `db_dir`。
所以 `tests/conftest.py:22` 设的 `DRIFOX_DATA_DIR=.dokifox-test` 对这条链**一点作用都没有**——
测试以为自己跑在隔离目录，实际写进了仓库 cwd。

**本轮已做**：测试侧新增 5 处改 `tmp_path`（累计 13 处，全部可执行调用点已无 `:memory:`；仅保留 3 处防回归注释说明）。属「止血」。

**下一轮要做的守卫（根治）**：
```python
# app/core/store/session_store.py:56  __init__ 内
# ⚠️ 位置要求：必须加在【单例早退判断之后】、【self._db_dir 赋值之前】。
#    否则单例的二次调用会误报（第二次进来 db_dir 可能已是规范化后的值）。
if isinstance(db_dir, str) and db_dir.startswith(":"):
    raise ValueError(
        f"SessionStore 不接受 SQLite DSN 形式（{db_dir!r}）。"
        f"若需内存库请显式实现（当前不支持 WAL/多线程）；"
        f"若需临时库请传真实目录路径。"
    )
```

**为什么不干脆「真正支持内存库」**：
1. `:memory:` 库不支持 WAL，多线程各自拿到**独立的库**，与本项目「多线程 + WAL」的假设直接冲突。
2. 更关键：这 5 个测试测的是**存储迁移逻辑**。用内存库测迁移，等于用错误的介质验证正确的逻辑
   —— 测试通过，但测的路径和生产不一样。

**`_db_dir` 影响面（已核实无风险）**：
```
:58-60  单例早退     # 防止重复初始化 / if _initialized: return
:61     self._db_dir = db_dir                       赋值
:62     self._db_path = str(Path(db_dir) / ...)     拼路径字符串（不写盘）
:355    os.makedirs(self._db_dir, exist_ok=True)    建目录（真正写盘点）
外部引用：0 处
```
**定位修正（本文件 grep 实测，全部行号已逐条核对）**：
- `self._db_dir = db_dir` 在 **:61**
- 单例早退判断在 **:58-60**（注释 + if + return），所以守卫插在 **:60 之后、:61 之前**
- 建目录在 **:355** 的 `os.makedirs`；:61-62 只拼路径字符串，不写盘

**验证方式（守卫加上后）**：
```python
# 应抛 ValueError，且 cwd 下不再出现 ":memory:" 目录
SessionStore(db_dir=":memory:")
```
回归要点：单例二次调用不误报；正常目录路径不受影响。


---

## D2 · `AnimationRegistry.prune()` 判据缺 `sip.isdeleted`（资源残留）

**性质**：缺陷（资源未回收）。不产生错误数据，但关窗路径会漏 1 个 daemon 线程。

**位置**：`app/widgets/anim_background.py` 的 `has_subscribers()` / `prune()`（`AnimationSource` 的订阅者判活）

**现状机制**（实测依据见 `app/widgets/scene_layer.py` 的注释与 plan #62）：
```
QWidget 被 Qt 直接析构（不走 SceneLayer.clear()）
  → sip.isdeleted(layer) == True   （C++ 已死）
  → 但 Python wrapper 仍存活        （widget 树 / 局部变量还持有）
  → has_subscribers() 判 r() is not None → True（误判「还有订阅者」）
  → prune() 跳过，sources 与预解码线程残留
  → 只有 wrapper 被 GC 后再调 prune() 才会清
```

**实测（plan #62）**：
```
[2] 宿主 deleteLater → sip.isdeleted(lay)=True 但 wrapper 存活=True
[4] prune() 后       → sources=1 prefetch=1     ← ❌ 没清掉
[5] del 引用 + gc 后 → 再 prune() → sources=0   ← 这时才清
```

**修法**：`has_subscribers()` 的判据从「存在 `r() is not None`」改为
「存在既非 `None`、又未被 sip 标记删除的订阅者」。

### 风险与代价（plan@win_791 起草）

**代价量级：可忽略（代码推断，未实测）**

规模上限推导：
- `AnimationRegistry._sources` 生产上限 ≈ 2（动画主题窗口 + 侧栏共享同一源）
- `AnimationSource._subscribers` 上限 = 4（scene / chat / window / sidebar 四区域各一层）
- 加 `sip.isdeleted` 检查后：最坏 2 × 4 = **8 次 C 调用 / 每次主题切换**
- 对照：一次主题切换实测数百 ms 级

**结论**：8 次微秒~十微秒级 C 调用相对数百 ms 的切换完全可忽略。真正的风险不在性能。

**风险 1（主要）：`sip` 跨 Qt 版本不通用**
- PyQt6 没有 `sip` 模块（改用 shiboken）
- `prune()` 被 `tab_manager_window.py:2660` 的 `finally` 调用 —— 若硬依赖 `import sip`，将来升级 PyQt 会让 `prune()` 抛 ImportError，进而**整个主题切换崩在 finally 里**
- **修法要求**：`try: import sip; except ImportError: sip = None`，判据退化为原 `r() is not None`（可能漏清但绝不崩）

**风险 2：判据语义要精确**
- `sip.isdeleted(obj)` 只接受 QObject 包装；若 `r()` 返回非 Qt 对象需先 `isinstance` 判断或整体 try 包裹
- 建议判据：`has_subscribers` 改为「存在既非 None、又未被 sip 标记删除的订阅者」

**真要实测时的验证方法（放大最坏情况，不必在生产规模反复采样）**
- 构造 10 源 × 10 订阅（生产上限的 5 倍）
- 在该放大构造下压一次 `prune()`，若总耗时仍 < 1ms → 生产规模无需再测
- 可直接用 `tools/perf/benchmark_core.py` 的计时设施

---

> ⚠️ **「可忽略」是代码推断，不是实测值。**
> 下一轮若有人引用这个结论做决策，请连同「未实测」一起引用，或按上面的放大构造补一次实测。

**优先级**：低于 D1。D1 会产生错误的磁盘写入，D2 只是晚回收（且 wrapper 被 GC 后自愈）。

---

# 第十一节 · 下一轮方案：774MB 稳态残差分段归因（本轮不做）

> **起草**：plan@win_791 ｜ **落盘**：#106
> **状态**：方案，未实施。本节只记录方法与判据，不含任何已测数字。

## 为何本轮不做

**任何源码打点都会让工作树文件数从 33 变 34+，全量回归与 A/B 基线当场作废。**
所以本方案只能排到下一轮——A/B 收口之后再动源码。

## 11.1 待解释量

- 774MB 稳态中约 **310MB 未闭合**（理论可解释量与实测之间的残差）
- **464MB「理论可解释量」是推导值**（由各部分估算相加得出，非端到端实测）
- ⚠️ **464MB 必须标「待重测」**：它本身未经独立验证，拿它当基准去算 310MB 残差，误差会传递

## 11.2 埋点清单（8 段）

| 段 | 位置 | 说明 |
|---|---|---|
| S0 | `main.py` 首批 import 后 | |
| S1 | `QApplication` 返回 | |
| S2 | `MainWidget.__init__` 返回前 | |
| S3 | 首窗 `show` 后 | |
| S4 | `PluginManager` 收尾 | `:54` 已有 `_probe`，**就地扩**，不新增打点位置 |
| S5 | 事件循环首次 idle | |
| S6 | T+3s | |
| S7 | T+10s | |

**每处 `mark()` 与 `mark_mem()` 必须成对出现**（只打时间不取内存，等于没测）。

**现状（实施前必须知道的缺口）**：
```
mark()      全仓仅 3 处调用
mark_mem()  全仓零调用
```
即：现有探针设施只做了「时间打点」的一半，内存口径从未接过线。

## 11.3 分段口径

```
Δ_fp(i) = fp(S_i) − fp(S_{i−1})
```
- 所有 `Δ_fp(i)` 相加**必须回到 774MB**；闭合差 > 50MB → 判「段没切全」，补段或查漏
- **每段 ≥3 次独立冷启动**（同一进程内重复不算）
- `main` 与 `renderer` **分开记**
- ⚠️ **774MB 是否含 renderer，当前口径未钉死** —— 实施前先确认，否则每段数字都可能差一个 renderer 的量级

## 11.4 判据阈值

| 条件 | 判定 |
|---|---|
| `Δ_fp < 30MB` 且 极差 < 中位 50% | **已解释**（该段不再追） |
| `Δ_fp > 100MB` | **嫌疑段**，在段内二分 |
| 信噪比 < 2 | **不可定**（不写进交付、不写改善百分比） |
| 探针 S7 与同轮 `benchmark_core` 稳态差 > 5% | **对齐未成立**，两条口径不可互证 |
| 无探针基线与探针轮差 > 20MB | **探针污染**，该结论作废 |

## 11.5 对齐方式

- **单点探针**：`app/utils/_startup_probe.py:98`
- **连续采样**：`tools/perf/benchmark_core.py:9/59`
- 两者**同 pid、同轮**对比（跨轮对比无意义）
- **外加一次不走探针的基线**——这是判「探针污染」的唯一依据
- 回归点：`tests/perf/test_memory_budget.py:217` **要先绿**，否则分母不可信

## 11.6 实施顺序

1. **重测 464MB 推导值**（先用端到端方式验证它，否则后面全建在假设上）
2. **S0~S3** 打点，验证 `fp` 单调不减（不单调说明打点位置本身有问题）
3. **S4~S7** + 闭合校验（Δ 相加回 774MB）
4. **仅对嫌疑段二分**（`Δ_fp > 100MB` 的那些）
5. **闭合后才谈优化** —— 未闭合前任何优化都是猜

---
## 附：测量产物归档约定（2026-11-27，踩坑记录）

`tools/perf/results/` 只被 `*.json` 与 `logs/` 规则覆盖，手写 `.md` 会出现在 `git status`；长期归档请写 `.json` 或补 ignore 规则。
