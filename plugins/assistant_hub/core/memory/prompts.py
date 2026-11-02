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
) -> str:
    """生成记忆体系说明书（对齐 WorkBuddy memory_system fragment 的 Layer 2/3 纪律）。

    只写分层结构与读写纪律：各层范围、何时写、写什么、检索选源、角色边界。
    操作语义（参数、截断拒写）归工具 schema；蒸馏完全由门禁指令注入，此处零提及。
    sections 非空时追加全局记忆分区纪律（空 = 自由格式，说明书中不出现分区段）。
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
        "该进全局记忆的四类：\n"
        "- 用户的硬性要求：表达语言、禁用项、交互习惯。关于用户而非关于代码，换项目照样成立\n"
        "- 工具与平台的客观坑：某工具/命令/框架自身的固有行为，谁用都会撞上\n"
        "- 验证方法论：判断「改对了没有」的通用框架（对照实验、指标校验、交叉验证）\n"
        "- 协作纪律：多会话/多 agent 并发下的通用生存法则\n"
        "\n"
        "不该进全局记忆的三类（写错层会误导其他项目）：\n"
        "- 项目架构知识（本项目实际有几层、哪个模块负责什么）→ 项目记忆\n"
        "- 具体业务规则（某个服务/接口/第三方的约定）→ 项目记忆\n"
        "- 过程与临时状态（今天修了什么、正在试什么方案）→ 工作日志，或干脆不记\n"
        "\n"
        "边界判据：「工具坑」与「业务规则」最易混。工具坑的适用面是「任何用这个工具的人」，\n"
        "业务规则的适用面是「这个项目的人」。前者进全局，后者进项目。\n"
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
        "分区后面那段「介绍」是用户对这个分区关注点的定义，不是装饰：整理时按它判断"
        "一条内容该归属哪里，也按它判断该分区内哪些条目已经偏离主题、需要重分配或删除。",
        "",
        "记一条用 memory_write(scope=global, mode=append, section=<分区名>)；"
        "整体整理用 mode=replace，正文按 ## 分区名 组织，不确定能否通过校验时先 dry_run=true 试写。",
    ]
    return "\n".join(parts)


def _sections_block(sections: List[Dict[str, str]]) -> str:
    """分区清单文本（- 名称：完整归类规则）；空 schema 返回空串。"""
    lines = []
    for s in sections or []:
        name = str(s.get("name") or "").strip()
        if not name:
            continue
        text = str(s.get("detail") or s.get("hint") or "").strip()
        lines.append(f"- {name}：{text}" if text else f"- {name}")
    return "\n".join(lines)


# ── 日志蒸馏门禁（新日首写拦截时返回给模型的指令）──────


def build_distill_gate(
    dates: List[str],
    project_file: str,
    global_file: str,
    sections: List[Dict[str, str]] | None = None,
) -> str:
    """生成蒸馏指令：新的一天首次写日志前，必须先蒸馏全部未蒸馏旧日志。

    dates 为待蒸馏日期（旧→新）。指令强调：校对去伪 → 项目蒸馏 → 全局严选 → 打标。
    sections 非空时第 4 步按分区强制整理（含逐条重判顺序与收敛兜底）。
    """
    date_list = "、".join(dates)
    reads = "\n".join(f"   memory_read(scope=daily, date={d})" for d in dates)
    global_step = _distill_global_step(global_file, sections)
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


def _distill_global_step(global_file: str, sections: List[Dict[str, str]] | None) -> str:
    """蒸馏第 4 步：无分区时保持原自由整理；有分区时改为强制重分配。"""
    if not sections:
        return (
            f"4. 严选全局记忆：只把「最珍贵 **且** 与项目无关」的经验放进 {global_file}\n"
            "   （memory_write scope=global 整体更新）。两个条件缺一不可：珍贵但不独特的不进、\n"
            "   珍贵但属于本项目特有的不进；没有合格条目就不改全局记忆，宁缺勿滥。"
        )
    names = _sections_block(sections)
    return (
        f"4. 严选全局记忆并按分区整理 {global_file}（memory_write scope=global 整体更新）：\n"
        "   入选标准不变：只收「最珍贵 **且** 与项目无关」的经验；两个条件缺一不可，\n"
        "   珍贵但不独特的不进、属于本项目特有的不进；没有合格条目不改全局记忆。\n"
        "\n"
        "   全局记忆只接受下列分区，每条内容必须归属其中，禁自建分区、禁「其他/杂项」类兜底区：\n"
        f"{names}\n"
        "\n"
        "   每个分区后面的介绍是用户对该分区关注点的定义，按它判断归属，不要只看分区名：\n"
        "   介绍里说「不写 X」的内容一律不进该区；介绍指向的关注点在本分区一条都没有时，\n"
        "   优先从其他分区的条目里找匹配的搬进来，而不是放着空。\n"
        "\n"
        "   现有条目逐条重判，按固定顺序决策（保证每次结果一致，不会在分区之间来回搬）：\n"
        "   a) 条目所在分区仍在清单内 → 检查它是否与该分区介绍明显不符；\n"
        "   b) 不确定就留在原地，只搬「明显不符」的；\n"
        "   c) 确实要搬的，取唯一最匹配的分区介绍；\n"
        "   d) 多个分区同样匹配 → 取清单中靠前的那个（确定性优先于更合理）；\n"
        "   e) 任何分区都不匹配 → 删除该条，禁强塞；\n"
        "   f) 位于清单外标题下的孤儿条目按同样规则重分配，归属不了就删。\n"
        "\n"
        "   格式硬要求：只有 ## 分区名 可作标题（分区名逐字照抄清单），条目一律 - 开头，\n"
        "   分区块内禁止 # 小节，分区外的总述区禁止 - 条目。\n"
        "   写之前先 memory_write(scope=global, mode=replace, dry_run=true) 提交候选，\n"
        "   校验不通过会返回结构化诊断（unknown_titles/header_in_block/preamble_bullets/skeleton），\n"
        "   按诊断改完再提交；反复失败就直接照 skeleton 填。\n"
        "   校验通过后去掉 dry_run 真写。全局整理未成功前不要打标。"
    )
