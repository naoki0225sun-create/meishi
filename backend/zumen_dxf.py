"""DXF（2DのCAD図面）を画像にする（phase30）

外部のライブラリは使わず、図面でよく使う要素だけを描く：
LINE・LWPOLYLINE・POLYLINE・CIRCLE・ARC・ELLIPSE・SPLINE・TEXT・MTEXT・INSERT（ブロック）・DIMENSION・SOLID
画像はPDFのページと同じ扱いで保存するので、縮小画像・差分・似た図面・印刷・共有・AIの読み取りがそのまま使える。
"""
import math
import os
import re

from PIL import Image, ImageDraw, ImageFont

MAX_ENT = 200_000
FONT_PATHS = [
    "/usr/share/fonts/opentype/ipafont-gothic/ipag.ttf",
    "/usr/share/fonts/truetype/fonts-japanese-gothic.ttf",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/noto-cjk/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
]
_FONT = {}


def _font(px: int):
    px = max(8, min(400, int(px)))
    if px in _FONT:
        return _FONT[px]
    f = None
    for p in FONT_PATHS:
        if os.path.exists(p):
            try:
                f = ImageFont.truetype(p, px)
                break
            except Exception:
                continue
    _FONT[px] = f or ImageFont.load_default()
    return _FONT[px]


def decode(data: bytes) -> str:
    head = data[:20000]
    if b"ANSI_932" in head:
        return data.decode("cp932", "replace")
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return data.decode("cp932", "replace")


def pairs(text: str):
    lines = text.splitlines()
    for i in range(0, len(lines) - 1, 2):
        try:
            code = int(lines[i].strip())
        except ValueError:
            continue
        yield code, lines[i + 1].rstrip("\r")


def parse(text: str) -> dict:
    """{'blocks': {名前: {'base': (x, y), 'ents': [...]}}, 'ents': [...]}。要素は {'type', 'g': [(code, value)]}"""
    blocks, ents = {}, []
    section, cur, block, n = None, None, None, 0
    expect_name = False
    for code, val in pairs(text):
        if code == 0:
            if cur is not None:
                if block is not None and section == "BLOCKS":
                    block["ents"].append(cur)
                elif section == "ENTITIES":
                    ents.append(cur)
                cur = None
            n += 1
            if n > MAX_ENT:
                break
            v = val.strip()
            if v == "SECTION":
                expect_name = True
                continue
            if v == "ENDSEC":
                section = None
                continue
            if v == "EOF":
                break
            if section == "BLOCKS" and v == "BLOCK":
                block = {"name": None, "base": (0.0, 0.0), "ents": [], "_hdr": True}
                continue
            if section == "BLOCKS" and v == "ENDBLK":
                if block and block.get("name"):
                    blocks[block["name"]] = block
                block = None
                continue
            if section in ("ENTITIES", "BLOCKS"):
                if block is not None:
                    block["_hdr"] = False
                cur = {"type": v, "g": []}
            continue
        if expect_name and code == 2:
            section = val.strip()
            expect_name = False
            continue
        if cur is not None:
            cur["g"].append((code, val))
        elif block is not None and block.get("_hdr"):
            if code == 2 and block["name"] is None:
                block["name"] = val.strip()
            elif code == 10:
                block["base"] = (_f(val), block["base"][1])
            elif code == 20:
                block["base"] = (block["base"][0], _f(val))
    if cur is not None and section == "ENTITIES":
        ents.append(cur)
    return {"blocks": blocks, "ents": ents}


def _f(v, d=0.0) -> float:
    try:
        x = float(v)
        return x if math.isfinite(x) else d
    except (TypeError, ValueError):
        return d


def _get(g, code, d=None):
    for c, v in g:
        if c == code:
            return v
    return d


def _all(g, code):
    return [v for c, v in g if c == code]


def _xy(g, cx=10, cy=20):
    return (_f(_get(g, cx)), _f(_get(g, cy)))


def clean_text(s: str) -> str:
    s = s.replace("%%c", "φ").replace("%%C", "φ").replace("%%d", "°").replace("%%D", "°").replace("%%p", "±").replace("%%P", "±")
    s = re.sub(r"\\[Pp]", "\n", s)
    s = re.sub(r"\\[A-Za-z][^;\\{}]*;", "", s)
    s = re.sub(r"\\[LlOoKk]", "", s)
    s = s.replace("{", "").replace("}", "").replace("\\~", " ")
    return s.strip()


def _arc_pts(cx, cy, r, a0, a1, step_deg=6):
    while a1 <= a0:
        a1 += 360
    n = max(2, int((a1 - a0) / step_deg) + 1)
    return [(cx + r * math.cos(math.radians(a0 + (a1 - a0) * i / (n - 1))), cy + r * math.sin(math.radians(a0 + (a1 - a0) * i / (n - 1)))) for i in range(n)]


