# -*- coding: utf-8 -*-
"""RA2/YR 地图生成器：空白地图 + 出生点 + 对称矿田 + 预览图 + Digest

地图几何（实证自 不公平13.yrm，Size=0,0,110,110）:
  瓦片格集合为菱形: y 行 1..2W-1，行内连续；
    行长度 L(y) = 2y-1        (y <= W)
             = 2*(2W-y)       (y >  W)
    起始 x = W + 1 - (L+1)//2
  总格数 = W*(2W-1)
坐标换算: waypoint 值 = X*1000 + Y；overlay 网格索引 = x + y*512
"""
import base64
import math
import random

import mapio

THEATERS = ['TEMPERATE', 'SNOW', 'URBAN', 'NEWURBAN', 'DESERT', 'LUNAR']
THEATER_CN = {'TEMPERATE': '温带', 'SNOW': '雪地', 'URBAN': '城市',
              'NEWURBAN': '新城市', 'DESERT': '沙漠', 'LUNAR': '月球'}

# ---------------- overlay ID（随游戏环境变化，见 overlay_catalog） ----------------
# 矿/宝石的真实编号来自 rules(md).ini 的 [OverlayTypes]（1-based）：
#   本机「重聚」环境：矿 = TIB01..TIB20 = 105..124；宝石 = GEM01..GEM12 = 28..39
#   原版 YR：矿 = 102..121；宝石 = 27..38
ORE_IDS = list(range(102, 122))
GEM_IDS = list(range(27, 39))
ORE_ID = 112        # 默认矿（TIB08）
GEM_ID = 27         # 默认宝石
# 平地瓦片：theater INI [General] ClearTile = 0（tileset 号），对应瓦片索引 0
CLEAR_TILE = 0


def set_overlay_ids(ore_ids, gem_ids):
    """由调用方（读取游戏 OverlayTypes 后）注入真实编号"""
    global ORE_IDS, GEM_IDS, ORE_ID, GEM_ID
    if ore_ids:
        ORE_IDS = list(ore_ids)
        ORE_ID = ORE_IDS[min(7, len(ORE_IDS) - 1)]   # TIB08 位置，和真实地图一致
    if gem_ids:
        GEM_IDS = list(gem_ids)
        GEM_ID = GEM_IDS[min(2, len(GEM_IDS) - 1)]   # GEM03 位置


def ore_paint(cx, cy, r, rng, cell_ok):
    """在 (cx,cy) 刷一块矿田，返回 {cell: (id, 数据值)}
    数据值 = 矿石密度/帧号 0..11，真实地图多为 4..11（中心更满）"""
    out = {}
    ri = max(1, int(r))
    for dy in range(-ri, ri + 1):
        span = ri - abs(dy)
        for dx in range(-span, span + 1):
            x, y = cx + dx, cy + dy
            if not cell_ok(x, y):
                continue
            d = abs(dx) + abs(dy)
            # 边缘稀疏（低密度）、中心饱满
            if d > ri * 0.7:
                val = rng.choice([1, 3, 4, 6])
            elif d > ri * 0.4:
                val = rng.choice([4, 6, 7, 8, 10])
            else:
                val = rng.choice([8, 10, 11, 11])
            out[(x, y)] = (ORE_ID, val)
    return out


def gem_paint(cx, cy, r, rng, cell_ok):
    out = {}
    ri = max(1, int(r))
    for dy in range(-ri, ri + 1):
        span = ri - abs(dy)
        for dx in range(-span, span + 1):
            x, y = cx + dx, cy + dy
            if not cell_ok(x, y):
                continue
            out[(x, y)] = (GEM_ID, rng.choice([4, 7, 10, 11]))
    return out



# ---------------- 几何 ----------------
def row_span(y, w):
    """第 y 行的 (起始x, 长度)"""
    L = (2 * y - 1) if y <= w else 2 * (2 * w - y)
    return w + 1 - (L + 1) // 2, L


def diamond_cells(w):
    cells = []
    for y in range(1, 2 * w):
        x0, L = row_span(y, w)
        for x in range(x0, x0 + L):
            cells.append((x, y))
    return cells


def cell_in_map(x, y, w):
    if not (1 <= x <= 2 * w - 1 and 1 <= y <= 2 * w - 1):
        return False
    x0, L = row_span(y, w)
    return x0 <= x < x0 + L


def player_starts(w, players, r_ratio=0.62):
    """按屏幕空间角度环形布置出生点（等距菱形外边距均匀）"""
    r = w * r_ratio
    starts = []
    for i in range(players):
        ang = math.radians(-135 + i * (360.0 / players))
        sx = r * math.cos(ang)
        sy = r * math.sin(ang)
        # 屏幕 -> iso: sx = x-y, sy = (x+y)/2  中心 (w,w)
        x = w + sy + sx / 2.0
        y = w + sy - sx / 2.0
        xi, yi = int(round(x)), int(round(y))
        for _ in range(60):
            if cell_in_map(xi, yi, w):
                break
            xi += -1 if xi > w else 1
            yi += -1 if yi > w else 1
        xi = min(max(xi, 1), 2 * w - 1)
        yi = min(max(yi, 1), 2 * w - 1)
        starts.append((xi, yi))
    return starts


