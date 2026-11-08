# -*- coding: utf-8 -*-
"""原生窗口壳（pywebview / WebView2）

用 Edge WebView2 内核渲染界面，但窗口是程序自己的：自带标题、图标，
没有地址栏和浏览器菜单，任务栏显示程序图标，不是浏览器的一个窗口。
pywebview 不可用时由调用方回退到 Edge「应用模式」。
"""
import os
import threading

TITLE = '红警地图管理器'
_WIN = {'obj': None, 'ready': False}


def available():
    try:
        import webview  # noqa: F401
        return True
    except Exception:
        return False


def run(url, on_close=None, size=(1460, 920), icon=None):
    """阻塞运行窗口，直到用户关闭"""
    import webview

    win = webview.create_window(
        TITLE, url,
        width=size[0], height=size[1],
        min_size=(1120, 700),
        background_color='#0e1116',
        text_select=False,
        confirm_close=False,
    )
    _WIN['obj'] = win

    def _closed():
        if on_close:
            try:
                on_close()
            except Exception:
                pass

    win.events.closed += _closed

    kwargs = {}
    if icon and os.path.isfile(icon):
        kwargs['icon'] = icon
    try:
        webview.start(**kwargs)
    except TypeError:
        webview.start()
    return True


def destroy():
    """从其它线程关掉窗口"""
    w = _WIN.get('obj')
    if w is None:
        return
    try:
        w.destroy()
    except Exception:
        pass


def start_in_thread(url, on_close=None, icon=None):
    t = threading.Thread(target=run, args=(url, on_close, (1460, 920), icon),
                         daemon=True)
    t.start()
    return t

# ---------------------------------------------------------------- 目录选择对话框
# HTTP 服务跑在后台线程；这里用独立 STA 线程调 Win32 原生文件夹选择框，
# 带超时保护，避免请求线程被卡死。
# 注意：本环境 Python 不含 tkinter，pywebview 的 WinForms 对话框又要求 UI/STA 线程，
# 所以直接用 shell32 的 SHBrowseForFolderW（Vista 风格：树 + 路径输入框 + 新建文件夹）。
_PICK_TIMEOUT = 180
BIF_RETURNONLYFSDIRS = 0x0001
BIF_EDITBOX = 0x0010
BIF_NEWDIALOGSTYLE = 0x0040


def _pick_native(initial=''):
    """弹出系统文件夹选择框，返回绝对路径；取消/失败返回 ''"""
    import ctypes
    from ctypes import wintypes

    class BROWSEINFO(ctypes.Structure):
        _fields_ = [('hwndOwner', wintypes.HWND), ('pidlRoot', ctypes.c_void_p),
                    ('pszDisplayName', wintypes.LPWSTR), ('lpszTitle', wintypes.LPCWSTR),
                    ('ulFlags', wintypes.UINT), ('lpfn', ctypes.c_void_p),
                    ('lParam', wintypes.LPARAM), ('iImage', ctypes.c_int)]

    try:
        ole32 = ctypes.windll.ole32
        shell32 = ctypes.windll.shell32
        user32 = ctypes.windll.user32
        # 64 位下必须显式声明指针宽度，否则默认 c_int 会截断 PIDL / HWND
        shell32.SHBrowseForFolderW.argtypes = [ctypes.c_void_p]
        shell32.SHBrowseForFolderW.restype = ctypes.c_void_p
        shell32.SHGetPathFromIDListW.argtypes = [ctypes.c_void_p, wintypes.LPWSTR]
        shell32.SHGetPathFromIDListW.restype = wintypes.BOOL
        ole32.CoTaskMemFree.argtypes = [ctypes.c_void_p]
        user32.GetForegroundWindow.restype = wintypes.HWND
        ole32.CoInitialize(None)   # CoInitialize == STA，COM 对话框需要
    except Exception:
        return ''

    disp = ctypes.create_unicode_buffer(1024)
    bi = BROWSEINFO()
    bi.hwndOwner = user32.GetForegroundWindow()
    bi.pszDisplayName = ctypes.cast(disp, wintypes.LPWSTR)
    bi.lpszTitle = '选择游戏目录（含 gamemd.exe 和 Maps 的那一层）'
    bi.ulFlags = BIF_RETURNONLYFSDIRS | BIF_EDITBOX | BIF_NEWDIALOGSTYLE
    try:
        pidl = shell32.SHBrowseForFolderW(ctypes.byref(bi))
        if not pidl:
            return ''
        out = ctypes.create_unicode_buffer(1024)
        okp = shell32.SHGetPathFromIDListW(pidl, out)
        ole32.CoTaskMemFree(pidl)
        return out.value if okp else ''
    except Exception:
        return ''


def pick_backend():
    """给测试/诊断用：当前实际生效的目录选择实现"""
    return 'win32'


def choose_folder(initial='', timeout=_PICK_TIMEOUT):
    """从任意线程安全地弹出系统文件夹选择框。

    返回选中的绝对路径；取消/失败/超时返回 ''（不会阻塞调用方超过 timeout 秒）。
    """
    box = {'v': '', 'done': False}

    def worker():
        try:
            box['v'] = _pick_native(initial) or ''
        except Exception:
            box['v'] = ''
        finally:
            box['done'] = True

    t = threading.Thread(target=worker, daemon=True)
    t.start()
    t.join(timeout)
    return box['v'] if box['done'] else ''
