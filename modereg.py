# -*- coding: utf-8 -*-
"""
游戏模式 / MPMaps*.ini 注册表管理 + 地图包导出导入

机制（实证 + 《重聚未来1.5开发者文档》）：
- 客户端扫描 INI/MPMaps*.ini 全部文件，读 [MultiMaps] 与每张地图的注册段
- 游戏模式在 INI/MPMaps.ini 的 [GameModes] 注册；[游戏模式] 段含 UIName/Text/MapCodeININame/ForcedOptions
- [GameModeAliases] 决定"选大模式时包含哪些子模式"
- 注册段 GameModes 决定地图出现在哪个模式；[Basic] GameMode 不写则 DTA 客户端默认所有模式不可用
- Maps/Custom 里的 .map/*yrm/*mpr 由客户端自动扫描（Client/custom_map_cache 为缓存）
"""
import hashlib
import json
import os
import re
import shutil
import time
import zipfile

import ini_edit

MAP_EXT = ('.yrm', '.map', '.mpr', '.mms')
MODE_FILE = 'MPMaps.ini'
PACK_FORMAT = 'ra2map-pack'
PACK_VERSION = 1

# 中文模式名 → 地图 [Basic] GameMode 的英文键（客户端内置模式）
MODE_TO_BASIC = {
    '常规作战': 'standard', '小队结盟': 'teamgame', '生死斗': 'Meatgrind',
    '海战': 'Navalwar', '合作模式': 'Cooperative', '邪恶联盟': 'Unholy Alliance',
    '科技共享': 'Tech Share', 'Megawealth': 'Megawealth', 'Duel': 'Duel',
    'Land Rush': 'Land Rush', 'Meat Grinder': 'Meat Grinder', 'Naval War': 'Naval War',
    'Team Alliance': 'Team Alliance', 'Standard': 'Standard',
}
DEFAULT_MODE = '常规作战'


def _read_text(path):
    raw = open(path, 'rb').read()
    bom = raw.startswith(b'\xef\xbb\xbf')
    data = raw[3:] if bom else raw
    try:
        text = data.decode('utf-8')
        enc = 'utf-8'
    except UnicodeDecodeError:
        text = data.decode('gbk', 'replace')
        enc = 'gbk'
    return text, enc, bom, ('\r\n' if '\r\n' in text else '\n')


def _backup(path, backup_dir):
    """改动前备份，返回备份路径；同文件只留最近 12 份"""
    if not os.path.isfile(path):
        return ''
    os.makedirs(backup_dir, exist_ok=True)
    stamp = time.strftime('%Y%m%d-%H%M%S')
    dst = os.path.join(backup_dir, '%s.%s.bak' % (stamp, os.path.basename(path)))
    n = 1
    while os.path.exists(dst):
        dst = os.path.join(backup_dir, '%s.%s.%d.bak' % (stamp, os.path.basename(path), n))
        n += 1
    shutil.copy2(path, dst)
    key = os.path.basename(path) + '.'
    olds = sorted((f for f in os.listdir(backup_dir) if f.startswith(key.rstrip('.')) and '.bak' in f),
                  reverse=True)
    for f in olds[12:]:
        try:
            os.remove(os.path.join(backup_dir, f))
        except OSError:
            pass
    return dst


class IniDoc:
    """保序 INI 读写：保留注释、空行、行尾风格与编码"""

    def __init__(self, path):
        self.path = path
        text, self.enc, self.bom, self.nl = _read_text(path)
        self.parts = []  # [[段名或 None, [原始行...]], ...]
        cur = [None, []]
        self.sep_nl = '\r\n' if '\r\n' in text else '\n'
        for ln in text.split('\n'):
            ln = ln.rstrip('\r')
            s = ln.strip()
            if s.startswith('[') and s.endswith(']'):
                self.parts.append(cur)
                cur = [s[1:-1], []]
            else:
                cur[1].append(ln)
        self.parts.append(cur)

    def _find(self, name):
        low = name.lower()
        for p in self.parts:
            if p[0] is not None and p[0].lower() == low:
                return p
        return None

    def has(self, name):
        return self._find(name) is not None

    def lines(self, name):
        p = self._find(name)
        return p[1] if p else []

    def set_lines(self, name, lines):
        p = self._find(name)
        if p:
            p[1] = list(lines)
        else:
            self.parts.append([name, list(lines)])

    def append_section(self, name, lines):
        """追加段（若已存在则覆盖）"""
        self.set_lines(name, lines)

    def remove_section(self, name):
        low = name.lower()
        self.parts = [p for p in self.parts if p[0] is None or p[0].lower() != low]

    def get(self, name, key, default=''):
        for ln in self.lines(name):
            m = re.match(r'^\s*([^=;\s][^=]*?)\s*=\s*(.*)$', ln)
            if m and m.group(1).strip().lower() == key.lower():
                return m.group(2).strip()
        return default

    def set(self, name, key, value):
        lines = self.lines(name)
        pat = re.compile(r'^\s*' + re.escape(key) + r'\s*=', re.I)
        for i, ln in enumerate(lines):
            if pat.match(ln):
                lines[i] = '%s=%s' % (key, value)
                return
        lines.append('%s=%s' % (key, value))
        if not self.has(name):
            self.append_section(name, lines)

    def dump(self):
        out = []
        for name, lines in self.parts:
            if name is not None:
                out.append('[%s]' % name)
            out.extend(lines)
        text = '\n'.join(out)
        if self.sep_nl == '\r\n':
            text = text.replace('\n', '\r\n')
        data = text.encode(self.enc)
        return (b'\xef\xbb\xbf' if self.bom else b'') + data

    def save(self, backup_dir):
        _backup(self.path, backup_dir)
        with open(self.path, 'wb') as f:
            f.write(self.dump())


def _norm(s):
    """规范化地图键：Maps 下的相对路径、统一斜杠、去扩展名、小写"""
    s = s.strip().replace('\\', '/')
    if s.lower().startswith('maps/'):
        s = s[5:]
    root, ext = os.path.splitext(s)
    if ext.lower() in MAP_EXT:
        s = root
    # 文件名可能带尾空格（如 "xxx .mpr"），注册段名没有 → 两边都要归一化
    return s.lower().strip().strip('/').strip()


def _sec_name(rel):
    """注册表段名 → 规范化键"""
    return _norm(rel)


def reg_files(gdir):
    ini = os.path.join(gdir, 'INI')
    if not os.path.isdir(ini):
        return []
    return sorted(f for f in os.listdir(ini)
                  if f.lower().startswith('mpmaps') and f.lower().endswith('.ini'))


def reg_path(gdir, name):
    return os.path.join(gdir, 'INI', name)


