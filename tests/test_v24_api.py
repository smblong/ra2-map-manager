# -*- coding: utf-8 -*-
"""v2.4 API 回归：平台导入 / 名称特征多选 / AI 多连接配置与自动兜底。

进程内起服务，游戏目录和数据目录全部指向临时文件夹，绝不碰真实游戏目录。
"""
import io
import json
import os
import shutil
import sys
import tempfile
import threading
import urllib.parse
import urllib.request
from http.server import ThreadingHTTPServer

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import ra2map            # noqa: E402
import ai_proxy          # noqa: E402
import platform_import as PI  # noqa: E402

PORT = 8964
BASE = 'http://127.0.0.1:%d' % PORT
HT = r'E:\game\HTPlatform'
OUT = []
TMP = None


def rec(tag, ok, detail=''):
    OUT.append((tag, bool(ok)))
    print(('  PASS ' if ok else '  FAIL ') + tag + (('  ' + str(detail)) if detail != '' else ''))


def req(path, data=None, raw=False):
    url = BASE + path
    if data is None:
        r = urllib.request.urlopen(url, timeout=30)
    else:
        r = urllib.request.urlopen(urllib.request.Request(
            url, data=json.dumps(data).encode('utf-8'),
            headers={'Content-Type': 'application/json'}), timeout=30)
    b = r.read()
    return b if raw else json.loads(b.decode('utf-8'))


def setup():
    global TMP
    TMP = tempfile.mkdtemp(prefix='ra2v24api_')
    gd = os.path.join(TMP, 'game')
    data = os.path.join(TMP, 'data')
    os.makedirs(os.path.join(gd, 'Maps', 'Custom'))
    os.makedirs(data)
    ra2map.CONFIG.clear()
    ra2map.CONFIG['gameDir'] = gd
    ra2map.CONFIG['recent'] = []
    ra2map.CONFIG['ignored'] = []
    ra2map.DATA = data
    ra2map.CONFIG_PATH = os.path.join(data, 'config.json')
    ra2map.TRASH = os.path.join(data, '回收站'); os.makedirs(ra2map.TRASH, exist_ok=True)
    ra2map.TMP = os.path.join(data, '临时'); os.makedirs(ra2map.TMP, exist_ok=True)
    ra2map.BACKUP_DIR = os.path.join(data, '地图备份')
    os.makedirs(ra2map.BACKUP_DIR, exist_ok=True)
    ra2map.invalidate()
    ra2map.detect_game_dirs = lambda force=False: [gd]
    ra2map.render_one = lambda q: {'ok': True, 'skipped': True}
    return gd, data


def fake_map(name, extra=''):
    # 筛选测试走「按文件名」口径，INI 里的名字用纯 ASCII 避免编码干扰
    return ('[Basic]\r\nName=testmap\r\nMaxPlayer=8\r\n' + extra +
            '\r\n[Map]\r\nSize=0,0,50,54\r\nTheater=TEMPERATE\r\n'
            '\r\n[IsoMapPack5]\r\n1=AAAA\r\n').encode('ascii')


