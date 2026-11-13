# -*- coding: utf-8 -*-
"""红警2 / 尤里的复仇 地图 INI 解析与编辑核心

红警地图（.yrm / .map / .mpr）本质是 GBK 编码的纯文本 INI，
但里面混着 [IsoMapPack5]、[OverlayPack]、[PreviewPack] 这类几万行的
base64 二进制段（zlib 压缩的地形/覆盖物/预览图）。

设计原则：
1) 全程按"行"处理，未修改的行保留原始字节，绝不重新编码 —— 避免中文乱码、
   避免二进制段被破坏；
2) 只重建"被改动过的段"，其余段字节级不变；
3) 写文件前自动备份，写文件用临时文件 + 原子替换。

键名说明：这里的中文说明按 FinalAlert 2 / FA2sp 实际生成的地图键名为准
（例如 TrainCrate / IgnoreGlobalAITriggers / MultiplayerOnly=1）。
"""

import os
import re
import shutil
import time

# 二进制段：内容不是 key=value，而是编号行 = base64(zlib(...))，不解析、不显示
BIN_SECTIONS = {
    'isomappack5', 'overlaypack', 'overlaydatapack', 'previewpack',
    'preview', 'mappack', 'overlaypack5', 'previewpack5',
}

_SECTION_RE = re.compile(rb'^\s*\[([^\]\r\n]+)\]')
_NUM_SECTION = re.compile(r'^[0-9A-Fa-f]{6,10}$')


def dec(b):
    """字节 -> 文本，优先 GBK（红警中文地图的通用编码）"""
    for cp in ('gbk', 'cp1252', 'latin-1'):
        try:
            return b.decode(cp)
        except UnicodeDecodeError:
            continue
    return b.decode('latin-1', 'replace')


def enc(s):
    """文本 -> 字节，优先 GBK"""
    for cp in ('gbk', 'gb18030', 'cp1252'):
        try:
            return s.encode(cp)
        except UnicodeEncodeError:
            continue
    return s.encode('latin-1', 'replace')


def split_kv(line):
    """拆出 key=value；注释、段头、空行、无等号行都返回 None"""
    b = line.rstrip(b'\r')
    s = b.strip()
    if not s or s.startswith(b';') or s.startswith(b'['):
        return None
    if b'=' not in b:
        return None
    k, v = b.split(b'=', 1)
    return dec(k).strip(), dec(v).strip()


# ---------------------------------------------------------------- 字段知识库
def T(label, tip='', **kw):
    d = {'label': label, 'tip': tip, 'type': 'text'}
    d.update(kw)
    return d


YY = {'type': 'enum', 'opts': ['yes', 'no']}          # yes / no
B10 = {'type': 'enum', 'opts': ['1', '0']}            # 1 / 0
INT = {'type': 'int'}
FLT = {'type': 'float'}
RO = {'ro': True}

SECTION_META = {
    'basic': {'label': '地图基础信息', 'tip': '地图名、人数、模式。这里的很多键在编辑器里看不到，但会真实影响游戏'},
    'specialflags': {'label': '特殊规则开关 ★', 'tip': '多人图的隐藏规则：战争迷雾、陨石雨、矿车无敌、固定联盟……游戏界面里完全看不到'},
    'lighting': {'label': '光照与画面色调 ★', 'tip': '整张图的画面色调。天气、晨昏、尤里心灵控制器生效时的偏色都在这里'},
    'map': {'label': '地形与地图参数', 'tip': '地形主题、地图尺寸、资源密度'},
    'ranking': {'label': '战役评级参数', 'tip': '标准通关时间与评价文字（战役图才用）'},
    'waypoints': {'label': '出生点与路径点 ★', 'tip': '格式：编号=X*1000+Y 的合并值。0~7 是玩家出生点，其余是脚本路径点'},
    'header': {'label': '旧格式头信息', 'tip': '老版本 RA2 地图的头部信息，一般可忽略'},
    'houses': {'label': '参战方列表', 'tip': '格式：序号=国家名。玩家的资金、结盟、科技等级写在以国家名命名的段里'},
    'countries': {'label': '国家定义', 'tip': '自定义国家的属性与可造单位'},
    'sides': {'label': '阵营定义'},
    'colors': {'label': '颜色定义', 'tip': '格式：序号=颜色名,RGB'},
    'units': {'label': '载具', 'tip': '格式：序号=归属,单位ID,血量(0-256),X,Y,朝向(0-255),任务,标签,老兵等级(0-3),编队'},
    'infantry': {'label': '步兵', 'tip': '格式：序号=归属,步兵ID,血量(0-256),X,Y,朝向(0-255),任务,标签,老兵等级(0-3),编队'},
    'aircraft': {'label': '飞行器', 'tip': '格式：序号=归属,单位ID,血量,X,Y,朝向,任务,标签,老兵等级,编队'},
    'structures': {'label': '建筑', 'tip': '格式：序号=归属,建筑ID,血量,X,Y,朝向,任务,标签,老兵等级,编队'},
    'terrain': {'label': '地形物件', 'tip': '格式：序号=物件ID,X,Y —— 树、石头等装饰'},
    'smudge': {'label': '地表污迹', 'tip': '格式：序号=污迹ID,X,Y'},
    'overlay': {'label': '地表覆盖', 'tip': '格式：序号=覆盖物ID,X,Y —— 矿、路等'},
    'tags': {'label': '触发器标签', 'tip': '格式：序号=标签ID,0,名字'},
    'events': {'label': '触发事件', 'tip': '格式：序号=事件类型,条件参数... 事件定义"什么时候发生"'},
    'actions': {'label': '触发动作', 'tip': '格式：序号=动作类型,参数... 动作定义"发生什么"'},
    'triggers': {'label': '触发器', 'tip': '格式：序号=事件序号,动作序号 —— 把事件和动作串起来'},
    'teamtypes': {'label': '小队类型', 'tip': 'AI 成组进攻用的编制'},
    'taskforces': {'label': '特遣部队', 'tip': '小队里包含哪些单位'},
    'scripttypes': {'label': 'AI 脚本', 'tip': 'AI 单位的行动脚本'},
    'aitriggertypes': {'label': 'AI 触发', 'tip': 'AI 何时组建小队、何时进攻'},
    'variablenames': {'label': '变量名', 'tip': '格式：序号=变量名'},
    'digest': {'label': '文件校验值', 'tip': '地图内容的摘要值。改了地图内容后这个值与实际内容不再匹配，个别客户端会提示地图不一致'},
    'fa2spversioncontrol': {'label': '编辑器版本标记', 'tip': 'FA2sp 写入的版本号，无游戏作用'},
    'isomappack5': {'label': '地形数据（二进制）', 'tip': '压缩的地形格子数据，是地图的"骨头"，只能由地图编辑器生成'},
    'overlaypack': {'label': '覆盖物数据（二进制）', 'tip': '压缩的矿脉/道路覆盖数据'},
    'overlaydatapack': {'label': '覆盖物数据（二进制）', 'tip': '压缩的覆盖物附加数据'},
    'previewpack': {'label': '内嵌预览图（二进制）', 'tip': '地图自带的小预览图（base64 + zlib）'},
    'preview': {'label': '内嵌预览（旧格式）', 'tip': '旧格式预览图'},
    'mappack': {'label': '地图打包数据（二进制）', 'tip': '压缩数据'},
}

# 跨段通用（玩家段、单位段都会出现）
COMMON = {
    'country': T('所属国家', '对应 [Countries] 段里定义的国家名'),
    'credits': T('初始资金', '标准多人图为 10000', **INT),
    'allies': T('盟友', '结盟的玩家序号，多个用逗号分隔，例如 0,2。留空 = 不结盟'),
    'edge': T('出生边缘', '', type='enum', opts=['North', 'South', 'East', 'West', '']),
    'iq': T('AI 智商', '0-100，越高越会造建筑和进攻', **INT),
    'percentbuilt': T('建造完成度', '0-100，任务图里 AI 开局基地的完成程度', **INT),
    'playercontrol': T('玩家控制', 'yes = 由真人或 AI 玩家控制', **YY),
    'techlevel': T('科技等级', '1-10，决定可造的单位与建筑', **INT),
    'base': T('开局基地', 'yes = 开局带基地车或建筑（任务图用）', **YY),
    'color': T('颜色', '玩家颜色名，见 [Colors] 段'),
    'side': T('阵营'),
    'nodecount': T('节点数', '寻路用，一般不填'),
}

