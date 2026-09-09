# 性能 / 卡顿 / 崩溃排查手册

> 用户说「卡 / 慢 / 内存涨 / 白屏 / 崩溃 / 闪退 / 0xC0000005」时加载。
> 流程仍走 `diagnose`（先构造反馈循环），本文件提供 DriFox 专属手法与既有工具。

---

## 一、先建基线（不要凭感觉）

```bash
python scripts/snapshot_project.py          # 记录 commit / 分支 / 未提交变更
python scripts/measure_perf.py --help       # 性能测量脚本（scripts/）
python scripts/perf_regression.py --help    # 回归对比
python scripts/tab_cycle_bench.py --help    # 切标签基准
python scripts/mem_track.py --help          # 内存采样
pytest tests/ -m perf                       # perf 标记基准（≥30s 的走 perf_long，需 QApplication）
```

参考文档：`docs/perf/performance-notes.md`、`scripts/measure_perf.md`。

**黄金规则**：卡顿必须量化成毫秒数（如「切 full 卡 270-335ms」），没有数字就没有优化。

## 二、卡顿定位手法（按性价比排序）

1. **确认能否在子进程复现**。不能复现 = 主程序环境固有成本（FramelessWindow + qfluentwidgets 全局钩子），别在 demo 里白测。
2. **二分屏蔽**：临时注释掉疑似子树 / 关闭插件，看耗时是否消失。
3. **`QStackedWidget` / 覆盖层**：页切换会触发整棵子树的 show 传播（polish + 逐控件 Show 事件）。已验证方案：叠放化（覆盖层常驻 visible，切换只 raise/lower）——`setCurrentIndex` 270ms → 2ms。
4. **延迟构造**：大面板文本页懒构造（首开 0.9s → 0.3s）。
5. **UI 线程同步 I/O**：全目录 `stat`、全量配置写盘、LLM token 统计——加 TTL 缓存 + 写盘防抖（实测 toggle 4-18ms → 1.6-4.2ms）。
6. **主程序内 cProfile 采样**：子进程测不出的剩余耗时用这招。

## 三、已知的高成本区

| 区域 | 症状 | 既有优化 |
|------|------|---------|
| `tab_manager_window._ContentStack` | 切页 270ms+ | 叠放 + keep-alive |
| `message_card` 图表 | 多图白屏 / 闪烁 | vault 回插 + echarts rAF 排队 + mermaid 并发限 2 |
| 技能开关设置卡 | 每次 toggle 卡 | cache key 1s TTL + blockSignals + 写盘 500ms 防抖 |
| 插件市场列表 | 底部大段空白 | 手动管理 content 尺寸 + `sizeHint()` 覆盖 |
| `agent_trace` 详情 | 首开 0.9s | 懒构造文本页 |

## 四、崩溃诊断

| 工具 | 位置 | 用途 |
|------|------|------|
| `app/core/crash_handler.py` | 崩溃处理入口 |
| `app/utils/veh_minidump.py` | VEH 异常捕获 + minidump；日志含全线程 Python 栈（崩溃线程★ + native_id 对照）、寄存器、libffi/_ctypes/Qt/sip 模块路径审计 |
| `tests/test_veh_selfcheck.py` | 自检 |

**排查顺序**
1. 取 `logs/` 与 dmp（非零 300KB 才算有效）。
2. 看崩溃线程的 Python 栈——ctypes/libffi 层崩溃只有 Python 栈能点名发起 FFI 调用的槽。
3. 模块路径审计：多副本 `libffi-8.dll` 错载是经典根因。
4. 启动崩溃 / 黑屏先看显卡：OpenGL ANGLE 路径（`libGLESv2.dll` / `d3dcompiler_47.dll`），无桌面 GL 的机器缺这两个 DLL 直接崩。

## 五、线程与 Qt 生命周期（崩溃的 Top 根因）

**规则**
1. 线程退出用**协作式取消**：在循环边界检查 `cancelled`，不要强杀。
2. 清理时 `quit()` → `wait()` 到线程**真正结束**后再丢引用。提前丢引用 = GC 跨线程析构 QObject = 0xC0000005。
3. `deleteLater()` 挂在 `t.finished` 上，不要在别处手动调。
4. 不要用固定 `wait(500)` 阻塞主线程。
5. 后台线程一律 signal → 主线程 slot；工作线程里不碰 widget。
6. 流式写共享状态要加锁（chat_worker 已有 `_stop_lock` 一类保护，新增共享状态照做）。

## 六、内存

- 卡片配额 `_MAX_RENDERED_CARDS` 与 viewer 池是主要闸门，不要绕过。
- WebEngine renderer 进程被 kill 后复用会白屏；强回收前问 `WebViewPool.pids()`。
- 长时间会话：历史压缩走 `app/core/history_compactor.py`。

## 七、收尾

```
1. 记录数字对比（优化前 → 优化后）
2. 加防回归测试（pytest -m perf 或专项脚本）
3. state_manager.py pitfall 记录 symptom/cause/fix
4. 同步 docs/perf/performance-notes.md
```