def list_map_files(gdir):
    root = os.path.join(gdir, 'Maps')
    out = {}
    if not os.path.isdir(root):
        return out
    for cur, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if not d.startswith('.') and d.lower() != 'cp']
        for f in files:
            if os.path.splitext(f)[1].lower() not in MAP_EXT:
                continue
            full = os.path.join(cur, f)
            rel = os.path.relpath(full, root).replace('\\', '/')
            out[_norm(rel)] = {'rel': rel, 'path': full, 'file': f,
                               'folder': os.path.dirname(rel) or '.'}
    return out


def _map_basic(path):
    """读地图头部 [Basic]/[Map]/[Waypoints]，gbk 解码"""
    try:
        with open(path, 'rb') as f:
            raw = f.read(262144)
    except OSError:
        return {}
    t = raw.decode('gbk', 'replace')
    def body(name):
        m = re.search(r'(?ms)^\[' + re.escape(name) + r'\]\s*\n(.*?)(?=^\[|\Z)', t)
        return m.group(1) if m else ''
    def kv(b, k):
        m = re.search(r'(?mi)^\s*' + re.escape(k) + r'\s*=\s*(.*)$', b)
        return m.group(1).strip() if m else ''
    basic, mp = body('Basic'), body('Map')
    name = kv(basic, 'Name')
    if name.lower().startswith('name:'):
        name = ''
    wps = []
    for i in range(16):
        v = kv(body('Waypoints'), str(i))
        if v:
            wps.append((i, v))
    return {
        'name': name or os.path.splitext(os.path.basename(path))[0],
        'author': kv(basic, 'Author') or kv(body('Header'), 'Author'),
        'maxPlayer': kv(basic, 'MaxPlayer') or kv(body('Header'), 'NumberStartingPoints') or '0',
        'minPlayer': kv(basic, 'MinPlayer') or '1',
        'gameMode': kv(basic, 'GameMode'),
        'size': kv(mp, 'Size') or '0,0,0,0',
        'localSize': kv(mp, 'LocalSize') or '',
        'waypoints': wps,
    }


def _strip_comment(v):
    return v.split(';')[0].strip()


def _modes_of(v):
    return [x.strip() for x in _strip_comment(v).split(',') if x.strip()]


def _read_all(gdir):
    """返回 {文件名: IniDoc}"""
    docs = {}
    for f in reg_files(gdir):
        p = reg_path(gdir, f)
        try:
            docs[f] = IniDoc(p)
        except Exception:
            pass
    return docs


def _entry_maps(docs):
    """{规范化地图键: {'file','section','modes','desc'}}，取自注册段"""
    out = {}
    for f, doc in docs.items():
        for name, lines in doc.parts:
            if name is None or not name.lower().startswith('maps'):
                continue
            k = _sec_name(name)
            if not k:
                continue
            modes, desc = [], ''
            for ln in lines:
                m = re.match(r'^\s*GameModes\s*=\s*(.*)$', ln, re.I)
                if m:
                    modes = _modes_of(m.group(1))
                m = re.match(r'^\s*Description\s*=\s*(.*)$', ln, re.I)
                if m:
                    desc = m.group(1).strip()
            out[k] = {'file': f, 'section': name, 'modes': modes, 'desc': desc}
    return out


def _game_modes(doc):
    """[(编号, 名称)]，按文件顺序"""
    out = []
    for ln in doc.lines('GameModes'):
        m = re.match(r'^\s*([^=;\s]+)\s*=\s*(.*)$', ln)
        if m:
            v = _strip_comment(m.group(2))
            if v:
                out.append((m.group(1).strip(), v))
    return out


def state(gdir):
    if not gdir or not os.path.isdir(gdir):
        return {'error': '未设置游戏目录'}
    docs = _read_all(gdir)
    main = docs.get(MODE_FILE)
    modes_def = _game_modes(main) if main else []
    defined = [n for _, n in modes_def]
    entries = _entry_maps(docs)
    mapfiles = list_map_files(gdir)

    # 各模式下的地图数（注册段 GameModes）
    counts = {}
    used = {}
    for k, e in entries.items():
        for m in e['modes']:
            counts[m] = counts.get(m, 0) + 1
            used.setdefault(m, 0)
    # 未定义模式（注册段里出现但 [GameModes] 没有）
    undefined = [{'name': m, 'count': c} for m, c in sorted(counts.items(), key=lambda x: -x[1])
                 if m not in defined]

    registries = []
    for f, doc in docs.items():
        multi = [ln.strip() for ln in doc.lines('MultiMaps') if re.match(r'^\s*[^=;\s]+\s*=', ln)]
        gm = {}
        for name, lines in doc.parts:
            if name is None or not name.lower().startswith('maps'):
                continue
            for ln in lines:
                mm = re.match(r'^\s*GameModes\s*=\s*(.*)$', ln, re.I)
                if mm:
                    for x in _modes_of(mm.group(1)):
                        gm[x] = gm.get(x, 0) + 1
        registries.append({'file': f, 'multi': len(multi),
                           'sections': sum(1 for n, _ in doc.parts if n and n.lower().startswith('maps')),
                           'modes': gm})

    unreg = []
    map_modes = {}
    for k, m in sorted(mapfiles.items()):
        e = entries.get(k)
        map_modes[m['rel']] = e['modes'] if e else []
        if not e:
            unreg.append({'rel': m['rel'], 'file': m['file'], 'folder': m['folder'],
                          'name': _map_basic(m['path']).get('name', '')})

    modes = []
    for idx, name in modes_def:
        sec = main.lines(name) if main else []
        uiname, text, mapcode = name, '', ''
        for ln in sec:
            mm = re.match(r'^\s*UIName\s*=\s*(.*)$', ln, re.I)
            if mm: uiname = _strip_comment(mm.group(1))
            mm = re.match(r'^\s*Text\s*=\s*(.*)$', ln, re.I)
            if mm: text = _strip_comment(mm.group(1))
            mm = re.match(r'^\s*MapCodeININame\s*=\s*(.*)$', ln, re.I)
            if mm: mapcode = _strip_comment(mm.group(1))
        modes.append({'idx': idx, 'name': name, 'uiname': uiname, 'text': text,
                      'mapCode': mapcode, 'count': counts.get(name, 0), 'defined': True})

    aliases = []
    if main:
        for ln in main.lines('GameModeAliases'):
            mm = re.match(r'^\s*([^=;\s]+)\s*=\s*(.*)$', ln)
            if mm:
                aliases.append({'key': mm.group(1).strip(),
                                'values': [x.strip() for x in _strip_comment(mm.group(2)).split(',') if x.strip()]})

    cache = os.path.join(gdir, 'Client', 'custom_map_cache')
    return {
        'gameDir': gdir, 'modeFile': MODE_FILE,
        'modes': modes, 'aliases': aliases, 'registries': registries,
        'undefined': undefined, 'unregistered': len(unreg), 'unregisteredList': unreg,
        'registered': len(entries), 'totalMaps': len(mapfiles),
        'mapModes': map_modes, 'customCache': os.path.isfile(cache),
        'defaultMode': DEFAULT_MODE,
    }


