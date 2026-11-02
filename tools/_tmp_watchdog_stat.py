# -*- coding: utf-8 -*-
"""临时：按阻塞点函数抽样完整栈（跑完即删）"""
import re
import collections

REPORT = re.compile(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}[^\[]*\[MainThreadWatchdog\] 主线程阻塞 ≥500ms（当前间隙 (\d+)ms）")
TARGETS = {"on_messages_updated", "_pump", "_on_hot_reload_requested", "on_finished", "_on_add"}

lines = open(r".drifox/logs/all.log", encoding="utf-8", errors="replace").readlines()
samples = collections.defaultdict(list)
i = 0
while i < len(lines):
    m = REPORT.match(lines[i])
    if not m:
        i += 1
        continue
    block = []
    j = i + 1
    while j < len(lines) and (lines[j].startswith("  File ") or lines[j].startswith("    ") or lines[j].startswith("(裸栈") or lines[j].startswith("──")):
        block.append(lines[j].rstrip())
        j += 1
    i = j
    for l in block:
        m3 = re.search(r'File "[^"]+(?:\\app\\|\\plugins\\)[^"]*", line \d+, in (\w+)', l)
        if m3 and m3.group(1) in TARGETS:
            if len(samples[m3.group(1)]) < 2:
                samples[m3.group(1)].append((m.group(1), "\n".join(block[:14])))
            break

for f, items in samples.items():
    print(f"========== {f} ==========")
    for gap, stack in items[:2]:
        print(f"--- gap={gap}ms ---")
        print(stack)
    print()
