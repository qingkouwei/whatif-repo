# -*- coding: utf-8 -*-
"""
云端通道：文案预取（HTTP）+ 状态上报（MQTT）。

两条链路职责不同，故障互不传染：

    HTTP  —— 启动时一次性拉某话题的三档文案，落盘。失败则用本地
              缓存或内置兜底，演示照常。它不在演示的关键路径上。
    MQTT  —— 实时档位与传感量推给云端做统计/看板，挂了就少个统计，
              观众毫无感知。

两个从实测里学到的写法：

1. request 的 .text 是生成器，不是字符串。直接当字符串用会得到一个
   生成器对象的 repr。必须迭代拼接，或者 .json() 拿 dict。

2. MQTT 重连必须用 close() 而不是 disconnect()。disconnect() 释放
   底层线程后再 connect() 回不来，表现为"第一次连得上，断网恢复后
   永远连不上"。这个坑排查成本极高，所以这里重连一律 close() 重建。

蜂窝侧还要注意：MQTTClient 的 server 参数必须是公网 IP 或可解析域名。
板子在运营商 NAT 后面，解析到内网地址（192.168/10.x/172.16）连不上，
所以 broker 部署要带公网入口。
"""

import utime
import ujson
import request
from umqtt import MQTTClient

import config


API_BASE = config.API_BASE if hasattr(config, "API_BASE") else "http://%s:8000" % config.MQTT_HOST


def _http_get_json(url, timeout=8):
    """GET 并解析 JSON。失败返回 None，不抛异常到调用方。"""
    try:
        resp = request.get(url, decode=True, timeout=timeout)
        if resp.status_code != 200:
            print("HTTP %s <- %s" % (resp.status_code, url))
            return None
        return resp.json()
    except Exception as e:
        print("HTTP 异常 %s: %s" % (url, e))
        return None


def prefetch_answers(topic_id, store, log):
    """一次性拉取该话题的三档文案并落盘。返回是否成功。"""
    url = "%s/api/answers?topic_id=%d&device=%s" % (API_BASE, topic_id, config.DEVICE_ID)
    data = _http_get_json(url)
    if not data or "answers" not in data:
        log("预取失败，退回本地缓存")
        return store.coverage() == 2
    ok = store.replace(topic_id, data.get("topic", ""), data["answers"],
                       int(utime.time()))
    return ok


def _http_post_json(url, payload, timeout=12):
    """POST JSON 并解析响应。失败返回 None。

    request.post 的 data 参数要传 bytes，这里手动序列化。
    .text 同样是生成器，用 .json() 直接拿 dict 最省事。
    """
    try:
        body = ujson.dumps(payload)
        resp = request.post(url, data=body, timeout=timeout,
                            headers={"Content-Type": "application/json"})
        if resp.status_code != 200:
            print("POST %s <- %s" % (resp.status_code, url))
            return None
        return resp.json()
    except Exception as e:
        print("POST 异常 %s: %s" % (url, e))
        return None


class TraceRecorder(object):
    """记录本次访问的距离轨迹，按秒聚合成 (min, spread)。

    为什么不把每秒原始点都发给云端：设备主循环 10Hz 采样，
    一次访问十几秒就是一两百个点，蜂窝上行虽然够，但喂模型太长。
    这里在设备侧先压成每秒两个数（最近距离、当秒抖动幅度），
    30 秒也就 60 个数。抖动幅度 spread 是判"是不是两个人"的关键，
    两个人一起凑近时同一秒内读数会在两个距离间跳。
    """

    def __init__(self, max_seconds=30):
        self._max = max_seconds
        self.reset()

    def reset(self):
        self._cur_min = None
        self._cur_max = None
        self._trace = []
        self._second_start = 0

    def feed(self, dist_cm, now_ms):
        """喂入一帧原始距离。每秒结算一次。"""
        if dist_cm is None:
            return
        if self._second_start == 0:
            self._second_start = now_ms
        if self._cur_min is None or dist_cm < self._cur_min:
            self._cur_min = dist_cm
        if self._cur_max is None or dist_cm > self._cur_max:
            self._cur_max = dist_cm
        # 满一秒，结算
        if now_ms - self._second_start >= 1000:
            spread = 0 if self._cur_max is None else int(self._cur_max - self._cur_min)
            self._trace.append({"min": int(self._cur_min), "spread": spread})
            if len(self._trace) > self._max:
                self._trace.pop(0)
            self._cur_min = None
            self._cur_max = None
            self._second_start = now_ms

    @property
    def trace(self):
        return list(self._trace)

    def seconds(self):
        return len(self._trace)