def _replace_mode_value(v, mapping):
    parts = _modes_of(v)
    out = []
    for p in parts:
        q = mapping.get(p, p)
        if q and q not in out:
            out.append(q)
    return ','.join(out)


def save_modes(gdir, data_dir, ops):
    """新建/改名/删除/归并模式，同步所有注册表与翻译文件。ops 为 dict"""
    backup_dir = os.path.join(data_dir, '配置备份')
    docs = _read_all(gdir)
    main = docs.get(MODE_FILE)
    if not main:
        return {'error': '找不到 INI/MPMaps.ini'}
    st = state(gdir)
    defined = [m['name'] for m in st['modes']]

    mapping = {}
    for r in ops.get('rename', []) or []:
        if r.get('old') and r.get('new') and r['old'] != r['new']:
            mapping[r['old']] = r['new']
    for mg in ops.get('merge', []) or []:
        if mg.get('from') and mg.get('to'):
            mapping[mg['from']] = mg['to']
    deletes = set(ops.get('delete', []) or [])
    # 同一次请求里"新建后立刻改名/删除"也要自洽：先对新增名应用改名映射
    adds = []
    for a in (ops.get('add', []) or []):
        n = a.get('name', '').strip()
        if not n:
            continue
        n = mapping.get(n, n)
        if n not in deletes:
            adds.append(n)

    # 1) 每张注册段的 GameModes 值同步
    changed = 0
    for f, doc in docs.items():
        dirty = False
        for name, lines in doc.parts:
            if name is None or not name.lower().startswith('maps'):
                continue
            for i, ln in enumerate(lines):
                mm = re.match(r'^(\s*GameModes\s*=\s*)(.*)$', ln, re.I)
                if not mm:
                    continue
                parts = []
                for p in _modes_of(mm.group(2)):
                    q = mapping.get(p, p)
                    if q in deletes:
                        continue
                    if q and q not in parts:
                        parts.append(q)
                newv = ','.join(parts) or DEFAULT_MODE
                if newv != _strip_comment(mm.group(2)):
                    lines[i] = mm.group(1) + newv
                    dirty = True
                    changed += 1
        if dirty:
            doc.save(backup_dir)
    # 主表可能被上面保存过，重新读
    docs = _read_all(gdir)
    main = docs[MODE_FILE]

    # 2) [GameModes] 段
    gm_lines = main.lines('GameModes')
    used_idx = set()
    for i, ln in enumerate(gm_lines):
        mm = re.match(r'^(\s*[^=;\s]+\s*=\s*)(.*)$', ln)
        if not mm:
            continue
        old = _strip_comment(mm.group(2))
        if not old:
            continue
        used_idx.add(mm.group(1).split('=')[0].strip())
        if old in deletes:
            gm_lines[i] = ''
            continue
        if old in mapping:
            gm_lines[i] = mm.group(1) + mapping[old]
    # 追加新模式
    def next_idx():
        nums = [int(x) for x in used_idx if x.isdigit()]
        n = max(nums) + 1 if nums else 0
        while str(n) in used_idx:
            n += 1
        used_idx.add(str(n))
        return str(n)
    add_meta = {}
    for a in (ops.get('add', []) or []):
        n = a.get('name', '').strip()
        if n:
            add_meta[mapping.get(n, n)] = a
    for name in adds:
        if name in defined or name in mapping.values():
            continue
        gm_lines.append('%s=%s' % (next_idx(), name))
    gm_lines = [l for l in gm_lines if l != '']
    main.set_lines('GameModes', gm_lines)

    # 3) 模式段：改名 / 删除 / 新建
    for old, new in mapping.items():
        for part in main.parts:
            name, lines = part
            if name is None or name.lower() != old.lower():
                continue
            part[0] = new
            for i, ln in enumerate(lines):
                mm = re.match(r'^(\s*UIName\s*=\s*)(.*)$', ln, re.I)
                if mm and _strip_comment(mm.group(2)) == old:
                    lines[i] = mm.group(1) + new
    for name in deletes:
        main.remove_section(name)
    for name in adds:
        if name in defined or main.has(name):
            continue
        meta = add_meta.get(name, {})
        lines = ['UIName = %s' % name,
                 'Text = %s' % (meta.get('text') or name),
                 'MapCodeININame=%s' % (meta.get('mapCode') or 'Standard.ini')]
        main.append_section(name, lines)

    # 4) [GameModeAliases]
    al = main.lines('GameModeAliases')
    for i, ln in enumerate(al):
        mm = re.match(r'^(\s*[^=;\s]+\s*=\s*)(.*)$', ln)
        if not mm:
            continue
        key = mm.group(1).split('=')[0].strip()
        vals = [mapping.get(v, v) for v in _modes_of(mm.group(2)) if v not in deletes]
        key = mapping.get(key, key)
        al[i] = '%s=%s' % (key, ','.join(vals))
    if ops.get('aliases') is not None:
        al = ['%s=%s' % (a['key'], ','.join(a['values'])) for a in ops['aliases']]
    main.set_lines('GameModeAliases', al)
    main.save(backup_dir)

    # 5) Translation.ini 的 UI:ModeText 键跟着改名
    trans = os.path.join(gdir, 'Resources', 'Translation.ini')
    if mapping and os.path.isfile(trans):
        try:
            text, enc, bom, nl = _read_text(trans)
            for old, new in mapping.items():
                text = re.sub(r'(?m)^(UI:ModeText:)' + re.escape(old) + r'=',
                              lambda m: m.group(1) + new + '=', text)
            _backup(trans, backup_dir)
            with open(trans, 'wb') as f:
                f.write((b'\xef\xbb\xbf' if bom else b'') + text.encode(enc))
        except Exception:
            pass

    after = state(gdir)
    return {'ok': True, 'changedEntries': changed,
            'modes': len(after['modes']), 'undefined': len(after['undefined']),
            'backupDir': backup_dir}


def _pick_target(gdir, folder, target):
    if target and target not in ('auto', ''):
        return target if target in reg_files(gdir) else target
    fl = (folder or '').lower()
    if 'ddly' in fl and 'MPMapsddly.ini' in reg_files(gdir):
        return 'MPMapsddly.ini'
    if 'madhq' in fl and 'MPMapsMadHq.ini' in reg_files(gdir):
        return 'MPMapsMadHq.ini'
    return MODE_FILE


