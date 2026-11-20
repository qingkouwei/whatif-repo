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

import paho.mqtt.client as mqtt

DEV = "solar-mock-01"
ROOM = "演示桌"
TOPIC_UP = "/solar_ledger/%s/up" % DEV
TOPIC_CMD = "/solar_ledger/%s/cmd" % DEV

SCENES = {
    # name: (natural 基线mV, 抖动, lamp分量, 温度偏移)
    "idle":  (180, 12, 0,   0.0),
    "hand":  (42,   6, 0,   +1.6),
    "torch": (1050, 40, 0,  0.0),
    "lamp":  (170, 10, 350, 0.0),
    "night": (22,   4, 0,   -0.5),
}

state = {"scene": "idle", "day_sun_min": 126.0, "seq": 0, "t0": time.time()}


def on_cmd(client, userdata, msg):
    try:
        c = json.loads(msg.payload.decode())
    except ValueError:
        return
    # 调试页经 /api/cmd 下发：{"action":..., "config":{...}}；也兼容 act 键
    act = c.get("action") or c.get("act")
    args = c.get("config") or {}
    if act == "gesture" and args.get("scene") in SCENES:
        state["scene"] = args["scene"]
        print(">>> 场景切换: %s" % act, args["scene"], flush=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=1883)
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

    while True:
        on_cmd_pending = cli  # noqa: loop_start 已处理回调
        base, jit, lamp, doff = SCENES[state["scene"]]
        t = time.time() - state["t0"]
        wobble = 1.0 + 0.06 * math.sin(t / 23.0) + random.uniform(-0.02, 0.02)
        natural = max(0, int(base * wobble + random.uniform(-jit, jit)))
        sat = 1 if natural >= 1150 else 0
        state["day_sun_min"] += args.interval / 60.0 if natural >= 400 else 0
        state["seq"] += 1
        now = time.time()
        payload = {
            "type": "reading", "dev": DEV,
            "ts": int(now), "ts_ms": int(now * 1000), "seq": state["seq"],
            "natural_mv": natural, "lamp_mv": lamp,
            "temp": round(24.6 + doff + 0.2 * math.sin(t / 50.0), 1),
            "rh": round(48.0 - natural / 200.0, 1),
            "lux": natural, "sun_min": round(natural / 400.0, 3),
            "naive_sun_min": round((natural + lamp) / 400.0, 3),
            "peak": natural, "sat_ratio": sat,
            "day": {"sun_min": round(state["day_sun_min"], 1),
                    "lamp_min": round(lamp / 35.0, 1), "sat_ratio": sat},
            "q": 0, "csq": random.randint(18, 24), "fw": "demo-mock-v1",
        }
        cli.publish(TOPIC_UP, json.dumps(payload, separators=(",", ":")), qos=0)
        print("pub %s nat=%4d lamp=%3d seq=%d" % (
            state["scene"], natural, lamp, state["seq"]), flush=True)
        time.sleep(args.interval)


if __name__ == "__main__":
    main()
