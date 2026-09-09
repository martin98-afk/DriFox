# 已踩坑点库（提炼版）

> **开工前扫一眼同类坑，收工后往里加。**
> 完整 50 条在 `state.json.known_pitfalls`（`state_manager.py show --summary` 看 Top5，满 50 条先归档再写新的）。
> 本文件按「反复出现的根因模式」归类，是坑点库的提炼视图。
> 涉及渲染细节的另见 `rendering-pipeline.md`。

---

## 一、流式渲染与滚动（最高频）

| # | 症状 | 根因模式 | 通用规避 |
|---|------|---------|---------|
| P033 | 流式滚动反复回归：要么强拉底覆盖阅读位置，要么不跟随 | **多套标志位启发式**维护跟随态（scroll delta 判断用户 vs 程序滚动），必然误判 | 收敛为**单一状态机 + 唯一实现**：程序自写基准 top，用户滚动不改基准；所有调用点收敛到一个函数，去掉监听器与标志位 |
| P046 | 工具/思考区滚动位置反复重置 | 程序性 scroll 事件被误判为用户滚动；save/restore 未保存 scrollTop | 对齐既有模式：wheel 同步置位 + `_suppressScrollEvent` 检查 + 程序性滚动标记 + scrollTop 保存恢复 |
| P048 | 折叠框展开瞬间视口内容被推走 | 外层列表**无 scroll anchoring**：高度变化时 value 不动 | 卡片记录 `_last_height_delta`，宿主按卡片顶是否在视口上方补偿 value；其它 emit 点清零 delta 防误补偿 |
| P004 | 流式底部冒出大量假工具框/思考卡 | 全文扫描协议标签**不感知 fence** | 注入前用 sentinel 提取 fence，convert 前放回；**所有**管线分支都要接（漏一处就复发） |
| P003 | 末尾闪现孤立空代码块 | 分段产出把 fence 切成多段 | fence 闭合时回溯起点，整段作为单段产出 |
| P001 | SVG 卡片的按钮消失 | 扫描器只认顶层裸 svg，被包一层就漏 | 扫描穿透「唯一子节点为 svg」的容器 |
| P028 | 用户气泡 resize 后高度不变 | 守卫条件误伤（本意只保护 CodeWebViewer） | 加守卫时明确保护对象，别用宽泛条件 |

> 模式总结：**渲染链路的 bug 80% 出在「状态由启发式推断」**。改成显式状态机 / 显式标记，是反复验证有效的解法。

## 二、卡顿与性能

| # | 症状 | 根因模式 | 通用规避 |
|---|------|---------|---------|
| P007 | 切页固定卡 270-335ms | `QStackedWidget` 页切换触发整棵子树 show 传播 | 叠放化：覆盖层常驻 visible，切换只 raise/lower（270ms → 2ms） |
| P049 | 设置卡开关每次都卡 | UI 线程同步做全目录 stat + 全量写盘 + 信号回环 | TTL 缓存 + `blockSignals` + 写盘防抖（4-18ms → 1.6-4.2ms） |
| P014 | 主线程卡 30s+ | **同步网络 I/O 混进只读缓存接口** | 读接口永不发网络；刷新走后台单飞 + 信号回主线程 |
| P047 | 多窗口状态串台 | 延迟刷新任务乱序执行，旧任务覆盖新状态 | 调度加自增序号，过期任务作废 |
| P010/P011/P012 | 列表底部大段空白 | `QLabel(wordWrap)` 的 C++ `sizeHint` 按理想宽度算，与实际布局差 8-50%；且 PyQt5 不派发 Python override | 手动管理 content 尺寸 + 覆盖 `sizeHint()` 用 `heightForWidth(当前宽度)`；两阶段同步（先撑开 → resize 重排 → 再校正） |
| P015 | 搜索过滤偶发失效 | 异步 reveal 任务晚于过滤执行，无差别 show 覆盖结果 | 异步显示前**重新判定当前过滤条件** |

> 模式总结：**offscreen 测不出的 UI 问题**（字体、sizeHint、真实布局）必须开真实应用验证。

## 三、线程 / 生命周期 / 崩溃

