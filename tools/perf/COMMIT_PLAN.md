# 提交计划（本轮 32 个文件改动 → 6 个 commit）

> 本文是**执行依据**，不是建议。执行时逐条对照。
> 落地时间：2026-11-27 12:13（工作树 20 已修改 + 11 未跟踪）
> **本文不执行任何 git 操作**，只描述分组与顺序。

## 〇、先读三条

1. **实际 32 个文件，不是 28 个**：`git diff --numstat` 为 20 个已修改文件，`git status --porcelain | grep '^??'` 为 12 个未跟踪文件（含本文件自身）。其中 2 个文件被拆进两个 commit，故 commit 侧统计为 33 项。
2. **两个文件必须 `git add -p` 拆分**（`main_widget.py`、`llm_settings_card.py`），这是唯一执行风险点，识别特征见各 commit 说明。
3. **四个 commit 无性能收益**（2 / 3 / 5 / 6），提交信息不得写「优化」「提速」。

## 一、推荐顺序

```
1 安全修复 → 2 静默失败 → 3 泄漏修复 → 4 性能优化 → 5 测试基建 → 6 文档+组织
```

排序理由：**前三个是「不修就是错的」，第四个是「修了更好」，后两个是「让前四个将来不被退化」。**

## 二、依赖关系

```
Commit 1  独立，可先落
Commit 2  独立
Commit 3  依赖 main_widget.py 的 7 行 M5 hunk（add -p 拆出）
Commit 4  依赖：
          · _code_cache.py 必须先于/同于 plugin_tool_loader.py
            （后者 :215 `from app.plugins.loaders import _code_cache`，拆开则中间态 ImportError）
          · llm_settings_card.py 懒建段（add -p）
          · main_widget.py 的 28 行 pump hunk（add -p）
Commit 5  依赖：pyproject.toml 的 addopts 新增 --ignore 目标必须同 commit，
          否则默认收集找不到目标 → pytest usage error
Commit 6  无硬依赖；NEXT_ROUND_PRIORITIES.md 被 scene_layer.py 注释引用，
          文档先落更稳
```

---

## Commit 1　安全修复：AST 门禁进程内缓存

```
fix(security): AST 门禁进程内缓存改存分析产物 + 内容哈希键

文件：
  M app/plugins/loaders/_ast_guard.py              (+294/-37)
  A tests/plugins/test_ast_guard_cache_safety.py   (+247, 7 条用例)
```

**性质**：安全修复（P0）

**实测收益**：AST 门禁磁盘缓存层 103ms → 60ms（同环境交替 7 轮，差 43ms vs 极差 5~7ms，信噪比 6.1，可下结论）

**两个 P0 分别是什么**（回滚任一都必须能被 bisect 精确命中）：

1. 缓存存 `GuardResult`（含 `ok`）→ 撤销 `plugin.json` 声明后仍放行；`_shadows_real_module` 遮蔽检测被绕过。热重载只改 plugin.json 不改 .py 时必然命中旧结论。
2. 缓存键含 `mtime_ns + size` → 等长改写源码 + `os.utime` 恢复 mtime 即可绕过新增 `sys.modules` 检测。

**为什么测试必须同 commit**：`test_worktree_not_in_injected_state` 直接读 `_ast_guard.py` 源码做断言，拆开会让只提交测试那一步红。

---

## Commit 2　bug 修复：四处静默失败

```
fix: 修四处静默失败（CAS 恒不命中 / 缓存清理调错对象 / 两处缓存无上限）

文件：
  M app/core/store/session_repository.py                (+60/-25)
  M app/core/infra/memory_governor.py                   (+8/-5)
  M app/utils/history_manager.py                        (+24/-2)
  M app/widgets/cards/settings/theme_style_card.py       (+17/-1)
```

**性质**：bug 修复。四个的共同特征是「不报错但失效」，将来查「为什么某功能一直没生效」时要能一次捞出。

**实测收益**：无性能收益。`session_repository` 顺带消除每次读白烧 174ms + 113MB，但那是故障修复的副作用，不是优化目标。

各自机制：

| 文件 | 失效机制 |
|---|---|
| `session_repository.py` | CAS 用 `serialize(msgs)` 当条件，键序与库内不一致 → 恒命中 0 行；返回值被丢弃 → 完全静默。改为用库内原始 blob，`rowcount != 1` 记 WARNING |
| `memory_governor.py` | `cache_clear()` 调在 `estimate_tokens`（普通函数，无此方法）上，AttributeError 被吞 → 8192 条缓存从未清理 |
| `history_manager.py` | 归档元数据缓存无上限，随归档数量线性增长（512 条上限 + FIFO 淘汰） |
| `theme_style_card.py` | 预览图缓存无上限，dpr=2 单条 1.07MB，跨会话累积即泄漏（24 条上限） |

---

## Commit 3　泄漏修复：动画源回收与 cleanup 遗漏

```
fix(leak): 动画源回收移入 finally + 消息卡三处 cleanup 漏清 + 工具卡映射释放

文件：
  M app/widgets/tab_manager_window.py   (+12/-4)
  M app/widgets/message_card.py         (+14/-0)
  M app/main_widget.py                  (仅 +7 的 M5 hunk，git add -p)
  M tests/widgets/test_tab_manager_window.py (+58/-1)
```

**性质**：泄漏修复（生命周期遗漏）

**实测收益**：无性能收益（防累积，非提速）

**为什么测试同 commit**：该测试新增 `_CREATED_WINDOWS` 登记 + `_destroy_created_windows()`（显式销毁窗口并 `sendPostedEvents(None, QEvent.DeferredDelete)` 泵到落地），正是验证 `tab_manager_window.py` 回收改动的配套设施。拆开则「回收改动」无护栏。

**测试知识点（写进 commit message 值得）**：`qtbot.addWidget` 只调 `deleteLater`，事件只是投递；`processEvents()` **不消费** `DeferredDelete`，必须 `sendPostedEvents(None, QEvent.DeferredDelete)`。这与 `main_widget.py` pump 段（28 行）是同一个 Qt 机制的两处应用。

**为什么与 Commit 2 分开**：Commit 2 是「逻辑错误导致功能失效」，本条是「对象生命周期未覆盖」，验证方式不同（本条要测多轮后计数是否归零）。

### `main_widget.py` 拆分识别

```
@@ -14859,0 +14860,28 @@   ← [PERF] next-tick 消费 DeferredDelete（28 行）→ 归 Commit 4
@@ -27307,0 +27336,7  @@   ← [泄漏修复 M5] _tool_card_map 持卡未释放（7 行）→ 归本条
```

两个 hunk 相距 12,476 行，`git add -p` 不会误选。**M5 hunk 特征**：含 `_tool_card_map` 与「泄漏修复（M5）」注释。

---

## Commit 4　性能优化

```
perf: 设置页懒建 + 主题探测去 fork + 日志 formatter 记忆化 + 编译产物缓存 + renderer 即时回收

文件：
  M app/widgets/cards/settings/llm_settings_card.py   (+303/-105，仅懒建段，git add -p)
  M app/utils/system_theme.py                         (+63/-12)
  M app/utils/theme_mode.py                           (+12/-2)
  M app/core/infra/logging_setup.py                   (+34/-0)
  M app/plugins/loaders/plugin_tool_loader.py          (+9/-1)
  A app/plugins/loaders/_code_cache.py                 (+10067 bytes)
  M app/main_widget.py                                 (仅 +28 的 pump hunk，git add -p)
```