def _bulge(p0, p1, b):
    """LWPOLYLINE のふくらみ（bulge）を弧の点列にする"""
    if abs(b) < 1e-9:
        return [p1]
    (x0, y0), (x1, y1) = p0, p1
    d = math.hypot(x1 - x0, y1 - y0)
    if d < 1e-12:
        return [p1]
    theta = 4 * math.atan(b)
    r = d / (2 * math.sin(theta / 2))
    mx, my = (x0 + x1) / 2, (y0 + y1) / 2
    h = r * math.cos(theta / 2)
    nx, ny = -(y1 - y0) / d, (x1 - x0) / d
    cx, cy = mx + nx * h * (1 if b > 0 else -1) * (1 if r > 0 else -1), my + ny * h * (1 if b > 0 else -1) * (1 if r > 0 else -1)
    a0 = math.atan2(y0 - cy, x0 - cx)
    n = max(2, int(abs(math.degrees(theta)) / 8) + 1)
    return [(cx + abs(r) * math.cos(a0 + theta * i / (n - 1)), cy + abs(r) * math.sin(a0 + theta * i / (n - 1))) for i in range(1, n)]


def flatten(doc: dict) -> tuple:
    """要素を (線の点列のリスト, 文字のリスト) にする。ブロックは展開する"""
    lines, texts = [], []

    def tf(m, p):
        a, b, c, d, e, f = m
        return (a * p[0] + b * p[1] + e, c * p[0] + d * p[1] + f)

    def mul(m1, m2):
        a1, b1, c1, d1, e1, f1 = m1
        a2, b2, c2, d2, e2, f2 = m2
        return (a1 * a2 + b1 * c2, a1 * b2 + b1 * d2, c1 * a2 + d1 * c2, c1 * b2 + d1 * d2, a1 * e2 + b1 * f2 + e1, c1 * e2 + d1 * f2 + f1)

    def scale_of(m):
        return math.sqrt(abs(m[0] * m[3] - m[1] * m[2])) or 1

    def walk(ents, m, depth):
        i = 0
        while i < len(ents):
            e = ents[i]
            t, g = e["type"], e["g"]
            i += 1
            if _get(g, 67) == "1":   # 紙空間は描かない
                continue
            if t == "LINE":
                lines.append([tf(m, _xy(g)), tf(m, _xy(g, 11, 21))])
            elif t == "LWPOLYLINE":
                pts, bul = [], []
                cur = None
                for c, v in g:
                    if c == 10:
                        cur = [_f(v), 0.0]
                    elif c == 20 and cur is not None:
                        cur[1] = _f(v)
                        pts.append(tuple(cur))
                        bul.append(0.0)
                    elif c == 42 and bul:
                        bul[-1] = _f(v)
                if len(pts) < 2:
                    continue
                closed = int(_f(_get(g, 70), 0)) & 1
                seq = [pts[0]]
                rng = range(len(pts)) if closed else range(len(pts) - 1)
                for k in rng:
                    seq += _bulge(pts[k], pts[(k + 1) % len(pts)], bul[k])
                lines.append([tf(m, p) for p in seq])
            elif t == "POLYLINE":
                closed = int(_f(_get(g, 70), 0)) & 1
                pts = []
                while i < len(ents) and ents[i]["type"] == "VERTEX":
                    pts.append(_xy(ents[i]["g"]))
                    i += 1
                if i < len(ents) and ents[i]["type"] == "SEQEND":
                    i += 1
                if len(pts) >= 2:
                    lines.append([tf(m, p) for p in pts + ([pts[0]] if closed else [])])
            elif t == "CIRCLE":
                cx, cy = _xy(g)
                lines.append([tf(m, p) for p in _arc_pts(cx, cy, _f(_get(g, 40)), 0, 360)])
            elif t == "ARC":
                cx, cy = _xy(g)
                lines.append([tf(m, p) for p in _arc_pts(cx, cy, _f(_get(g, 40)), _f(_get(g, 50)), _f(_get(g, 51)))])
            elif t == "ELLIPSE":
                cx, cy = _xy(g)
                mx, my = _xy(g, 11, 21)
                ratio = _f(_get(g, 40), 1)
                t0, t1 = _f(_get(g, 41), 0), _f(_get(g, 42), 2 * math.pi)
                while t1 <= t0:
                    t1 += 2 * math.pi
                n = max(8, int((t1 - t0) / (2 * math.pi) * 72))
                pts = []
                for k in range(n + 1):
                    a = t0 + (t1 - t0) * k / n
                    pts.append((cx + mx * math.cos(a) - my * ratio * math.sin(a), cy + my * math.cos(a) + mx * ratio * math.sin(a)))
                lines.append([tf(m, p) for p in pts])
            elif t == "SPLINE":
                xs, ys = _all(g, 11), _all(g, 21)
                if len(xs) < 2:
                    xs, ys = _all(g, 10), _all(g, 20)
                pts = [(_f(x), _f(y)) for x, y in zip(xs, ys)]
                if len(pts) >= 2:
                    lines.append([tf(m, p) for p in pts])
            elif t in ("SOLID", "3DFACE", "TRACE"):
                pts = [_xy(g, 10 + k, 20 + k) for k in range(4)]
                if t == "SOLID":
                    pts = [pts[0], pts[1], pts[3], pts[2]]
                lines.append([tf(m, p) for p in pts + [pts[0]]])
            elif t in ("TEXT", "MTEXT", "ATTRIB"):
                s = _get(g, 1, "") or ""
                if t == "MTEXT":
                    s = "".join(_all(g, 3)) + s
                s = clean_text(s)
                if not s:
                    continue
                p = tf(m, _xy(g))
                if t in ("TEXT", "ATTRIB") and (_get(g, 72) not in (None, "0") or _get(g, 73) not in (None, "0")) and _get(g, 11) is not None:
                    p = tf(m, _xy(g, 11, 21))
                rot = _f(_get(g, 50), 0)
                if t == "MTEXT" and _get(g, 11) is not None:
                    rot = math.degrees(math.atan2(_f(_get(g, 21)), _f(_get(g, 11))))
                rot += math.degrees(math.atan2(m[2], m[0]))
                texts.append({"p": p, "h": _f(_get(g, 40), 2.5) * scale_of(m), "s": s, "rot": rot, "mtext": t == "MTEXT"})
            elif t in ("INSERT", "DIMENSION") and depth < 6:
                name = (_get(g, 2) or "").strip()
                blk = doc["blocks"].get(name)
                if not blk:
                    continue
                if t == "DIMENSION":
                    walk(blk["ents"], m, depth + 1)
                    continue
                ix, iy = _xy(g)
                sx, sy = _f(_get(g, 41), 1) or 1, _f(_get(g, 42), 1) or 1
                r = math.radians(_f(_get(g, 50), 0))
                bx, by = blk["base"]
                ins = (sx * math.cos(r), -sy * math.sin(r), sx * math.sin(r), sy * math.cos(r), ix, iy)
                base = (1, 0, 0, 1, -bx, -by)
                walk(blk["ents"], mul(m, mul(ins, base)), depth + 1)
                # ブロックの後ろに付く属性（ATTRIB）
                while i < len(ents) and ents[i]["type"] in ("ATTRIB", "SEQEND"):
                    if ents[i]["type"] == "ATTRIB":
                        walk([ents[i]], m, depth + 1)
                    i += 1

    walk(doc["ents"], (1, 0, 0, 1, 0, 0), 0)
    return lines, texts


