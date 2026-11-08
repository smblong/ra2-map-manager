# 贡献指南

感谢你愿意帮忙。这个项目是从自用工具长出来的，代码里还留着不少「先跑起来再说」的痕迹，
所以**最欢迎的不是大重构，而是能复现的问题、实测数据和针对性补丁**。

## 开始前

- 平台：**Windows 10/11 + Python 3.11+**（依赖 WebView2、`winreg`、资源管理器 `/select`、CNCMaps 渲染器，暂不支持 Linux/macOS）
- 安装：`python -m pip install -r requirements-dev.txt`
- 启动：`python ra2map.py`

## 最有用的三类贡献

1. **新平台地图库适配**：在 `platform_import.py` 里加一个扫描分支，并附上该平台的目录结构说明与一个可公开的样本（不要上传任何加密主体或平台资源）。
2. **不同 MOD 下的实测反馈**：矿/宝石 Overlay ID 是 **mod 相关**的，从 `expandmdXX.mix` 的 `[OverlayTypes]` 明文读取；CNCMaps 渲染器硬编码的 102-121 在某些 mod 上全错。请说明 MOD 名称、版本、实测 ID 范围。
3. **能复现的 Bug**：见下方「提 Issue 要带什么」。

## 提 Issue 要带什么

| 项 | 说明 |
| --- | --- |
| 版本 | EXE 文件版本或 `ra2map.VERSION` |
| 环境 | Windows 版本、游戏/MOD 名称与版本、WebView2 是否可用 |
| 复现步骤 | 具体到点哪个按钮、选哪张图、期望什么、实际什么 |
| 日志 | `数据/运行日志.txt` 的相关片段（**请先删掉里面的 API Key、路径等私人信息**） |
| 截图 | 有界面问题时尽量附 |

## 代码约定

- 前端是**单个 `web/index.html`**，两个 script 块：主块挂 `window.__app`，绘制块挂 `window.__pt`（末尾整块 try/catch 落到 `window.__errs`）。**没有 npm、没有构建步骤**，别引入框架。
- 行尾：`web/index.html` 是 **LF**，`ra2map.py` 是 **CRLF**。用脚本批量替换时注意别把行尾改坏。
- 后端 API 集中在 `ra2map.py` 的 `api_*` 方法里；新增模块要同步加进 `红警地图管理器.spec` 的 `hiddenimports`，否则打包后 EXE 里会缺模块。
- **写盘先备份、失败整体回滚**是硬约定。任何新增的地图写入路径都要遵守：平台目录只读、覆盖前备份、AI 方案确认后才落盘。
- 明文 API Key 绝不回传前端：`public_config()` 只给 `hasKey` / `keyMasked`。

## 测试

```bash
python tests/test_v24_api.py
python tests/test_v23_namefilter.py
```

- 改了 `web/index.html` **必须**跑通用原生窗口冒烟（会真的弹窗并刷矿复核）；仓库里的两个用例是 API 级、临时目录隔离、不碰真实游戏目录。
- 测试图用完必须删（游戏 `Maps/Custom` 与 `数据/地图备份` 两处）；测试素材从现有地图自动定位，不要硬编码地图名。
- UI 断言切换筛选条件后要等统计文案刷新再读 DOM，否则会撞上 `loadMaps()` 的异步竞态。

## 提交 PR

- 一个 PR 只做一件事，写清楚「改了什么 / 为什么 / 怎么验的」。
- 涉及版本号时，同步 `ra2map.py` 的 `VERSION`、`web/index.html` 的 logo、`version_info.txt`、`README.md`、`CHANGELOG.md` 与 Release Notes。
- 不要提交 `数据/`、`dist/`、`build/`、`*.exe`、`*.zip`、`_ref/`（已在 `.gitignore`）。
- **不要提交任何 API Key、私人路径、真实地图文件或平台资源。**

## 明确不做

- 破解 `.ra2mp` 等平台的加密地图主体
- 修改游戏可执行文件、注入进程、联机大厅补丁
- 内置地图库、账号系统、在线服务

详见 [SECURITY.md](SECURITY.md) 与 [README 的边界说明](README.md#安全隐私与边界)。