def _make_section(rel, meta, mode, sep):
    stem = os.path.splitext(os.path.basename(rel))[0]
    folder = os.path.dirname(rel).replace('/', sep)
    sec = sep.join(['Maps'] + ([folder] if folder else []) + [stem])
    try:
        mx = int(str(meta.get('maxPlayer', '0')).strip() or 0)
    except ValueError:
        mx = 0
    try:
        mn = int(str(meta.get('minPlayer', '1')).strip() or 1)
    except ValueError:
        mn = 1
    lines = [
        'MinPlayers=%d' % (mn or 1),
        'MaxPlayers=%d' % (mx or 8),
        'Description=[%d]%s' % (mx or 8, meta.get('name') or stem),
        'Author=%s' % (meta.get('author') or ''),
        'EnforceMaxPlayers=true',
        'Size=%s' % (meta.get('size') or '0,0,0,0'),
    ]
    if meta.get('localSize'):
        lines.append('LocalSize=%s' % meta['localSize'])
    lines.append('GameModes=%s' % mode)
    for i, v in (meta.get('waypoints') or [])[:max(0, min(mx or 8, 8))]:
        lines.append('Waypoint%d=%s' % (i, v))
    return sec, lines


def _ensure_basic_gamemode(path, basic_value):
    """字节级最小插入：只给没有 GameMode 的 [Basic] 段补一行，不动其他字节"""
    try:
        raw = open(path, 'rb').read()
    except OSError:
        return False
    m = re.search(rb'(?im)^\[Basic\][ \t]*(\r?\n)', raw)
    if not m:
        return False
    start = m.end()
    m2 = re.search(rb'(?m)^\[[^\]]+\][ \t]*\r?\n', raw[start:])
    end = start + (m2.start() if m2 else len(raw) - start)
    if re.search(rb'(?im)^[ \t]*GameMode[ \t]*=', raw[start:end]):
        return False
    nl = b'\r\n' if m.group(1) == b'\r\n' else b'\n'
    out = raw[:start] + b'GameMode=' + basic_value.encode('ascii', 'replace') + nl + raw[start:]
    with open(path, 'wb') as f:
        f.write(out)
    return True


def autoreg(gdir, data_dir, ids, target='auto', mode=DEFAULT_MODE, fix_basic=True):
    """把未注册地图补进 MPMaps*.ini（[MultiMaps] + 注册段）"""
    backup_dir = os.path.join(data_dir, '配置备份')
    mapfiles = list_map_files(gdir)
    entries = _entry_maps(_read_all(gdir))
    if ids and ids[0] == '*':
        keys = [k for k in mapfiles if k not in entries]
    else:
        keys = []
        root = os.path.join(gdir, 'Maps')
        for i in (ids or []):
            k = _norm(i)
            if k not in mapfiles and os.path.isabs(str(i)):
                try:
                    k = _norm(os.path.relpath(i, root).replace('\\', '/'))
                except ValueError:
                    pass
            if k in mapfiles and k not in entries:
                keys.append(k)
    if not keys:
        return {'ok': True, 'registered': 0, 'skipped': len(ids or []), 'basicFixed': 0,
                'files': [], 'message': '没有需要补注册的地图'}

    by_file = {}
    basic_fixed = 0
    basic_value = MODE_TO_BASIC.get(mode, 'standard')
    for k in keys:
        m = mapfiles[k]
        f = _pick_target(gdir, m['folder'], target)
        by_file.setdefault(f, []).append((k, m))
        if fix_basic and _ensure_basic_gamemode(m['path'], basic_value):
            basic_fixed += 1

    result_files = []
    done = 0
    for f, items in by_file.items():
        p = reg_path(gdir, f)
        doc = IniDoc(p) if os.path.isfile(p) else None
        if doc is None:
            # 新建注册表
            ini = os.path.join(gdir, 'INI')
            os.makedirs(ini, exist_ok=True)
            open(p, 'wb').write(b'[MultiMaps]\r\n')
            doc = IniDoc(p)
        # 分隔符风格跟随该文件已有段名
        sep = '/'
        for name, _ in doc.parts:
            if name and name.lower().startswith('maps') and '\\' in name:
                sep = '\\'
                break
        multi = doc.lines('MultiMaps')
        nums = [int(mm.group(1)) for ln in multi
                if (mm := re.match(r'^\s*(\d+)\s*=', ln))]
        n = max(nums) + 1 if nums else 1
        for k, m in items:
            meta = _map_basic(m['path'])
            sec, lines = _make_section(m['rel'], meta, mode, sep)
            if doc.has(sec):
                continue
            while any(ln.strip().startswith('%d=' % n) for ln in multi):
                n += 1
            multi.append('%d=%s' % (n, sec))
            n += 1
            doc.append_section(sec, lines)
            done += 1
        doc.set_lines('MultiMaps', multi)
        doc.save(backup_dir)
        result_files.append({'file': f, 'added': sum(1 for _ in items)})
    return {'ok': True, 'registered': done, 'basicFixed': basic_fixed,
            'files': result_files, 'backupDir': backup_dir}


def unregister(gdir, data_dir, paths):
    """删除地图时清理注册项，返回可写入回收站清单的片段"""
    backup_dir = os.path.join(data_dir, '配置备份')
    keys = set()
    for p in paths:
        root = os.path.join(gdir, 'Maps')
        try:
            keys.add(_norm(os.path.relpath(p, root).replace('\\', '/')))
        except ValueError:
            pass
    if not keys:
        return []
    removed = []
    for f in reg_files(gdir):
        p = reg_path(gdir, f)
        doc = IniDoc(p)
        dirty = False
        for name, lines in list(doc.parts):
            if name is None or _sec_name(name) not in keys:
                continue
            removed.append({'file': f, 'section': name, 'lines': list(lines)})
            doc.remove_section(name)
            dirty = True
        ml = doc.lines('MultiMaps')
        keep = []
        for ln in ml:
            mm = re.match(r'^\s*([^=;\s]+)\s*=\s*(.*)$', ln)
            if mm and _sec_name(mm.group(2)) in keys:
                removed.append({'file': f, 'multiKey': mm.group(1).strip(),
                                'multiValue': mm.group(2).strip()})
                dirty = True
                continue
            keep.append(ln)
        if dirty:
            doc.set_lines('MultiMaps', keep)
            doc.save(backup_dir)
    return removed


