## 改了什么

## 为什么

（关联 Issue：# ）

## 怎么验的

- [ ] `python tests/test_v24_api.py`
- [ ] `python tests/test_v23_namefilter.py`
- [ ] 改了前端 → 已跑通用原生窗口冒烟（刷矿落盘复核）
- [ ] 打包部署 → 已跑部署版 EXE 实证
- [ ] 新增模块已加进 `红警地图管理器.spec` 的 `hiddenimports`

## 同步检查

- [ ] 版本号六处同步（源码 / logo / version_info / README / CHANGELOG / Release Notes）
- [ ] 没有提交 `数据/`、`dist/`、`build/`、`*.exe`、`*.zip`、`_ref/`
- [ ] 没有提交 API Key、私人路径、真实地图或平台资源
- [ ] 写盘路径遵守「先备份、失败回滚」
