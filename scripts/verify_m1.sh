#!/usr/bin/env bash
# scripts/verify_m1.sh  M1 backend skeleton acceptance (idempotent)
# Compat fix (T4 batch): macOS bundled bash 3.2 has no multibyte
# support and mangles CJK comments; force ASCII-only comments here.
export LC_ALL=en_US.UTF-8 2>/dev/null || true
export LANG=en_US.UTF-8

#
# Exit codes:
#   0 = 
#   1 = health / ws hello / Qt 
#   2 =  server 
#   3 = 
#   4 = python  / server  /  curl
#
# Usage:scripts/verify_m1.sh [--keep-logs]
#   --keep-logs keep server log path for debugging (default: delete)
set -euo pipefail

if [ "${1:-}" = "--keep-logs" ]; then
  KEEP_LOGS=1
else
  KEEP_LOGS=0
fi

# ---- protocol constants (single source; adjust here only) ----
SERVER_MODULE="server.main"      # module: python -m server.main
HANDSHAKE_TIMEOUT=30             # seconds to wait for handshake JSON (env load may stretch cold start)
HEALTH_PATH="/api/health"
HEALTH_HTTP_CODE="200"
WS_PATH="/ws"
WS_TIMEOUT=5                     # seconds to wait for hello frame
EXPECT_HELLO_SUBSTR="hello"      # hello frame assertion substring (lowercase)
QT_BLOCK_MARKER="[qt-block]"     # T4 qt guard marker line

# ---- python resolver: M1_PYTHON > .venv/bin/python > python3 > python ----
PYTHON_BIN="${M1_PYTHON:-}"
if [ -z "$PYTHON_BIN" ]; then
  if [ -x ".venv/bin/python" ]; then
    PYTHON_BIN=".venv/bin/python"
  elif command -v python3 >/dev/null 2>&1; then
    PYTHON_BIN="python3"
  elif command -v python >/dev/null 2>&1; then
    PYTHON_BIN="python"
  else
    echo "FAIL: no python found (set M1_PYTHON)" >&2
    exit 4
  fi
fi
command -v curl >/dev/null 2>&1 || { echo "FAIL: curl missing" >&2; exit 4; }
if [ ! -f "server/main.py" ]; then
  echo "FAIL: server/main.py missing (M1 not implemented)" >&2
  exit 4
fi

LOG="$(mktemp "${TMPDIR:-/tmp}/m1_verify.XXXXXX")"
SERVER_PID=""

cleanup() {
  if [ -n "$SERVER_PID" ]; then
    kill "$SERVER_PID" 2>/dev/null || true
    i=0
    while [ "$i" -lt 10 ] && kill -0 "$SERVER_PID" 2>/dev/null; do
      sleep 0.1
      i=$((i + 1))
    done
    kill -9 "$SERVER_PID" 2>/dev/null || true
    wait "$SERVER_PID" 2>/dev/null || true
  fi
  if [ "$KEEP_LOGS" = "1" ]; then
    echo "log kept: $LOG"
  else
    rm -f "$LOG"
  fi
}
trap cleanup EXIT

# ---- 1) start server in bg, log to temp file ----
# Compat fix (T4 batch): non-interactive bash redirects bg jobs' stdin to
# /dev/null, which trips the server's stdin-EOF auto-exit; use the
# --no-stdin-guard flag and let cleanup() kill the server instead.
"$PYTHON_BIN" -m "$SERVER_MODULE" --no-stdin-guard >"$LOG" 2>&1 &
SERVER_PID=$!
echo "server started (pid=$SERVER_PID, module=$SERVER_MODULE"

# ---- 2) read handshake JSON from stdout (one line, starts with '{', has port) ----
PORT=""
is_port_conflict() {
  grep -qiE 'address already in use|errno 48|winerror 10048' "$LOG"
}
max_iters=$((HANDSHAKE_TIMEOUT * 4))
i=0
while [ "$i" -lt "$max_iters" ]; do
  if ! kill -0 "$SERVER_PID" 2>/dev/null; then
    if is_port_conflict; then
      echo "FAIL: port conflict (server died on start):" >&2
      tail -5 "$LOG" >&2
      exit 3
    fi
    echo "FAIL: server exited early, no handshake JSON:" >&2
    tail -5 "$LOG" >&2
    exit 2
  fi
  line="$(grep -m1 '^{' "$LOG" || true)"
  if [ -n "$line" ]; then
    PORT="$("$PYTHON_BIN" -c 'import json,sys
try:
    d = json.loads(sys.argv[1])
    ds = d.get("drifox_server", {})
    print(ds.get("port", ""), ds.get("token", ""))
except Exception:
    print("")' "$line")"
    if [ -n "$PORT" ]; then
      TOKEN="$(echo "$PORT" | cut -d" " -f2)"
      PORT="$(echo "$PORT" | cut -d" " -f1)"
      break
    fi
  fi
  sleep 0.25
  i=$((i + 1))