**性质**：性能优化

**实测收益**（全部附信噪比判断，比值 < 2 不写进交付）：

| 项 | 数字 | 判断 |
|---|---|---|
| 设置页首开 | 1260ms → 41ms | 差 30 倍，**可下结论** |
| mcp/lsp 预建 | 144/147ms → 10/5ms | 差 14 倍，**可下结论** |
| 主题探测 | macOS 每次 fork 8~15ms × 3 调用点 → 进程内 CFPreferences | 移除子进程，方向确定 |
| 日志 formatter | loguru 11 sink × 2.6ms ≈ 28ms → 记忆化后近零 | 可下结论 |
| code cache | compile 35~38ms（19 模块，120ns/byte）→ 跨进程命中 | 可下结论 |
| renderer 回收 | 切会话 RSS 峰值 1671.8MB → 1136.6MB（renderer 11 → 1） | 计数型，**可下结论** |
| 首帧 | -104ms(n=2) / -54ms(n=3) | **不可定**，信噪比 1.5 / 0.8，**不写进交付** |

> **数字证据强度（写 commit message 前必读，三分类）**
>
> **① 有源码/文档锚点，可直接引用**
> - renderer 回收 `1671.8MB → 1136.6MB（renderer 11 → 1）` —— 锚点 `app/main_widget.py:14869` 附近注释原文：「…避免『待销毁但未销毁』窗口内的 RSS 峰值叠加（实测 1136.6MB → 1671.8MB）」
> - 首帧四轮数据 —— build@win_797 正在写进 `PERF_FALSIFIED.md`，落档后即成锚点，届时可引用
>
> **② 无锚点，不得写进 commit message**（源码与文档均零命中，仅见于本文件转述）
> - 设置页 `1260ms → 41ms` —— `1260` 全仓 8 处命中全是 `mermaid.min.js` 压缩码
> - mcp/lsp `144/147ms → 10/5ms` —— 源码 `llm_settings_card.py:2308` 写的是 **137/141**；`→10/5` 只出现在本文件第 145 行
> - AST `103ms → 60ms`、信噪比 6.1 —— `_ast_guard.py` 零命中，只在本文件第 52 行
> - `174ms + 113MB` —— `session_repository.py` 零命中，只在本文件第 77 行
>
> **③ 已判死，任何场合都不得引用**
> - `tools/perf/results/baseline_before_selfcheck.md` 的全部数值（自比对 Δ 恒 0，数据本身失效）
> - `751MB renderer`（build@win_793 标注「来源不明不可引用」，全仓无原始 `report.json`）

### `llm_settings_card.py` 拆分识别

- **懒建段（本条）**：`_LAZY_PAGE_BUILDERS`、`_ensure_page_ready`、`_build_lsp_page`、`_build_plugin_components_page`、`_ensure_plugins_page`、`_register_list_card`、`_nav_order`（页序表，`_prefetch_pages` 的依赖，**必须留在本条**，见 §9.6）、`_prefetch_*`
- **nav QSS 段（归 Commit 6）**：`_nav_qss_dirty`、父容器 `setStyleSheet`、`objectName + active` 属性选择器

### 依赖要点

`_code_cache.py` 与 `plugin_tool_loader.py` 必须同 commit：后者 `:215` 有 `from app.plugins.loaders import _code_cache`，拆开则中间态 ImportError。

---

## Commit 5　测试基建：预算门与护栏

```
test(perf): 启动/内存预算门 + 插件加载冒烟 + code cache 安全护栏 + storage 测试修正

文件：
  A tests/perf/test_startup_budget.py                    (+8948 B)
  A tests/perf/test_memory_budget.py                     (+10743 B)
  A tests/plugins/test_plugin_load_smoke.py              (+12603 B)
  A tests/plugins/test_code_cache_safety.py              (+16051 B)
  M pyproject.toml                                       (+13/-3)
  M tests/perf/README.md                                 (+122/-0)
  M tests/plugins/test_storage_engine.py                 (+14/-6)
  M tests/plugins/test_storage_consumer_migration.py     (+8/-2)
  M tests/plugins/test_ui_storage_migration.py           (+8/-2)
```

**性质**：测试基建

**实测收益**：无性能收益（防「改慢无人报警」）

**为什么独立**：这些不改变产品行为，但改变「什么算失败」。单独 commit 让「某天突然多了一堆红」能一眼归因到护栏上线，而不是误判成回归。

### ⚠️ 执行前置确认（必须做）

```bash
git diff --numstat tests/plugins/test_storage_engine.py \
                    tests/plugins/test_storage_consumer_migration.py \
                    tests/plugins/test_ui_storage_migration.py
```
**必须三行都有输出**。若只有 `test_storage_engine.py`，说明 #73 未跑完，本条暂缓。这是本计划唯一需要「先确认后执行」的 commit。

### `pyproject.toml` 改动性质

`addopts` 从「整体排除 `tests/perf`」改为「逐个排除已知不可用文件」——目的是让预算门（`test_startup_budget` / `test_memory_budget`）进入默认收集。否则「改慢无人报警」的护栏等于不存在。

### storage 测试修正的真实原因（写进 commit message）

```
test_storage_engine.py 原用 db_dir=":memory:" —— ":memory:" 不是 SQLite 内存模式，
SessionStore 把它当目录名拼路径，在 cwd 下真的建出 <cwd>/:memory:/sessions.db
（sqlite3 PRAGMA database_list 返回绝对路径，SQLite 自己证明的）。
已改用 tmp_path；同时补 SessionStore._instance 单例重置
（否则本用例 db_dir 被前一个实例静默忽略）。
产品侧守卫（startswith(":") 校验）不在本轮，见 NEXT_ROUND_PRIORITIES.md D1 节。
```

**为什么本轮只修测试侧**：`:memory:` 库不支持 WAL，多线程各自拿到独立库，与本项目「多线程 + WAL」假设冲突；而这 5 处测试测的是存储迁移逻辑，用内存库测迁移等于用错误介质验证正确逻辑。所以本轮把测试改成真实临时目录（治标），产品侧加守卫防将来再犯（治本），下一轮做。

---

## Commit 6　代码组织 + 测量工具与文档

```
docs(perf): 测量规程/证伪清单/优先项；nav QSS 集中定义；scene_layer 注释

文件：
  A tools/perf/benchmark_core.py                          (测量工具)
  A tools/perf/measure_gate.py                            (测量门禁)
  A tools/perf/MEASUREMENT_PROTOCOL.md                    (测量规程)
  A tools/perf/PERF_FALSIFIED.md                          (证伪清单)
  A tools/perf/NEXT_ROUND_PRIORITIES.md                   (下一轮优先项)
  M app/widgets/cards/settings/llm_settings_card.py       (仅 nav QSS 段，见附录 B.2 方案 C)
  M app/widgets/scene_layer.py                            (+18/-0，纯注释)
```

**性质**：代码组织 + 工具与文档