DOCS = {
    'basic': {
        'name': T('地图名称', '大厅地图列表里显示的名字'),
        'author': T('作者'),
        'percent': T('完成度', '0-100，编辑器里显示的完成百分比', **INT),
        'gamemode': T('游戏模式', '仅作显示，例如 standard'),
        'inittime': T('初始资金', '开局给多少钱。标准多人图 10000', **INT),
        'minplayer': T('最少玩家', '', **INT),
        'maxplayer': T('最多玩家', '决定大厅的人数上限，也对应出生点数量', **INT),
        'multiplayeronly': T('仅限多人', '1 = 只在多人对局出现，单人/遭遇战列表里看不到；0 = 都能看到', **B10),
        'official': T('官方地图', 'yes = 标记为官方地图，部分客户端显示官方图标', **YY),
        'freeradar': T('免费雷达', 'yes = 不用造雷达也能看全图', **YY),
        'ignoreglobalaitriggers': T('忽略全局 AI 触发', 'yes = 不执行游戏内置的全局 AI 逻辑，自制图常用', **YY),
        'fixedseed': T('固定随机种子', 'yes = 每局随机序列相同；联机一般保持 no', **YY),
        'traincrate': T('训练木箱', 'yes = 打箱子掉落奖励', **YY),
        'truckcrate': T('部队木箱', 'yes = 地图上散布部队木箱', **YY),
        'carryovermoney': T('继承资金', 'yes = 上一关剩余资金带到下一关', **YY),
        'carryovercap': T('继承上限', '继承资金的最大值', **INT),
        'endofgame': T('结束电影', '通关后播放的电影名，no = 不播'),
        'intro': T('开场电影'),
        'brief': T('任务简报'),
        'theme': T('背景音乐'),
        'movie': T('过场电影'),
        'loadingscreen': T('载入画面'),
        'numscreens': T('载入画面数', '', **INT),
        'ls640title': T('640 读盘标题'),
        'ls800title': T('800 读盘标题'),
        'nextscenario': T('下一关地图'),
        'altnextscenario': T('备用下一关'),
        'skipscore': T('跳过计分', 'yes = 结束后不显示计分画面', **YY),
        'skipmapselect': T('跳过选图', 'yes = 进入时不弹地图选择', **YY),
        'onetimeonly': T('仅可玩一次', 'yes = 打一次后不再出现在列表', **YY),
        'timer.inherit': T('计时器继承', 'yes = 沿用上一关的计时器', **YY),
        'playerkillcount': T('击杀统计', 'yes = 界面显示击杀数', **YY),
        'tutorial': T('教学关', 'yes = 按教学关处理，显示操作提示', **YY),
        'windowed': T('窗口化', '', **YY),
        'newniformat': T('INI 格式版本', '引擎据此判断地图格式（YR 多为 4 或 11），改了会解析失败', **INT, **RO),
        'icegrowthenabled': T('冰面生长', 'yes = 冰面随时间扩张', **YY),
        'veingrowthenabled': T('矿脉生长', 'yes = 矿会慢慢长回来', **YY),
        'tiberiumgrowthenabled': T('矿脉生长（旧键）', '', **YY),
        'tiberiumdeathtovisceroid': T('矿脉死亡生化怪', 'yes = 矿被采完的地方可能出现生化怪', **YY),
        'altnextscenario': T('备用下一关'),
    },
    'specialflags': {
        'inert': T('惰性模式', 'yes = 地图不跑常规战斗逻辑，用于纯展示/过场', **YY),
        'fogofwar': T('战争迷雾', 'yes = 未探明区域黑幕覆盖（TS 遗留功能，红警里很少用）', **YY),
        'ionstorms': T('离子风暴', 'yes = 随机出现风暴，摧毁飞行器并干扰电力', **YY),
        'mcvdeploy': T('允许基地车展开', 'yes = 基地车可以展开成建造厂。关掉后玩家只能靠初始建筑', **YY),
        'meteorites': T('陨石雨', 'yes = 随机落陨石', **YY),
        'visceroids': T('生化怪', 'yes = 随机出现生化怪', **YY),
        'fixedalliance': T('固定联盟', 'yes = 禁止外交变更，联盟关系锁死', **YY),
        'tiberiumgrows': T('矿脉生长', 'yes = 矿会扩散', **YY),
        'initialveteran': T('初始老兵', 'yes = 开局所有单位都是老兵等级', **YY),
        'harvesterimmune': T('矿车无敌', 'yes = 矿车不受伤害，防止开局被偷矿车', **YY),
        'tiberiumspreads': T('矿脉扩散', 'yes = 矿会向外蔓延', **YY),
        'tiberiumexplosive': T('矿脉爆炸', 'yes = 矿被摧毁时会爆炸', **YY),
        'destroyablebridges': T('桥梁可摧毁', 'yes = 桥可以被炸断', **YY),
    },
    'lighting': {
        'red': T('主光 红', '0.0 - 2.0，越大越偏红', **FLT),
        'green': T('主光 绿', '0.0 - 2.0', **FLT),
        'blue': T('主光 蓝', '0.0 - 2.0，越大越偏蓝', **FLT),
        'level': T('主光强度', '越大画面越亮', **FLT),
        'ground': T('地面光照', '', **FLT),
        'ambient': T('环境光', '越大整体越亮、对比越弱', **FLT),
        'ionred': T('离子风暴 红', '', **FLT),
        'iongreen': T('离子风暴 绿', '', **FLT),
        'ionblue': T('离子风暴 蓝', '', **FLT),
        'ionlevel': T('离子风暴 强度', '', **FLT),
        'ionground': T('离子风暴 地面', '', **FLT),
        'ionambient': T('离子风暴 环境光', '', **FLT),
        'dominatorred': T('心灵控制器 红', '尤里超武生效时的画面偏色', **FLT),
        'dominatorgreen': T('心灵控制器 绿', '', **FLT),
        'dominatorblue': T('心灵控制器 蓝', '', **FLT),
        'dominatorlevel': T('心灵控制器 强度', '', **FLT),
        'dominatorground': T('心灵控制器 地面', '', **FLT),
        'dominatorambient': T('心灵控制器 环境光', '', **FLT),
        'dominatorambientchangerate': T('心灵控制器 渐变速度', '', **FLT),
    },
    'map': {
        'size': T('地图尺寸', '格式 左,上,宽,高（格）。改这里不会重画地形，只会让整张图错位 —— 已锁定', type='text', **RO),
        'localsize': T('可见区域', '镜头可移动范围', type='text', **RO),
        'theater': T('地形主题', '决定地表贴图与可用装饰物。改错会花屏', type='enum',
                     opts=['TEMPERATE', 'SNOW', 'URBAN', 'DESERT', 'NEWURBAN', 'LUNAR']),
        'wood': T('木材密度', '0-100', **INT),
        'ice': T('冰面密度', '0-100', **INT),
        'water': T('水域密度', '0-100', **INT),
        'icegrowth': T('冰面生长', '', **YY),
        'icegrowthrate': T('冰面生长速度', '', **INT),
        'scrollmethod': T('滚动方式', '', **INT),
        'scrollrate': T('滚动速度', '', **INT),
        'newniformat': T('INI 格式版本', '', **INT, **RO),
    },
    'ranking': {
        'partimeeasy': T('简单难度标准时间', '格式 时:分:秒'),
        'partimemedium': T('普通难度标准时间', '格式 时:分:秒'),
        'partimehard': T('困难难度标准时间', '格式 时:分:秒'),
        'overpartitle': T('超时标题'),
        'underpartitle': T('达标标题'),
        'overparmessage': T('超时文字'),
        'underparmessage': T('达标文字'),
    },
}

# 推荐添加的隐藏属性（按段）
SUGGESTS = {
    'basic': [
        ('MultiplayerOnly', '0', '仅限多人 = 0', '改成 0 后这张图也会出现在单人/遭遇战列表里'),
        ('FreeRadar', 'yes', '免费雷达', '不用造雷达就能看全图'),
        ('IgnoreGlobalAITriggers', 'yes', '忽略全局 AI 触发', '自制图常用，避免被内置 AI 逻辑干扰'),
        ('TrainCrate', 'yes', '训练木箱', '打箱子掉奖励'),
        ('TruckCrate', 'yes', '部队木箱', '地图上散布木箱'),
        ('CarryOverMoney', 'yes', '继承资金', '任务图把资金带到下一关'),
        ('CarryOverCap', '10000', '继承上限', ''),
        ('PlayerKillCount', 'yes', '显示击杀数', ''),
        ('Timer.Inherit', 'yes', '计时器继承', ''),
        ('Percent', '100', '完成度', '编辑器里显示的完成百分比'),
        ('InitTime', '10000', '初始资金', '标准多人图 10000'),
        ('Official', 'no', '取消官方标记', ''),
    ],
    'specialflags': [
        ('FogOfWar', 'yes', '战争迷雾', '未探明区域黑幕覆盖（很影响手感，慎开）'),
        ('InitialVeteran', 'yes', '开局全老兵', '所有单位一出生就是老兵'),
        ('HarvesterImmune', 'yes', '矿车无敌', '防偷矿车'),
        ('MCVDeploy', 'yes', '允许基地车展开', ''),
        ('FixedAlliance', 'yes', '固定联盟', '禁止外交变更'),
        ('DestroyableBridges', 'yes', '桥梁可摧毁', ''),
        ('Meteorites', 'no', '陨石雨', ''),
        ('IonStorms', 'no', '离子风暴', ''),
        ('Visceroids', 'no', '生化怪', ''),
        ('TiberiumGrows', 'yes', '矿脉生长', ''),
        ('TiberiumSpreads', 'yes', '矿脉扩散', ''),
        ('TiberiumExplosive', 'no', '矿脉爆炸', ''),
    ],
    'lighting': [
        ('Red', '1.000000', '主光 红', '0-2，越大越偏红'),
        ('Green', '1.000000', '主光 绿', ''),
        ('Blue', '1.000000', '主光 蓝', ''),
        ('Level', '0.002000', '主光强度', ''),
        ('Ground', '0.000000', '地面光照', ''),
        ('Ambient', '1.010000', '环境光', '越大越亮'),
    ],
    'ranking': [
        ('ParTimeEasy', '00:10:00', '简单标准时间', '格式 时:分:秒'),
        ('ParTimeMedium', '00:15:00', '普通标准时间', ''),
        ('ParTimeHard', '00:20:00', '困难标准时间', ''),
    ],
}

