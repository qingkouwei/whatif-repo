# -*- coding: utf-8 -*-
"""
合并固件配置：一块板同时跑「同桌/凑近」（超声）与「AI房评师」（光照）。

正常情况不用改这里——传感器插上即自动识别。
下面三个 FORCE 开关只用于一种场景：某个传感器的自动判定被误杀时强制点亮。
上板如果发现"明明插了光敏但链路没激活"，先跑 calib 看读数，再决定要不要
FORCE_ADC=True，别一上来就强制（强制会把悬空随机值当真数据上报）。
"""

# ★ 模组 utime.time()/localtime() 是 UTC（NITZ 双重时区实测无法归零，
# setTimeZone(0) 无效）。本地时区按小时数校正，北京=8。
# 不校正的后果：白天判定(7-18点)把北京白天当凌晨，光照全走"夜间灯事件"，
# 日照账本与上报 ts 全部错位。
UTC_OFFSET_H = 8
NTP_RESYNC_S = 1800    # 30 分钟 NTP 重同步（启动即先对一次，见 main.ntp_sync）

DEVICE_ID = "whatif-merged-01"
ROOM_NAME = "演示房间"           # 光照链路注册到看板用这个名字
FIRMWARE = "0.2.0-merged"

# ---- 自动判定强制开关（默认全 False，让传感器自己说话）----
FORCE_BH1750 = False   # True=跳过 I2C 探测直接按 BH1750 初始化
FORCE_ADC = False      # True=跳过读数合理性检查，强制启用 KY-018
FORCE_LIGHT = False    # True=两个光照探测都失败也硬起光照链路（用 KY-018 口径）

# ---------------------------------------------------------------------------
# 超声（链路一）—— 引脚与阈值沿用 whatif-lab/firmware 的实测分配
# ---------------------------------------------------------------------------
# GPIO31 独占给 Echo（官方示例里它和蜂鸣器/数码管打架，理由见原 config 注释）
PIN = {
    "trig":  30,
    "echo":  31,     # 必须经 1k/2k 分压后进板
    "rgb_r": 32,
    "rgb_g": 4,
    "rgb_b": 5,
    "buzzer": 6,
    "led":    7,
    "lux":   None,
}

TIER2_ENTER_CM = 38.0
TIER2_LEAVE_CM = 52.0
TIER1_ENTER_CM = 95.0
TIER1_LEAVE_CM = 115.0
TIER_OUTSIDE = 0
TIER_MID = 1
TIER_CLOSE = 2

RAISE_CONFIRM_FRAMES = 4
LOWER_CONFIRM_FRAMES = 1
NO_ECHO_TIMEOUT_MS = 600
SAMPLE_INTERVAL_MS = 100       # 超声节拍
REPORT_INTERVAL_MS = 400       # 链路一 state 上报节拍

# ---------------------------------------------------------------------------
# 光照（链路二）—— 沿用 firmware-solar v2 参数
# ---------------------------------------------------------------------------
ADC_CHANNEL = "ADC1"
DIVIDER_R1_KOHM = 100.0        # KY-018 外部分压；上板跑 calib 后按实际改
DIVIDER_R2_KOHM = 100.0
SATURATION_MV = 1150           # ★ calib.py 实测顶值回填
LIGHT_INVERT = False           # ★ calib.py 判极性回填
SUN_ACTIVE_MV = 300
LAMP_RISE_MV = 150
LAMP_NIGHT_ON_MV = 250
DAY_START_HOUR = 7
DAY_END_HOUR = 18
FILTER_WINDOW = 5
STATS_WINDOW_S = 60
REPORT_INTERVAL_S = 300        # 链路二 5 分钟一条

# BH1750 口径阈值（lux，传感器在位时生效）
SUN_ACTIVE_LUX = 200
LAMP_RISE_LUX = 300
LAMP_NIGHT_ON_LUX = 50

# ---------------------------------------------------------------------------
# 网络：两个 topic 各走各的云端，互不认识对方数据
# ---------------------------------------------------------------------------
MQTT_HOST = "101.200.34.183"
MQTT_PORT = 1883
MQTT_USER = ""
MQTT_PASSWORD = ""

TOPIC_STATE = "/whatif/%s/state" % DEVICE_ID          # 链路一 → whatif-cloud:8000
TOPIC_CMD = "/whatif/%s/cmd" % DEVICE_ID              # 链路一下行（调阈值/切话题）
SOLAR_TOPIC_UP = "/solar_ledger/%s/up" % DEVICE_ID    # 链路二 → dashboard:8001
MQTT_TOPIC_UP = SOLAR_TOPIC_UP                        # uplink 缺省值（会被构造参数覆盖）

API_BASE = "http://%s:8000" % MQTT_HOST               # 三档文案 + 行为判定
CACHE_PATH = "/usr/answer.json"

# ---------------------------------------------------------------------------
# 离线队列（两条链路共用一个队列空间，条数很少够用）
# ---------------------------------------------------------------------------
QUEUE_PATH = "/usr/pending.ndjson"
QUEUE_FLUSH_S = 60
QUEUE_MAX_KB = 60
