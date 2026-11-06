# -*- coding: utf-8 -*-
"""prompts.py — 记忆系统说明书（中文）。

结构对齐 WorkBuddy 的 workbuddy-memory-system.md（三层记忆规范），
品牌词全部替换为 drifox。写入由模型经 memory_write 自主完成，
后台整理/反思的 LLM 模板已随管线一并删除。
"""

from typing import Dict, List

# ── 记忆系统说明书（BuildSystemPrompt 注入，照抄 WorkBuddy 结构）────


def build_memory_system(
    global_file: str,
    project_dir: str,
    limits: Dict[str, int] | None = None,
    sections: List[Dict[str, str]] | None = None,
    quotas: Dict[str, int] | None = None,
    tone_ratio: int = 30,
) -> str:
    """生成记忆体系说明书（对齐 WorkBuddy memory_system fragment 的 Layer 2/3 纪律）。

    只写分层结构与读写纪律：各层范围、何时写、写什么、检索选源、角色边界。
    操作语义（参数、截断拒写）归工具 schema；蒸馏完全由门禁指令注入，此处零提及。
    sections 非空时追加全局记忆分区纪律（空 = 自由格式，说明书中不出现分区段）。
    quotas/tone_ratio 由用户的感性/理性滑杆决定，只影响分区段的配额提示。
    """
    g_limit = (limits or {}).get("global", 4000)
    p_limit = (limits or {}).get("project", 3000)
    base = (
        "IMPORTANT: 你拥有跨会话长期记忆，分两层，各自有不同的范围与写入纪律。\n"
        "读写用 memory_read / memory_write；操作语义（参数、超限拒写）见工具说明。\n"
        f"全局记忆文件：{global_file}\n"
        f"项目记忆目录：{project_dir}\n"
        "\n"
        "# 用户级全局记忆（scope=global）\n"
        f"文件：{global_file} | 范围：全部项目 | 上限 {g_limit} 字符\n"
        "整体替换、先读后写，条目碎片化，保持精简。\n"
        "写入前提只有一个：这条内容离开当前项目后仍然成立。换一个项目就不成立的内容，\n"
        "一律写项目记忆，不要写这里。\n"
        "\n"
        "全局记忆有两种价值，缺一种就会退化成工作台账或正确的废话：\n"
        "- 理性：让下一个项目少犯错。可复用、可验证。\n"
        "- 感性：让下一次更像个人。对这个人的稳定理解、对自己位置的认识。\n"
        "  感性条目不要求独特，只要求真实——「不独特」正是它成立的原因（谁都遇到的事，\n"
        "  才是稳定的人性）。\n"
        "\n"
        "理性的两类（该进全局）：\n"
        "- 工具与平台的客观坑：某工具/命令/框架自身的固有行为，谁用都会撞上\n"
        "- 验证方法论：判断「改对了没有」的通用框架（对照实验、指标校验、交叉验证）\n"
        "\n"
        "感性的两类（该进全局）：\n"
        "- 这个人的样子：他怎么想事、在意什么、什么让他不耐烦、他的品味与雷区。\n"
        "  写成「具体行为 + 我的解读」，例如「他说先不做多半是已在心里权衡过成本，\n"
        "  这时追问细节会被嫌烦，直接给判断他才接话」。禁形容词标签（「用户喜欢简洁」）。\n"
        "- 我自己：我是谁、我和他是什么关系、我该以什么姿态出现、\n"
        "  我在哪些事上会反复犯同一个错。\n"
        "\n"
        "不该进全局记忆（写错层会误导其他项目）：\n"
        "- 项目架构知识（本项目实际有几层、哪个模块负责什么）→ 项目记忆\n"
        "- 具体业务规则（某个服务/接口/第三方的约定）→ 项目记忆\n"
        "- 过程与临时状态（今天修了什么、正在试什么方案）→ 工作日志，或干脆不记\n"
        "- 正确的废话（「用户重视效率」「用户喜欢清晰表达」）→ 什么都不算，删掉\n"
        "\n"
        "边界判据：「工具坑」与「业务规则」最易混。工具坑的适用面是「任何用这个工具的人」，\n"
        "业务规则的适用面是「这个项目的人」。前者进全局，后者进项目。\n"
        "感性条目的判断标准：它能不能改变我下次的反应。改变不了就是标签，不是记忆。\n"
        "除用户明确要求，不存密钥。\n"
        "\n"
        "# 项目级工作记忆（scope=project / daily）\n"
        f"目录：{project_dir} | 范围：仅当前项目\n"
        "- YYYY-MM-DD.md — 每日工作日志。只追加，永不覆盖。\n"
        f"- MEMORY.md — 沉淀后的长期项目笔记，上限 {p_limit} 字符，整体更新。\n"
        "今天的日志不存在时，先创建目录与日期文件再写。\n"
        "\n"
        "何时写：完成实质工作后立即记录。实质工作包括：修复 bug、改动或新建功能、\n"
        "重构或架构调整、选定技术方案；用户交代项目约定或偏好时，同时更新项目 MEMORY.md。\n"
        "对这个人的理解发生变化时，立刻写——不要等他要求。典型时刻：他的判断出乎你预料、\n"
        "他对同一件事反复表达同一种态度、他的情绪或节奏明显变化。\n"
        "不写过程性信息（搜索结果、临时路径、工具报错）——只留跨会话仍有价值的结论。\n"
        "\n"
        "怎么读：按需选源，不必全读。\n"
        "- 本项目过去的工作 → 近期日志（新→旧）与项目 MEMORY.md\n"
        "- 跨项目事项或位置不明 → 全局记忆\n"
        "- 与历史无关 → 跳过，不读记忆文件\n"
        "\n"
        "角色边界：记忆只是补充，不替代正常回复、最终答案或用户要的交付物。"
    )
    if not sections:
        return base
    parts = [
        base,
        "",
        "# 全局记忆分区（强制）",
        "全局记忆必须按下列分区组织；每条内容必须归属某个分区，"
        "禁出现清单外的 ## 标题、禁在分区块内用 # 小节、禁在分区外写 - 条目：",
        _sections_block(sections),
        "",
        "分区收什么/不收什么、每区字符配额与实际用量，用 memory_read(scope=sections) 拉取。"
        "首次往某区写入前、或要整理全局记忆时，必须先读它。",
        "配额由用户的感性/理性天平决定（同侧各区平分）。配额是上限不是任务："
        "写不满就空着，为了填满写标签式空话比留白更糟；超配额先合并同义条目再删过时内容。",
        "",
        "记一条用 memory_write(scope=global, mode=append, section=<分区名>)；"
        "整体整理用 mode=replace，正文按 ## 分区名 组织，不确定能否通过校验时先 dry_run=true 试写。",
    ]
    return "\n".join(parts)

