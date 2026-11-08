# -*- coding: utf-8 -*-
"""AI 做图 / 改图助手。

设计原则：
  1. 只走 OpenAI 兼容的 /chat/completions，标准库 urllib，零新增依赖；
  2. API Key 只存本机 数据/ai_config.json，绝不写日志、绝不回传给前端明文；
  3. **先出方案、用户点「应用」才落盘**；AI 只返回结构化 JSON 操作计划；
  4. 不上传地图文件原文，只发结构化摘要（尺寸/主题/统计/可编辑字段）；
  5. 计划经过白名单 + 范围校验，禁止改二进制段 / Digest / 触发器段，防止崩图。
"""
import json
import os
import re
import shutil
import time
import traceback
import urllib.error
import urllib.request

import ini_edit as INI
import mapedit
import mapfog

CONFIG_NAME = 'ai_config.json'

PRESETS = [
    {'id': 'deepseek', 'name': 'DeepSeek',
     'baseUrl': 'https://api.deepseek.com/v1', 'model': 'deepseek-chat',
     'hint': 'api.deepseek.com，性价比高，中文好'},
    {'id': 'hunyuan', 'name': '腾讯混元',
     'baseUrl': 'https://api.hunyuan.cloud.tencent.com/v1', 'model': 'hunyuan-turbo',
     'hint': '腾讯云混元，OpenAI 兼容端点'},
    {'id': 'openai', 'name': 'OpenAI',
     'baseUrl': 'https://api.openai.com/v1', 'model': 'gpt-4o-mini',
     'hint': '需要可访问 OpenAI 的网络'},
    {'id': 'custom', 'name': '自定义 / 本地模型',
     'baseUrl': '', 'model': '',
     'hint': '任何 OpenAI 兼容服务（含 Ollama / vLLM / 中转站）'},
]

# 只允许 AI 改这些文本段，避免破坏地图二进制段与触发器逻辑
EDITABLE_SECTIONS = {
    'basic', 'specialflags', 'lighting', 'waypoints', 'ranking',
    'map', 'multiplayerdialogsettings',
}
BLOCKED_KEYS = {'digest', 'isomappack5', 'overlaypack', 'overlaydatapack',
                'previewpack', 'preview', 'mappack'}
SAFE_NAME_RE = re.compile(r'^[A-Za-z0-9_ .\-]{1,64}$')
MAX_OPS = 60
MAX_CELLS = 4000
MAX_VALUE_LEN = 200

SYSTEM_PROMPT = '''你是《红色警戒2 / 尤里的复仇》地图编辑助手，工作在「红警地图管理器」里。
用户会用中文描述想怎么改地图。你只能输出一个 JSON 对象，不要 Markdown 代码围栏，不要多余文字。

JSON 格式：
{
  "explain": "用中文简短说明你打算做什么、为什么",
  "ops": [
    {"type":"paint","kind":"ore","cells":[[x,y],...],"val":9},
    {"type":"paint","kind":"gem","cells":[[x,y],...],"val":9},
    {"type":"paint","kind":"erase","cells":[[x,y],...]},
    {"type":"height","cells":[[x,y],...],"level":0},
    {"type":"waypoint","index":0,"cell":[x,y]},
    {"type":"set","section":"Basic","key":"MaxPlayer","value":"8"},
    {"type":"add","section":"Basic","key":"Author","value":"某某"},
    {"type":"del","section":"Basic","key":"..."},
    {"type":"fog","action":"on"}
  ]
}

规则：
* 坐标系是 IsoMapPack5 的菱形屏幕坐标 (Dx,Dy)，地图尺寸为 W 时 Dx、Dy 都在 1..2W-1，
  且必须落在菱形内：第 y 行的有效 x 范围是 x0..x0+L-1，
  L = (y<=W) ? 2y-1 : 2*(2W-y)，x0 = W+1 - floor((L+1)/2)。
  **不确定坐标时，优先用地图中心附近的格子**，不要给界外坐标。
* 刷矿/宝石：kind 只能是 ore/gem/erase/other；val 是密度帧 0..11，常用 9；
  other 需要额外给 id (0..255)。cells 最多 %d 格，尽量给成片的连续坐标。
* height：level 0..14，0 是压平。
* waypoint：index 0..7，0 通常不用；cell 是 [x,y]。
* set/add/del：section 只能是 Basic / SpecialFlags / Lighting / Waypoints /
  Ranking / Map / MultiplayerDialogSettings 之一；key 为英文字母数字；
  值不能含换行，长度不超过 %d。不要改 Digest、任何 *Pack* 段、Triggers/Events/Actions/Tags。
* fog：action 只能是 on（取消迷雾/全图可见）或 off（恢复）。
* 不确定就少做：宁可只返回少量安全操作，也不要编造坐标或字段。
* 如果用户只是问「某个参数怎么用 / 该怎么调」，把 ops 留空，只写 explain，
  用中文把含义、取值范围、推荐值讲清楚。
* 最多 %d 个 op。所有解释都用简体中文。
''' % (MAX_CELLS, MAX_VALUE_LEN, MAX_OPS)


