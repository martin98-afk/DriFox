---
name: evolver
description: 集成 Evolver 自进化引擎 — 通过 GEP 协议将 Agent 的运行经验沉淀为可复用的 Gene/Capsule 资产，实现 AI Agent 持续自进化。支持 Hook 自动日志采集、手动触发进化循环、审查模式等。
---

# Evolver 自进化引擎

Evolver 是一个基于 **GEP（Genome Evolution Protocol）** 的 AI Agent 自进化引擎。它通过扫描运行日志，提取失败模式和优化信号，匹配最佳的 Gene/Capsule 资产，输出受协议约束的进化 prompt。

> **核心原则**：Evolver 是 prompt 生成器，不是代码修改器。它生成指导进化的 prompt，不直接修改代码。

## 工作流程

```
DriFox 会话
  │  (Hook 自动采集)
  ▼
memory/ 目录 (JSONL 日志)
  │  (/evolver --run)
  ▼
Evolver CLI (GEP 协议引擎)
  │
  ├─► 扫描 signals → 匹配 Gene/Capsule
  ├─► 输出 GEP prompt
  └─► 记录 EvolutionEvent
```

## 使用方式

### 自动采集（无需干预）

插件 Hook 会在以下时机自动记录日志到 `./memory/`：
| 事件 | 记录内容 |
|------|----------|
| 会话创建 | 项目信息、Evolver 可用性 |
| 工具执行 (write/edit/bash/multi_edit) | 工具名、文件路径、消息摘要 |
| AI 回复 | 回复摘要、是否包含错误 |

每累计 10 条 AI 回复日志，自动触发一次进化检查。

### 手动触发

```
/evolver --run       单次进化 → 输出 GEP prompt
/evolver --review    审查模式（人工确认）
/evolver --status    查看状态
/evolver --log       查看最近进化记录
```

## 安装前提

需要 [Node.js](https://nodejs.org/) >= 18 和 git：
```bash
npm install -g @evomap/evolver
```

## 输出产物

执行 `evolver` 后生成：
- **GEP prompt**: 协议约束的进化指令
- **EvolutionEvent**: 可审计的进化事件 JSONL
- **Gene/Capsule 匹配**: 从本地资产库选择最佳匹配

## 配置策略

通过环境变量调整进化倾向：
- `EVOLVE_STRATEGY=balanced` (默认 50%创新/30%优化/20%修复)
- `EVOLVE_STRATEGY=innovate` (80%创新，快速出新功能)
- `EVOLVE_STRATEGY=harden` (40%修复，聚焦稳定性)
- `EVOLVE_STRATEGY=repair-only` (80%修复，紧急模式)

## 架构说明

```
plugins/evolver/
├── .drifox-plugin/plugin.json    ← 插件清单
├── hooks/
│   ├── hooks.json                ← Hook 定义（自动捕获）
│   └── evolver_hook.py           ← Hook 处理器
├── commands/
│   └── evolver.md                ← /evolver 命令
└── skills/
    └── evolver/SKILL.md          ← 本技能文档

memory/                           ← 进化记忆（自动生成）
├── tools_YYYY-MM-DD.jsonl        ← 工具调用日志
├── assistant_YYYY-MM-DD.jsonl    ← AI 回复日志
├── session_YYYY-MM-DD.jsonl      ← 会话日志
└── evolution_YYYY-MM-DD.jsonl    ← 进化事件
```

## 参考链接

- [Evolver GitHub](https://github.com/EvoMap/evolver) ⭐ 8.7k
- [EvoMap 官网](https://evomap.ai)
- [GEP 协议文档](https://evomap.ai/wiki)
- [arXiv:2604.15097](https://arxiv.org/abs/2604.15097)