def _sections_block(
    sections: List[Dict[str, str]],
    quotas: Dict[str, int] | None = None,
) -> str:
    """分区清单文本（- 名称：完整归类规则（基调，配额））；空 schema 返回空串。

    quotas 为 None 时只出名称+hint：system prompt 里不放大段可变文本，
    否则拖一次天平就得重建整个 system prompt，服务端前缀缓存全废。
    """
    lines = []
    for s in sections or []:
        name = str(s.get("name") or "").strip()
        if not name:
            continue
        if quotas is None:
            # 轻量形态：只给名字+一句话，保证写对区；完整规则走工具按需读
            hint = str(s.get("hint") or "").strip()
            lines.append(f"- {name}：{hint}" if hint else f"- {name}")
            continue
        text = str(s.get("detail") or s.get("hint") or "").strip()
        line = f"- {name}：{text}" if text else f"- {name}"
        tone = str(s.get("tone") or "rational").strip().lower()
        line += f"（{'感性' if tone == 'emotional' else '理性'}区"
        if name in quotas:
            line += f"，配额 {quotas[name]} 字符"
        line += "）"
        lines.append(line)
    return "\n".join(lines)


# ── 日志蒸馏门禁（新日首写拦截时返回给模型的指令）──────


def build_distill_gate(
    dates: List[str],
    project_file: str,
    global_file: str,
    sections: List[Dict[str, str]] | None = None,
    quotas: Dict[str, int] | None = None,
    tone_ratio: int = 30,
) -> str:
    """生成蒸馏指令：新的一天首次写日志前，必须先蒸馏全部未蒸馏旧日志。

    dates 为待蒸馏日期（旧→新）。指令强调：校对去伪 → 项目蒸馏 → 全局严选 → 打标。
    sections 非空时第 4 步按分区强制整理（含逐条重判顺序与收敛兜底）。
    """
    date_list = "、".join(dates)
    reads = "\n".join(f"   memory_read(scope=daily, date={d})" for d in dates)
    global_step = _distill_global_step(global_file, sections, quotas, tone_ratio)
    return f"""本次写入被暂时拒绝：今天的第一条日志之前，必须先蒸馏所有未蒸馏的旧日志。

待蒸馏日志：{date_list}

请严格按以下步骤执行（全部用 memory_read / memory_write 完成，不要用 bash）：

1. 逐日读取原文：
{reads}

2. 校对记忆（去伪存真）：逐条核对，移除不实、过时、已被后续实践推翻的内容；
   与 {project_file} 现有条目重复的直接丢弃；数字/路径/错误原文必须与日志一致，禁止脑补。

3. 蒸馏进项目记忆：把校对后仍成立的内容合并进 {project_file}
   （memory_write scope=project 整体更新，先读后写，保持精简，遵守 AGENTS.md 分层铁律）。

{global_step}

5. 全部蒸馏完成后，一条命令给所有未蒸馏日志打标（保留可查，之后蒸馏/门禁均忽略）：
   memory_write(scope=daily, mark_distilled=true)

6. 全部完成后，重试刚才的日志写入。"""


