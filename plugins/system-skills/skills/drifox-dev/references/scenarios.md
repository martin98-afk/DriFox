# 常见场景流程

> SKILL.md 分派后，按场景取流程。

---

## 一、新增功能

```
1. brainstorming：需求对齐 → 设计文档 docs/superpowers/specs/YYYY-MM-DD-<topic>.md
2. writing-plans 拆实施计划
3. grep / glob 定位文件；读 architecture.md 确认落点（别往 main_widget.py 堆）
4. 实现：遵守 conventions.md（行尾 / 范围 / 中文）
5. 注册组件（命令 / Agent / Skill / 插件）
6. ruff check . + 改动文件 ruff format --check + 相关 pytest
7. 同步文档（README / CHANGELOG / docs）
8. git commit（feat: scope - summary）+ state_manager 记 decision
```

## 二、修复 Bug

```
1. diagnose 六阶段：构造反馈循环 → 复现 → 3-5 个可证伪假设 → 探针 → 修复 → 清理
2. 先查 known-pitfalls.md 有没有同类坑（省掉一半时间）
3. 根因 → 最小化修改 → 加防回归测试
4. state_manager.py pitfall --module X --symptom "..." --cause "..." --fix "..."
5. commit（fix: scope - summary）
```

禁止：没复现就改代码；改渲染 / 热重载 / hook 不读 pitfall 库。

## 三、渲染 / 滚动 / 流式改动（专项）

```
1. 读 rendering-pipeline.md + known-pitfalls.md §一
2. 明确改的是哪条分支（流式增量 / 历史非流式 / 流式结束终渲染）
3. 动过骨架 JS → 递增 _SKELETON_CACHE_VERSION
4. 注入类处理 → fence 哨兵，检查 5 处管线全覆盖
5. 高度 → HeightCommitBatch；滚动 → wheel 同步置位 + suppress 检查
6. 实测：长消息、多卡片、切 tab 回来、滚到底不跳、无白屏
```

## 四、性能 / 卡顿 / 崩溃

```
1. 建基线：snapshot_project.py + scripts/measure_perf.py（量化毫秒数）
2. 确认能否在子进程复现；不能 = 主程序环境固有成本
3. 按 perf-playbook.md §二 二分定位
4. 修完对比数字 + 加 -m perf 防回归
5. 崩溃：logs/ + dmp → veh_minidump 全线程栈 → 模块路径审计
6. 同步 docs/perf/performance-notes.md
```

## 五、新增插件

```
1. 用 plugin-creator 技能（11 类组件）；UI 插件用 ui-plugin-creator
2. .drifox-plugin/plugin.json 声明组件 + config_schema（E1）
3. 组件目录：commands/ agents/ skills/ themes/ hooks/ tools/ ui/ model_adapters/ ...
4. 第三方 SDK vendor 到 deps/，函数内延迟导入
5. 热更新验证（新增组件类型要确认差异补载生效）
6. 更新插件 README + marketplace.json（CI 自动重生成）
```

## 六、重构 / 优化

```
1. snapshot_project.py 建基线；focus --task "..."
2. 只动范围内文件（no_unrelated_refactor）
3. 每步跑 ruff + 相关测试
4. 结束记 decision + 更新 architecture.md（如果结构变了）
```

## 七、发版

见 `testing-build.md` §四（版本号五处同步 + 实机验证）。

## 八、需要用户决策

```bash
python scripts/state_manager.py question --question "..." --context "..." [--blocking]
```

阻塞任务用 `--blocking`（摘要置顶）；否则默认非阻塞，仅记录。