def _extent(lines, texts):
    xs = sorted(x for ln in lines for x, _ in ln) + [t["p"][0] for t in texts]
    ys = sorted(y for ln in lines for _, y in ln) + [t["p"][1] for t in texts]
    if not xs:
        return None
    xs.sort()
    ys.sort()
    k = len(xs)
    lo, hi = int(k * 0.002), max(0, int(k * 0.998) - 1)   # 遠くに飛んだ点で全体が小さくならないように
    x0, x1, y0, y1 = xs[lo], xs[hi], ys[lo], ys[hi]
    if x1 - x0 < 1e-9:
        x1 = x0 + 1
    if y1 - y0 < 1e-9:
        y1 = y0 + 1
    return x0, y0, x1, y1


def render(data: bytes, px: int = 2400):
    """DXF を白地の画像にする。(PIL.Image, 文字) を返す。描けないときは (None, '')"""
    doc = parse(decode(data))
    lines, texts = flatten(doc)
    ext = _extent(lines, texts)
    if not ext:
        return None, ""
    x0, y0, x1, y1 = ext
    w, h = x1 - x0, y1 - y0
    pad = 0.03
    k = px / (max(w, h) * (1 + 2 * pad))
    W, H = max(200, int(w * k * (1 + 2 * pad))), max(200, int(h * k * (1 + 2 * pad)))
    ox, oy = x0 - w * pad, y1 + h * pad
    im = Image.new("RGB", (W, H), "white")
    dr = ImageDraw.Draw(im)
    tr = lambda p: ((p[0] - ox) * k, (oy - p[1]) * k)
    lw = max(1, round(px / 1600))
    for ln in lines:
        pts = [tr(p) for p in ln]
        if len(pts) >= 2:
            dr.line(pts, fill=(20, 24, 32), width=lw, joint="curve")
    for t in texts:
        size = t["h"] * k * (1.25 if not t["mtext"] else 1.15)
        if size < 6:
            continue
        f = _font(size)
        x, y = tr(t["p"])
        lines_s = t["s"].split("\n")[:30]
        if abs(t["rot"]) % 360 < 0.5:
            yy = y - size if not t["mtext"] else y
            for s in lines_s:
                dr.text((x, yy), s, fill=(20, 24, 32), font=f)
                yy += size * 1.3
        else:
            tw = int(max(dr.textlength(s, font=f) for s in lines_s) + 4)
            th = int(size * 1.35 * len(lines_s) + 4)
            if tw * th > 4_000_000:
                continue
            tim = Image.new("L", (tw, th), 0)
            td = ImageDraw.Draw(tim)
            for j, s in enumerate(lines_s):
                td.text((2, 2 + j * size * 1.3), s, fill=255, font=f)
            rim = tim.rotate(t["rot"], expand=True, resample=Image.BICUBIC)
            im.paste((20, 24, 32), (int(x), int(y - rim.size[1])), rim)
    text = "\n".join(t["s"] for t in texts)[:20000]
    return im, text