**实测收益**：**无性能收益**。nav QSS 改完反而略慢（批量后），收益是「QSS 只有一处定义、不必逐 13 个控件同步」。

### nav QSS 提交信息写法（刻意不写毫秒）

```
代码组织：nav 按钮 QSS 集中到父容器（单一真源，选中态只改两个按钮）。
注：此为可维护性改动，无性能收益（实测批量后略慢 <1ms）。
```

**为什么不写具体毫秒**：这段的真实结论是「改完反而慢」，写任何数字都会引出「那为什么要改」的追问。坦诚写「无收益 + 略慢 <1ms」反而提前回答了这个问题——因为 QSS 原来散在 13 个按钮里。

> 早期记录的「13 个按钮逐个 setStyleSheet 实测 24.8ms」已被证伪。真实值是逐个 1.85ms 量级、批量后略慢。这一条本身是 `PERF_FALSIFIED.md` 里「用自造探针代替真实调用链」的同类案例。

### `scene_layer.py` 说明

**+18/-0 全部是注释**，记录两件事：
- `destroyed.connect` 归还 `AnimationRegistry` 引用已证伪并移除（A/B 两版 6 次验证，槽体内任何 C-API 调用 SIGSEGV）
- 已知缺口与修法（未实施）：`has_subscribers()` 缺 `sip.isdeleted` 判据，Qt 直接析构时残留 1 个 AnimationSource + 1 个预解码线程

这是防止后人重踩的高价值注释，但**不改变一行逻辑**，放进「无行为变更」的 commit 才诚实。

---

## 三、12 个未跟踪文件处置：全部保留

| 文件 | 归属 | 性质 |
|---|---|---|
| `app/plugins/loaders/_code_cache.py` | **4** | 产品代码（编译产物缓存） |
| `tests/perf/test_startup_budget.py` | **5** | 护栏 |
| `tests/perf/test_memory_budget.py` | **5** | 护栏 |
| `tests/plugins/test_plugin_load_smoke.py` | **5** | 护栏 |
| `tests/plugins/test_code_cache_safety.py` | **5** | 护栏 |
| `tests/plugins/test_ast_guard_cache_safety.py` | **1** | 安全回归 |
| `tools/perf/benchmark_core.py` | **6** | 测量工具 |
| `tools/perf/measure_gate.py` | **6** | 测量门禁 |
| `tools/perf/MEASUREMENT_PROTOCOL.md` | **6** | 规程 |
| `tools/perf/PERF_FALSIFIED.md` | **6** | 证伪清单 |
| `tools/perf/NEXT_ROUND_PRIORITIES.md` | **6** | 下一轮优先项 |

**一条都不排除。** 未跟踪文件在提交时最容易被忘，尤其多人先后改过同一个文件时（`MEASUREMENT_PROTOCOL.md` 是 win_790 建、win_798 与 win_797 追加）。

**执行后自检**：`git status --porcelain` 应只剩空或与本表无关的新增项；若仍有本表任一文件以 `??` 出现，说明漏提交。

---

## 四、覆盖完整性自检

| Commit | 文件数 | 明细 |
|---|---|---|
| 1 安全修复 | 2 | `_ast_guard.py`、`test_ast_guard_cache_safety.py` |
| 2 静默失败 | 4 | `session_repository.py`、`memory_governor.py`、`history_manager.py`、`theme_style_card.py` |
| 3 泄漏修复 | 4 | `tab_manager_window.py`、`message_card.py`、`main_widget.py`(M5)、`test_tab_manager_window.py` |
| 4 性能优化 | 7 | `llm_settings_card.py`(懒建)、`system_theme.py`、`theme_mode.py`、`logging_setup.py`、`plugin_tool_loader.py`、`_code_cache.py`、`main_widget.py`(pump) |
| 5 测试基建 | 9 | 4 个新测试 + `pyproject.toml` + `tests/perf/README.md` + 3 个 storage 测试 |
| 6 文档+组织 | 7 | 5 个 `tools/perf/*` + `llm_settings_card.py`(nav QSS) + `scene_layer.py` |

**2+4+4+7+9+7 = 33 项，对应 32 个文件**（`llm_settings_card.py` 与 `main_widget.py` 各被拆进两个 commit）。**32 个文件全部覆盖，无遗漏。**

> 32 = 20 个已修改 + 11 个未跟踪 + 本文件（`COMMIT_PLAN.md`）自身。
> 本文件自身不需要写进任何 commit 的「文件」列表，但它会随 Commit 6 一起落盘（同属 `tools/perf/`）。

## 五、四个「无性能收益」的 commit

| Commit | 提交信息必须体现 |
|---|---|
| 2 静默失败 | 修的是「一直没生效」，不是「变快」 |
| 3 泄漏修复 | 防累积，非提速 |
| 5 测试基建 | 不改产品行为 |
| 6 文档+组织 | nav QSS 实测反而略慢 |

## 六、三处提交信息必须避开「性能」字样

1. `llm_settings_card.py` nav QSS → 写「代码组织：QSS 集中定义」，**不写毫秒数字**
2. `scene_layer.py` → 写「注释：记录已证伪方案与已知缺口」，**不写优化**
3. `theme_style_card.py` → 这是**缓存上限**（泄漏修复），不是「缓存优化提升命中率」

## 七、执行风险提示

| 风险 | 说明 | 规避 |
|---|---|---|
| **两个文件需 add -p** | `main_widget.py`（两 hunk 相距 12,476 行）、`llm_settings_card.py`（懒建 vs nav QSS） | 按本文给出的识别特征逐 hunk 判断；误用 `git add <file>` 会把两种性质混进同一 commit |
| **Commit 5 前置** | 三个 storage 测试必须都已改动 | 执行前跑 `git diff --numstat` 确认三行都有输出 |
| **未跟踪文件遗漏** | 12 个文件容易只 add 了一部分 | 执行前跑 `tools/perf/check_plan_coverage.py --strict-blocks`，退出码 0 才算清单完整 |
| **Commit 4 拆包** | `_code_cache.py` 与 `plugin_tool_loader.py` 拆开 → ImportError | 两者必须同一 commit |

## 八、本文来源与时效

- 依据：`git diff --numstat`（20 个已修改）+ `git status --porcelain`（12 个未跟踪，含本文件），2026-11-27 12:13 快照
- 各 commit 收益数字来源：`tools/perf/PERF_FALSIFIED.md`、`MEASUREMENT_PROTOCOL.md`、各成员回报及本仓 `tools/perf/results/` 下的 report
- **数字若与本文件不符，以 report 原始数据为准**，不以转述为准

### 覆盖核对脚本（执行前必跑）

```bash
.venv/bin/python tools/perf/check_plan_coverage.py                   # 宽松：正文提及即算覆盖
.venv/bin/python tools/perf/check_plan_coverage.py --strict-blocks   # 严格：只认 commit 代码块
```

退出码：`0` = 覆盖完整；`1` = 有改动文件不在计划里；`2` = 计划提及了不存在的文件。

**为什么要有这个脚本**：本文件的 32 个文件清单是**人工清点**的，第一版漏了
`tests/widgets/test_tab_manager_window.py`（+58/-1，是 `tab_manager_window.py` 回收改动的
配套护栏），靠脚本按 `git diff --numstat` 逐个匹配才抓到。同一天内团队有三处
「人工处理列表」出错（转述串成员 / 命令重复计数 / 清单漏项），机器核对一次就对。

