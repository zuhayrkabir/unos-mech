"""Build grid.bin from the arena: 2 m cells with ground height, obstacle interval, and walk surface.
Run: python3 prep.py <arena_dir> <out_file>"""
import array, json, math, struct, sys

arena, out = sys.argv[1], sys.argv[2]
w = json.load(open(f"{arena}/v2/world.json"))
T = w["terrain"]
tnx, tny, tminx, tminy = T["nx"], T["ny"], T["minX"], T["minY"]
ter = array.array("h"); ter.frombytes(open(f"{arena}/v2/terrain.i16", "rb").read())
if sys.byteorder != "little": ter.byteswap()

def tground(x, y):
    fx = min(max(x - tminx, 0), tnx - 1.001); fy = min(max(y - tminy, 0), tny - 1.001)
    i, j = int(fx), int(fy); u, v = fx - i, fy - j
    a = ter[j*tnx+i]; b = ter[j*tnx+i+1]; c = ter[(j+1)*tnx+i]; d = ter[(j+1)*tnx+i+1]
    return ((a*(1-u)+b*u)*(1-v) + (c*(1-u)+d*u)*v) / 100.0

C = 2.0
NX = int(math.ceil((tnx - 1) / C)); NY = int(math.ceil((tny - 1) / C))
N = NX * NY
ground = [0.0]*N
for j in range(NY):
    for i in range(NX):
        x = tminx + (i+.5)*C; y = tminy + (j+.5)*C
        ground[j*NX+i] = tground(x, y)
NONE = 32767
lo = [NONE]*N; hi = [-NONE]*N
bridge_top = [None]*N; bridge_base = [None]*N
blocked = bytearray(N)

def pip(x, y, r):
    ins = False; n = len(r)//2
    jx, jy = r[-2], r[-1]
    for k in range(n):
        ix, iy = r[2*k], r[2*k+1]
        if (iy > y) != (jy > y) and x < (jx-ix)*(y-iy)/(jy-iy) + ix: ins = not ins
        jx, jy = ix, iy
    return ins

for p in w["prisms"]:
    r = p["ring"]; xs = r[0::2]; ys = r[1::2]
    i0 = max(0, int((min(xs)-tminx)/C)); i1 = min(NX-1, int((max(xs)-tminx)/C))
    j0 = max(0, int((min(ys)-tminy)/C)); j1 = min(NY-1, int((max(ys)-tminy)/C))
    for j in range(j0, j1+1):
        y = tminy + (j+.5)*C
        for i in range(i0, i1+1):
            x = tminx + (i+.5)*C
            if not pip(x, y, r): continue
            if any(pip(x, y, h) for h in p.get("holes") or []): continue
            k = j*NX+i
            b, t = p["base"], p["top"]
            lo[k] = min(lo[k], int(b*100)); hi[k] = max(hi[k], int(math.ceil(t*100)))
            g = ground[k]
            if p["kind"] == "bridge":
                if b < g + 9:
                    bridge_top[k] = max(bridge_top[k] or -1e9, t)
            else:
                if b < g + 9 and t > g + 1.2: blocked[k] = 1
surf = array.array("h", [0]*N)
for k in range(N):
    s = ground[k]
    if bridge_top[k] is not None and not blocked[k]: s = max(s, bridge_top[k])
    elif hi[k] > -NONE and not blocked[k] and lo[k]/100 < ground[k] + 9: s = max(s, hi[k]/100)
    surf[k] = int(round(s*100))
g16 = array.array("h", [int(round(g*100)) for g in ground])
lo16 = array.array("h", lo); hi16 = array.array("h", hi)
hdr = json.dumps({"nx": NX, "ny": NY, "cell": C, "minx": tminx, "miny": tminy}).encode()
with open(out, "wb") as f:
    f.write(struct.pack("<I", len(hdr))); f.write(hdr)
    for a in (g16, lo16, hi16, surf): f.write(a.tobytes())
    f.write(bytes(blocked))
print("grid", NX, NY, "blocked", sum(blocked), "of", N, file=sys.stderr)