HOUSE_SUGGESTS = [
    ('Credits', '10000', '初始资金', '多人对局里资金通常由大厅设置决定，这里改的是地图内置默认值'),
    ('Allies', '', '盟友', '结盟的玩家序号，例如 0,2'),
    ('TechLevel', '10', '科技等级', '1-10'),
    ('PercentBuilt', '100', '建造完成度', '任务图 AI 基地完成度'),
    ('PlayerControl', 'yes', '玩家控制', ''),
    ('IQ', '0', 'AI 智商', '0-100'),
    ('Edge', 'North', '出生边缘', 'North/South/East/West'),
    ('Base', 'no', '开局基地', ''),
]

HOUSE_NAMES = {
    'americans', 'russians', 'yuricountry', 'british', 'french', 'germans',
    'alliance', 'confederation', 'africans', 'arabs', 'gdi', 'nod', 'neutral', 'special',
}


# ---------------------------------------------------------------- 中文名称 / 帮助知识库
# 资料来源：飞书《重聚未来 1.5 开发者文档（编写中）》+ FinalAlert 2 地图 INI 惯例。
# 这里的内容会被：①属性编辑器每行显示中文名；②每行的 ⓘ 帮助；③顶栏「帮助」总菜单 共同使用。
SRC = '重聚未来 1.5 开发者文档'

# 参战方（国家）段的中文名：地图里以国家名作为段名，存放资金/结盟/科技等级
HOUSE_LABELS = {
    'americans': '美国', 'alliance': '韩国', 'africans': '利比亚', 'arabs': '伊拉克',
    'british': '英国', 'confederation': '古巴', 'french': '法国', 'germans': '德国',
    'russians': '苏联', 'yuricountry': '尤里', 'gdi': 'GDI 全球防御', 'nod': 'NOD 兄弟会',
    'neutral': '中立方', 'special': '特殊方',
}
HOUSE_HELP = {
    'americans': '盟军·美国，特色：空降伞兵', 'alliance': '盟军·韩国，特色：黑鹰战机',
    'africans': '苏军·利比亚，特色：自爆卡车', 'arabs': '苏军·伊拉克，特色：辐射工兵',
    'british': '盟军·英国，特色：狙击手', 'confederation': '苏军·古巴，特色：恐怖分子',
    'french': '盟军·法国，特色：巨炮', 'germans': '盟军·德国，特色：坦克杀手',
    'russians': '苏军·苏联，特色：磁能坦克', 'yuricountry': '尤里阵营（尤里的复仇）',
    'gdi': '泰伯利亚系列遗留段名，红警里通常不用', 'nod': '泰伯利亚系列遗留段名，红警里通常不用',
    'neutral': '中立/平民所属，地图上的中立建筑常用', 'special': '特殊所属，脚本或过场用',
}

# 键名 → 中文名。段内键名大小写不敏感，数字键（0/1/2…）单独走 PARAM_LABEL
FIELD_LABELS = {
    # 触发器 / 小队 / 脚本 / 动作参数块（编号段里最常见）
    'name': '名称', 'group': '分组编号', 'owner': '所属方', 'house': '所属方',
    'teamtype': '小队类型', 'scripttype': 'AI 脚本', 'taskforce': '特遣部队',
    'tag': '触发标签', 'trigger': '触发器', 'event': '触发事件', 'action': '触发动作',
    'waypoint': '路径点', 'repeating': '重复类型', 'enabled': '启用', 'disabled': '禁用',
    'side': '阵营', 'color': '颜色', 'start': '起始编号', 'count': '数量',
    'param': '参数', 'type': '类型', 'index': '序号', 'id': '编号',
    # MPMaps.ini 注册段（游戏大厅读的就是这些）
    'minplayers': '最少玩家', 'maxplayers': '最多玩家', 'description': '大厅显示名称',
    'author': '作者', 'enforcemaxplayers': '强制人数上限', 'size': '地图尺寸',
    'localsize': '可见区域', 'gamemodes': '可用游戏模式', 'briefing': '地图中央说明',
    'iscoopmission': '是否合作任务', 'disallowedplayerstarts': '禁用出生点',
    'disallowedplayersides': '禁用阵营', 'disallowedplayercolors': '禁用颜色',
    'forcedoptions': '强制游戏选项', 'uiname': '模式显示名',
    'mapcodeininame': '注入的地图 INI', 'text': '模式介绍',
    # 地图基础信息 / 常见键
    'gamemode': '游戏模式', 'percent': '完成度', 'inittime': '初始资金',
    'multiplayeronly': '仅限多人', 'official': '官方地图', 'freeradar': '免费雷达',
    'ignoreglobalaitriggers': '忽略全局 AI 触发', 'fixedseed': '固定随机种子',
    'traincrate': '训练木箱', 'truckcrate': '部队木箱', 'carryovermoney': '继承资金',
    'carryovercap': '继承资金上限', 'playerkillcount': '显示击杀数',
    'timer.inherit': '计时器继承', 'skipscore': '跳过计分', 'skipmapselect': '跳过选图',
    'endofgame': '结束电影', 'intro': '开场电影', 'brief': '任务简报', 'theme': '背景音乐',
    'nextscenario': '下一关地图', 'altnextscenario': '备用下一关',
    'newniformat': 'INI 格式版本', 'tutorial': '教学关', 'windowed': '窗口化',
    # 规则开关
    'fogofwar': '战争迷雾', 'mcvdeploy': '允许基地车展开', 'ionstorms': '离子风暴',
    'meteorites': '陨石雨', 'visceroids': '生化怪', 'fixedalliance': '固定联盟',
    'tiberiumgrows': '矿脉生长', 'tiberiumspreads': '矿脉扩散',
    'tiberiumexplosive': '矿脉爆炸', 'initialveteran': '开局全老兵',
    'harvesterimmune': '矿车无敌', 'destroyablebridges': '桥梁可摧毁', 'inert': '惰性模式',
    # 参战方 / 单位
    'credits': '初始资金', 'allies': '盟友', 'edge': '出生边缘', 'iq': 'AI 智商',
    'percentbuilt': '建造完成度', 'playercontrol': '玩家控制', 'techlevel': '科技等级',
    'base': '开局基地', 'nodecount': '节点数', 'country': '所属国家',
    # 光照
    'red': '主光 红', 'green': '主光 绿', 'blue': '主光 蓝', 'level': '主光强度',
    'ground': '地面光照', 'ambient': '环境光',
    'theater': '地形主题', 'wood': '木材密度', 'ice': '冰面密度', 'water': '水域密度',
    # 触发器 / 脚本 / 小队段内
    'partimeeasy': '简单标准时间', 'partimemedium': '普通标准时间', 'partimehard': '困难标准时间',
    'overpartitle': '超时标题', 'underpartitle': '达标标题',
    'digest': '文件校验值',
}

# 重点字段的详细帮助：desc 说明 / howto 怎么调 / rng 取值范围 / rec 推荐值
# / eg 示例 / warn 注意 / src 来源
def H(desc, howto='', rng='', rec='', eg='', warn='', src=''):
    return {'desc': desc, 'howto': howto, 'rng': rng, 'rec': rec,
            'eg': eg, 'warn': warn, 'src': src}