**两种模式的区别（实测验证过）**：把某文件从 commit 代码块里删掉、但后文仍提及时，
宽松模式 `exit=0` 只给 `[提示]`，严格模式 `exit=1` 判失败。
**执行前用 `--strict-blocks`**——它能防「文件被移出清单但仍被提及」这种漏 add。
---
## 九、执行补遗：hunk 级切分精确表（2026-11-27 12:30 实测）

> 本节补正文未覆盖的「怎么切」层面。全部结论在 `HEAD + 主树两文件` 的独立 worktree 里实测得出。
> **正文 §七 若与本节冲突，以本节为准**（§七 的 `_nav_order` 归属已按本节修正）。

### 9.1 一句话结论

| 文件 | 切分方式 | 原因 |
|---|---|---|
| `main_widget.py` | `printf 'n\ny\n' \| git add -p` | 2 个纯新增 hunk，相距 12,476 行，交互式最简单 |
| `llm_settings_card.py` | `-U0` patch 切片 + `git apply --cached --unidiff-zero` | 33 个 hunk，C4/C6 归属**交替分布**，不能按连续区间切 |

### 9.2 `llm_settings_card.py` 的 33 个 U0 hunk 归属

**C6 只占 6 个：15, 19, 23, 24, 25, 26；其余 27 个归 C4。**

| 组 | hunk 编号 | +A | -D |
|---|---|---|---|
| **C6** | 15, 19, 23, 24, 25, 26 | **+56** | **-8** |
| **C4** | 1-14, 16-18, 20-22, 27-33（27 个） | **+247** | **-97** |
| 合计 | 33 | **+303** | **-105** |

与 `git diff --numstat` 的 `303 105` 完全一致；C6 的 `+56/-8` 与 build@win_793 独立计算一致（双方交叉验证通过）。

C6 六个 hunk 逐条：

```
hunk 15  + 4 - 1   @@ -1275 +1314,4 @@      _build_side_nav：btn.setObjectName/setProperty
hunk 19  + 2 - 1   @@ -1512 +1562,2 @@      _sync_plugin_tabs：同两行
hunk 23  +36 - 6   @@ -1612,6 +1670,36 @@  _update_nav_styles 重写 + _nav_frame_style 主体
hunk 24  + 9 - 0   @@ -1619,0 +1708,9 @@    _nav_frame_style 的 QSS 组合体
hunk 25  + 2 - 0   @@ -1624,0 +1722,2 @@    _nav_frame_style 收尾
hunk 26  + 3 - 0   @@ -1947,0 +2047,3 @@    _on_config_changed：_nav_qss_dirty 置脏
```

**符号归属（用于核对，不是猜测）**：

| 符号 | 出现的 hunk | 归 |
|---|---|---|
| `settingsNavBtn` | 15, 19, 24 | C6 |
| `_nav_qss_dirty` | 23, 26 | C6 |
| `_nav_order` | 14, 16, 18, 20, 33 | **C4** |

**关键陷阱**：`15/19/24` 夹在 `14/16/18/20` 之间。按「前 22 个给 C4、后 11 个给 C6」这类区间切法，必然把 `setObjectName` 带进 C4，C4 的 `grep settingsNavBtn 期望 0` 立刻失败；反之若把 `_nav_order` 切给 C6，C4 提交后相邻页预热会**静默失效**（见 §9.6）。

### 9.2.1　`#21`（设置页懒建）与 `#41`（重 idle 预 import）同属 C4，不拆

**结论**：`llm_settings_card.py` 在六个 commit 里**只跨 C4 与 C6**，不存在第三个归属点。

| 归属 | 段 | 内容 |
|---|---|---|
| **C4** | U0 hunk **28** | `_heavy_prewarm_started` 类属性 + `_prewarm_heavy_modules()` 方法（原 `:2314` / `:2316-2346`） |
| **C4** | U0 hunk **33** | `_prefetch_pages` 里的 `[PERF #41]` 注释 + 调用（原 `:2376-2380`） |
| **C6** | hunk 15/19/23/24/25/26 | nav QSS（`settingsNavBtn` / `_nav_qss_dirty` / `_nav_frame_style`） |

依据：我按 hunk 编号实测——含 `_prewarm_heavy_modules` 的只有 hunk 28，含其调用点的只有 hunk 33，两者都在 C4 的 27 个 hunk 内（`1-14,16-18,20-22,27-33`）。因此**不需要为 #41 单独开 commit，也不要试图把它切到 C6**。

### 9.3 `main_widget.py` 两个 hunk

hunk 按文件内行号升序消费，顺序固定：

```
@@ -14859,0 +14860,28 @@   [PERF] next-tick 消费 DeferredDelete（pump）→ C4
@@ -27307,0 +27336,7  @@   [泄漏修复 M5] _tool_card_map 持卡未释放   → C3
```

C3 取 M5 用 `printf 'n\ny\n'`；C4 时 `main_widget.py` 只剩 pump，整文件 `git add` 即可。

### 9.4 `--unidiff-zero` 的条件性（修正一个直觉）

实测：**不是无条件必需**。

| hunk 类型 | 普通 `git apply --cached` | `--unidiff-zero` |
|---|---|---|
| 纯新增（`@@ -a,0 +b,N @@`，old count = 0） | **rc=0 成功** | rc=0 成功 |
| 含删除行（old count > 0） | **rc=1**：`error: patch failed: <file>:<line>` / `patch does not apply` | **rc=0 成功** |

所以：
- `main_widget.py` 两个 hunk 都是 `+N/-0` → 普通 apply 也能过（但本手册走 `add -p`，不涉及）
- `llm_settings_card.py` 33 个 hunk 绝大多数含删除行 → **必须 `--unidiff-zero`**

### 9.5 失败模式对照

| 方法 | 典型失败 | 是否静默 | 现象 |
|---|---|---|---|
| `printf` 管道 `git add -p` | 文件 hunk 数变化（别人又改了）→ `y/n` 序列消费到**错误的 hunk** | **会静默** | 命令 `rc=0`、无报错，只是暂存了不该暂存的东西；**只有内容 grep 能抓到** |
| `git apply --cached` 忘 `--unidiff-zero` | 含删除行时被拒 | 不会 | `rc=1` + `patch does not apply`；**index 不变，可修正重试** |
| `git apply --cached --unidiff-zero` 但 hunk 编号错 | 该进的没进 / 不该进的进了 | 会静默 | `rc=0`，同双查抓 |
| **`-U0` 子集 apply（#113 实测）** | **落点漂移，产出语法非法文件**（`IndentationError` / `SyntaxError`） | **会静默** | `rc=0`，`git diff --cached` 内容 grep **全过**也发现不了；**必须加 `python -c "import ast; ast.parse(open(f).read())"` 才能抓到** |
| `--include/--exclude` | 只能按**文件**过滤，无法按 hunk | — | 不适用于本任务 |

### 9.6 为什么 `_nav_order` 必须归 C4：没有测试兜底

**事实（build@win_792 的 #107 发现）**：**没有任何测试覆盖 `llm_settings_card` 的 prefetch 逻辑与 `_nav_order`。**

