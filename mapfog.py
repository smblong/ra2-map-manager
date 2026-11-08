# -*- coding: utf-8 -*-
"""地图「取消迷雾 / 全图可见」。

机制（实证，不是猜的）：
  * 玩家社区标准做法（本机 XKD2022.mpr 就是现成模板）：
      [Triggers] <ID>=Americans,<none>,RA2MAP_RevealAll,0,1,1,1,0
      [Events]   <ID>=1,13,0,<delay>        # 13 = 流逝时间
      [Actions]  <ID>=1,16,0,0,0,0,0,0,A   # 16 = Reveal all map
      [Tags]     <TAG>=0,RA2MAP_RevealAll 1,<ID>
    Action 16 出自 ModEnc《Actions (maps)/RA2YR/000 049》，官方说明
    "Reveals the entire map to the player. May cause reconnection error in multiplayer!"。
  * [Basic] FreeRadar=yes：不用造雷达就有小地图（FA2 StdMapRA2.ini 标准字段）。
  * [SpecialFlags] FogOfWar=no：战争迷雾（半透明灰雾），本机 629 张图默认都是 no。

读写复用 ini_edit.MapIni（已在 629 张真实地图上验证）：按 \\n 保序切行，
兼容本机地图里普遍存在的 UTF-16 BOM、CRLF/LF/CR 混合行尾、GBK 段名，
只改文本段，二进制段（IsoMapPack5 / OverlayPack / PreviewPack）原样保留。
"""
import json
import os
import re
import shutil
import time

import ini_edit as INI

TRIG_NAME = 'RA2MAP_RevealAll'
TAG_SUFFIX = ' 1'
ID_RE = re.compile(r'^[0-9A-Fa-f]{8}$')


# ---------------------------------------------------------------- INI 薄封装
class RawIni(object):
    """给 MapIni 补一层「按段名找键 / 追加条目 / 删键」的简洁接口。"""

    def __init__(self, path):
        self.path = path
        self.m = INI.MapIni(path)
        self._sync()

    def _sync(self):
        self.lines = self.m.lines
        self.secs = [(s['start'], s['name'].lower(), s['end'])
                     for s in self.m.sections]

    def has(self, name):
        return self.m.find(name) is not None

    def keys(self, name):
        sec = self.m.find(name)
        if not sec:
            return []
        return [(kv['line'], kv['k'].strip().lower(), kv['v'])
                for kv in self.m.keys_of(sec)]

    def get(self, name, key, default=None):
        for _, k, v in self.keys(name):
            if k == key.lower():
                return v
        return default

    def set(self, name, key, value):
        """存在则改、不存在则加；段不存在则建段并写第一行。"""
        k, v = str(key), str(value)
        if not self.has(name):
            self.m.apply([{'op': 'addsec', 'section': name, 'keys': [[k, v]]}])
            self._sync()
            return 'addsec'
        self.m.apply([{'op': 'set', 'section': name, 'key': k, 'value': v}])
        self._sync()
        return 'set'

    def append_section(self, name, kv_lines):
        pairs = [x.split('=', 1) for x in kv_lines if '=' in x]
        self.m.apply([{'op': 'addsec', 'section': name, 'keys': pairs}])
        self._sync()

    def add_line(self, name, text):
        """向已存在（或新建）的段追加一整行（用于 8 位 hex ID 条目）。"""
        k, v = text.split('=', 1)
        if not self.has(name):
            self.m.apply([{'op': 'addsec', 'section': name, 'keys': [[k, v]]}])
        else:
            self.m.apply([{'op': 'add', 'section': name, 'key': k, 'value': v}])
        self._sync()

    def drop_key(self, name, key):
        sec = self.m.find(name)
        if not sec:
            return 0
        n = sum(1 for kv in self.m.keys_of(sec)
                if kv['k'].strip().lower() == str(key).lower())
        if n:
            self.m.apply([{'op': 'del', 'section': name, 'key': str(key)}])
            self._sync()
        return n

    def dump(self):
        return self.m.changed_bytes()


