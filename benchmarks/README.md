# DriFox 性能基线基准套件

可复现基线，用于优化前后对照。所有脚本用 `uv run python benchmarks/bench_xxx.py` 运行。

## 环境与纪律

- 每次运行自动 chdir 到独立临时目录（`.drifox` 用户数据隔离，不污染真实 sessions.db/配置）
- 关闭本机正在运行的 DriFox 主程序后测试（避免资源争抢干扰数据）
- 结果 JSON 落盘 `benchmarks/results/`，环境信息（python/平台/CPU/内存/时间戳）自动附上

## 脚本清单

| 脚本 | 测量项 | 复跑命令 |
|---|---|---|
| `bench_common.py` | 公共工具（隔离/采样/斜率/判定），被其余脚本 import | — |
| `bench_startup.py` | GUI 全链路启动耗时（python→主窗 show）+ 稳态 RSS/tracemalloc + Top20 大对象 | `uv run python benchmarks/bench_startup.py --runs 3` |
| `bench_importtime.py` | `-X importtime` 导入耗时 Top20（累计/自身/一级包聚合） | `uv run python benchmarks/bench_importtime.py --top 20` |
| `bench_session_leak.py` | 会话"新建→关闭"循环 N 轮内存斜率（引擎层 ChatBackend + HistoryManager + SessionStore 全链路） | `uv run python benchmarks/bench_session_leak.py --rounds 40 --msgs 20` |
| `bench_chat_pipeline.py` | 对话管线一轮（MessageCard 流式渲染 + messages_to_api 序列化 + 历史持久化 + 释放）| `uv run python benchmarks/bench_chat_pipeline.py --rounds 30 --chunks 40` |
| | 同上对照组：只建卡不走渲染 | `uv run python benchmarks/bench_chat_pipeline.py --rounds 30 --chunks 40 --no-render`（注意会覆盖 chat_pipeline.json，跑完手动另存） |
| `bench_longrun.py` | Phase A 渲染累积 M 卡线性度；Phase B K 会话 × N 次切换泄漏 | `uv run python benchmarks/bench_longrun.py --cards 60 --sessions 8 --switches 40` |
| `bench_report.py` | 汇总 results/*.json → summary.txt | `uv run python benchmarks/bench_report.py` |

## 一键全量复跑

```powershell
uv run python benchmarks/bench_startup.py --runs 3
uv run python benchmarks/bench_importtime.py --top 20
uv run python benchmarks/bench_session_leak.py --rounds 40 --msgs 20
uv run python benchmarks/bench_chat_pipeline.py --rounds 30 --chunks 40   # 渲染组
uv run python benchmarks/bench_longrun.py --cards 60 --sessions 8 --switches 40
uv run python benchmarks/bench_report.py
```

## 基线摘要（2026-08-20，Python 3.14 / Win11）

1. **启动**：python→主窗 show 中位 **6.865s**；主窗口构造段 677ms；稳态 RSS **607MB**（show+6s），tracemalloc 157.6MB
2. **导入**：总 self 3128ms；Top 包 openai 1054ms(34%)、app 266ms、lsprotocol 181ms、mcp 164ms
3. **会话生命周期**：40 轮×20 条，tracemalloc **+1.98 KB/轮**（R²=0.948）RSS 不涨 → 无泄漏
4. **会话切换**：8 会话×40 次，0 KB/次 → 干净
5. **对话渲染链**：MessageCard 构造→deleteLater 每轮 **RSS +556~576 KB（R²=0.99）**，Python 堆仅 3.4KB/轮 → Qt C++ 侧累积（疑样式/字体/tooltip 缓存），渲染管线本身只贡献 ~20KB/轮 → **优化重点**

## 已知坑

- `QT_QPA_PLATFORM=offscreen` 会触发 design_tokens 0xC0000005 崩溃，不要用
- WebEngine 相关脚本必须 QApplication 创建前 `import PyQt5.QtWebEngineWidgets` 且 `AA_ShareOpenGLContexts`
- 懒加载 batch 需要 `init_shared_web_profile(parent=app)`（缺则 qFatal）