# ---------------------------------------------------------------- 配置
def config_path(data_dir):
    return os.path.join(data_dir, CONFIG_NAME)


CONFIG_VERSION = 2


def _conn_id():
    return 'c_' + os.urandom(4).hex()


def _norm_conn(raw, i):
    """规整一条连接配置；key 为空时由 save_config 负责回填旧值。"""
    raw = raw if isinstance(raw, dict) else {}
    models = [str(x).strip() for x in (raw.get('models') or []) if str(x).strip()]
    enabled = [str(x).strip() for x in (raw.get('enabledModels') or []) if str(x).strip()]
    enabled = [x for x in enabled if not models or x in models] or (models[:1] if not enabled else enabled)
    models = list(dict.fromkeys(models + enabled))
    try:
        timeout = max(10, min(int(raw.get('timeout') or 60), 180))
    except Exception:
        timeout = 60
    return {
        'id': str(raw.get('id') or _conn_id()),
        'name': str(raw.get('name') or ('连接 %d' % (i + 1))).strip()[:40],
        'preset': str(raw.get('preset') or 'custom'),
        'baseUrl': str(raw.get('baseUrl') or '').strip().rstrip('/'),
        'key': str(raw.get('key') or '').strip(),
        'models': models[:60],
        'enabledModels': enabled[:60],
        'timeout': timeout,
        'enabled': bool(raw.get('enabled', True)),
    }


def load_config(data_dir):
    """v2.4 多连接配置；自动把 v1.7~v2.3 的单连接配置迁移过来。"""
    p = config_path(data_dir)
    raw = {}
    if os.path.isfile(p):
        try:
            with open(p, 'r', encoding='utf-8') as f:
                raw = json.load(f) or {}
        except Exception:
            pass
    conns = raw.get('connections')
    if not isinstance(conns, list) or not conns:
        model = str(raw.get('model') or PRESETS[0]['model']).strip()
        conns = [{
            'id': 'c_default', 'name': '默认连接',
            'preset': raw.get('preset') or 'deepseek',
            'baseUrl': raw.get('baseUrl') or PRESETS[0]['baseUrl'],
            'key': raw.get('key') or '',
            'models': [model] if model else [],
            'enabledModels': [model] if model else [],
            'timeout': raw.get('timeout') or 60,
            'enabled': True,
        }]
    cfg = {
        'version': CONFIG_VERSION,
        'connections': [_norm_conn(c, i) for i, c in enumerate(conns)],
        'active': raw.get('active') if isinstance(raw.get('active'), dict) else {},
        'fallback': bool(raw.get('fallback', True)),
        'temperature': raw.get('temperature', 0.3),
    }
    try:
        cfg['temperature'] = max(0.0, min(float(cfg['temperature']), 1.0))
    except Exception:
        cfg['temperature'] = 0.3
    if not cfg['connections']:
        cfg['connections'] = [_norm_conn({}, 0)]
    cid = cfg['active'].get('connId') or cfg['connections'][0]['id']
    if not any(c['id'] == cid for c in cfg['connections']):
        cid = cfg['connections'][0]['id']
    model = cfg['active'].get('model') or ''
    if not model:
        c = next((x for x in cfg['connections'] if x['id'] == cid), cfg['connections'][0])
        model = (c['enabledModels'] or c['models'] or [''])[0]
    cfg['active'] = {'connId': cid, 'model': model}
    # 兼容 v1.7~v2.3 的调用点
    first = cfg['connections'][0]
    cfg.update({'preset': first['preset'], 'baseUrl': first['baseUrl'],
                'model': model, 'key': first['key'], 'timeout': first['timeout']})
    return cfg


