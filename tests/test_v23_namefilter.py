# -*- coding: utf-8 -*-
"""v2.3 后端测试：名称特征筛选 + 右键「打开地图文件位置」。

不碰真实游戏目录：
  1. name_flags / name_match 纯函数边界
  2. 用固定假数据直接调 Handler.api_maps，验证 nf / nfbase / only / q 的叠加
  3. reveal_in_explorer 传参（explorer /select,<path> 单参数）
  4. 真起一个临时 HTTP 服务，验证 /api/openfolder 正常 / 不存在 / 越权三种返回
"""
import json
import os
import shutil
import sys
import tempfile
import threading
import urllib.error
import urllib.parse
import urllib.request
from http.server import ThreadingHTTPServer

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import ra2map as R                 # noqa: E402

PORT = 8957
OUT = []


def rec(tag, v, detail=''):
    OUT.append((tag, v))
    print('%s: %s' % (tag, v) + (('  ' + str(detail)) if detail != '' else ''))


def q(**kw):
    return {k: [str(v)] for k, v in kw.items()}


def fake_maps():
    """name = 游戏大厅显示名；fileStem = 磁盘文件名（卡片上显示）。"""
    base = dict(folder='Custom', theater='Temperate', size=1024, mtime=1,
                players='2', hasThumb=True, width=0, height=0)
    rows = [
        ('A', '小块地5', 'xkdb5'),
        ('B', 'No name', '小块地'),
        ('C', 'c1a01md', 'c1a01md'),
        ('D', '两人一小块地', 'liangren'),
        ('E', '123', '123'),
        ('F', 'abc※', 'abc※'),
    ]
    out = []
    for tag, name, stem in rows:
        m = dict(base)
        m.update(id='Custom/' + stem + '.mpr', file=stem + '.mpr',
                 name=name, stem=stem, fileStem=stem,
                 hasThumb=(tag != 'F'))
        out.append(m)
    return out