def _backup(path, backup_dir):
    os.makedirs(backup_dir, exist_ok=True)
    stem, ext = os.path.splitext(os.path.basename(path))
    stamp = time.strftime('%Y%m%d_%H%M%S')
    dst = os.path.join(backup_dir, '%s_迷雾_%s%s' % (stem, stamp, ext))
    n = 1
    while os.path.exists(dst):
        dst = os.path.join(backup_dir, '%s_迷雾_%s_%d%s' % (stem, stamp, n, ext))
        n += 1
    shutil.copy2(path, dst)
    return dst


def _atomic_write(path, data):
    tmp = path + '.tmp-fog'
    with open(tmp, 'wb') as f:
        f.write(data)
    os.replace(tmp, path)


def _state_path(data_dir):
    return os.path.join(data_dir, '迷雾状态.json')


def _load_state(data_dir):
    p = _state_path(data_dir)
    if not os.path.isfile(p):
        return {}
    try:
        with open(p, 'r', encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        return {}


def _save_state(data_dir, st):
    os.makedirs(data_dir, exist_ok=True)
    p = _state_path(data_dir)
    tmp = p + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(st, f, ensure_ascii=False, indent=1)
    os.replace(tmp, p)


# ---------------------------------------------------------------- 检测 / ID
def _used_ids(doc):
    used = set()
    for st, sn, en in doc.secs:
        for i in range(st + 1, min(en, len(doc.lines))):
            ln = doc.lines[i]
            if b'=' not in ln:
                continue
            k = ln.split(b'=', 1)[0].decode('latin-1').strip()
            if ID_RE.match(k):
                used.add(k.upper())
    return used


def _new_id(doc, used):
    """生成不冲突的 8 位 hex ID，前缀 A7 便于识别/回退。"""
    for n in range(0xA7000000, 0xA8000000):
        cand = '%08X' % n
        if cand not in used:
            used.add(cand)
            return cand
    raise ValueError('找不到可用的触发器 ID')


def _find_own(doc):
    """找本程序注入的 trigger / tag，返回 (triggerId, tagIds)。"""
    trig = None
    for _, k, v in doc.keys('Triggers'):
        if TRIG_NAME in v:
            trig = k.upper()
            break
    tags = []
    for _, k, v in doc.keys('Tags'):
        if TRIG_NAME in v or (trig and trig.lower() in v.lower()):
            tags.append(k.upper())
    return trig, tags


def state(path):
    """只读：返回当前地图的迷雾相关状态。"""
    if not os.path.isfile(path):
        return {'ok': False, 'error': '文件不存在'}
    try:
        d = RawIni(path)
        trig, tags = _find_own(d)
        return {
            'ok': True,
            'file': os.path.basename(path),
            'revealed': bool(trig),
            'triggerId': trig or '',
            'tagIds': tags,
            'freeRadar': d.get('Basic', 'FreeRadar', ''),
            'fogOfWar': d.get('SpecialFlags', 'FogOfWar', ''),
            'hasTriggers': d.has('Triggers'),
            'triggerCount': len(d.keys('Triggers')),
        }
    except Exception as e:
        return {'ok': False, 'error': str(e)}


# ---------------------------------------------------------------- 开启 / 恢复
def apply(path, opts, backup_root, data_dir, rel_key=''):
    """给地图加「取消迷雾」。opts: reveal / freeRadar / fogOfWar / delay / force。"""
    opts = opts or {}
    do_reveal = bool(opts.get('reveal', True))
    do_radar = bool(opts.get('freeRadar', True))
    do_fog = bool(opts.get('fogOfWar', True))
    try:
        delay = max(0, min(int(opts.get('delay', 0) or 0), 100000))
    except Exception:
        delay = 0
    force = bool(opts.get('force', False))

    if not os.path.isfile(path):
        return {'ok': False, 'error': '文件不存在'}
    d = RawIni(path)
    before = state(path)
    if not before.get('ok'):
        return before
    need_radar = do_radar and str(before['freeRadar']).strip().lower() != 'yes'
    need_fog = do_fog and str(before['fogOfWar']).strip().lower() != 'no'
    need_reveal = do_reveal and not before['revealed']
    if not (need_radar or need_fog or need_reveal) and not force:
        return {'ok': True, 'changed': False, 'skipped': '已经是全图可见状态',
                'state': before}

    st = _load_state(data_dir)
    key = rel_key or os.path.basename(path)
    rec = st.get(key, {})
    old_radar = rec.get('oldFreeRadar', before['freeRadar'])

    changes = []
    bak = _backup(path, backup_root)
    try:
        if need_radar:
            d.set('Basic', 'FreeRadar', 'yes')
            changes.append('[Basic] FreeRadar=yes')
        if need_fog:
            d.set('SpecialFlags', 'FogOfWar', 'no')
            changes.append('[SpecialFlags] FogOfWar=no')

        if need_reveal:
            used = _used_ids(d)
            tid = _new_id(d, used)
            tagid = _new_id(d, used)
            d.add_line('Triggers', '%s=Americans,<none>,%s,0,1,1,1,0' % (tid, TRIG_NAME))
            d.add_line('Events', '%s=1,13,0,%d' % (tid, delay))
            d.add_line('Actions', '%s=1,16,0,0,0,0,0,0,A' % tid)
            d.add_line('Tags', '%s=0,%s%s,%s' % (tagid, TRIG_NAME, TAG_SUFFIX, tid))
            changes.append('注入触发器 %s（Action 16 揭示全图）' % tid)
            rec.update({'triggerId': tid, 'tagId': tagid,
                        'oldFreeRadar': before['freeRadar'],
                        'at': time.strftime('%Y-%m-%d %H:%M:%S')})
            st[key] = rec
            _save_state(data_dir, st)

        _atomic_write(path, d.dump())
        return {
            'ok': True, 'changed': True, 'changes': changes,
            'backup': bak, 'triggerId': rec.get('triggerId', ''),
            'delay': delay, 'state': state(path),
        }
    except Exception as e:
        try:
            shutil.copy2(bak, path)   # 回滚
        except Exception:
            pass
        return {'ok': False, 'error': str(e), 'backup': bak}


def remove(path, backup_root, data_dir, rel_key=''):
    """恢复迷雾：移除本程序注入的 trigger/event/action/tag，并还原 FreeRadar。"""
    if not os.path.isfile(path):
        return {'ok': False, 'error': '文件不存在'}
    d = RawIni(path)
    before = state(path)
    if not before.get('ok'):
        return before
    key = rel_key or os.path.basename(path)
    st = _load_state(data_dir)
    rec = st.get(key, {})

    tid = before['triggerId'] or rec.get('triggerId', '')
    tags = list(before['tagIds'] or [])
    if rec.get('tagId') and rec['tagId'] not in tags:
        tags.append(rec['tagId'])
    if not tid and not tags:
        return {'ok': True, 'changed': False, 'skipped': '没有本程序注入的全图揭示'}

    bak = _backup(path, backup_root)
    try:
        n = 0
        if tid:
            for sec in ('Triggers', 'Events', 'Actions'):
                n += d.drop_key(sec, tid)
        for t in tags:
            n += d.drop_key('Tags', t)
        old = rec.get('oldFreeRadar', '')
        d.set('Basic', 'FreeRadar', old or 'no')
        _atomic_write(path, d.dump())
        st.pop(key, None)
        _save_state(data_dir, st)
        return {'ok': True, 'changed': True, 'removedLines': n,
                'freeRadar': old or 'no', 'backup': bak, 'state': state(path)}
    except Exception as e:
        try:
            shutil.copy2(bak, path)
        except Exception:
            pass
        return {'ok': False, 'error': str(e), 'backup': bak}
