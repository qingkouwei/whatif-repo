# -*- coding: utf-8 -*-
"""
What If Lab · 距离即权限 —— 设备主程序

跑在 EG800Z QuecDuino（QuecPython）上。

数据流：

    HC-SR04 ─┐
    AHT20   ─┼─ 主循环 ── 档位判定 ─┬─ 渲染（REPL / 屏 / 数码管）
    KY-018  ─┘                      └─ MQTT 状态上报

    云端 /api/answers?topic=N ── 启动时一次性预取三档文案 ── 落盘缓存

两条链路故意分开。文案预取走 HTTP，一次拿全，之后演示不依赖网络；
MQTT 只负责把实时档位和传感量推给云端做统计与看板，它挂了不影响演示。
这么设计的唯一理由：路演现场断网是必然事件，不是意外事件。

上电后的自检顺序不能改：先确认蜂窝网络就绪，再连 broker，
最后才进主循环。跳过网络检查直接连 MQTT 的写法在办公桌上测没问题，
换到楼道、换到 SIM 欠费、换到展会这种信号垃圾场，就是一句
"connect 抛异常"然后把整个程序带走。
"""

import utime
import _thread
import checkNet
import net
import sim
from machine import Pin, I2C
from misc import ADC

import config
from lib.ultrasonic import UltrasonicSensor
from lib.proximity import ProximityJudge
from lib.display import RGBLED, Buzzer, Heartbeat, Console
from lib.answer_store import AnswerStore
from lib.renderer import ConsoleRenderer
from lib.cloud import prefetch_answers, Uplink, TraceRecorder, judge_behavior
from lib.env import AHT20

# ---------------------------------------------------------------------------

VERSION = "0.1.0"


class AppState(object):
    """主循环与 MQTT 回调线程之间的共享状态，只做读写、不做逻辑。"""

    def __init__(self):
        self.topic_id = config.TOPIC_ID
        self.report_ok = 0
        self.report_fail = 0
        self.prefetch_ok = False
        self.stop = False
        # 行为判定结果，由判定线程写、主循环读。None 表示还没回或没发起。
        # 这是模型真正参与决策的地方：近档显示的话来自模型对轨迹的判断，
        # 不是模板填空。判定失败时退回 store 里的静态近档文案。
        self.judge_result = None
        self.judge_busy = False


def wait_network(tries=3):
    """四步注网链路。返回 (ok, detail)。

    stage 含义：1 查 SIM，2 查注网，3 查 PDP。stage=3 且 state=1 才通。
    把 stage/state 打出来，比反复 try/except 猜哪步错了快十倍。
    """
    for i in range(tries):
        try:
            stage, state = checkNet.waitNetworkReady(30)
            if stage == 3 and state == 1:
                csq = net.csqQueryPoll()
                return True, "stage=3 state=1 csq=%s" % csq
            print("网络未就绪 stage=%s state=%s，重试 %d/%d" % (stage, state, i + 1, tries))
        except Exception as e:
            print("注网异常：%s" % e)
        utime.sleep_ms(2000)
    return False, "注网失败（检查天线是否接好、SIM 是否欠费）"


def refresh_divisor(ultra, env, log):
    """用环境温度刷新超声除数。读不到温度就保留上一次的值。"""
    try:
        rh, temp = env.read()
        if temp is not None:
            ultra.set_temperature(temp)
            return temp, rh
    except Exception as e:
        log("AHT20 读取失败：%s" % e)
    return None, None


def lux_mv(adc):
    """光敏分压后电压（mV）。ADC 返回单位是 mV，不是 0-4095 码值。

    这里不做 lux 换算：LDR 强非线性、对波长响应与肉眼不同，
    没有照度计标定之前，任何 lux 数字都是假的。只报 mV，
    把"够不够亮"的判断留给云端，并且允许它拒绝判断。
    """
    try:
        return adc.read(ADC.ADC1)
    except Exception:
        return None


# ---------------------------------------------------------------------------

def mqtt_thread(state, uplink, log):
    """MQTT 在独立线程里跑，失败不拖垮主循环。"""
    try:
        uplink.run(state)
    except Exception as e:
        log("上行线程退出：%s" % e)


def judge_thread(state, recorder, env_info_fn, log):
    """行为判定在独立线程里跑。

    judge_behavior 是阻塞 HTTP（最长 12 秒），放主循环里会把采样节拍
    整个卡住——观众正凑近，屏幕却冻住，这是演示里最难看的故障。
    所以主循环只负责在合适时机把 state.judge_busy 置 True 并起线程，
    这里算完把结果写回 state.judge_result，升档到近档时读一次。

    一次访问只判一次：进中档时发起。这时轨迹已经攒了几秒的接近过程，
    够模型看出是路过、犹豫还是结伴；等凑到近档再发起就晚了，
    12 秒超时基本等于没有。
    """
    try:
        trace = recorder.trace
        dwell = recorder.seconds()
        result = judge_behavior(trace, dwell, env_info_fn(), log)
        state.judge_result = result
    except Exception as e:
        log("判定线程异常：%s" % e)
        state.judge_result = None
    finally:
        state.judge_busy = False


