# -*- coding: utf-8 -*-
"""
一块板、两个作品 —— 传感器自动感知的合并固件

开机不"选择跑哪个程序"，能力全部装载，谁在线谁说话：

    HC-SR04 超声  → 运行期热识别：连续 8 帧有效回波才激活（对着墙挥一下手
                     就能看到激活日志）。激活后：档位状态机 + RGB/蜂鸣 +
                     4Hz /whatif/.../state 上报 + 行为判定线程
                     ——「同桌/凑近」链路，云端 whatif-cloud:8000
    BH1750 光照   → 开机 I2C 扫 0x23，应答即有（数字传感器，探测可靠）
    KY-018 光敏   → ADC 读数合理性预探测（悬空引脚是随机值，不敢信单次读数）
                     任一在线：5 分钟 /solar_ledger/.../up + 自注册
                     ——「AI房评师」链路，云端 dashboard:8001
    AHT20 温湿度  → I2C 0x38。超声温度补偿 + 两边数据共用；失败不阻断

为什么超声不学 I2C 那样开机探测一次定终身：没回波可能是"没插"也可能是
"插着但对着吸音物/超过量程"，开机一票否决会把后者误杀。热识别的代价是
拔插响应慢（停用要连续 30 秒全灭），换来现场永不误判。

LED 指示归链路一（档位色）。光照链路单独点亮时 RGB 保持熄灭，
它的反馈在看板页面上——现场讲解词记得指屏幕。
"""

import utime
import _thread
import checkNet
from misc import ADC
from machine import Pin, I2C

import config
from lib.ultrasonic import UltrasonicSensor
from lib.proximity import ProximityJudge
from lib.display import RGBLED, Buzzer, Heartbeat, Console
from lib.answer_store import AnswerStore
from lib.renderer import ConsoleRenderer
from lib import prox_cloud
from lib.light import LightSensor, cfg_divider
from lib.bh1750 import BH1750Sensor
from lib.aht20 import AHT20
from lib.queue import OfflineQueue
from lib.solar_uplink import Uplink as SolarUplink

VERSION = "0.3.0-merged-tzfix"


def ntp_sync():
    """NTP 权威对时：ntptime.settime() 把模组 UTC 钟对准，
    设备 ts 真值 = 对时后的 UTC 秒经 bj_now() +8。单次对时只正起点，
    晶振会漂，靠主循环每 30 分钟重同步兜住。"""
    try:
        import ntptime
        ntptime.settime()
        log("NTP 对时完成 ts=%s" % bj_now())
        return True
    except Exception as e:
        log("NTP 对时失败：%s（继续用本地钟，下轮重试）" % e)
        return False


def bj_now():
    """北京时间 epoch 秒：模组时钟恒 UTC，统一在这一处加偏移。"""
    return int(utime.time()) + int(getattr(config, "UTC_OFFSET_H", 0)) * 3600


def log(msg):
    try:
        stamp = utime.localtime(bj_now()).strftime("%m-%d %H:%M:%S")
    except Exception:
        stamp = "--:--:--"
    print("[%s] %s" % (stamp, msg))


def wait_network():
    try:
        stage, state = checkNet.waitNetworkReady(30)
        return stage == 3 and state == 1
    except Exception as e:
        log("注网异常 %s" % e)
        return False


# ---------------------------------------------------------------------------
# 传感器存在性判定
# ---------------------------------------------------------------------------

def probe_i2c_addr(i2c, addr):
    try:
        i2c.writeto(addr, b"")
        return True
    except Exception:
        return False


def probe_adc(adc, channel):
    """连读 8 次：悬空引脚的典型特征是贴 0、贴量程顶、或恒定单值。
    有真实光敏分压电路时读数应在中间区且有环境微动。"""
    try:
        vals = [adc.read(channel) for _ in range(8)]
    except Exception:
        return False, []
    lo, hi = min(vals), max(vals)
    if lo < 30 or hi > 1150:
        return False, vals          # 贴边：悬空或未接分压
    if hi - lo < 3:
        return False, vals          # 八次完全一样：不像真实环境
    return True, vals


# ---------------------------------------------------------------------------
# 链路一：超声 + 档位 + 行为判定（热识别）
# ---------------------------------------------------------------------------

