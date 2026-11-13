# 回归测试

仓库里放的是**自包含、不碰真实游戏目录**的两个 API 级用例，方便贡献者快速验证核心逻辑。

```bash
python tests/test_v24_api.py         # 平台导入 / 名称特征多选 / AI 多连接与自动兜底
python tests/test_v23_namefilter.py  # 名称特征筛选边界 / 右键定位参数 / 临时 HTTP 服务
```

两个脚本都把 `ra2map.DATA` / `CONFIG_PATH` / `META_CACHE_PATH` / `TRASH` / `TMP` / `BACKUP_DIR`
隔离到临时目录，用假地图数据或 mock 的 `/models` 响应，不会写真实 `数据/` 或游戏 `Maps/`。

## 本地完整回归清单

开发机上还有几组不便公开的用例（依赖 629 张实测地图样本或 pywebview 弹窗）：

| 用例 | 用途 |
| --- | --- |
| `test_v241_namefix.py` | v2.4.1 名称口径修正：含差额闭合断言、零张纯中文卡片 |
| `test_v24_ui.py` | 原生窗口 UI 回归（多选条、平台导入面板、AI 配置、模块内 AI） |
| `test_v24_exe.py` | 部署版 EXE 实证：起进程 → ping/catalog → 平台扫描 → `/api/quit` |
| `test_native.py` | 通用原生窗口冒烟：新建地图 + 刷矿保存后复核 120 格矿 |
| `test_v21_headfix.py` / `test_v22_headfix.py` | 坏字节头/尾修复回归 |

**改了 `web/index.html` 必跑 `test_native.py`**；打包部署后跑 `test_v24_exe.py`。

## 纪律

- 测试图（接口测试图 / UI 端到端测试图）用完**必须删**：游戏 `Maps/Custom` + `数据/地图备份` 两处。
- 测试素材一律从 `Maps/Custom/*.mpr` 自动定位，不硬编码地图名。
- UI 测试必须隔离数据目录，否则会污染源码 `数据/ai_config.json`。
- UI 断言切换口径/勾条件后，要等 `nfStat` 文案刷新再读 DOM（`loadMaps()` 异步竞态）。