这意味着如果 hunk 14/16/18/20/33 被误切到 C6：
- C4 提交后 `_prefetch_pages` 读 `self.__dict__.get("_nav_order")` 得到 `None`
- `cur not in order` → 立即 `return`
- **相邻页预热带原地失效，无异常、无日志、无测试变红**

这是「`_nav_order` 必须跟 C4 走」的第二重理由（第一重是它是 `_prefetch_pages` 的直接依赖）。**唯一可靠的发现手段是提交后人工核对 §9.2 的符号表，不能指望 CI。**

### 9.7 C1→C6 每步的暂存命令（可直接照抄）

```bash
# ⚠ 本块是概览：C3/C4 对 llm_settings_card.py 的 hunk 级切分见附录 B，必须合起来执行
cd /Users/dingma/work/DriFox/DriFox
PY=/Users/dingma/work/DriFox/DriFox/.venv/bin/python

# ── C1 安全修复
git add app/plugins/loaders/_ast_guard.py tests/plugins/test_ast_guard_cache_safety.py

# ── C2 静默失败
git add app/core/store/session_repository.py app/core/infra/memory_governor.py \
        app/utils/history_manager.py app/widgets/cards/settings/theme_style_card.py

# ── C3 泄漏修复（main_widget 只取 M5）
git add app/widgets/tab_manager_window.py app/widgets/message_card.py \
        tests/widgets/test_tab_manager_window.py
printf 'n\ny\n' | git add -p -- app/main_widget.py
git diff --cached -- app/main_widget.py | grep -c "_tool_card_map"   # ≥1
git diff --cached -- app/main_widget.py | grep -c "DeferredDelete"   # ==0

# ── C4 性能优化（llm_settings_card 只取 27 hunk；main_widget 整文件）
git add app/utils/system_theme.py app/utils/theme_mode.py \
        app/core/infra/logging_setup.py app/plugins/loaders/plugin_tool_loader.py \
        app/plugins/loaders/_code_cache.py app/main_widget.py
# llm_settings_card：只暂存 C4 的 27 个 hunk（方案 C，四步，详见附录 B.2）
git show HEAD:app/widgets/cards/settings/llm_settings_card.py > /tmp/head_llm.py
git diff -U0 -- app/widgets/cards/settings/llm_settings_card.py > /tmp/llm_all.patch
.venv/bin/python /tmp/apply_u0.py /tmp/head_llm.py /tmp/llm_all.patch /tmp/c4_only.py "1-14,16-18,20-22,27-33"
SHA=$(git hash-object -w /tmp/c4_only.py)
git update-index --cacheinfo 100644,$SHA,app/widgets/cards/settings/llm_settings_card.py
git diff --cached -- app/widgets/cards/settings/llm_settings_card.py | grep -c "settingsNavBtn"   # 必须 0

# ── C5 测试基建
git add pyproject.toml tests/perf/README.md \
        tests/plugins/test_storage_engine.py tests/plugins/test_ui_storage_migration.py \
        tests/plugins/test_storage_consumer_migration.py \
        tests/perf/test_startup_budget.py tests/perf/test_memory_budget.py \
        tests/plugins/test_code_cache_safety.py tests/plugins/test_plugin_load_smoke.py

# ── C6 文档+组织（llm_settings_card 剩余全部）
git add app/widgets/cards/settings/llm_settings_card.py app/widgets/scene_layer.py \
        tools/perf/COMMIT_PLAN.md tools/perf/check_plan_coverage.py \
        tools/perf/MEASUREMENT_PROTOCOL.md tools/perf/PERF_FALSIFIED.md \
        tools/perf/NEXT_ROUND_PRIORITIES.md tools/perf/benchmark_core.py tools/perf/measure_gate.py
```

### 9.8　18 个「整文件 add」文件清单

除 `main_widget.py` 与 `llm_settings_card.py` 需要 hunk 级切分（§9.3 / §9.2.1）外，
**其余 18 个已修改文件都可整文件 `git add`**——即使有多 hunk，也都属同一个 commit，不存在跨 commit 混装。

| 文件 | U3 hunk 数 | 归属 | 性质 / 单一性 |
|---|---|---|---|
| `app/core/infra/logging_setup.py` | 2 | **C4** | formatter 记忆化 |
| `app/core/infra/memory_governor.py` | 1 | **C2** | cache_clear 调错对象 |
| `app/core/store/session_repository.py` | 7 | **C2** | CAS 原始 blob + rowcount 告警 |
| `app/plugins/loaders/_ast_guard.py` | 4 | **C1** | 缓存协议 + 哈希键 |
| `app/plugins/loaders/plugin_tool_loader.py` | 1 | **C4** | 接 _code_cache |
| `app/utils/history_manager.py` | 3 | **C2** | 512 上限 |
| `app/utils/system_theme.py` | 2 | **C4** | 主题探测去 fork |
| `app/utils/theme_mode.py` | 1 | **C4** | 同上 |
| `app/widgets/cards/settings/theme_style_card.py` | 3 | **C2** | 24 条上限 |
| `app/widgets/message_card.py` | 1 | **C3** | cleanup 漏清 |
| `app/widgets/scene_layer.py` | 1 | **C6** | 纯注释 |
| `app/widgets/tab_manager_window.py` | 1 | **C3** | prune 移 finally |
| `pyproject.toml` | 1 | **C5** | addopts 放开 tests/perf |
| `tests/perf/README.md` | 1 | **C5** | 护栏说明 |
| `tests/plugins/test_storage_consumer_migration.py` | 1 | **C5** | tmp_path 隔离 |
| `tests/plugins/test_storage_engine.py` | 4 | **C5** | 同上 |
| `tests/plugins/test_ui_storage_migration.py` | 1 | **C5** | 同上 |
| `tests/widgets/test_tab_manager_window.py` | 2 | **C3** | fixture 显式销毁 |

> **判据**：上表 18 个文件 + 2 个需拆分文件 = 20 个已修改文件，全部有归属，无遗漏。
> 13 个未跟踪文件（`A`）天然整文件 add，无需 hunk 判断。
> 每步 add 完仍要跑附录 A 对应节的 `git status` 计数与 `git diff --cached \| grep -c` 双查。

---
## 附录 A：提交后逐步验证清单

> 每一节对应一个 commit。**提交完立刻跑本节命令**，五项全对再进下一个。全部只读。

统一约定（后文省略）：
```bash
cwd = /Users/dingma/work/DriFox/DriFox
PY  = .venv/bin/python
```

全程预期噪声（C1~C6 都存在，**不是本批引入**）：
```
pytest --collect-only 恒有 1 error = tests/widgets/test_identity_header.py（缺 PIL）
```

对账总表：

| 步骤 | `git status --porcelain=v1 -uall \| wc -l` | 本步清零的文件 |
|---|---|---|
| 提交前 | **33** | — |
| C1 后 | **31** | `_ast_guard.py`、`test_ast_guard_cache_safety.py` |
| C2 后 | **27** | `session_repository.py`、`memory_governor.py`、`history_manager.py`、`theme_style_card.py` |
| C3 后 | **24** | `tab_manager_window.py`、`message_card.py`、`test_tab_manager_window.py` |
| C4 后 | **18** | `logging_setup.py`、`system_theme.py`、`theme_mode.py`、`plugin_tool_loader.py`、`_code_cache.py`、`main_widget.py` |
| C5 后 | **9** | `pyproject.toml`、`tests/perf/README.md`、3 个 storage 测试、4 个新测试 |
| C6 后 | **0** | `llm_settings_card.py`、`scene_layer.py`、7 个 `tools/perf/*` |

