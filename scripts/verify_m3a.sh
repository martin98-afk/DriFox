#!/usr/bin/env bash
# scripts/verify_m3a.sh — M3a 端点断言清单（curl 逐字段核对 rest.ts typed）
#
# 用法：bash scripts/verify_m3a.sh [port] [token]
#   （缺省从 /tmp/m3a_*.log 抓最近一次服务的握手）
set -uo pipefail
cd "$(dirname "$0")/.."

LOG="${M3A_LOG:-$(ls -t /tmp/m3a_*.log 2>/dev/null | head -1)}"
PORT="${1:-$(grep -m1 -o '"port":[0-9]*' "$LOG" 2>/dev/null | cut -d: -f2)}"
TOKEN="${2:-$(grep -m1 -o '"token":"[^"]*"' "$LOG" 2>/dev/null | cut -d'"' -f4)}"
if [ -z "$PORT" ] || [ -z "$TOKEN" ]; then
  echo "FAIL: 未取得 port/token（先起服务或传参）" >&2
  exit 2
fi
B="http://127.0.0.1:$PORT"
H="X-Drifox-Token: $TOKEN"  # 🟡2 REST token 校验
fail=0

# 断言辅助：$1 名称 $2 期望 jq 表达式（对 $3 响应体）
assert() {
  local name="$1" expr="$2" body="$3"
  if echo "$body" | .venv/bin/python -c "
import json,sys
try:
    d = json.loads(sys.stdin.read())
    ok = ($expr)
except Exception as e:
    ok = False
print('PASS' if ok else 'FAIL', \"$name\" if not ok else '')
" 2>/dev/null | grep -q PASS; then
    echo "PASS  $name"
  else
    fail=1
    echo "FAIL  $name — $(echo "$body" | head -c 160)"
  fi
}

J() { .venv/bin/python -c "import json,sys; d=json.loads(sys.stdin.read()); print(json.dumps(eval(sys.argv[1]), ensure_ascii=False)[:160])" "$1" 2>/dev/null; }

# 1) config/model GET+PUT 往返
body=$(curl -s -H "$H" "$B/api/config/model")
assert "GET /api/config/model 返回对象" "isinstance(d, dict)" "$body"
newcfg=$(echo "$body" | J "d.get('config', d)")
body=$(curl -s -X PUT -H "$H" "$B/api/config/model" -H 'content-type: application/json' -d "$newcfg")
assert "PUT /api/config/model ok" "'ok' in d and d['ok']" "$body"

# 2) agents
body=$(curl -s -H "$H" "$B/api/agents")
assert "GET /api/agents 裸数组（rest.ts AgentInfo[]）" "isinstance(d, list) and all('name' in a for a in d)" "$body"

# 3) plugins：裸数组 + name/enabled/version/config_schema.fields[].type ∈ 8 种
body=$(curl -s -H "$H" "$B/api/plugins")
assert "GET /api/plugins 裸数组（rest.ts PluginInfo[]）" "isinstance(d, list)" "$body"
assert "plugins 条目 name/enabled/version" "all(set(['name','enabled','version']) <= set(p) for p in d)" "$body"
assert "config_schema.fields[].type ∈ 8 类型" "all(
  f['type'] in ('text','password','bool','select','number','textarea','model','link','action')
  for p in d for f in ((p.get('config_schema') or {}).get('fields') or []))" "$body"

# 4) enable/disable 往返（禁用再启用，恢复原状）
first=$(echo "$body" | J "d[0]['name']" | tr -d '"')
body=$(curl -s -X POST -H "$H" "$B/api/plugins/$first/disable")
assert "POST disable ok" "d.get('ok')" "$body"
body=$(curl -s -X POST -H "$H" "$B/api/plugins/$first/enable")
assert "POST enable ok" "d.get('ok')" "$body"

# 5) action（schema 校验语义）
body=$(curl -s -X POST -H "$H" "$B/api/plugins/$first/action" -H 'content-type: application/json' -d '{"field_key":"__nope__"}')
assert "POST action（字段不存在→抛错语义）" "'ok' not in d or d.get('ok') is False or d.get('stub')" "$body"

# 6) uninstall 未实现语义
body=$(curl -s -X DELETE -H "$H" "$B/api/plugins/$first")
assert "DELETE plugins not_implemented" "d.get('code')=='not_implemented'" "$body"

# 7) context/usage 形状 {token_count,limit}（ContextUpdatedPayload）
body=$(curl -s -H "$H" "$B/api/context/usage")
assert "GET /api/context/usage 形状" "'token_count' in d and 'limit' in d" "$body"

# 8) input-history 裸数组（InputHistoryItem[]）
body=$(curl -s -H "$H" "$B/api/input-history")
assert "GET /api/input-history 数组" "isinstance(d, list)" "$body"

# 9) coding-plan/usage 占位结构
body=$(curl -s -H "$H" "$B/api/coding-plan/usage")
assert "GET /api/coding-plan/usage 占位注明" "'note' in d" "$body"

# 10) user/profile
body=$(curl -s -H "$H" "$B/api/user/profile")
assert "GET /api/user/profile name" "isinstance(d.get('name'), str) and d.get('name')" "$body"

# 11) UI 槽清单四端点
body=$(curl -s -H "$H" "$B/api/ui/sidebar-items")
assert "GET /api/ui/sidebar-items item_id/label/group/pinned" "isinstance(d.get('items'), list) and all(
  set(['item_id','label','group','pinned']) <= set(i) for i in d['items'])" "$body"
sid=$(echo "$body" | J "d['items'][0]['item_id']" | tr -d '"' 2>/dev/null)
if [ -n "$sid" ]; then
  body=$(curl -s -X PUT -H "$H" "$B/api/ui/pinned" -H 'content-type: application/json' \
    -d "{\"key\":\"ui.sidebar.pinned.$sid\",\"value\":true}")
  assert "PUT /api/ui/pinned 持久化" "d.get('ok')" "$body"
fi
body=$(curl -s -H "$H" "$B/api/ui/context-menu")
assert "GET /api/ui/context-menu items" "'items' in d" "$body"
body=$(curl -s -H "$H" "$B/api/ui/plugin-windows")
assert "GET /api/ui/plugin-windows window_id/title" "'items' in d and all('window_id' in i for i in d['items'])" "$body"

# welcome-tabs：用清单里的插件名逐个查（无注册时空数组也过）
pname=$(echo "$(curl -s -H "$H" "$B/api/plugins")" | J "d[0]['name']" | tr -d '"')
body=$(curl -s -H "$H" "$B/api/plugins/$pname/welcome-tabs")
assert "GET /api/plugins/{name}/welcome-tabs items" "'items' in d" "$body"

echo "──────"
if [ "$fail" -eq 0 ]; then
  echo "PASS: M3a 断言清单全绿"
  exit 0
fi
echo "FAIL: M3a 存在未过断言"
exit 1
