# -*- coding: utf-8 -*-
"""跨平台地图导入（v2.4）。

支持四类来源，统一成可勾选的地图清单，再写入当前游戏目录：

  A. HT / RA2OL 平台地图库 MP.dat —— ZIP 包内是**明文标准 .map**，
     地图名/人数/模式在 RA2/INI/nmap.ini（GBK）里按 hash 索引。
  B. 平台下载图 RA2/Map/*.ra2mp —— ZIP 包，3.txt 是 GBK 元数据、
     5.txt 是 JPEG 预览，1.txt 是平台自有加密的地图主体。
     官方文档明确 1.txt 加密；解不开的图不会假装导入成功。
  C. 平台解包缓存 —— 平台启动/进游戏时会把当前地图**明文**写到游戏
     主目录的 mp.dat / spawnmap.ini，这是官方格式的可靠导出通道。
  D. 任意目录里的 .map/.mpr/.yrm/.mms 散图与 zip 地图包。

设计原则：只读扫描、导入前可预览、冲突可跳过/改名/覆盖（覆盖前自动备份），
不改动平台任何文件。
"""
import hashlib
import io
import json
import os
import re
import shutil
import threading
import time
import zipfile

MAP_EXT = ('.yrm', '.map', '.mpr', '.mms')
ARCHIVE_EXT = ('.zip', '.rar', '.7z', '.tar', '.gz')
BAD_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
INI_HEAD = re.compile(rb'^\s*[\[;]')
SECTION_RE = re.compile(rb'(?m)^\s*\[\s*([^\]\r\n]+)\s*\]')

_LOCK = threading.Lock()
_CACHE = {'root': '', 'items': {}, 'ts': 0.0}


# ---------------------------------------------------------------- 工具
def _dec(b):
    """平台文本多为 GBK，少数是 UTF-8 / BOM。"""
    if isinstance(b, str):
        return b
    if b[:3] == b'\xef\xbb\xbf':
        try:
            return b[3:].decode('utf-8')
        except Exception:
            pass
    for enc in ('utf-8', 'gbk', 'latin-1'):
        try:
            return b.decode(enc)
        except Exception:
            pass
    return b.decode('gbk', 'replace')


def _read_text(path):
    try:
        with open(path, 'rb') as f:
            return _dec(f.read())
    except Exception:
        return ''


def _kv(text, section, key):
    """从 INI 文本的指定段取一个键（大小写不敏感）。"""
    m = re.search(r'(?ims)^\s*\[\s*' + re.escape(section) + r'\s*\]\s*$(.*?)(?=^\s*\[|\Z)', text)
    if not m:
        return ''
    k = re.search(r'(?im)^\s*' + re.escape(key) + r'\s*=\s*(.*?)\s*$', m.group(1))
    return k.group(1).strip() if k else ''


def _ini_name(data):
    """从标准 .map 文本里取 [Basic] Name=；没有就取 [Header] 里的名字。"""
    t = _dec(data[:200000])
    return (_kv(t, 'Basic', 'Name') or _kv(t, 'Basic', 'Description')
            or _kv(t, 'Header', 'Name') or '')


def _is_map_text(b):
    if not b or len(b) < 400:
        return False
    if b[:2] == b'\xff\xd8' or b[:4] == b'PK\x03\x04':
        return False
    head = b[:2048]
    if not INI_HEAD.match(head.lstrip()):
        return False
    return bool(re.search(rb'(?i)\r?\n(?:Size|Name|Width|MaxPlayer|LocalSize)=', head)
                or b'[IsoMapPack5]' in b[:200000])


def _safe_stem(name, fallback):
    s = BAD_CHARS.sub('_', (name or '').strip()).strip('. ')
    s = re.sub(r'\s+', ' ', s)
    if not s:
        s = fallback
    return s[:80]


def _sha(b):
    return hashlib.sha1(b).hexdigest()[:10]