def save_config(data_dir, data):
    data = data if isinstance(data, dict) else {}
    old = load_config(data_dir)
    old_by_id = {c['id']: c for c in old['connections']}
    cfg = dict(old)
    if isinstance(data.get('connections'), list):
        conns = [_norm_conn(c, i) for i, c in enumerate(data['connections'])]
        for c in conns:
            # 前端不回传明文 key：留空 = 沿用已保存的密钥
            if not c['key'] and c['id'] in old_by_id:
                c['key'] = old_by_id[c['id']]['key']
        cfg['connections'] = conns or [_norm_conn({}, 0)]
    elif any(k in data for k in ('baseUrl', 'model', 'preset', 'key')):
        # 兼容老版前端的单连接写法
        c = dict(cfg['connections'][0])
        for k in ('preset', 'baseUrl', 'model'):
            if k in data:
                c[k] = str(data[k]).strip()
        if data.get('key'):
            c['key'] = str(data['key']).strip()
        if c.get('model'):
            c['models'] = list(dict.fromkeys(c['models'] + [c['model']]))
            c['enabledModels'] = [c['model']]
        if 'timeout' in data:
            c['timeout'] = data['timeout']
        cfg['connections'] = [_norm_conn(c, 0)] + cfg['connections'][1:]
    if isinstance(data.get('active'), dict):
        cfg['active'] = {'connId': str(data['active'].get('connId') or ''),
                         'model': str(data['active'].get('model') or '')}
    if 'fallback' in data:
        cfg['fallback'] = bool(data['fallback'])
    if 'temperature' in data:
        try:
            cfg['temperature'] = max(0.0, min(float(data['temperature']), 1.0))
        except Exception:
            pass
    c0 = cfg['connections'][0]
    cfg.update({'preset': c0['preset'], 'baseUrl': c0['baseUrl'],
                'model': cfg['active'].get('model') or (c0['enabledModels'] or [''])[0],
                'key': c0['key'], 'timeout': c0['timeout']})
    os.makedirs(data_dir, exist_ok=True)
    p = config_path(data_dir)
    tmp = p + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(cfg, f, ensure_ascii=False, indent=1)
    os.replace(tmp, p)
    return public_config(cfg)


def _mask(k):
    return (k[:4] + '••••' + k[-4:]) if len(k) > 8 else ('••••' if k else '')


def public_config(cfg=None, data_dir=None):
    """永远不把明文 API Key 回传给前端。"""
    if cfg is None:
        cfg = load_config(data_dir)
    conns = []
    for c in cfg['connections']:
        d = dict(c)
        k = d.pop('key', '')
        d['key'] = ''
        d['hasKey'] = bool(k)
        d['keyMasked'] = _mask(k)
        d['ready'] = bool(k and d['baseUrl'] and (d['enabledModels'] or d['models']) and d['enabled'])
        conns.append(d)
    first = conns[0] if conns else {}
    return {
        'version': CONFIG_VERSION,
        'connections': conns,
        'active': cfg.get('active') or {},
        'fallback': bool(cfg.get('fallback', True)),
        'temperature': cfg.get('temperature', 0.3),
        'presets': PRESETS,
        'options': model_options(cfg),
        'ready': any(c['ready'] for c in conns),
        # 兼容旧字段
        'preset': first.get('preset', ''),
        'baseUrl': first.get('baseUrl', ''),
        'model': (cfg.get('active') or {}).get('model') or (first.get('enabledModels') or [''])[0],
        'timeout': first.get('timeout', 60),
        'hasKey': first.get('hasKey', False),
        'keyMasked': first.get('keyMasked', ''),
        'key': '',
    }


# ---------------------------------------------------------------- 地图摘要
def map_digest(path):
    """给 AI 的结构化摘要，绝不发送地图文件原文。"""
    out = {'file': os.path.basename(path)}
    try:
        d = INI.MapIni(path)
        for sec in ('Basic', 'SpecialFlags', 'Lighting', 'Map',
                    'Waypoints', 'MultiplayerDialogSettings'):
            s = d.find(sec)
            if not s:
                continue
            kv = {}
            for x in d.keys_of(s)[:28]:
                k = x['k'].strip()
                if k.lower() in BLOCKED_KEYS or len(x['v']) > MAX_VALUE_LEN:
                    continue
                kv[k] = x['v'][:MAX_VALUE_LEN]
            if kv:
                out[sec] = kv
        out['sections'] = [s['name'] for s in d.sections][:45]
        out['sectionCount'] = len(d.sections)
        try:
            sz = d.get('Map', 'Size', '') if hasattr(d, 'get') else ''
        except Exception:
            sz = ''
        if not sz:
            for s in d.sections:
                if s['name'].lower() == 'map':
                    for kv in d.keys_of(s):
                        if kv['k'].strip().lower() == 'size':
                            sz = kv['v']
        out['mapSize'] = sz
    except Exception as e:
        out['iniError'] = str(e)
    return out