> **注 1（C4 的 18 怎么来的）**：`llm_settings_card.py` 在本步**只提交 27 个 hunk，文件仍留 `M` 给 C6**，
> 故本步实际清零 **6** 项而不是 §四 声明的 7 项：`24 − 6 = 18`。
> 若按 §四 的「7 个」直接减会得到 17，那是把「commit 语义的文件数」和「`git status` 的清零项数」混用了。
>
> **注 2（C6 的 0 怎么来的）**：本步多清 **2 个计划自身/配套文件**（`COMMIT_PLAN.md`、`check_plan_coverage.py`，
> 它们未列入正文件清单但同样随本步落盘），故本步清零 **9** 项而不是 §四 声明的 7 项：`9 − 9 = 0`。
> 若按 §四 的「7 个」直接减会得到 2，那会误判成漏 add。
>
> **口径声明**：本表数值（`33 → 31 → 27 → 24 → 18 → 9 → 0`）为 **`git status --porcelain=v1 -uall | wc -l` 实测行数**，
> 以**本表绝对值为准**；§四 的「各 commit 文件数」（2/4/4/7/9/7）是 **commit 语义计数**（按文件归属，不按清零时刻）。
> **两个口径不混用**：提交时逐步对账只认本表。

### C1　安全修复：AST 门禁进程内缓存

**① 剩余项数 = 31（19 M + 12 ??）**
```bash
git status --porcelain=v1 -uall | wc -l                                             # 31
git status --porcelain=v1 -uall | grep -E "_ast_guard|test_ast_guard_cache_safety"   # 空
```

**② 清零文件**：`app/plugins/loaders/_ast_guard.py`、`tests/plugins/test_ast_guard_cache_safety.py`

**③ import 冒烟**
```bash
.venv/bin/python -c "from app.plugins.loaders import _ast_guard as g; print('exports_ok', all(callable(getattr(g,n,None)) for n in ('guard_plugin_module','guard_plugin_module_once','audit_dangerous_imports','audit_blocking_calls','flush_disk_cache')))"
# 期望 exports_ok True
```

**④ 关键符号**
```bash
.venv/bin/python -c "print('no_code_cache_dep', '_code_cache' not in open('app/plugins/loaders/_ast_guard.py').read())"
# 期望 True（C1 不得依赖 C4 才有的模块）
.venv/bin/python -c "import app.plugins.registries.ui_plugin_registry, app.plugins.loaders.provider_loader, app.plugins.loaders.runtime_component_loader, app.plugins.loaders.plugin_tool_loader; print('callers_ok')"
grep -c "_SCHEMA_VERSION\|flush_disk_cache" app/plugins/loaders/_ast_guard.py        # ≥ 2
```

**⑤ 预期噪声**：无。该测试文件 7 条用例应全绿。

### C2　静默失败：四处不报错但失效

**① 剩余项数 = 27（15 M + 12 ??）**
```bash
git status --porcelain=v1 -uall | wc -l                                                      # 27
git status --porcelain=v1 -uall | grep -E "session_repository|memory_governor|history_manager|theme_style_card"  # 空
```

**② 清零文件**：`session_repository.py`、`memory_governor.py`、`history_manager.py`、`theme_style_card.py`

**③ import 冒烟**
```bash
.venv/bin/python -c "import app.core.store.session_repository, app.core.infra.memory_governor, app.utils.history_manager; print('c2_import_ok')"
```

**④ 关键符号**（四处修复各一条特征）
```bash
grep -c "rowcount" app/core/store/session_repository.py                   # ≥ 1
grep -c "cache_clear" app/core/infra/memory_governor.py                   # ≥ 1（且不能调在 estimate_tokens 上）
grep -cE "_MAX|上限|FIFO" app/utils/history_manager.py                    # ≥ 1（512 条上限）
grep -cE "_MAX|24" app/widgets/cards/settings/theme_style_card.py         # ≥ 1（24 条上限）
```

**⑤ 预期噪声**：无。

### C3　泄漏修复：动画源回收与 cleanup 遗漏

**① 剩余项数 = 24（12 M + 12 ??）**
```bash
git status --porcelain=v1 -uall | wc -l                                              # 24
git status --porcelain=v1 -uall | grep -E "tab_manager_window|message_card"          # 空
git status --porcelain=v1 -uall | grep main_widget                                   # 仍为 " M "（pump 留给 C4）
```

**② 清零文件**：`tab_manager_window.py`、`message_card.py`、`test_tab_manager_window.py`

**③ import 冒烟**
```bash
.venv/bin/python -c "import app.widgets.message_card, app.widgets.tab_manager_window; print('c3_import_ok')"
```

**④ 关键符号**
```bash
grep -c "_tool_card_map" app/main_widget.py                        # ≥ 1（M5 释放）
grep -c "DeferredDelete" app/main_widget.py                        # ≥ 1（pump 仍在工作树）
grep -cE "_CREATED_WINDOWS|sendPostedEvents\(None, QEvent\.DeferredDelete\)" tests/widgets/test_tab_manager_window.py   # ≥ 2
```

**⑤ 预期噪声**：`test_tab_manager_window.py` 1 条存量红
`TestSidebarAutoExpandAfterSqueeze::test_button_collapse_then_window_widen_expands`（HEAD 同样红）。

### C4　性能优化（含两个 `add -p`，本步风险最高）

**① 剩余项数 = 18（7 M + 11 ??）**
```bash
git status --porcelain=v1 -uall | wc -l                                 # 18
git status --porcelain=v1 -uall | grep main_widget                      # 空（两 hunk 都已提）
git status --porcelain=v1 -uall | grep llm_settings_card                # 仍为 " M "（只剩 nav QSS 段）
```

**② 清零文件**：`logging_setup.py`、`system_theme.py`、`theme_mode.py`、`plugin_tool_loader.py`、`_code_cache.py`、`main_widget.py`

**③ import 冒烟**
```bash
.venv/bin/python -c "from app.plugins.loaders import _code_cache as c; print('code_cache_ok', callable(getattr(c,'get_code',None)), callable(getattr(c,'schedule_flush',None)))"
# 期望 code_cache_ok True True
.venv/bin/python -c "import app.core.infra.logging_setup, app.utils.system_theme, app.utils.theme_mode; print('c4_import_ok')"
```

**④ 关键符号**
```bash
grep -c "_code_cache" app/plugins/loaders/plugin_tool_loader.py                              # ≥ 1（同 commit）
grep -cE "_LAZY_PAGE_BUILDERS|_schedule_idle_prefetch" app/widgets/cards/settings/llm_settings_card.py   # ≥ 2

# ★ 本步唯一「拆分错就静默混提交」的检查点：nav 段属 C6，此处必须为 0
grep -c "settingsNavBtn" app/widgets/cards/settings/llm_settings_card.py                     # 必须 == 0
```

