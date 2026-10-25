import collections
import os

from PIL import Image

base = os.path.join(os.environ["TEMP"], "drifox_paste")
for name in ["截图_1025_183100.png", "截图_1025_183131.png"]:
    p = os.path.join(base, name)
    im = Image.open(p).convert("RGB")
    w, h = im.size
    px = im.load()
    print("===", name, im.size)
    seen = [[False] * w for _ in range(h)]
    best = None
    for y in range(h):
        for x in range(w):
            if seen[y][x]:
                continue
            r, g, b = px[x, y]
            if not (r > 240 and g > 240 and b > 240):
                seen[y][x] = True
                continue
            q = collections.deque([(x, y)])
            seen[y][x] = True
            n = 0
            minx = maxx = x
            miny = maxy = y
            while q:
                cx, cy = q.popleft()
                n += 1
                minx = min(minx, cx)
                maxx = max(maxx, cx)
                miny = min(miny, cy)
                maxy = max(maxy, cy)
                for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1)):
                    nx, ny = cx + dx, cy + dy
                    if 0 <= nx < w and 0 <= ny < h and not seen[ny][nx]:
                        rr, gg, bb = px[nx, ny]
                        seen[ny][nx] = True
                        if rr > 240 and gg > 240 and bb > 240:
                            q.append((nx, ny))
            if best is None or n > best[0]:
                best = (n, minx, miny, maxx, maxy)
    n, minx, miny, maxx, maxy = best
    bw = maxx - minx + 1
    bh = maxy - miny + 1
    print(f"  blob n={n} bbox=({minx},{miny})-({maxx},{maxy}) size={bw}x{bh}")
    print(f"  logical@1.5x = {bw / 1.5:.0f}x{bh / 1.5:.0f}")
