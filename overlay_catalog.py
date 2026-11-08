# -*- coding: utf-8 -*-
"""Overlay ID 目录：从游戏 MIX 中解析 [OverlayTypes] 列表

背景（实证）：
  RA2/YR 地图的 OverlayPack 每格一个字节 = overlay 类型编号，
  而编号 -> 名字的映射来自 rules(md).ini 的 [OverlayTypes] 段（1-based 顺序编号）。
  不同 mod 的列表不同，因此矿石/宝石的真实编号必须从当前游戏环境读取：

  本机「重聚」环境实测：
    矿   TIB01..TIB20 = 105..124   （真实地图大量用 112 = TIB08）
    宝石 GEM01..GEM12 = 28..39     （真实地图大量用 30 = GEM03）
    另有 TIB2_* = 130..149、TIB3_* = 150..169（其他矿种）
  原版 RA2/YR 是 102..121 / 27..38，作为兜底。
"""
import json
import os
import re

CACHE_NAME = 'overlay_types.json'

# 原版兜底
VANILLA_ORE = list(range(102, 122))
VANILLA_GEM = list(range(27, 39))


def _parse_overlay_section(text):
    """从一段文本里抽 [OverlayTypes] 的数字条目"""
    s = text.find('[OverlayTypes]')
    if s < 0:
        return {}
    seg = text[s + len('[OverlayTypes]'):]
    # 到下一个段头为止
    m = re.search(r'\n\s*\[', seg)
    if m:
        seg = seg[:m.start()]
    out = {}
    for line in seg.splitlines():
        line = line.split(';')[0].strip()
        if '=' not in line:
            continue
        k, v = line.split('=', 1)
        k, v = k.strip(), v.strip()
        if k.isdigit():
            out[int(k)] = v.upper()
    return out


def scan_game(game_dir, cache_dir=None):
    """扫描游戏目录下的 mix 文件，取条目最多的 [OverlayTypes] 列表"""
    best = {}
    best_src = ''
    try:
        names = [f for f in os.listdir(game_dir) if f.lower().endswith('.mix')]
    except OSError:
        names = []
    # mod 的规则通常在 expandmd*.mix / ra2md.mix 里，优先扫这些
    names.sort(key=lambda f: (0 if f.lower().startswith('expandmd') else
                              1 if f.lower() == 'ra2md.mix' else 2, f))
    for fn in names:
        p = os.path.join(game_dir, fn)
        try:
            data = open(p, 'rb').read()
        except OSError:
            continue
        pos = 0
        while True:
            i = data.find(b'[OverlayTypes]', pos)
            if i < 0:
                break
            tail = data[i:i + 40000].decode('latin-1', 'replace')
            ent = _parse_overlay_section(tail)
            if len(ent) > len(best):
                best, best_src = ent, fn
            pos = i + 1
    if cache_dir and best:
        try:
            with open(os.path.join(cache_dir, CACHE_NAME), 'w', encoding='utf-8') as f:
                json.dump({'source': best_src, 'types': best}, f, ensure_ascii=False)
        except OSError:
            pass
    return best, best_src


def load(game_dir, cache_dir=None):
    """优先用缓存，否则扫描"""
    if cache_dir:
        p = os.path.join(cache_dir, CACHE_NAME)
        if os.path.exists(p):
            try:
                with open(p, encoding='utf-8') as f:
                    d = json.load(f)
                types = {int(k): v for k, v in d.get('types', {}).items()}
                if types:
                    return types, d.get('source', 'cache')
            except (OSError, ValueError):
                pass
    return scan_game(game_dir, cache_dir)


_TIB_RE = re.compile(r'^TIB(\d+)$')
_TIB2_RE = re.compile(r'^TIB2_\d+$')
_TIB3_RE = re.compile(r'^TIB3_\d+$')
_GEM_RE = re.compile(r'^GEM\d+$')


def classify(types):
    """把 overlay 目录分类成 矿 / 宝石 / 其他矿种"""
    ore, gem, ore2, ore3 = [], [], [], []
    for i in sorted(types):
        n = types[i].upper()
        if _TIB_RE.match(n):
            ore.append(i)
        elif _TIB2_RE.match(n):
            ore2.append(i)
        elif _TIB3_RE.match(n):
            ore3.append(i)
        elif _GEM_RE.match(n):
            gem.append(i)
    res = {
        'ore': ore or VANILLA_ORE,
        'gem': gem or VANILLA_GEM,
        'ore2': ore2,
        'ore3': ore3,
        'source': 'game' if (ore or gem) else 'vanilla',
    }
    return res