class _ReportState(object):
    """prox_cloud.Uplink 需要的 report_ok/fail 计数器宿主。"""
    def __init__(self):
        self.report_ok = 0
        self.report_fail = 0


def _judge_worker(trace, dwell, env, done_cb):
    """行为判定线程体：阻塞 HTTP，算完回调写回主任务。"""
    try:
        result = prox_cloud.judge_behavior(trace, dwell, env, log)
        done_cb(result)
    except Exception as e:
        log("判定线程异常：%s" % e)
        done_cb(None)


class ProximityTask(object):

    ACTIVE_FRAMES = 8           # 100ms 节拍，约 0.8s 有效回波即激活
    DEAD_SECONDS = 30           # 激活后连续 30s 无有效回波才停用

    def __init__(self, net_ok):
        self.active = False
        self._good = 0
        self._last_valid_ms = 0
        self.net_ok = net_ok

        self.ultra = UltrasonicSensor(config.PIN["trig"], config.PIN["echo"])
        self.judge = ProximityJudge({
            "tier2_enter": config.TIER2_ENTER_CM, "tier2_leave": config.TIER2_LEAVE_CM,
            "tier1_enter": config.TIER1_ENTER_CM, "tier1_leave": config.TIER1_LEAVE_CM,
            "raise_frames": config.RAISE_CONFIRM_FRAMES,
            "lower_frames": config.LOWER_CONFIRM_FRAMES,
        })
        self.store = AnswerStore(config.CACHE_PATH)
        self.renderer = ConsoleRenderer()
        self.recorder = prox_cloud.TraceRecorder()
        self.judge_result = None
        self.judge_busy = False
        self.uplink = None
        self._tier = 0
        self._last_report = 0

        try:
            self.rgb = RGBLED(config.PIN["rgb_r"], config.PIN["rgb_g"],
                              config.PIN["rgb_b"], active_level=0)
            self.buzzer = Buzzer(config.PIN["buzzer"], active_level=0)
        except Exception as e:
            log("RGB/蜂鸣初始化失败（链路继续，无灯无响）：%s" % e)
            self.rgb = self.buzzer = None

    def _on_judge_done(self, result):
        self.judge_result = result
        self.judge_busy = False

    def _start_judge(self, env_info):
        """进中距离就后台发起行为判定：此时轨迹里已有"走近"的过程。
        等凑近才请求要 12 秒 HTTP，观众不等。独立线程，卡不住采样。"""
        if self.judge_busy or not self.net_ok:
            return
        self.judge_busy = True
        self.judge_result = None
        _thread.start_new_thread(_judge_worker, (
            self.recorder.trace, self.recorder.seconds(), env_info,
            self._on_judge_done))

    def feed(self, dist, env_info, now_ms):
        # ---- 存在性 ----
        if not self.active:
            if dist is not None:
                self._good += 1
                if self._good >= self.ACTIVE_FRAMES:
                    self.active = True
                    log("★ 超声在线（连续 %d 帧有效回波），链路一激活" % self._good)
                    self._prefetch()
            else:
                self._good = 0
            return
        if dist is None:
            if self._last_valid_ms and \
                    utime.ticks_diff(now_ms, self._last_valid_ms) > self.DEAD_SECONDS * 1000:
                self.active = False
                self._good = 0
                log("○ 超声 %d 秒无回波，链路一停用" % self.DEAD_SECONDS)
                if self.rgb:
                    self.rgb.off()
            return
        self._last_valid_ms = now_ms

        # ---- 链路一正常工作 ----
        self.recorder.feed(dist, now_ms)
        changed = self.judge.feed(dist, now_ms)
        if not changed:
            changed = self.judge.timeout(now_ms, config.NO_ECHO_TIMEOUT_MS)

        if changed:
            new_tier = self.judge.tier
            if self.rgb:
                self.rgb.set({0: "red", 1: "yellow", 2: "green"}[new_tier])
            if new_tier > self._tier and self.buzzer:
                self.buzzer.beep(90)

            if new_tier == config.TIER_MID:
                self._start_judge(env_info)

            text, fell_back = self.store.get(new_tier)
            note = ""
            if new_tier == config.TIER_CLOSE:
                jr = self.judge_result
                if jr and jr.get("close_text"):
                    text = jr["close_text"]
                    note = "（AI 判定：%s）" % jr.get("behavior", "?")
            if new_tier == 0:
                self.recorder.reset()
                self.judge_result = None
            self.renderer.show(new_tier, text, {})
            log("%s -> %s%s%s" % (
                ProximityJudge.LABEL[self._tier], ProximityJudge.LABEL[new_tier],
                note, "（兜底文案）" if (fell_back and not note) else ""))
            self._tier = new_tier
            self._last_report = 0

        if self.uplink and utime.ticks_diff(now_ms, self._last_report) >= config.REPORT_INTERVAL_MS:
            self.uplink.publish_state(self._tier, dist, self.ultra.k_divisor,
                                      env_info.get("lux_mv"),
                                      self.store.coverage(), _RS)
            self._last_report = now_ms

    def set_temperature(self, temp_c):
        if self.active and temp_c is not None:
            self.ultra.set_temperature(temp_c)

    def _prefetch(self):
        if not self.net_ok:
            return
        try:
            topic = getattr(config, "TOPIC_ID", 1)
            prox_cloud.prefetch_answers(topic, self.store, log)
        except Exception as e:
            log("文案预取失败（用缓存）：%s" % e)