def _help_hint():
    """把帮助知识库压缩成一段给 AI 的参数速查。"""
    try:
        g = INI.help_guide()
        ch = [c.get('title', '') for c in (g.get('chapters') or [])][:8]
        idx = [(x.get('k'), x.get('label')) for x in (g.get('fieldIndex') or [])][:80]
        return {'chapters': ch,
                'fieldHints': ['%s=%s' % (k, v) for k, v in idx if k and v]}
    except Exception:
        return {}


# ---------------------------------------------------------------- 调用 LLM
# ---------------------------------------------------------------- 调用 LLM（v2.4 多连接 + 自动兜底）
_HTTP_MSG = {400: '请求格式不对', 401: 'API Key 无效或已过期', 402: '余额不足',
             403: '没有权限（检查密钥/套餐）', 404: '接口地址或模型名不对',
             408: '请求超时', 409: '请求冲突', 429: '调用太频繁或额度用完',
             500: '服务端错误', 502: '服务端网关错误', 503: '服务暂不可用',
             504: '服务端超时'}
_RETRY_CODES = {401, 402, 403, 404, 408, 409, 429, 500, 502, 503, 504, 520, 521, 522, 524}


def _endpoint(base, tail):
    base = (base or '').strip().rstrip('/')
    if base.endswith('/' + tail):
        return base
    return base + '/' + tail


def _chat_once(conn, model, messages, temperature=0.3):
    """单次调用一条连接的一个模型；retryable 决定失败后要不要换下一个。"""
    base = (conn.get('baseUrl') or '').strip().rstrip('/')
    key = (conn.get('key') or '').strip()
    if not (base and key and model):
        return {'ok': False, 'error': '这条连接缺 Base URL / API Key / 模型', 'retryable': False}
    if not base.startswith(('http://', 'https://')):
        return {'ok': False, 'error': 'Base URL 必须以 http:// 或 https:// 开头', 'retryable': False}
    body = {'model': model, 'messages': messages,
            'temperature': float(temperature), 'stream': False}
    req = urllib.request.Request(
        _endpoint(base, 'chat/completions'),
        data=json.dumps(body, ensure_ascii=False).encode('utf-8'),
        headers={'Content-Type': 'application/json',
                 'Authorization': 'Bearer ' + key,
                 'User-Agent': 'RA2MapManager/2.4'},
        method='POST')
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=int(conn.get('timeout') or 60)) as r:
            raw = r.read().decode('utf-8', 'replace')
        data = json.loads(raw)
        text = (data.get('choices') or [{}])[0].get('message', {}).get('content', '')
        return {'ok': True, 'text': text or '',
                'usage': data.get('usage') or {},
                'model': data.get('model') or model,
                'elapsed': round(time.time() - t0, 2)}
    except urllib.error.HTTPError as e:
        detail = ''
        try:
            detail = e.read().decode('utf-8', 'replace')[:300]
        except Exception:
            pass
        msg = _HTTP_MSG.get(e.code, 'HTTP %d' % e.code)
        return {'ok': False, 'error': '%s：%s' % (msg, detail[:160]),
                'retryable': e.code in _RETRY_CODES,
                'elapsed': round(time.time() - t0, 2)}
    except Exception as e:
        return {'ok': False, 'error': '连接失败：%s' % e, 'retryable': True,
                'elapsed': round(time.time() - t0, 2)}


