# -*- coding: utf-8 -*-
"""VEH 捕获器自测：子进程内主动抛 AV，验证 log/dmp/Python栈/模块审计落盘。"""
import os
import sys
import tempfile

tmpdir = tempfile.mkdtemp(prefix="drifox_veh_test_")
os.environ["DRIFOX_CRASH_DIR"] = tmpdir

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.utils.veh_minidump import install

ok = install()
print("install:", ok)
assert ok, "VEH install failed"


def victim_frame_a():
    victim_frame_b()


def victim_frame_b():
    import ctypes

    # 故意在深层函数里抛 AV：模拟"槽回调里 ctypes 调用崩溃"
    ctypes.windll.kernel32.RaiseException(0xC0000005, 0, 0, None)


import threading

t = threading.Thread(target=victim_frame_a, name="CrashSimThread")
t.start()
t.join(timeout=10)
print("survived (ctypes SEH may have converted AV to OSError)")
print("CRASH_DIR=" + tmpdir)

# 检查落盘
import glob

logs = glob.glob(os.path.join(tmpdir, "*.log"))
dmps = glob.glob(os.path.join(tmpdir, "*.dmp"))
print("log files:", logs)
print("dmp files:", [(p, os.path.getsize(p)) for p in dmps])
if logs:
    print("---- log head ----")
    with open(logs[0], encoding="utf-8") as f:
        content = f.read()
    for line in content.splitlines()[:60]:
        print(line)
    has_py = "--- python stacks" in content
    has_victim = "victim_frame_b" in content
    has_audit = "--- module audit" in content
    has_reg = "--- registers ---" in content
    print("CHECK python_stacks=%s victim_in_stack=%s module_audit=%s registers=%s"
          % (has_py, has_victim, has_audit, has_reg))