def restore_reg(gdir, data_dir, fragments):
    """回收站还原时把注册项写回"""
    if not fragments:
        return 0
    backup_dir = os.path.join(data_dir, '配置备份')
    by_file = {}
    for fr in fragments:
        by_file.setdefault(fr['file'], []).append(fr)
    n = 0
    for f, frs in by_file.items():
        p = reg_path(gdir, f)
        if not os.path.isfile(p):
            continue
        doc = IniDoc(p)
        dirty = False
        for fr in frs:
            if 'section' in fr and not doc.has(fr['section']):
                doc.append_section(fr['section'], fr.get('lines') or [])
                dirty = True
                n += 1
        ml = doc.lines('MultiMaps')
        for fr in frs:
            if 'multiKey' not in fr:
                continue
            if any(re.match(r'^\s*' + re.escape(fr['multiKey']) + r'\s*=', ln) for ln in ml):
                continue
            ml.append('%s=%s' % (fr['multiKey'], fr['multiValue']))
            dirty = True
        if dirty:
            doc.set_lines('MultiMaps', ml)
            doc.save(backup_dir)
    return n


def _sha1(path):
    h = hashlib.sha1()
    with open(path, 'rb') as f:
        while True:
            b = f.read(1024 * 1024)
            if not b:
                break
            h.update(b)
    return h.hexdigest()


def build_pack(gdir, data_dir, ids, opts, out_path):
    """导出地图包（zip）：manifest.json + maps/<相对路径> + 模式定义 + 注册条目"""
    include_modes = bool(opts.get('includeModes', True))
    include_reg = bool(opts.get('includeReg', True))
    mapfiles = list_map_files(gdir)
    docs = _read_all(gdir)
    entries = _entry_maps(docs)
    if ids and ids[0] == '*':
        keys = sorted(mapfiles)
    else:
        keys = [_norm(i) for i in (ids or []) if _norm(i) in mapfiles]
    keys = list(dict.fromkeys(keys))

    main = docs.get(MODE_FILE)
    modes_block = {'gameModes': [], 'sections': [], 'aliases': []}
    if include_modes and main:
        modes_block['gameModes'] = [list(x) for x in _game_modes(main)]
        modes_block['aliases'] = [ln for ln in main.lines('GameModeAliases') if '=' in ln]
        names = [n for _, n in modes_block['gameModes']]
        for name, lines in main.parts:
            if name and name in names:
                modes_block['sections'].append({'name': name, 'lines': list(lines)})

    reg_entries = []
    if include_reg:
        for k in keys:
            e = entries.get(k)
            if not e:
                continue
            doc = docs.get(e['file'])
            if not doc:
                continue
            multi = ''
            for ln in doc.lines('MultiMaps'):
                mm = re.match(r'^\s*([^=;\s]+)\s*=\s*(.*)$', ln)
                if mm and _sec_name(mm.group(2)) == k:
                    multi = ln.strip()
                    break
            reg_entries.append({'file': e['file'], 'section': e['section'],
                                'lines': list(doc.lines(e['section'])), 'multi': multi})

    maps = []
    for k in keys:
        m = mapfiles[k]
        maps.append({'rel': m['rel'], 'size': os.path.getsize(m['path']),
                     'sha1': _sha1(m['path'])})
    manifest = {
        'format': PACK_FORMAT, 'version': PACK_VERSION,
        'created': time.strftime('%Y-%m-%d %H:%M:%S'),
        'game': os.path.basename(gdir.rstrip('\\/')),
        'includeModes': include_modes, 'includeReg': include_reg,
        'maps': maps, 'modes': modes_block, 'regEntries': reg_entries,
    }
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with zipfile.ZipFile(out_path, 'w', zipfile.ZIP_DEFLATED, compresslevel=6) as z:
        z.writestr('manifest.json', json.dumps(manifest, ensure_ascii=False, indent=1).encode('utf-8'))
        for m in maps:
            z.write(mapfiles[_norm(m['rel'])]['path'], 'maps/' + m['rel'])
    return {'ok': True, 'path': out_path, 'size': os.path.getsize(out_path),
            'maps': len(maps), 'regEntries': len(reg_entries),
            'modes': len(modes_block['gameModes'])}


def _is_pack(data):
    """判断上传的 zip 是不是本管理器导出的地图包"""
    import io
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            if 'manifest.json' not in z.namelist():
                return None
            man = json.loads(z.read('manifest.json').decode('utf-8'))
            return man if man.get('format') == PACK_FORMAT else None
    except Exception:
        return None


def peek_pack(data):
    man = _is_pack(data)
    if not man:
        return None
    return {'format': man.get('format'), 'created': man.get('created'), 'game': man.get('game'),
            'maps': len(man.get('maps') or []), 'regEntries': len(man.get('regEntries') or []),
            'modes': len((man.get('modes') or {}).get('gameModes') or []),
            'includeModes': man.get('includeModes'), 'includeReg': man.get('includeReg')}


def install_pack(gdir, data_dir, data, mode='skip', apply_modes=True, apply_reg=True):
    """安装地图包：地图落盘 + 注册条目合并 + 模式定义合并（全部相对路径，可移植）"""
    import io
    man = _is_pack(data)
    if not man:
        return {'error': '不是有效的地图配置包'}
    backup_dir = os.path.join(data_dir, '配置备份')
    root = os.path.join(gdir, 'Maps')
    os.makedirs(root, exist_ok=True)
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        names = set(z.namelist())
        installed = skipped = renamed = 0
        installed_keys = []
        for m in man.get('maps') or []:
            rel = m['rel'].replace('\\', '/')
            if rel.startswith('/') or '..' in rel.split('/'):
                continue
            src = 'maps/' + rel
            if src not in names:
                continue
            dst = os.path.join(root, rel.replace('/', os.sep))
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            if os.path.exists(dst):
                if mode == 'skip':
                    skipped += 1
                    installed_keys.append(_norm(rel))
                    continue
                if mode == 'rename':
                    stem, ext = os.path.splitext(dst)
                    i = 2
                    while os.path.exists('%s(%d)%s' % (stem, i, ext)):
                        i += 1
                    dst = '%s(%d)%s' % (stem, i, ext)
                    renamed += 1
            with z.open(src) as f, open(dst, 'wb') as o:
                shutil.copyfileobj(f, o)
            installed += 1
            installed_keys.append(_norm(os.path.relpath(dst, root).replace('\\', '/')))

        # 注册条目合并
        reg_added = 0
        if apply_reg and man.get('includeReg'):
            by_file = {}
            for e in man.get('regEntries') or []:
                by_file.setdefault(e['file'], []).append(e)
            for f, ents in by_file.items():
                p = reg_path(gdir, f)
                if not os.path.isfile(p):
                    # 目标注册表不存在 → 落到主表，避免凭空建表
                    f = MODE_FILE
                    p = reg_path(gdir, f)
                doc = IniDoc(p)
                ml = doc.lines('MultiMaps')
                nums = [int(mm.group(1)) for ln in ml
                        if (mm := re.match(r'^\s*(\d+)\s*=', ln))]
                n = max(nums) + 1 if nums else 1
                dirty = False
                for e in ents:
                    if doc.has(e['section']):
                        continue
                    doc.append_section(e['section'], e.get('lines') or [])
                    key = ''
                    if e.get('multi'):
                        mm = re.match(r'^\s*([^=;\s]+)\s*=\s*(.*)$', e['multi'])
                        if mm:
                            key = mm.group(1).strip()
                    if not key or any(re.match(r'^\s*' + re.escape(key) + r'\s*=', ln) for ln in ml):
                        while any(ln.strip().startswith('%d=' % n) for ln in ml):
                            n += 1
                        key = str(n)
                        n += 1
                    ml.append('%s=%s' % (key, e['section']))
                    dirty = True
                    reg_added += 1
                if dirty:
                    doc.set_lines('MultiMaps', ml)
                    doc.save(backup_dir)

        # 模式定义合并（只补缺失，不覆盖）
        modes_added = 0
        if apply_modes and man.get('includeModes'):
            mb = man.get('modes') or {}
            p = reg_path(gdir, MODE_FILE)
            if os.path.isfile(p):
                doc = IniDoc(p)
                cur = dict((name, idx) for idx, name in _game_modes(doc))
                gm = doc.lines('GameModes')
                nums = [int(x) for x in cur.values() if x.isdigit()]
                n = max(nums) + 1 if nums else 0
                for idx, name in mb.get('gameModes') or []:
                    if name in cur:
                        continue
                    while str(n) in cur.values():
                        n += 1
                    gm.append('%d=%s' % (n, name))
                    cur[name] = str(n)
                    n += 1
                    modes_added += 1
                    for sec in mb.get('sections') or []:
                        if sec['name'] == name and not doc.has(name):
                            doc.append_section(name, sec.get('lines') or [])
                if modes_added:
                    doc.set_lines('GameModes', gm)
                    doc.save(backup_dir)

    return {'ok': True, 'installed': installed, 'skipped': skipped, 'renamed': renamed,
            'regAdded': reg_added, 'modesAdded': modes_added,
            'backupDir': backup_dir,
            'maps': [(m.get('rel'), m.get('size')) for m in (man.get('maps') or [])][:200]}