def fetch_models(data_dir, payload):
    """填入 API Key 后点「获取模型」：拉 /models，避免手打模型名写错。"""
    payload = payload if isinstance(payload, dict) else {}
    cfg = load_config(data_dir)
    cid = str(payload.get('connId') or '')
    cur = next((c for c in cfg['connections'] if c['id'] == cid), None)
    base = (payload.get('baseUrl') or (cur or {}).get('baseUrl') or '').strip().rstrip('/')
    key = (payload.get('key') or '').strip() or ((cur or {}).get('key') or '')
    if not base.startswith(('http://', 'https://')):
        return {'ok': False, 'error': '先填好 Base URL（一般要带 /v1）'}
    if not key:
        return {'ok': False, 'error': '先填 API Key（或保存一次已配置的密钥）'}
    req = urllib.request.Request(
        _endpoint(base, 'models'),
        headers={'Authorization': 'Bearer ' + key,
                 'User-Agent': 'RA2MapManager/2.4'})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            data = json.loads(r.read().decode('utf-8', 'replace'))
    except urllib.error.HTTPError as e:
        detail = ''
        try:
            detail = e.read().decode('utf-8', 'replace')[:200]
        except Exception:
            pass
        return {'ok': False, 'error': '%s：%s' % (_HTTP_MSG.get(e.code, 'HTTP %d' % e.code), detail[:140])}
    except Exception as e:
        return {'ok': False, 'error': '连接失败：%s' % e}
    raw = data.get('data') if isinstance(data, dict) else data
    if isinstance(data, dict) and not raw:
        raw = data.get('models') or data.get('result') or []
    models = []
    for x in (raw or []):
        if isinstance(x, str):
            models.append(x)
        elif isinstance(x, dict):
            v = x.get('id') or x.get('name') or x.get('model')
            if v:
                models.append(str(v))
    models = sorted(set(models))[:200]
    return {'ok': True, 'models': models, 'count': len(models), 'baseUrl': base}


def model_options(cfg=None, data_dir=None):
    """所有启用连接 × 勾选模型，供前端「当前使用」下拉。"""
    if cfg is None:
        cfg = load_config(data_dir)
    out = []
    for c in cfg['connections']:
        if not c.get('enabled', True) or not c['key'] or not c['baseUrl']:
            continue
        for m in (c['enabledModels'] or c['models']):
            out.append({'connId': c['id'], 'connName': c['name'], 'model': m,
                        'label': '%s · %s' % (c['name'], m)})
    return out


def chat(data_dir, messages, prefer=None, temperature=None):
    """按优先级自动试所有可用模型；某个 Key 余额不足/限流就换下一个，不中断任务。"""
    cfg = load_config(data_dir)
    opts = model_options(cfg)
    if not opts:
        return {'ok': False, 'attempts': [],
                'error': '还没有可用模型：请到「AI 助手配置」添加连接、获取模型并勾选'}
    by_id = {c['id']: c for c in cfg['connections']}
    ordered, seen = [], set()
    p = prefer if isinstance(prefer, dict) else {}
    keys = [(p.get('connId'), p.get('model')),
            (cfg['active'].get('connId'), cfg['active'].get('model'))]
    keys += [(o['connId'], o['model']) for o in opts]
    for cid, mdl in keys:
        k = (cid, mdl)
        if cid and mdl and k not in seen and cid in by_id and by_id[cid]['key']:
            seen.add(k)
            ordered.append(k)
    if not cfg.get('fallback', True):
        ordered = ordered[:1]
    temp = cfg['temperature'] if temperature is None else temperature
    attempts, last = [], ''
    for cid, mdl in ordered[:12]:
        c = by_id[cid]
        r = _chat_once(c, mdl, messages, temp)
        attempts.append({'connId': cid, 'connName': c['name'], 'model': mdl,
                         'ok': bool(r.get('ok')), 'error': (r.get('error') or '')[:140]})
        if r.get('ok'):
            return {'ok': True, 'text': r['text'], 'usage': r.get('usage'),
                    'model': r.get('model') or mdl, 'elapsed': r.get('elapsed'),
                    'used': {'connId': cid, 'connName': c['name'], 'model': mdl},
                    'attempts': attempts, 'switched': len(attempts) > 1}
        last = r.get('error') or '未知错误'
        if not r.get('retryable'):
            break
    return {'ok': False, 'error': '所有可用模型都失败了：' + last,
            'attempts': attempts, 'switched': len(attempts) > 1}


def _extract_json(text):
    t = (text or '').strip()
    t = re.sub(r'^```(?:json)?\s*', '', t, flags=re.I)
    t = re.sub(r'\s*```$', '', t)
    i, j = t.find('{'), t.rfind('}')
    if i < 0 or j <= i:
        return None
    try:
        return json.loads(t[i:j + 1])
    except Exception:
        return None