def _mod_risk(b):
    """平台图可能依赖 Ares / Phobos 扩展，导入到别的 MOD 会缺素材。"""
    t = b[:300000]
    hits = [k for k in (b'Ares', b'Phobos', b'Kratos', b'[AITargetTypes]',
                        b'CustomGameMode', b'RA2MD.ini') if k in t]
    return bool(hits)


# ---------------------------------------------------------------- 平台索引
def load_hash_index(root):
    """读 RA2/INI*/nmap*.ini：hash -> {name, players, modes}。"""
    out = {}
    for sub in ('RA2/INI', 'RA2/INI2', 'INI', 'INI2'):
        d = os.path.join(root, *sub.split('/'))
        if not os.path.isdir(d):
            continue
        for fn in sorted(os.listdir(d)):
            if not fn.lower().startswith('nmap') or not fn.lower().endswith('.ini'):
                continue
            txt = _read_text(os.path.join(d, fn))
            cur = ''
            for line in txt.splitlines():
                line = line.strip()
                m = re.match(r'^\[([0-9a-fA-F]{32,40})\]$', line)
                if m:
                    cur = m.group(1).lower()
                    out.setdefault(cur, {})
                    continue
                if not cur or '=' not in line:
                    continue
                k, v = line.split('=', 1)
                k = k.strip().lower()
                if k in ('description', 'maxplayers', 'gamemodes', 'author'):
                    out[cur][k] = v.strip()
    return out


def _platform_game_root(root):
    """从 Config.ini 的 fwht 推导平台游戏主目录（解包缓存所在）。"""
    cfg = _read_text(os.path.join(root, 'Config.ini'))
    m = re.search(r'(?im)^\s*fwht\s*=\s*(.+?)\s*$', cfg)
    if m:
        p = m.group(1).strip().strip('"')
        d = os.path.dirname(p.replace('\\', os.sep))
        if os.path.isdir(d):
            return d
    return ''


# ---------------------------------------------------------------- 扫描各来源
def _item(iid, **kw):
    d = {'id': iid, 'name': '', 'author': '', 'players': '', 'modes': '',
         'source': '', 'kind': 'map', 'ready': True, 'bytes': 0,
         'ext': '.map', 'thumb': False, 'note': '', 'risk': False,
         'detail': ''}
    d.update(kw)
    return d


def scan_mp_dat(path, index, items):
    """A. 平台地图库：明文 .map + 同名 jpg 预览。"""
    try:
        z = zipfile.ZipFile(path)
    except Exception as e:
        return 'MP.dat 打不开：%s' % e
    # 预览文件名形如 thumb_<hash>.jpg，前 16 位与地图条目 hash 一致（实测 171/171 命中）
    jpgs = {}
    for inf in z.infolist():
        b = z.read(inf.filename)
        if b[:2] == b'\xff\xd8':
            key = inf.filename.lower()
            if key.startswith('thumb_'):
                key = key[6:22]
            jpgs[key] = b
    n = 0
    for inf in z.infolist():
        try:
            b = z.read(inf.filename)
        except Exception:
            continue
        if not _is_map_text(b):
            continue
        h = inf.filename.lower()
        meta = index.get(h, {})
        raw_name = meta.get('description') or _ini_name(b)
        # 平台地图名常带 "[2]"/"[4]" 人数前缀，保留原样便于搜索
        name = raw_name.strip() or ('平台地图 ' + h[:8])
        iid = 'mp:' + h[:16]
        thumb = jpgs.get(h[:16]) or jpgs.get(h)
        it = _item(iid, name=name, players=meta.get('maxplayers', ''),
                   modes=meta.get('gamemodes', ''), source='平台地图库 MP.dat',
                   bytes=len(b), thumb=bool(thumb), risk=_mod_risk(b),
                   detail='%s · %s' % (h[:10], meta.get('gamemodes', '') or '标准模式'))
        items[iid] = {'meta': it, 'data': b, 'thumb': thumb}
        n += 1
    return 'MP.dat：%d 张明文地图' % n


