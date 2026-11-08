# -*- coding: utf-8 -*-
"""
红警地图管理器 —— 本地服务端
- 扫描游戏 Maps 目录，解析地图元数据（名称/人数/尺寸/作者）
- 用游戏自带的 CNCMaps 渲染器生成真实预览图（无需进游戏）
- 删除走"回收站"，可还原
- 支持拖拽导入 yrm/map/mpr 以及 zip/rar/7z 地图包
"""
import hashlib
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import traceback
import uuid
import zipfile
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs, unquote

try:
    from PIL import Image
except ImportError:
    print("缺少 Pillow，请执行: python -m pip install pillow")
    sys.exit(1)

import ini_edit as INI
import mapgen
import mapedit
import overlay_catalog
import modereg
import native_win
import mapfog
import ai_proxy
import platform_import as PI
import trigger_defs as TRIG

# 打包成 exe 后：资源在临时解压目录(_MEIPASS)，数据要落盘持久保存
FROZEN = bool(getattr(sys, 'frozen', False))
if FROZEN:
    APP_DIR = os.path.dirname(os.path.abspath(sys.executable))
    RES_DIR = getattr(sys, '_MEIPASS', APP_DIR)
else:
    APP_DIR = os.path.dirname(os.path.abspath(__file__))
    RES_DIR = APP_DIR
ROOT = APP_DIR
WEB = os.path.join(RES_DIR, 'web')


def _pick_data_dir():
    """优先用 exe 同级的"数据"（整个文件夹一起移动时缓存可带走）；
    单独把 exe 拷到别处时，退到用户目录，不污染桌面/游戏目录"""
    local_app = os.path.join(os.environ.get('LOCALAPPDATA') or tempfile.gettempdir(),
                             'RedAlertMapManager', '数据')
    beside = os.path.join(APP_DIR, '数据')
    cands = [beside, local_app] if os.path.isdir(beside) else [local_app, beside]
    for c in cands:
        try:
            os.makedirs(os.path.join(c, '预览缓存'), exist_ok=True)
            return c
        except OSError:
            continue
    return beside


DATA = _pick_data_dir()
THUMBS = os.path.join(DATA, '预览缓存')
BASEMAP_DIR = os.path.join(DATA, '地形底图')
TRASH = os.path.join(DATA, '回收站')
TMP = os.path.join(DATA, '临时')
CONFIG_PATH = os.path.join(DATA, 'config.json')
META_CACHE_PATH = os.path.join(DATA, '元数据缓存.json')
for d in (DATA, THUMBS, BASEMAP_DIR, TRASH, TMP):
    os.makedirs(d, exist_ok=True)

VERSION = '2.4.1'
MAP_EXT = ('.yrm', '.map', '.mpr', '.mms')
IMG_EXT = ('.jpg', '.jpeg', '.png')
ARCHIVE_EXT = ('.zip', '.rar', '.7z', '.tar', '.gz', '.lzh')
FOLDER_LABEL = {
    'custom': '自定义地图', 'standard': '官方标准', 'cooperative': '合作任务',
    'workshop': '工坊地图', 'ddly': 'DDLY 图包', 'madhq': 'MadHQ 图包',
}
CREATE_NO_WINDOW = 0x08000000
RENDER_SEM = threading.Semaphore(3)
BASEMAP_SEM = threading.Semaphore(1)
META_LOCK = threading.Lock()
LOG_LOCK = threading.Lock()


LOG_PATH = os.path.join(DATA, '运行日志.txt')
SERVER = {}
QUIT = {'at': 0.0}


def schedule_quit(delay=4.5):
    """界面窗口被关掉后延迟退出；期间若页面又发出请求（比如按了 F5 刷新）就取消"""
    QUIT['at'] = time.time() + delay
    target = QUIT['at']

    def worker():
        while True:
            time.sleep(0.3)
            if QUIT['at'] == 0 or QUIT['at'] != target:
                return
            if time.time() >= target:
                log('界面窗口已关闭，程序自动退出')
                os._exit(0)

    threading.Thread(target=worker, daemon=True).start()


def log(msg):
    line = '[%s] %s' % (time.strftime('%Y-%m-%d %H:%M:%S'), msg)
    with LOG_LOCK:
        try:
            if sys.stdout is not None:
                print(line, flush=True)
        except Exception:
            pass
        try:
            if os.path.isfile(LOG_PATH) and os.path.getsize(LOG_PATH) > 3 * 1024 * 1024:
                os.replace(LOG_PATH, LOG_PATH + '.old')
            with open(LOG_PATH, 'a', encoding='utf-8') as f:
                f.write(line + '\n')
        except Exception:
            pass


def fatal(msg, detail=''):
    """打包成无控制台程序后，错误必须弹窗告知，否则双击后毫无反应"""
    log('致命错误: ' + msg + (' | ' + detail if detail else ''))
    try:
        import ctypes
        ctypes.windll.user32.MessageBoxW(
            None, msg + ('\n\n' + detail if detail else ''), '红警地图管理器', 0x10)
    except Exception:
        pass


