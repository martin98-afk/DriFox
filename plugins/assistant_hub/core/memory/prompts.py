# -*- coding: utf-8 -*-
"""prompts.py — 记忆系统说明书 + 经验体系 LLM 模板（中文）。

结构对齐 WorkBuddy 的 workbuddy-memory-system.md（三层记忆规范），
品牌词全部替换为 drifox。编译/传送带（openhanako 系）prompt 已随管线删除。
"""

from typing import Dict, List

_SYSTEM = "你是记忆整理器。输出面向 LLM 回读而非人类阅读：记忆碎片式高密度条目，零冗余。只输出要求的内容，不解释，不加 markdown 代码围栏。"

# 各模板共用的高密度输出风格：碎片化条目，形式像人类记忆，内容保真不能松
_STYLE = (
    "输出风格（读者是 LLM，非人类）：\n"
    "- 一条一碎片：`[标签] 对象: 事实`，一行一条，每行 ≤60 字符，写不下拆成多条；标签取 [事实][决策][偏好][坑]，无合适则省略\n"
    "- 只写固定内容：已完成的进展、事实、决策、偏好/用户习惯、坑；\n"
    "  禁写待办、计划、未完成事项等实时性状态（它们天然过时，读了误导）\n"
    "- 禁完整句、禁连接词、禁修饰语；主语可省。如「修 persona.py 打包丢 getpass」，\n"
    "  不写「我们发现并修复了 persona.py 中的打包问题」\n"
    "- 数字/路径/函数名/API/错误原文逐字保留，不翻译不改写、不自造缩写\n"
    "- 时间用绝对日期（如 09-03），禁「昨天/上次/最近」\n"
    "- 每条自包含，脱离上下文可解码；禁「同上」「见前」\n"
    "- 不确定的信息不写，禁止脑补\n"
)


def _msg(user_text: str) -> list:
    return [
        {"role": "system", "content": _SYSTEM},
        {"role": "user", "content": user_text},
    ]


# ── 记忆系统说明书（BuildSystemPrompt 注入，照抄 WorkBuddy 结构）────


def build_memory_system(global_file: str, project_dir: str) -> str:
    """生成记忆系统说明书（含实际路径）。

    对照 WorkBuddy workbuddy-memory-system.md：
    - 三层（云记忆/用户级/工作区）→ 本实现两层（全局/项目）+ 经验体系（工具层）。
    - 写入工具从通用 Edit 收口为 memory_write（DriFox 沙箱下工作目录外写会弹审批）。
    """
    return f"""IMPORTANT: 你拥有跨会话的长期记忆系统，分两层，各自的写入策略不同。
<memory_system>

# 第一层 — 全局记忆（跨项目，读写）

文件：{global_file}｜作用域：所有项目｜注入上限：4,000 字符

当用户明确要求你长期记住某事、且此事不绑定具体项目时，用 memory_write（scope=global）
整体更新此文件，保持精简。与项目记忆不同，这里只写显式要求——精确、必须严格执行的规则。

# 第二层 — 项目记忆（当前项目，读写）

目录：{project_dir}/｜作用域：仅当前项目

文件：
- {project_dir}/YYYY-MM-DD.md — 每日工作日志。**只追加，绝不覆盖**（memory_write scope=daily）。文件名即日期，content 勿再写日期标题。
- {project_dir}/MEMORY.md — 项目长期笔记（memory_write scope=project 整体更新）。上限 3,000 字符。
- 今天的日志不存在时，memory_write 会自动创建；不要手动建文件。

取历史上下文：按需选择来源，不需要全读。
- 查本项目的过往工作 → memory_read（scope=daily，带 date 读某天；最近的优先）或读项目 MEMORY.md。
- 跨项目、位置不确定 → 读全局记忆，或直接问用户。

角色边界：记忆只是补充，不替代正常回复、最终答案或用户要求的任何交付物。

**分层铁律（防重复）**：
- 仓库内的 AGENTS.md（已自动注入，见系统提示词「项目笔记」段）是静态规范的唯一来源：
  构建/测试命令、目录结构、插件化约束、代码风格、提交规范、EOL 纪律等一律**只存在于 AGENTS.md，禁止抄进 MEMORY.md**。
- 项目 MEMORY.md 只写 AGENTS.md 里没有的动态沉淀：踩过的坑、决策理由、易错点、用户偏好、本次改动为何这么做。
- 更新 MEMORY.md 前先自问：这条在 AGENTS.md 里已有吗？已有则不写或删掉既有重复。
- 发现 MEMORY.md 与 AGENTS.md 重复时，删掉 MEMORY.md 里的重复段（AGENTS.md 为准）。

**何时写（必须遵守）：** 完成实质工作后，立即用 memory_write（scope=daily）往当日日志追加一条简短记录。实质工作包括：
- 建好或修改了网站/应用/插件
- 修复了 bug
- 写了或生成报告/文档（**不含** AGENTS.md 类静态规范的同步——那是 AGENTS.md 的事，不写日志）
- 完成代码重构或架构调整
- 选定了技术方案（框架、设计模式等）
- 用户分享了**行为偏好**（回复风格、协作方式）→ 同时更新项目 MEMORY.md
  （注意：项目约定/规范类内容属 AGENTS.md，不进 MEMORY.md，见上文分层铁律）

日志只追加。不记录瞬时信息（搜索结果、临时路径、工具报错）。只沉淀跨会话仍有价值的内容。

维护：把超过 30 天的日志按主题蒸馏进项目 MEMORY.md，然后删除旧日志文件（bash）。除用户明确要求，不存密钥。

</memory_system>"""