def main():
    gd, data = setup()
    cust = os.path.join(gd, 'Maps', 'Custom')
    # 4 张不同名称特征的测试图（按文件名口径）
    for n in ('中文名.mpr', 'abc.mpr', 'abc123.mpr', '小块地5.mpr'):
        with open(os.path.join(cust, n), 'wb') as f:
            f.write(fake_map(os.path.splitext(n)[0]))
    httpd = ThreadingHTTPServer(('127.0.0.1', PORT), ra2map.Handler)
    th = threading.Thread(target=httpd.serve_forever, daemon=True)
    th.start()
    try:
        # ------------------------------------------------ 基础
        ping = req('/api/ping')
        rec('版本号 2.4.1', ping.get('version') == '2.4.1', ping.get('version'))

        # ------------------------------------------------ 名称特征多选
        def maps(**kw):
            p = '&'.join('%s=%s' % (k, v) for k, v in kw.items())
            return req('/api/maps?nfbase=file' + ('&' + p if p else ''))

        rec('按文件名含中文', maps(nf='hascjk')['count'] == 2, maps(nf='hascjk')['count'])
        rec('按文件名含数字', maps(nf='hasdigit')['count'] == 2, maps(nf='hasdigit')['count'])
        r = req('/api/maps?nfbase=file&nf=hasalpha&nf=hasdigit')
        rec('多选 AND：含字母+含数字 = abc123', r['count'] == 1 and r['items'][0]['fileStem'] == 'abc123',
            [x['fileStem'] for x in r['items']])
        r = req('/api/maps?nfbase=file&nf=hasalpha&nf=hasdigit&nflogic=or')
        rec('多选 OR：含字母或数字 = 3', r['count'] == 3, [x['fileStem'] for x in r['items']])
        r = req('/api/maps?nfbase=file&nf=onlyalpha')
        rec('只有字母 = abc', r['count'] == 1 and r['items'][0]['fileStem'] == 'abc')
        r = req('/api/maps?nfbase=file&nf=nocjk&nf=nodigit')
        rec('多选 AND：无中文且无数字 = abc', r['count'] == 1)

        # ------------------------------------------------ 平台扫描（只读）
        sc = req('/api/platform/scan?root=' + urllib.parse.quote(HT))
        rec('平台扫描无错', not sc.get('error') and sc['count'] > 100,
            '%s 条 / 可导入 %s / 加密 %s' % (sc.get('count'), sc.get('ready'), sc.get('locked')))
        rec('MP.dat 中文名解析', any('万福玛利亚' in m['name'] for m in sc['items']))
        rec('ra2mp 加密项标记', sc['locked'] == 2 and all(
            m['ready'] is False for m in sc['items'] if m['source'] == '平台地图包 .ra2mp'))
        rec('解包缓存通道', any(m['source'] == '平台解包缓存' for m in sc['items']))
        rec('预览配对', sum(1 for m in sc['items'] if m['thumb']) >= 170)
        rec('导入目标列表', any(t['path'] == 'Custom' for t in sc['targets']))
        thumb_item = next(m for m in sc['items'] if m['thumb'])
        b = req('/api/platform/thumb?id=' + urllib.parse.quote(thumb_item['id']), raw=True)
        rec('预览图输出 JPEG', b[:2] == b'\xff\xd8', len(b))

        # ------------------------------------------------ 平台导入落盘（临时目录）
        pick = [m['id'] for m in sc['items'] if m['ready'] and m['source'] == '平台地图库 MP.dat'][:2]
        r = req('/api/platform/import', {'ids': pick, 'target': 'Custom', 'conflict': 'skip'})
        rec('导入 2 张平台地图', r.get('imported') == 2, r)
        files = [f for f in os.listdir(cust) if f not in
                 ('中文名.mpr', 'abc.mpr', 'abc123.mpr', '小块地5.mpr')]
        rec('落盘文件可读', len(files) == 2 and all(
            open(os.path.join(cust, f), 'rb').read(1) in (b'[', b';') for f in files), files)
        r2 = req('/api/platform/import', {'ids': pick, 'target': 'Custom', 'conflict': 'skip'})
        rec('同名跳过', r2.get('skipped') == 2 and r2.get('imported') == 0, r2)
        r3 = req('/api/platform/import', {'ids': pick, 'target': 'Custom', 'conflict': 'rename'})
        rec('同名自动改名', r3.get('imported') == 2, r3)
        locked = [m['id'] for m in sc['items'] if not m['ready']]
        r4 = req('/api/platform/import', {'ids': locked, 'target': 'Custom'})
        rec('加密图拒绝写入', r4.get('imported') == 0 and r4.get('locked') == 2, r4)

        # ------------------------------------------------ AI 配置（脱敏/多连接）
        cfg0 = req('/api/ai/config')
        rec('配置默认结构', cfg0.get('version') == 2 and isinstance(cfg0.get('connections'), list))
        saved = req('/api/ai/config', {
            'connections': [
                {'id': 'c1', 'name': '主号', 'preset': 'deepseek',
                 'baseUrl': 'https://a.example/v1', 'key': 'sk-aaa111111111',
                 'models': ['m-chat', 'm-reason'], 'enabledModels': ['m-chat', 'm-reason'],
                 'timeout': 45, 'enabled': True},
                {'id': 'c2', 'name': '备用号', 'preset': 'custom',
                 'baseUrl': 'https://b.example/v1', 'key': 'sk-bbb222222222',
                 'models': ['m-chat'], 'enabledModels': ['m-chat'],
                 'timeout': 60, 'enabled': True},
            ],
            'active': {'connId': 'c1', 'model': 'm-chat'},
            'fallback': True, 'temperature': 0.4})
        rec('保存 2 条连接（同一模型可配不同连接）', len(saved['connections']) == 2)
        rec('密钥不明文回传', all(c['key'] == '' and c['hasKey'] for c in saved['connections']),
            [c['keyMasked'] for c in saved['connections']])
        rec('可用模型 3 个', len(saved['options']) == 3, saved['options'])
        rec('配置就绪', saved['ready'] is True)
        # key 留空表示沿用
        saved2 = req('/api/ai/config', {'connections': saved['connections']})
        rec('留空 key 不丢密钥', all(c['hasKey'] for c in saved2['connections']))

        # ------------------------------------------------ 获取模型（mock /models）
        orig_open = urllib.request.urlopen

        class FakeResp(io.BytesIO):
            def __enter__(self): return self
            def __exit__(self, *a): return False

        def patched(reqobj, *a, **kw):
            u = getattr(reqobj, 'full_url', str(reqobj))
            if '/models' in u and '127.0.0.1' not in u:
                return FakeResp(json.dumps({'data': [{'id': 'm-chat'}, {'id': 'm-new'}]}).encode())
            return orig_open(reqobj, *a, **kw)

        urllib.request.urlopen = patched
        try:
            fm = req('/api/ai/models', {'connId': 'c1', 'baseUrl': 'https://a.example/v1'})
            rec('获取模型列表', fm.get('ok') and fm['models'] == ['m-chat', 'm-new'], fm)
        finally:
            urllib.request.urlopen = orig_open

        # ------------------------------------------------ 自动兜底（mock _chat_once）
        orig_chat = ai_proxy._chat_once
        calls = []

        def flaky(conn, model, messages, temperature=0.3):
            calls.append((conn['name'], model))
            if conn['id'] == 'c1' and model == 'm-chat':
                return {'ok': False, 'error': '402 余额不足', 'retryable': True}
            return {'ok': True, 'text': '兜底回答', 'model': model, 'usage': {}, 'elapsed': 0.1}

        ai_proxy._chat_once = flaky
        try:
            r = req('/api/ai/chat', {'question': 'MaxPlayer 怎么用', 'module': 'editor'})
            rec('AI 问答成功', r.get('ok') and '兜底回答' in r.get('text', ''), r.get('error'))
            rec('自动兜底已切换', r.get('switched') is True and len(r.get('attempts') or []) >= 2,
                r.get('attempts'))
            rec('返回实际使用模型', (r.get('used') or {}).get('connId') == 'c1', r.get('used'))
            rec('候选顺序：优先当前使用', calls[0] == ('主号', 'm-chat'), calls)

            # 生成方案 + 应用（mock 返回规范 JSON）
            def plan_chat(conn, model, messages, temperature=0.3):
                return {'ok': True, 'text': json.dumps({
                    'explain': '把最大玩家改成 6',
                    'ops': [{'type': 'set', 'section': 'Basic', 'key': 'MaxPlayer', 'value': '6'}]
                }), 'model': model, 'usage': {}, 'elapsed': 0.1}
            ai_proxy._chat_once = plan_chat
            mid = ra2map.get_maps()[0]['id']
            r = req('/api/ai/plan', {'question': '把最大玩家改成 6', 'id': mid, 'module': 'detail'})
            rec('AI 生成方案', r.get('ok') and len(r.get('ops') or []) == 1, r.get('ops'))
            r = req('/api/ai/apply', {'id': mid, 'ops': r['ops']})
            rec('AI 方案落盘', r.get('ok') is True, r.get('error'))
        finally:
            ai_proxy._chat_once = orig_chat

        # ------------------------------------------------ 关闭兜底
        req('/api/ai/config', {'fallback': False})
        orig2 = ai_proxy._chat_once
        ai_proxy._chat_once = lambda c, m, msgs, temperature=0.3: {
            'ok': False, 'error': '429 限流', 'retryable': True}
        try:
            r = req('/api/ai/chat', {'question': '测试', 'module': 'general'})
            rec('关闭兜底只试一次', not r.get('ok') and len(r.get('attempts') or []) == 1,
                r.get('attempts'))
        finally:
            ai_proxy._chat_once = orig2

    finally:
        httpd.shutdown()
        httpd.server_close()
        shutil.rmtree(TMP, ignore_errors=True)

    ok = sum(1 for _, v in OUT if v)
    print('\n结果：%d/%d 通过' % (ok, len(OUT)))
    return 0 if ok == len(OUT) else 1


if __name__ == '__main__':
    import urllib.parse
    sys.exit(main())
