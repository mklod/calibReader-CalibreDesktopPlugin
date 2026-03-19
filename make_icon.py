"""Generate images/icon.png — a circular-arrows sync icon for the CalibReader Sync plugin."""
import struct, zlib, math, os

SIZE  = 64
BLUE  = (34, 138, 230, 255)

def make_png(img, w, h):
    def chunk(tag, data):
        c = tag + data
        return struct.pack('>I', len(data)) + c + struct.pack('>I', zlib.crc32(c) & 0xffffffff)
    raw = b''
    for y in range(h):
        raw += b'\x00'
        for x in range(w):
            raw += bytes(img[y][x])
    ihdr = struct.pack('>IIBBBBB', w, h, 8, 6, 0, 0, 0)
    out  = b'\x89PNG\r\n\x1a\n'
    out += chunk(b'IHDR', ihdr)
    out += chunk(b'IDAT', zlib.compress(raw, 9))
    out += chunk(b'IEND', b'')
    return out

img = [[(0, 0, 0, 0)] * SIZE for _ in range(SIZE)]

def paint(x, y, color, alpha):
    if 0 <= x < SIZE and 0 <= y < SIZE and alpha > 0:
        r, g, b, _ = color
        img[y][x] = (r, g, b, min(255, int(alpha)))

cx, cy = SIZE / 2.0, SIZE / 2.0

# 80% of previous size (was 0.38 / 0.25)
R_O   = SIZE * 0.304   # outer radius
R_I   = SIZE * 0.200   # inner radius
R_MID = (R_O + R_I) / 2.0
RING_W = R_O - R_I

# ── ring arcs ─────────────────────────────────────────────────────────────────
for y in range(SIZE):
    for x in range(SIZE):
        dx = x - cx + 0.5
        dy = y - cy + 0.5
        dist = math.sqrt(dx * dx + dy * dy)
        if dist < R_I - 1.5 or dist > R_O + 1.5:
            continue
        a_deg = math.degrees(math.atan2(dy, dx)) % 360
        # Arc 1: 30° → 165°   Arc 2: 210° → 345°
        in_arc = (30 <= a_deg <= 165) or (210 <= a_deg <= 345)
        if not in_arc:
            continue
        d_edge = min(R_O - dist, dist - R_I)
        aa = max(0.0, min(1.0, d_edge + 0.75))
        paint(x, y, BLUE, aa * 255)

# ── arrowheads — sharp pointed triangles ──────────────────────────────────────
# Key: AL must be much larger than AW for a sharp point (ratio ~3:1)
AW = RING_W * 0.95   # half-width of arrowhead base (≈ ring width)
AL = RING_W * 2.8    # length of arrowhead — 3× base width = sharp point

def fill_triangle(pts, color):
    xs = [p[0] for p in pts]; ys = [p[1] for p in pts]
    x0 = max(0, int(min(xs)) - 1); x1 = min(SIZE - 1, int(max(xs)) + 1)
    y0 = max(0, int(min(ys)) - 1); y1 = min(SIZE - 1, int(max(ys)) + 1)
    def cross(o, a, b): return (a[0]-o[0])*(b[1]-o[1]) - (a[1]-o[1])*(b[0]-o[0])
    p0, p1, p2 = pts
    for y in range(y0, y1 + 1):
        for x in range(x0, x1 + 1):
            p = (x + 0.5, y + 0.5)
            d0 = cross(p0, p1, p); d1 = cross(p1, p2, p); d2 = cross(p2, p0, p)
            if (d0 >= 0 and d1 >= 0 and d2 >= 0) or (d0 <= 0 and d1 <= 0 and d2 <= 0):
                img[y][x] = color

for tip_deg in [165, 345]:
    A    = math.radians(tip_deg)
    tang = (-math.sin(A), math.cos(A))    # clockwise tangent
    norm = ( math.cos(A), math.sin(A))    # radial outward
    tip  = (cx + R_MID * norm[0], cy + R_MID * norm[1])
    # Base centre is AL behind the tip (against the tangent direction)
    bc   = (tip[0] - tang[0] * AL, tip[1] - tang[1] * AL)
    p0   = (bc[0] + norm[0] * AW, bc[1] + norm[1] * AW)
    p1   = (bc[0] - norm[0] * AW, bc[1] - norm[1] * AW)
    fill_triangle([p0, p1, tip], BLUE)

os.makedirs('images', exist_ok=True)
with open('images/icon.png', 'wb') as f:
    f.write(make_png(img, SIZE, SIZE))
print('Created images/icon.png — preview at CalibReaderSync/images/icon.png')