# ---------------------------------------------------------------------------
# 链路二：光照 + 5 分钟上报 + 断网队列
# ---------------------------------------------------------------------------

class SolarTask(object):

    def __init__(self, light, kind):
        self.light = light
        self.kind = kind
        self.uplink = None
        self.last_report = 0
        self.day = {"sun_min": 0.0, "lamp_min": 0.0, "sat_sec": 0,
                    "total": 0, "date": "", "peak": 0}

    def tick(self, now_ms):
        epoch = bj_now()
        t = utime.localtime(epoch)
        bucket = self.light.sample(epoch, t[3], t[4])
        date_key = "%04d-%02d-%02d" % (t[0], t[1], t[2])
        if date_key != self.day["date"]:
            self.day.update({"date": date_key, "sun_min": 0.0, "lamp_min": 0.0,
                             "sat_sec": 0, "total": 0, "peak": 0})
        if bucket:
            self.day["sun_min"] += bucket["sun_min"]
            self.day["lamp_min"] += bucket["lamp_min"]
            self.day["total"] += 60
            self.day["sat_sec"] += int(round(bucket["sat_ratio"] * 60))
            mx = bucket.get("max_mv", bucket.get("max_lux", 0))
            if mx > self.day["peak"]:
                self.day["peak"] = mx

        if self.uplink and utime.ticks_diff(now_ms, self.last_report) >= config.REPORT_INTERVAL_S * 1000:
            self.last_report = now_ms
            snap = self.light.snapshot()
            is_bh = self.kind.startswith("bh1750")
            nat_key = "natural_lux" if is_bh else "natural_mv"
            lamp_key = "lamp_lux" if is_bh else "lamp_mv"
            payload = {
                "type": "reading",
                "ts": epoch, "ts_ms": epoch * 1000,
                "natural_mv": snap.get(nat_key, 0),
                "lamp_mv": snap.get(lamp_key, 0),
                # 瞬时读数：账本口径不变（聚合值），只给调试台画"快线"。
                # 旧版服务端不认识这个键，多传无害。
                "inst_mv": self.light.last(),
                "sun_min": snap["sun_min_sum"],
                "naive_sun_min": round(snap["sun_min_sum"] + snap["lamp_min_sum"], 1),
                "peak": self.day["peak"],
                "sat_ratio": snap["sat_ratio_max"],
                "day": {"sun_min": round(self.day["sun_min"], 1),
                        "lamp_min": round(self.day["lamp_min"], 1),
                        "sat_ratio": round(self.day["sat_sec"] / float(self.day["total"] or 1), 3)},
                "fw": VERSION,
            }
            self.uplink.publish(payload)
            log("光照上报 sun1h=%.1fmin 今日=%.1fmin 饱和=%.0f%%" % (
                snap["sun_min_sum"], self.day["sun_min"],
                snap["sat_ratio_max"] * 100))
            if snap["sat_ratio_max"] > 0.2:
                log("⚠ 饱和率>20%：白天顶部被削平，加大分压比/贴遮光，改完重跑 calib")

    def current_lux(self):
        snap = self.light.snapshot()
        return snap.get("natural_lux", snap.get("natural_mv", 0))