# ---------------------------------------------------------------- 配置
def load_json(path, default):
    try:
        with open(path, 'r', encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        return default


def save_json(path, obj):
    tmp = path + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(obj, f, ensure_ascii=False, indent=1)
    os.replace(tmp, path)


CONFIG = load_json(CONFIG_PATH, {})
META_CACHE = load_json(META_CACHE_PATH, {})
_cache_dirty = False


def save_meta_cache():
    global _cache_dirty
    with META_LOCK:
        if not _cache_dirty:
            return
        _cache_dirty = False
        save_json(META_CACHE_PATH, META_CACHE)


def game_dir():
    d = CONFIG.get('gameDir') or ''
    return d if d and os.path.isdir(d) else ''


def maps_root():
    g = game_dir()
    if not g:
        return ''
    p = os.path.join(g, 'Maps')
    return p if os.path.isdir(p) else ''


# ---------------------------------------------------------------- 目录探测
_DETECTED = {'dirs': [], 'ts': 0}


def detect_game_dirs(force=False):
    """扫描各盘，找含 Resources\\ClientDefinitions.ini 的游戏目录"""
    if not force and _DETECTED['ts'] and time.time() - _DETECTED['ts'] < 600:
        return _DETECTED['dirs']
    found = []
    skip = {'windows', 'program files', 'program files (x86)', 'programdata', '$recycle.bin',
            'system volume information', 'appdata', 'node_modules'}
    roots = []
    for letter in 'CDEFGHIJ':
        r = letter + ':\\'
        if os.path.isdir(r):
            roots.append(r)
    for r in roots:
        base_depth = r.rstrip('\\').count('\\')
        for cur, dirs, files in os.walk(r):
            depth = cur.rstrip('\\').count('\\') - base_depth
            if 'ClientDefinitions.ini' in files and os.path.basename(cur).lower() == 'resources':
                g = os.path.dirname(cur)
                if os.path.isfile(os.path.join(g, 'gamemd.exe')):
                    found.append(g)
            if depth >= 3:
                dirs[:] = []
                continue
            dirs[:] = [d for d in dirs if d.lower() not in skip]
    found = sorted(set(found))
    _DETECTED['dirs'] = found
    _DETECTED['ts'] = time.time()
    return found


def find_renderer(gdir):
    """定位 CNCMaps.Renderer.exe"""
    if not gdir:
        return None
    c = os.path.join(gdir, 'Resources', 'RandomMapGenerator_RA2', 'Map Renderer', 'CNCMaps.Renderer.exe')
    if os.path.isfile(c):
        return (c, os.path.dirname(c))
    for cand in detect_game_dirs():
        c = os.path.join(cand, 'Resources', 'RandomMapGenerator_RA2', 'Map Renderer', 'CNCMaps.Renderer.exe')
        if os.path.isfile(c):
            return (c, os.path.dirname(c))
    return None


def find_7z(gdir):
    for c in (os.path.join(gdir or '', 'Resources', '7z.exe'),
              r'C:\Program Files\7-Zip\7z.exe', r'C:\Program Files (x86)\7-Zip\7z.exe'):
        if os.path.isfile(c):
            return c
    return None


def mix_dir_for(path):
    """地图所属游戏的根目录（含 ra2md.mix 的那层）"""
    cur = os.path.dirname(os.path.abspath(path))
    for _ in range(6):
        for mix in ('ra2md.mix', 'ra2.mix', 'multimd.mix'):
            if os.path.isfile(os.path.join(cur, mix)):
                return cur
        parent = os.path.dirname(cur)
        if parent == cur:
            break
        cur = parent
    return game_dir()


# ---------------------------------------------------------------- 元数据解析
def _section(text, name):
    m = re.search(r'(?ms)^\[' + re.escape(name) + r'\]\s*\n(.*?)(?=^\[|\Z)', text)
    return m.group(1) if m else ''


def _kv(body, key):
    m = re.search(r'(?mi)^\s*' + re.escape(key) + r'\s*=(.*)$', body)
    return m.group(1).strip() if m else ''


def _read_head(path, limit=6 * 1024 * 1024, chunk=196608):
    """地图文件里 [PreviewPack] 内嵌图片会排在靠前位置，[Basic] 可能被推到几十 KB 之后，
    所以按块读取，一旦关键段都到齐就停，避免整份读入。"""
    buf = b''
    try:
        with open(path, 'rb') as f:
            while len(buf) < limit:
                b = f.read(chunk)
                if not b:
                    break
                buf += b
                if buf.count(b'[Basic]') and buf.count(b'[Map]') and buf.count(b'Name='):
                    break
    except Exception:
        pass
    return buf.decode('gbk', 'replace')


def parse_map(path):
    st = os.stat(path)
    key = '%s|%d|%d' % (path.lower(), int(st.st_mtime), st.st_size)
    with META_LOCK:
        hit = META_CACHE.get(key)
    if hit:
        return hit
    meta = {}
    try:
        text = _read_head(path)
        basic = _section(text, 'Basic')
        mapp = _section(text, 'Map')
        head_info = _section(text, 'Header') or _section(text, 'Head')
        name = _kv(basic, 'Name')
        if name.lower().startswith('name:'):
            name = ''
        author = _kv(basic, 'Author') or _kv(head_info, 'Author')
        meta = {
            'name': name or '',
            'author': author or '',
            'maxPlayer': _kv(basic, 'MaxPlayer') or _kv(head_info, 'NumberStartingPoints'),
            'minPlayer': _kv(basic, 'MinPlayer') or '',
            'gameMode': _kv(basic, 'GameMode') or _kv(head_info, 'GameMode'),
            'official': (_kv(basic, 'Official') or '').lower() == 'yes',
            'theater': _kv(mapp, 'Theater'),
            'size': _kv(mapp, 'Size'),
            'localSize': _kv(mapp, 'LocalSize'),
            'valid': bool(basic or mapp),
        }
    except Exception:
        meta = {'name': '', 'valid': False}
    with META_LOCK:
        META_CACHE[key] = meta
        global _cache_dirty
        _cache_dirty = True
    return meta


def parse_size(s):
    try:
        a = [int(x) for x in s.split(',')]
        if len(a) == 4:
            return a[2], a[3]
        if len(a) == 2:
            return a[0], a[1]
    except Exception:
        pass
    return 0, 0


# ------------------------------------------------- 名称特征筛选（v2.3 / v2.4.1 修正）
# 卡片上显示的是文件名（stem / fileStem），游戏大厅里显示的是地图 INI 的
# [Basic] Name（name）。实测 629 张图里 447 张两者不同，所以筛选依据可切换。
# 只认 ASCII 字母 / 阿拉伯数字 / CJK 汉字，其余归类为「分隔符」或「其它符号」。
# v2.4.1：大量自制图的 [Basic] Name 是占位串（No name 等），它不是真名字，
# 按游戏名口径判定时必须回退到文件名，否则「只有字母」会混进一堆纯中文卡片。
_NAME_SEPS = set(' \t\r\n-_.()[]{}!+~#&,:;/\'"|@')
NF_PLACEHOLDER = {'', 'no name', 'noname', 'no-name', 'unnamed', 'new map',
                  'newmap', 'untitled', '未命名', '无名', 'none', 'n/a'}


def is_placeholder_name(s):
    """地图 INI 里的占位名 / 空名，不是作者真正起的名字。"""
    return (s or '').strip().lower() in NF_PLACEHOLDER


def nf_value(m, nfbase='name'):
    """返回这张图实际参与名称特征判定的字符串（前端卡片小字也按这个显示）。"""
    stem = m.get('fileStem') or m.get('stem') or ''
    if nfbase == 'file':
        return stem
    n = m.get('name') or ''
    return stem if is_placeholder_name(n) else n


def name_flags(s):
    """把名称拆成 (含字母, 含数字, 含汉字, 含其它符号) 四个布尔标记。"""
    a = d = c = x = False
    for ch in (s or ''):
        o = ord(ch)
        if 65 <= o <= 90 or 97 <= o <= 122:
            a = True
        elif 48 <= o <= 57:
            d = True
        elif 0x3400 <= o <= 0x9fff:
            c = True
        elif ch in _NAME_SEPS:
            pass
        else:
            x = True
    return a, d, c, x


def name_match(s, nf):
    """名称 s 是否命中筛选码 nf；nf 为空 = 不筛选，未知码一律放行。"""
    if not nf:
        return True
    a, d, c, x = name_flags(s)
    if nf == 'hasalpha':  return a
    if nf == 'hasdigit':  return d
    if nf == 'hascjk':    return c
    if nf == 'noalpha':   return not a
    if nf == 'nodigit':   return not d
    if nf == 'nocjk':     return not c
    if nf == 'alnum':     return a and d
    if nf == 'special':   return x
    if nf == 'onlyalpha': return a and not (d or c or x)
    if nf == 'onlydigit': return d and not (a or c or x)
    if nf == 'onlycjk':   return c and not (a or d or x)
    return True


# ---------------------------------------------------------------- 扫描
_SCAN_LOCK = threading.Lock()


def scan_maps():
    root = maps_root()
    if not root:
        return []
    items = []
    for cur, dirs, files in os.walk(root):
        # 跳过 CP（整合包自带的历史快照，里面是整份游戏副本，重复且量大）
        dirs[:] = [d for d in dirs if not d.startswith('.') and d.lower() != 'cp']
        low = {f.lower(): f for f in files}
        for f in files:
            ext = os.path.splitext(f)[1].lower()
            if ext not in MAP_EXT:
                continue
            full = os.path.join(cur, f)
            try:
                st = os.stat(full)
            except OSError:
                continue
            rel = os.path.relpath(full, root).replace('\\', '/')
            folder = os.path.dirname(rel) or '.'
            stem = os.path.splitext(f)[0]
            img = ''
            for ie in IMG_EXT:
                if (stem + ie).lower() in low:
                    img = os.path.join(cur, low[(stem + ie).lower()])
                    break
            m = parse_map(full)
            w, h = parse_size(m.get('size') or '')
            items.append({
                'id': rel,
                'file': f,
                'stem': stem,
                'folder': folder,
                'folderLabel': FOLDER_LABEL.get(folder.split('/')[-1].lower(), folder.split('/')[-1]),
                'size': st.st_size,
                'mtime': int(st.st_mtime),
                'name': m.get('name') or stem,
                'fileStem': stem,
                'players': m.get('maxPlayer') or '',
                'minPlayers': m.get('minPlayer') or '',
                'theater': m.get('theater') or '',
                'width': w, 'height': h,
                'author': m.get('author') or '',
                'official': m.get('official', False),
                'hasImg': bool(img),
                'hasThumb': os.path.isfile(thumb_path_for(full)) or bool(img),
            })
    items.sort(key=lambda x: (x['folder'].lower(), x['file'].lower()))
    save_meta_cache()
    return items


_MAPS_CACHE = {'data': [], 'ts': 0, 'dir': ''}


def get_maps(force=False):
    g = game_dir()
    with _SCAN_LOCK:
        if not force and _MAPS_CACHE['ts'] and _MAPS_CACHE['dir'] == g and time.time() - _MAPS_CACHE['ts'] < 30:
            return _MAPS_CACHE['data']
        data = scan_maps()
        _MAPS_CACHE.update({'data': data, 'ts': time.time(), 'dir': g})
        return data


def invalidate():
    _MAPS_CACHE['ts'] = 0
    _MODES_CACHE['ts'] = 0


_MODES_CACHE = {'ts': 0, 'dir': '', 'data': None}


def modes_state(force=False):
    """游戏模式与注册表状态，30 秒缓存"""
    g = game_dir()
    if not force and _MODES_CACHE['data'] and _MODES_CACHE['dir'] == g \
            and time.time() - _MODES_CACHE['ts'] < 30:
        return _MODES_CACHE['data']
    try:
        d = modereg.state(g)
    except Exception as e:
        log('读取模式配置失败: %s' % e)
        return {'error': str(e)}
    _MODES_CACHE.update({'ts': time.time(), 'dir': g, 'data': d})
    return d

def abs_of(mid):
    root = maps_root()
    if not root:
        raise ValueError('未设置游戏目录')
    p = os.path.normpath(os.path.join(root, mid.replace('/', os.sep)))
    if not p.lower().startswith(os.path.normpath(root).lower()):
        raise ValueError('非法路径')
    return p


def reveal_in_explorer(path):
    """打开所在文件夹并选中该文件（v2.3 右键「打开地图文件位置」）。"""
    p = os.path.normpath(path)
    try:
        # /select, 必须和路径连成一个参数，拆成两个时带空格的路径会失效
        subprocess.Popen(['explorer', '/select,' + p])
    except Exception:
        try:
            os.startfile(os.path.dirname(p))
        except Exception:
            pass


def shutdown_app():
    time.sleep(0.35)
    h = SERVER.get('httpd')
    if h is not None:
        try:
            h.shutdown()
        except Exception:
            pass
    os._exit(0)


# ---------------------------------------------------------------- 缩略图 / 渲染
def thumb_path_for(path, width=560):
    st = os.stat(path)
    key = hashlib.sha1(('%s|%d|%d|%d' % (path.lower(), st.st_mtime, st.st_size, width)).encode('utf-8')).hexdigest()
    return os.path.join(THUMBS, key + '.jpg')


# ---------------------------------------------------------------- 真实地形底图（v1.8）
# CNCMaps 的 RA2/YR 等轴测瓦片宽 60、高 30；-F 输出整张 fullmap，
# 其像素坐标与本程序 painter 的 ptCx/ptCy 投影在 S=60 时 1:1 对齐（已实证）。
BASEMAP_TILE = 60


def basemap_path_for(path, width):
    st = os.stat(path)
    key = hashlib.sha1(('%s|%d|%d|%d|fullmap' % (
        path.lower(), st.st_mtime, st.st_size, width)).encode('utf-8')).hexdigest()
    return os.path.join(BASEMAP_DIR, key + '.jpg')


def _render_fullmap(path, gdir):
    """调用 CNCMaps 输出 -F 全图（不带出生点标记 / 矿标记），返回 (Image, err)。"""
    r = find_renderer(gdir)
    if not r:
        return None, '游戏目录里没找到 CNCMaps 渲染器'
    exe, workdir = r
    mixdir = mix_dir_for(path)
    outdir = tempfile.mkdtemp(dir=TMP)
    # -o 显式输出名：部分合作任务 .map 会在 DetermineMapName() 抛异常，
    # 指定 -o 可绕过该查找（实测 rc=0 正常出图）。
    outbase = os.path.join(outdir, 'basemap')
    variants = [
        ['-Y', '-m', mixdir, '-i', path, '-d', outdir, '-o', outbase, '-p', '-F'],
        ['-Y', '-m', mixdir, '-i', path, '-d', outdir, '-o', outbase, '-j', '-F'],
        ['-Y', '-m', mixdir, '-i', path, '-d', outdir, '-p', '-F'],
        ['-y', '-m', mixdir, '-i', path, '-d', outdir, '-o', outbase, '-p', '-F'],
    ]
    last_err = ''
    try:
        for args in variants:
            try:
                proc = subprocess.run([exe] + args, cwd=workdir, capture_output=True,
                                      timeout=240, creationflags=CREATE_NO_WINDOW)
                out = (proc.stdout or b'').decode('utf-8', 'replace')
                errs = [ln.strip() for ln in out.splitlines() if '[Error]' in ln]
                if errs:
                    last_err = re.sub(r'[^\x20-\x7e]', '', errs[-1])[:160]
            except Exception as e:
                log('地形底图渲染异常 %s: %s' % (os.path.basename(path), e))
                continue
            fs = [os.path.join(outdir, f) for f in os.listdir(outdir)
                  if f.lower().endswith(('.png', '.jpg', '.jpeg'))]
            fs = [f for f in fs if os.path.getsize(f) > 1024]
            if fs:
                try:
                    im = Image.open(max(fs, key=os.path.getsize)).convert('RGB')
                    im.load()
                    return im, ''
                except Exception as e:
                    log('读取地形底图失败: %s' % e)
        return None, ('渲染器没有输出图片' + (('：' + last_err) if last_err else ''))
    finally:
        shutil.rmtree(outdir, ignore_errors=True)


def ensure_basemap(path, width=1800, fresh=False):
    """返回 (jpg 路径, meta)；缓存键含 mtime+size，保存地图后会自动重渲染。"""
    width = max(600, min(3200, int(width or 1800)))
    tgt = basemap_path_for(path, width)
    meta_p = tgt + '.json'
    if not fresh and os.path.isfile(tgt) and os.path.isfile(meta_p):
        m = load_json(meta_p, None)
        if m:
            return tgt, m
    with BASEMAP_SEM:
        if not fresh and os.path.isfile(tgt) and os.path.isfile(meta_p):
            m = load_json(meta_p, None)
            if m:
                return tgt, m
        im, err = _render_fullmap(path, game_dir())
        if im is None:
            return None, {'error': err}
        nw, nh = im.size
        if nw > width:
            im = im.resize((width, max(1, int(nh * width / nw))), Image.LANCZOS)
        iw, ih = im.size
        im.save(tgt, 'JPEG', quality=88, optimize=True)
        meta = {'nativeW': nw, 'nativeH': nh, 'imgW': iw, 'imgH': ih,
                'tile': BASEMAP_TILE, 'width': width,
                'scale': iw / float(nw)}
        save_json(meta_p, meta)
        log('真实地形底图已生成：%s %dx%d（原图 %dx%d）' %
            (os.path.basename(path), iw, ih, nw, nh))
        return tgt, meta

def _render_to_image(path, gdir):
    """调用 CNCMaps 渲染器，返回 PIL.Image 或 None"""
    r = find_renderer(gdir)
    if not r:
        return None
    exe, workdir = r
    mixdir = mix_dir_for(path)
    outdir = tempfile.mkdtemp(dir=TMP)
    variants = [
        ['-Y', '-m', mixdir, '-i', path, '-d', outdir, '-j', '--mark-start-pos', '-r'],
        ['-y', '-m', mixdir, '-i', path, '-d', outdir, '-j'],
        ['-Y', '-m', mixdir, '-i', path, '-d', outdir, '-j'],
    ]
    try:
        for args in variants:
            cmd = [exe] + args
            try:
                subprocess.run(cmd, cwd=workdir, capture_output=True, timeout=180,
                               creationflags=CREATE_NO_WINDOW)
            except Exception as e:
                log('渲染异常 %s: %s' % (os.path.basename(path), e))
                continue
            jpgs = [os.path.join(outdir, f) for f in os.listdir(outdir)
                    if f.lower().endswith(('.jpg', '.jpeg', '.png'))]
            jpgs = [j for j in jpgs if os.path.getsize(j) > 1024]
            if jpgs:
                try:
                    im = Image.open(max(jpgs, key=os.path.getsize))
                    im.load()
                    return im.convert('RGB')
                except Exception as e:
                    log('读取渲染结果失败: %s' % e)
        return None
    finally:
        shutil.rmtree(outdir, ignore_errors=True)


def _save_variants(im, path):
    """由大图生成 缩略图 + 详情图"""
    w, h = im.size
    for width, target in ((560, thumb_path_for(path, 560)), (1500, thumb_path_for(path, 1500))):
        if w <= width:
            out = im
        else:
            nh = max(1, int(h * width / w))
            out = im.resize((width, nh), Image.LANCZOS)
        out.save(target, 'JPEG', quality=86, optimize=True)
    return thumb_path_for(path, 560)


def ensure_image(path, width=560):
    """返回图片路径；优先用地图自带 jpg，其次用渲染结果，第三才现渲染"""
    tgt = thumb_path_for(path, width)
    if os.path.isfile(tgt) and os.path.getsize(tgt) > 512:
        return tgt
    base = os.path.splitext(path)[0]
    for ie in IMG_EXT:
        for cand in (base + ie, base + ie.upper()):
            if os.path.isfile(cand):
                try:
                    im = Image.open(cand)
                    im.load()
                    _save_variants(im.convert('RGB'), path)
                    return tgt
                except Exception:
                    break
    with RENDER_SEM:
        if os.path.isfile(tgt) and os.path.getsize(tgt) > 512:
            return tgt
        im = _render_to_image(path, game_dir())
        if im is None:
            return None
        _save_variants(im, path)
    return tgt if os.path.isfile(tgt) else None


WARM = {'running': False, 'done': 0, 'total': 0, 'fail': 0, 'current': '', 'stop': False}
WARM_LOCK = threading.Lock()
WARM_WORKERS = 3


def warm_worker(items):
    global WARM
    q = list(items)
    ql = threading.Lock()

    def take():
        with ql:
            return q.pop(0) if q else None

    def work():
        while not WARM.get('stop'):
            it = take()
            if it is None:
                return
            try:
                p = abs_of(it['id'])
                if ensure_image(p, 560):
                    with WARM_LOCK:
                        WARM['done'] += 1
                else:
                    with WARM_LOCK:
                        WARM['fail'] += 1
            except Exception:
                with WARM_LOCK:
                    WARM['fail'] += 1
            WARM['current'] = it['file']

    ts = [threading.Thread(target=work, daemon=True) for _ in range(WARM_WORKERS)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    with WARM_LOCK:
        WARM['running'] = False
        WARM['current'] = ''
    invalidate()
    log('批量生成预览完成: 成功 %d / 失败 %d' % (WARM['done'], WARM['fail']))


# ---------------------------------------------------------------- 回收站
def trash_manifest_path():
    return os.path.join(TRASH, 'manifest.json')


def trash_items():
    return load_json(trash_manifest_path(), [])


def move_to_trash(paths, reg_fn=None):
    man = trash_items()
    stamp = time.strftime('%Y%m%d-%H%M%S')
    moved = []
    for p in paths:
        if not os.path.isfile(p):
            continue
        uid = uuid.uuid4().hex[:10]
        box = os.path.join(TRASH, uid)
        os.makedirs(box, exist_ok=True)
        base = os.path.basename(p)
        dst = os.path.join(box, base)
        shutil.move(p, dst)
        entry = {'uid': uid, 'name': base, 'orig': p, 'stamp': stamp,
                 'size': os.path.getsize(dst)}
        if reg_fn:
            try:
                entry['reg'] = reg_fn(p) or []
            except Exception as e:
                log('记录注册片段失败: %s' % e)
                entry['reg'] = []
        # 同时带走同名图片
        stem = os.path.splitext(p)[0]
        for ie in IMG_EXT:
            for cand in (stem + ie, stem + ie.upper()):
                if os.path.isfile(cand):
                    try:
                        shutil.move(cand, os.path.join(box, os.path.basename(cand)))
                        entry.setdefault('extra', []).append(cand)
                    except Exception:
                        pass
                    break
        man.append(entry)
        moved.append(entry)
    save_json(trash_manifest_path(), man)
    return moved


def restore(uid):
    man = trash_items()
    for e in list(man):
        if e['uid'] != uid:
            continue
        box = os.path.join(TRASH, uid)
        src = os.path.join(box, e['name'])
        dst = e['orig']
        if os.path.exists(dst):
            stem, ext = os.path.splitext(dst)
            n = 2
            while os.path.exists('%s(还原%d)%s' % (stem, n, ext)):
                n += 1
            dst = '%s(还原%d)%s' % (stem, n, ext)
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        if os.path.isfile(src):
            shutil.move(src, dst)
        if dst == e['orig'] and e.get('reg'):
            try:
                modereg.restore_reg(game_dir(), DATA, e['reg'])
            except Exception as ex:
                log('恢复注册项失败: %s' % ex)
        for extra in e.get('extra', []):
            cand = os.path.join(box, os.path.basename(extra))
            if os.path.isfile(cand):
                try:
                    shutil.move(cand, os.path.join(os.path.dirname(dst), os.path.basename(extra)))
                except Exception:
                    pass
        shutil.rmtree(box, ignore_errors=True)
        man.remove(e)
        save_json(trash_manifest_path(), man)
        return True
    return False


def empty_trash():
    for uid in os.listdir(TRASH):
        p = os.path.join(TRASH, uid)
        if os.path.isdir(p):
            shutil.rmtree(p, ignore_errors=True)
    save_json(trash_manifest_path(), [])
    return True


# ---------------------------------------------------------------- 导入
def _extract_archive(src, dest, gdir):
    ext = os.path.splitext(src)[1].lower()
    if ext == '.zip':
        with zipfile.ZipFile(src) as z:
            for n in z.namelist():
                # 防路径穿越
                name = n.replace('\\', '/')
                if name.startswith('/') or '..' in name.split('/'):
                    continue
                target = os.path.join(dest, os.path.basename(name))
                if n.endswith('/'):
                    continue
                with z.open(n) as f, open(target, 'wb') as o:
                    shutil.copyfileobj(f, o)
        return True
    z7 = find_7z(gdir)
    if not z7:
        return False
    try:
        subprocess.run([z7, 'x', '-y', '-o' + dest, src], capture_output=True,
                       timeout=600, creationflags=CREATE_NO_WINDOW)
        # 7z 会保留目录结构，这里拍平
        flat = os.path.join(dest, '_flat')
        os.makedirs(flat, exist_ok=True)
        for cur, _dirs, files in os.walk(dest):
            if cur.startswith(flat):
                continue
            for f in files:
                try:
                    shutil.move(os.path.join(cur, f), os.path.join(flat, f))
                except Exception:
                    pass
        if os.path.isdir(flat):
            for f in os.listdir(flat):
                shutil.move(os.path.join(flat, f), os.path.join(dest, f))
            shutil.rmtree(flat, ignore_errors=True)
        return True
    except Exception as e:
        log('解压失败: %s' % e)
        return False


def _sha1(path, limit=64 * 1024 * 1024):
    h = hashlib.sha1()
    with open(path, 'rb') as f:
        while True:
            b = f.read(1024 * 1024)
            if not b:
                break
            h.update(b)
            if f.tell() > limit:
                break
    return h.hexdigest()


def import_file(src_path, orig_name, target_dir, mode='skip'):
    """把 src_path 放进 target_dir，重名按 mode 处理"""
    ext = os.path.splitext(orig_name)[1].lower()
    res = {'file': orig_name, 'status': 'ok', 'dst': ''}
    if ext not in MAP_EXT:
        res['status'] = 'skip-nonmap'
        return res
    os.makedirs(target_dir, exist_ok=True)
    base = os.path.basename(orig_name)
    dst = os.path.join(target_dir, base)
    if os.path.exists(dst):
        same = False
        try:
            same = _sha1(dst) == _sha1(src_path)
        except Exception:
            pass
        if same:
            res['status'] = 'skip-same'
            return res
        if mode == 'skip':
            res['status'] = 'skip-exists'
            return res
        if mode == 'overwrite':
            pass
        else:  # rename
            stem, e = os.path.splitext(base)
            n = 2
            while os.path.exists(os.path.join(target_dir, '%s(%d)%s' % (stem, n, e))):
                n += 1
            dst = os.path.join(target_dir, '%s(%d)%s' % (stem, n, e))
    shutil.copy2(src_path, dst)
    res['dst'] = dst
    res['status'] = 'ok'
    return res


# ---------------------------------------------------------------- HTTP
class Handler(BaseHTTPRequestHandler):
    server_version = 'RA2MapManager/1.0'

    def log_message(self, fmt, *args):
        pass

    # -------- helpers
    def send_json(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False).encode('utf-8')
        self.send_response(code)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        self.wfile.write(body)

    def send_file(self, path, ctype='application/octet-stream', cache=False):
        try:
            with open(path, 'rb') as f:
                data = f.read()
        except Exception:
            self.send_json({'error': 'not found'}, 404)
            return
        self.send_response(200)
        self.send_header('Content-Type', ctype)
        self.send_header('Content-Length', str(len(data)))
        self.send_header('Cache-Control', 'public, max-age=86400' if cache else 'no-store')
        self.end_headers()
        self.wfile.write(data)

    def send_basemap(self, path, meta):
        try:
            with open(path, 'rb') as f:
                data = f.read()
        except Exception:
            return self.send_json({'error': 'not found'}, 404)
        self.send_response(200)
        self.send_header('Content-Type', 'image/jpeg')
        self.send_header('Content-Length', str(len(data)))
        for k, h in (('nativeW', 'X-Base-Native-W'),
                    ('nativeH', 'X-Base-Native-H'),
                    ('imgW', 'X-Base-Img-W'), ('imgH', 'X-Base-Img-H'),
                    ('tile', 'X-Base-Tile'), ('scale', 'X-Base-Scale')):
            self.send_header(h, str(meta.get(k, '')))
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        self.wfile.write(data)

    def api_basemap(self, q):
        mid = (q.get('id') or [''])[0]
        if not mid:
            return self.send_json({'error': '缺少地图 id'}, 400)
        try:
            p = abs_of(mid)
        except Exception as e:
            return self.send_json({'error': str(e)}, 400)
        if not os.path.isfile(p):
            return self.send_json({'error': '文件不存在'}, 404)
        try:
            width = int((q.get('w') or ['1800'])[0])
        except Exception:
            width = 1800
        fresh = (q.get('fresh') or [''])[0] not in ('', '0', 'false')
        tgt, meta = ensure_basemap(p, width, fresh)
        if tgt is None:
            return self.send_json({'error': meta.get('error') or '渲染失败'}, 404)
        return self.send_basemap(tgt, meta)
    def body(self):
        n = int(self.headers.get('Content-Length') or 0)
        if not n:
            return b''
        buf = b''
        while len(buf) < n:
            chunk = self.rfile.read(min(1048576, n - len(buf)))
            if not chunk:
                break
            buf += chunk
        return buf

    def qs(self):
        return parse_qs(urlparse(self.path).query)

    # -------- routes
    def do_GET(self):
        try:
            QUIT['at'] = 0.0
            u = urlparse(self.path)
            path = u.path
            q = self.qs()
            if path in ('/', '/index.html'):
                return self.send_file(os.path.join(WEB, 'index.html'), 'text/html; charset=utf-8')
            if path.startswith('/static/'):
                fp = os.path.join(WEB, path[len('/static/'):])
                if os.path.isfile(fp):
                    ext = os.path.splitext(fp)[1].lower()
                    ct = {'.css': 'text/css', '.js': 'application/javascript',
                          '.png': 'image/png', '.svg': 'image/svg+xml'}.get(ext, 'text/plain')
                    return self.send_file(fp, ct + '; charset=utf-8', True)
            if path == '/api/state':
                return self.send_json(self.api_state())
            if path == '/api/ping':
                return self.send_json({'app': 'ra2map', 'version': VERSION,
                                       'port': SERVER.get('port', 0)})
            if path == '/api/quit':
                self.send_json({'ok': True})
                threading.Thread(target=shutdown_app, daemon=True).start()
                return
            if path == '/api/openfolder':
                mid = (q.get('id') or [''])[0]
                try:
                    p = abs_of(mid)
                except Exception as e:
                    return self.send_json({'error': str(e)}, 400)
                if os.path.isfile(p):
                    reveal_in_explorer(p)
                    return self.send_json({'ok': True, 'path': p})
                return self.send_json({'error': '文件不存在'}, 404)
            if path == '/api/platform/scan':
                return self.send_json(self.api_platform_scan(q))
            if path == '/api/platform/thumb':
                return self.api_platform_thumb(q)
            if path == '/api/maps':
                return self.send_json(self.api_maps(q))
            if path == '/api/thumb':
                return self.api_thumb(q, 560)
            if path == '/api/preview':
                return self.api_thumb(q, 1500)
            if path == '/api/meta':
                return self.api_meta(q)
            if path == '/api/edit':
                return self.send_json(api_edit_info(q))
            if path == '/api/edit/raw':
                return self.send_json(api_edit_raw(q))
            if path == '/api/editor':
                return self.send_json(api_editor_load(q))
            if path == '/api/editor/basemap':
                return self.api_basemap(q)
            if path == '/api/editor/catalog':
                return self.send_json(api_catalog())
            if path == '/api/editor/cell':
                return self.send_json(api_editor_cell(q))
            if path == '/api/help':
                return self.send_json(api_help(q))
            if path == '/api/help/field':
                return self.send_json(api_help_field(q))
            if path == '/api/help/section':
                return self.send_json(api_help_section(q))
            if path == '/api/pick-folder':
                return self.send_json(api_pick_folder(q))
            if path == '/api/fog/state':
                return self.send_json(api_fog_state(q))
            if path == '/api/ai/config':
                return self.send_json(ai_proxy.public_config(data_dir=DATA))
            if path == '/api/env':
                return self.send_json(api_env())
            if path == '/api/maps/headcheck':
                return self.send_json(api_maps_headcheck())
            if path == '/api/modes':
                return self.send_json(modes_state())
            if path == '/api/job':
                return self.send_json(dict(WARM))
            if path == '/api/trash':
                return self.send_json({'items': trash_items()})
            if path == '/api/renderall':
                return self.send_json(start_warm((q.get('folder') or [''])[0]))
            if path == '/api/renderone':
                return self.send_json(render_one(q))
            if path == '/api/reveal':
                return self.api_reveal(q)
            self.send_json({'error': 'not found'}, 404)
        except (BrokenPipeError, ConnectionAbortedError, ConnectionResetError):
            # 浏览器/窗口取消请求、探测超时导致的断连是正常现象，别刷 Traceback
            return
        except Exception as e:
            log(traceback.format_exc())
            self.send_json({'error': str(e)}, 500)

    def do_POST(self):
        try:
            QUIT['at'] = 0.0
            u = urlparse(self.path)
            q = self.qs()
            if u.path == '/api/bye':
                # 页面卸载（关窗口）时由 sendBeacon 触发；刷新会在延迟内被新请求取消
                self.send_json({'ok': True})
                schedule_quit()
                return
            if u.path == '/api/config':
                data = json.loads(self.body().decode('utf-8') or '{}')
                g = (data.get('gameDir') or '').strip().strip('"')
                if g:
                    g = os.path.normpath(g.replace('/', '\\'))
                if g and not os.path.isdir(g):
                    return self.send_json({'error': '目录不存在：' + g}, 400)
                CONFIG['gameDir'] = g
                CONFIG.setdefault('recent', [])
                CONFIG.setdefault('ignored', [])
                if g in CONFIG['ignored']:
                    CONFIG['ignored'].remove(g)
                if g and g not in CONFIG['recent']:
                    CONFIG['recent'].insert(0, g)
                    CONFIG['recent'] = CONFIG['recent'][:8]
                save_json(CONFIG_PATH, CONFIG)
                invalidate()
                _DETECTED['ts'] = 0
                return self.send_json(self.api_state())
            if u.path == '/api/delete':
                data = json.loads(self.body().decode('utf-8') or '{}')
                ids = data.get('ids') or []
                paths = []
                for i in ids:
                    try:
                        paths.append(abs_of(i))
                    except Exception:
                        pass
                # 删除地图时同步清理 MPMaps*.ini 注册项，片段随回收站保存，可还原
                frag_map = {}
                try:
                    frags = modereg.unregister(game_dir(), DATA, paths)
                    for f in frags:
                        k = modereg._norm(f.get('section') or f.get('multiValue') or '')
                        frag_map.setdefault(k, []).append(f)
                except Exception as e:
                    log('清理注册项失败: %s' % e)
                    frags = []
                root = maps_root()
                def _reg_for(p):
                    try:
                        return frag_map.get(modereg._norm(os.path.relpath(p, root).replace('\\', '/')), [])
                    except Exception:
                        return []
                moved = move_to_trash(paths, reg_fn=_reg_for)
                invalidate()
                return self.send_json({'deleted': len(moved), 'items': moved,
                                       'unregistered': len(frags)})
            if u.path == '/api/restore':
                data = json.loads(self.body().decode('utf-8') or '{}')
                ok = 0
                for uid in data.get('uids') or []:
                    if restore(uid):
                        ok += 1
                invalidate()
                return self.send_json({'restored': ok})
            if u.path == '/api/trash/empty':
                empty_trash()
                return self.send_json({'ok': True})
            if u.path == '/api/maps/move':
                return self.send_json(api_maps_move(json.loads(self.body().decode('utf-8') or '{}')))
            if u.path == '/api/maps/rename':
                return self.send_json(api_maps_rename(json.loads(self.body().decode('utf-8') or '{}')))
            if u.path == '/api/maps/fog':
                return self.send_json(api_maps_fog(json.loads(self.body().decode('utf-8') or '{}')))
            if u.path == '/api/maps/headcheck':
                return self.send_json(api_maps_headcheck())
            if u.path == '/api/maps/headfix':
                return self.send_json(api_maps_headfix())
            if u.path == '/api/ai/config':
                return self.send_json(ai_proxy.save_config(
                    DATA, json.loads(self.body().decode('utf-8') or '{}')))
            if u.path == '/api/ai/models':
                return self.send_json(ai_proxy.fetch_models(
                    DATA, json.loads(self.body().decode('utf-8') or '{}')))
            if u.path == '/api/ai/chat':
                return self.send_json(api_ai_chat(json.loads(self.body().decode('utf-8') or '{}')))
            if u.path == '/api/ai/plan':
                return self.send_json(api_ai_plan(json.loads(self.body().decode('utf-8') or '{}')))
            if u.path == '/api/ai/apply':
                return self.send_json(api_ai_apply(json.loads(self.body().decode('utf-8') or '{}')))
            if u.path == '/api/dirs/add':
                return self.send_json(api_dirs_add(json.loads(self.body().decode('utf-8') or '{}')))
            if u.path == '/api/dirs/remove':
                return self.send_json(api_dirs_remove(json.loads(self.body().decode('utf-8') or '{}')))
            if u.path == '/api/modes/save':
                return self.send_json(api_modes_save(json.loads(self.body().decode('utf-8') or '{}')))
            if u.path == '/api/modes/autoreg':
                return self.send_json(api_modes_autoreg(json.loads(self.body().decode('utf-8') or '{}')))
            if u.path == '/api/modes/clearcache':
                return self.send_json(api_modes_clearcache())
            if u.path == '/api/pack/export':
                return self.send_json(api_pack_export(json.loads(self.body().decode('utf-8') or '{}')))
            if u.path == '/api/pack/open':
                return self.send_json(api_pack_open(json.loads(self.body().decode('utf-8') or '{}')))
            if u.path == '/api/platform/import':
                return self.send_json(self.api_platform_import(
                    json.loads(self.body().decode('utf-8') or '{}')))
            if u.path == '/api/import':
                return self.api_import(q)
            if u.path == '/api/edit/save':
                data = json.loads(self.body().decode('utf-8') or '{}')
                return self.send_json(api_edit_save(data))
            if u.path == '/api/edit/trigger':
                data = json.loads(self.body().decode('utf-8') or '{}')
                return self.send_json(api_trigger_op(data))
            if u.path == '/api/reveal':
                data = json.loads(self.body().decode('utf-8') or '{}')
                return self.api_reveal({'id': [data.get('id', '')]})
            if u.path == '/api/editor/save':
                data = json.loads(self.body().decode('utf-8') or '{}')
                return self.send_json(api_editor_save(data))
            if u.path == '/api/editor/new':
                data = json.loads(self.body().decode('utf-8') or '{}')
                return self.send_json(api_new_map(data))
            self.send_json({'error': 'not found'}, 404)
        except (BrokenPipeError, ConnectionAbortedError, ConnectionResetError):
            # 浏览器/窗口取消请求、探测超时导致的断连是正常现象，别刷 Traceback
            return
        except Exception as e:
            log(traceback.format_exc())
            self.send_json({'error': str(e)}, 500)

    # -------- api impl
    def api_state(self):
        g = game_dir()
        ignored = [os.path.normcase(x) for x in CONFIG.get('ignored', [])]
        det = [d for d in detect_game_dirs()
               if os.path.normcase(d) not in ignored]
        recent = [d for d in CONFIG.get('recent', [])
                  if os.path.normcase(d) not in ignored]
        if not g and det:
            CONFIG['gameDir'] = det[0]
            save_json(CONFIG_PATH, CONFIG)
            g = det[0]
        maps = get_maps() if g else []
        folders = {}
        for m in maps:
            folders.setdefault(m['folder'], 0)
            folders[m['folder']] += 1
        r = find_renderer(g)
        return {
            'gameDir': g,
            'candidates': det,
            'recent': recent,
            'mapsRoot': maps_root(),
            'total': len(maps),
            'withThumb': sum(1 for m in maps if m['hasThumb']),
            'folders': [{'path': k, 'label': FOLDER_LABEL.get(k.split('/')[-1].lower(), k.split('/')[-1]),
                         'count': v} for k, v in sorted(folders.items())],
            'renderer': os.path.basename(r[0]) if r else '',
            'rendererPath': r[0] if r else '',
            'trashCount': len(trash_items()),
            'job': dict(WARM),
        }

    # -------- v2.4 跨平台地图导入
    def api_platform_scan(self, q):
        root = (q.get('root') or [''])[0].strip().strip('"') or PI.guess_root()
        r = PI.scan(root)
        r['defaultRoot'] = PI.guess_root()
        mr = maps_root()
        r['mapsRoot'] = mr
        targets = [{'path': 'Custom', 'label': '自定义地图 (Custom)'}]
        if mr:
            for d in sorted(os.listdir(mr)):
                if d.lower() == 'custom' or not os.path.isdir(os.path.join(mr, d)):
                    continue
                targets.append({'path': d, 'label': d})
        r['targets'] = targets
        return r

    def api_platform_thumb(self, q):
        b = PI.thumb((q.get('id') or [''])[0])
        if not b:
            return self.send_json({'error': 'no preview'}, 404)
        self.send_response(200)
        self.send_header('Content-Type', 'image/jpeg')
        self.send_header('Content-Length', str(len(b)))
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        self.wfile.write(b)

    def api_platform_import(self, data):
        ids = data.get('ids') or []
        if not ids:
            return {'error': '没有勾选任何地图'}
        mr = maps_root()
        if not mr:
            return {'error': '未设置游戏目录：请先在左上角选到红警安装目录'}
        target = (data.get('target') or 'Custom').strip()
        tgt = mr if target in ('__root__', '') else os.path.join(mr, target)
        r = PI.import_items(ids, tgt, data.get('conflict') or 'skip', BACKUP_DIR)
        if r.get('imported'):
            invalidate()
            log('平台导入：%d 张 → %s（跳过 %d / 加密 %d / 失败 %d）' %
                (r['imported'], tgt, r['skipped'], r['locked'], r['failed']))
        return r

    def api_maps(self, q):
        maps = get_maps()
        folder = (q.get('folder') or [''])[0]
        kw = (q.get('q') or [''])[0].strip().lower()
        sort = (q.get('sort') or ['folder'])[0]
        only = (q.get('only') or [''])[0]
        # v2.4：名称特征改成多选，nf 可重复或逗号分隔；默认「同时满足」，可切「满足任一」
        nfl = []
        for v in (q.get('nf') or []):
            nfl += [x.strip() for x in v.split(',') if x.strip()]
        nfl = list(dict.fromkeys(nfl))[:8]
        nflogic = (q.get('nflogic') or ['and'])[0]
        # name = 游戏大厅显示名，file = 磁盘文件名。旧 API 默认仍是 name（保持兼容），
        # 前端 v2.4.1 起默认传 file —— 卡片显示什么就按什么筛，所见即所得。
        nfbase = (q.get('nfbase') or ['name'])[0]
        out = []
        for m in maps:
            if folder and folder != 'ALL':
                if folder == 'ROOT':
                    if '/' in m['folder']:
                        continue
                elif not (m['folder'] == folder or m['folder'].startswith(folder + '/')):
                    continue
            if only == 'nothumb' and m['hasThumb']:
                continue
            if only == 'hasthumb' and not m['hasThumb']:
                continue
            if nfl:
                s = nf_value(m, nfbase)
                hit = [name_match(s, code) for code in nfl]
                if (nflogic == 'or' and not any(hit)) or (nflogic != 'or' and not all(hit)):
                    continue
            if kw:
                blob = (m['file'] + ' ' + m['name'] + ' ' + m['folder'] + ' ' + m['theater']).lower()
                if kw not in blob:
                    continue
            out.append(m)
        if sort == 'name':
            out.sort(key=lambda x: x['name'].lower())
        elif sort == 'size':
            out.sort(key=lambda x: -x['size'])
        elif sort == 'time':
            out.sort(key=lambda x: -x['mtime'])
        elif sort == 'players':
            out.sort(key=lambda x: (-(int(x['players']) if str(x['players']).isdigit() else 0), x['file'].lower()))
        return {'items': out, 'count': len(out)}

    def api_thumb(self, q, width):
        mid = (q.get('id') or [''])[0]
        try:
            p = abs_of(mid)
        except Exception as e:
            return self.send_json({'error': str(e)}, 400)
        if not os.path.isfile(p):
            return self.send_json({'error': 'missing'}, 404)
        img = ensure_image(p, width)
        if not img:
            return self.send_json({'error': 'no preview'}, 404)
        self.send_file(img, 'image/jpeg', True)

    def api_meta(self, q):
        mid = (q.get('id') or [''])[0]
        p = abs_of(mid)
        m = parse_map(p)
        st = os.stat(p)
        w, h = parse_size(m.get('size') or '')
        info = dict(m)
        info.update({'id': mid, 'path': p, 'sizeBytes': st.st_size,
                     'mtime': int(st.st_mtime), 'width': w, 'height': h,
                     'sha1': _sha1(p)})
        return self.send_json(info)

    def api_reveal(self, q):
        mid = (q.get('id') or [''])[0]
        try:
            p = abs_of(mid)
        except Exception as e:
            return self.send_json({'error': str(e)}, 400)
        if os.path.isfile(p):
            subprocess.Popen(['explorer', '/select,', os.path.normpath(p)])
        return self.send_json({'ok': True})

    def api_import(self, q):
        name = unquote((q.get('name') or ['map.yrm'])[0])
        target = (q.get('target') or ['__auto__'])[0]
        mode = (q.get('mode') or ['skip'])[0]
        data = self.body()
        if not data:
            return self.send_json({'error': '空文件'}, 400)
        pack = modereg.peek_pack(data)
        if pack:
            r = modereg.install_pack(
                game_dir(), DATA, data, mode=mode,
                apply_modes=(q.get('modes') or ['1'])[0] != '0',
                apply_reg=(q.get('reg') or ['1'])[0] != '0')
            log('安装地图包：%s' % json.dumps(pack, ensure_ascii=False))
            invalidate()
            return self.send_json({'pack': pack, 'install': r,
                                   'imported': r.get('installed', 0),
                                   'skipped': r.get('skipped', 0),
                                   'registered': r.get('regAdded', 0),
                                   'modesAdded': r.get('modesAdded', 0),
                                   'results': [], 'target': ''})
        tmpd = tempfile.mkdtemp(dir=TMP)
        try:
            safe = os.path.basename(name) or 'upload.bin'
            src = os.path.join(tmpd, safe)
            with open(src, 'wb') as f:
                f.write(data)
            ext = os.path.splitext(safe)[1].lower()
            root = maps_root()
            if not root:
                return self.send_json({'error': '未设置游戏目录'}, 400)
            tgt_dir = os.path.join(root, 'Custom') if target in ('__auto__', '', 'Custom') else os.path.join(root, target)

            results = []
            if ext in ARCHIVE_EXT:
                ext_dir = os.path.join(tmpd, 'x')
                os.makedirs(ext_dir, exist_ok=True)
                if not _extract_archive(src, ext_dir, game_dir()):
                    return self.send_json({'error': '解压失败（缺 7z 或包损坏）'}, 400)
                names = [f for f in os.listdir(ext_dir)]
                # 先导入地图，再补图片
                mapfiles = [f for f in names if os.path.splitext(f)[1].lower() in MAP_EXT]
                imgfiles = [f for f in names if os.path.splitext(f)[1].lower() in IMG_EXT]
                for f in mapfiles:
                    results.append(import_file(os.path.join(ext_dir, f), f, tgt_dir, mode))
                for f in imgfiles:
                    stem = os.path.splitext(f)[0].lower()
                    if any(os.path.splitext(r['file'])[0].lower() == stem for r in results if r['status'] == 'ok'):
                        try:
                            shutil.copy2(os.path.join(ext_dir, f), os.path.join(tgt_dir, f))
                        except Exception:
                            pass
            else:
                results.append(import_file(src, safe, tgt_dir, mode))
            ok = [r for r in results if r['status'] == 'ok']
            reg = {'registered': 0, 'basicFixed': 0}
            if (q.get('autoreg') or ['0'])[0] == '1' and ok:
                try:
                    reg = modereg.autoreg(game_dir(), DATA,
                                          [r['dst'] for r in ok],
                                          target=(q.get('regtarget') or ['auto'])[0],
                                          mode=(q.get('regmode') or [modereg.DEFAULT_MODE])[0],
                                          fix_basic=(q.get('fixbasic') or ['1'])[0] == '1')
                except Exception as e:
                    log('自动补注册失败: %s' % e)
            invalidate()
            _sk = sum(1 for r in results if r['status'].startswith('skip'))
            _fail = sum(1 for r in results if r['status'] != 'ok' and not r['status'].startswith('skip'))
            log('导入 [%s] → %s：导入%d 跳过%d 失败%d 补注册%d' % (
                safe, tgt_dir, len(ok), _sk, _fail, reg.get('registered', 0)))
            return self.send_json({
                'target': tgt_dir, 'results': results,
                'imported': len(ok),
                'skipped': _sk,
                'failed': _fail,
                'registered': reg.get('registered', 0),
                'basicFixed': reg.get('basicFixed', 0),
            })
        finally:
            shutil.rmtree(tmpd, ignore_errors=True)


# ---------------------------------------------------------------- 游戏模式 / 地图包
def api_modes_save(data):
    try:
        r = modereg.save_modes(game_dir(), DATA, data)
        invalidate()
        log('保存游戏模式：变更 %s 处' % r.get('changedEntries', 0))
        return r
    except Exception as e:
        log(traceback.format_exc())
        return {'error': str(e)}


def api_modes_autoreg(data):
    try:
        r = modereg.autoreg(game_dir(), DATA,
                            data.get('ids') or ['*'],
                            target=data.get('target') or 'auto',
                            mode=data.get('mode') or modereg.DEFAULT_MODE,
                            fix_basic=bool(data.get('fixBasic', True)))
        invalidate()
        log('补注册地图 %d 张（补写 GameMode %d 张）' % (
            r.get('registered', 0), r.get('basicFixed', 0)))
        return r
    except Exception as e:
        log(traceback.format_exc())
        return {'error': str(e)}



# ---------------------------------------------------------------- 帮助 / 重命名 / 游戏目录管理
def api_help(q):
    term = (q.get('q') or [''])[0].strip()
    if term:
        return {'ok': True, 'query': term, 'results': INI.help_search(term)}
    return dict(INI.help_guide(), ok=True)

def api_help_field(q):
    sec = (q.get('section') or [''])[0]
    key = (q.get('key') or [''])[0]
    val = (q.get('value') or [''])[0]
    return dict(INI.field_help(sec, key, val), ok=True)

def api_help_section(q):
    name = (q.get('name') or [''])[0]
    return dict(INI.section_help(name), ok=True)

def api_pick_folder(q):
    # probe=1：只回报当前目录选择实现，不弹窗（给部署版冒烟测试/排障用）
    if (q.get('probe') or [''])[0]:
        return {'ok': True, 'probe': True, 'backend': native_win.pick_backend(),
                'version': VERSION}
    initial = (q.get('initial') or [''])[0] or game_dir()
    try:
        path = native_win.choose_folder(initial)
    except Exception as e:
        log('目录选择失败: %s' % e)
        return {'ok': False, 'error': '打开目录窗口失败：%s' % e}
    if not path:
        return {'ok': True, 'cancelled': True}
    return {'ok': True, 'cancelled': False, 'path': os.path.normpath(path)}

def _norm_dir(p):
    return os.path.normpath(str(p or '').strip().strip('"').replace('/', '\\'))

def api_dirs_add(data):
    p = _norm_dir(data.get('path'))
    if not p or p in ('.', '\\'):
        return {'ok': False, 'error': '路径为空'}
    if not os.path.isdir(p):
        return {'ok': False, 'error': '目录不存在：' + p}
    CONFIG.setdefault('recent', [])
    CONFIG.setdefault('ignored', [])
    if p in CONFIG['ignored']:
        CONFIG['ignored'].remove(p)
    if p not in CONFIG['recent']:
        CONFIG['recent'].insert(0, p)
        CONFIG['recent'] = CONFIG['recent'][:12]
    save_json(CONFIG_PATH, CONFIG)
    _DETECTED['ts'] = 0
    log('添加游戏目录: ' + p)
    return {'ok': True, 'path': p, 'recent': CONFIG['recent']}

def api_dirs_remove(data):
    p = _norm_dir(data.get('path'))
    if not p:
        return {'ok': False, 'error': '路径为空'}
    CONFIG.setdefault('recent', [])
    CONFIG.setdefault('ignored', [])
    if p in CONFIG['recent']:
        CONFIG['recent'].remove(p)
    if p not in CONFIG['ignored']:
        CONFIG['ignored'].append(p)
    save_json(CONFIG_PATH, CONFIG)
    _DETECTED['ts'] = 0
    active = os.path.normcase(game_dir()) == os.path.normcase(p)
    log('移除游戏目录(仅列表): ' + p)
    return {'ok': True, 'path': p, 'stillActive': active,
            'recent': CONFIG['recent'], 'ignored': CONFIG['ignored']}

def api_maps_rename(data):
    mid = data.get('id') or ''
    name = data.get('name') or ''
    if not mid or not str(name).strip():
        return {'ok': False, 'error': '缺少地图或新名称'}
    g = game_dir()
    if not g:
        return {'ok': False, 'error': '未设置游戏目录'}
    opts = {
        'basic': bool(data.get('basic', True)),
        'reg': bool(data.get('reg', True)),
        'description': bool(data.get('description', True)),
        'conflict': data.get('conflict') or 'rename',
    }
    try:
        r = modereg.rename_map(g, DATA, mid, name, opts)
    except Exception as e:
        log(traceback.format_exc())
        return {'ok': False, 'error': str(e)}
    if r.get('ok'):
        invalidate()
        with META_LOCK:
            META_CACHE.clear()
        log('重命名地图 [%s] → [%s]，注册表 %d 个' % (
            r.get('oldFile'), r.get('file'), r.get('regUpdated', 0)))
    return r


# ---------------------------------------------------------------- v1.7 取消迷雾
def api_fog_state(q):
    mid = (q.get('id') or [''])[0]
    p = abs_of(mid)
    if not p or not os.path.isfile(p):
        return {'ok': False, 'error': '找不到这张地图'}
    return mapfog.state(p)


def api_maps_fog(data):
    """右键「取消迷雾 / 恢复迷雾」，支持勾选多张批量。"""
    ids = data.get('ids') or []
    action = (data.get('action') or 'on').lower()
    if action not in ('on', 'off'):
        return {'ok': False, 'error': '未知操作'}
    if not ids:
        return {'ok': False, 'error': '没有选中地图'}
    if not game_dir():
        return {'ok': False, 'error': '未设置游戏目录'}
    opts = {
        'reveal': bool(data.get('reveal', True)),
        'freeRadar': bool(data.get('freeRadar', True)),
        'fogOfWar': bool(data.get('fogOfWar', True)),
        'delay': data.get('delay', 0),
        'force': bool(data.get('force', False)),
    }
    results, failed = [], []
    for mid in ids:
        p = abs_of(mid)
        if not p or not os.path.isfile(p):
            failed.append({'id': mid, 'error': '文件不存在'})
            continue
        try:
            if action == 'on':
                r = mapfog.apply(p, opts, BACKUP_DIR, DATA, mid)
            else:
                r = mapfog.remove(p, BACKUP_DIR, DATA, mid)
        except Exception as e:
            log('迷雾处理失败 [%s]: %s' % (mid, traceback.format_exc()))
            r = {'ok': False, 'error': str(e)}
        r['id'] = mid
        results.append(r)
        if not r.get('ok'):
            failed.append({'id': mid, 'error': r.get('error') or '未知错误'})
    changed = sum(1 for r in results if r.get('changed'))
    if changed:
        invalidate()
        with META_LOCK:
            META_CACHE.clear()
        log('%s迷雾：%d 张处理，%d 张改动，%d 张失败' %
            ('取消' if action == 'on' else '恢复',
             len(results), changed, len(failed)))
    return {'ok': len(failed) == 0, 'action': action, 'changed': changed,
            'total': len(results), 'failed': failed, 'results': results}


# ---------------------------------------------------------------- v1.7 AI 助手
def api_ai_plan(data):
    """只生成方案，不写地图。用户点「应用」才会调用 api_ai_apply。"""
    mid = data.get('id') or ''
    p = abs_of(mid) if mid else ''
    try:
        r = ai_proxy.plan(DATA, data.get('question'), p or None,
                          data.get('context') or {}, data.get('mode') or 'edit',
                          prefer=data.get('prefer'), module=data.get('module') or '')
    except Exception as e:
        log('AI 方案失败: %s' % traceback.format_exc())
        return {'ok': False, 'error': str(e)}
    if r.get('ok'):
        log('AI 生成方案：%d 个操作 / %.1fs / %s' %
            (len(r.get('ops') or []), float(r.get('elapsed') or 0),
             r.get('model') or ''))
    return r


def api_ai_chat(data):
    """AI 助手通用问答：任意模块都能调用，按模块切换提示词。"""
    mid = data.get('id') or ''
    try:
        p = abs_of(mid) if mid else ''
        r = ai_proxy.ask(DATA, data.get('question'),
                         module=data.get('module') or 'general',
                         context=data.get('context') or {},
                         map_path=p or None, prefer=data.get('prefer'))
        if r.get('ok'):
            log('AI 问答：模块 %s / 模型 %s / 切换 %d 次' %
                (data.get('module') or 'general', r.get('model') or '',
                 len(r.get('attempts') or [])))
        return r
    except Exception as e:
        log('AI 问答失败: %s' % traceback.format_exc())
        return {'ok': False, 'error': str(e)}


def api_ai_apply(data):
    mid = data.get('id') or ''
    p = abs_of(mid)
    if not p or not os.path.isfile(p):
        return {'ok': False, 'error': '找不到这张地图'}
    try:
        r = ai_proxy.apply_plan(p, data.get('ops') or [], DATA, BACKUP_DIR, mid)
    except Exception as e:
        log('AI 应用失败: %s' % traceback.format_exc())
        return {'ok': False, 'error': str(e)}
    if r.get('ok'):
        invalidate()
        with META_LOCK:
            META_CACHE.clear()
        log('AI 方案已应用：%s' % '、'.join(r.get('done') or []))
        try:
            render_one({'id': [mid]})
        except Exception:
            pass
    return r


# ---------------------------------------------------------------- v1.7 环境自检
# ------------------------------------------------------------ 坏字节体检（v2.1 头部，v2.2 扩展头部+尾部）
BAD_HEAD = b'\xff\xfe[\r'   # UTF-16LE BOM + 孤立的 '['：.NET 客户端会把整份文件误读成乱码
TAIL_JUNK = re.compile(rb'\[\r\n\Z|\[\n\Z|\[\r\Z')   # 尾部孤 '[' 行：Rampastring IniFile 解析时抛异常，整张图被拒


def tail_junk_start(raw):
    """返回尾部孤 '[' 行的起始偏移；无则 None。"""
    m = TAIL_JUNK.search(raw)
    if not m:
        return None
    start = m.start()
    # '[' 必须位于行首：前一个字节是换行，或 '[' 就是文件第一个字节
    if start > 0 and raw[start - 1] not in (0x0a, 0x0d):
        return None
    return start


def map_bad_kind(raw):
    """检查一份地图字节：返回坏点列表，元素为 ('head', 偏移) 或 ('tail', 偏移)。"""
    bad = []
    if raw.startswith(BAD_HEAD):
        bad.append(('head', len(BAD_HEAD)))
    s = tail_junk_start(raw)
    if s is not None:
        bad.append(('tail', s))
    return bad


def scan_bad_heads():
    """扫 Maps 下所有地图，返回 [(绝对路径, 相对id, 坏点列表)]，头尾都查。"""
    root = maps_root()
    out = []
    if not root:
        return out
    for cur, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if not d.startswith('.') and d.lower() != 'cp']
        for f in files:
            if os.path.splitext(f)[1].lower() not in MAP_EXT:
                continue
            p = os.path.join(cur, f)
            try:
                with open(p, 'rb') as fh:
                    raw = fh.read()
            except OSError:
                continue
            bad = map_bad_kind(raw)
            if bad:
                out.append((p, os.path.relpath(p, root).replace('\\', '/'), bad))
    return out


def api_maps_headcheck():
    """地图文件头尾体检：多少张、几张带坏字节。"""
    bad = scan_bad_heads()
    return {'ok': True, 'total': len(get_maps()), 'bad': [b[1] for b in bad]}


def api_maps_headfix():
    """备份到 地图备份\\坏字节修复-<时间> 后同时清理两头，其余一字节不动。"""
    items = scan_bad_heads()
    if not items:
        return {'ok': True, 'fixed': 0, 'backup': '', 'left': 0}
    bdir = os.path.join(BACKUP_DIR, '坏字节修复-' + time.strftime('%Y%m%d-%H%M%S'))
    fixed = 0
    for p, rel, _bad in items:
        try:
            with open(p, 'rb') as fh:
                raw = fh.read()
        except OSError:
            continue
        cur_bad = map_bad_kind(raw)
        if not cur_bad:
            continue     # 扫描到修复之间被别的程序改过，跳过不动
        new = raw
        if new.startswith(BAD_HEAD):
            new = new[len(BAD_HEAD):]
        s = tail_junk_start(new)
        if s is not None:
            new = new[:s]
        if new == raw:
            continue
        dst = os.path.join(bdir, rel.replace('/', os.sep))
        os.makedirs(os.path.dirname(dst) or bdir, exist_ok=True)
        shutil.copy2(p, dst)
        tmp = p + '.bytefix'
        with open(tmp, 'wb') as f:
            f.write(new)
        os.replace(tmp, p)
        fixed += 1
    invalidate()
    if fixed:
        log('地图坏字节修复：%d/%d 张，备份: %s' % (fixed, len(items), bdir))
    return {'ok': True, 'fixed': fixed, 'backup': bdir if fixed else '',
            'left': len(items) - fixed}


def api_env():
    """给「发给别人双击能不能用」一个可核对的实证结论。"""
    def reg_value(hive, path, name):
        try:
            import winreg
            k = winreg.OpenKey(hive, path)
            return winreg.QueryValueEx(k, name)[0]
        except Exception:
            return ''

    def webview2():
        try:
            import winreg
            for hv in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
                for pre in (r'SOFTWARE\WOW6432Node', r'SOFTWARE'):
                    p = pre + r'\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}'
                    v = reg_value(hv, p, 'pv')
                    if v:
                        return str(v)
        except Exception:
            pass
        return ''

    def dotnet():
        try:
            import winreg
            rel = reg_value(winreg.HKEY_LOCAL_MACHINE,
                            r'SOFTWARE\Microsoft\NET Framework Setup\NDP\v4\Full',
                            'Release')
            return ('4.8+' if int(rel) >= 528040 else '4.x') if rel else ''
        except Exception:
            return ''

    wv = webview2()
    dn = dotnet()
    g = game_dir()
    bad_heads = len(scan_bad_heads()) if g else 0
    rr = find_renderer(g) if g else None
    renderer = rr[0] if rr else ''
    writable = True
    try:
        t = os.path.join(DATA, '.写入测试.tmp')
        with open(t, 'wb') as f:
            f.write(b'ok')
        os.remove(t)
    except Exception:
        writable = False
    tips = []
    if not wv:
        tips.append('未检测到 WebView2 运行库：窗口会自动回退到 Edge 应用模式，功能仍可用；'
                    '如需原生窗口，可安装微软「WebView2 Runtime」（Win10/11 一般已预装）。')
    if not g:
        tips.append('还没找到游戏目录：点左上角路径按钮「浏览添加」，或让程序自动扫描。'
                    '没有游戏也能导入/编辑属性/重命名，只是没有预览图。')
    if g and not renderer:
        tips.append('游戏目录里没找到 CNCMaps 渲染器：预览图不可用，其余功能不受影响。'
                    '渲染器随游戏本体提供，不是本程序需要额外安装的组件。')
    if not writable:
        tips.append('数据目录不可写：请把 exe 放到有写入权限的文件夹（不要放系统目录）。')
    if bad_heads:
        tips.append('%d 张地图文件头/尾带坏字节（头部 UTF-16 BOM 会把整份文件读成乱码、'
                    '尾部孤 [ 行会让客户端解析崩溃拒载）：点「一键修复」即可同时清理两头，'
                    '原文件先备份到 地图备份。' % bad_heads)
    return {
        'ok': True, 'version': VERSION, 'frozen': FROZEN,
        'python': sys.version.split()[0],
        'webview2': wv, 'dotnet': dn,
        'nativeWindow': native_win.available(),
        'folderPicker': native_win.pick_backend(),
        'gameDir': g, 'renderer': os.path.basename(renderer) if renderer else '',
        'dataDir': DATA, 'writable': writable,
        'port': SERVER.get('port', 0),
        'maps': len(get_maps()) if g else 0,
        'badHeads': bad_heads,
        'standalone': FROZEN,
        'tips': tips,
        'verdict': ('单文件 EXE 已自带 Python 运行时与全部依赖，双击即可使用，'
                    '不需要安装 Python / pip / VC++ 运行库 / .NET。'),
    }


def api_maps_move(data):
    """右键批量移动：改游戏模式归属，或物理移动到别的目录"""
    ids = data.get('ids') or []
    if not ids:
        return {'ok': False, 'error': '没有选中地图'}
    kind = data.get('kind') or 'mode'
    try:
        if kind == 'mode':
            r = modereg.set_map_modes(
                game_dir(), DATA, ids, data.get('modes') or [],
                op=data.get('op') or 'replace',
                target=data.get('target') or 'auto',
                fix_basic=bool(data.get('fixBasic', True)))
        elif kind == 'folder':
            r = modereg.move_maps(game_dir(), DATA, ids,
                                  data.get('folder') or '',
                                  conflict=data.get('conflict') or 'rename')
        else:
            return {'ok': False, 'error': '未知移动类型: %s' % kind}
    except Exception as e:
        log(traceback.format_exc())
        return {'ok': False, 'error': str(e)}
    if r.get('ok'):
        invalidate()
        dest = ','.join(data.get('modes') or []) if kind == 'mode' else data.get('folder')
        log('移动地图 %d 张 [%s] → %s' % (r.get('changed', 0), kind, dest))
    return r


def api_modes_clearcache():
    p = os.path.join(game_dir(), 'Client', 'custom_map_cache')
    if os.path.isfile(p):
        try:
            modereg._backup(p, os.path.join(DATA, '配置备份'))
            os.remove(p)
            log('已清理第三方地图缓存（下次启动游戏会重建）')
            return {'ok': True, 'cleared': True}
        except Exception as e:
            return {'error': str(e)}
    return {'ok': True, 'cleared': False}


def api_pack_export(data):
    out_dir = os.path.join(DATA, '导出')
    os.makedirs(out_dir, exist_ok=True)
    name = re.sub(r'[\\/:*?"<>|]', '_', data.get('name') or '') or (
        '红警地图包_' + time.strftime('%Y%m%d_%H%M%S'))
    if not name.lower().endswith('.zip'):
        name += '.zip'
    out = os.path.join(out_dir, name)
    try:
        r = modereg.build_pack(game_dir(), DATA, data.get('ids') or ['*'], {
            'includeModes': bool(data.get('includeModes', True)),
            'includeReg': bool(data.get('includeReg', True)),
        }, out)
        r['dir'] = out_dir
        log('导出地图包 [%s]：%d 张地图 / %d 条注册 / %d 个模式' % (
            name, r.get('maps', 0), r.get('regEntries', 0), r.get('modes', 0)))
        return r
    except Exception as e:
        log(traceback.format_exc())
        return {'error': str(e)}


def api_pack_open(data):
    p = data.get('path') or os.path.join(DATA, '导出')
    try:
        if os.path.isfile(p):
            subprocess.Popen(['explorer', '/select,', os.path.normpath(p)])
        else:
            subprocess.Popen(['explorer', os.path.normpath(p)])
        return {'ok': True}
    except Exception as e:
        return {'error': str(e)}


# ---------------------------------------------------------------- 地图属性编辑
BACKUP_DIR = os.path.join(DATA, '地图备份')


# 属性行 → 地图位置：地图上的可见位置只有几类，其余参数没有固定位置
_LOC_ALL = {'lighting': '光照参数作用于整张地图',
            'specialflags': '全局开关，影响整张地图'}


def _edit_loc(sec_low, key, locs):
    """返回 {kind: point|rect|all, x, y, rect, label}；无固定位置返回 None"""
    if not locs:
        return None
    kl = (key or '').strip()
    klow = kl.lower()
    if sec_low == 'waypoints' and kl.isdigit():
        v = (locs.get('waypoints') or {}).get(kl)
        if not v:
            return None
        n = int(kl)
        label = ('出生点 %d（玩家 %d）' % (n, n + 1)) if n <= 7 else ('路径点 %d' % n)
        return {'kind': 'point', 'x': v[0], 'y': v[1], 'label': label}
    if sec_low == 'map' and klow in ('size', 'localsize', 'localrect'):
        r = (locs.get('rects') or {}).get(klow)
        if not r:
            return None
        if klow == 'size':
            rect = [0, 0, max(1, r[2] - 1), max(1, r[3] - 1)]
            label = '地图边界 %d × %d' % (r[2], r[3])
        else:
            rect = [r[0], r[1], r[0] + max(1, r[2]) - 1, r[1] + max(1, r[3]) - 1]
            label = '可玩区域 / 局部尺寸'
        return {'kind': 'rect', 'rect': rect,
                'x': (rect[0] + rect[2]) // 2, 'y': (rect[1] + rect[3]) // 2,
                'label': label}
    if sec_low in ('structures', 'units', 'infantry', 'aircraft'):
        o = (locs.get('objects') or {}).get(sec_low, {}).get(kl.upper())
        if not o:
            return None
        kind = mapedit._OBJ_LABEL.get(sec_low, '对象')
        return {'kind': 'point', 'x': o[0], 'y': o[1],
                'label': '%s %s' % (kind, o[2] or '')}
    if sec_low == 'terrain':
        o = (locs.get('terrain') or {}).get(kl.upper())
        if not o:
            return None
        return {'kind': 'point', 'x': o[0], 'y': o[1],
                'label': '地形物 %s' % (o[2] or '')}
    if sec_low in _LOC_ALL:
        return {'kind': 'all', 'label': _LOC_ALL[sec_low]}
    return None


DEFAULT_HOUSES = ['Americans', 'Alliance', 'French', 'Germans', 'British', 'Africans',
                  'Arabs', 'Confederation', 'Russians', 'YuriCountry', 'GDI', 'Nod',
                  'Neutral', 'Special']


def _sec_rows(m, name):
    s = m.find(name)
    return m.keys_of(s) if s else []


def map_trigger_ctx(m):
    """[Actions] 下拉框的候选值：所属方 / 作战小队 / 触发事件 / 路径点"""
    houses = []
    for e in _sec_rows(m, 'houses'):
        try:
            idx = int((e.get('k') or '').strip())
        except Exception:
            continue
        c = (e.get('v') or '').strip()
        cn = INI.HOUSE_LABELS.get(c.lower(), c)
        houses.append({'v': str(idx), 'label': ('%s %s' % (cn, c)) if cn != c else c})
    if not houses:
        for i, c in enumerate(DEFAULT_HOUSES):
            cn = INI.HOUSE_LABELS.get(c.lower(), c)
            houses.append({'v': str(i), 'label': ('%s %s' % (cn, c)) if cn != c else c})
    # 4475~4482 是引擎内置的「玩家 A~H」伪所属方：动作 14 用它把单位交给第 1~8 号出生位的玩家。
    # 真实地图里动作 14 的参数全是这一组（实测 474 处，0 处是 0~13 的国家序号），必须列出来，
    # 否则下拉框只会显示「自定义值」。地图 [Houses] 里另加的 4475 之类的值不冲突。
    for i in range(8):
        houses.append({'v': str(4475 + i),
                       'label': '玩家 %s（%d · 出生位 %d）' % ('ABCDEFGH'[i], 4475 + i, i + 1)})

    teams = []
    for e in _sec_rows(m, 'teamtypes'):
        tid = (e.get('v') or '').strip()
        if not tid:
            continue
        nm = ''
        sec = m.find(tid)
        if sec:
            for kk in m.keys_of(sec):
                if (kk.get('k') or '').strip().lower() == 'name':
                    nm = (kk.get('v') or '').strip()
                    break
        teams.append({'v': tid, 'label': ('%s （%s）' % (nm, tid)) if nm else tid})

    triggers, trig_names = [], {}
    for e in _sec_rows(m, 'triggers'):
        tid = (e.get('k') or '').strip()
        parts = [x.strip() for x in (e.get('v') or '').split(',')]
        nm = parts[2] if len(parts) > 2 else ''
        trig_names[tid.upper()] = nm
        triggers.append({'v': tid, 'label': ('%s （%s）' % (nm, tid)) if nm else tid})

    wps, wpt_codes = [], []
    for i in range(26):
        wpt_codes.append({'v': TRIG.base26(i), 'label': TRIG.base26(i)})
    for e in _sec_rows(m, 'waypoints'):
        k = (e.get('k') or '').strip()
        if not k.isdigit():
            continue
        n = int(k)
        code = TRIG.base26(n)
        if n <= 7:
            wps.append({'v': str(n), 'label': '出生点 %d（%s）' % (n + 1, code)})
            continue
        label = '路径点 %d' % n
        wps.append({'v': str(n), 'label': '%s（%s）' % (label, code)})
        if not any(x['v'] == code for x in wpt_codes):
            wpt_codes.append({'v': code, 'label': '%s · %s' % (code, label)})

    return {
        'enums': {'houses': houses, 'teams': teams, 'triggers': triggers,
                  'wps': wps, 'wptCodes': wpt_codes},
        'trigNames': trig_names,
        'def': {'house': '0', 'team': (teams[0]['v'] if teams else ''),
                'trigger': (triggers[0]['v'] if triggers else ''), 'wp': '0', 'wpt': 'A'},
    }


def api_trigger_op(data):
    """触发动作：下拉框改完后由后台拼出最终字符串 / 给出新动作的默认参数"""
    op = (data.get('op') or 'compose').strip()
    c = data.get('ctx') or {}
    ctx = {'house': str(c.get('house', '0') or '0'),
           'team': str(c.get('team', '') or ''),
           'trigger': str(c.get('trigger', '') or ''),
           'wp': str(c.get('wp', '0') or '0'),
           'wpt': (str(c.get('wpt', 'A') or 'A').strip().upper() or 'A')}
    if op == 'default':
        try:
            aid = int(data.get('type'))
        except Exception:
            return {'error': '动作类型必须是数字'}
        blk = TRIG.default_block(aid, ctx, self_id=str(data.get('self') or '').strip())
        return {'ok': True, 'block': blk, 'value': TRIG.compose_actions([blk])}
    if op == 'parse':
        return {'ok': True, 'parsed': TRIG.parse_actions(str(data.get('value') or ''))}
    blocks = data.get('blocks')
    if not isinstance(blocks, list):
        return {'error': 'blocks 必须是数组'}
    try:
        val = TRIG.compose_actions(blocks)
    except Exception as e:
        return {'error': str(e)}
    return {'ok': True, 'value': val, 'parsed': TRIG.parse_actions(val)}


def api_edit_info(q):
    mid = (q.get('id') or [''])[0]
    p = abs_of(mid)
    if not os.path.isfile(p):
        return {'error': '文件不存在'}
    if os.path.splitext(p)[1].lower() not in MAP_EXT:
        return {'error': '这种文件不是文本 INI 地图，无法编辑'}
    m = INI.MapIni(p)
    secs = m.info()
    # v1.9：给每个参数挂「在地图上的作用位置」，右侧点一下就能在画布上高亮
    try:
        locs = mapedit.edit_locs(p)
    except Exception:
        locs = {}
    # v2.0：触发动作拆成中文下拉框——这里一次性把动作表、候选值、每行的解析结果带给前端
    tctx = None
    try:
        if any((s.get('name') or '').strip().lower() in ('actions', 'events') for s in secs):
            tctx = map_trigger_ctx(m)
    except Exception:
        log(traceback.format_exc())
        tctx = None
    for s in secs:
        low = (s.get('name') or '').strip().lower()
        for kk in (s.get('keys') or []):
            kk['loc'] = _edit_loc(low, kk.get('k'), locs)
            if low in ('actions', 'events') and tctx:
                nm = tctx['trigNames'].get((kk.get('k') or '').strip().upper())
                if nm:
                    kk['label'] = nm
                    kk['known'] = True
            if low == 'actions':
                try:
                    kk['act'] = TRIG.parse_actions(kk.get('v'))
                except Exception:
                    kk['act'] = {'ok': False, 'err': '解析失败', 'blocks': []}
    return {
        'ok': True,
        'id': mid,
        'path': p,
        'file': os.path.basename(p),
        'size': m.original_size,
        'sectionCount': len(secs),
        'keyCount': sum(len(s['keys']) for s in secs),
        'hasDigest': any(s['name'].lower() == 'digest' for s in secs),
        'backupDir': BACKUP_DIR,
        'sections': secs,
        'trig': ({'actions': TRIG.action_meta(), 'enums': tctx['enums'], 'def': tctx['def']}
                 if tctx else None),
    }


def api_edit_raw(q):
    mid = (q.get('id') or [''])[0]
    p = abs_of(mid)
    if not os.path.isfile(p):
        return {'error': '文件不存在'}
    m = INI.MapIni(p)
    return {'ok': True, 'text': m.raw_text(), 'size': m.original_size}


def api_edit_save(data):
    mid = data.get('id') or ''
    changes = data.get('changes') or []
    p = abs_of(mid)
    if not os.path.isfile(p):
        return {'error': '文件不存在'}
    if not changes:
        return {'ok': True, 'changed': 0, 'msg': '没有改动'}
    # 值里不允许换行，否则会写出新行把 INI 结构搞坏
    clean = []
    for c in changes:
        c2 = dict(c)
        if isinstance(c2.get('value'), str):
            c2['value'] = c2['value'].replace('\r', ' ').replace('\n', ' ').strip()
        clean.append(c2)
    m = INI.MapIni(p)
    done = m.apply(clean)
    if not m.is_changed():
        return {'ok': True, 'changed': 0, 'msg': '内容与原来相同，未写入'}
    backup = m.save(backup_root=BACKUP_DIR)
    invalidate()
    with META_LOCK:
        META_CACHE.clear()
    st = os.stat(p)
    log('修改地图 [%s] %d 处改动，备份: %s' % (os.path.basename(p), len(done), backup or '无'))
    return {'ok': True, 'changed': len(done), 'items': done, 'backup': backup,
            'mtime': int(st.st_mtime), 'size': st.st_size}


# ---------------------------------------------------------------- 绘制编辑器
_CATALOG = {'types': None, 'src': '', 'ts': 0.0}
CATALOG_PATH = os.path.join(DATA, 'overlay_types.json')


def get_catalog(force=False):
    """overlay 类型表（矿/宝石 ID 依赖游戏环境，见 overlay_catalog）"""
    g = game_dir()
    if not force and _CATALOG['types'] and time.time() - _CATALOG['ts'] < 300:
        return _CATALOG['types']
    if not g:
        _CATALOG['types'] = {}
        return {}
    try:
        types, src = overlay_catalog.load(g, DATA)
        _CATALOG.update({'types': types, 'src': src, 'ts': time.time()})
        cls = overlay_catalog.classify(types)
        mapgen.set_overlay_ids(cls['ore'], cls['gem'])
    except Exception:
        log(traceback.format_exc())
        _CATALOG['types'] = {}
    return _CATALOG['types']


def api_catalog():
    types = get_catalog()
    cls = overlay_catalog.classify(types) if types else {
        'ore': mapgen.ORE_IDS, 'gem': mapgen.GEM_IDS, 'ore2': [], 'ore3': []}
    return {'ok': True, 'source': _CATALOG.get('src', ''),
            'count': len(types),
            'ore': cls['ore'], 'gem': cls['gem'],
            'ore2': cls['ore2'], 'ore3': cls['ore3'],
            'oreId': mapgen.ORE_ID, 'gemId': mapgen.GEM_ID}


def api_editor_load(q):
    mid = (q.get('id') or [''])[0]
    if not mid:
        return {'error': '缺少地图 id'}
    try:
        p = abs_of(mid)
    except Exception as e:
        return {'error': str(e)}
    if not os.path.isfile(p):
        return {'error': '文件不存在'}
    get_catalog()
    try:
        info = mapedit.load(p, DATA, game_dir(), get_catalog())
    except Exception as e:
        log(traceback.format_exc())
        return {'error': '读取地图失败：%s' % e}
    info.update({'id': mid, 'name': os.path.splitext(os.path.basename(p))[0],
                 'file': os.path.basename(p)})
    return info


def api_editor_cell(q):
    """绘制画板里的"检查"工具：某一格上的全部参数"""
    mid = (q.get('id') or [''])[0]
    try:
        x = int((q.get('x') or ['0'])[0])
        y = int((q.get('y') or ['0'])[0])
    except ValueError:
        return {'error': '坐标格式错误'}
    if not mid:
        return {'error': '缺少地图 id'}
    try:
        p = abs_of(mid)
    except Exception as e:
        return {'error': str(e)}
    if not os.path.isfile(p):
        return {'error': '文件不存在'}
    get_catalog()
    try:
        return mapedit.cell_info(p, x, y, get_catalog())
    except Exception as e:
        log(traceback.format_exc())
        return {'error': '读取失败：%s' % e}


def api_editor_save(data):
    mid = data.get('id') or ''
    try:
        p = abs_of(mid)
    except Exception as e:
        return {'error': str(e)}
    if not os.path.isfile(p):
        return {'error': '文件不存在'}
    get_catalog()
    ops = {'paint': data.get('paint') or [], 'waypoints': data.get('waypoints'),
           'height': data.get('height')}
    try:
        res = mapedit.save(p, ops, backup_root=BACKUP_DIR)
    except Exception as e:
        log(traceback.format_exc())
        return {'error': '保存失败：%s' % e}
    if res.get('changed'):
        invalidate()
        with META_LOCK:
            META_CACHE.clear()
        # 立即用渲染器刷新缩略图
        try:
            render_one({'id': [mid]})
        except Exception:
            pass
        log('绘制保存 [%s] overlay=%s 备份=%s' %
            (os.path.basename(p), res.get('overlayCells'), res.get('backup') or '无'))
    return res


def api_new_map(data):
    g = game_dir()
    if not g:
        return {'error': '未设置游戏目录'}
    root = maps_root()
    if not root:
        return {'error': '找不到 Maps 目录'}
    folder = (data.get('folder') or 'Custom').strip() or 'Custom'
    dest = os.path.join(root, folder)
    get_catalog()
    try:
        size = max(30, min(200, int(data.get('size') or 80)))
    except (TypeError, ValueError):
        size = 80
    try:
        players = max(2, min(8, int(data.get('players') or 4)))
    except (TypeError, ValueError):
        players = 4
    try:
        res = mapedit.create(dest, data.get('name') or '新建地图', size=size,
                             theater=(data.get('theater') or 'TEMPERATE').upper(),
                             players=players,
                             ore=bool(data.get('ore', True)),
                             gems=bool(data.get('gems', True)),
                             seed=data.get('seed'),
                             author=data.get('author') or '红警地图管理器',
                             max_player=players)
    except Exception as e:
        log(traceback.format_exc())
        return {'error': '新建失败：%s' % e}
    invalidate()
    rel = os.path.relpath(res['path'], root).replace('\\', '/')
    res['id'] = rel
    try:
        render_one({'id': [res['id']]})
    except Exception:
        pass
    log('新建地图 [%s] %d 字节 出生点 %s' % (os.path.basename(res['path']),
                                            res['size'], res['starts']))
    return res


def render_one(q):
    mid = (q.get('id') or [''])[0]
    try:
        p = abs_of(mid)
    except Exception as e:
        return {'error': str(e)}
    tgt = thumb_path_for(p, 560)
    base = os.path.splitext(p)[0]
    for ie in IMG_EXT:
        for cand in (base + ie, base + ie.upper()):
            if os.path.isfile(cand):
                try:
                    os.remove(cand)
                except Exception:
                    pass
    for w in (560, 1500):
        try:
            os.remove(thumb_path_for(p, w))
        except Exception:
            pass
    img = ensure_image(p, 560)
    invalidate()
    return {'ok': bool(img), 'id': mid}


def start_warm(folder=''):
    global WARM
    if WARM.get('running'):
        return dict(WARM)
    maps = get_maps()
    if folder and folder not in ('ALL', ''):
        maps = [m for m in maps if m['folder'] == folder or m['folder'].startswith(folder + '/')]
    todo = [m for m in maps if not os.path.isfile(thumb_path_for(abs_of(m['id'])))]
    if not todo:
        return {'running': False, 'total': 0, 'done': 0, 'msg': '全部已有预览'}
    WARM = {'running': True, 'done': 0, 'total': len(todo), 'fail': 0, 'current': '', 'stop': False}
    t = threading.Thread(target=warm_worker, args=(todo,), daemon=True)
    t.start()
    return dict(WARM)


def browser_paths():
    pf = os.environ.get('ProgramFiles') or r'C:\Program Files'
    pf86 = os.environ.get('ProgramFiles(x86)') or r'C:\Program Files (x86)'
    la = os.environ.get('LOCALAPPDATA') or ''
    return [
        os.path.join(pf86, r'Microsoft\Edge\Application\msedge.exe'),
        os.path.join(pf, r'Microsoft\Edge\Application\msedge.exe'),
        os.path.join(pf, r'Google\Chrome\Application\chrome.exe'),
        os.path.join(pf86, r'Google\Chrome\Application\chrome.exe'),
        os.path.join(la, r'Google\Chrome\Application\chrome.exe'),
    ]


def focus_ui():
    """已有界面窗口就把它调到前面，避免连点几次开出一堆窗口"""
    try:
        import ctypes
        user32 = ctypes.windll.user32
        hits = []
        WNDENUMPROC = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)

        def cb(hwnd, lparam):
            if user32.IsWindowVisible(hwnd):
                n = user32.GetWindowTextLengthW(hwnd)
                if n:
                    buf = ctypes.create_unicode_buffer(n + 1)
                    user32.GetWindowTextW(hwnd, buf, n + 1)
                    t = buf.value.strip()
                    if t.startswith('红警地图管理器') and '文件资源管理器' not in t:
                        hits.append(hwnd)
            return True

        user32.EnumWindows(WNDENUMPROC(cb), 0)
        for h in hits:
            user32.ShowWindow(h, 9)          # SW_RESTORE
            if user32.SetForegroundWindow(h):
                return True
        return bool(hits)
    except Exception:
        return False


