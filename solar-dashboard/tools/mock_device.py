"""mock 设备：演示用光源场景模拟器（走真实 MQTT 链路）

不是往库里塞假行——它按固件 v2 的 payload 契约发 topic
/solar_ledger/solar-mock-01/up，服务端订阅、去重、入库、日账
走的都是和真机一模一样的链路。演示时断开的只是"光"这一环。

场景（由看板调试页经 /api/cmd 下发，或手动 mosquitto_pub）：
  idle   室内自然光   ~180mV 微光漂移
  hand   手握住遮光   ~40mV  + 温度缓升（手温）
  torch  手电直射     ~1050mV 顶部接近饱和
  lamp   台灯事件     基线+350 的阶跃（触发服务端灯事件分类）
  night  熄灯暗背景   ~20mV

用法（服务器或本机均可，需 paho-mqtt）：
  python3 mock_device.py --host 101.200.34.183
"""
import argparse
import json
import math
import random
import time
import urllib.request

import paho.mqtt.client as mqtt

DEV = "solar-mock-01"
ROOM = "演示桌"
TOPIC_UP = "/solar_ledger/%s/up" % DEV
TOPIC_CMD = "/solar_ledger/%s/cmd" % DEV


def sun_base_mv(now=None):
    """真实钟时的日照钟形（晴，与 backfill 同一公式）。

    idle 场景必须跟着太阳走：mock 平线会在"今天"的曲线上
    留下一条与日照毫无关系的直线，明天演示时一眼假。
    """
    lt = time.localtime(now)
    h = lt.tm_hour + lt.tm_min / 60.0
    if h < 6 or h > 19:
        return 0
    v = math.exp(-((h - 12.7) ** 2) / (2 * 3.3 ** 2))
    return int(min(1140, v * 1180))


def scene_params():
    """场景基线。握住=遮光比例、手电/night=定值、台灯=日照基线上叠灯。"""
    scene = state["scene"]
    sun = sun_base_mv()
    if scene == "idle":
        return sun, 12 + sun // 40, 0, 0.0
    if scene == "hand":
        # 握住是"遮"，读数应远低于当前自然基线；夜里本来就暗，
        # 给个略高于背景的读数保证曲线仍在动
        return max(8, int(sun * 0.10)), 5, 0, +1.6
    if scene == "torch":
        return 1050, 40, 0, 0.0
    if scene == "lamp":
        return sun, 10, 430, 0.0          # 与回填台灯同口径
    return 5, 3, 0, -0.5                  # night

state = {"scene": "idle", "day_sun_min": 0.0, "day_naive_min": 0.0,
         "day_lamp_min": 0.0, "day_date": time.strftime("%Y-%m-%d"),
         "seq": 0, "t0": time.time()}

SCENE_NAMES = ("idle", "hand", "torch", "lamp", "night")


def seed_day_from_ledger(api, dev):
    """启动时把今日日账读回来作为累计初值。

    不做这件事的话，mock 从 0 开始累计并覆盖 daily_ledger，
    回填进去的当日日照会被清零——数据看起来"越演示越少"。
    """
    try:
        houses = json.load(urllib.request.urlopen(api + "/api/houses", timeout=5))
        room_id = None
        for h in houses:
            for r in json.load(urllib.request.urlopen(
                    api + "/api/houses/%d/rooms" % h["id"], timeout=5)):
                if r.get("device_id") == dev:
                    room_id = r["room_id"]
        if room_id is None:
            return
        led = json.load(urllib.request.urlopen(
            api + "/api/rooms/%d/ledger?days=1" % room_id, timeout=5))
        days = led.get("days") or []
        if days:
            today = time.strftime("%Y-%m-%d")
            state["day_date"] = today
            for d in days:
                # 只 seed 今天那一行；昨天的累计拖进今天就是数据错误
                if d.get("date") == today:
                    state["day_sun_min"] = float(d.get("sun_minutes") or 0)
                    state["day_naive_min"] = float(d.get("naive_minutes") or 0)
            print("日账初值(%s): sun=%.1f naive=%.1f" % (
                today, state["day_sun_min"], state["day_naive_min"]))
    except Exception as e:
        print("读日账失败（从 0 累计）:", e)


