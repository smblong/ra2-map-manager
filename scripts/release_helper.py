# -*- coding: utf-8 -*-
"""版本发布辅助工具（维护者用，玩家不需要）。

在任意目录运行都可以，路径按本文件位置自动定位：

    python scripts/release_helper.py check                 # 检查版本号六处是否同步
    python scripts/release_helper.py manifest             # 输出所有分发包的校验清单
    python scripts/release_helper.py plan v2.4.2          # 输出该版本的百度网盘上传计划
    python scripts/release_helper.py sync <源码目录>       # 把源码同步进本仓库

注意：本脚本**不上传**任何文件。百度网盘的二进制上传通道与限制见
docs/维护者发布备忘.md；纯文本说明可由网盘接口直接写入。
"""
import hashlib
import io
import os
import re
import shutil
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WORK = os.path.dirname(REPO)                      # 工作区根（分发包所在目录）
NETDISK = '/游戏/红色警戒重置版/地图管理器'
HISTORY = NETDISK + '/历史版本'
SHARE = 'https://pan.baidu.com/s/1BV8eli3-T1HvKi8DK0Zteg?pwd=bg24'

SOURCE_FILES = [
    'ra2map.py', 'ai_proxy.py', 'platform_import.py', 'mapedit.py', 'mapfog.py',
    'mapgen.py', 'mapio.py', 'modereg.py', 'native_win.py', 'overlay_catalog.py',
    'ini_edit.py', 'trigger_defs.py', 'web/index.html',
    '红警地图管理器.spec', 'version_info.txt',
]


def digests(path):
    d = open(path, 'rb').read()
    return len(d), hashlib.md5(d).hexdigest(), hashlib.sha256(d).hexdigest()


def current_version():
    s = io.open(os.path.join(REPO, 'ra2map.py'), encoding='utf-8', errors='replace').read()
    m = re.search(r"^VERSION\s*=\s*['\"]([^'\"]+)", s, re.M)
    return m.group(1) if m else '(未找到)'


def cmd_check():
    v = current_version()
    print('当前版本:', v)
    checks = [
        ('ra2map.py VERSION', os.path.join(REPO, 'ra2map.py'), v),
        ('前端 logo', os.path.join(REPO, 'web/index.html'), '<small>v%s</small>' % v),
        ('version_info FileVersion', os.path.join(REPO, 'version_info.txt'), v + '.0'),
        ('CHANGELOG', os.path.join(REPO, 'CHANGELOG.md'), '[v%s]' % v),
        ('README 徽章', os.path.join(REPO, 'README.md'), 'version-v' + v),
    ]
    ok = True
    for name, path, needle in checks:
        hit = needle in io.open(path, encoding='utf-8', errors='replace').read()
        print('  %-26s %s' % (name, 'OK' if hit else '!!! 缺失 ' + needle))
        ok = ok and hit
    # Release Notes 按版本命名，允许不存在（新版本尚未写）
    rn = os.path.join(REPO, 'RELEASE_NOTES_v%s.md' % v)
    print('  %-26s %s' % ('Release Notes', 'OK' if os.path.exists(rn) else '（本版尚未创建）'))
    print('结论:', '版本号同步' if ok else '有项目未同步，请先修再发布')
    return 0 if ok else 1


def cmd_manifest():
    import glob
    zips = sorted(glob.glob(os.path.join(WORK, '红警地图管理器_v*.zip')))
    if not zips:
        print('工作区里没有分发包:', WORK)
        return 1
    lines = ['版本分发包校验清单', '生成目录: ' + WORK, '=' * 100]
    lines.append('%-42s %12s  %-34s %s' % ('文件名', '大小(B)', 'MD5', 'SHA256'))
    lines.append('-' * 100)
    for p in zips:
        size, md5, sha = digests(p)
        lines.append('%-42s %12d  %-34s %s' % (os.path.basename(p), size, md5, sha))
    lines += ['', '网盘目标目录: ' + NETDISK, '历史版本目录: ' + HISTORY,
              '分享链接（目录级，替换/新增文件不影响）: ' + SHARE]
    text = '\n'.join(lines) + '\n'
    out = os.path.join(WORK, '发布校验清单.txt')
    io.open(out, 'w', encoding='utf-8', newline='').write(text)
    print(text)
    print('已写入:', out)
    return 0


def cmd_plan(ver):
    name = '红警地图管理器_v%s.zip' % ver.lstrip('v')
    local = os.path.join(WORK, name)
    if not os.path.exists(local):
        print('找不到分发包:', local)
        return 1
    size, md5, sha = digests(local)
    print('=' * 78)
    print('版本 %s 的百度网盘上传计划' % ver)
    print('=' * 78)
    print('本地文件 :', local)
    print('大小     : %d B' % size)
    print('MD5      :', md5)
    print('SHA256   :', sha)
    print()
    print('① 上传新包  ->', NETDISK + '/' + name)
    print('② 把上一版 zip 从上级目录移入', HISTORY, '（不删除、不覆盖）')
    print('③ 更新以下纯文本（可直接写入网盘任意中文目录）：')
    for f in ['版本说明.txt', '更新记录.txt', '历史版本/历史版本索引.txt']:
        print('     ', NETDISK + '/' + f)
    print('④ GitHub：CHANGELOG.md 新增条目 + Release tag v%s + 上传 zip 附件' % ver.lstrip('v'))
    print('⑤ 论坛：目录级分享链接不变 ->', SHARE)
    print()
    print('⚠ 二进制 zip 的上传通道见 docs/维护者发布备忘.md 第三节：')
    print('   当前接口只能直写文本；需要 bdpan CLI（已登录）或网盘客户端手动拖入。')
    print('   纯文本①②③可由网盘接口直接写入，无需 CLI。')
    return 0


def cmd_sync(src):
    src = os.path.abspath(src)
    if not os.path.isdir(src):
        print('源码目录不存在:', src)
        return 1
    changed = 0
    for rel in SOURCE_FILES:
        a, b = os.path.join(src, rel), os.path.join(REPO, rel)
        if not os.path.exists(a):
            print('  跳过（源缺失）:', rel)
            continue
        os.makedirs(os.path.dirname(b), exist_ok=True)
        if os.path.exists(b) and open(a, 'rb').read() == open(b, 'rb').read():
            continue
        shutil.copy2(a, b)
        print('  同步:', rel)
        changed += 1
    print('共同步 %d 个文件。注意：此操作只覆盖源码，不动 README/CHANGELOG/docs。' % changed)
    print('下一步：python scripts/release_helper.py check')
    return 0


def main():
    args = sys.argv[1:]
    if not args:
        print(__doc__)
        return 0
    cmd = args[0]
    if cmd == 'check':
        return cmd_check()
    if cmd == 'manifest':
        return cmd_manifest()
    if cmd == 'plan':
        if len(args) < 2:
            print('用法: plan v2.4.2')
            return 1
        return cmd_plan(args[1])
    if cmd == 'sync':
        if len(args) < 2:
            print('用法: sync <源码目录>')
            return 1
        return cmd_sync(args[1])
    print('未知命令:', cmd)
    print(__doc__)
    return 1


if __name__ == '__main__':
    sys.exit(main())
