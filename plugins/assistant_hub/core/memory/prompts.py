# -*- coding: utf-8 -*-
"""prompts.py — 记忆/Dream/经验的全部 LLM prompt 模板（中文）。

约定：
- 每个 build_* 返回 chat messages（[{"role","content"},...]）。
- 模板语言为中文（DriFox 主语境）；输出严格按要求格式，不解释、不加代码围栏。
- 对齐 openhanako lib/memory/prompts/{compile,dream,fact-extraction}.ts 的语义。
"""

from typing import List

_SYSTEM = "你是记忆整理器。输出面向 LLM 回读而非人类阅读：记忆碎片式高密度条目，零冗余。只输出要求的内容，不解释，不加 markdown 代码围栏。"

# 各模板共用的高密度输出风格：碎片化条目，形式像人类记忆，内容保真不能松
_STYLE = (
    "输出风格（读者是 LLM，非人类）：\n"
    "- 一条一碎片：`[标签] 对象: 事实`，标签取 [事实][决策][待办][偏好][坑]，无合适则省略\n"
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


# ── 传送带编译 ────────────────────────────────────────────


def build_compile_today(turns: str, prev_today: str) -> list:
    prev = prev_today.strip() or "（今日暂无记忆草稿）"
    return _msg(
        "把新对话中有价值的信息合并进已有草稿，输出**更新后的完整今日记忆**。\n"
        + _STYLE +
        "- 相似碎片合并，过时碎片修正或删除，寒暄丢弃\n"
        "- 按重要度排序；总量 ≤20 行，接近上限时合并保主干\n"
        "- 不编造新对话里没有的信息\n\n"
        f"【已有草稿】\n{prev}\n\n【今日新对话】\n{turns.strip() or '（无）'}"
    )


def build_compile_daily(prev_today: str) -> list:
    return _msg(
        "把昨日全天「今日记忆」草稿蒸馏成 ≤3 行日记碎片，只留那天的重要进展、决定、未完成事项。\n"
        + _STYLE +
        f"\n【昨日记忆草稿】\n{prev_today.strip() or '（空）'}"
    )


def build_compile_facts(existing_facts: str, turns: str) -> list:
    existing = existing_facts.strip() or "（暂无）"
    return _msg(
        "从新对话提取值得长期记住的事实（身份、关系、偏好、约束、长期项目、硬性要求），"
        "与已有事实合并去重，输出**完整的重要事实清单**。\n"
        + _STYLE +
        "- 只收「过时会让助手犯错」级别的事实，一次性话题不收\n"
        "- 与已有事实冲突时以新对话为准\n"
        "- 总量 ≤30 行；接近上限时合并同类、淘汰最陈旧最不关键的\n\n"
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
        "优化下面记忆单元的表述：指代不明的词（如「那个」「上次」）改成具体对象/绝对日期，"
        "其余按碎片风格压缩，信息不丢。输出优化后的完整列表，`- ` 开头，一行一条，不增不删。\n"
        + _STYLE +
        f"\n【记忆单元】\n{units}"
    )


def build_dream_compose(optimized: str) -> list:
    return _msg(
        "把优化后的记忆单元整理成结构化**长期记忆**文档：\n"
        "- 按主题聚合成 3-6 个小节，每节一个 `## 标题`\n"
        "- 节内条目 `- ` 开头，碎片式高密度，语义等价的碎片合并\n"
        "- 总长压到原文 60% 左右且不超过 1500 字；超限继续合并次要细节\n"
        "- 不丢任何一条独立事实；已过时或被新信息覆盖的旧条目可删\n\n"
        + _STYLE +
        f"【记忆单元】\n{optimized}"
    )


def build_dream_verify(current_sections: str, composed: str) -> list:
    return _msg(
        "你是记忆整理的质量校验员。对比「整理前」和「整理后」，检查：\n"
        "1. 语义保真：整理后有没有新增原文不存在的事实？\n"
        "2. 溯源完整：原文中独立的长期事实有没有被误删？\n"
        "3. 压缩合理：是否完成了实质压缩（而不是原样照抄）？\n\n"
        "判定标准（宁可宽松，不要吹毛求疵）：\n"
        "- 整理是「压缩合并」而非「逐条照抄」：多条相近碎片合并成一条、长条目改写成碎片表述、\n"
        "  细枝末节被聚合概括，都算正常，不算遗漏；只有「整块独立事实完全消失」才判 provenance_ok=false\n"
        "- 过时的、已被新信息覆盖的条目被删除是预期行为，不算遗漏\n\n"
        '只输出一个 JSON 对象：{"semantic_ok": true/false, "provenance_ok": true/false, '
        '"sufficient_compression": true/false, "feedback": "失败原因一句话"}\n'
        "全部通过则 feedback 留空。\n\n"
        f"【整理前】\n{current_sections}\n\n【整理后】\n{composed}"
    )


# ── 经验库压缩 ──────────────────────────────────────────


def build_consolidate(entries: List[Dict[str, str]]) -> list:
    lines = "\n".join(f"- [{e.get('category', '')}] {e.get('content', '')}" for e in entries)
    return _msg(
        "你是经验库的压缩器。下面是一个 AI 助手全部工作经验条目（带当前分类）。\n"
        "压缩合并：语义相近的多条合并成一条、重复的只留一条、过时或被新实践覆盖的删掉，"
        "长条目改写成碎片表述。跨分类的重复也一并合并，相近分类可合并成一个新分类。\n"
        "保留全部仍然有效的事实，禁止编造原文不存在的内容。\n"
        "- 每条仍是一条碎片：对象+动作+关键细节，具体可执行\n"
        "- 每条给出分类（≤8 字，可沿用或新起）\n"
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
        "- 每条给出分类（≤8 字，如「代码风格」「工作流」「工具使用」）\n"
        '- 只输出 JSON 数组：[{"category": "...", "content": "..."}]，没有心得输出 []\n\n'
        f"【助手人格】\n{identity_and_persona.strip()[:1500]}\n\n"
        f"【最近记忆】\n{memory_md.strip()[:4000] or '（空）'}\n\n"
        f"【已有经验库】\n{existing_experience.strip()[:2500] or '（空）'}"
    )
