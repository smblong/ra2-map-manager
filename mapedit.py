# -*- coding: utf-8 -*-
"""地图绘制编辑器后端：读取网格 / 刷矿刷宝石 / 出生点 / 高度 / 新建地图

坐标体系（实证）
  IsoMapPack5 里的 X,Y 就是 iso 菱形坐标 (Dx,Dy)，屏幕位置 px=Dx*tileW, py=Dy*tileH
  Overlay 网格索引 = Dx + 512*Dy
  Waypoint 值 = Dx*1000 + Dy
"""
import base64
import os
import shutil
import time
import zlib

import mapio
import mapgen
import overlay_catalog

MAP_EXT = ('.yrm', '.map', '.mpr', '.mmx', '.yro')


# ---------------------------------------------------------------- 工具
def _png(width, height, rgb):
    """极简 PNG 编码（RGB 三通道），返回 bytes"""
    import struct
    raw = bytearray()
    stride = width * 3
    for y in range(height):
        raw.append(0)
        raw += rgb[y * stride:(y + 1) * stride]

    def mk(tag, data):
        return (struct.pack('>I', len(data)) + tag + data +
                struct.pack('>I', zlib.crc32(tag + data) & 0xffffffff))

    ihdr = struct.pack('>IIBBBBB', width, height, 8, 2, 0, 0, 0)
    return (b'\x89PNG\r\n\x1a\n' + mk(b'IHDR', ihdr) +
            mk(b'IDAT', zlib.compress(bytes(raw), 6)) + mk(b'IEND', b''))


def map_size(raw):
    """从 [Map] Size=0,0,W,H 取 W"""
    for name, kind, lines in mapgen.parse_sections(raw):
        if name.lower() == 'map':
            for l in lines:
                s = l.decode('latin-1', 'replace').strip()
                if s.lower().startswith('size='):
                    try:
                        parts = [int(x) for x in s.split('=', 1)[1].split(',')]
                        return parts[2], parts[3]
                    except (ValueError, IndexError):
                        pass
    return None, None


def _decode_opt(raw, sec, empty):
    """返回 (grid, ok)。ok=False 表示该段存在但解不开（少数地图分块结构异常）"""
    lines = mapio.read_section_lines(raw, sec)
    if not lines:
        return {}, True
    try:
        data, _ = mapio.pack_decode(lines)
        return mapio.overlay_grid(data, empty), True
    except Exception:
        return {}, False


