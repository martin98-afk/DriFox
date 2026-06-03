import re

# 验证 agent.py
with open(r'D:/work/DriFox/app/core/agent.py', 'r', encoding='utf-8') as f:
    src = f.read()
m = re.search(r'global_contract = """(.+?)""".strip\(\)', src, re.DOTALL)
if m and '工具调用并行原则' in m.group(1):
    print('✅ agent.py: "工具调用并行原则" 段落已注入')
    section = m.group(1)
    if '互相独立' in section and '并行发出' in section and '串行的典型场景' in section:
        print('   └── 三个并行原则子项完整')
else:
    print('❌ agent.py: 段落缺失或格式错误')

# 验证 chat_worker.py
with open(r'D:/work/DriFox/app/core/workers/chat_worker.py', 'r', encoding='utf-8') as f:
    src2 = f.read()
if '"parallel_tool_calls": True' in src2:
    print('✅ chat_worker.py: parallel_tool_calls=True 已注入')
    # 确认位置在 req_kwargs dict 内
    idx = src2.find('"parallel_tool_calls": True')
    ctx_before = src2[max(0, idx-200):idx]
    if 'req_kwargs: Dict' in ctx_before:
        print('   └── 位置正确（在 req_kwargs dict 内）')
else:
    print('❌ chat_worker.py: 参数缺失')