def open_ui(url):
    """用 Edge/Chrome 的「应用模式」开一个独立窗口。
    不依赖系统默认浏览器关联——无控制台程序里 webbrowser.open 常常静默失效，
    用户看到的就是"双击没反应"。应用窗口没有地址栏，也更像个桌面软件。"""
    if focus_ui():
        log('界面窗口已存在，已切换到前台')
        return True
    for exe in browser_paths():
        if not os.path.isfile(exe):
            continue
        try:
            subprocess.Popen([exe, '--app=' + url, '--window-size=1440,900',
                              '--no-first-run', '--no-default-browser-check'],
                             creationflags=CREATE_NO_WINDOW)
            log('已用应用窗口打开界面: ' + exe)
            return True
        except Exception as e:
            log('应用窗口启动失败 %s: %s' % (exe, e))
    try:
        import webbrowser
        if webbrowser.open(url):
            log('已用默认浏览器打开界面')
            return True
    except Exception as e:
        log('默认浏览器打开失败: %s' % e)
    fatal('界面没能自动打开。',
          '请手动在浏览器地址栏输入：\n' + url + '\n\n（程序仍在后台运行，关闭它请用任务管理器）')
    return False


def _use_native_window():
    """能否用原生窗口（设置了 RA2MAP_EDGE=1 可强制回退到 Edge 应用模式）"""
    if os.environ.get('RA2MAP_EDGE') == '1':
        return False
    try:
        import native_win  # noqa: F401
        return native_win.available()
    except Exception:
        return False