def _read_waypoints(raw):
    out = {}
    for name, kind, lines in mapgen.parse_sections(raw):
        if name.lower() == 'waypoints':
            for l in lines:
                s = l.decode('latin-1', 'replace').strip()
                if '=' in s:
                    k, v = s.split('=', 1)
                    try:
                        val = int(v.strip())
                        out[int(k.strip())] = [val // 1000, val % 1000]
                    except ValueError:
                        pass
    return out


def preview_png(raw, w, h, overlay, ore_ids, gem_ids):
    """把 PreviewPack 解出来转 PNG；缺失时用平地上色合成"""
    lines = mapio.read_section_lines(raw, 'PreviewPack')
    W2, H2 = 2 * w, w
    if lines:
        try:
            data, _ = mapio.pack_decode(lines)
            if len(data) >= W2 * H2 * 3:
                return _png(W2, H2, data[:W2 * H2 * 3])
        except Exception:
            pass
    # 合成：绿底 + 矿黄 + 宝石蓝
    img = bytearray(W2 * H2 * 3)
    for i in range(W2 * H2):
        img[i * 3:i * 3 + 3] = b'\x5e\x8a\x46'
    for (x, y), v in overlay.items():
        sx, sy = (x - y) + w - 1, (x + y) // 2 - 1
        if not (0 <= sx < W2 and 0 <= sy < H2):
            continue
        col = b'\xc2\xa6\x3c' if v in ore_ids else (b'\x3c\xb4\xd2' if v in gem_ids else b'\x88\x88\x88')
        o = (sy * W2 + sx) * 3
        img[o:o + 3] = col
    return _png(W2, H2, img)


# ---------------------------------------------------------------- 读取
def load(path, data_dir=None, game_dir=None, catalog=None):
    raw = open(path, 'rb').read()
    w, h = map_size(raw)
    if not w:
        raise ValueError('无法读取地图尺寸')
    ov, ok_ov = _decode_opt(raw, 'OverlayPack', mapio.OVERLAY_EMPTY_IDX)
    ovd, ok_ovd = _decode_opt(raw, 'OverlayDataPack', mapio.OVERLAY_EMPTY_DATA)
    warn = ''
    if not (ok_ov and ok_ovd):
        warn = '这张地图的 overlay 数据分块结构异常，读不出来，已切换为只读（避免误写损坏）'
    tiles = {}
    tl = mapio.read_section_lines(raw, 'IsoMapPack5')
    if tl:
        try:
            tdata, _ = mapio.pack_decode(tl)
            for x, y, ti, sub, lv, ice in mapio.tiles_iter(tdata):
                tiles[(x, y)] = (ti, sub, lv, ice)
        except Exception:
            pass

    cls = {'ore': [], 'gem': []}
    if catalog:
        cls = overlay_catalog.classify(catalog)
    ore_ids = set(cls['ore'])
    gem_ids = set(cls['gem'])

    cells = []
    for (x, y), v in ov.items():
        kind = 'ore' if v in ore_ids else ('gem' if v in gem_ids else 'other')
        cells.append([x, y, v, ovd.get((x, y), 0), kind])

    levels = [[x, y, lv] for (x, y), (ti, sub, lv, ice) in tiles.items() if lv]

    return {
        'ok': True,
        'w': w, 'h': h,
        'theater': _theater(raw),
        'cells': cells,
        'levels': levels,
        'waypoints': _read_waypoints(raw),
        'tileCount': len(tiles),
        'preview': 'data:image/png;base64,' + base64.b64encode(
            preview_png(raw, w, w, ov, ore_ids, gem_ids)).decode(),
        'readonly': bool(warn), 'warn': warn,
        'oreId': mapgen.ORE_ID, 'gemId': mapgen.GEM_ID,
        'oreIds': sorted(ore_ids)[:40], 'gemIds': sorted(gem_ids)[:40],
        'size': len(raw),
    }


def _theater(raw):
    for name, kind, lines in mapgen.parse_sections(raw):
        if name.lower() == 'map':
            for l in lines:
                s = l.decode('latin-1', 'replace').strip()
                if s.lower().startswith('theater='):
                    return s.split('=', 1)[1].strip().upper()
    return 'TEMPERATE'


# ---------------------------------------------------------------- 格点检查
_OBJ_SECS = ('Structures', 'Units', 'Infantry')


def _section_rows(raw, want):
    """按段名（不区分大小写、保留重名段）收全部行，返回 [fields]"""
    rows = []
    for name, kind, lines in mapgen.parse_sections(raw):
        if name.upper() == want.upper():
            for l in lines:
                s = l.decode('latin-1', 'replace').strip()
                if '=' in s:
                    rows.append(s.split('=', 1)[1].split(','))
    return rows


def _objects_at(raw, w):
    """解析建筑/单位/步兵 → {段名: [{house,type,x,y}]}
    X,Y 字段位置不同编辑器不一样（多为第 3,4 字段），按"界内命中率"投票选偏移"""
    out = {}
    for sn in _OBJ_SECS:
        rows = _section_rows(raw, sn)
        if not rows:
            continue
        best_off, best_hits = None, 0
        for off in (2, 3, 4, 5):
            hits = 0
            for f in rows:
                try:
                    if mapgen.cell_in_map(int(f[off]), int(f[off + 1]), w):
                        hits += 1
                except (ValueError, IndexError):
                    pass
            if hits > best_hits:
                best_off, best_hits = off, hits
        if best_off is None or best_hits < max(1, int(len(rows) * 0.6)):
            continue  # 布局无法识别，宁缺毋滥
        objs = []
        for f in rows:
            try:
                x, y = int(f[best_off]), int(f[best_off + 1])
            except (ValueError, IndexError):
                continue
            objs.append({'house': (f[0] or '').strip(),
                         'type': (f[1] if len(f) > 1 else '').strip(),
                         'x': x, 'y': y})
        out[sn] = objs
    return out


# ---------------------------------------------------------------- 属性联动定位
_OBJ_LABEL = {'structures': '建筑', 'units': '载具', 'infantry': '步兵',

             'aircraft': '飞行器'}


def _rows_keyed(raw, want):
    """按段名收 (键, 逗号字段)，键保留原样，用于属性行反查坐标"""
    rows = []
    for name, kind, lines in mapgen.parse_sections(raw):
        if name.upper() == want.upper():
            for l in lines:
                s = l.decode('latin-1', 'replace').strip()
                if '=' in s:
                    k, v = s.split('=', 1)
                    rows.append((k.strip(), v.split(',')))
    return rows


def _best_xy_offset(rows, w):
    """X,Y 在字段里的偏移随编辑器不同（多在 2~5），按界内命中率投票"""
    best_off, best_hits = None, 0
    for off in (2, 3, 4, 5):
        hits = 0
        for k, f in rows:
            try:
                if mapgen.cell_in_map(int(f[off]), int(f[off + 1]), w):
                    hits += 1
            except (ValueError, IndexError):
                pass
        if hits > best_hits:
            best_off, best_hits = off, hits
    if best_off is None or best_hits < max(1, int(len(rows) * 0.6)):
        return None
    return best_off


def _obj_keyed(raw, w):
    """{段名小写: {键大写: (x, y, 类型名)}}；识别不出布局的段直接跳过"""
    out = {}
    for sn in ('Structures', 'Units', 'Infantry', 'Aircraft'):
        rows = _rows_keyed(raw, sn)
        if not rows:
            continue
        off = _best_xy_offset(rows, w)
        if off is None:
            continue
        d = {}
        for k, f in rows:
            try:
                d[k.upper()] = (int(f[off]), int(f[off + 1]),
                               (f[1] if len(f) > 1 else '').strip())
            except (ValueError, IndexError):
                pass
        if d:
            out[sn.lower()] = d
    return out


def _terrain_keyed(raw, w):
    """[Terrain] 键编码两种并存：x*1000+y 与 x+512*y，按界内命中率选"""
    rows = _rows_keyed(raw, 'Terrain')
    if not rows:
        return {}

    def decode(k, mode):
        if mode == 'k1000':
            return k // 1000, k % 1000
        return k % 512, k // 512

    modes = []
    for mode in ('k1000', 'grid'):
        hits = 0
        for k, f in rows:
            try:
                x, y = decode(int(k), mode)
            except ValueError:
                continue
            if mapgen.cell_in_map(x, y, w):
                hits += 1
        modes.append((hits, mode))
    modes.sort(reverse=True)
    if not modes or modes[0][0] < max(1, int(len(rows) * 0.6)):
        return {}
    mode = modes[0][1]
    d = {}
    for k, f in rows:
        try:
            x, y = decode(int(k), mode)
        except ValueError:
            continue
        d[k.upper()] = (x, y, (f[0] if f else '').strip())
    return d


def edit_locs(path):
    """给「属性行 → 地图位置」用：出生点 / 地图边界 / 建筑单位坐标 / 地形物坐标"""
    raw = open(path, 'rb').read()
    w, h = map_size(raw)
    if not w:
        return {}
    rects = {}
    for name, kind, lines in mapgen.parse_sections(raw):
        if name.lower() != 'map':
            continue
        for l in lines:
            s = l.decode('latin-1', 'replace').strip()
            if '=' not in s:
                continue
            k, v = s.split('=', 1)
            k = k.strip().lower()
            if k in ('size', 'localsize', 'localrect'):
                try:
                    p = [int(t) for t in v.split(',')][:4]
                except ValueError:
                    continue
                if len(p) == 4:
                    rects[k] = p
    return {
        'w': w, 'h': h,
        'waypoints': {str(k): v for k, v in _read_waypoints(raw).items()},
        'rects': rects,
        'objects': _obj_keyed(raw, w),
        'terrain': _terrain_keyed(raw, w),
    }


def _terrain_map(raw, w):
    """[Terrain] 键 → {(x,y): 名称}。键编码两种并存：x*1000+y（实证主流）与 x+512*y，
    按界内命中率选一种"""
    pairs = []
    for name, kind, lines in mapgen.parse_sections(raw):
        if name.upper() == 'TERRAIN':
            for l in lines:
                s = l.decode('latin-1', 'replace').strip()
                if '=' in s:
                    k, _, v = s.partition('=')
                    try:
                        pairs.append((int(k), v.strip()))
                    except ValueError:
                        pass
    if not pairs:
        return {}
    schemes = ((lambda k: (k // 1000, k % 1000)),
               (lambda k: (k % 512, k // 512)))
    best, best_hits = {}, -1
    for fx in schemes:
        hits, d = 0, {}
        for k, _v in pairs:
            x, y = fx(k)
            if mapgen.cell_in_map(x, y, w):
                hits += 1
                d[(x, y)] = True
        if hits > best_hits:
            best, best_hits = d, hits
    if best_hits < max(1, int(len(pairs) * 0.5)):
        return {}
    # 选中的方案：用第二次遍历确定键→坐标（简单起见重新算命中率）
    fix = None
    for fx in schemes:
        hits = 0
        for k, _v in pairs:
            x, y = fx(k)
            if mapgen.cell_in_map(x, y, w):
                hits += 1
        if hits == best_hits:
            fix = fx
            break
    out = {}
    for k, v in pairs:
        x, y = fix(k)
        if (x, y) in best:
            out.setdefault((x, y), v)
    return out


def cell_info(path, x, y, catalog=None):
    """点击一格 → 汇总这一格上的全部参数（瓦片/覆盖物/出生点/地形物件/建筑/单位/步兵）"""
    raw = open(path, 'rb').read()
    w, h = map_size(raw)
    if not w:
        raise ValueError('无法读取地图尺寸')
    x, y = int(x), int(y)
    info = {'ok': True, 'x': x, 'y': y, 'w': w, 'inMap': mapgen.cell_in_map(x, y, w)}
    if not info['inMap']:
        return info

    # 瓦片（tileset / 变体 / 高度 / 冰）
    tl = mapio.read_section_lines(raw, 'IsoMapPack5')
    if tl:
        try:
            tdata, _ = mapio.pack_decode(tl)
            for tx, ty, ti, sub, lv, ice in mapio.tiles_iter(tdata):
                if (tx, ty) == (x, y):
                    info['tile'] = {'tileset': ti, 'sub': sub, 'level': lv, 'ice': ice}
                    break
        except Exception:
            pass

    # 覆盖物（矿/宝石/其他）
    types = catalog or {}
    ov, _ = _decode_opt(raw, 'OverlayPack', mapio.OVERLAY_EMPTY_IDX)
    ovd, _ = _decode_opt(raw, 'OverlayDataPack', mapio.OVERLAY_EMPTY_DATA)
    if (x, y) in ov:
        vid = ov[(x, y)]
        kind = 'other'
        if types:
            cls = overlay_catalog.classify(types)
            if vid in cls['ore'] or vid in cls['ore2'] or vid in cls['ore3']:
                kind = 'ore'
            elif vid in cls['gem']:
                kind = 'gem'
        info['overlay'] = {'id': vid, 'name': types.get(vid, ''),
                           'kind': kind, 'val': ovd.get((x, y), 0)}

    # 出生点
    wps = _read_waypoints(raw)
    info['waypoints'] = [k for k, v in wps.items() if v == [x, y]]

    # 地形物件（树/岩石）
    tr = _terrain_map(raw, w)
    if (x, y) in tr:
        info['terrain'] = tr[(x, y)]

    # 建筑 / 单位 / 步兵（建筑是锚点格）
    for sn, lst in _objects_at(raw, w).items():
        hit = [o for o in lst if (o['x'], o['y']) == (x, y)]
        if hit:
            info[sn.lower()] = hit
    return info


# ---------------------------------------------------------------- 保存
def save(path, ops, backup_root=None):
    """ops: {'paint': [{'cells': [[x,y],...], 'kind': 'ore'|'gem'|'other'|'erase', 'val': 0-11}],
             'waypoints': {'0': [x,y], ...},
             'height': {'cells': [[x,y],...], 'level': 0-14}}"""
    raw = open(path, 'rb').read()
    w, h = map_size(raw)
    if not w:
        raise ValueError('无法读取地图尺寸')
    sections = mapgen.parse_sections(raw)
    by_name = {}
    for i, (n, k, l) in enumerate(sections):
        by_name.setdefault(n, i)

    changed_overlay = changed_tiles = changed_wp = False
    ov, ok_ov = _decode_opt(raw, 'OverlayPack', mapio.OVERLAY_EMPTY_IDX)
    ovd, ok_ovd = _decode_opt(raw, 'OverlayDataPack', mapio.OVERLAY_EMPTY_DATA)
    if ops.get('paint') and not (ok_ov and ok_ovd):
        raise ValueError('这张地图的 overlay 数据无法解析，为避免写坏，已拒绝保存')
    ore_ids = set(mapgen.ORE_IDS)
    gem_ids = set(mapgen.GEM_IDS)

    paint = (ops.get('paint') or [])
    if paint:
        changed_overlay = True
        for item in paint:
            kind = item.get('kind') or 'ore'
            val = int(item.get('val') if item.get('val') is not None else 8)
            val = max(0, min(11, val))
            vid = {'ore': mapgen.ORE_ID, 'gem': mapgen.GEM_ID}.get(kind)
            for c in item.get('cells') or []:
                x, y = int(c[0]), int(c[1])
                if not mapgen.cell_in_map(x, y, w):
                    continue
                if kind == 'erase':
                    ov.pop((x, y), None)
                    ovd.pop((x, y), None)
                elif vid is not None:
                    ov[(x, y)] = vid
                    ovd[(x, y)] = val
                elif kind == 'other' and item.get('id'):
                    ov[(x, y)] = int(item['id']) & 0xFF
                    ovd[(x, y)] = val

    hgt = ops.get('height')
    if isinstance(hgt, dict):
        hgt = [hgt]
    if hgt:
        groups = [g for g in hgt if g and g.get('cells')]
        if groups:
            changed_tiles = True
            tl = mapio.read_section_lines(raw, 'IsoMapPack5')
            tdata, _ = mapio.pack_decode(tl)
            want = {}
            for g in groups:
                lvl = max(0, min(15, int(g.get('level') or 0)))
                for c in g['cells']:
                    want[(int(c[0]), int(c[1]))] = lvl
            cells = []
            for x, y, ti, sub, level, ice in mapio.tiles_iter(tdata):
                if (x, y) in want:
                    level = want[(x, y)]
                cells.append((x, y, ti, sub, level))
            new_tiles = mapio.tiles_build(cells)
            idx = by_name.get('IsoMapPack5')
            if idx is not None:
                sections[idx] = ('IsoMapPack5', 'pack', mapio.pack_encode(new_tiles, 'lit'))

    if changed_overlay:
        idx = by_name.get('OverlayPack')
        payload = mapio.pack_encode(
            mapio.overlay_build(ov, mapio.OVERLAY_EMPTY_IDX), 'lcw')
        if idx is not None:
            sections[idx] = ('OverlayPack', 'pack', payload)
        else:
            sections.append(('OverlayPack', 'pack', payload))
        idx = by_name.get('OverlayDataPack')
        payload = mapio.pack_encode(
            mapio.overlay_build(ovd, mapio.OVERLAY_EMPTY_DATA), 'lcw')
        if idx is not None:
            sections[idx] = ('OverlayDataPack', 'pack', payload)
        else:
            sections.append(('OverlayDataPack', 'pack', payload))

    wps = ops.get('waypoints')
    if wps is not None:
        changed_wp = True
        lines = []
        for k in sorted(wps, key=lambda s: int(s)):
            v = wps[k]
            if v is None:
                continue
            x, y = int(v[0]), int(v[1])
            if not mapgen.cell_in_map(x, y, w):
                continue
            lines.append(('%d=%d' % (int(k), x * 1000 + y)).encode())
        idx = by_name.get('Waypoints')
        if idx is not None:
            sections[idx] = ('Waypoints', 'text', lines)
        else:
            sections.append(('Waypoints', 'text', lines))

    if not (changed_overlay or changed_tiles or changed_wp):
        return {'ok': True, 'changed': False, 'msg': '没有改动'}

    data = mapgen.serialize(sections)
    backup = None
    if backup_root:
        try:
            os.makedirs(backup_root, exist_ok=True)
            stamp = time.strftime('%Y%m%d_%H%M%S')
            stem = os.path.splitext(os.path.basename(path))[0]
            backup = os.path.join(backup_root, '%s_%s%s' %
                                  (stem, stamp, os.path.splitext(path)[1]))
            shutil.copy2(path, backup)
        except OSError:
            backup = None
    with open(path, 'wb') as f:
        f.write(data)
    return {'ok': True, 'changed': True, 'backup': backup,
            'overlayCells': len(ov), 'size': len(data)}


# ---------------------------------------------------------------- 新建
def create(dest_dir, name, size=80, theater='TEMPERATE', players=4,
           ore=True, gems=True, seed=None, author='红警地图管理器', max_player=8):
    os.makedirs(dest_dir, exist_ok=True)
    safe = ''.join(ch for ch in (name or '未命名') if ch not in '\\/:*?"<>|').strip() or '未命名'
    path = os.path.join(dest_dir, safe + '.yrm')
    n = 1
    while os.path.exists(path):
        path = os.path.join(dest_dir, '%s_%d.yrm' % (safe, n))
        n += 1
    data, starts = mapgen.new_map(name=safe, size=size, theater=theater,
                                  players=players, ore=ore, gems=gems,
                                  seed=seed, author=author, max_player=max_player)
    with open(path, 'wb') as f:
        f.write(data)
    return {'ok': True, 'path': path, 'starts': starts, 'size': len(data)}