FIELD_HELP = {
    'name': H('这段/这个对象的名字。在编号参数块里通常是小队名、脚本名或触发器名',
              '随便改成好认的中文或英文，只影响编辑器里的可读性，不影响游戏逻辑',
              '任意文本，不要带逗号（很多参数块用逗号分隔字段）', '例如：第一波进攻小队',
              'Name=第一波犀牛坦克', '参数块里的名字不要和别的块重名，否则脚本引用可能指错', SRC),
    'group': H('分组编号，把多个小队/触发归到一组，AI 触发可以整组启用',
               '-1 = 不分组（最常见的默认值）；要分组就填 0、1、2…',
               '-1 或 0 以上的整数', '-1（保持不分组）', 'Group=-1',
               '乱改会让 AI 触发整组失效，除非你知道自己在配 AI，否则不要动', SRC),
    'gamemode': H('地图内部标记的游戏模式，DTA 客户端靠它判断这张图能不能在对应模式下用',
                  '多人图建议写 standard；留空在很多客户端里等于「所有模式都不可用」',
                  '模式标识，常用 standard', 'standard',
                  'GameMode=standard',
                  '★ 飞书文档明确：不写 GameMode，DTA 客户端默认所有模式不可用（2023 版相反，默认全可用）；客户端日志会报 Custom map X has no game modes!', SRC),
    'maxplayer': H('大厅里这张图允许的最大玩家数，也决定显示几个出生点',
                   '按地图实际出生点数量填，2 人图填 2，8 人图填 8；别填比出生点多',
                   '1-8', '与 [Waypoints] 里 0~7 出生点个数一致', 'MaxPlayer=8',
                   '飞书文档：DTA 客户端读取默认为 0，2023 版默认为路径点 0-7 的数量', SRC),
    'minplayer': H('大厅里这张图允许开局的最少玩家数', '一般填 2；合作任务可填 1',
                   '1-8', '2', 'MinPlayer=2', '飞书文档：DTA 客户端读取默认为 0', SRC),
    'multiplayeronly': H('是否只在多人对局出现',
                         '填 1 = 单人/遭遇战列表里看不到；填 0 = 都能看到。想让图在遭遇战里能选，就填 0',
                         '0 或 1', '0', 'MultiplayerOnly=0', '', SRC),
    'inittime': H('地图内置的初始资金默认值', '多人对局实际资金通常由大厅设置覆盖；改这里主要影响单机/任务图',
                  '非负整数', '10000', 'InitTime=10000', '', 'FA2 惯例'),
    'freeradar': H('免费雷达：不用造雷达也能看全图', '对战图建议 no，娱乐图可以 yes',
                   'yes / no', 'no', 'FreeRadar=no', '和 SkirmishLobby.ini 里的「免费全图」选项是两回事', SRC),
    'ignoreglobalaitriggers': H('忽略游戏内置的全局 AI 触发逻辑',
                                '自制图、任务图建议 yes，避免内置 AI 干扰；纯对战图保持 no',
                                'yes / no', 'yes', 'IgnoreGlobalAITriggers=yes', '', 'FA2 惯例'),
    'fixedseed': H('固定随机序列，每局的箱子、矿脉随机结果都一样',
                   '联机对战保持 no，否则每局都一模一样', 'yes / no', 'no', 'FixedSeed=no', '', 'FA2 惯例'),
    'fogofwar': H('战争迷雾（未探明区域黑幕覆盖）',
                  '红警原生不靠这个键做迷雾，多数客户端忽略；想开迷雾请在大厅勾「可扩散战争迷雾」',
                  'yes / no', 'no', 'FogOfWar=no', '飞书文档把 [SpecialFlags] 归为「特定标识，地编中可查看」；TS 遗留键，改了常常没效果', SRC),
    'mcvdeploy': H('基地车能否展开成建造厂', '关掉后玩家只能用地图预置的建筑，适合防守/解谜图',
                   'yes / no', 'yes', 'MCVDeploy=yes', '', 'FA2 惯例'),
    'harvesterimmune': H('矿车无敌，不会被打爆', '娱乐图、防偷矿车图开 yes；正规对战保持 no',
                         'yes / no', 'no', 'HarvesterImmune=yes', '', 'FA2 惯例'),
    'initialveteran': H('开局所有单位都是老兵等级', '娱乐图开 yes', 'yes / no', 'no',
                        'InitialVeteran=yes', '', 'FA2 惯例'),
    'fixedalliance': H('固定联盟：禁止游戏内改外交关系', '组队图开 yes 防拆队', 'yes / no', 'no',
                       'FixedAlliance=yes', '', 'FA2 惯例'),
    'tiberiumgrows': H('矿会随时间长回来', '长时间对战图开 yes 避免矿采光', 'yes / no', 'yes',
                       'TiberiumGrows=yes', '', 'FA2 惯例'),
    'tiberiumspreads': H('矿会向外蔓延', '和生长配合；不想要满地矿就 no', 'yes / no', 'yes',
                         'TiberiumSpreads=yes', '', 'FA2 惯例'),
    'tiberiumexplosive': H('矿被摧毁时会爆炸', '娱乐图可开，对战图保持 no', 'yes / no', 'no',
                           'TiberiumExplosive=no', '', 'FA2 惯例'),
    'ambient': H('环境光强度，越大整张图越亮、明暗对比越弱', '正常 1.0 左右；画面太黑调到 1.2，太灰调回 0.9',
                 '0.0 - 2.0', '1.010000', 'Ambient=1.010000', '一次性大幅调整容易过曝，建议每次改 0.1', SRC),
    'red': H('主光源的红色分量', '三个分量一起调：夜晚调低 Blue，黄昏调高 Red',
             '0.0 - 2.0', '1.000000', 'Red=1.000000', '', SRC),
    'green': H('主光源的绿色分量', '和 Red/Blue 一起调', '0.0 - 2.0', '1.000000', 'Green=1.000000', '', SRC),
    'blue': H('主光源的蓝色分量', '调高偏冷（夜晚/雪地），调低偏暖（沙漠/黄昏）',
              '0.0 - 2.0', '1.000000', 'Blue=1.000000', '', SRC),
    'level': H('主光源强度', '通常是个很小的值，别照着 1.0 填', '0.0 - 1.0', '0.002000',
               'Level=0.002000', '这个键和参战方段的 Level 含义不同，别混淆', SRC),
    'theater': H('地形主题，决定地表贴图、可用树木和建筑外观',
                 '改了不会重画地形，只会让已有地块贴图错乱；要换主题请重新建图',
                 'TEMPERATE / SNOW / URBAN / DESERT / NEWURBAN / LUNAR', 'TEMPERATE',
                 'Theater=TEMPERATE', '★ 改错会花屏，建议不动', SRC),
    'size': H('地图尺寸：左,上,宽,高（格）', '只改这个数字不会重画地形，会让整张图错位，已锁定',
              '四个整数', '保持原值', 'Size=0,0,100,100', '★ 已锁定；要改尺寸请用「绘制地图」重建', ''),
    'digest': H('地图内容的摘要校验值', '改过地图后这个值就失效了，个别客户端会提示地图不一致',
                '32 位十六进制', '保持不动', 'Digest=AB12...', '不要手改；提示不一致时重新导出地图即可', ''),
    # ---- MPMaps.ini 注册段（飞书文档「INI词条 / MPMaps.ini」）
    'description': H('游戏大厅地图列表里显示的名字', '想给朋友看的中文名写这里；注意它和文件名是两回事',
                     '任意文本', '与地图文件名一致的易读名字', 'Description=2人 冰天雪地',
                     '改这里不会改文件名；用右键「重命名」可以一次性同步', SRC),
    'minplayers': H('注册段里的最少玩家数（大厅读这个，不是地图 [Basic] 里的）',
                    '和地图 MaxPlayer 保持一致', '1-8', '2', 'MinPlayers=2', '', SRC),
    'maxplayers': H('注册段里的最多玩家数（大厅读这个）', '和地图 MaxPlayer 保持一致',
                    '1-8', '8', 'MaxPlayers=8', '', SRC),
    'author': H('作者名，显示在大厅地图信息里', '填自己的名字', '任意文本', '', 'Author=smblong', '', SRC),
    'gamemodes': H('这张图在哪些游戏模式下可用，多个用逗号分隔',
                   '用顶栏「游戏模式」或右键「移动到游戏模式」来改，别手敲模式名',
                   '已在 [GameModes] 注册的模式名', '标准对战', 'GameModes=常规作战,小队结盟',
                   '★ 写了没注册的模式名 = 这张图在游戏里不出现', SRC),
    'localsize': H('可见区域（镜头可移动范围）', '飞书文档标为「可见大小？」；通常照抄地图 [Map] 的 LocalSize',
                   '左,上,宽,高', '', 'LocalSize=0,0,100,100', '', SRC),
    'waypoint0': H('注册段里的 0 号出生点坐标', '格式 X*1000+Y；多人图 0~7 对应 8 个玩家位',
                   '合并坐标整数', '', 'Waypoint0=25080', '和地图 [Waypoints] 里的一致', SRC),
    'briefing': H('显示在地图中央的文字，多用于合作任务', '@ 可换行；普通对战图留空',
                  '任意文本', '', 'Briefing=守住基地@坚持 20 分钟', '飞书文档：@ 为换行，[重聚新增] 支持自动换行', SRC),
    'iscoopmission': H('是否为合作任务，只有 true 时某些合作任务字段才生效',
                       '普通对战图 false / 不写；合作任务填 true', 'true / false', 'false',
                       'IsCoopMission=true', '', SRC),
    'disallowedplayerstarts': H('合作任务专用：初始化电脑位置，禁用哪些出生点',
                                '多个用逗号分隔', '出生点序号', '', 'DisallowedPlayerStarts=0,1', '', SRC),
    'disallowedplayersides': H('合作任务专用：禁用阵营', '多个用逗号分隔', '阵营序号', '', '', '', SRC),
    'disallowedplayercolors': H('合作任务专用：禁用颜色', '多个用逗号分隔', '颜色序号', '', '', '', SRC),
    'forcedoptions': H('强制游戏选项控制', '指向一个「指定的游戏选项控制」段，里面写 游戏选项=值',
                       '段名或选项串', '', 'ForcedOptions=NoSuperweapons', '飞书文档：SkirmishLobby.ini 里也有同名字段', SRC),
    'uiname': H('游戏模式在大厅里显示的名字', '改这里等于改游戏里看到的模式名称',
                '翻译键或文本', '', 'UIName=标准对战', '顶栏「游戏模式」可以可视化改名并自动同步', SRC),
    'mapcodeininame': H('进入这个模式时注入地图的 INI 文件', '指向 INI/Map Code/ 下的文件',
                        '文件名', '', 'MapCodeININame=MapCode_Standard.ini', '', SRC),
    'text': H('游戏模式的介绍文字', '鼠标悬停在模式上会显示', '任意文本', '', 'Text=常规 1v1 对战', '', SRC),
}

# 段专属帮助：同一个键在不同段里含义不同时优先用它
FIELD_HELP_BY_SEC = {
    ('basic', 'name'): H('地图内部名称，地图编辑器里显示的名字',
                         '想改大厅里显示的名字，改的是 MPMaps.ini 注册段的 Description；用右键「重命名」可以一起改',
                         '任意文本', '', 'Name=冰天雪地', '和文件名、注册段名是三个东西', SRC),
    ('basic', 'author'): H('地图作者名（写在地图文件里）',
                           '大厅显示的作者来自注册段 Author，两处可以不一致', '任意文本', '', 'Author=smblong', '', SRC),
    ('header', 'name'): H('旧格式地图头里的地图名', '老地图才用，一般不用改', '', '', '', '', ''),
    ('waypoints', '0'): H('0 号玩家出生点，格式 X*1000+Y',
                          '用「绘制地图」的出生点工具改，别手算坐标', '合并坐标', '', '0=25080 → X=25,Y=80',
                          '改错会让玩家出生在地图外', SRC),
}