# ── 经验库压缩 ──────────────────────────────────────────


def build_consolidate(entries: List[Dict[str, str]]) -> list:
    lines = "\n".join(f"- [{e.get('category', '')}] {e.get('content', '')}" for e in entries)
    return _msg(
        "你是经验库的压缩器。下面是一个 AI 助手全部工作经验条目（带当前分类）。\n"
        "压缩合并：语义相近的多条合并成一条、重复的只留一条、过时或被新实践覆盖的删掉，"
        "长条目改写成碎片表述。跨分类的重复也一并合并，相近分类可合并成一个新分类。\n"
        "保留全部仍然有效的事实，禁止编造原文不存在的内容。\n"
        "- 每条仍是一条碎片：对象+动作+关键细节，具体可执行\n"
        "- 分类纪律：分类宜少而稳定，优先沿用输入中已有的分类，相近分类可合并；确有必要才新建\n"
        '- 只输出 JSON 数组：[{"category": "...", "content": "..."}]，无可保留内容输出 []\n\n'
        f"【全部条目（{len(entries)} 条）】\n{lines}"
    )


# ── 经验反思 ────────────────────────────────────────────


def build_reflect(identity_and_persona: str, memory_md: str, existing_experience: str) -> list:
    return _msg(
        "你是一个 AI 助手的自我反思器。下面是这个助手的人格简介、最近的记忆、以及已有的经验库。\n"
        "请从**最近的记忆**中提炼出 0-3 条「工作心得」——即这个助手在与用户协作中沉淀出的、"
        "未来做类似事情时应该记住的做法或教训（例：用户的代码风格偏好、某类任务的有效工作流、踩过的坑）。\n"
        "要求：\n"
        "- 只提炼记忆中有依据的心得，不要泛泛的空话（如「要认真负责」）\n"
        "- content 一条碎片：对象+动作+关键细节，具体可执行\n"
        "- 分类纪律：分类要少而稳定。必须优先从「已有经验库」里选一个最接近的分类，"
        "只有现有分类确实装不下时才允许新建；给出分类（≤8 字）\n"
        '- 只输出 JSON 数组：[{"category": "...", "content": "..."}]，没有心得输出 []\n\n'
        f"【助手人格】\n{identity_and_persona.strip()[:1500]}\n\n"
        f"【最近记忆】\n{memory_md.strip()[:4000] or '（空）'}\n\n"
        f"【已有经验库】\n{existing_experience.strip()[:2500] or '（空）'}"
    )