def main():
    rec('版本 2.4.1', R.VERSION == '2.4.1', R.VERSION)

    # ---- 1. 纯函数 ----
    cases = [
        ('小块地5', 'hasdigit', True), ('小块地5', 'onlycjk', False),
        ('小块地5', 'hascjk', True), ('小块地5', 'nodigit', False),
        ('abc', 'onlyalpha', True), ('abc', 'hasdigit', False),
        ('No name', 'onlyalpha', True), ('c1a01md', 'alnum', True),
        ('c1a01md', 'onlyalpha', False), ('WWW.RA2.CLUB', 'hasdigit', True),
        ('两人一小块地', 'onlycjk', True), ('123', 'onlydigit', True),
        ('3-4人图', 'onlydigit', False), ('abc※', 'special', True),
        ('abc※', 'onlyalpha', False), ('abc', 'nocjk', True),
        ('小块地', 'noalpha', True), ('', 'hasalpha', False),
        (None, 'hascjk', False), ('abc', '', True),
        ('abc', 'unknown-code', True),
    ]
    bad = [(s, n, e, R.name_match(s, n)) for s, n, e in cases if R.name_match(s, n) != e]
    rec('name_match %d 个边界用例' % len(cases), not bad, bad[:3])
    rec('name_flags 四标记正确', R.name_flags('a5中※') == (True, True, True, True),
        R.name_flags('a5中※'))
    rec('分隔符不算其它符号', R.name_flags('a-b_c.d (e)') == (True, False, False, False))
    rec('占位名识别', R.is_placeholder_name('No name') and R.is_placeholder_name('')
        and R.is_placeholder_name('  UNNAMED ') and not R.is_placeholder_name('小块地'))
    rec('占位名按游戏名口径回退到文件名',
        R.nf_value({'name': 'No name', 'fileStem': '小块地'}, 'name') == '小块地')
    rec('按文件名口径不受占位名影响',
        R.nf_value({'name': 'No name', 'fileStem': '小块地'}, 'file') == '小块地')
    rec('真英文名不回退',
        R.nf_value({'name': 'Caverns of Siberia', 'fileStem': '西伯利亚'}, 'name') == 'Caverns of Siberia')

    # ---- 2. api_maps 筛选（固定假数据，精确计数） ----
    orig_get = R.get_maps
    R.get_maps = lambda force=False: fake_maps()
    try:
        def ids(**kw):
            return [x['id'] for x in R.Handler.api_maps(object(), q(**kw))['items']]

        rec('nf 为空返回 6 张', len(ids()) == 6, len(ids()))
        rec('按游戏名 含数字 = A/C/E', ids(nf='hasdigit') ==
            ['Custom/xkdb5.mpr', 'Custom/c1a01md.mpr', 'Custom/123.mpr'])
        rec('按游戏名 含中文 = A/B/D（B 的 No name 已回退文件名）', ids(nf='hascjk') ==
            ['Custom/xkdb5.mpr', 'Custom/小块地.mpr', 'Custom/liangren.mpr'])
        rec('按文件名 含中文 = B（口径不同）', ids(nf='hascjk', nfbase='file') ==
            ['Custom/小块地.mpr'])
        rec('按游戏名 含字母 = C/F（占位名不参与判定）', ids(nf='hasalpha') ==
            ['Custom/c1a01md.mpr', 'Custom/abc※.mpr'])
        rec('按文件名 只有中文 = B', ids(nf='onlycjk', nfbase='file') ==
            ['Custom/小块地.mpr'])
        rec('按文件名 只有字母 = D（C 带数字不算）',
            ids(nf='onlyalpha', nfbase='file') == ['Custom/liangren.mpr'])
        rec('按游戏名 只有字母 = 0 张（No name 占位已回退）', ids(nf='onlyalpha') == [])
        rec('按游戏名 只有数字 = E', ids(nf='onlydigit') == ['Custom/123.mpr'])
        rec('按游戏名 字母+数字混合 = C', ids(nf='alnum') == ['Custom/c1a01md.mpr'])
        rec('按游戏名 不含中文 = C/E/F', len(ids(nf='nocjk')) == 3)
        rec('按游戏名 不含字母 = A/B/D/E', len(ids(nf='noalpha')) == 4)
        rec('按游戏名 含其它符号 = F', ids(nf='special') == ['Custom/abc※.mpr'])
        rec('未知筛选码放行', len(ids(nf='not-a-code')) == 6)
        rec('与预览筛选叠加（缺预览+特殊符号）',
            ids(nf='special', only='nothumb') == ['Custom/abc※.mpr'])
        rec('与预览筛选叠加（有预览+特殊符号）= 0',
            ids(nf='special', only='hasthumb') == [])
        rec('与搜索词叠加', ids(nf='hascjk', q='两人') == ['Custom/liangren.mpr'])
        rec('count 与 items 一致',
            R.Handler.api_maps(object(), q(nf='hasdigit'))['count'] == 3)
        rec('排序参数不破坏筛选',
            len(R.Handler.api_maps(object(), q(nf='hasdigit', sort='name'))['items']) == 3)
    finally:
        R.get_maps = orig_get

    # ---- 3. explorer 参数 ----
    orig_popen = R.subprocess.Popen
    calls = []
    R.subprocess.Popen = lambda args, *a, **k: calls.append(args)
    try:
        p = r'D:\Games\RA2\Maps\Custom\a b.mpr'
        R.reveal_in_explorer(p)
        rec('explorer /select 单参数拼接', calls == [['explorer', '/select,' + os.path.normpath(p)]], calls)
    finally:
        R.subprocess.Popen = orig_popen

    # ---- 4. /api/openfolder 路由 ----
    tmp = tempfile.mkdtemp(prefix='ra2v23_')
    httpd = None
    orig_gd = R.CONFIG.get('gameDir') or ''
    orig_reveal = R.reveal_in_explorer
    revealed = []
    try:
        gd = os.path.join(tmp, 'game')
        cust = os.path.join(gd, 'Maps', 'Custom')
        os.makedirs(cust)
        with open(os.path.join(cust, '定位测试.mpr'), 'wb') as f:
            f.write(b'[Basic]\r\nName=x\r\n')
        R.CONFIG['gameDir'] = gd
        R.invalidate()
        R.reveal_in_explorer = lambda p: revealed.append(p)

        httpd = ThreadingHTTPServer(('127.0.0.1', PORT), R.Handler)
        threading.Thread(target=httpd.serve_forever, daemon=True).start()

        def get(mid):
            u = 'http://127.0.0.1:%d/api/openfolder?id=%s' % (PORT, urllib.parse.quote(mid))
            try:
                with urllib.request.urlopen(u, timeout=5) as r:
                    return r.status, json.loads(r.read().decode('utf-8'))
            except urllib.error.HTTPError as e:
                return e.code, json.loads(e.read().decode('utf-8'))

        st, body = get('Custom/定位测试.mpr')
        rec('正常定位返回 200 ok', st == 200 and body.get('ok') is True, (st, body))
        rec('返回真实绝对路径', body.get('path', '').endswith('定位测试.mpr'), body.get('path'))
        rec('服务端确实调用了 reveal', len(revealed) == 1 and revealed[0].endswith('定位测试.mpr'))
        st2, body2 = get('Custom/不存在.mpr')
        rec('文件不存在返回 404', st2 == 404 and 'error' in body2, (st2, body2))
        st3, body3 = get('../../evil.mpr')
        rec('越权路径返回 400', st3 == 400 and 'error' in body3, (st3, body3))
    finally:
        R.reveal_in_explorer = orig_reveal
        R.CONFIG['gameDir'] = orig_gd
        R.invalidate()
        if httpd is not None:
            httpd.shutdown()
            httpd.server_close()
        shutil.rmtree(tmp, ignore_errors=True)

    # ---- 5. 真实目录烟测 ----
    real = R.get_maps()
    ndigit = sum(1 for m in real if R.name_match(m['fileStem'], 'hasdigit'))
    rec('真实目录 600+ 张可读', len(real) >= 600, len(real))
    rec('真实目录 按文件名含数字 > 0', ndigit > 0, ndigit)

    npass = sum(1 for _, v in OUT if v)
    print('\n%d/%d PASS' % (npass, len(OUT)))
    return 0 if npass == len(OUT) else 1


if __name__ == '__main__':
    sys.exit(main())