# 数字编号键（0/1/2…）在各类段里的含义
PARAM_LABEL = '参数行'
PARAM_HELP = H(
    '编号参数行：键是序号，值才是真正的内容，格式由所在段决定',
    '照着同段其它行的格式改，逗号分隔的字段一个都不能少',
    '见对应段的段帮助', '保持原有字段个数', '0=1,HTNK,256,50,50,0,0,0,3,0',
    '★ 这是地图的二进制化参数，改错字段会让单位/触发直接失效；不确定就先备份', SRC)

SECTION_LABELS = dict(HOUSE_LABELS)
SECTION_LABELS.update({
    'actions': '触发动作', 'events': '触发事件', 'triggers': '触发器',
    'tags': '触发标签', 'taskforces': '特遣部队', 'teamtypes': '作战小队',
    'scripttypes': 'AI 脚本', 'aitriggertypes': 'AI 触发',
    'aitriggertypesenable': 'AI 触发开关', 'variablenames': '变量名',
    'houses': '参战方列表', 'countries': '国家定义', 'sides': '阵营定义',
    'colors': '颜色定义', 'units': '载具', 'infantry': '步兵', 'aircraft': '飞行器',
    'structures': '建筑', 'terrain': '地形物件', 'smudge': '地表污迹',
    'overlay': '地表覆盖', 'waypoints': '出生点与路径点',
    'basic': '地图基础信息', 'specialflags': '特殊规则开关',
    'lighting': '光照与画面色调', 'map': '地形与地图参数', 'ranking': '战役评级参数',
    'header': '旧格式头信息', 'digest': '文件校验值',
    'isomappack5': '地形数据（二进制）', 'overlaypack': '覆盖物数据（二进制）',
    'overlaydatapack': '覆盖物附加数据（二进制）', 'previewpack': '内嵌预览图（二进制）',
    'preview': '内嵌预览（旧格式）', 'mappack': '地图打包数据（二进制）',
    'fa2spversioncontrol': '编辑器版本标记',
})

# 段帮助：飞书文档「INI词条 / 地图」章节逐段拆解
SECTION_HELP = {
    'basic': H('地图信息。飞书文档说「地编中可查看一部分」，剩下的隐藏项就在这里改。',
               '重点看 MaxPlayer / MinPlayer / MultiplayerOnly / GameMode 四个键',
               '', '', '', '★ GameMode 不写，DTA 客户端默认所有模式不可用', SRC),
    'specialflags': H('特定标识，地编中可查看。控制战争迷雾、矿车无敌、联盟锁定等隐藏规则。',
                      '绝大多数用 yes / no 开关，按需打开，别一口气全开',
                      'yes / no', '', '', 'FogOfWar 在红警里基本无效，别指望它开迷雾', SRC),
    'ranking': H('单人图中右边显示的时间、评价文字；地编中可查看。',
                 'ParTimeEasy / Medium / Hard 用 时:分:秒；多人图不用管',
                 '时:分:秒', '00:10:00 / 00:15:00 / 00:20:00', '', '', SRC),
    'houses': H('所属方列表：格式 序号=国家名；玩家的资金、结盟、科技等级写在以国家名命名的段里。',
                '看这张图有哪些玩家，就数这里几行', '', '', '', '', SRC),
    'lighting': H('地图光照，地编可设置。控制整张图的色调、亮度与尤里心灵控制器生效时的偏色。',
                  'Red/Green/Blue 三色 + Ambient/Level 一起调，每次小改一点',
                  '0.0 - 2.0', '', '', '', SRC),
    'preview': H('飞书文档标为「地图预览大小?」，旧格式的内嵌预览。', '', '', '', '',
                 '二进制段，不要手改', SRC),
    'waypoints': H('路径点。格式 编号=X*1000+Y；0~7 是玩家出生点，其余是脚本路径点。',
                   '想改出生点位置，用「绘制地图」里的出生点工具，别手算坐标',
                   '合并坐标', '', '0=25080 → X=25, Y=80', '改错会让玩家出生在地图外', SRC),
    'map': H('地图大小与地形主题。', 'Size / LocalSize 已锁定，改了会整图错位',
             '', '', '', '★ 要改尺寸请重新建图', SRC),
    'structures': H('地图上的所有单位/建筑信息（飞书文档原话）。',
                    '格式 序号=所属,建筑ID,血量,X,Y,朝向,任务,标签,老兵等级,编队',
                    '', '', '', '字段顺序错一个，建筑就会消失或归属错误', SRC),
    'aitriggertypesenable': H('AI 触发允许情况，可在地编里设置。', '', '', '', '', '', SRC),
    'tags': H('触发标签，每个触发都有并应有标签。格式：触发标签 = 触发重复类型,触发名,标签',
              '', '', '', '', '标签缺失的触发通常不会执行', SRC),
    'taskforces': H('特遣部队：定义小队里包含哪些单位。', '格式 序号=数量,单位ID', '', '', '', '', SRC),
    'teamtypes': H('作战小队：把特遣部队、脚本、分组编号串起来。',
                   '常见键 Name=小队名、Group=-1、ScriptType、TaskForce', '', '', '', '', SRC),
    'scripttypes': H('脚本：这一栏只写每个脚本的脚本标签，具体行为要去脚本标签那一节看。',
                     '', '', '', '', '', SRC),
    'triggers': H('触发选项。格式 标签 = 所属,关联触发标签,触发名,禁止否,简单否,中等否,困难否',
                  '', '', '', '', '', SRC),
    'events': H('触发条件。格式 标签 = 条件数量,第一个条件类型,第一个条件参数,第二个…',
                '', '', '', '', '注意：条件参数可能有多个，也就是多个逗号', SRC),
    'actions': H('触发结果。格式 标签 = 结果数量,第一个结果类型,第一个结果参数,第二个…',
                 '', '', '', '', '注意：结果参数可能有多个，也就是多个逗号', SRC),
    'units': H('地图预置的载具。', '格式 序号=归属,单位ID,血量(0-256),X,Y,朝向(0-255),任务,标签,老兵(0-3),编队',
               '', '', '', '', SRC),
    'infantry': H('地图预置的步兵。', '格式同载具', '', '', '', '', SRC),
    'terrain': H('地形物件：树、石头等装饰。格式 序号=物件ID,X,Y', '', '', '', '', '', SRC),
    'overlay': H('地表覆盖：矿、路等。格式 序号=覆盖物ID,X,Y', '', '', '', '',
                 '矿的覆盖物 ID 随 Mod 变化，本程序的「绘制地图」会自动识别', SRC),
    'digest': H('地图内容摘要值，改动地图后失效。', '', '', '', '', '不要手改', SRC),
}