def validate_plan(plan, width=None):
    """把 AI 输出收敛成安全、可执行的操作列表。"""
    warnings = []
    ops = []
    raw = plan.get('ops') if isinstance(plan, dict) else None
    if raw is None:
        raw = []
    if not isinstance(raw, list):
        return [], ['AI 返回的 ops 不是数组，已忽略']
    total = 0
    for o in raw[:MAX_OPS]:
        if not isinstance(o, dict):
            continue
        t = str(o.get('type') or '').lower()
        if t == 'paint':
            kind = str(o.get('kind') or 'ore').lower()
            if kind not in ('ore', 'gem', 'erase', 'other'):
                warnings.append('忽略未知画笔类型 %s' % kind)
                continue
            cells = _cells(o.get('cells'))
            if not cells:
                warnings.append('忽略没有有效坐标的刷格操作')
                continue
            total += len(cells)
            if total > MAX_CELLS:
                warnings.append('格子总数超过 %d，已截断' % MAX_CELLS)
                cells = cells[:max(0, MAX_CELLS - (total - len(cells)))]
            item = {'type': 'paint', 'kind': kind, 'cells': cells,
                    'val': _int(o.get('val'), 9, 0, 11)}
            if kind == 'other':
                item['id'] = _int(o.get('id'), 0, 0, 255)
            ops.append(item)
        elif t == 'height':
            cells = _cells(o.get('cells'))
            if not cells:
                continue
            ops.append({'type': 'height', 'cells': cells,
                        'level': _int(o.get('level'), 0, 0, 14)})
        elif t == 'waypoint':
            c = o.get('cell')
            if not (isinstance(c, (list, tuple)) and len(c) == 2):
                warnings.append('忽略格式不对的出生点')
                continue
            ops.append({'type': 'waypoint', 'index': _int(o.get('index'), 0, 0, 7),
                        'cell': [_int(c[0], 1, 1, 511), _int(c[1], 1, 1, 511)]})
        elif t in ('set', 'add', 'del'):
            sec = str(o.get('section') or '').strip()
            key = str(o.get('key') or '').strip()
            if sec.lower() not in EDITABLE_SECTIONS:
                warnings.append('禁止修改段 [%s]，已忽略' % sec)
                continue
            if not SAFE_NAME_RE.match(sec) or not SAFE_NAME_RE.match(key):
                warnings.append('忽略不合法的段名/键名')
                continue
            if key.lower() in BLOCKED_KEYS or 'pack' in key.lower() or 'digest' in key.lower():
                warnings.append('禁止修改二进制/校验字段 %s' % key)
                continue
            val = str(o.get('value') or '')[:MAX_VALUE_LEN]
            val = re.sub(r'[\r\n\x00]', ' ', val)
            ops.append({'type': t, 'section': sec, 'key': key, 'value': val})
        elif t == 'fog':
            a = str(o.get('action') or 'on').lower()
            if a not in ('on', 'off'):
                continue
            ops.append({'type': 'fog', 'action': a})
        else:
            warnings.append('忽略未知操作类型 %s' % t)
    if len(raw) > MAX_OPS:
        warnings.append('操作数超过 %d，只保留前 %d 条' % (MAX_OPS, MAX_OPS))
    return ops, warnings


def _int(v, default, lo, hi):
    try:
        n = int(float(v))
    except Exception:
        return default
    return max(lo, min(n, hi))


def _cells(v):
    out = []
    if not isinstance(v, list):
        return out
    for c in v[:MAX_CELLS]:
        if isinstance(c, (list, tuple)) and len(c) == 2:
            x, y = _int(c[0], -1, -1, 511), _int(c[1], -1, -1, 511)
            if 1 <= x <= 511 and 1 <= y <= 511:
                out.append([x, y])
    return out


def describe_plan(ops):
    """给前端展示的中文清单。"""
    out = []
    for o in ops:
        t = o['type']
        n = len(o.get('cells') or [])
        if t == 'paint':
            kn = {'ore': '刷矿石', 'gem': '刷宝石', 'erase': '擦除覆盖物',
                  'other': '刷指定覆盖物'}.get(o['kind'], o['kind'])
            out.append('%s %d 格（密度 %s）' % (kn, n, o.get('val')))
        elif t == 'height':
            out.append('把 %d 格高度改为 %s' % (n, o.get('level')))
        elif t == 'waypoint':
            out.append('设置出生点 %s → (%s,%s)' % (o.get('index'), o['cell'][0], o['cell'][1]))
        elif t == 'set':
            out.append('修改 [%s] %s = %s' % (o['section'], o['key'], o['value']))
        elif t == 'add':
            out.append('新增 [%s] %s = %s' % (o['section'], o['key'], o['value']))
        elif t == 'del':
            out.append('删除 [%s] %s' % (o['section'], o['key']))
        elif t == 'fog':
            out.append('取消迷雾（全图可见）' if o['action'] == 'on' else '恢复迷雾')
    return out


