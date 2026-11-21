"""Static map model: 2 m grid of ground height, solid intervals and walkability; LOS and A* on a 4 m grid."""
import array
import heapq
import json
import math
import os
import struct
import sys

NONE = 32767


def _build(arena):
    w = json.load(open(os.path.join(arena, "v2", "world.json")))
    T = w["terrain"]
    tnx, tny, tminx, tminy = T["nx"], T["ny"], T["minX"], T["minY"]
    ter = array.array("h")
    ter.frombytes(open(os.path.join(arena, "v2", "terrain.i16"), "rb").read())
    if sys.byteorder != "little":
        ter.byteswap()
    C = 2.0
    NX = int(math.ceil((tnx - 1) / C))
    NY = int(math.ceil((tny - 1) / C))
    N = NX * NY
    ground = [0.0] * N
    for j in range(NY):
        fy = min((j + .5) * C, tny - 1.001)
        jj = int(fy); v = fy - jj
        r0 = jj * tnx; r1 = r0 + tnx
        for i in range(NX):
            fx = min((i + .5) * C, tnx - 1.001)
            ii = int(fx); u = fx - ii
            a = ter[r0 + ii]; b = ter[r0 + ii + 1]; c = ter[r1 + ii]; d = ter[r1 + ii + 1]
            ground[j * NX + i] = ((a * (1 - u) + b * u) * (1 - v) + (c * (1 - u) + d * u) * v) / 100.0
    lo = [NONE] * N
    hi = [-NONE] * N
    btop = [None] * N
    blocked = bytearray(N)

    def pip(x, y, r):
        ins = False
        n = len(r) // 2
        jx, jy = r[-2], r[-1]
        for k in range(n):
            ix, iy = r[2 * k], r[2 * k + 1]
            if (iy > y) != (jy > y) and x < (jx - ix) * (y - iy) / (jy - iy) + ix:
                ins = not ins
            jx, jy = ix, iy
        return ins

    for p in w["prisms"]:
        r = p["ring"]
        xs = r[0::2]; ys = r[1::2]
        i0 = max(0, int((min(xs) - tminx) / C)); i1 = min(NX - 1, int((max(xs) - tminx) / C))
        j0 = max(0, int((min(ys) - tminy) / C)); j1 = min(NY - 1, int((max(ys) - tminy) / C))
        holes = p.get("holes") or []
        b, t = p["base"], p["top"]
        isb = p["kind"] == "bridge"
        for j in range(j0, j1 + 1):
            y = tminy + (j + .5) * C
            for i in range(i0, i1 + 1):
                x = tminx + (i + .5) * C
                if not pip(x, y, r):
                    continue
                if holes and any(pip(x, y, h) for h in holes):
                    continue
                k = j * NX + i
                lo[k] = min(lo[k], int(b * 100)); hi[k] = max(hi[k], int(math.ceil(t * 100)))
                g = ground[k]
                if isb:
                    if b < g + 9:
                        btop[k] = max(btop[k] if btop[k] is not None else -1e9, t)
                elif b < g + 9 and t > g + 1.2:
                    blocked[k] = 1
    surf = [0] * N
    for k in range(N):
        s = ground[k]
        if not blocked[k]:
            if btop[k] is not None:
                s = max(s, btop[k])
            elif hi[k] > -NONE and lo[k] / 100 < ground[k] + 9:
                s = max(s, hi[k] / 100)
        surf[k] = int(round(s * 100))
    g16 = array.array("h", [int(round(g * 100)) for g in ground])
    return dict(nx=NX, ny=NY, cell=C, minx=tminx, miny=tminy), g16, array.array("h", lo), array.array("h", hi), array.array("h", surf), blocked


def save(path, hdr, g, lo, hi, surf, blocked):
    h = json.dumps(hdr).encode()
    with open(path, "wb") as f:
        f.write(struct.pack("<I", len(h))); f.write(h)
        for a in (g, lo, hi, surf):
            f.write(a.tobytes())
        f.write(bytes(blocked))