# 总帮助：把飞书文档按使用主题重新编排成可查的章节
GUIDE = [
    {'id': 'start', 'title': '一、先搞清楚三个「目录」', 'icon': '📁',
     'lead': '很多人「图导进去了但游戏里找不到」，都是因为搞混了物理目录、游戏模式和注册表。',
     'parts': [
         {'h': '游戏目录里跟地图有关的文件夹',
          'p': '飞书文档《文件目录（部分）》章节摘录，程序也是按这个结构找地图的：',
          'table': {'cols': ['路径', '作用'], 'rows': [
              ['Maps/', '存放所有遭遇战地图'],
              ['Maps/Custom', '存放所有第三方多人地图；Map 格式地图放这里'],
              ['INI/Map Code/', '游戏模式所用的 INI（模式要注入地图的配置）'],
              ['INI/MPMaps.ini', '管理多人地图与游戏模式的主注册表'],
              ['INI/MPMaps*.ini', '其它注册表（ddly / MadHQ 等），客户端会一并扫描'],
              ['Client/custom_map_cache', '第三方地图的缓存信息，用记事本能看出端倪'],
              ['Client/client.log', '客户端运行日志，每次运行会清空上一次的'],
              ['FinalAlert2SP/', '地图编辑器；地编提示「游戏安装不正确」时删掉 FinalAlert.ini 重新指定'],
              ['Resources/SkirmishLobby.ini', '遭遇战界面与游戏选项（多人/局域网另有对应 ini）'],
          ]}},
         {'h': '地图该放哪',
          'bullets': [
              '飞书文档原话：Map 格式地图放在 Maps/Custom 里。',
              '[重聚新增] 地图格式 (yrm, map, mpr) 放根目录即可。',
              '本程序把图统一放进游戏目录的 Maps 下（默认 Maps/Custom，也可以选 ddly、MadHQ 等子目录）。',
              '★ 客户端只在启动时扫描地图和 MPMaps*.ini，所以导入/改模式后必须退出游戏大厅再打开。',
          ]},
         {'h': '三个概念别混',
          'table': {'cols': ['概念', '在哪', '本程序里对应'], 'rows': [
              ['物理目录', 'Maps/ 下的文件夹', '左侧「目录」栏：Custom / Standard / ddly / MadHQ'],
              ['游戏模式', 'INI/MPMaps.ini 的 [GameModes]', '左侧「游戏模式」栏 + 顶栏「游戏模式」'],
              ['地图注册', 'INI/MPMaps*.ini 的 [MultiMaps]', '导入时「自动补注册」、右键「移动到游戏模式」'],
          ]}},
     ]},
    {'id': 'mode', 'title': '二、游戏模式与地图注册', 'icon': '◈',
     'lead': '飞书文档：「MPMaps.ini 这个 ini 是用来管理多人地图和游戏模式的。」',
     'parts': [
         {'h': '[GameModes] / [游戏模式]',
          'p': '游戏模式在 [GameModes] 里注册，每个模式自己的段里写这些字段：',
          'table': {'cols': ['字段', '含义', '怎么调'], 'rows': [
              ['UIName', '游戏模式名称', '想让游戏里显示什么就写什么；顶栏「游戏模式」可可视化改名'],
              ['MapCodeININame', '注入地图内的 ini', '指向 INI/Map Code/ 下的文件'],
              ['Text', '游戏模式介绍', '悬停提示文字'],
              ['ForcedOptions', '模式指定的游戏选项控制', '写「游戏选项=值」，见 SkirmishLobby.ini'],
          ]}},
         {'h': '[MultiMaps] / [多人地图]',
          'p': '多人地图在 [MultiMaps] 里注册，下面是飞书文档列出的字段与本程序的对应功能：',
          'table': {'cols': ['字段', '含义', '本程序怎么做'], 'rows': [
              ['MinPlayers / MaxPlayers', '最小 / 最大人数', '补注册时从地图 [Basic] 自动带出'],
              ['Description', '显示在客户端地图列表的地图名称', '右键「重命名」可同步'],
              ['Author', '作者', '补注册时留空，可自行填'],
              ['Size / LocalSize', '地图大小 / 可见大小？', '从地图 [Map] 复制'],
              ['GameModes', '什么游戏模式可用', '右键「移动到游戏模式」就是改这一项'],
              ['Waypoint*', '路径点', '从地图 [Waypoints] 复制'],
              ['Briefing', '显示在地图中央的文字，多用于合作任务', '合作图才填，@ 换行'],
              ['IsCoopMission', '是否为合作任务，只有 true 时某些合作字段才生效', '合作图填 true'],
              ['DisallowedPlayerStarts / Sides / Colors', '合作任务专用：初始化电脑位置/阵营/颜色', '多个用逗号隔开'],
              ['EnforceMaxPlayers', '飞书文档作者标为「忘了」', '保持默认'],
          ]}},
         {'h': '最容易踩的坑',
          'warn': '★ [Basic] GameMode 如不写，DTA 客户端默认所有模式不可用（2023 版相反，默认全可用）。客户端日志会写 Custom map X has no game modes!。本程序导入时默认勾选「补写 [Basic] GameMode」就是为这个。',
          'bullets': [
              '一张图可以同时属于多个模式（GameModes= 用逗号分隔），所以它和物理目录不是一一对应。',
              '写了没在 [GameModes] 注册的模式名 = 这张图在游戏里不出现。',
              '删除地图时，本程序会同步清掉 MPMaps*.ini 里的注册项；从回收站还原时写回去。',
              '所有 INI 改动前自动备份到 数据\\配置备份\\。',
          ]},
     ]},
    {'id': 'attr', 'title': '三、属性编辑器 · 段速查', 'icon': '⚙',
     'lead': '飞书文档《INI词条 / 地图》章节逐段拆解。进入「编辑属性 / 隐藏项」后，鼠标点每段的 ⓘ 也能看到。',
     'parts': [
         {'h': '地图 INI 里都有什么段',
          'table': {'cols': ['段', '作用（飞书文档）', '动不动'], 'rows': [
              ['[Basic]', '地图信息，地编中可查看一部分', '★ 常改：人数、GameMode、仅限多人'],
              ['[SpecialFlags]', '特定标识，地编中可查看', '按需开隐藏规则'],
              ['[Ranking]', '单人图右边的时间等信息', '只做战役图时改'],
              ['[Houses]', '所属方，地编中可查看', '一般不动'],
              ['[Lighting]', '地图光照，地编可设置', '调画面色调'],
              ['[Preview]', '地图预览大小?', '不动'],
              ['[Waypoints]', '路径点', '用绘制工具的出生点，别手算'],
              ['[Map]', '地图大小', 'Size 已锁定，别改'],
              ['[Triggers] / [Events] / [Actions]', '触发选项 / 触发条件 / 触发结果', '格式见各段 ⓘ，参数可能有多个逗号'],
              ['[Tags] / [TaskForces] / [TeamTypes]', '触发标签 / 特遣部队 / 作战小队', '每个触发都应有标签'],
              ['[AITriggerTypesEnable]', 'AI 触发允许情况', '地编里设置'],
          ]}},
     ]},
    {'id': 'howto', 'title': '四、常用参数怎么调', 'icon': '🎛',
     'lead': '把「调成什么」直接写在这里；每个键的详细版点属性行旁边的 ⓘ。',
     'parts': [
         {'h': '让导入的图能在游戏里被看到',
          'bullets': [
              '第 1 步：导入时勾「自动补注册到游戏模式」（默认已勾）。',
              '第 2 步：勾「补写地图 [Basic] GameMode」（默认已勾）。',
              '第 3 步：注册表按目录自动选：ddly → MPMapsddly.ini，MadHQ → MPMapsMadHq.ini，其余 → MPMaps.ini。',
              '第 4 步：关掉游戏大厅重新打开（客户端启动时才扫描）。',
              '还看不到：打开「游戏模式」弹窗看「未注册地图」，一键补注册；必要时清一下 Client/custom_map_cache。',
          ]},
         {'h': '常用值速查',
          'table': {'cols': ['想达到的效果', '改哪个键', '填什么'], 'rows': [
              ['让图在遭遇战/单人列表出现', 'Basic / MultiplayerOnly', '0'],
              ['让图在所有模式可用', 'Basic / GameMode', 'standard'],
              ['大厅显示 8 人', 'Basic / MaxPlayer + 注册段 MaxPlayers', '8'],
              ['不用造雷达看全图', 'Basic / FreeRadar', 'yes'],
              ['防开局被偷矿车', 'SpecialFlags / HarvesterImmune', 'yes'],
              ['禁止游戏内改联盟', 'SpecialFlags / FixedAlliance', 'yes'],
              ['矿采光后能长回来', 'SpecialFlags / TiberiumGrows', 'yes'],
              ['画面太黑', 'Lighting / Ambient', '1.0 → 1.2'],
              ['夜晚冷色调', 'Lighting / Blue', '1.0 → 1.3'],
              ['自制任务图不被内置 AI 干扰', 'Basic / IgnoreGlobalAITriggers', 'yes'],
          ]}},
         {'h': '不建议手改的',
          'warn': '[Map] Size / LocalSize、Theater、Digest、所有二进制段（IsoMapPack5 / OverlayPack / PreviewPack）都不要手改。改尺寸请用「绘制地图」重建；刷矿、摆出生点、压平高度也都在绘制工具里做。',
          'bullets': ['参数块（编号段）里的 0=1,HTNK 这种行，逗号字段一个都不能少，改之前先备份。',
                      '保存前程序会自动备份原文件到 数据\\地图备份\\，改错了可以拿回来。']},
     ]},
    {'id': 'ops', 'title': '五、日常操作', 'icon': '🖱',
     'lead': '本程序自己的功能，配合上面的机制一起用。',
     'parts': [
         {'h': '导入 / 归类 / 重命名 / 删除',
          'bullets': [
              '导入：把 yrm/map/mpr/zip/rar/7z 拖进窗口，或点「导入地图」；同名可选跳过 / 自动改名 / 覆盖。',
              '右键地图：移动到游戏模式、移动到目录、重命名、查看详情、勾选、删除到回收站。',
              '批量：开「批量选择」后勾选多张，右键其中一张就是批量操作；选择栏也有「移动到…」。',
              '重命名：可同时改文件名、地图 [Basic] Name、注册段 Description 和 [MultiMaps] 路径。',
              '删除：文件进回收站，MPMaps*.ini 的注册项一起清掉；回收站还原时注册项写回去。',
          ]},
         {'h': '预览与绘制',
          'bullets': [
              '预览图用游戏自带的 CNCMaps 渲染器生成，不进游戏就能看图。',
              '「绘制地图」：刷矿/刷宝石/橡皮/摆出生点/压平高度；「⌖ 检查」点格子看地形、高度、覆盖物、出生点、建筑/单位/步兵。',
              '新建地图：顶栏「新建地图」，设尺寸、主题、玩家数、初始矿田后直接进画板。',
          ]},
         {'h': '游戏目录',
          'bullets': [
              '点顶栏那条路径打开「选择游戏目录」；「浏览添加」会弹系统文件夹窗口，不用再复制路径。',
              '不需要的目录点条目右边的 ✕ 移除（只从列表移除，不会删硬盘上的文件）。',
              '程序也会自动扫描各盘找含 Resources\\ClientDefinitions.ini + gamemd.exe 的目录。',
          ]},
     ]},
    {'id': 'pack', 'title': '六、打包发给朋友', 'icon': '📦',
     'lead': '导出的是 zip：manifest.json + maps/相对路径 + 模式定义 + 注册条目。',
     'parts': [
         {'h': '导出',
          'bullets': ['顶栏「导出地图包」→ 选地图（勾选的或全部）→ 勾上「包含游戏模式定义」「包含注册表条目」。',
                      '不勾这两个，朋友那边只会有一堆散图，游戏里看不到。']},
         {'h': '朋友那边导入',
          'bullets': ['把这个 zip 直接拖进「导入地图」，程序自动识别地图包格式。',
                      '依次完成：地图落盘 → 合并缺失的游戏模式 → 合并注册条目。',
                      '包里全部用相对路径，不写死盘符，游戏装在 D 盘 E 盘都能装。',
                      '装完让朋友退出游戏大厅重新打开。']},
     ]},
    {'id': 'faq', 'title': '七、常见问题', 'icon': '❓',
     'lead': '按现象查。',
     'parts': [
         {'h': 'Q&A',
          'table': {'cols': ['现象', '原因 / 处理'], 'rows': [
              ['刚导入的图游戏里找不到', '① 忘了点「开始导入」；② 没补注册；③ 没写 [Basic] GameMode；④ 游戏大厅没重启'],
              ['游戏里的模式和管理器目录对不上', '两个维度：目录是物理文件夹，模式是 MPMaps.ini 里的逻辑分类，一张图能属于多个模式'],
              ['模式名改了但游戏里还是旧的', '退出游戏大厅重新打开；客户端只在启动时读 MPMaps*.ini'],
              ['预览图错位/发黑', '换张图重新生成预览；或清 数据\\预览缓存\\ 后刷新'],
              ['地编提示「游戏安装不正确」', '飞书文档：删掉 FinalAlert2SP\\FinalAlert.ini，打开 FinalAlert2SP.exe 重新指定游戏'],
              ['地图中文名乱码', '地图是 GBK 编码，本程序按 GBK 读写；不要用记事本另存为 UTF-8'],
              ['改完参数游戏没反应', '确认改的段是客户端真正读的那个（大厅读 MPMaps.ini 注册段，不是地图 [Basic]）'],
          ]}},
     ]},
    {'id': 'src', 'title': '八、资料来源', 'icon': '📖',
     'lead': '帮助内容主要来自飞书《重聚未来 1.5 开发者文档（编写中）》，按本程序的功能重新拆解编排。',
     'parts': [
         {'h': '飞书文档要点',
          'bullets': ['游戏基于尤里的复仇 1.001，扩展平台默认为 Ares3 和 Phobos37。',
                      '单人地图必须使用 map 格式。',
                      'rules_custom.ini / art_custom.ini 用来改游戏数据。',
                      'Resources/SkirmishLobby.ini 管遭遇战界面；[cmbGame] 控件名硬编码，不要改名。',
                      'expandmd97.mix 是 Mod 数值文件，客户端靠更换它切换简单 Mod/任务包。']},
         {'h': '文档里给的延伸资料',
          'bullets': ['红警地图指导：https://ra2map.github.io/',
                      '红警DIY论坛：http://bbs.ra2diy.com/',
                      'DTA 客户端中文翻译文件：https://github.com/KuroNoSeiHai/Translation.ini',
                      '重聚2023 1.3 平台源码：https://github.com/Mr-188/Reunion-2023'],
          'warn': '使用资源时请标注原作者。'},
     ]},
]