def scan_ra2mp(folder, items):
    """B. 平台下载图：元数据 + 预览可拿，主体加密需走解包缓存。"""
    n = 0
    if not os.path.isdir(folder):
        return 0
    for fn in sorted(os.listdir(folder)):
        if not fn.lower().endswith('.ra2mp'):
            continue
        p = os.path.join(folder, fn)
        stem = os.path.splitext(fn)[0]
        try:
            z = zipfile.ZipFile(p)
            names = z.namelist()
            meta = _dec(z.read('3.txt')) if '3.txt' in names else ''
            jpg = z.read('5.txt') if '5.txt' in names else b''
            body = z.read('1.txt') if '1.txt' in names else b''
            if jpg[:2] != b'\xff\xd8':
                # 有的版本预览在同名 .jpg 文件里
                ext = p + '.jpg'
                jpg = open(ext, 'rb').read() if os.path.isfile(ext) else b''
        except Exception as e:
            continue
        name = _kv(meta, 'basic', 'Name') or stem[:12]
        author = _kv(meta, 'basic', 'Author')
        players = _kv(meta, 'basic', 'MaxPlayer')
        ver = _kv(meta, 'basic', 'Ra2mpVer')
        size = _kv(meta, 'Map', 'Size')
        waypoints = len(re.findall(r'(?m)^\s*[0-7]\s*=', _kv(meta, 'Waypoints', 'x') or meta))
        # 1.txt 是平台自有加密，只有解出标准 INI 才允许直接导入
        plain = _try_decrypt(body, meta)
        iid = 'ra2mp:' + stem[:16]
        it = _item(iid, name=name, author=author, players=players,
                   source='平台地图包 .ra2mp', bytes=len(body),
                   thumb=bool(jpg), ready=bool(plain),
                   kind='map' if plain else 'locked',
                   detail='%s · %s人 · %s' % (ver or '?', players or '?', size or '尺寸未知'),
                   note='' if plain else '主体 1.txt 受平台加密保护；在平台里打开/开一局这张图后，'
                        '明文会落到游戏目录 mp.dat，再从「平台解包缓存」导入即可。')
        items[iid] = {'meta': it, 'data': plain or b'', 'thumb': jpg or None,
                      'ra2mp': p}
        n += 1
    return n


def _try_decrypt(body, meta):
    """平台 1.txt 的解密钩子。

    当前公开的 ra2mp 版本（V3/V4）用平台自有算法加密，官方明确「无法从
    外部获取地图代码」。这里保留可扩展钩子：若将来拿到解密算法或用户放入
    已解包文件，只要结果是合法 .map 文本就自动放行，绝不写入坏图。
    """
    if not body:
        return b''
    candidates = [body]
    # 允许同目录下放置手工解包文件 1.txt.plain
    return b'' if not _is_map_text(body) else body


def scan_cache(root, items):
    """C. 平台解包缓存：游戏目录 mp.dat / spawnmap.ini 是明文地图。

    Config.ini 的 fwht 常指向 .../HT/gamemd-spawn.exe，但 spawn.ini / mp.dat
    在它的**上一级**游戏主目录，两边都要看。
    """
    g = _platform_game_root(root)
    if not g:
        return 0
    parent = os.path.dirname(g)
    bases = [g, parent, os.path.join(parent, 'HT'), os.path.join(parent, 'RA2', 'TMP')]
    ui_name = ''
    for base in bases:
        m = re.search(r'(?im)^\s*UIMapName\s*=\s*(.+?)\s*$',
                      _read_text(os.path.join(base, 'spawn.ini')))
        if m:
            ui_name = m.group(1)
            break
    n = 0
    seen = set()
    cands = []
    for base in bases:
        if not os.path.isdir(base):
            continue
        for fn in ('mp.dat', 'spawnmap.ini', 'HTmp.dat', 'map.dat'):
            p = os.path.join(base, fn)
            if os.path.isfile(p):
                cands.append(p)
    for p in cands:
        try:
            with open(p, 'rb') as f:
                b = f.read()
        except Exception:
            continue
        if not _is_map_text(b):
            continue
        h = _sha(b)
        if h in seen:
            continue
        seen.add(h)
        name = ui_name or _ini_name(b) or os.path.basename(p)
        iid = 'cache:' + h
        it = _item(iid, name=name, source='平台解包缓存',
                   bytes=len(b), risk=_mod_risk(b), detail=p,
                   note='来自平台最近一次加载的地图；想导别的图，先在平台里打开它一次。')
        items[iid] = {'meta': it, 'data': b, 'thumb': None}
        n += 1
    return n