**⑤ 预期噪声**：无。本步不测性能，数字口径以 `PERF_FALSIFIED.md` 为准。

### C5　测试基建

**① 剩余项数 = 9（2 M + 7 ??）**
```bash
git status --porcelain=v1 -uall | wc -l     # 9
git status --porcelain=v1 -uall             # 应恰好只剩 9 行：
#  M app/widgets/cards/settings/llm_settings_card.py
#  M app/widgets/scene_layer.py
# ?? tools/perf/{COMMIT_PLAN,MEASUREMENT_PROTOCOL,NEXT_ROUND_PRIORITIES,PERF_FALSIFIED}.md
# ?? tools/perf/{benchmark_core,check_plan_coverage,measure_gate}.py
```

**② 清零文件**：`pyproject.toml`、`tests/perf/README.md`、3 个 storage 测试、4 个新测试

**③ import/收集冒烟**（约 6s）
```bash
.venv/bin/python -m pytest --collect-only -q 2>&1 | tail -2
# 期望 11010 tests collected, 1 error in ~6s
```

**④ 关键符号：护栏必须真的在收集里**
```bash
.venv/bin/python -m pytest --collect-only -q 2>/dev/null | grep -cE "test_startup_budget|test_memory_budget"
# ≥ 2（若被 --ignore 掉，预算护栏等于不存在）
.venv/bin/python -m pytest --collect-only -q 2>/dev/null | grep -cE "test_ui_memory_baseline|test_recycle_efficacy|test_stream_stage_bench"
# == 0（5 类已知不可用文件仍须排除）
```

**⑤ 预期噪声**：两条
- collection 的 `1 error` = `test_identity_header.py` 缺 PIL，全程存在，不算失败。
- 裸跑 `pytest` 会带上 `test_startup_budget` / `test_memory_budget` 两个**性能预算门**：这两门**故意**对「改慢」报红，也会受环境波动影响，属设计意图。**判回归请用「与 HEAD 逐文件对照」，不要用裸跑红绿。**

### C6　文档 + 组织（nav QSS 与 scene_layer 注释）

**① 剩余项数 = 0**
```bash
git status --porcelain=v1 -uall | wc -l     # 0
```

**② 清零文件**：`llm_settings_card.py`、`scene_layer.py`、7 个 `tools/perf/*`（含 `COMMIT_PLAN.md` 与 `check_plan_coverage.py`，二者未列入正文件清单，随本步同目录 `git add`）

**③ import 冒烟**
```bash
.venv/bin/python -c "import app.widgets.scene_layer; print('c6_import_ok')"
```

**④ 关键符号**
```bash
grep -c "settingsNavBtn" app/widgets/cards/settings/llm_settings_card.py        # ≥ 2
grep -c "_nav_qss_dirty" app/widgets/cards/settings/llm_settings_card.py        # ≥ 2
grep -c "setStyleSheet(self._nav_btn_style" app/widgets/cards/settings/llm_settings_card.py   # == 0
grep -cE "已证伪|sip\.isdeleted" app/widgets/scene_layer.py                     # ≥ 1

.venv/bin/python -m pytest tests/widgets/test_font_size_stepper.py tests/widgets/test_settings_nav_icon_theme.py -q
# 期望 12 passed, 1 failed
```

**⑤ 预期噪声**：`test_font_size_options_delta_mapping` 存量红
（`design_tokens.py:107` 默认档位已是 `"4"`，断言仍写 `"2"`；HEAD 同样红）。

---
## 附录 B：分块暂存精确命令

> 只解决一件事：**两个文件必须在 commit 内部按 hunk 切分**，其余文件 `git add <path>` 即可。
> 配合附录 A 使用：**先用本附录切好暂存，再用附录 A 逐项验证。**

### B.0　前置：生成 U0 patch 并核对 hunk 编号

> ⚠ **本节的 `apply --cached` 方案已被 #113 实测推翻（子集 apply 落点漂移，产出语法非法文件），已由 #122 方案 C 替换（见 B.2）；旧方案仅作失败记录保留。**
> 具体：27 hunk 子集 → `IndentationError`（`{inactive}` 插进了 `_nav_btn_style` 的 f-string）；6 个 nav hunk 子集 → `SyntaxError`；
> `--3way` / `--recount` / 倒序均无效。根因是 `-U0` 无上下文，`git apply` 用 old-side 行号定位 index，排除部分 hunk 后偏移无法补偿。
> **B.2 已改用方案 C（正向重建 + `update-index --cacheinfo`），不再走 `git apply`。**
> 另：下面的 awk 配方会**漏掉 hunk 25**（该 hunk 只有 `+{inactive}` / `+{active}` 两行，不含标记词），实际输出是 `15 19 23 24 26` 而非 `15 19 23 24 25 26`。

```bash
cd /Users/dingma/work/DriFox/DriFox

git diff -U0 -- app/widgets/cards/settings/llm_settings_card.py > /tmp/llm_all.patch
git diff -U0 -- app/widgets/cards/settings/llm_settings_card.py | grep -c '^@@'      # 期望 33

awk '/^@@/{n++; next} /settingsNavBtn|_nav_qss_dirty/{print n}' /tmp/llm_all.patch | sort -un | tr '\n' ' '
# 期望输出：15 19 23 24 25 26

git diff --numstat -- app/widgets/cards/settings/llm_settings_card.py                # 期望 303  105
```
> 第 3 步输出不是 `15 19 23 24 25 26` 说明 hunk 边界已变（文件被改过）。**不要沿用 §9.2 的编号**，用 `git diff -U0 <file> | grep '^@@' | cat -n` 重新定位并重跑第 3 步。

### B.1　`main_widget.py`（C3 取 M5，C4 取 pump）

```bash
# ── C3：只取 M5（跳过第 1 个 pump，接受第 2 个 M5）
git reset -q
printf 'n\ny\n' | git add -p -- app/main_widget.py

git diff --cached -- app/main_widget.py | grep -c "_tool_card_map"     # 必须 ≥ 1
git diff --cached -- app/main_widget.py | grep -c "DeferredDelete"     # 必须 == 0
git diff -- app/main_widget.py | grep -c "DeferredDelete"              # 必须 ≥ 1
git diff -- app/main_widget.py | grep -c "_tool_card_map"              # 必须 == 0
```
```bash
# ── C4：pump 是此时唯一剩余改动，整文件暂存
git add app/main_widget.py
git status --porcelain -- app/main_widget.py     # 期望空
```

**实测**：`cached: DeferredDelete=4 / _tool_card_map=0`，`uncached: DeferredDelete=0 / _tool_card_map=2`。

### B.2　`llm_settings_card.py`（C4 取 27 hunk，C6 取 6 个）—— 方案 C

> **已由 #122 验证的方案。** 不再使用 `git apply`：从 HEAD 全量内容 + U0 patch **正向重建**出目标文件，
> 用 `git hash-object -w` 写 blob、`git update-index --cacheinfo` 直接改 index。
> 绕开 patch 定位：每个 hunk 的 `old_start` 都是 HEAD 里的绝对行号，与前面应用了几个 hunk 无关。

**33 hunk 归属**：C6 = **15, 19, 23, 24, 25, 26**；C4 = 其余 27 个 = `1-14,16-18,20-22,27-33`。