def load(path):
    data = open(path, "rb").read()
    n = struct.unpack("<I", data[:4])[0]
    hdr = json.loads(data[4:4 + n])
    N = hdr["nx"] * hdr["ny"]
    off = 4 + n
    arrs = []
    for _ in range(4):
        a = array.array("h"); a.frombytes(data[off:off + 2 * N]); off += 2 * N
        arrs.append(a)
    return (hdr, *arrs, bytearray(data[off:off + N]))


class World:
    def __init__(self, arena=None, fallback=None):
        parts = None
        if arena:
            try:
                parts = _build(arena)
            except Exception as e:  # noqa
                print("map build failed:", e, file=sys.stderr)
        if parts is None:
            parts = load(fallback)
        hdr, self.g, self.lo, self.hi, self.surf, self.blocked = parts
        self.NX, self.NY, self.C = hdr["nx"], hdr["ny"], hdr["cell"]
        self.minx, self.miny = hdr["minx"], hdr["miny"]
        self.maxx = self.minx + self.NX * self.C
        self.maxy = self.miny + self.NY * self.C
        # 4 m nav grid
        self.NX4, self.NY4 = self.NX // 2, self.NY // 2
        N4 = self.NX4 * self.NY4
        self.walk = bytearray(N4)
        self.s4 = [0.0] * N4
        NX = self.NX
        margin = 8  # cells of 4 m kept off the map edge
        for j in range(self.NY4):
            for i in range(self.NX4):
                k2 = (2 * j) * NX + 2 * i
                ks = (k2, k2 + 1, k2 + NX, k2 + NX + 1)
                ok = not any(self.blocked[q] for q in ks)
                if i < margin // 2 or j < margin // 2 or i >= self.NX4 - margin // 2 or j >= self.NY4 - margin // 2:
                    ok = False
                k4 = j * self.NX4 + i
                self.walk[k4] = 1 if ok else 0
                self.s4[k4] = sum(self.surf[q] for q in ks) / 400.0
        # soften: cells next to walls get a cost penalty (keep paths off building edges)
        self.cost = bytearray(N4)
        for j in range(1, self.NY4 - 1):
            for i in range(1, self.NX4 - 1):
                k = j * self.NX4 + i
                if self.walk[k]:
                    near = 0
                    for dj in (-1, 0, 1):
                        for di in (-1, 0, 1):
                            if not self.walk[k + dj * self.NX4 + di]:
                                near += 1
                    self.cost[k] = near

    # ---------- queries ----------
    def k2(self, x, y):
        i = int((x - self.minx) / self.C); j = int((y - self.miny) / self.C)
        if i < 0 or j < 0 or i >= self.NX or j >= self.NY:
            return -1
        return j * self.NX + i

    def ground(self, x, y):
        k = self.k2(x, y)
        return self.g[k] / 100.0 if k >= 0 else 0.0

    def surface(self, x, y):
        k = self.k2(x, y)
        return self.surf[k] / 100.0 if k >= 0 else 0.0

    def los(self, a, b, step=1.6, skip_end=2.5):
        """True if the straight segment a->b clears buildings and terrain (trees ignored)."""
        dx, dy, dz = b[0] - a[0], b[1] - a[1], b[2] - a[2]
        L = math.sqrt(dx * dx + dy * dy + dz * dz)
        if L < 1e-6:
            return True
        n = int((L - skip_end) / step)
        if n <= 0:
            return True
        sx, sy, sz = dx / L * step, dy / L * step, dz / L * step
        x, y, z = a[0] + sx * .5, a[1] + sy * .5, a[2] + sz * .5
        g, lo, hi = self.g, self.lo, self.hi
        minx, miny, C, NX, NY = self.minx, self.miny, self.C, self.NX, self.NY
        for _ in range(n):
            i = int((x - minx) / C); j = int((y - miny) / C)
            if 0 <= i < NX and 0 <= j < NY:
                k = j * NX + i
                zc = z * 100
                if zc < g[k] - 40:
                    return False
                if lo[k] <= zc <= hi[k]:
                    return False
            x += sx; y += sy; z += sz
        return True

    def k4(self, x, y):
        i = int((x - self.minx) / 4.0); j = int((y - self.miny) / 4.0)
        if i < 0 or j < 0 or i >= self.NX4 or j >= self.NY4:
            return -1
        return j * self.NX4 + i

    def c4(self, k):
        j, i = divmod(k, self.NX4)
        return (self.minx + (i + .5) * 4.0, self.miny + (j + .5) * 4.0)

    def walkable(self, x, y):
        k = self.k4(x, y)
        return k >= 0 and self.walk[k] == 1

    def nearest_walk(self, x, y, maxr=12):
        k = self.k4(x, y)
        if k >= 0 and self.walk[k]:
            return k
        i0 = int((x - self.minx) / 4.0); j0 = int((y - self.miny) / 4.0)
        best, bd = -1, 1e9
        for r in range(1, maxr + 1):
            for dj in range(-r, r + 1):
                for di in (-r, r) if abs(dj) != r else range(-r, r + 1):
                    i, j = i0 + di, j0 + dj
                    if 0 <= i < self.NX4 and 0 <= j < self.NY4:
                        q = j * self.NX4 + i
                        if self.walk[q]:
                            d = di * di + dj * dj
                            if d < bd:
                                bd, best = d, q
            if best >= 0:
                return best
        return -1

    def line_walk(self, a, b):
        """Straight ground line a->b stays on walkable cells with gentle steps."""
        dx, dy = b[0] - a[0], b[1] - a[1]
        L = math.hypot(dx, dy)
        n = max(1, int(L / 2.0))
        prev = None
        for s in range(n + 1):
            t = s / n
            k = self.k4(a[0] + dx * t, a[1] + dy * t)
            if k < 0 or not self.walk[k]:
                return False
            h = self.s4[k]
            if prev is not None and abs(h - prev) > 2.2:
                return False
            prev = h
        return True


