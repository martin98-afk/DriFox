import io
import json

p = "plugins/system-skills/skills/drifox-dev/state/state.json"
d = json.load(io.open(p, encoding="utf-8"))
ps = d.get("known_pitfalls", [])
print("total", len(ps))
for it in ps:
    s = json.dumps(it, ensure_ascii=False)
    if any(k in s for k in ["工具", "tool", "残留", "更新"]):
        print("---")
        print(json.dumps(it, ensure_ascii=False, indent=1)[:1200])
