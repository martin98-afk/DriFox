---
description: 手动触发 Evolver 自进化引擎，扫描 memory 日志生成 GEP 进化 prompt
type: prompt
argument-hint:
  "[--status]": "查看 Evolver 运行状态"
  "[--run]": "立即运行一次进化循环"
  "[--review]": "审查模式（人工确认后应用）"
  "[--loop]": "启动持续进化守护循环"
  "[--install]": "检查并安装 Evolver CLI"
  "[--log]": "查看最近进化日志"
mutex_groups:
  mode: ["--status", "--run", "--review", "--loop", "--install", "--log"]
prompt_sections:
  --status: "status"
  --run: "run"
  --review: "review"
  --loop: "loop"
  --install: "install"
  --log: "log"
---

# /evolver 命令 — Evolver 自进化引擎

你正在处理 `/evolver` 命令。用户输入的参数（`$ARGUMENTS`）是 `/evolver` 后的所有文本。

## 📋 执行规则

1. **解析参数**，确定子命令（`--status` / `--run` / `--review` / `--loop` / `--install` / `--log`）。
2. **每个操作必须给用户明确反馈**，包括成功/失败/注意事项。
3. **如果 evolver CLI 未安装**，提示用户并给出安装命令。
4. **所有 evolver 命令在项目根目录的终端中执行**。

## 目录结构

```
plugins/evolver/          ← 本插件目录
├── .drifox-plugin/       ← 插件清单
├── hooks/
│   ├── hooks.json        ← Hook 配置（自动捕获会话日志）
│   └── evolver_hook.py   ← Hook 处理脚本
├── commands/
│   └── evolver.md        ← 本命令文件
└── skills/
    └── evolver/SKILL.md  ← AI 技能文档

memory/                   ← 进化记忆目录（由 Hook 自动生成）
├── tools_YYYY-MM-DD.jsonl    ← 工具调用日志
├── assistant_YYYY-MM-DD.jsonl ← AI 回复日志
├── session_YYYY-MM-DD.jsonl  ← 会话事件日志
└── evolution_YYYY-MM-DD.jsonl ← 进化事件记录
```

---

## 子命令详情
<!-- section:status -->
### `--status` — 查看状态

检查：
1. `evolver` CLI 是否可用（`evolver --version`）
2. `memory/` 目录是否存在，统计各日志文件行数
3. 是否有 `.env` 配置了 EvoMap Hub（可选）
<!-- end -->
<!-- section:run -->
### `--run` — 单次进化

执行 `evolver` 命令，输出 GEP 进化 prompt：
```bash
npx --yes @evomap/evolver
```

输出内容：
- 策略预设（如 `balanced`）
- 扫描到的信号数
- 匹配的 Gene/Capsule
- GEP prompt（可直接复制使用）
<!-- end -->
<!-- section:review -->
### `--review` — 审查模式

```bash
npx --yes @evomap/evolver --review
```
生成进化 prompt 后等待人工确认，适用于重要变更前审查。
<!-- end -->
<!-- section:loop -->
### `--loop` — 持续进化

```bash
npx --yes @evomap/evolver --loop
```
在后台持续运行进化循环。如需终止，用户可 Ctrl+C 或关闭终端。
> **注意**：--loop 输出的 sessions_spawn(...) 指令是纯文本，不会被 DriFox 自动执行。
> 如需自动应用进化，将 Evolver 集成到支持该协议的宿主运行时（如 OpenClaw）中。
<!-- end -->
<!-- section:install -->
### `--install` — 安装/检查

```bash
npm install -g @evomap/evolver
```
检测 Node.js 是否可用，安装后验证。
<!-- end -->
<!-- section:log -->
### `--log` — 查看进化日志

查看 `memory/evolution_YYYY-MM-DD.jsonl` 最近的 5 条进化记录。
如果日志不存在，提示尚无进化记录。
<!-- end -->

---

## 上下文注入

Evolver 插件的 Hook 系统已在以下事件中自动记录日志：
- **SessionStart**: 会话创建时记录
- **PostToolUse**: write/edit/bash/multi_edit 工具执行后记录
- **PostAssistantMessage**: AI 回复后记录（每 10 轮自动触发一次进化检查）

因此 `/evolver --run` 时，evolver CLI 可以直接扫描 `memory/` 中已有数据，无需额外配置。

---

## 参考链接

- [Evolver GitHub](https://github.com/EvoMap/evolver)
- [EvoMap 官网](https://evomap.ai)
- [GEP 协议文档](https://evomap.ai/wiki)
- [arXiv 论文](https://arxiv.org/abs/2604.15097)