def on_cmd(client, userdata, msg):
    try:
        c = json.loads(msg.payload.decode())
    except ValueError:
        return
    # 调试页经 /api/cmd 下发：{"action":..., "config":{...}}；也兼容 act 键
    act = c.get("action") or c.get("act")
    args = c.get("config") or {}
    if act == "gesture" and args.get("scene") in SCENE_NAMES:
        state["scene"] = args["scene"]
        print(">>> 场景切换: %s" % act, args["scene"], flush=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=1883)
    ap.add_argument("--api", default="http://127.0.0.1:8001",
                    help="看板 API 地址，用于启动时读回今日日账")
    ap.add_argument("--interval", type=float, default=2.0,
                    help="上报节拍秒。演示建议 2s（真机是 300s，这里加速是为了"
                         "让观众当场看到曲线动，口径向观众说明）")
    args = ap.parse_args()

    cli = mqtt.Client(client_id="solar-mock-01-sim")
    cli.connect(args.host, args.port, 60)
    cli.subscribe(TOPIC_CMD, qos=1)
    cli.on_message = on_cmd
    cli.loop_start()
    print("mock ->", args.host, TOPIC_UP, "节拍 %.1fs" % args.interval)
    seed_day_from_ledger(args.api, DEV)

    while True:
        today = time.strftime("%Y-%m-%d")
        if today != state["day_date"]:          # 跨午夜：日账归零开新页
            state["day_date"] = today
            state["day_sun_min"] = state["day_naive_min"] = state["day_lamp_min"] = 0.0
        base, jit, lamp, doff = scene_params()
        t = time.time() - state["t0"]
        wobble = 1.0 + 0.06 * math.sin(t / 23.0) + random.uniform(-0.02, 0.02)
        natural = max(0, int(base * wobble + random.uniform(-jit, jit)))
        # 日账双口径逐帧累计：sun 只认真实越过阈值的自然光；
        # naive 连灯光一起算——演示"灯光虚增"的活证据就是这么攒的
        if natural >= 400 and lamp == 0:
            state["day_sun_min"] += args.interval / 60.0
        if natural + lamp >= 400:
            state["day_naive_min"] += args.interval / 60.0
        if lamp:
            state["day_lamp_min"] += args.interval / 60.0
        sat = 1 if natural >= 1150 else 0
        state["seq"] += 1
        now = time.time()
        payload = {
            "type": "reading", "dev": DEV,
            "ts": int(now), "ts_ms": int(now * 1000), "seq": state["seq"],
            "natural_mv": natural, "lamp_mv": lamp,
            "temp": round(24.6 + doff + 0.2 * math.sin(t / 50.0), 1),
            "rh": round(48.0 - natural / 200.0, 1),
            "lux": natural, "sun_min": round(state["day_sun_min"], 1),
            "naive_sun_min": round(state["day_naive_min"], 1),
            "peak": natural, "sat_ratio": sat,
            "day": {"sun_min": round(state["day_sun_min"], 1),
                    "naive_sun_min": round(state["day_naive_min"], 1),
                    "lamp_min": round(state["day_lamp_min"], 1), "sat_ratio": sat},
            "q": 0, "csq": random.randint(18, 24), "fw": "demo-mock-v1",
        }
        cli.publish(TOPIC_UP, json.dumps(payload, separators=(",", ":")), qos=0)
        print("pub %s nat=%4d lamp=%3d seq=%d" % (
            state["scene"], natural, lamp, state["seq"]), flush=True)
        time.sleep(args.interval)


if __name__ == "__main__":
    main()