class AStar:
    """Time-sliced A* on the 4 m grid."""

    def __init__(self, world, start, goal):
        self.w = world
        self.start, self.goal = start, goal
        self.open = [(0.0, 0.0, start)]
        self.came = {start: -1}
        self.gs = {start: 0.0}
        gx, gy = divmod(goal, world.NX4)
        self.gj, self.gi = gx, gy
        self.done = start == goal
        self.path = [start] if self.done else None
        self.expanded = 0

    def h(self, k):
        j, i = divmod(k, self.w.NX4)
        dx, dy = abs(i - self.gi), abs(j - self.gj)
        return (dx + dy) + (1.4142 - 2) * min(dx, dy)

    def step(self, budget):
        if self.done:
            return True
        w = self.w
        NX4 = w.NX4
        walk, s4, cost = w.walk, w.s4, w.cost
        op, came, gs = self.open, self.came, self.gs
        goal = self.goal
        for _ in range(budget):
            if not op:
                self.done = True
                self.path = None
                return True
            f, gk, k = heapq.heappop(op)
            if gk > gs.get(k, 1e18):
                continue
            if k == goal:
                path = []
                while k != -1:
                    path.append(k); k = came[k]
                self.path = path[::-1]
                self.done = True
                return True
            self.expanded += 1
            hk = s4[k]
            j, i = divmod(k, NX4)
            for di, dj, c in ((1, 0, 1.0), (-1, 0, 1.0), (0, 1, 1.0), (0, -1, 1.0),
                              (1, 1, 1.4142), (1, -1, 1.4142), (-1, 1, 1.4142), (-1, -1, 1.4142)):
                ii, jj = i + di, j + dj
                if ii < 0 or jj < 0 or ii >= NX4 or jj >= w.NY4:
                    continue
                q = jj * NX4 + ii
                if not walk[q]:
                    continue
                if di and dj and (not walk[j * NX4 + ii] or not walk[jj * NX4 + i]):
                    continue
                dh = abs(s4[q] - hk)
                if dh > 2.2:
                    continue
                ng = gk + c * (1.0 + 0.15 * cost[q]) + dh * 0.3
                if ng < gs.get(q, 1e18):
                    gs[q] = ng
                    came[q] = k
                    heapq.heappush(op, (ng + self.h(q), ng, q))
            if self.expanded > 60000:
                self.done = True
                self.path = None
                return True
        return False