def main():
    t0 = utime.ticks_ms()

    def log(msg):
        print("[%6.1fs] %s" % (utime.ticks_diff(utime.ticks_ms(), t0) / 1000.0, msg))

    Console().banner(config.DEVICE_ID, config.TOPICS[config.TOPIC_ID])
    log("版本 %s" % VERSION)

    # ---- 1. 网络 ----
    ok, detail = wait_network()
    log("蜂窝网络：%s (%s)" % ("就绪" if ok else "不可用", detail))
    if not ok:
        log("继续启动：文案有本地缓存，MQTT 会自动重连，演示不受影响")

    # ---- 2. 外设 ----
    ultra = UltrasonicSensor(config.PIN["trig"], config.PIN["echo"])
    env = AHT20(I2C(I2C.I2C0, I2C.STANDARD_MODE))
    adc = ADC()
    rgb = RGBLED(config.PIN["rgb_r"], config.PIN["rgb_g"], config.PIN["rgb_b"],
                 active_level=0)
    buzzer = Buzzer(config.PIN["buzzer"], active_level=0)
    beat = Heartbeat(config.PIN["led"])
    store = AnswerStore(config.CACHE_PATH)
    renderer = ConsoleRenderer()
    judge = ProximityJudge({
        "tier2_enter": config.TIER2_ENTER_CM, "tier2_leave": config.TIER2_LEAVE_CM,
        "tier1_enter": config.TIER1_ENTER_CM, "tier1_leave": config.TIER1_LEAVE_CM,
        "raise_frames": config.RAISE_CONFIRM_FRAMES, "lower_frames": config.LOWER_CONFIRM_FRAMES,
    })

    temp, rh = refresh_divisor(ultra, env, log)
    last_temp_c = temp if temp is not None else 20.0
    log("初始温度 %s℃，超声除数 %.2f" % (temp, ultra.k_divisor))

    # ---- 3. 预取三档文案 ----
    state = AppState()
    if ok:
        got = prefetch_answers(state.topic_id, store, log)
        state.prefetch_ok = got
        log("文案预取：%s（缓存覆盖 %d/2 档）" % ("成功" if got else "失败", store.coverage()))
    else:
        log("跳过预取，使用本地缓存 %d/2 档" % store.coverage())

    # ---- 4. 上行通道 ----
    uplink = Uplink(config, store, state)
    if ok:
        _thread.start_new_thread(mqtt_thread, (state, uplink, log))

    # ---- 5. 主循环 ----
    rgb.set("red")
    tier = judge.tier
    last_report = 0
    temp_throttle_ms = 0
    session_start = 0
    max_tier = 0
    recorder = TraceRecorder()
    judge_used = False          # 本次访问的近档文案是否已被模型结果刷新

    def env_info():
        return {"temp_c": last_temp_c, "lux_mv": lux_mv(adc)}

    log("进入主循环。观众距离判定线：%.0fcm / %.0fcm" % (
        config.TIER1_ENTER_CM, config.TIER2_ENTER_CM))

    while not state.stop:
        now = utime.ticks_ms()

        dist = ultra.read()
        changed = judge.feed(dist, now)
        if not changed:
            changed = judge.timeout(now, config.NO_ECHO_TIMEOUT_MS)
        recorder.feed(dist, now)

        # 温度每 60s 刷一次，除数跟着展厅里的人气一起变
        if now - temp_throttle_ms > 60000:
            t2, rh2 = refresh_divisor(ultra, env, log)
            if t2 is not None:
                last_temp_c = t2
            temp_throttle_ms = now

        if changed:
            new_tier = judge.tier
            rgb.set({0: "red", 1: "yellow", 2: "green"}[new_tier])

            if new_tier > tier:
                buzzer.beep(90)
            if new_tier == 0 and tier > 0:
                # 一次访问结束，记录这次最多凑到多近
                if max_tier > 0:
                    uplink.note_session(max_tier, utime.ticks_diff(now, session_start))
                max_tier = 0
                judge_used = False
            if new_tier > 0 and tier == 0:
                session_start = now
                recorder.reset()
                state.judge_result = None
            if new_tier > max_tier:
                max_tier = new_tier

            # 进中档就发起行为判定：此时轨迹里已经有"走近"的过程，
            # 够模型分辨路过/犹豫/结伴。等升近档再发就晚了——HTTP
            # 最长 12 秒，观众不会在近档等你 12 秒。
            if new_tier == config.TIER_MID and not state.judge_busy and ok:
                state.judge_busy = True
                _thread.start_new_thread(judge_thread, (state, recorder, env_info, log))

            text, fell_back = store.get(new_tier)
            note = ""
            if new_tier == config.TIER_CLOSE:
                jr = state.judge_result
                if jr and jr.get("close_text"):
                    text = jr["close_text"]
                    judge_used = True
                    note = "（AI 判定：%s）" % jr.get("behavior", "?")
                elif fell_back:
                    note = "（内置兜底文案）"
            renderer.show(new_tier, text, {"full_len": len(store.get(2)[0])})
            log("%s -> %s%s%s" % (
                ProximityJudge.LABEL[tier], ProximityJudge.LABEL[new_tier],
                note,
                "" if note else ("（本地缓存文案）" if fell_back else "")))
            tier = new_tier
            last_report = 0      # 档位变化立刻上报，不等节拍

        beat.tick(now)

        if now - last_report >= config.REPORT_INTERVAL_MS:
            mv = lux_mv(adc)
            uplink.publish_state(judge.tier, dist, ultra.k_divisor, mv,
                                 store.coverage(), state)
            last_report = now
            Console().status(ProximityJudge.LABEL[judge.tier], dist,
                             last_temp_c, mv, store.coverage())

        utime.sleep_ms(config.SAMPLE_INTERVAL_MS)

    rgb.off()
    uplink.close()


main()
