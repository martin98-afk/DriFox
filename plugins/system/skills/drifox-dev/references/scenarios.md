# 常见开发场景流程

> SKILL.md 在任务触及以下场景时分派这里取流程。

---

## 一、新增功能（建议先 brainstorming）

```
1. 加载 brainstorming 技能做需求对齐
   → 写出设计文档到 docs/superpowers/specs/YYYY-MM-DD-<topic>.md
2. 加载 writing-plans 技能拆解为实施计划
3. 用 grep / glob 定位要修改的文件
4. 阅读涉及的入口模块和关键类
5. 遵守项目编码规范、信号链一致性、多窗口隔离策略
6. 注册新组件（命令/Agent/Skill/插件）
7. ruff check + 跑相关测试
8. 同步文档（AGENTS.md / README / docs/）
9. git commit（feat: scope - summary）
```

> **Skill 决策树**：用户说「加功能」「做新东西」「实现某某」 → 优先 `brainstorming`。

## 二、修复 Bug（强制 diagnose）

```
1. 加载 diagnose 技能，进入 6 阶段诊断循环
   - Phase 1: 构造一个能跑、能验证的反馈循环
   - Phase 2: 复现
   - Phase 3: 3-5 个可证伪假设
   - Phase 4: 逐个探针 instrumentation（打 [DEBUG-xxx] 前缀）
   - Phase 5: 修复 + 回归测试
   - Phase 6: 清理 + post-mortem + 写坑点
2. 复现 → 根因 → 最小化修改 → 加测试防回归
3. 记录到 state.json known_pitfalls：
   python scripts/state_manager.py pitfall \
     --module <name> --symptom "..." --cause "..." --fix "..."
4. commit（fix: scope - summary）
```

> **Skill 决策树**：用户说「不工作」「报错了」「挂了」「卡顿」「回归」 → 强制 `diagnose`，**禁止**未走复现就动手改代码。

## 三、新增插件

```
1. mkdir .drifox/plugins/<plugin-name>
2. 创建 .drifox-plugin/plugin.json（components 子项按需 true）
3. 按需创建 commands/ agents/ skills/ themes/ hooks/
4. 配置 .mcp.json（如需）
5. 重启或调用 PluginManager.rescan_plugin() 热更新
6. 更新 .drifox/plugins/README.md 或相关文档
```

## 四、修改已有 Skill

```
1. 定位 skills/<name>/SKILL.md
2. 编辑内容；如果触发条件变化 → 修改 description
3. 实际触发测试：跑一次确认 LLM 加载正确
4. 更新 README.md（如影响外部使用）
```

## 五、重构 / 性能优化

```
1. 先 snapshot_project.py 建立基线（行数、commit）
2. 标注 current_focus：scripts/state_manager.py focus --task "..."
3. 每次大改动跑 ruff + 相关测试
4. 任务结束记录决策：
   scripts/state_manager.py decision --scope <area> --decision "..." --rationale "..."
5. 不要触碰任务范围外的代码（no_unrelated_refactor=true）
```

## 六、需要用户决策时

```
python scripts/state_manager.py question \
  --question "<问题>" \
  --context "<背景>" \
# 用 --blocking 标记阻塞当前任务的问题，non-blocking 仅记录不阻塞
```
