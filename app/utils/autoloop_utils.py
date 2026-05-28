"""AutoLoop 工具函数

从共享笔记中解析步骤清单，供主界面展示。
"""

import re


def parse_steps_from_notes(notes: str, total_steps: int) -> list[str]:
    """从笔记中解析步骤名称列表

    支持格式：
      - [ ] [步骤 1] <描述> | <文件> | <验证方式>
      - [ ] 步骤 1 <描述> | <文件> | <验证方式>
      - [x] [步骤 1] <描述>
      - [ ] 步骤 1 <描述>

    Args:
        notes: 共享笔记内容
        total_steps: 期望的步骤总数（用于保底补齐）

    Returns:
        步骤名称列表，如 ["步骤1标题", "步骤2标题", ...]
    """
    # 预分配结果列表，默认用占位符
    result = [f"步骤 {i}" for i in range(1, total_steps + 1)]

    if not notes:
        return result

    # 匹配两种格式的步骤行
    patterns = [
        r'[-*]\s*\[.*?\]\s*\[步骤\s*(\d+)\]\s*(.*)',
        r'[-*]\s*\[.*?\]\s*步骤\s*(\d+)\s*(.*)',
    ]

    for line in notes.split("\n"):
        line = line.strip()
        if not line:
            continue
        for pattern in patterns:
            m = re.match(pattern, line)
            if m:
                step_num = int(m.group(1))
                description = m.group(2).strip()
                # 取描述的第一段（管道符之前）
                desc = description.split("|")[0].strip()
                if desc and 1 <= step_num <= total_steps:
                    result[step_num - 1] = desc
                break

    return result