def blob(cx, cy, r):
    """菱形矿田（等距菱形，近似 FA2 自动矿田外形）"""
    out = []
    ri = max(1, int(r))
    for dy in range(-ri, ri + 1):
        span = ri - abs(dy)
        for dx in range(-span, span + 1):
            out.append((cx + dx, cy + dy))
    return out


# ---------------- 预览图 (2W x W RGB) ----------------
def build_preview(w, ore_cells):
    W2, H2 = 2 * w, w
    img = bytearray(W2 * H2 * 3)
    for i in range(W2 * H2):
        o = i * 3
        img[o], img[o + 1], img[o + 2] = 32, 40, 52
    ore = set(ore_cells)
    for (x, y) in diamond_cells(w):
        sx = (x - y) + w - 1
        sy = (x + y) // 2 - 1
        if not (0 <= sx < W2 and 0 <= sy < H2):
            continue
        col = (196, 168, 58) if (x, y) in ore else (96, 132, 72)
        o = (sy * W2 + sx) * 3
        img[o], img[o + 1], img[o + 2] = col
    return bytes(img)


# ---------------- 生成地图 ----------------
def new_map(name, size=80, theater='TEMPERATE', players=4, ore=True,
            gems=False, seed=None, author='红警地图管理器', max_player=8):
    rng = random.Random(seed)
    w = int(size)
    cells = diamond_cells(w)
    starts = player_starts(w, players)

    overlay_idx, overlay_data = {}, {}
    ok = lambda x, y: cell_in_map(x, y, w)
    if ore:
        for (sx_, sy_) in starts:
            dx, dy = w - sx_, w - sy_
            norm = max(abs(dx), abs(dy)) or 1
            ox = sx_ + int(dx / norm * w * 0.20)
            oy = sy_ + int(dy / norm * w * 0.20)
            painted = ore_paint(ox, oy, max(3, int(w * 0.08)), rng, ok)
            painted.update(ore_paint(sx_, sy_ + int(w * 0.10),
                                     max(2, int(w * 0.045)), rng, ok))
            for (x, y), (vid, val) in painted.items():
                overlay_idx[(x, y)] = vid
                overlay_data[(x, y)] = val
    if gems:
        for (x, y), (vid, val) in gem_paint(w, w, max(2, int(w * 0.05)),
                                            rng, ok).items():
            overlay_idx[(x, y)] = vid
            overlay_data[(x, y)] = val

    tiles = mapio.tiles_build([(x, y, CLEAR_TILE, 0, 0) for (x, y) in cells])
    ov = mapio.overlay_build(overlay_idx, mapio.OVERLAY_EMPTY_IDX)
    ovd = mapio.overlay_build(overlay_data, mapio.OVERLAY_EMPTY_DATA)
    preview = build_preview(w, list(overlay_idx.keys()))

    ls_l = max(1, w // 10)
    ls_t = max(1, w // 10 + 2)
    ls_w = max(4, int(w * 0.8))
    ls_h = max(4, int(w * 0.78))

    def blk(title, lines):
        return '[' + title + ']\r\n' + '\r\n'.join(lines) + '\r\n\r\n'

    ini = ''
    ini += blk('Basic', [
        f'Name={name}', 'Theme=No theme', 'Percent=0', 'GameMode=standard',
        'InitTime=10000', 'Official=no', 'EndOfGame=no', 'FreeRadar=yes',
        f'MaxPlayer={max(2, min(8, max_player))}', 'MinPlayer=2',
        'SkipScore=no', 'TrainCrate=no', 'TruckCrate=no', 'OneTimeOnly=no',
        'CarryOverCap=0', 'NewINIFormat=4', 'NextScenario=', 'SkipMapSelect=no',
        'MultiplayerOnly=1', 'IceGrowthEnabled=no', 'VeinGrowthEnabled=no',
        'TiberiumGrowthEnabled=yes', 'IgnoreGlobalAITriggers=no',
        'TiberiumDeathToVisceroid=no'])
    if author:
        ini += blk('Briefing', [f'1={author} 生成'])
    ini += blk('Map', [f'Size=0,0,{w},{w}', f'Theater={theater}',
                       f'LocalSize={ls_l},{ls_t},{ls_w},{ls_h}'])
    ini += blk('SpecialFlags', [f'{k}={v}' for k, v in [
        ('Inert', 'no'), ('FogOfWar', 'no'), ('IonStorms', 'no'),
        ('MCVDeploy', 'yes'), ('Meteorites', 'no'), ('Visceroids', 'no'),
        ('FixedAlliance', 'no'), ('TiberiumGrows', 'yes'), ('InitialVeteran', 'no'),
        ('HarvesterImmune', 'no'), ('TiberiumSpreads', 'no'),
        ('TiberiumExplosive', 'no'), ('DestroyableBridges', 'yes')]])
    ini += blk('Houses', [f'{i}={h}' for i, h in enumerate([
        'Americans', 'Alliance', 'French', 'Germans', 'British', 'Africans',
        'Arabs', 'Confederation', 'Russians', 'YuriCountry', 'GDI', 'Nod',
        'Neutral', 'Special'])])
    ini += blk('Lighting', [f'{k}={v}' for k, v in [
        ('Red', '1.000000'), ('Blue', '1.000000'), ('Green', '1.000000'),
        ('Level', '0.002000'), ('Ground', '0.000000'), ('IonRed', '1.000000'),
        ('Ambient', '1.010000'), ('IonBlue', '1.000000'), ('IonGreen', '1.000000'),
        ('IonLevel', '0.032000'), ('IonGround', '0.000000'),
        ('IonAmbient', '0.600000'), ('DominatorRed', '0.850000'),
        ('DominatorBlue', '0.300000'), ('DominatorGreen', '0.200000'),
        ('DominatorLevel', '0.000000'), ('DominatorGround', '0.000000'),
        ('DominatorAmbient', '1.500000'),
        ('DominatorAmbientChangeRate', '0.009000')]])

    dg = bytearray(rng.getrandbits(8) for _ in range(20))
    dg[-1] = 0x80
    tail = blk('Waypoints', [f'{i}={x * 1000 + y}' for i, (x, y) in enumerate(starts)])
    tail += blk('Digest', ['1=' + base64.b64encode(bytes(dg)).decode()])

    def pack_sec(sec, data, codec):
        lines = mapio.pack_encode(data, codec)
        return b'[' + sec.encode() + b']\r\n' + b'\r\n'.join(lines) + b'\r\n\r\n'

    blobs = pack_sec('IsoMapPack5', tiles, 'lit')
    blobs += pack_sec('PreviewPack', preview, 'lit')
    blobs += b'[Preview]\r\nSize=0,0,%d,%d\r\n\r\n' % (2 * w, w)
    blobs += pack_sec('OverlayPack', ov, 'lcw')
    blobs += pack_sec('OverlayDataPack', ovd, 'lcw')

    out = ini.encode('gbk', errors='replace') + blobs + tail.encode('gbk', errors='replace')
    return out, starts


def resave(path_in, path_out, tiles=None, overlay=None, overlay_data=None):
    """读现有地图 -> 可选替换 tiles/overlay -> 写回（其它段原样保留）"""
    raw = open(path_in, 'rb').read()
    sections = parse_sections(raw)
    if tiles is not None:
        sections['IsoMapPack5'] = ('pack', mapio.pack_encode(tiles, 'lzo'))
    if overlay is not None:
        sections['OverlayPack'] = ('pack', mapio.pack_encode(overlay, 'lcw'))
    if overlay_data is not None:
        sections['OverlayDataPack'] = ('pack', mapio.pack_encode(overlay_data, 'lcw'))
    return serialize(sections)


def parse_sections(raw):
    """切成 [(name, kind, payload)]，保持原始顺序，**保留重名段**
    （实测有 73 张地图存在重名段，如 [Virus]，不能按名字合并）"""
    text = raw.replace(b'\r\n', b'\n')
    out = []
    cur, buf = None, []
    for ln in text.split(b'\n'):
        s = ln.strip()
        if s.startswith(b'[') and s.endswith(b']'):
            if cur is not None:
                out.append((cur, buf))
            cur = s[1:-1].decode('latin-1')
            buf = []
        elif cur is not None:
            buf.append(ln)
    if cur is not None:
        out.append((cur, buf))
    res = []
    for name, lines in out:
        kind = 'pack' if name in ('IsoMapPack5', 'OverlayPack', 'OverlayDataPack',
                                  'PreviewPack') else 'text'
        res.append((name, kind, [l for l in lines if l.strip()]))
    return res


def serialize(sections):
    parts = []
    for name, kind, lines in sections:
        # 段名按 latin-1 原样写回：个别地图有非 ASCII 的段名（二进制残留），
        # 用 gbk 重编码会损坏，导致重新解析时段名不一致
        try:
            nbytes = name.encode('latin-1')
        except UnicodeEncodeError:
            nbytes = name.encode('gbk', errors='replace')
        head = b'[' + nbytes + b']\r\n'
        if kind == 'pack' and lines and isinstance(lines[0], bytes):
            body = b'\r\n'.join(lines)
        else:
            body = b'\r\n'.join(l if isinstance(l, bytes) else str(l).encode('gbk', errors='replace')
                                for l in lines)
        parts.append(head + body + b'\r\n\r\n')
    return b''.join(parts)