def find_running(base_port, span=20):
    """已有实例在跑时，直接复用它，避免双击多次开一堆进程抢端口"""
    import urllib.request
    for cand in range(base_port, base_port + span):
        try:
            with urllib.request.urlopen('http://127.0.0.1:%d/api/ping' % cand, timeout=0.35) as r:
                data = json.loads(r.read().decode('utf-8'))
            if data.get('app') == 'ra2map':
                return cand
        except Exception:
            continue
    return None


def main():
    log('进程启动 v%s (frozen=%s)' % (VERSION, FROZEN))
    base_port = int(os.environ.get('RA2MAP_PORT') or 8777)
    try:
        det = detect_game_dirs()
        if not game_dir() and det:
            CONFIG['gameDir'] = det[0]
            save_json(CONFIG_PATH, CONFIG)
    except Exception:
        pass
    import socket

    class Server(ThreadingHTTPServer):
        allow_reuse_address = False
        daemon_threads = True

    alive = find_running(base_port)
    if alive:
        url = 'http://127.0.0.1:%d/' % alive
        log('已有实例在运行，直接打开界面: ' + url)
        open_ui(url)
        return

    httpd = None
    port = base_port
    for cand in range(base_port, base_port + 20):
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 0)
        try:
            s.bind(('127.0.0.1', cand))
            s.close()
        except OSError:
            s.close()
            continue
        try:
            httpd = Server(('127.0.0.1', cand), Handler)
            port = cand
            break
        except OSError:
            continue
    if httpd is None:
        fatal('启动失败：本地端口 8777-8796 全被占用。',
              '请关闭其他占用端口的程序后重试，或重启电脑。')
        return
    SERVER['httpd'] = httpd
    SERVER['port'] = port
    url = 'http://127.0.0.1:%d/' % port
    log('红警地图管理器 v%s 已启动: %s' % (VERSION, url))
    log('程序目录: ' + APP_DIR)
    log('游戏目录: ' + (game_dir() or '未设置'))
    if not os.environ.get('RA2MAP_NOBROWSER') and _use_native_window():
        # 原生窗口：WebView2 只能跑在主线程，本地服务放后台线程
        log('使用原生窗口（WebView2）')
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        try:
            import native_win  # 修复：_use_native_window 内的 import 不进本作用域，exe 里必 NameError
            native_win.run(url, on_close=shutdown_app,
                           icon=os.path.join(RES_DIR, 'app.ico'))
        except Exception:
            log('原生窗口失败，回退到浏览器应用模式：\n' + traceback.format_exc())
            open_ui(url)
            try:
                httpd.serve_forever()
            except KeyboardInterrupt:
                pass
        finally:
            shutdown_app()
        return
    if not os.environ.get('RA2MAP_NOBROWSER'):
        def _later():
            time.sleep(0.5)
            open_ui(url)
        threading.Thread(target=_later, daemon=True).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        log('服务已退出')


if __name__ == '__main__':
    try:
        main()
    except Exception:
        fatal('程序异常退出', traceback.format_exc()[-1200:])