def judge_behavior(trace, dwell_s, env, log):
    """把轨迹发给云端，让模型判行为并返回近档该说的话。

    返回 dict（含 source/close_text）或 None。失败时调用方应退回
    本地缓存的静态近档文案，绝不能因为这一个请求没回来就卡住演示。
    """
    url = "%s/api/judge" % API_BASE
    data = _http_post_json(url, {
        "trace": trace, "dwell_s": dwell_s, "env": env})
    if not data or not data.get("behavior"):
        log("行为判定失败，近档用本地缓存文案")
        return None
    log("行为=%s 来源=%s" % (data.get("behavior"), data.get("source")))
    return data



class Uplink(object):
    """MQTT 上行 + 下行指令 + 会话统计。"""

    def __init__(self, cfg, store, state):
        self._cfg = cfg
        self._store = store
        self._state = state
        self._client = None
        self._connected = False
        self._seq = 0
        self._sessions = []

    # ---- 连接 ----

    def _ensure(self):
        if self._connected:
            return True
        try:
            self._client = MQTTClient(
                self._cfg.DEVICE_ID, self._cfg.MQTT_HOST, self._cfg.MQTT_PORT,
                self._cfg.MQTT_USER, self._cfg.MQTT_PASSWORD,
                keepalive=60, reconn=True, version=4)
            self._client.connect()
            self._client.subscribe(self._cfg.TOPIC_CMD, qos=0)
            self._client.set_callback(self._on_cmd)
            self._connected = True
            return True
        except Exception as e:
            print("MQTT 连接失败：%s" % e)
            self._connected = False
            return False

    def _drop(self):
        # 重连一律 close()，不用 disconnect()
        try:
            if self._client:
                self._client.close()
        except Exception:
            pass
        self._client = None
        self._connected = False

    # ---- 上行 ----

    def publish_state(self, tier, dist, k_divisor, lux_mv, coverage, state):
        if not self._ensure():
            state.report_fail += 1
            return
        self._seq += 1
        payload = {
            "seq": self._seq,
            "tier": tier,
            "dist_cm": dist,
            "k": round(k_divisor, 3),
            "lux_mv": lux_mv,
            "cache": coverage,
            "topic_id": self._store.topic_id,
            "csq": self._csq(),
        }
        try:
            self._client.publish(self._cfg.TOPIC_STATE, ujson.dumps(payload), qos=0)
            state.report_ok += 1
            self._client.check_msg()   # 顺带处理下行
        except Exception as e:
            print("上行发送失败：%s，稍后重建连接" % e)
            self._drop()
            state.report_fail += 1

    def note_session(self, max_tier, dur_ms):
        """一次观众访问结束：记录最远看到第几档 + 停留时长。
        这两个数是路演后最有说服力的现场数据，看板靠它排人气。"""
        self._sessions.append((max_tier, dur_ms))
        if len(self._sessions) > 20:
            self._sessions.pop(0)

    @staticmethod
    def _csq():
        try:
            import net
            return net.csqQueryPoll()
        except Exception:
            return None

    # ---- 下行 ----

    def _on_cmd(self, topic, data):
        try:
            cmd = ujson.loads(data)
        except Exception:
            return
        act = cmd.get("act")
        if act == "set_topic":
            tid = int(cmd.get("topic_id", config.TOPIC_ID))
            if tid in config.TOPICS:
                self._store_topic(tid)
        elif act == "refresh":
            self._refresh()

    def _store_topic(self, tid):
        self._state.topic_id = tid
        prefetch_answers(tid, self._store, lambda m: print(m))
        print("已切换话题 -> %s" % config.TOPICS[tid])

    def _refresh(self):
        prefetch_answers(self._state.topic_id, self._store, lambda m: print(m))

    def run(self, state):
        """独立线程：维持连接 + 处理下行。"""
        while not state.stop:
            if self._connected:
                try:
                    self._client.check_msg()
                except Exception as e:
                    print("下行检查异常：%s" % e)
                    self._drop()
            utime.sleep_ms(500)

    def close(self):
        self._drop()
