"""监控 release workflow 直到完成。完成后打印最终状态。"""
import json
import time
import urllib.request

API = "https://api.github.com/repos/martin98-afk/DriFox"
RUN_ID = 35074424065
INTERVAL = 60  # 秒
MAX_WAIT = 1800  # 30 分钟上限

start = time.time()
prev_summary = None
while True:
    elapsed = int(time.time() - start)
    if elapsed > MAX_WAIT:
        print(f"[monitor] 超时 {MAX_WAIT}s 仍未完成")
        break
    try:
        req = urllib.request.Request(
            f"{API}/actions/runs/{RUN_ID}/jobs",
            headers={"User-Agent": "release-monitor"},
        )
        jobs = json.loads(urllib.request.urlopen(req, timeout=20).read()).get("jobs", [])
        summary = []
        for j in jobs:
            summary.append(f"{j['name']}:{j['status'][:3]}:{j.get('conclusion') or '-'}")
        if summary != prev_summary:
            print(f"[{elapsed:4d}s] " + " | ".join(summary))
            prev_summary = summary

        run_req = urllib.request.Request(
            f"{API}/actions/runs/{RUN_ID}",
            headers={"User-Agent": "release-monitor"},
        )
        run = json.loads(urllib.request.urlopen(run_req, timeout=20).read())
        if run["status"] in ("completed", "failure"):
            print(f"[{elapsed:4d}s] workflow 最终状态: {run['status']} / {run.get('conclusion')}")
            break
    except Exception as e:
        print(f"[{elapsed:4d}s] query error: {e}")
    time.sleep(INTERVAL)