# ---------------------------------------------------------------- 地图归类 / 批量移动
IMG_EXT = ('.jpg', '.jpeg', '.png', '.bmp', '.gif', '.tga', '.pcx')


def _resolve_ids(gdir, ids):
    """把 id（相对路径或绝对路径）解析成 mapfiles 的 key，保序去重"""
    mapfiles = list_map_files(gdir)
    root = os.path.join(gdir, 'Maps')
    keys, missing = [], []
    for i in (ids or []):
        k = _norm(str(i))
        if k not in mapfiles and os.path.isabs(str(i)):
            try:
                k = _norm(os.path.relpath(str(i), root).replace('\\', '/'))
            except ValueError:
                pass
        if k in mapfiles and k not in keys:
            keys.append(k)
        elif k not in mapfiles:
            missing.append(str(i))
    return mapfiles, keys, missing


def _doc_sep(doc):
    for name, _ in doc.parts:
        if name and name.lower().startswith('maps') and '\\' in name:
            return '\\'
    return '/'


def _get_doc(gdir, name, docs):
    p = reg_path(gdir, name)
    if not os.path.isfile(p):
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, 'wb') as f:
            f.write(b'[MultiMaps]\r\n')
        docs[name] = IniDoc(p)
    elif name not in docs:
        docs[name] = IniDoc(p)
    return docs[name]


def _add_registration(doc, rel, meta, modes, sep):
    """给未注册地图补 [MultiMaps] 编号 + 注册段，返回 (段名, 是否新增)"""
    sec, lines = _make_section(rel, meta, ','.join(modes), sep)
    if doc.has(sec):
        return sec, False
    multi = doc.lines('MultiMaps')
    nums = [int(mm.group(1)) for ln in multi
            if (mm := re.match(r'^\s*(\d+)\s*=', ln))]
    n = max(nums) + 1 if nums else 1
    while any(ln.strip().startswith('%d=' % n) for ln in multi):
        n += 1
    multi.append('%d=%s' % (n, sec))
    doc.set_lines('MultiMaps', multi)
    doc.append_section(sec, lines)
    return sec, True


def set_map_modes(gdir, data_dir, ids, modes, op='replace', target='auto', fix_basic=True):
    """把一批地图移入 / 追加 / 移出指定游戏模式。

    ids：地图相对路径（同 /api/maps 的 id）或绝对路径，单张批量均可。
    未注册地图会先补注册段，避免「移动完游戏里还是看不到」。
    改动前每个 MPMaps*.ini 自动备份到 数据/配置备份/。
    """
    if isinstance(modes, str):
        modes = [x.strip() for x in modes.split(',') if x.strip()]
    modes = [str(m).strip() for m in (modes or []) if str(m).strip()]
    if op not in ('replace', 'append', 'remove'):
        return {'ok': False, 'error': '未知操作: %s' % op}
    if not modes and op != 'remove':
        return {'ok': False, 'error': '没有指定目标模式'}
    backup_dir = os.path.join(data_dir, '配置备份')
    mapfiles, keys, missing = _resolve_ids(gdir, ids)
    if not keys:
        return {'ok': False, 'error': '没有找到要移动的地图', 'missing': missing}

    docs = _read_all(gdir)
    entries = _entry_maps(docs)
    dirty, details = set(), []
    registered = basic_fixed = 0

    # 1) 已注册：直接改 GameModes=；保留注册段其余字段
    for k in keys:
        e = entries.get(k)
        if not e:
            continue
        doc = _get_doc(gdir, e['file'], docs)
        old = list(e['modes'])
        if op == 'replace':
            new = list(modes)
        elif op == 'append':
            new = list(dict.fromkeys(old + modes))
        else:
            new = [x for x in old if x not in modes] or [DEFAULT_MODE]
        doc.set(e['section'], 'GameModes', ','.join(new))
        dirty.add(e['file'])
        details.append({'rel': mapfiles[k]['rel'], 'action': 'set',
                        'from': old, 'to': new, 'file': e['file']})

    # 2) 未注册：按目标注册表分组补注册（按目录自动挑 MPMaps*.ini）
    groups = {}
    for k in keys:
        if k in entries:
            continue
        f = _pick_target(gdir, mapfiles[k]['folder'], target)
        groups.setdefault(f, []).append(k)
    basic_value = MODE_TO_BASIC.get(modes[0] if modes else DEFAULT_MODE, 'standard')
    for f, ks in groups.items():
        doc = _get_doc(gdir, f, docs)
        sep = _doc_sep(doc)
        for k in ks:
            m = mapfiles[k]
            want = list(dict.fromkeys(modes)) if op in ('replace', 'append') else [DEFAULT_MODE]
            sec, added = _add_registration(doc, m['rel'], _map_basic(m['path']), want, sep)
            dirty.add(f)
            registered += 1 if added else 0
            if fix_basic and _ensure_basic_gamemode(m['path'], basic_value):
                basic_fixed += 1
            details.append({'rel': m['rel'], 'action': 'register',
                            'from': [], 'to': want, 'file': f, 'section': sec})

    # 3) 每个文件只落盘一次
    for f in sorted(dirty):
        docs[f].save(backup_dir)
    return {'ok': True, 'changed': len(details), 'registered': registered,
            'basicFixed': basic_fixed, 'mode': ','.join(modes), 'op': op,
            'files': sorted(dirty), 'missing': missing,
            'details': details[:200], 'backupDir': backup_dir}


