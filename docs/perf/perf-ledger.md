# perf 方案台账（perf-ledger）

> **日期**: 2026-11-19
> **维护**: build（E13 立档）；后续 perf 单在「一、实施台账」追加行
> **范围**: 2026-11-19 性能专项批（E1-E12）与调研终表封版（27 项）的权威索引
> **上游**: `docs/perf/` 既有诊断文档（memleak_master_plan / memory-governance 等）记录「为什么」，
> 本台账只记录「做了什么、效果多少、去哪找」。编号体系（B/T/Q/P/R/E/卡X/机X）以本台账为唯一权威表。
> **核对纪律**: 每个 commit hash 与文件清单均经 `git show --stat` 核对（E13），不得凭记忆追加。

---

## 一、实施台账（2026-11-19 性能批）

### 2026-11-28 聊天界面复测

Windows、Python 3.14.2、Qt 5。下表为本机离屏微基准，进程预热后取样。历史首屏与流式正文另用真实 WebEngine 窗口回放。

| 场景 | 优化前 | 优化后 |
|---|---:|---:|
| 普通助手消息，每卡子控件数 | 27 | 16 |
| 连续创建并保留 60 张未渲染助手卡片，进程 RSS 增量 | 17.9 MiB | 9.8 MiB |
| 同批卡片，单卡构造中位耗时 | 2.42 ms | 1.32 ms |
| 6000×4000 JPEG 生成 80 px 高缩略图，6 个独立文件取中位数 | 82.0 ms | 9.7 ms |
| 连续追加 12000 个 32 字符片段，延后渲染，3 轮总耗时中位数 | 396 ms | 222 ms |

重试栏和文件差异操作在首次有对应内容时创建。隐藏流式卡片会暂停装饰动画计时器，重新显示时按当前流状态恢复。附件缩略图通过 Qt 解码器请求预览尺寸，点击放大仍从源文件或会话内嵌图片读取。流式边界判断仅读取末尾 128 个字符。

卡片 RSS 只涵盖未创建 viewer 的控件。追加测试以空渲染回调模拟一次积压，测量 Python 端处理耗时。真实窗口验证入口为 `tests/ui/test_history_first_paint.py` 和 `tests/perf/test_visible_text_cadence.py`（后者需设 `DRIFOX_VISIBLE_CADENCE=1`）。

### 2026-11-19 历史记录

| commit | 单号 | 内容一句 | 关键验证数字 | 新增测试 |
|---|---|---|---|---|
| `b9c14630` | E2 | context-tier memo：shape/normalize 骨架化产物按 `(id,len)` LRU 512 缓存，命中免 parse+dump | 11 新用例（hit/miss/id 复用防线/LRU 上限） | `test_tier_memo.py` |
| `5e6fca76` | E2 | memory-governor 会话清理摘除两处会话无关的图标缓存（键空间无 session 维度） | 行为断言：清理后图标缓存原样保留、token 缓存照清（对照） | `test_memory_governor_icon_cache_scope.py` |
| `2f29ff63` | E1（顺手批） | quickwins 三连：system-tokens md5 身份短路、charts 逐块标记、orphan-fix 早退 | 8 用例（budget resolver/chart viewer/orphan fix） | `test_builtin_budget_resolver.py` 等 3 文件 |
| `6fe79514` | E7 | PluginConfigStore 读盘进程级缓存（键=path+mtime_ns，写路径失效）+ cfg() 共享 store 实例 | **读盘对照：10 key × 2 轮投影 20 次 parse → 1 次**（同文件多 key 共享一次读盘） | `test_context_cfg_shared_store.py` + store 6 用例 |
| `735009e2` | E7 | skills 缓存 key 动态源签名段（iterdir+stat）纳入既有 30s TTL（原每次现算） | TTL 命中零现算 / 过期重算 / invalidate 清动态段，21 passed（含 reloaders 钩子链） | `test_local_skills_contrib.py` +3 用例 |
| `0c3fe727` | E7 | 启动期 daemon 预热 `resolve_context_limit`（provider 插件惰性 import + AST 审计冷启动） | **首次发送尖峰 106ms → 47.7ms**（E5 实测口径）；不改启动时序（AST 守卫） | `test_main_context_limit_prewarm.py` |
| `cd7cbbee` | E6 | 流式游标化批：pending-parts 块追加、proto-state 扫描、快照实心游标，消除流式 O(n²) | **fuzz 5 种子 × 40 轮零失配**（等价性）；游标位置确定性 | `test_stream_cursor_e6.py` |
| `5fa407f5` | E8 | responses 序列化路径耗时 diag mark（为 [B1] 式增量投影决策供数；源码开/打包关） | 打点输出 `耗时 X.XXms msgs=N items=M`；PROBE=0 零日志；产物逐点等价 | `test_serializer_diag_mark.py` |
| `7056a182` | E8 | EngineSession SessionStart+BuildSystemPrompt 注入会话级缓存 + `invalidate_prompt_injections()` 显式失效 | turn 两次 BuildSystemPrompt 仅触发 1 次、PreUserMessage 每轮实时（含 user 消息） | `test_engine_session_prompt_cache.py` |
| `e0b4dd04` | E8 | 保存链 provider 回填改游标增量 + has_user_message 全量扫改会话级布尔态 | 旧消息兜底回填等价、多匹配跳过语义保持、布尔态切会话重置；既有保存链与基线同数 | `test_session_save_incremental_backfill.py` |
| `3bb285b9` | E9 | LSP 符号操作 sleep(0.3) 改就绪轮询（25ms 步进）+ did_open/close 保活引用计数（延时关闭窗口） | **就绪命中 ~30ms（原固定 300ms）**、兜底 ≈300ms 照常执行、连续操作 1 open / 到期 1 close | `test_lsp_symbol_op_ready.py` |
| `02e025c1` | E10 | session extras 改 (sid,msg_idx) upsert diff 写、未变 tick 零 SQL | **write_extras -94%**（E10 基准口径） | `test_session_extras_upsert.py` |
| `9e0d533d` | E11 | 测试修复：recycle 守卫假设（viewer 桩 + 几何排开双拦截点）与内存基线稳态口径落地 | **recycle verdict 120→12（Δ=-108，原 Δ=0 假象）**；**稳态 12 卡 / 14.5 MB/会话**（对照零回收上界 136.9-146.6 MB/会话） | 两 perf 文件重构（改断言口径） |
| `5f6b25e7` | E12 | 流式拍（尾条 _partial）跳过 extract+write_extras、blob 全量直存，finish 拍自然恢复剥离 | **流式拍 extras 链 122→15ms @16K、513→69ms @64K（净收益 86.6%）**；serialize 膨胀实测可忽略（嵌套计时口径） | `test_session_streaming_offload.py` |
| （进行中） | E14 | 卡Q 读链后台化 + 懒 merge（fix-smith 执行中，碰 session_repository 与 main_widget 打开链） | 完成后补行 | — |

