# -*- coding: utf-8 -*-
"""prompts.py — 记忆/Dream/经验的全部 LLM prompt 模板（中文）。

约定：
- 每个 build_* 返回 chat messages（[{"role","content"},...]）。
- 模板语言为中文（DriFox 主语境）；输出严格按要求格式，不解释、不加代码围栏。
- 对齐 openhanako lib/memory/prompts/{compile,dream,fact-extraction}.ts 的语义。
"""

_SYSTEM = "你是记忆整理器。只输出要求的内容，不解释，不加 markdown 代码围栏。"


def _msg(user_text: str) -> list:
    return [
        {"role": "system", "content": _SYSTEM},
        {"role": "user", "content": user_text},
    ]


# ── 传送带编译 ────────────────────────────────────────────


def build_compile_today(turns: str, prev_today: str) -> list:
    prev = prev_today.strip() or "（今日暂无记忆草稿）"
    return _msg(
        "以下是助手「今日记忆」的已有草稿，和今天新产生的对话内容。\n"
        "请把新对话中有价值的信息合并进今日记忆，输出**更新后的完整今日记忆**。\n"
        "要求：\n"
        "- 要点式（- 开头），总长不超过 600 字；接近上限时必须合并、只保留主干\n"
        "- 电报体极简：砍掉连接词、修饰语、完整句式，一行一条短语，只留对象+动作+关键数字/路径/状态\n"
        "- 保留具体事实：人名、日期、偏好、约定、进行中的事项、重要决定\n"
        "- 相似内容合并，过时的修正，琐碎寒暄丢弃\n"
        "- 按重要度排序，不要编造没有的信息\n\n"
        f"【已有草稿】\n{prev}\n\n【今日新对话】\n{turns.strip() or '（无）'}"
    )


def build_compile_daily(prev_today: str) -> list:
    return _msg(
        "下面是昨天一整天的「今日记忆」草稿。请把它蒸馏成 2-3 句话的日记，"
        "记录那天重要的事、进展和未完成的事项。总长不超过 150 字。\n\n"
        f"【昨日记忆草稿】\n{prev_today.strip() or '（空）'}"
    )


def build_compile_facts(existing_facts: str, turns: str) -> list:
    existing = existing_facts.strip() or "（暂无）"
    return _msg(
        "以下是助手已记录的「重要事实」，和新对话内容。\n"
        "请从新对话中提取值得长期记住的事实（用户身份、关系、偏好、约束、长期项目、硬性要求），"
        "与已有事实合并去重后，输出**完整的重要事实清单**。\n"
        "要求：\n"
        "- 每条一行，`- ` 开头\n"
        "- 只收「过时会让助手犯错」级别的事实，一次性话题不收\n"
        "- 与已有事实冲突时以新对话为准\n"
        "- 不确定的信息不要收\n"
        "- 电报体极简：每条砍到最短短语，去连接词修饰语，保留具体数字/路径/名称\n"
        "- 全清单总长不超过 1200 字：接近上限时必须合并同类条目、淘汰最陈旧最不关键的条目\n\n"
        f"【已有重要事实】\n{existing}\n\n【新对话】\n{turns.strip() or '（无）'}"
    )


# ── Dream 五步（对齐 dream/runner.ts 管线语义）──────────


def build_dream_atomize(memory_text: str) -> list:
    return _msg(
        "把下面的记忆文本拆成**原子记忆单元**：每条只包含一个独立的事实/事件/偏好/约定，"
        "一行一条，`- ` 开头。丢弃纯寒暄和已失效的临时信息。不要合并，不要改写事实本身。\n\n"
        f"【记忆文本】\n{memory_text}"
    )


def build_dream_dedupe(units: str) -> list:
    return _msg(
        "下面是记忆单元列表。找出语义重复的条目（同一件事的不同表述），"
        "输出**去重后的单元列表**（保留信息最完整的一条表述），`- ` 开头，一行一条。\n"
        "只删语义重复，不删相似但不同的事实。\n\n"
        f"【记忆单元】\n{units}"
    )


def build_dream_optimize(units: str) -> list:
    return _msg(
        "下面是记忆单元列表。请优化每条的表述：统一用第三人称陈述句，补全缺失的主语和时间，"
        "去掉口语化和指代不明的词（如「那个」「上次」改成具体对象/日期）。\n"
        "同时电报体极简：每条压到最短短语，去虚词和修饰语，只留主干与关键数字/路径，信息不丢。\n"
        "输出优化后的完整列表，`- ` 开头，一行一条，不增不删。\n\n"
        f"【记忆单元】\n{units}"
    )


def build_dream_compose(optimized: str) -> list:
    return _msg(
        "下面是优化后的记忆单元。请整理成结构化的**长期记忆**文档：\n"
        "- 按主题聚合成 3-6 个小节，每节一个 `## 标题`\n"
        "- 节内条目 `- ` 开头，电报体极简：去虚词修饰，只留主干与关键数字/路径，语义等价合并\n"
        "- 总长度压缩到原文的 60% 左右，且**不超过 1500 字**（超限必须继续合并次要细节），但不丢任何一条独立事实\n"
        "- 已经过时或被新信息覆盖的旧条目可删除\n\n"
        f"【记忆单元】\n{optimized}"
    )


def build_dream_verify(current_sections: str, composed: str) -> list:
    return _msg(
        "你是记忆整理的质量校验员。对比「整理前」和「整理后」，检查：\n"
        "1. 语义保真：整理后有没有新增原文不存在的事实？\n"
        "2. 溯源完整：原文中独立的长期事实有没有被误删？\n"
        "3. 压缩合理：是否完成了实质压缩（而不是原样照抄）？\n\n"
        "判定标准（宁可宽松，不要吹毛求疵）：\n"
        "- 整理是「压缩合并」而非「逐条照抄」：多条相近条目合并成一条、长条目精简措辞、\n"
        "  细枝末节被聚合概括，都算正常，不算遗漏；只有「整块独立事实完全消失」才判 provenance_ok=false\n"
        "- 过时的、已被新信息覆盖的条目被删除是预期行为，不算遗漏\n\n"
        '只输出一个 JSON 对象：{"semantic_ok": true/false, "provenance_ok": true/false, '
        '"sufficient_compression": true/false, "feedback": "失败原因一句话"}\n'
        "全部通过则 feedback 留空。\n\n"
        f"【整理前】\n{current_sections}\n\n【整理后】\n{composed}"
    )


# ── 经验反思 ────────────────────────────────────────────


def build_reflect(identity_and_persona: str, memory_md: str, existing_experience: str) -> list:
    return _msg(
        "你是一个 AI 助手的自我反思器。下面是这个助手的人格简介、最近的记忆、以及已有的经验库。\n"
        "请从**最近的记忆**中提炼出 0-3 条「工作心得」——即这个助手在与用户协作中沉淀出的、"
        "未来做类似事情时应该记住的做法或教训（例：用户的代码风格偏好、某类任务的有效工作流、踩过的坑）。\n"
        "要求：\n"
        "- 只提炼记忆中有依据的心得，不要泛泛的空话（如「要认真负责」）\n"
        "- 每条给出分类（≤8 字，如「代码风格」「工作流」「工具使用」）\n"
        '- 只输出 JSON 数组：[{"category": "...", "content": "..."}]，没有心得输出 []\n\n'
        f"【助手人格】\n{identity_and_persona.strip()[:1500]}\n\n"
        f"【最近记忆】\n{memory_md.strip()[:4000] or '（空）'}\n\n"
        f"【已有经验库】\n{existing_experience.strip()[:2500] or '（空）'}"
    )