| # | 症状 | 根因模式 | 通用规避 |
|---|------|---------|---------|
| — | 0xC0000005 随机崩 | **GC 跨线程析构 QObject** | 协作式取消 → `quit()` + `wait()` 真正结束 → 再丢引用；`deleteLater` 挂 `finished`；不用固定 `wait(500)` 阻塞主线程 |
| P024 | 历史会话加载后消息为空，DB 被覆写成空数组 | 内存释放与延迟保存（QTimer 1000ms）**竞态** | 保存路径加空值守卫；释放跳过 pending 会话 |
| P022 | MCP 并行连接互相杀死 | 两套防重机制互不知晓 | 共享防重集合；`on_done` 移出锁外 |
| P008 | minidump 0 字节、模块枚举全 unknown | ctypes 未声明 argtypes → 伪句柄 int 溢出；探测返回值误判；in-process `MiniDumpWriteDump` 的 ExceptionParam 恒 NOACCESS | 显式 `argtypes`（HANDLE 用 `c_void_p`）；探测忽略返回值用 needed；`ExceptionParam=None`，异常上下文由 log 承载 |

## 四、插件热重载（高频复发区）

| # | 症状 | 根因模式 | 通用规避 |
|---|------|---------|---------|
| P034 | 给已装插件新增组件类型，热加载不生效需重启 | 去抖把请求合并，兜底 diff 恒空 | 空转分支补「manifest 声明 vs 运行时注册」差异检查 |
| P051/P021 | UI/MCP 组件热重载后不刷新 | 广播分支漏掉对应子系统；槽位签名短路吞掉重建 | 热重载广播要覆盖全部子系统；刷新函数带 `force` 参数 |
| P038 | 改了代码热重载无效 | `sys.modules` 缓存旧模块（共享 manager / 动态 importlib 加载点） | 加载点加 mtime 自检 + 按前缀 purge `sys.modules` |
| P020/P019/P018 | 热重载后 hook 顺序变化 / 分组错位 / 幽灵状态 | unregister 全删后 append 到末尾；索引未同步修正；id 漂移 | 卸载前记录位置，注册时按原位置 insert；pop 后遍历修正其它索引；id 按文件顺序防重 |
| P023 | hook 来源标签错乱 | insert 索引在多 skill 交错下错位 | 用 `rule.skill_name` 作为归属真相，全量重建映射 |
| — | QShortcut ambiguity | 命令热重载未销毁旧 shortcut | 重建前先销毁旧实例 |
| — | gateway 包加载失败 | 插件 SDK **顶层导入**第三方依赖（dingtalk_stream） | SDK vendor 到 `<插件>/deps/`，`sys.path` 优先，函数内**延迟导入** |

## 五、打包与运行环境

| # | 症状 | 根因 | 规避 |
|---|------|------|------|
| — | 无桌面 GL 的机器启动即崩 | 打包时删掉 `libGLESv2.dll` / `d3dcompiler_47.dll`（ANGLE） | 保留 ANGLE DLL |
| P013 | 打包后插件市场 icon 全占位 | 删掉 `libcrypto-1_1-x64.dll`，Qt SSL(OpenSSL 1.1) 初始化失败 | 保留 Qt SSL 依赖 |
| — | 插件 data 目录丢失 | PyInstaller 未 add-data | 见 `testing-build.md` |

## 六、状态同步 / 多窗口

| # | 症状 | 根因模式 | 通用规避 |
|---|------|---------|---------|
| P006 | 流式输出中新建项目，原 tab 被切走 | 入口函数**缺流式保护**，先改了全局状态 | 破坏性操作前先判流式，走「只影响新窗口」的分支 |
| P035 | 团队工作路径错配 | 单槽持久化被上次运行残留污染 | 持久化按 run_id 粒度，读取命中即返回不回退 |
| P050 | 临时助手记忆落到主助手 | 写入路径未走 session override | 建立 session→归属映射，所有写路径都解析 |
| P041/P040 | 卡片残留 / 常驻 tab 被删 | 恢复路径误调 `show()`；`remove_tab` 未排除常驻 id | 显示/删除前判断激活态与常驻集合 |
| P032 | 用量刷新停更 | 关窗误杀共享 key；TTL == tick 周期导致每两轮才刷新 | 销毁前查同 key 存活窗口；`force` 强制重拉 |
| P017 | 保存后面板 UI 停在旧快照 | 保存路径未触发 UI 刷新 | 保存入口统一追加刷新（内部判 `isVisible()`，不可见零开销） |

## 七、往坑点库写条目的格式

```bash
python scripts/state_manager.py pitfall \
  --module <模块> --symptom "<现象，带数字>" \
  --cause "<根因，要具体到函数/机制>" --fix "<修法，要可复用>"
```

写不好的坑点（只写现象不写根因）等于没写。目标：下一个人看到能直接避开。