done
if [ -z "$PORT" ]; then
  if is_port_conflict; then
    echo "FAIL: port conflict evidence during handshake wait" >&2
    tail -5 "$LOG" >&2
    exit 3
  fi
  echo "FAIL: ${HANDSHAKE_TIMEOUT}s s: no valid handshake JSON (one line with port field)" >&2
  tail -5 "$LOG" >&2
  exit 2
fi
case "$PORT" in
  ''|*[!0-9]*) echo "FAIL: handshake port invalid: $PORT" >&2; exit 2 ;;
esac
echo "handshake OK: port=$PORT"

# ---- 3) /api/health assert HTTP 200 ----
code="$(curl -sS -o /dev/null -w '%{http_code}' --max-time 5 "http://127.0.0.1:${PORT}${HEALTH_PATH}")" || {
  echo "FAIL: ${HEALTH_PATH} request failed (no response)" >&2
  exit 1
}
if [ "$code" != "$HEALTH_HTTP_CODE" ]; then
  echo "FAIL: ${HEALTH_PATH} expect HTTP $HEALTH_HTTP_CODE, got $code" >&2
  exit 1
fi
echo "health OK: HTTP $code"

# ---- 4) WS hellostdlib RFC6455 minimal client, assert first frame ----
if ! "$PYTHON_BIN" - "$PORT" "$WS_PATH" "$WS_TIMEOUT" "$EXPECT_HELLO_SUBSTR" "${TOKEN:-}" <<'PYEOF'

import base64, os, socket, sys, time

port, ws_path, timeout, expect = int(sys.argv[1]), sys.argv[2], float(sys.argv[3]), sys.argv[4].lower()
token = sys.argv[5] if len(sys.argv) > 5 else ""
if token:
    ws_path = ws_path + "?token=" + token
host = "127.0.0.1"
key = base64.b64encode(os.urandom(16)).decode()
req = (
    "GET %s HTTP/1.1\r\n"
    "Host: %s:%d\r\n"
    "Upgrade: websocket\r\n"
    "Connection: Upgrade\r\n"
    "Sec-WebSocket-Key: %s\r\n"
    "Sec-WebSocket-Version: 13\r\n\r\n" % (ws_path, host, port, key)
)
try:
    s = socket.create_connection((host, port), timeout=timeout)
except OSError as e:
    sys.exit("ws: connect failed: %s" % e)
s.settimeout(timeout)
s.sendall(req.encode())

buf = b""
deadline = time.time() + timeout

def recv_more():
    global buf
    if time.time() > deadline:
        sys.exit("ws: timeout waiting server data")
    try:
        chunk = s.recv(4096)
    except socket.timeout:
        sys.exit("ws: timeout waiting server data")
    if not chunk:
        sys.exit("ws: closed by peer before hello")
    buf += chunk

while b"\r\n\r\n" not in buf:
    recv_more()
sep = buf.find(b"\r\n\r\n")
status_line = buf.split(b"\r\n", 1)[0].decode("latin-1")
if " 101" not in status_line:
    sys.exit("ws: expected 101 Switching Protocols, got: %s" % status_line)
buf = buf[sep + 4:]

def need(n):
    global buf
    while len(buf) < n:
        recv_more()

if len(buf) < 2:
    need(2)
b0, b1 = buf[0], buf[1]
opcode = b0 & 0x0F
if opcode == 8:
    sys.exit("ws: server sent close frame instead of hello")
ln = b1 & 0x7F
off = 2
if ln == 126:
    need(4)
    ln = int.from_bytes(buf[2:4], "big")
    off = 4
elif ln == 127:
    need(10)
    ln = int.from_bytes(buf[2:10], "big")
    off = 10
masked = b1 & 0x80
mask = b""
if masked:  # RFC6455 服务端帧不应掩码；防御性兼容
    need(off + 4)
    mask = buf[off:off + 4]
    off += 4
need(off + ln)
data = buf[off:off + ln]
if masked:
    data = bytes(b ^ mask[i % 4] for i, b in enumerate(data))
msg = data.decode("utf-8", errors="replace")
print("WS_FIRST_FRAME:", msg)
s.close()
if expect in msg.lower():
    sys.exit(0)
sys.exit("ws: no '%s' in first frame: %r" % (expect, msg))
PYEOF
then
  echo "FAIL: WS hello  ws: " >&2
  exit 1
fi
echo "ws hello OK ($WS_PATH first frame contains '$EXPECT_HELLO_SUBSTR')"

# ---- 5) QtWidgets import  ----
qt_bad="$(grep -nE '(ImportError|ModuleNotFoundError)' "$LOG" 2>/dev/null | grep -iE 'qtwidgets|pyqt5' || true)"
if [ -n "$qt_bad" ]; then
  echo "FAIL:  Qt import " >&2
  echo "$qt_bad" >&2
  exit 1
fi
if grep -qF "$QT_BLOCK_MARKER" "$LOG"; then
  echo "qt  OK $QT_BLOCK_MARKER"
else
  echo "WARN:  $QT_BLOCK_MARKER   M1  Qt  T4 "
fi

echo "PASS: M1 acceptance (handshake / health / ws hello / qt guard)"
exit 0
