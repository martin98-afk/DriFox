"""
Agent Canvas Designer — JSON 配置校验与清洗工具
=================================================
在写入 config.json 前调用，预防 JSON 损坏。

用法：
    python tools/sanitize_config.py check <file>   # 语法 + 结构校验
    python tools/sanitize_config.py fix   <file>   # 校验 + 自动修复
    python tools/sanitize_config.py clean <file>   # 仅引号清洗

退出码：0=通过/已修复, 1=有警告, 2=无法修复
"""

import json
import re
import sys
import os


def read_file(path):
    with open(path, 'r', encoding='utf-8') as f:
        return f.read()


def write_file(path, content):
    with open(path, 'w', encoding='utf-8') as f:
        f.write(content)


# ========== 结构校验 ==========

def check_structure(data: dict) -> list:
    """检查配置结构，返回错误/警告列表"""
    issues = []
    nodes = data.get('nodes', [])
    conns = data.get('connections', [])

    if not isinstance(nodes, list):
        return ['❌ nodes 不是数组']
    if not isinstance(conns, list):
        return ['❌ connections 不是数组']

    # 1. start/end
    types = [n.get('type') for n in nodes]
    if 'start' not in types:
        issues.append('❌ 缺少 start 节点')
    if 'end' not in types:
        issues.append('❌ 缺少 end 节点')

    # 2. id 唯一
    ids = [n.get('id') for n in nodes]
    if len(ids) != len(set(ids)):
        dupes = {id for id in ids if ids.count(id) > 1}
        issues.append(f'❌ 重复 id: {dupes}')

    # 3. 所有 targetId 指向存在
    all_ids = set(ids)
    for c in conns:
        if c.get('sourceId') not in all_ids:
            issues.append(f'❌ 无效连接 sourceId={c.get("sourceId")}')
        if c.get('targetId') not in all_ids:
            issues.append(f'❌ 无效连接 targetId={c.get("targetId")}')

    # 4. 所有非 end 节点都有出边（孤立节点检查）
    source_ids = {c.get('sourceId') for c in conns}
    for n in nodes:
        nid = n['id']
        if n.get('type') not in ('end',) and nid not in source_ids:
            issues.append(f'⚠️  节点 {nid} ({n.get("type")}) 没有出边')

    # 5. 所有 config 值必须是字符串
    for n in nodes:
        nid = n['id']
        cfg = n.get('config', {})
        for key, val in cfg.items():
            if not isinstance(val, str):
                issues.append(f'⚠️  {nid}.config.{key} 不是字符串: {repr(val)}')

    # 6. 坐标在合理范围内
    for n in nodes:
        x = n.get('x', 0)
        y = n.get('y', 0)
        if not isinstance(x, (int, float)) or not isinstance(y, (int, float)):
            issues.append(f'⚠️  {n.get("id")} 坐标不是数字')

    return issues


# ========== 字符清洗 ==========

def sanitize_prompt_text(text: str) -> str:
    """
    清洗 prompt 文本中的中文引号：
    - 判定为"不通过" → 判定为「不通过」
    - 只改中文上下文中的成对 ASCII 引号
    """
    # 中文引号对：汉字/中文标点后的 "中文内容" 后跟汉字/中文标点
    text = re.sub(
        r'(?<=[\u4e00-\u9fff\u3000-\u303f\uff00-\uffef])"([^"\n]{1,40})"(?=[\u4e00-\u9fff\u3000-\u303f\uff00-\uffef，。、；：！？）\)\])])',
        r'「\1」',
        text
    )
    return text


def sanitize_all_strings(data):
    """递归清洗所有字符串值中的中文引号"""
    if isinstance(data, dict):
        return {k: sanitize_all_strings(v) for k, v in data.items()}
    elif isinstance(data, list):
        return [sanitize_all_strings(v) for v in data]
    elif isinstance(data, str):
        return sanitize_prompt_text(data)
    return data


# ========== 原始文本级检测（写入前调用） ==========

def raw_text_scan(raw: str) -> list:
    """
    在写入前扫描原始 JSON 文本，检测潜在风险。
    这是在 JSON 解析之前的安全网。
    """
    issues = []

    # 1. 尝试解析
    try:
        json.loads(raw)
    except json.JSONDecodeError as e:
        pos = e.pos
        start = max(0, pos - 60)
        end = min(len(raw), pos + 60)
        issues.append(f'❌ JSON 语法错误: {e}')
        issues.append(f'   上下文: ...{raw[start:end]}...')
        return issues  # 有语法错误就不继续了

    # 2. 检查字符串中未转义的控制字符（除了 \n \t \r）
    for i, ch in enumerate(raw):
        if ord(ch) < 32 and ch not in ('\n', '\t', '\r'):
            start = max(0, i - 20)
            end = min(len(raw), i + 20)
            issues.append(f'⚠️  发现控制字符 U+{ord(ch):04X} 在位置 {i}: ...{raw[start:end]}...')

    # 3. 检查变量引用格式 {{...}} 是否完整
    for m in re.finditer(r'\{\{', raw):
        start = m.start()
        # 从 {{ 后找对应的 }}
        rest = raw[m.end():]
        end = rest.find('}}')
        if end == -1:
            # 没有找到闭合 }}
            ctx = raw[start:start+40]
            issues.append(f'⚠️  变量引用缺少闭合 "}}": {ctx}...')
        elif end > 100:
            # {{ 和 }} 之间太长，可能漏了一个 }}
            ctx = raw[start:start+30]
            issues.append(f'⚠️  变量引用距离过大(>{end}字符): {ctx}...')

    return issues