# ---------------------------------------------------------------------------

_RS = _ReportState()


def main():
    log("merged v%s boot dev=%s" % (VERSION, config.DEVICE_ID))
    net_ok = wait_network()
    log("蜂窝：%s" % ("就绪" if net_ok else "不可用（本地采集照常，联网自动恢复）"))

    adc = ADC()
    try:
        adc.open()
    except Exception:
        pass
    i2c = I2C(I2C.I2C0, I2C.STANDARD_MODE)

    # ---- 链路二：光照源判定（数字优先） ----
    solar = None
    if config.FORCE_BH1750 or probe_i2c_addr(i2c, 0x23):
        try:
            bh = BH1750Sensor(i2c, config)
            if bh.ok:
                solar = SolarTask(bh, "bh1750")
                log("★ BH1750 在线，光照链路=数字 lux 口径")
        except Exception as e:
            log("BH1750 初始化异常：%s" % e)
    if solar is None:
        ch = getattr(adc, config.ADC_CHANNEL)
        alive, vals = probe_adc(adc, ch)
        if alive or config.FORCE_ADC:
            solar = SolarTask(LightSensor(adc, config), "ky018")
            log("★ KY-018 在线（预探测 %s），光照链路=相对亮度 mV 口径" % vals)
        else:
            log("○ 光照链路停用：无 BH1750，KY-018 读数异常（查接线/分压）%s" % vals)

    # ---- 温湿度（两边共用） ----
    env = AHT20()

    # ---- 链路一 ----
    prox = ProximityTask(net_ok)

    # ---- MQTT：两条链路各自独立连接 ----
    queue = OfflineQueue(config)
    if net_ok:
        try:
            prox.uplink = prox_cloud.Uplink(config, prox.store, _RS)
        except Exception as e:
            log("链路一上行初始化失败：%s" % e)
        if solar:
            try:
                solar.uplink = SolarUplink(config, queue,
                                           up_topic=config.SOLAR_TOPIC_UP)
                solar.uplink.connect()
                solar.uplink.publish({"type": "register", "dev": config.DEVICE_ID,
                                      "room": config.ROOM_NAME, "fw": VERSION})
                log("链路二上行就绪，已自注册房间「%s」" % config.ROOM_NAME)
            except Exception as e:
                log("链路二上行失败（本地照常）：%s" % e)

    if not solar:
        log("提示：只有超声时 RGB 灯就是全部反馈；两者都在时光照看页面")
    Console().banner(config.DEVICE_ID, "merged: proximity + solar")

    # ---- 主循环 ----
    beat = Heartbeat(config.PIN["led"])
    last_solar = 0
    last_prox = 0
    last_env = 0
    last_ntp = utime.ticks_ms()        # 30 分钟 NTP 重同步锚点
    last_temp_c = None

    while True:
        now = utime.ticks_ms()

        # 每 30 分钟重对时：晶振漂移 + 断网重连后时钟可能整体偏走
        if utime.ticks_diff(now, last_ntp) >= config.NTP_RESYNC_S * 1000:
            last_ntp = now
            ntp_sync()

        # 每 60s：温度刷新超声除数（展厅人气一多温度就爬，除数不跟着走距离会漂）
        if utime.ticks_diff(now, last_env) >= 60000:
            last_env = now
            temp, rh = env.read()
            if temp is not None:
                last_temp_c = temp
                prox.set_temperature(temp)

        if utime.ticks_diff(now, last_prox) >= config.SAMPLE_INTERVAL_MS:
            last_prox = now
            try:
                dist = prox.ultra.read()
            except Exception:
                dist = None
            lux = solar.current_lux() if solar else None
            prox.feed(dist, {"temp_c": last_temp_c, "lux_mv": lux}, now)

        if solar and utime.ticks_diff(now, last_solar) >= 1000:
            last_solar = now
            try:
                solar.tick(now)
            except Exception as e:
                log("光照 tick 异常：%s" % e)

        beat.tick(now)
        utime.sleep_ms(20)


main()