#### 步骤 1：取 HEAD 内容 + U0 patch

```bash
cd /Users/dingma/work/DriFox/DriFox
git show HEAD:app/widgets/cards/settings/llm_settings_card.py > /tmp/head_llm.py
git diff -U0 -- app/widgets/cards/settings/llm_settings_card.py > /tmp/llm_all.patch

wc -l < /tmp/head_llm.py                       # 期望 2221
grep -c '^@@' /tmp/llm_all.patch               # 期望 33
```

#### 步骤 2：正向重建出「C4 版」文件

```bash
.venv/bin/python /tmp/apply_u0.py /tmp/head_llm.py /tmp/llm_all.patch /tmp/c4_only.py "1-14,16-18,20-22,27-33"
# 期望输出：hunks_total=33 applied=27 out=/tmp/c4_only.py lines=2371
```

`/tmp/apply_u0.py` 全文（写成文件后即可用）：

```python
#!/usr/bin/env python3
"""从 HEAD 全量内容 + U0 patch 正向重建：只应用指定编号的 hunk。

用法: apply_u0.py <head_file> <u0_patch> <out_file> <编号，如 1,2,5-9>

坐标约定（关键，别改）：
  old_count == 0  → 0-based 插入点 = old_start（"在第 old_start 行之后插入"）
  old_count >  0  → 0-based 起点   = old_start - 1
未选中的 hunk 不参与重建，游标也不动 —— 因此各选中 hunk 的 old_start
都是 HEAD 里的绝对行号，互不干扰，不存在偏移补偿问题。
"""
import re
import sys


def parse_hunks(patch_path):
    hunks, cur = [], None
    for ln in open(patch_path, encoding="utf-8"):
        if ln.startswith("@@"):
            m = re.match(r"@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@", ln)
            cur = {"old_start": int(m.group(1)),
                   "old_count": int(m.group(2)) if m.group(2) is not None else 1,
                   "body": []}
            hunks.append(cur)
        elif cur is not None and ln.strip() != "" and ln[0] in "+- \\":
            cur["body"].append(ln.rstrip("\n"))
    return hunks


def parse_picks(spec):
    picks = set()
    for part in spec.split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            a, b = part.split("-")
            picks.update(range(int(a), int(b) + 1))
        else:
            picks.add(int(part))
    return picks


def main():
    head_file, patch_file, out_file, spec = sys.argv[1:5]
    picks = parse_picks(spec)
    hunks = parse_hunks(patch_file)
    head = open(head_file, encoding="utf-8", newline="").read().splitlines(keepends=True)

    out, cursor, applied = [], 0, 0
    for idx, h in enumerate(hunks, 1):
        if idx not in picks:
            continue
        start = h["old_start"] if h["old_count"] == 0 else h["old_start"] - 1
        if start < cursor:
            sys.exit(f"hunk {idx} 起点 {start} 早于游标 {cursor}（hunk 顺序异常）")
        out.extend(head[cursor:start])
        for ln in h["body"]:
            if ln.startswith("-"):
                continue
            if ln.startswith("+") or ln.startswith(" "):
                out.append(ln[1:] + "\n")
        cursor = start + h["old_count"]
        applied += 1
    out.extend(head[cursor:])

    with open(out_file, "w", encoding="utf-8", newline="") as f:
        f.write("".join(out))
    print(f"hunks_total={len(hunks)} applied={applied} out={out_file} lines={len(out)}")


if __name__ == "__main__":
    main()
```

#### 步骤 3：把重建结果写入 index

```bash
SHA=$(git hash-object -w /tmp/c4_only.py)
echo "blob=$SHA"
git update-index --cacheinfo 100644,$SHA,app/widgets/cards/settings/llm_settings_card.py
```

#### 步骤 4：七条判据（全过才算对）

```bash
P=app/widgets/cards/settings/llm_settings_card.py

# ① 重建文件本身语法合法
.venv/bin/python -c "import ast; ast.parse(open('/tmp/c4_only.py', encoding='utf-8').read()); print('c4_syntax_ok')"

# ② 不含 C6 的 nav QSS 标记（两条都必须是 0）
grep -c "settingsNavBtn"   /tmp/c4_only.py      # 期望 0
grep -c "_nav_qss_dirty"   /tmp/c4_only.py      # 期望 0

# ③ 含 C4 的页序表（_nav_order 是 _prefetch_pages 的依赖，缺了预热静默失效）
grep -c "_nav_order"       /tmp/c4_only.py      # 期望 ≥ 3

# ④ 原子性：再应用 6 个 nav hunk 必须与工作树逐字节一致
.venv/bin/python /tmp/apply_u0.py /tmp/head_llm.py /tmp/llm_all.patch /tmp/all33.py "1-33"
cmp /tmp/all33.py $P && echo "BYTE_IDENTICAL"   # 必须打出 BYTE_IDENTICAL

# ⑤ index 里的版本必须能解析（坏掉的只会是 index，工作树永远合法）
.venv/bin/python -c "import ast,subprocess; ast.parse(subprocess.check_output(['git','show',':'+'$P']).decode()); print('index_syntax_ok')"

# ⑥ 行数对账：HEAD 2221 → C4 版 2371 → 工作树 2419（C6 净增 48）
wc -l < /tmp/head_llm.py; wc -l < /tmp/c4_only.py; wc -l < $P

# ⑦ index 里不得含 nav 标记（反向判据，比「有/无」更能抓「多切了」）
git show :$P | grep -c "settingsNavBtn"         # 必须 0
```

#### 步骤 5：C6 时把剩余全部写入 index

```bash
git add app/widgets/cards/settings/llm_settings_card.py
git status --porcelain -- app/widgets/cards/settings/llm_settings_card.py     # 期望空
```

#### 为什么不用 `git apply --cached`（失败记录）

`-U0` 无上下文，`git apply` 用 **old-side 行号在 index 里定位**；排除部分 hunk 后，后续 hunk 的落点无法补偿，
**产出语法非法文件**（27 hunk 子集 → `IndentationError`，`{inactive}` 插进了 `_nav_btn_style` 的 f-string；
6 个 nav hunk 子集 → `SyntaxError`）。`--3way` / `--recount` / 倒序均无效。
更危险的是：**内容 grep 判据在坏文件上全部通过**，只有 `ast.parse` 能抓到。

### B.3　总原则

> **提交前双查：`git diff --cached` 里「本 commit 特征」必须 > 0，「下一 commit 特征」必须 = 0。**
>
> `rc=0` 不代表切对了——`printf` 管道与 `apply` 选错 hunk 都是静默成功。**只有内容 grep 能证明。**

| commit | 文件 | 本 commit 特征 | 下一 commit 特征（必须 0） |
|---|---|---|---|
| C3 | `main_widget.py` | `_tool_card_map` | `DeferredDelete` |
| C4 | `main_widget.py` | `DeferredDelete` | ——（M5 已在 C3 落盘，整文件暂存） |
| C4 | `llm_settings_card.py` | `_nav_order` | `settingsNavBtn`、`_nav_qss_dirty` |
| C6 | `llm_settings_card.py` | `settingsNavBtn` | ——（最后一个 commit，整文件暂存） |
