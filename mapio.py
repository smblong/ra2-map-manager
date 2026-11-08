# -*- coding: utf-8 -*-
"""RA2/YR 地图 pack 段编解码器
实证结论（2026-11-05）:
  - 段内行为 `N=<b64片段>`，全部片段拼流后一次 b64decode
  - 流 = 连续块: 头 4 字节 struct '<HH' (LE压缩长, LE解压长) + 压缩数据
  - IsoMapPack5 / PreviewPack: LZO1X 压缩（lzallright）
  - OverlayPack / OverlayDataPack: LCW 压缩（Westwood 格式，绝对偏移模式）
  - IsoMapPack5 解压后 11 字节/格:
      [X u16][Y u16][TileIndex int32][TileSubIndex u8][Level u8][IceGrowth u8]
  - OverlayPack/OverlayDataPack 解压后固定 262144 字节 = 512x512 网格
      索引 = x + y*512；OverlayPack=类型字节(0xFF=空)，OverlayDataPack=帧/密度
"""
import base64, struct
from lzallright import LZOCompressor

_MAX_CHUNK = 0x2000      # 8192，与游戏一致
_LINE_CHARS = 70         # 每行 b64 字符数，与原版一致
OVERLAY_GRID = 512       # overlay 网格边长
OVERLAY_EMPTY = 0xFF

LZO_SECTIONS = {'IsoMapPack5', 'PreviewPack', 'Preview'}
LCW_SECTIONS = {'OverlayPack', 'OverlayDataPack'}


# ---------------- 分块流 ----------------
def read_section_lines(raw: bytes, name: str):
    """从地图原始字节中取某段所有行（含 N= 前缀）。"""
    lines = raw.replace(b'\r\n', b'\n').split(b'\n')
    cur, out = None, []
    for ln in lines:
        s = ln.strip()
        if s.startswith(b'[') and s.endswith(b']'):
            if cur == name and out:
                return out
            cur = s[1:-1].decode('latin-1')
            out = []
        elif cur == name and s:
            out.append(s)
    return out


def _split_chunks(stream: bytes):
    """按 '<HH' 头走完整个流，返回 [(comp, dec, blob)]。"""
    out, off, n = [], 0, len(stream)
    while off + 4 <= n:
        comp, dec = struct.unpack_from('<HH', stream, off)
        off += 4
        blob = stream[off:off + comp]
        if len(blob) != comp:
            raise ValueError(f'块截断: 需{comp}实{len(blob)}')
        out.append((comp, dec, blob))
        off += comp
    if off != n:
        raise ValueError(f'流尾 {n - off} 字节未消费')
    return out


def _build_stream(chunks):
    """[(comp, dec, blob)] -> 流字节"""
    out = bytearray()
    for comp, dec, blob in chunks:
        out += struct.pack('<HH', comp, dec) + blob
    return bytes(out)


# ---------------- LCW ----------------
def lcw_decode(blob: bytes, out_size: int) -> bytes:
    """Westwood LCW 解压。自动识别绝对/相对模式（首字节 0x00 = 相对）。"""
    out = bytearray()
    i, n = 0, len(blob)
    relative = bool(blob) and blob[0] == 0
    if relative:
        i = 1

    def u16():
        nonlocal i
        v = blob[i] | (blob[i + 1] << 8)
        i += 2
        return v

    while i < n and len(out) < out_size:
        flag = blob[i]; i += 1
        if flag & 0x80:
            if flag & 0x40:
                size = (flag & 0x3F) + 3
                if flag == 0xFE:                    # 长填充
                    size = min(u16(), out_size - len(out))
                    v = blob[i]; i += 1
                    out += bytes([v]) * size
                    continue
                if flag == 0xFF:                    # 长拷贝
                    size = u16()
                    off = u16()
                else:                               # 短拷贝
                    off = u16()
                size = min(size, out_size - len(out))
                src = (len(out) - off) if relative else off
                for _ in range(size):
                    out.append(out[src] if 0 <= src < len(out) else 0)
                    src += 1
            else:
                if flag == 0x80:                    # 结束
                    break
                size = min(flag & 0x3F, out_size - len(out))   # 短字面量
                take = min(size, n - i)
                out += blob[i:i + take]; i += take
        else:                                       # 短相对拷贝
            size = (flag >> 4) + 3
            off = ((flag & 0xF) << 8) | blob[i]; i += 1
            size = min(size, out_size - len(out))
            for _ in range(size):
                out.append(out[-off] if off <= len(out) else 0)
    return bytes(out)


def lcw_encode(data: bytes) -> bytes:
    """简易 LCW 压缩（绝对模式，短字面量+长填充+长绝对拷贝+结束标记）。"""
    out = bytearray()
    i, n = 0, len(data)
    while i < n:
        # 统计本位置开始的重复字节长度
        run = 1
        while i + run < n and data[i + run] == data[i] and run < 0xFFFF:
            run += 1
        if run >= 3:
            if run <= 62:                   # 短运行也用长填充（简单可靠）
                out.append(0xFE)
                out += struct.pack('<H', run)
                out.append(data[i])
                i += run
            else:                            # 拆分超长填充
                r = min(run, 0xFFFF)
                out.append(0xFE)
                out += struct.pack('<H', r)
                out.append(data[i])
                i += r
        else:
            # 收集字面量（遇到 >=3 的重复就停）
            j = i
            lit_end = j
            while lit_end < n and lit_end - j < 63:
                r = 1
                while lit_end + r < n and data[lit_end + r] == data[lit_end]:
                    r += 1
                if r >= 3:
                    break
                lit_end += 1
            size = max(1, lit_end - j)
            out.append(0x80 | size)
            out += data[j:j + size]
            i += size
    out.append(0x80)                         # 结束标记
    return bytes(out)