def _new_section_name(old_sec, dst_rel):
    """按原段名的斜杠风格，生成 Maps/<新目录>/<新文件名> 段名"""
    sep = '\\' if '\\' in str(old_sec) else '/'
    parts = [p for p in dst_rel.split('/') if p]
    stem = os.path.splitext(parts[-1])[0]
    return sep.join(['Maps'] + parts[:-1] + [stem])


def _sidecar_images(path):
    stem = os.path.splitext(path)[0]
    out = []
    for ie in IMG_EXT:
        for cand in (stem + ie, stem + ie.upper()):
            if os.path.isfile(cand) and cand not in out:
                out.append(cand)
                break
    return out


def move_maps(gdir, data_dir, ids, folder, conflict='rename'):
    """把一批地图物理移动到 Maps 下的另一个目录，并同步改注册段名 / [MultiMaps] 路径。

    注册项保留在原来的 MPMaps*.ini 里（客户端会扫描全部注册表），只更新地图路径；
    文件移动或配置写入任一步失败都会整体回滚。
    """
    folder = str(folder or '').strip().replace('\\', '/').strip('/')
    if not folder or folder.startswith('.') or '..' in folder.split('/'):
        return {'ok': False, 'error': '目标目录不合法'}
    if conflict not in ('rename', 'skip'):
        return {'ok': False, 'error': '同名处理方式不合法'}
    root = os.path.join(gdir, 'Maps')
    dst_dir = os.path.normpath(os.path.join(root, *folder.split('/')))
    if not dst_dir.lower().startswith(os.path.normpath(root).lower()):
        return {'ok': False, 'error': '目标目录不在 Maps 下'}
    backup_dir = os.path.join(data_dir, '配置备份')
    mapfiles, keys, missing = _resolve_ids(gdir, ids)
    if not keys:
        return {'ok': False, 'error': '没有找到要移动的地图', 'missing': missing}

    plan, skipped, taken = [], [], set()
    for k in keys:
        m = mapfiles[k]
        src = m['path']
        if os.path.normcase(os.path.dirname(src)) == os.path.normcase(dst_dir):
            skipped.append({'rel': m['rel'], 'reason': '已在该目录'})
            continue
        dst = os.path.join(dst_dir, m['file'])
        if os.path.exists(dst) or os.path.normcase(dst) in taken:
            if conflict == 'skip':
                skipped.append({'rel': m['rel'], 'reason': '目标已有同名'})
                continue
            stem, ext = os.path.splitext(m['file'])
            i = 2
            while os.path.exists(dst) or os.path.normcase(dst) in taken:
                dst = os.path.join(dst_dir, '%s (%d)%s' % (stem, i, ext))
                i += 1
        taken.add(os.path.normcase(dst))
        new_rel = os.path.relpath(dst, root).replace('\\', '/')
        plan.append({'key': k, 'src': src, 'dst': dst,
                     'oldRel': m['rel'], 'newRel': new_rel})
    if not plan:
        return {'ok': True, 'changed': 0, 'skipped': skipped, 'files': [],
                'message': '没有需要移动的地图', 'backupDir': backup_dir}

    os.makedirs(dst_dir, exist_ok=True)
    moved = []  # (src, dst, [(imgSrc, imgDst)...])
    try:
        for p in plan:
            imgs = _sidecar_images(p['src'])
            shutil.move(p['src'], p['dst'])
            pairs = []
            for ip in imgs:
                stem = os.path.splitext(p['dst'])[0]
                idst = stem + os.path.splitext(ip)[1]
                if os.path.normcase(ip) == os.path.normcase(idst):
                    continue
                try:
                    shutil.move(ip, idst)
                    pairs.append((ip, idst))
                except OSError:
                    pass
            moved.append((p['src'], p['dst'], pairs))
    except Exception as e:
        for src, dst, pairs in reversed(moved):
            try:
                shutil.move(dst, src)
            except OSError:
                pass
            for a, b in reversed(pairs):
                try:
                    shutil.move(b, a)
                except OSError:
                    pass
        return {'ok': False, 'error': '移动文件失败，已回滚: %s' % e}

    def rollback():
        for path, bak in saved:
            if bak and os.path.isfile(bak):
                try:
                    shutil.copy2(bak, path)
                except OSError:
                    pass
        for src, dst, pairs in reversed(moved):
            try:
                shutil.move(dst, src)
            except OSError:
                pass
            for a, b in reversed(pairs):
                try:
                    shutil.move(b, a)
                except OSError:
                    pass

    saved = []
    key_to = {p['key']: p for p in plan}
    try:
        for f, doc in _read_all(gdir).items():
            path = reg_path(gdir, f)
            changed = False
            for part in doc.parts:
                name = part[0]
                if not name or not name.lower().startswith('maps'):
                    continue
                p = key_to.get(_sec_name(name))
                if p:
                    part[0] = _new_section_name(name, p['newRel'])
                    changed = True
            out = []
            for ln in doc.lines('MultiMaps'):
                mm = re.match(r'^\s*([^=;\s]+)\s*=\s*(.*)$', ln)
                p = key_to.get(_sec_name(mm.group(2))) if mm else None
                if p:
                    out.append('%s=%s' % (mm.group(1).strip(),
                                          _new_section_name(mm.group(2).strip(), p['newRel'])))
                    changed = True
                else:
                    out.append(ln)
            if changed:
                doc.set_lines('MultiMaps', out)
                bak = _backup(path, backup_dir)
                with open(path, 'wb') as fh:
                    fh.write(doc.dump())
                saved.append((path, bak))
    except Exception as e:
        rollback()
        return {'ok': False, 'error': '更新注册表失败，已回滚: %s' % e}

    return {'ok': True, 'changed': len(plan), 'skipped': skipped,
            'files': [p['newRel'] for p in plan], 'backupDir': backup_dir}


_BAD_NAME_CHARS = set('\\/:*?"<>|')
_WIN_RESERVED = {'con', 'prn', 'aux', 'nul', 'com1', 'com2', 'com3', 'com4',
                 'com5', 'com6', 'com7', 'com8', 'com9', 'lpt1', 'lpt2',
                 'lpt3', 'lpt4', 'lpt5', 'lpt6', 'lpt7', 'lpt8', 'lpt9'}


