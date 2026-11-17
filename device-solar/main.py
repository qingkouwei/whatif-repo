# -*- coding: utf-8 -*-
"""
采光采集固件 v2 —— 主入口（连同 config.py、lib/ 放 /usr，本文件必须叫 main.py）

上电顺序（顺序是实测教训，别调）：
1. 等蜂窝网络就绪（四步链路 checkNet，stage=3 state=1 才算通）
2. 确认 RTC/网络时间有效（时间不对，"白天/夜间"分会全错；
   utime.localtime() 年份 <2025 视为没同步，等待重试）
3. 加载账本 → 起采集 → 每 5 分钟一条 type=reading 上报（对齐看板契约）
   → 分钟桶累计进账本，每 30 分钟落盘一次（NOR flash 省着用）
4. 断网样本进离线队列，恢复后自动补报（backfill=1）

payload 契约 v2（dashboard/app.py 的 save_reading 按这个取字段）：
{
  "type": "reading", "dev": ..., "seq": 自增(去重靠它), "ts": epoch,
  "natural_mv": 自然光分量均值, "lamp_mv": 灯光分量均值,
  "temp": ℃|null, "rh": %|null, "csq": 0~31,
  "sun_min": 最近一小时自然光分钟, "naive_sun_min": 剔除灯光前的日照分钟,
  "peak": 最近一小时峰值mV, "sat_ratio": 最近一小时饱和率,
  "day": {"sun_min": 今天累计自然光分钟, "lamp_min": ..., "sat_ratio": ...},
  "backfill": 0|1
}

时间接口说明：QuecPython 网络时间来自基站 NITZ 或 NTP，个别固件
默认不开。发现 localtime 不对，REPL 执行 import ntptime; ntptime.settime()。
上板第一件事先核对时间，README 有核对方法。
"""

import utime
import ujson
import uos
import checkNet
from misc import ADC

import config
from lib.light import LightSensor, cfg_divider
from lib.queue import OfflineQueue
from lib.uplink import Uplink
from lib.aht20 import AHT20

LEDGER_PATH = "/usr/ledger_v2.json"
LEDGER_SAVE_S = 1800          # 账本落盘节拍：半小时一次，掉电最多丢半小时

adc = ADC()
try:
    adc.open()                # 个别固件要显式 open，失败容错跳过
except Exception:
    pass


def log(msg):
    try:
        stamp = utime.localtime().strftime("%m-%d %H:%M:%S")
    except Exception:
        stamp = "??:??:??"
    print("[%s] %s" % (stamp, msg))


def wait_network(timeout_s=60):
    try:
        stage, state = checkNet.waitNetworkReady(timeout_s)
        if stage == 3 and state == 1:
            return True
        log("网络未就绪 stage=%s state=%s" % (stage, state))
    except Exception as e:
        log("注网异常：%s" % e)
    return False


def time_ok():
    """localtime 年份不合理就是时间没同步。"""
    try:
        return utime.localtime()[0] >= 2025
    except Exception:
        return False


def load_ledger():
    default = {"date": "", "sun_min": 0.0, "lamp_min": 0.0, "dark_min": 0.0,
               "sat_sec": 0, "total_sec": 0, "peak_mv": 0, "days": {}}
    try:
        if LEDGER_PATH not in uos.listdir("/usr"):
            return dict(default)
        f = open(LEDGER_PATH, "r")
        data = ujson.loads(f.read())
        f.close()
        for k, v in default.items():
            if k not in data:
                data[k] = {} if isinstance(v, dict) else v
        return data
    except Exception as e:
        log("账本损坏，重建：%s" % e)
        return dict(default)


def save_ledger(ledger):
    """原子写：先 .tmp 再 rename，掉电不会留半个 JSON。"""
    tmp = LEDGER_PATH + ".tmp"
    f = open(tmp, "w")
    f.write(ujson.dumps(ledger, separators=(",", ":")))
    f.close()
    try:
        uos.remove(LEDGER_PATH)
    except OSError:
        pass
    uos.rename(tmp, LEDGER_PATH)


def roll_day(ledger, date_key):
    """跨天：把昨天归档进 days（留最近 30 天），当日计数清零。"""
    prev = ledger["date"]
    if prev and prev != date_key:
        ledger["days"][prev] = {
            "sun_min": round(ledger["sun_min"], 1),
            "lamp_min": round(ledger["lamp_min"], 1),
            "dark_min": round(ledger["dark_min"], 1),
            "sat_ratio": round(ledger["sat_sec"] / float(ledger["total_sec"] or 1), 3),
            "peak_mv": ledger["peak_mv"]}
        if len(ledger["days"]) > 30:
            oldest = min(ledger["days"].keys())
            del ledger["days"][oldest]
        log("跨天归档 %s sun=%.1f min" % (prev, ledger["sun_min"]))
    ledger["date"] = date_key
    ledger.update({"sun_min": 0.0, "lamp_min": 0.0, "dark_min": 0.0,
                   "sat_sec": 0, "total_sec": 0, "peak_mv": 0})


