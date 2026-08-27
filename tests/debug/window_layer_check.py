# 外部检查：列出目标进程所有窗口的 layer（0=普通窗口，非0=floating/utility）
import sys
from Quartz import (
    CGWindowListCopyWindowInfo,
    kCGNullWindowID,
    kCGWindowListOptionAll,
    kCGWindowListOptionOnScreenOnly,
)

owner_kw = sys.argv[1] if len(sys.argv) > 1 else "python"
infos = CGWindowListCopyWindowInfo(kCGWindowListOptionOnScreenOnly, kCGNullWindowID)
for i in infos:
    owner = i.get("kCGWindowOwnerName", "")
    if owner_kw.lower() not in owner.lower():
        continue
    name = i.get("kCGWindowName", "") or ""
    layer = i.get("kCGWindowLayer", -1)
    bounds = i.get("kCGWindowBounds", {})
    print(f"owner={owner} name={name!r} layer={layer} bounds={dict(bounds)}")