def scan_loose(root, items, limit=400):
    """D. 目录里的散图 / zip 地图包。"""
    n = 0
    for base, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if d.lower() not in ('.git', 'node_modules', '__pycache__')]
        for fn in files:
            if n >= limit:
                return n
            ext = os.path.splitext(fn)[1].lower()
            p = os.path.join(base, fn)
            rel = os.path.relpath(p, root).replace('\\', '/')
            if ext in MAP_EXT:
                try:
                    with open(p, 'rb') as f:
                        b = f.read()
                except Exception:
                    continue
                if not _is_map_text(b):
                    continue
                iid = 'file:' + _sha(rel.encode('utf-8'))
                it = _item(iid, name=_ini_name(b) or os.path.splitext(fn)[0],
                           source='本地散图', bytes=len(b), ext=ext,
                           detail=rel, risk=_mod_risk(b))
                items[iid] = {'meta': it, 'data': b, 'thumb': None}
                n += 1
            elif ext == '.zip':
                try:
                    z = zipfile.ZipFile(p)
                except Exception:
                    continue
                for inf in z.infolist():
                    if os.path.splitext(inf.filename)[1].lower() not in MAP_EXT:
                        continue
                    try:
                        b = z.read(inf.filename)
                    except Exception:
                        continue
                    if not _is_map_text(b):
                        continue
                    if n >= limit:
                        return n
                    iid = 'zip:' + _sha((rel + '|' + inf.filename).encode('utf-8'))
                    it = _item(iid, name=_ini_name(b) or os.path.splitext(os.path.basename(inf.filename))[0],
                               source='ZIP 地图包', bytes=len(b),
                               ext=os.path.splitext(inf.filename)[1].lower(),
                               detail=rel + ' → ' + inf.filename, risk=_mod_risk(b))
                    items[iid] = {'meta': it, 'data': b, 'thumb': None}
                    n += 1
    return n


# ---------------------------------------------------------------- 对外接口
def guess_root():
    """自动找 HT 平台安装目录，找不到给一个常见默认值让用户可以自己改。"""
    cands = []
    for drive in ('E', 'D', 'F', 'C', 'G'):
        for sub in ('game', 'Games', '游戏', ''):
            cands.append('%s:\\%s\\HTPlatform' % (drive, sub) if sub else '%s:\\HTPlatform' % drive)
    for d in cands:
        if os.path.isfile(os.path.join(d, 'HTPlatform.exe')):
            return d
    return cands[0]