# ---------------------------------------------------------------- 对外接口
MODULE_GUIDE = {
    'maps': '地图库：搜索、筛选、目录归类、预览生成、批量操作。',
    'detail': '地图详情：解释 INI 参数含义、判断这张图适合什么玩法、给出修改建议。',
    'editor': 'INI 编辑器：逐段逐键解释字段、取值范围和改动后果。',
    'painter': '地图绘制：矿石/宝石/覆盖物/出生点/地形的坐标与参数。',
    'workbench': '绘制工作台：画笔、密度、高度、菱形坐标、保存与备份。',
    'modes': '游戏模式与注册：MPMaps 注册、模式项、地图归属。',
    'import': '导入地图：格式差异、编码、冲突处理、跨平台兼容性。',
    'platform': '平台地图导入：ra2mp / MP.dat / 解包缓存的区别与导入建议。',
    'fog': '战争迷雾：Shroud / FreeRadar / FogOfWar 相关参数。',
    'triggers': '触发器：事件、动作、标签、所属方与常见写法。',
    'general': '全局问答：可以问红警地图、联机、MOD、工具用法等任何问题。',
}


def ask(data_dir, question, module='general', context=None, map_path=None, prefer=None):
    """AI 助手的通用问答：按当前模块切换系统提示，带上下文但不上传地图原文。"""
    q = (question or '').strip()
    if not q:
        return {'ok': False, 'error': '请先写下问题'}
    digest = map_digest(map_path) if map_path else {}
    ext = context if isinstance(context, dict) else {}
    guide = MODULE_GUIDE.get(module or 'general', MODULE_GUIDE['general'])
    sys = ('你是「红警地图管理器」里的 AI 助手，用简体中文、直接简洁地回答。\n'
           '当前所在模块：%s\n'
           '用户在这个模块里能做的事：%s\n'
           '请结合模块场景回答；涉及具体参数时给出字段名、取值范围和推荐值。\n'
           '如果问题需要改地图，明确告诉用户可以用「生成方案」把操作列出来再确认应用，'
           '不要臆造坐标或字段。不要输出 JSON，不要 Markdown 代码围栏。' % (module, guide))
    messages = [
        {'role': 'system', 'content': sys},
        {'role': 'user', 'content': '当前状态与地图摘要：\n' +
         json.dumps({'地图': digest, '状态': ext, '参数速查': _help_hint()},
                    ensure_ascii=False)[:12000] + '\n\n用户问题：' + q[:3000]},
    ]
    r = chat(data_dir, messages, prefer=prefer)
    if not r.get('ok'):
        return r
    return {'ok': True, 'text': r['text'][:6000],
            'explain': r['text'][:6000], 'ops': [], 'steps': [], 'warnings': [],
            'usage': r.get('usage'), 'elapsed': r.get('elapsed'),
            'model': r.get('model'), 'used': r.get('used'),
            'attempts': r.get('attempts'), 'switched': r.get('switched')}


def plan(data_dir, question, map_path=None, context=None, mode='edit',
         prefer=None, module=''):
    """生成方案，不落盘。mode='ask' 时只问答、不产出 ops。"""
    q = (question or '').strip()
    if not q:
        return {'ok': False, 'error': '请先写下你想让 AI 做什么'}
    digest = map_digest(map_path) if map_path else {}
    ext = context if isinstance(context, dict) else {}
    if module:
        ext = dict(ext)
        ext['当前模块'] = MODULE_GUIDE.get(module, module)
    payload = {
        '地图': digest,
        '当前状态': ext,
        '参数速查': _help_hint(),
        '模式': '仅咨询（不要产出 ops）' if mode == 'ask' else '产出可执行方案',
    }
    sys = SYSTEM_PROMPT
    if mode == 'ask':
        sys += '\n本次是「仅咨询」模式：ops 必须是空数组，explain 里给出完整中文解答。'
    messages = [
        {'role': 'system', 'content': sys},
        {'role': 'user', 'content': '地图摘要与当前状态：\n' +
         json.dumps(payload, ensure_ascii=False)[:12000] +
         '\n\n用户要求：' + q[:2000]},
    ]
    r = chat(data_dir, messages, prefer=prefer)
    if not r.get('ok'):
        return r
    obj = _extract_json(r['text'])
    if obj is None:
        return {'ok': True, 'explain': r['text'][:2000], 'ops': [],
                'warnings': ['AI 没有返回规范 JSON，已按纯文本回答处理'],
                'usage': r.get('usage'), 'elapsed': r.get('elapsed'),
                'model': r.get('model'), 'used': r.get('used'),
                'attempts': r.get('attempts'), 'switched': r.get('switched')}
    ops, warns = validate_plan(obj, digest.get('mapSize'))
    if mode == 'ask':
        ops, warns = [], warns
    return {'ok': True, 'explain': str(obj.get('explain') or '')[:2000],
            'ops': ops, 'steps': describe_plan(ops), 'warnings': warns,
            'usage': r.get('usage'), 'elapsed': r.get('elapsed'),
            'model': r.get('model'), 'used': r.get('used'),
            'attempts': r.get('attempts'), 'switched': r.get('switched')}