def is_num_section(name):
    return bool(_NUM_SECTION.match(name or ''))


def field_label(sec_name, key):
    """键的中文名：优先原 DOCS，再查通用表，数字键走参数行"""
    d = field_doc(sec_name, key)
    if d.get('label'):
        return d['label']
    k = (key or '').strip().lower()
    if k.isdigit():
        if (sec_name or '').lower() == 'waypoints':
            n = int(k)
            return ('玩家出生点 %d' % n) if n < 8 else ('路径点 %d' % n)
        return '%s %s' % (PARAM_LABEL, k)
    if k in FIELD_LABELS:
        return FIELD_LABELS[k]
    # Waypoint0 / Waypoint1 … 这类带编号的键
    m = re.match(r'^(waypoint|param|unit|action|event|condition)(\d+)$', k)
    if m and m.group(1) in FIELD_LABELS:
        return '%s %s' % (FIELD_LABELS[m.group(1)], m.group(2))
    return ''


def field_help(sec_name, key, value=''):
    """给前端 ⓘ 用的完整帮助对象"""
    low = (sec_name or '').lower()
    k = (key or '').strip().lower()
    label = field_label(sec_name, key)
    d = field_doc(sec_name, key)
    h = dict(FIELD_HELP_BY_SEC.get((low, k)) or FIELD_HELP.get(k) or {})
    if k.isdigit() and is_num_section(sec_name):
        h = dict(PARAM_HELP)
    # [Waypoints] 里的数字键是出生点/路径点坐标
    if low == 'waypoints' and k.isdigit() and not h:
        n = int(k)
        h = {'desc': '%s：%s，格式 X*1000+Y' % (
                 '玩家出生点 %s' % n if n < 8 else '脚本路径点 %s' % n,
                 '大厅里第 %d 个玩家从这里出现' % (n + 1) if n < 8 else 'AI 脚本/触发引用的路径点'),
             'howto': '用「绘制地图」的出生点工具改，不要手算坐标', 'rng': '合并坐标',
             'rec': '', 'eg': '0=25080 → X=25, Y=80',
             'warn': '坐标写错会让玩家或 AI 跑到地图外', 'src': SRC}
    # 带编号的键复用基础帮助
    m = re.match(r'^(waypoint|param|unit|action|event|condition)(\d+)$', k)
    if not h and m and m.group(1) in FIELD_HELP:
        h = dict(FIELD_HELP[m.group(1)])
    desc = h.get('desc') or d.get('tip') or ''
    if not desc:
        desc = '原始键 %s：地图 INI 里的一个参数，含义由它所在的段决定。' % key
        if is_num_section(sec_name):
            desc = '编号参数块里的第 %s 行参数；格式由引用它的 [Actions]/[TeamTypes]/[ScriptTypes] 决定。' % key
    sec_h = section_help(sec_name).get('desc') or ''
    return {
        'key': key, 'label': label or key, 'section': sec_name,
        'sectionLabel': section_label(sec_name),
        'desc': desc, 'howto': h.get('howto', ''), 'rng': h.get('rng', ''),
        'rec': h.get('rec', ''), 'eg': h.get('eg', ''), 'warn': h.get('warn', ''),
        'src': h.get('src', '') or (SRC if desc else ''),
        'sectionDesc': sec_h, 'value': value,
        'known': bool(h or d.get('label') or label),
    }


def section_label(name, cur=''):
    if cur:
        return cur
    n = (name or '').lower()
    if n in HOUSE_LABELS:
        return '%s (%s)' % (HOUSE_LABELS[n], name)
    if is_num_section(name):
        return '触发器参数块'
    return SECTION_LABELS.get(n, '')


def section_help(name, cur_label='', cur_tip=''):
    low = (name or '').lower()
    h = dict(SECTION_HELP.get(low) or {})
    if low in HOUSE_LABELS:
        h = {'desc': '%s：以国家名命名的参战方段。飞书文档：玩家的资金、结盟、科技等级写在以国家名命名的段里。' % HOUSE_LABELS[low],
             'howto': '常用键 Credits（初始资金）、Allies（盟友）、TechLevel（科技等级）、IQ（AI 智商）、Edge（出生边缘）',
             'rng': '', 'rec': '', 'eg': 'Credits=10000', 'warn': '',
             'src': SRC, 'extra': HOUSE_HELP.get(low, '')}
    elif is_num_section(name):
        h = {'desc': '[%s] 是编号参数块：被 [Actions] / [TeamTypes] / [ScriptTypes] 里的动作引用的参数。' % name,
             'howto': '键是序号（0、1、2…），值是逗号分隔的参数；Name= 是名称，Group=-1 表示不分组。',
             'rng': '', 'rec': '', 'eg': '0=1,HTNK', 'warn': '★ 逗号字段一个都不能少，改错会让触发/单位失效；改前先备份。',
             'src': SRC}
    desc = h.get('desc') or cur_tip or ''
    if not desc:
        desc = '段 [%s]：地图 INI 里的一个配置段。每行是一个 键=值 参数。' % name
    return {
        'name': name, 'label': cur_label or section_label(name),
        'desc': desc, 'howto': h.get('howto', ''), 'rng': h.get('rng', ''),
        'rec': h.get('rec', ''), 'eg': h.get('eg', ''), 'warn': h.get('warn', ''),
        'src': h.get('src', ''), 'extra': h.get('extra', ''),
        'known': bool(h),
    }


def help_guide():
    return {'title': '使用帮助', 'subtitle': '按《%s》拆解编排' % SRC,
            'source': SRC, 'chapters': GUIDE,
            'fieldIndex': sorted([{'k': k, 'label': v} for k, v in FIELD_LABELS.items()],
                                 key=lambda x: x['k'])}


def help_search(q):
    """帮助搜索：章节标题 / 正文 / 字段中文名 / 键名"""
    q = (q or '').strip().lower()
    if not q:
        return []
    out = []
    for g in GUIDE:
        blob = str(g).lower()
        if q in blob:
            out.append({'type': 'chapter', 'id': g['id'], 'title': g['title'],
                        'hit': '章节内容匹配「%s」' % q})
    for k, v in FIELD_LABELS.items():
        if q in k or q in v:
            h = FIELD_HELP.get(k) or {}
            out.append({'type': 'field', 'key': k, 'label': v,
                        'hit': (h.get('desc') or '')[:80]})
    for k, v in SECTION_LABELS.items():
        if q in k or q in v:
            out.append({'type': 'section', 'key': k, 'label': v, 'hit': '配置段'})
    return out[:60]


def field_doc(sec_name, key):
    low = sec_name.lower()
    k = key.lower()
    if low in DOCS and k in DOCS[low]:
        return dict(DOCS[low][k])
    if k in COMMON:
        return dict(COMMON[k])
    return {}


