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


def build_memory_system(global_file: str, project_dir: str, limits: Dict[str, int] | None = None) -> str:
    """生成记忆体系说明书（对齐 WorkBuddy memory_system fragment 的 Layer 2/3 纪律）。

    只写分层结构与读写纪律：各层范围、何时写、写什么、检索选源、角色边界。
    操作语义（参数、截断拒写）归工具 schema；蒸馏完全由门禁指令注入，此处零提及。
    """
    g_limit = (limits or {}).get("global", 4000)
    p_limit = (limits or {}).get("project", 3000)
    return (
        "IMPORTANT: 你拥有跨会话长期记忆，分两层，各自有不同的范围与写入纪律。\n"
        "读写用 memory_read / memory_write；操作语义（参数、超限拒写）见工具说明。\n"
        f"全局记忆文件：{global_file}\n"
        f"项目记忆目录：{project_dir}\n"
        "\n"
        "# 用户级全局记忆（scope=global）\n"
        f"文件：{global_file} | 范围：全部项目 | 上限 {g_limit} 字符\n"
        "仅当用户明确要求长期记住、且内容不绑定具体项目时才写入；用于必须逐字遵守的强制规则。\n"
        "整体替换、先读后写，条目碎片化，保持精简。\n"
        "\n"
        "# 项目级工作记忆（scope=project / daily）\n"
        f"目录：{project_dir} | 范围：仅当前项目\n"
        "- YYYY-MM-DD.md — 每日工作日志。只追加，永不覆盖。\n"
        f"- MEMORY.md — 沉淀后的长期项目笔记，上限 {p_limit} 字符，整体更新。\n"
        "今天的日志不存在时，先创建目录与日期文件再写。\n"
        "\n"
        "何时写：完成实质工作后立即记录。实质工作包括：修复 bug、改动或新建功能、\n"
        "重构或架构调整、选定技术方案；用户交代项目约定或偏好时，同时更新项目 MEMORY.md。\n"
        "不写过程性信息（搜索结果、临时路径、工具报错）——只留跨会话仍有价值的结论。\n"
        "\n"
        "怎么读：按需选源，不必全读。\n"
        "- 本项目过去的工作 → 近期日志（新→旧）与项目 MEMORY.md\n"
        "- 跨项目事项或位置不明 → 全局记忆\n"
        "- 与历史无关 → 跳过，不读记忆文件\n"
        "\n"
        "角色边界：记忆只是补充，不替代正常回复、最终答案或用户要的交付物。"
    )


# ── 日志蒸馏门禁（新日首写拦截时返回给模型的指令）──────


def build_distill_gate(dates: List[str], project_file: str, global_file: str) -> str:
    """生成蒸馏指令：新的一天首次写日志前，必须先蒸馏全部未蒸馏旧日志。

    dates 为待蒸馏日期（旧→新）。指令强调：校对去伪 → 项目蒸馏 → 全局严选 → 打标。
    """
    date_list = "、".join(dates)
    reads = "\n".join(f"   memory_read(scope=daily, date={d})" for d in dates)
    marks = "\n".join(f"   memory_write(scope=daily, date={d}, mark_distilled=true)" for d in dates)
    return f"""本次写入被暂时拒绝：今天的第一条日志之前，必须先蒸馏所有未蒸馏的旧日志。

待蒸馏日志：{date_list}

请严格按以下步骤执行（全部用 memory_read / memory_write 完成，不要用 bash）：

1. 逐日读取原文：
{reads}

2. 校对记忆（去伪存真）：逐条核对，移除不实、过时、已被后续实践推翻的内容；
   与 {project_file} 现有条目重复的直接丢弃；数字/路径/错误原文必须与日志一致，禁止脑补。

3. 蒸馏进项目记忆：把校对后仍成立的内容合并进 {project_file}
   （memory_write scope=project 整体更新，先读后写，保持精简，遵守 AGENTS.md 分层铁律）。

4. 严选全局记忆：只把「最珍贵 **且** 与项目无关」的经验放进 {global_file}
   （memory_write scope=global 整体更新）。两个条件缺一不可：珍贵但不独特的不进、
   珍贵但属于本项目特有的不进；没有合格条目就不改全局记忆，宁缺勿滥。

5. 给已蒸馏日志打标（保留可查，之后蒸馏/门禁均忽略）：
{marks}

6. 全部完成后，重试刚才的日志写入。"""


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
