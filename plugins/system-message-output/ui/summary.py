"""按本组调用次数选前两类；名称与文件目标语义来自注册元数据。"""
from app.plugins.sdk import caps_for


def _phrase(name, calls, named, file_target):
    n = len(calls)
    active = any(call.state == 'running' for call in calls)
    if named and name == '执行命令' and not active:
        return '运行了命令' if n == 1 else f'运行了 {n} 条命令'
    action = ('正在' + name if active else name + '了') if named else (
        f'正在调用 {name}' if active else f'调用了 {name}')
    if file_target and all(call.target is not None for call in calls):
        files = len({call.target for call in calls})
        prefix = f'分 {n} 次' if files != n else ''
        return f'{prefix}{action} {files} 个文件'
    return f'{action} {n} 次'


_lookup = None


def describe(items):
    global _lookup
    actions = {}
    labels = {}
    for item in items:
        if item.kind != 'tool':
            continue
        if item.name not in labels:
            if _lookup is None:
                cap = caps_for('tool.read', plugin='system-message-output', surface='ui')
                _lookup = cap()['get'] if cap else None
            registration = _lookup(item.name) if _lookup else None
            cn = registration.cn_name if registration else ''
            file_target = bool(registration and registration.metadata.get('permission_arg') == 'filePath')
            labels[item.name] = (cn or item.name or '工具', bool(cn), file_target)
        actions.setdefault(labels[item.name], []).append(item)
    if not actions:
        thoughts = sum(item.kind == 'reasoning' for item in items)
        return f'思考 {thoughts} 次' if thoughts else '消息详情'
    ranked = sorted(actions.items(), key=lambda pair: -len(pair[1]))
    phrases = [_phrase(name, calls, named, file_target) for (name, named, file_target), calls in ranked[:2]]
    other = sum(len(calls) for _, calls in ranked[2:])
    if other:
        phrases.append(f'另有 {other} 次操作')
    return '，'.join(phrases)