def register_device(uplink):
    """开机自注册：看板 devices 表按 device_id 归属房间，
    没登记的设备数据会被服务端直接丢弃（get_room_by_device 返回 None）。
    发一条 type=register 让服务端免手工建表。"""
    uplink.publish({
        "type": "register",
        "dev": config.DEVICE_ID,
        "room": config.ROOM_NAME,
        "fw": config.FIRMWARE,
    })


def main():
    log("solar v%s boot dev=%s" % (config.FIRMWARE, config.DEVICE_ID))
    if not config.MQTT_HOST:
        log("⚠ MQTT_HOST 为空：只采集不联网，烧录前先填 config")
    net_ok = wait_network()
    log("蜂窝：%s" % ("就绪" if net_ok else "不可用（天线/SIM/欠费三查）"))

    if not time_ok():
        log("时间未同步，等 30 秒重试…")
        utime.sleep(30)
        if not time_ok():
            log("⚠ 时间仍无效：昼夜分会错，先按 README 校时再谈数据可信")

    queue = OfflineQueue(config)
    uplink = Uplink(config, queue)
    light = LightSensor(adc, config)
    env = AHT20()
    ledger = load_ledger()
    if net_ok:
        register_device(uplink)         # 自注册：免进服务器手工建房间映射
    log("账本载入 date=%s sun=%.1f min | 离线积压 %d 条 | 分压比 %.3f" % (
        ledger["date"], ledger["sun_min"], queue.depth(), cfg_divider(config)))

    last_report_ms = 0
    last_ledger_save_ms = 0
    rolled_into = ledger["date"]

    while True:
        t = utime.localtime()
        epoch = int(utime.mktime(t))
        date_key = "%04d-%02d-%02d" % (t[0], t[1], t[2])

        if date_key != rolled_into:
            roll_day(ledger, date_key)
            rolled_into = date_key
            save_ledger(ledger)

        bucket = light.sample(epoch, t[3], t[4])
        if bucket:
            ledger["sun_min"] += bucket["sun_min"]
            ledger["lamp_min"] += bucket["lamp_min"]
            ledger["dark_min"] += bucket["dark_min"]
            ledger["sat_sec"] += int(round(bucket["sat_ratio"] * 60))
            ledger["total_sec"] += 60
            if bucket["max_mv"] > ledger["peak_mv"]:
                ledger["peak_mv"] = bucket["max_mv"]

        now_ms = utime.ticks_ms()
        if utime.ticks_diff(now_ms, last_report_ms) >= config.REPORT_INTERVAL_S * 1000:
            last_report_ms = now_ms
            snap = light.snapshot()
            temp, rh = env.read()
            sun_h = snap["sun_min_sum"]
            lamp_h = snap["lamp_min_sum"]
            payload = {
                "type": "reading",
                "dev": config.DEVICE_ID,
                "ts": epoch,
                "ts_ms": epoch * 1000,
                "natural_mv": snap["natural_mv"],
                "lamp_mv": snap["lamp_mv"],
                "temp": temp,
                "rh": rh,
                # 看板契约字段：naive = 若不剔灯光会算出的日照分钟。
                # 设备端分类本来就是分列的，naive 取两者和即可，
                # 这个字段是留给"数据质量"视图算虚增量的。
                "sun_min": sun_h,
                "naive_sun_min": round(sun_h + lamp_h, 1),
                "peak": snap["max_mv"],
                "sat_ratio": snap["sat_ratio_max"],
                "day": {
                    "sun_min": round(ledger["sun_min"], 1),
                    "lamp_min": round(ledger["lamp_min"], 1),
                    "sat_ratio": round(
                        ledger["sat_sec"] / float(ledger["total_sec"] or 1), 3)},
                "q": queue.depth(),
                "fw": config.FIRMWARE,
            }
            uplink.publish(payload)
            if snap["sat_ratio_max"] > 0.2:
                log("⚠ 近一小时饱和率 %.0f%%：白天曲线顶部被削平，"
                    "加大分压比或贴半透明遮挡，改完必须重跑 calib.py"
                    % (snap["sat_ratio_max"] * 100))
            log("上报 nat=%s lamp=%s sun1h=%.1fmin 饱和=%.0f%% 积压=%d" % (
                snap["natural_mv"], snap["lamp_mv"], sun_h,
                snap["sat_ratio_max"] * 100, queue.depth()))

        uplink.check()
        queue.tick(now_ms)
        if queue.depth():
            n = uplink.drain_backfill(3)
            if n:
                log("补报 %d 条，剩 %d" % (n, queue.depth()))

        if utime.ticks_diff(now_ms, last_ledger_save_ms) >= LEDGER_SAVE_S * 1000:
            save_ledger(ledger)
            last_ledger_save_ms = now_ms

        utime.sleep_ms(config.SAMPLE_INTERVAL_MS)


main()
