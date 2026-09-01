# -*- coding: utf-8 -*-
"""assistant_hub — 智能体助手中心（系统插件）

参考 OpenHanako core/agent.ts + server/routes/agents.ts + lib/memory/* 的
"助手（assistant / persona / pinned / today / longterm / dream / skills）"
数据模型，并把其能力复刻到 DriFox 的 PyQt5 体系内。

设计要点
=========

1. **助手 vs 智能体（双层抽象）**
   - **智能体（agent）**：DriFox 既有概念，由 plugins/system/agents/*.md 决定，
     决定 LLM 的工具/权限/参数/系统提示；保留为**系统层级**不变，供子智能体调用。
   - **助手（assistant）**：本插件新引入的用户层概念。一个助手是
     "人格 + 专属技能 + 私域记忆" 的封装，本质上把一个 assistant_id
     注入到一个**动态派生的 subagent** 来跑，原 build 智能体保持纯系统层不变。

2. **存储布局**
   每个助手存放在 ``<app_data_dir>/assistant_hub/<assistant_id>/``：
   - assistant.yaml               — 元数据（name / yuan / color / 主助手标记 / 技能白名单 / model 覆盖）
   - AGENTS.public.md             — 对外人格（无须内部细节时使用）
   - pinned.md                    — 置顶记忆（手动钉选，永不衰减）
   - memory/today.md              — 当下/今日记忆（来自最近 N 轮会话）
   - memory/longterm.md           — 长期记忆（Dream 整理后的精华）
   - memory/dream/history.json    — Dream 自动整理的历史修订
   - skills/<name>.md             — 助手专属技能描述（执行时注入到 system prompt）
   - avatars/assistant.{png,jpg,…} — 头像（被 QSS 渲染为圆形）
   人格（persona）只读内置，存 ``plugins/assistant_hub/personas/<id>/persona.md``，
   新增人格走 persona-creator 技能，不做 UI 编辑。

3. **UI 入口**
   标题栏常驻 tab 「助手」（无 ×），点开即通过
   ``UIPluginRegistry.toggle_floating_card("assistant_hub")`` 拉起 full 容器浮动卡。
   与 agent_trace 共用同一套 "标题栏 full-card" 模式：上方助手弧形卡片堆叠，
   下方单列分区（基本信息 / 人格切换 / 记忆 / 技能 / Dream）。

4. **运行时**
   选择某个助手后，``AssistantManager.set_active(assistant_id)`` 会同步
   AgentManager：注册一个动态 agent（name = ``assistant_<id>``，mode=subagent），
   把 personas/<yuan>/persona.md 拼成 prompt、内存 exclude 全部 skill =
   assistants.yaml 中明示放行的子集。这样子智能体上下文里"调用助手"
   实际就是调用 ``subagent_para(name="assistant_<id>")``。
   原 build 智能体的 markdown 加载路径 / PluginHostService 完全不动。
"""
