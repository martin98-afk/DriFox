#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""测试 shell 压缩实装"""
import subprocess
import sys
sys.stdout.reconfigure(encoding='utf-8')

print("=== Test 1: 直接导入 compress ===")
try:
    from app.tools.shell_compressor import compress
    print("OK")
    
    result = compress("git status", """On branch dev
Changes not staged for commit:
  modified:   app/tools/shell_compressor.py
  modified:   app/tools/terminal_tools.py
""")
    print(f"Result: {result}")
except Exception as e:
    print(f"Error: {type(e).__name__}: {e}")

print()
print("=== Test 2: 执行 git status ===")
r = subprocess.run('git status', shell=True, capture_output=True, text=True, cwd='D:/work/DriFox')
combined = '\n'.join(filter(None, [r.stdout.strip(), r.stderr.strip()]))
result = compress("git status", combined)
print(f"Compressed ({len(combined)} chars -> {len(result)} chars):")
print(result)

print()
print("=== Test 3: 执行 cargo build ===")
r = subprocess.run('dir /b app\\tools\\*.py', shell=True, capture_output=True, text=True, cwd='D:/work/DriFox')
combined = '\n'.join(filter(None, [r.stdout.strip(), r.stderr.strip()]))
result = compress("ls", combined)
print(f"Compressed ({len(combined)} chars -> {len(result)} chars):")
print(result)