# ---------------- 统一 pack 编解码 ----------------
def pack_decode(value_lines):
    """base64 行 -> 块流 -> 解压拼接。自动识别 LZO/LCW。返回 (bytes, codec)。"""
    vals = [x.partition(b'=')[2] if b'=' in x[:8] else x for x in value_lines]
    stream = base64.b64decode(b''.join(vals))
    chunks = _split_chunks(stream)
    out = bytearray()
    codec = 'lzo'
    try:
        for comp, dec, blob in chunks:
            out += LZOCompressor.decompress(blob, dec)
    except Exception:
        codec = 'lcw'
        out = bytearray()
        for comp, dec, blob in chunks:
            out += lcw_decode(blob, dec)
    return bytes(out), codec


def pack_encode(data: bytes, codec='lzo'):
    """bytes -> 块流 -> base64 70字符行。
    codec:
      'lzo'  最小 LZO（lzallright，压缩率高，但 CNCMaps 的 C# MiniLZO 移植版解不了匹配 token）
      'lit'  纯字面量 LZO（每块 = [17+len][原文][11 00 00]，任何解码器都认，文件约 1.36x）
      'lcw'  Westwood LCW（overlay 段）
    """
    chunks = []
    if codec == 'lcw':
        for i in range(0, len(data), _MAX_CHUNK):
            blob = lcw_encode(data[i:i + _MAX_CHUNK])
            chunks.append((len(blob), min(_MAX_CHUNK, len(data) - i), blob))
    elif codec == 'lit':
        _LIT = 238
        for i in range(0, len(data), _LIT):
            part = data[i:i + _LIT]
            blob = bytes([17 + len(part)]) + part + b'\x11\x00\x00'
            chunks.append((len(blob), len(part), blob))
    else:
        comp_obj = LZOCompressor()
        for i in range(0, len(data), _MAX_CHUNK):
            blob = comp_obj.compress(data[i:i + _MAX_CHUNK])
            chunks.append((len(blob), min(_MAX_CHUNK, len(data) - i), blob))
    stream = _build_stream(chunks)
    b64 = base64.b64encode(stream)
    return [b'%d=%s' % (i // _LINE_CHARS + 1, b64[i:i + _LINE_CHARS])
            for i in range(0, len(b64), _LINE_CHARS)]


# ---------------- 瓦片 ----------------
TILE = 11  # 每格字节数

def tiles_iter(data: bytes):
    """迭代瓦片: (x, y, tile_idx, sub, level, ice)"""
    for i in range(len(data) // TILE):
        x, y, ti = struct.unpack_from('<HHI', data, i * TILE)
        sub, level, ice = data[i * TILE + 8], data[i * TILE + 9], data[i * TILE + 10]
        yield x, y, ti, sub, level, ice


def tiles_build(cells):
    """[(x, y, tile_idx, sub, level),...] -> 解压字节。按(x,y)排序输出。"""
    out = bytearray()
    for x, y, ti, sub, level in sorted(cells, key=lambda c: (c[1], c[0])):
        out += struct.pack('<HHIBBB', x, y, ti, sub, level, 0)
    return bytes(out)


# ---------------- Overlay 网格 ----------------
OVERLAY_EMPTY_IDX = 0xFF   # OverlayPack 空类型
OVERLAY_EMPTY_DATA = 0x00  # OverlayDataPack 空帧

def overlay_grid(data: bytes, empty=OVERLAY_EMPTY_IDX):
    """262144 字节 -> {cell(x,y): byte}（跳过空值）"""
    g = {}
    for i, v in enumerate(data):
        if v != empty:
            g[(i % OVERLAY_GRID, i // OVERLAY_GRID)] = v
    return g


def overlay_build(grid: dict, empty=OVERLAY_EMPTY_IDX):
    """{cell(x,y): byte} -> 262144 字节"""
    buf = bytearray([empty]) * (OVERLAY_GRID * OVERLAY_GRID)
    for (x, y), v in grid.items():
        if 0 <= x < OVERLAY_GRID and 0 <= y < OVERLAY_GRID:
            buf[y * OVERLAY_GRID + x] = v & 0xFF
    return bytes(buf)


if __name__ == '__main__':
    import sys
    for path in sys.argv[1:]:
        raw = open(path, 'rb').read()
        print('=' * 60)
        print(path.split('\\')[-1].split('/')[-1])
        allok = True
        for sec in ('IsoMapPack5', 'OverlayPack', 'OverlayDataPack', 'PreviewPack'):
            vls = read_section_lines(raw, sec)
            if not vls:
                print(f'  [{sec}] 无')
                continue
            try:
                data, codec = pack_decode(vls)
            except Exception as e:
                print(f'  [{sec}] 解码失败: {e}')
                allok = False
                continue
            try:
                # 用原编码器重编码再解，验证
                data2, _ = pack_decode(pack_encode(data, codec))
                same = data2 == data
            except Exception as e:
                same = False
            extra = ''
            if sec == 'IsoMapPack5':
                extra = f' 瓦片={len(data)//TILE}'
            if sec.startswith('Overlay'):
                empty = OVERLAY_EMPTY_IDX if sec == 'OverlayPack' else OVERLAY_EMPTY_DATA
                g = overlay_grid(data, empty)
                extra = f' 非空格={len(g)} codec={codec}'
            print(f'  [{sec}] {len(data)}B{extra} 回环={same}')
            if not same:
                allok = False
        print('  总体:', 'PASS' if allok else 'FAIL')
