#!/usr/bin/env bash
# scripts/verify_m2b.sh — M2b 验收驱动（场景 1 回归 + 2/3/4 异常流）
#
# 用法：bash scripts/verify_m2b.sh
#   服务 A（默认缓冲）跑场景 1/2/3；服务 B（DRIFOX_EVENT_BUFFER=4）跑场景 4 resync。
# 前置：scripts/ws-assert.mjs、node ≥ 18（全局 WebSocket/fetch）、.venv python。
set -uo pipefail

cd "$(dirname "$0")/.."
PYTHON=".venv/bin/python"
fail=0

stop_srv() { pkill -f "server\.main --no-stdin-guard" 2>/dev/null; sleep 1; }

start_srv() { # $1=port $2=extra env (VAR=val 或空)
  env $2 "$PYTHON" -m server.main --no-stdin-guard --stub-model --port "$1" > "/tmp/m2b_$1.log" 2>&1 &
  for _ in $(seq 1 60); do
    grep -q '"port"' "/tmp/m2b_$1.log" 2>/dev/null && break
    sleep 1
  done
  grep -m1 -o '"port":[0-9]*' "/tmp/m2b_$1.log" | cut -d: -f2
}

TOKEN_OF() { grep -m1 -o '"token":"[^"]*"' "/tmp/m2b_$1.log" | cut -d'"' -f4; }

echo "=== 服务 A：场景 1/2/3 ==="
PORT_A=$(start_srv 18981 "")
TOKEN_A=$(TOKEN_OF 18981)
echo "port=$PORT_A token=${TOKEN_A:0:8}..."

for scene in 1 2 3; do
  echo "--- 场景 $scene ---"
  node scripts/ws-assert.mjs "ws://127.0.0.1:$PORT_A/ws?token=$TOKEN_A" "$scene" || fail=1
done
stop_srv

echo "=== 服务 B（缓冲=4）：场景 4 ==="
PORT_B=$(start_srv 18982 "DRIFOX_EVENT_BUFFER=4")
TOKEN_B=$(TOKEN_OF 18982)
echo "port=$PORT_B token=${TOKEN_B:0:8}..."
echo "--- 场景 4 ---"
node scripts/ws-assert.mjs "ws://127.0.0.1:$PORT_B/ws?token=$TOKEN_B" "4" || fail=1
stop_srv

echo "=== 汇总 ==="
if [ "$fail" -eq 0 ]; then
  echo "PASS: M2b 全场景通过"
  exit 0
fi
echo "FAIL: M2b 存在未过场景"
exit 1