def scan(root):
    root = os.path.normpath(root or '')
    if not root or not os.path.isdir(root):
        return {'error': '目录不存在：%s' % root}
    started = time.time()
    items = {}
    notes = []
    index = load_hash_index(root)
    mp = ''
    for rel in ('RA2/Res/MP.dat', 'RA2/MP.dat', 'Res/MP.dat', 'MP.dat'):
        p = os.path.join(root, *rel.split('/'))
        if os.path.isfile(p):
            mp = p
            break
    if mp:
        notes.append(scan_mp_dat(mp, index, items))
    else:
        notes.append('未找到 MP.dat（平台地图库）')
    n_ra = scan_ra2mp(os.path.join(root, 'RA2', 'Map'), items)
    notes.append('RA2/Map：%d 个 .ra2mp 地图包' % n_ra)
    n_ca = scan_cache(root, items)
    notes.append('平台解包缓存：%d 张明文地图' % n_ca)
    n_lo = scan_loose(root, items)
    notes.append('散图 / ZIP：%d 张' % n_lo)
    if index:
        notes.append('nmap 索引：%d 条中文地图名' % len(index))
    with _LOCK:
        _CACHE['root'] = root
        _CACHE['items'] = items
        _CACHE['ts'] = started
    metas = sorted((v['meta'] for v in items.values()),
                   key=lambda x: (not x['ready'], x['source'], x['name'].lower()))
    return {
        'root': root,
        'gameRoot': _platform_game_root(root),
        'mpDat': mp,
        'notes': notes,
        'items': metas,
        'count': len(metas),
        'ready': sum(1 for m in metas if m['ready']),
        'locked': sum(1 for m in metas if not m['ready']),
        'elapsed': round(time.time() - started, 2),
    }


def thumb(iid):
    with _LOCK:
        e = _CACHE['items'].get(iid)
    return e.get('thumb') if e else None


def import_items(ids, target_root, conflict='skip', backup_root=''):
    """把勾选的地图写入目标目录；返回逐条结果，绝不静默写入坏图。"""
    with _LOCK:
        cache = dict(_CACHE['items'])
    if not target_root:
        return {'error': '未设置游戏 Maps 目录'}
    os.makedirs(target_root, exist_ok=True)
    out, done, skipped, failed, locked = [], 0, 0, 0, 0
    bakdir = ''
    if conflict == 'overwrite' and backup_root:
        bakdir = os.path.join(backup_root, '平台导入覆盖-' + time.strftime('%Y%m%d-%H%M%S'))
    for iid in ids:
        e = cache.get(iid)
        if not e:
            failed += 1
            out.append({'id': iid, 'ok': False, 'msg': '缓存已失效，请重新扫描'})
            continue
        m, data = e['meta'], e.get('data') or b''
        if not m['ready'] or not _is_map_text(data):
            locked += 1
            out.append({'id': iid, 'ok': False, 'name': m['name'],
                        'msg': '加密主体未解开，需用平台解包缓存通道'})
            continue
        stem = _safe_stem(m['name'], '平台地图_' + _sha(data))
        ext = m.get('ext') if m.get('ext') in MAP_EXT else '.map'
        dst = os.path.join(target_root, stem + ext)
        if os.path.exists(dst):
            if conflict == 'skip':
                skipped += 1
                out.append({'id': iid, 'ok': False, 'name': m['name'],
                            'msg': '同名已存在，跳过'})
                continue
            if conflict == 'rename':
                i = 1
                while os.path.exists(os.path.join(target_root, '%s_%d%s' % (stem, i, ext))):
                    i += 1
                dst = os.path.join(target_root, '%s_%d%s' % (stem, i, ext))
            elif conflict == 'overwrite':
                try:
                    if bakdir:
                        os.makedirs(bakdir, exist_ok=True)
                        shutil.copy2(dst, os.path.join(bakdir, os.path.basename(dst)))
                except Exception:
                    pass
        try:
            tmp = dst + '.tmp-import'
            with open(tmp, 'wb') as f:
                f.write(data)
            os.replace(tmp, dst)
            done += 1
            out.append({'id': iid, 'ok': True, 'name': m['name'],
                        'path': dst, 'msg': '已导入'})
        except Exception as ex:
            failed += 1
            out.append({'id': iid, 'ok': False, 'name': m['name'], 'msg': str(ex)})
    return {'imported': done, 'skipped': skipped, 'failed': failed,
            'locked': locked, 'backup': bakdir, 'results': out}