# ========== CLI 命令 ==========

def cmd_check(filepath):
    """语法校验 + 结构检查"""
    raw = read_file(filepath)
    basename = os.path.basename(filepath)

    # 1. 原始文本扫描
    raw_issues = raw_text_scan(raw)

    # 2. 如果 JSON 有效，做结构检查
    struct_issues = []
    if not raw_issues:  # 没有语法错误
        try:
            data = json.loads(raw)
            struct_issues = check_structure(data)
        except json.JSONDecodeError:
            pass

    all_issues = raw_issues + struct_issues

    if all_issues:
        print(f'📋 {basename} 检查结果:')
        for issue in all_issues:
            print(f'  {issue}')
        return 1
    else:
        data = json.loads(raw)
        print(f'✅ {basename} 检查通过（{len(data.get("nodes",[]))} 节点, {len(data.get("connections",[]))} 连接）')
        return 0


def cmd_fix(filepath):
    """尝试修复已知问题：引号清洗 + 类型修复"""
    raw = read_file(filepath)
    basename = os.path.basename(filepath)
    changes = []

    # Step 1: 尝试 JSON 解析
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        # 尝试修复：中文引号清洗后重新解析
        print(f'❌ JSON 语法错误，尝试自动修复...')
        new_raw = sanitize_prompt_text(raw)
        try:
            data = json.loads(new_raw)
            changes.append('替换了中文语境中的 ASCII 引号为「」')
            raw = new_raw  # 用修复后的文本继续
        except json.JSONDecodeError as e2:
            print(f'❌ 自动修复无效: {e2}')
            print(f'💡 请检查 system_prompt/user_prompt 中的引号是否成对')
            return 2

    # Step 2: 结构检查
    struct_issues = check_structure(data)
    critical = [i for i in struct_issues if i.startswith('❌')]
    warnings = [i for i in struct_issues if i.startswith('⚠️')]

    # Step 3: 清洗 prompt 中的中文引号
    cleaned = sanitize_all_strings(data)

    # Step 4: 修复 config 非字符串值
    fixed_count = 0
    for n in cleaned.get('nodes', []):
        cfg = n.get('config', {})
        for key, val in cfg.items():
            if not isinstance(val, str):
                cfg[key] = str(val)
                fixed_count += 1

    # Step 5: 写回
    data_json = json.dumps(data, ensure_ascii=False, indent=2)
    cleaned_json = json.dumps(cleaned, ensure_ascii=False, indent=2)

    if cleaned_json != data_json or fixed_count > 0:
        write_file(filepath, cleaned_json)
        if cleaned_json != data_json:
            changes.append('清洗了中文引号')
        if fixed_count > 0:
            changes.append(f'修复了 {fixed_count} 个非字符串值')

    if changes:
        print(f'✅ 已修复 {basename}')
        for c in changes:
            print(f'  • {c}')
    else:
        print(f'✅ {basename} 无需修复')

    if critical:
        print(f'\n⚠️  仍有 {len(critical)} 个结构问题需要手动处理:')
        for i in critical:
            print(f'  {i}')

    return 1 if critical else 0


def cmd_clean(filepath):
    """仅清洗引号（不检查结构）"""
    raw = read_file(filepath)
    basename = os.path.basename(filepath)
    try:
        data = json.loads(raw)
        cleaned = sanitize_all_strings(data)
        new_content = json.dumps(cleaned, ensure_ascii=False, indent=2)
        if new_content != raw:
            write_file(filepath, new_content)
            print(f'✅ 已清洗 {basename} 中的中文引号')
        else:
            print(f'✅ {basename} 无需清洗')
        return 0
    except json.JSONDecodeError as e:
        # 对无效 JSON 也尝试清洗
        new_raw = sanitize_prompt_text(raw)
        try:
            json.loads(new_raw)
            write_file(filepath, new_raw)
            print(f'✅ 已修复并清洗 {basename}')
            return 0
        except json.JSONDecodeError:
            print(f'❌ JSON 损坏且无法自动修复: {e}')
            return 2


if __name__ == '__main__':
    if len(sys.argv) < 3:
        print(__doc__)
        sys.exit(0)

    command = sys.argv[1]
    filepath = sys.argv[2]

    if not os.path.exists(filepath):
        print(f'❌ 文件不存在: {filepath}')
        sys.exit(2)

    commands = {'check': cmd_check, 'fix': cmd_fix, 'clean': cmd_clean}
    if command not in commands:
        print(f'❌ 未知命令: {command}，可用: check, fix, clean')
        sys.exit(2)

    sys.exit(commands[command](filepath))