def suggests_for(sec_name):
    low = sec_name.lower()
    if low in SUGGESTS:
        return [{'k': k, 'v': v, 'label': lb, 'tip': tp} for k, v, lb, tp in SUGGESTS[low]]
    if low in HOUSE_NAMES:
        return [{'k': k, 'v': v, 'label': lb, 'tip': tp} for k, v, lb, tp in HOUSE_SUGGESTS]
    return []


def value_hint(sec_name, key, value):
    """给值补一条人类可读的解释（不改值本身）"""
    low = sec_name.lower()
    k = key.lower()
    if low == 'waypoints':
        try:
            n = int(value)
            x, y = n // 1000, n % 1000
            tag = '玩家出生点' if k.isdigit() and int(k) < 8 else '路径点'
            return '%s　坐标 X=%d, Y=%d' % (tag, x, y)
        except Exception:
            return ''
    if k == 'maxplayer' or k == 'minplayer':
        return ''
    if low == 'digest':
        return '内容摘要（改动地图后此值会失效）'
    if k in ('alls', 'allies') and value:
        return '与玩家序号 %s 结盟' % value
    return ''


# ---------------------------------------------------------------- 文件对象
class MapIni:
    def __init__(self, path):
        self.path = path
        with open(path, 'rb') as f:
            self.raw = f.read()
        self.original_size = len(self.raw)
        self.lines = self.raw.split(b'\n')
        self.sections = []
        self._parse()

    # ---- 解析
    def _parse(self):
        self.sections = []
        for i, ln in enumerate(self.lines):
            m = _SECTION_RE.match(ln)
            if m:
                self.sections.append({
                    'name': dec(m.group(1)).strip(),
                    'start': i,
                    'end': len(self.lines),
                })
        for i, s in enumerate(self.sections):
            if i + 1 < len(self.sections):
                s['end'] = self.sections[i + 1]['start']
        cnt = {}
        for s in self.sections:
            n = s['name'].lower()
            s['occ'] = cnt.get(n, 0)
            cnt[n] = s['occ'] + 1

    def find(self, name, occ=0):
        for s in self.sections:
            if s['name'].lower() == name.lower() and s['occ'] == occ:
                return s
        return None

    def keys_of(self, sec):
        out = []
        for i in range(sec['start'] + 1, sec['end']):
            kv = split_kv(self.lines[i])
            if kv:
                out.append({'k': kv[0], 'v': kv[1], 'line': i})
        return out

    # ---- 结构化输出（给前端）
    def info(self):
        out = []
        for idx, s in enumerate(self.sections):
            low = s['name'].lower()
            meta = dict(SECTION_META.get(low, {}))
            if _NUM_SECTION.match(s['name']) and not meta:
                meta = {'label': '触发器参数块', 'tip': '被 [Actions] 里的动作引用的参数，格式 序号=参数值'}
            elif not meta:
                meta = {'label': '', 'tip': ''}
            binary = low in BIN_SECTIONS
            sec_label = section_label(s['name'], meta.get('label', ''))
            sec_doc = section_help(s['name'], sec_label, meta.get('tip', ''))
            keys = []
            if not binary:
                for kv in self.keys_of(s):
                    d = field_doc(s['name'], kv['k'])
                    fdoc = field_help(s['name'], kv['k'], kv['v'])
                    lb = d.get('label') or field_label(s['name'], kv['k'])
                    keys.append({
                        'k': kv['k'],
                        'v': kv['v'],
                        'label': lb or '其它参数',
                        'known': bool(lb),
                        'tip': d.get('tip') or (fdoc.get('howto') if fdoc.get('known') else ''),
                        'doc': fdoc,
                        'type': d.get('type', 'text'),
                        'opts': d.get('opts', []),
                        'min': d.get('min'),
                        'max': d.get('max'),
                        'ro': bool(d.get('ro')),
                        'hint': value_hint(s['name'], kv['k'], kv['v']),
                    })
            out.append({
                'idx': idx,
                'name': s['name'],
                'occ': s['occ'],
                'label': sec_label,
                'tip': meta.get('tip') or (sec_doc.get('desc') if sec_doc.get('known') else ''),
                'doc': sec_doc,
                'kind': ('bin' if binary else
                         'core' if low in ('basic', 'specialflags', 'lighting', 'map', 'ranking', 'waypoints') else
                         'house' if low in HOUSE_NAMES else 'other'),
                'binary': binary,
                'lines': s['end'] - s['start'] - 1,
                'keys': keys,
                'suggests': [] if binary else suggests_for(s['name']),
            })
        return out

    def raw_text(self):
        """用于"原文查看"：只读，方便核对"""
        return dec(self.raw)

    # ---- 编辑
    def apply(self, changes):
        """应用一批改动，返回实际生效的条目列表。
        change = {'op':'set'|'del'|'add'|'addsec', 'section':名称, 'occ':0,
                  'key':键, 'value':值}
        """
        done = []

        # 新增段：追加到文件末尾
        for c in [x for x in changes if x.get('op') == 'addsec']:
            name = (c.get('section') or '').strip()
            if not name or self.find(name):
                continue
            block = [enc('[%s]' % name)]
            ks = c.get('keys') or []
            for kv in ks:
                block.append(enc('%s=%s' % (kv[0], kv[1])))
            block.append(b'')
            self.lines.extend(block)
            self._parse()
            done.append({'op': 'addsec', 'section': name, 'count': len(ks)})

        # 按段分组，从文件末尾往前处理，避免行号位移
        groups = {}
        for c in changes:
            op = c.get('op')
            if op not in ('set', 'del', 'add'):
                continue
            k = ((c.get('section') or '').lower(), int(c.get('occ') or 0))
            groups.setdefault(k, []).append(c)

        order = []
        for k in groups:
            s = self.find(k[0], k[1])
            if s:
                order.append((s['start'], k))
        for _, key in sorted(order, reverse=True):
            sec = self.find(key[0], key[1])
            if not sec or key[0] in BIN_SECTIONS:
                continue
            done += self._apply_to_section(sec, groups[key])
        self._parse()
        return done

    def _apply_to_section(self, sec, changes):
        sets, dels, adds = {}, set(), []
        for c in changes:
            op = c.get('op')
            k = (c.get('key') or '').strip()
            if not k:
                continue
            if op == 'set':
                sets[k.lower()] = (k, c.get('value') or '')
            elif op == 'del':
                dels.add(k.lower())
            elif op == 'add':
                adds.append((k, c.get('value') or ''))

        # 段内已有的键：add 要降级为 set，否则会写出重复键（INI 里后者覆盖前者，看着像没改）
        existing = {}
        for i in range(sec['start'] + 1, sec['end']):
            kv = split_kv(self.lines[i])
            if kv:
                existing.setdefault(kv[0].strip().lower(), i)
        real_adds = []
        seen = set()
        for k, v in adds:
            kl = k.lower()
            if kl in existing or kl in sets:
                sets.setdefault(kl, (k, v))
                if kl in existing:
                    sets[kl] = (k, v)
                continue
            if kl in seen:
                continue
            seen.add(kl)
            real_adds.append((k, v))
        adds = real_adds

        done = []
        body = []
        last_key_pos = 0
        for i in range(sec['start'] + 1, sec['end']):
            ln = self.lines[i]
            kv = split_kv(ln)
            if kv is None:
                body.append(ln)
                continue
            kl = kv[0].strip().lower()
            if kl in dels:
                done.append({'op': 'del', 'section': sec['name'], 'key': kv[0]})
                continue
            if kl in sets:
                k2, v2 = sets.pop(kl)
                tail = b'\r' if ln.endswith(b'\r') else b''
                body.append(enc('%s=%s' % (k2, v2)) + tail)
                done.append({'op': 'set', 'section': sec['name'], 'key': k2,
                             'value': v2, 'old': kv[1]})
                last_key_pos = len(body)
                continue
            body.append(ln)
            last_key_pos = len(body)

        # 段里原本没有的键：add 的 + set 了但段内不存在的
        new_pairs = list(adds) + list(sets.values())
        if new_pairs:
            ins = [(enc('%s=%s' % (k, v)), k, v) for k, v in new_pairs]
            pos = last_key_pos if last_key_pos else 0
            body[pos:pos] = [b for b, _, _ in ins]
            for _, k, v in ins:
                done.append({'op': 'add', 'section': sec['name'], 'key': k, 'value': v})

        self.lines[sec['start']:sec['end']] = [self.lines[sec['start']]] + body
        return done

    # ---- 落盘
    def changed_bytes(self):
        return b'\n'.join(self.lines)

    def is_changed(self):
        return self.changed_bytes() != self.raw

    def save(self, backup_root=None):
        data = self.changed_bytes()
        backup = ''
        if backup_root:
            d = os.path.join(backup_root, time.strftime('%Y%m%d-%H%M%S'))
            os.makedirs(d, exist_ok=True)
            base = os.path.splitext(os.path.basename(self.path))[0]
            ext = os.path.splitext(self.path)[1]
            backup = os.path.join(d, '%s%s' % (base, ext))
            n = 1
            while os.path.exists(backup):
                backup = os.path.join(d, '%s_%d%s' % (base, n, ext))
                n += 1
            shutil.copy2(self.path, backup)
        tmp = self.path + '.tmp-edit'
        with open(tmp, 'wb') as f:
            f.write(data)
        os.replace(tmp, self.path)
        self.raw = data
        self.original_size = len(data)
        return backup