def rename_map(gdir, data_dir, map_id, new_name, opts=None):
    """快速重命名一张地图。

    - 文件名 + 同名预览图一起改；同名可选自动加 (2)
    - 可选写地图 [Basic] Name（地图编辑器里显示的名字）
    - 可选同步 MPMaps*.ini：注册段名、[MultiMaps] 值、注册段 Description（大厅显示名）
    - 任一步失败整体回滚；INI 与原地图文件改动前都备份
    """
    opts = opts or {}
    do_basic = opts.get('basic', True)
    do_reg = opts.get('reg', True)
    do_desc = opts.get('description', True)
    conflict = opts.get('conflict', 'rename')
    if conflict not in ('rename', 'error'):
        conflict = 'rename'

    name = str(new_name or '').strip().strip('"').strip()
    stem_in, ext_in = os.path.splitext(name)
    if ext_in.lower() in MAP_EXT:
        name = stem_in.strip()
    if not name:
        return {'ok': False, 'error': '新名称不能为空'}
    if any(c in _BAD_NAME_CHARS or ord(c) < 32 for c in name):
        return {'ok': False, 'error': '名称含非法字符 \\ / : * ? " < > |'}
    if name.startswith('.') or name.endswith('.') or name != name.strip():
        return {'ok': False, 'error': '名称不能以点开头/结尾，也不能有首尾空格'}
    if name.split('.')[0].lower() in _WIN_RESERVED:
        return {'ok': False, 'error': '这是 Windows 保留文件名，请换一个'}
    if len(name) > 120:
        return {'ok': False, 'error': '名称太长（最多 120 字）'}

    root = os.path.join(gdir, 'Maps')
    mapfiles, keys, missing = _resolve_ids(gdir, [map_id])
    if not keys:
        return {'ok': False, 'error': '找不到这张地图', 'missing': missing}
    m = mapfiles[keys[0]]
    src = m['path']
    if not os.path.isfile(src):
        return {'ok': False, 'error': '地图文件不存在'}
    old_file = m['file']
    old_stem, ext = os.path.splitext(old_file)
    old_key = keys[0]

    dst = os.path.join(os.path.dirname(src), name + ext)
    same_file = os.path.normcase(dst) == os.path.normcase(src)
    if not same_file and os.path.exists(dst):
        if conflict == 'error':
            return {'ok': False, 'error': '已经有同名地图「%s」' % (name + ext), 'conflict': True}
        i = 2
        while os.path.exists(dst):
            dst = os.path.join(os.path.dirname(src), '%s (%d)%s' % (name, i, ext))
            i += 1
    real_name = os.path.splitext(os.path.basename(dst))[0]
    new_rel = os.path.relpath(dst, root).replace('\\', '/')

    backup_dir = os.path.join(data_dir, '配置备份')
    map_backup_dir = os.path.join(data_dir, '地图备份')
    saved = []          # (ini 路径, 备份路径)
    moved = []          # (原路径, 新路径)
    map_bak = ''
    basic_updated = False
    reg_files_changed = []
    desc_updated = 0

    def rollback():
        for path, bak in saved:
            if bak and os.path.isfile(bak):
                try:
                    shutil.copy2(bak, path)
                except OSError:
                    pass
        if map_bak and os.path.isfile(map_bak):
            cur = dst if os.path.isfile(dst) else src
            try:
                shutil.copy2(map_bak, cur)
            except OSError:
                pass
        for a, b in reversed(moved):
            try:
                shutil.move(b, a)
            except OSError:
                pass

    try:
        # 1) 改地图内部 [Basic] Name（文件还在原位，先备份原文件）
        if do_basic:
            doc = ini_edit.MapIni(src)
            doc.apply([{'op': 'set', 'section': 'Basic', 'occ': 0,
                        'key': 'Name', 'value': real_name}])
            if doc.is_changed():
                map_bak = doc.save(backup_root=map_backup_dir)
                basic_updated = True

        # 2) 改文件名 + 同名预览图
        imgs = _sidecar_images(src)
        if not same_file:
            shutil.move(src, dst)
            moved.append((src, dst))
        for ip in imgs:
            idst = os.path.join(os.path.dirname(dst), real_name + os.path.splitext(ip)[1])
            if os.path.normcase(ip) == os.path.normcase(idst):
                continue
            if os.path.isfile(ip) and not os.path.exists(idst):
                shutil.move(ip, idst)
                moved.append((ip, idst))

        # 3) 同步注册表：注册段名 / [MultiMaps] 值 / Description
        if do_reg:
            for f, doc in _read_all(gdir).items():
                path = reg_path(gdir, f)
                changed = False
                for part in doc.parts:
                    nm = part[0]
                    if not (nm and nm.lower().startswith('maps') and _sec_name(nm) == old_key):
                        continue
                    new_sec = _new_section_name(nm, new_rel)
                    part[0] = new_sec
                    changed = True
                    if do_desc:
                        ls = list(doc.lines(new_sec))
                        hit = False
                        for i, ln in enumerate(ls):
                            if re.match(r'^\s*Description\s*=', ln, re.I):
                                ls[i] = 'Description=%s' % real_name
                                hit = True
                                break
                        if not hit:
                            ls.append('Description=%s' % real_name)
                        doc.set_lines(new_sec, ls)
                        desc_updated += 1
                out = []
                for ln in doc.lines('MultiMaps'):
                    mm = re.match(r'^\s*([^=;\s]+)\s*=\s*(.*)$', ln)
                    if mm and _sec_name(mm.group(2).strip()) == old_key:
                        out.append('%s=%s' % (mm.group(1).strip(),
                                              _new_section_name(mm.group(2).strip(), new_rel)))
                        changed = True
                    else:
                        out.append(ln)
                if changed:
                    doc.set_lines('MultiMaps', out)
                    bak = _backup(path, backup_dir)
                    with open(path, 'wb') as fh:
                        fh.write(doc.dump())
                    saved.append((path, bak))
                    reg_files_changed.append(f)
    except Exception as e:
        rollback()
        return {'ok': False, 'error': '重命名失败，已回滚：%s' % e}

    return {
        'ok': True, 'id': new_rel, 'file': os.path.basename(dst),
        'oldId': m['rel'], 'oldFile': old_file, 'name': real_name,
        'basicUpdated': basic_updated, 'regUpdated': len(reg_files_changed),
        'regFiles': reg_files_changed, 'descUpdated': desc_updated,
        'imagesMoved': max(0, len(moved) - (0 if same_file else 1)),
        'backup': map_bak, 'backupDir': backup_dir,
    }