def _backup(path, backup_root):
    os.makedirs(backup_root, exist_ok=True)
    stem, ext = os.path.splitext(os.path.basename(path))
    stamp = time.strftime('%Y%m%d_%H%M%S')
    dst = os.path.join(backup_root, '%s_AI_%s%s' % (stem, stamp, ext))
    n = 1
    while os.path.exists(dst):
        dst = os.path.join(backup_root, '%s_AI_%s_%d%s' % (stem, stamp, n, ext))
        n += 1
    shutil.copy2(path, dst)
    return dst


def _atomic(path, data):
    tmp = path + '.tmp-ai'
    with open(tmp, 'wb') as f:
        f.write(data)
    os.replace(tmp, path)


def apply_plan(path, ops, data_dir, backup_root, rel_key=''):
    """用户确认后执行方案。每一步重新读盘，顺序安全；失败整体回滚到最初备份。"""
    ops, warns = validate_plan({'ops': ops})
    if not ops:
        return {'ok': False, 'error': '没有可执行的操作', 'warnings': warns}
    if not os.path.isfile(path):
        return {'ok': False, 'error': '地图文件不存在'}
    first = _backup(path, backup_root)
    done = []
    try:
        # 1) 属性改动（文本 INI）
        changes = [{'op': o['type'], 'section': o['section'], 'key': o['key'],
                    'value': o.get('value', '')}
                   for o in ops if o['type'] in ('set', 'add', 'del')]
        if changes:
            d = INI.MapIni(path)
            n = len(d.apply(changes))
            if d.is_changed():
                _atomic(path, d.changed_bytes())
            done.append('属性改动 %d 项' % n)

        # 2) 迷雾
        for o in ops:
            if o['type'] != 'fog':
                continue
            if o['action'] == 'on':
                r = mapfog.apply(path, {'reveal': True, 'freeRadar': True,
                                        'fogOfWar': True, 'delay': 0},
                                 backup_root, data_dir, rel_key)
            else:
                r = mapfog.remove(path, backup_root, data_dir, rel_key)
            if not r.get('ok'):
                raise RuntimeError('迷雾处理失败：' + str(r.get('error')))
            done.append('取消迷雾' if o['action'] == 'on' else '恢复迷雾')

        # 3) 绘制：paint + waypoints 合并一次，height 按 level 分组各一次
        paints = [{'cells': o['cells'], 'kind': o['kind'], 'val': o.get('val', 9),
                   'id': o.get('id', 0)}
                  for o in ops if o['type'] == 'paint']
        wps = {'%d' % o['index']: o['cell'] for o in ops if o['type'] == 'waypoint'}
        if paints or wps:
            r = mapedit.save(path, {'paint': paints, 'waypoints': wps}, backup_root)
            if not r.get('ok'):
                raise RuntimeError('绘制保存失败：' + str(r.get('error')))
            done.append('刷格 %d 组 / 出生点 %d 个' % (len(paints), len(wps)))
        for o in ops:
            if o['type'] != 'height':
                continue
            r = mapedit.save(path, {'height': {'cells': o['cells'],
                                               'level': o['level']}}, backup_root)
            if not r.get('ok'):
                raise RuntimeError('高度保存失败：' + str(r.get('error')))
            done.append('改高度 %d 格 → %s' % (len(o['cells']), o['level']))

        return {'ok': True, 'done': done, 'steps': describe_plan(ops),
                'warnings': warns, 'backup': first}
    except Exception as e:
        try:
            shutil.copy2(first, path)
        except Exception:
            pass
        return {'ok': False, 'error': str(e), 'backup': first,
                'trace': traceback.format_exc()[-800:]}