def _distill_global_step(
    global_file: str,
    sections: List[Dict[str, str]] | None,
    quotas: Dict[str, int] | None = None,
    tone_ratio: int = 30,
) -> str:
    """蒸馏第 4 步：无分区时自由整理；有分区时按理性/感性分区强制重分配。"""
    if not sections:
        return (
            f"4. 严选全局记忆：只把「与项目无关、离开当前项目仍成立」的内容放进 {global_file}\n"
            "   （memory_write scope=global 整体更新）。两种价值都要看：\n"
            "   理性——可复用、可验证的工具坑与方法；\n"
            "   感性——对这个人的稳定理解、对自己位置的认识（不要求独特，只要求真实，\n"
            "   且能改变我下次的反应；「用户喜欢简洁」这类标签不算）。\n"
            "   没有合格条目就不改全局记忆，宁缺勿滥。"
        )
    g_limit = sum(int(v) for v in (quotas or {}).values()) or 4000
    emo = int(g_limit) * max(0, min(100, int(tone_ratio))) // 100
    names = _sections_block(sections, quotas)
    return (
        f"4. 严选全局记忆并按分区整理 {global_file}（memory_write scope=global 整体更新）：\n"
        "   入选前提不变：与项目无关、离开当前项目仍成立。在此之上分两种价值：\n"
        "   理性（知识）：可复用、可验证的工具坑与方法。同一坑只留一条，不写重复踩坑的流水账。\n"
        "   感性（理解）：对这个人的稳定理解、对自己位置的认识。\n"
        "     入选标准是「真实且能改变我下次的反应」，**不要求独特**——\n"
        "     不独特正是它成立的原因（谁都遇到的事才是稳定的人性）。\n"
        "     典型候选：他的判断出乎你预料的地方、他对同一件事反复表达的同一种态度、\n"
        "     他明显不耐烦或明显满意的时刻、你自己反复犯的同一个错。\n"
        "     必须写成「具体行为 + 我的解读」；形容词标签要么改写要么删。\n"
        "\n"
        "   全局记忆只接受下列分区，每条内容必须归属其中，禁自建分区、禁「其他/杂项」类兜底区：\n"
        f"{names}\n"
        f"   感性占 {tone_ratio}%：感性池 {emo} 字符、理性池 {int(g_limit) - emo} 字符，同侧各区平分。\n"
        "\n"
        "   归属判定：先判这条记忆改变我什么——改变做事方式进理性区，改变我对这个人\n"
        "   或对自己位置的看法进感性区。两边都像时按这个判，不按话题。\n"
        "   分区介绍说「不写 X」的内容一律不进该区；某区一条都没有时优先从其他分区找匹配的搬进来。\n"
        "\n"
        "   现有条目逐条重判，按固定顺序决策（保证每次结果一致，不会在分区之间来回搬）：\n"
        "   a) 条目所在分区仍在清单内 → 检查它是否与该分区介绍明显不符；\n"
        "   b) 不确定就留在原地，只搬「明显不符」的；\n"
        "   c) 确实要搬的，取唯一最匹配的分区介绍；\n"
        "   d) 多个分区同样匹配 → 取清单中靠前的那个（确定性优先于更合理）；\n"
        "   e) 任何分区都不匹配 → 删除该条，禁强塞；\n"
        "   f) 位于清单外标题下的孤儿条目按同样规则重分配，归属不了就删。\n"
        "\n"
        "   配额是上限不是任务：某区写不满就留着，禁为凑数编条目；超配额时先合并同义条目、\n"
        "   再删过时内容，仍超说明该区该精简到只留最锋利的。\n"
        "\n"
        "   格式硬要求：只有 ## 分区名 可作标题（分区名逐字照抄清单），条目一律 - 开头，\n"
        "   分区块内禁止 # 小节，分区外的总述区禁止 - 条目。\n"
        "   写之前先 memory_write(scope=global, mode=replace, dry_run=true) 提交候选，\n"
        "   校验不通过会返回结构化诊断（unknown_titles/header_in_block/preamble_bullets/skeleton），\n"
        "   按诊断改完再提交；反复失败就直接照 skeleton 填。\n"
        "   校验通过后去掉 dry_run 真写。全局整理未成功前不要打标。"
    )