关联背景 commit（同批调研/清账，非本轮实施）：`2761c685`（artifacts 画廊设计记录）、`a867211b`/`830895bb`/`90f3927b`/`e354f313`/`f8a7044a`（更早的 R6/#I3 系列与 perf refactor，见各自 commit message）。

---

## 二、调研终表（27 项封版分组）

封版时间 2026-11-19。分组口径：已实施 / 缓办 / 否决 / 仅登记 / 需确认已裁决。

### 已实施（由一节 13 commit 覆盖）

| 单号 | 项 | commit |
|---|---|---|
| E2 | 项J（shape memo）、项K（normalize memo）、卡R①（图标缓存摘除） | `b9c14630` + `5e6fca76` |
| E1 | 顺手批：system-tokens md5 身份短路、charts 逐块标记、orphan-fix 早退（封版表对应项以素材卡为准） | `2f29ff63` |
| E7 | 卡A（config 读盘缓存 + cfg 共享实例）、项I（skills key TTL）、尖峰预热单 | `6fe79514` + `735009e2` + `0c3fe727` |
| E6 | 项G（流式游标化批） | `cd7cbbee` |
| E8 | 卡E（responses diag）、项H（hook 注入缓存）、机2（provider 回填增量） | `5fa407f5` + `7056a182` + `e0b4dd04` |
| E9 | 卡M（LSP 轮询 + 保活） | `3bb285b9` |
| E10 | extras 写路径 upsert diff（见「仅登记」注） | `02e025c1` |
| E11 | #11/#15 测试侧修复（非优化项，属测试真实性） | `9e0d533d` |

### 缓办（有明确重启条件）

| 单号 | 项 | 缓办理由 / 重启条件 |
|---|---|---|
| — | 卡D：tier 双遍扫描 | 契约级前置（ContextPolicy 契约改动），需单独立项设计 |
| — | 机3：圆环增量 | 浮点等价红线待设计（圆环累计值浮点累加顺序敏感） |
| — | 项4：书账 | R1 弱（收益不足以覆盖改动风险），观察内存治理效果后复议 |

### 否决

| 项 | 否决理由 |
|---|---|
| 项6（主动 XX 设计） | 主动设计被否，维持现状 |
| 卡P（启动线优化） | 用户明确排除启动线（启动时序红线） |

### 仅登记（低优先 / 已由其他单覆盖）

| 单号 | 项 | 状态说明 |
|---|---|---|
| — | 卡B：[T-C] | 已修（历史 commit 覆盖），登记备查 |
| — | 卡L：事实缩水 | 记录在案，无动作 |
| — | 机1：低优先 | 暂不处理 |
| — | 机4 / 机5 | µs 级收益，不实施；extras 写路径大头已由 E10（`02e025c1`）覆盖 |

### 需确认已裁决

| 单号 | 项 | 裁决 |
|---|---|---|
| E7 | 项I：新技能最长 30s 不可见 | 用户已接受 30s 折衷（即时感知由 `invalidate_skills_cache` 钩子承担，插件变更时已接） |

---

## 三、误区排除记录（防未来重复误判）

### ① 「LRU 回收失效」＝ 测试假象

perf 基线曾判「灌入 120 张、配额 12，淘汰 Δ=0，回收未生效」。#11 追因定论：测试卡的构造**不满足回收链守卫**——只置 `_lazy_rendered=True` 而 viewer 为 None，被 `is_layout_height_settled`（viewer None 必判 False）拦下，`_batch_fully_rendered` 恒 False，候选全空；另有第二拦截点：布局未激活时卡片 geometry 全部重叠 (0,0)，viewport 扫描判「全批可见」→ 全批保护。两处均为测试构造缺陷，**业务代码的回收链本身健康**（守卫放行后 verdict 120→12、空槽 108）。教训：性能测试的「零效果」先查测试自身的采集链路是否可达被测代码，再判业务失效；测试侧修复见 `9e0d533d`。

### ② 「多会话 RSS 无平台期」＝ 分配器表观驻留

#4/#15 曾以「多会话 RSS 线性上升无平台期」推断回收未生效。#15 定论：旧口径卡片全部强引用在裸列表、无容器无回收器，量得的是**零回收上界**；且对象真实释放后 RSS 因分配器表观驻留不即时回落，RSS 单独不能作「未回收」证据。正确口径是「容器内 + 配额回收后稳态」：记账卡数恒 12/配额、稳态 ≈14.5 MB/会话（对照上界 136.9-146.6 MB/会话）。判定依据优先级：存活对象数/记账数 > RSS（RSS 只作辅助）。教训：内存判定必须区分「对象是否被引用」「C++ 是否析构」「RSS 是否回落」三层口径；修复见 `9e0d533d`。

### ③ 「send 尖峰 ≠ tiktoken」＝ provider 插件惰性 import + AST 审计

首次发送实测 96~108ms 尖峰，直觉归因 tiktoken 编码。事实：本机未装 tiktoken；真凶是 `resolve_context_limit` 链路的 provider 插件模块惰性加载 + AST 审计 + profile 缓存冷启动。E5 预热实验实证：后台预热一次后降至 47.7ms，已落地（`0c3fe727`，daemon 线程、不改启动时序）。教训：尖峰归因先查「首次调用独有的冷启动路径」（惰性 import/缓存构建），而非默认怀疑热点常客。

---

## 四、遗留与后续

### 方案 α 窗口语义（E12 落定的持久化语义，动 save 链前必读）

流式期间的 blob **自含全量字段（不剥离）**——流式拍（尾条 `_partial`）跳过 extract + write_extras，整拍直接全量直存；崩溃窗口上限 = 一个流式拍周期（25s partial flush 间隔），窗口内强退恢复**零丢失为结构保证**。落定拍（finish）自然恢复轻量 blob（剥离语义回归）。任何未来动 save 链的改动必须维持这条不变式：**流式拍全量、落定拍轻量**，破坏它等于打开崩溃丢失窗口。实施见 `5f6b25e7`。

| 项 | 状态 | 说明 / 核实路径 |
|---|---|---|
| 卡Q / E14：读链后台预取 + 懒 merge | 实施中（fix-smith 执行中） | 完成后在「一、实施台账」补行 |
| C3 / E16：`get_session_messages` 游标不随 configs 变化失效 | 跟进票（e0b4dd04 引入） | 切服务商后游标仍指旧读取态：旧行为可回填、新行为被跳过。后果 = UI 显示级缺 provider_name（非数据丢失）。建议：游标挂 configs 代际号（configs 变更即失效重读） |
| C4 / E16：`invalidate_prompt_injections` 无生产接线 | 跟进票（7056a182 引入） | 长会话内 agent 热切 / 技能 reload 后 prompt 注入 stale。建议：agent 切换点与 builtin_reloaders 挂钩调用失效，或 TTL 兜底 |
| 英文键 `get("model")` 5 处 vs 中文键「模型名称」 | 线索待核实 | 核实路径：查 `_get_model_config` 是否有英文别名读法；若无，统一改双读（先例 `subagent_worker.py:403` 附近的双读形态） |
| 存量失败 6 个（transport 插件注册 / 测试环境类） | 建议另开单 | 本轮多单基线对照反复复现（HEAD 上即失败），与本轮改动无关；集中在 transport 插件注册与测试环境假设 |
| 素材卡规范改进 | 建议采纳 | 出卡附「已核对 commit 范围」+「同函数族 ±30 行 [PERF] 注释」字段——本轮多单实际受益于人工补做这两步 |
| 存量测试债（并行批遗留） | 已登记 | `-k "tier or context..."` 组合下 5 失败（`test_three_tiers_no_full_recount` ×3、`test_tool_result_pruner` ×2）与 TeamMail 过滤点 AST 守卫（期望 12 实际 10）、`test_lsp_gate_source.py` 2 失败（mock 未跟上 `command` kwarg）、`test_serializer_single_entry`/`test_e2e_serializer_plugin` 3 失败——均为 HEAD 基线复现的存量，建议随「存量失败 6 个」一并另开单 |
