# -*- coding: utf-8 -*-
"""
MQTT 上报通道 v2：上行 + 下行指令 + 离线队列联动。

与上一版的差别：

1. 发送失败不再只是打日志。失败样本进 OfflineQueue，
   连上后由 main 的 drain 循环补报，补报包带 backfill=True，
   看板据此区分实时数据和历史回放。
2. 重连策略沿用实测结论：连接异常后 close() 重建，
   不用 disconnect()（后者释放线程后 connect 回不来）。
3. publish 失败与连接断开要分开处理：蜂窝链路经常是
   "socket 还在、发出去没人收"，publish 抛异常一律视作断线。

payload 契约（v2，云端 dashboard 按这个收）：
{
  "type": "min" | "health",
  "ts": epoch秒, "dev": DEVICE_ID, "seq": 自增,
  "mv": 当前5秒均值(mV,分压节点), "sun_min": 最近桶自然光分钟,
  "lamp_min": 最近桶灯光分钟, "sat_ratio": 饱和率0~1,
  "avg": 60桶滚动平均mV, "peak": 60桶最大mV,
  "day": "YYYY-MM-DD", "backfill": 0|1, "csq": 信号
}
"""

import utime
import ujson
import net
from umqtt import MQTTClient


class Uplink(object):

    def __init__(self, cfg, queue):
        self._cfg = cfg
        self._queue = queue
        self._client = None
        self._connected = False
        self._seq = 0
        self._fail = 0
        self._sent = 0

    # ------------------------------------------------------------------

    def connect(self):
        if self._connected:
            return True
        if not self._cfg.MQTT_HOST:
            return False
        try:
            self._client = MQTTClient(
                self._cfg.DEVICE_ID, self._cfg.MQTT_HOST, self._cfg.MQTT_PORT,
                self._cfg.MQTT_USER, self._cfg.MQTT_PASSWORD,
                keepalive=60, reconn=True, version=4)
            self._client.connect()
            self._client.subscribe(self._cfg.MQTT_TOPIC_DOWN, qos=0)
            self._client.set_callback(self._on_cmd)
            self._connected = True
            print("MQTT connected -> %s" % self._cfg.MQTT_HOST)
        except Exception as e:
            print("MQTT connect failed: %s" % e)
            self._drop()
        return self._connected

    def _drop(self):
        try:
            if self._client:
                self._client.close()
        except Exception:
            pass
        self._client = None
        self._connected = False

    # ------------------------------------------------------------------

    def publish(self, payload):
        """成功返回 True；失败入队返回 False。"""
        if not self._connected and not self.connect():
            self._queue.put(payload)
            self._fail += 1
            return False
        payload = dict(payload)
        # 已有 seq 的（补报样本）保留原编号：乱序或撞号会被看板
        # 的 seq 去重误丢。只有新发的才自增。
        if "seq" not in payload or not payload["seq"]:
            self._seq += 1
            payload["seq"] = self._seq
        elif payload["seq"] > self._seq:
            self._seq = payload["seq"]
        try:
            csq = net.csqQueryPoll()
            payload["csq"] = csq
        except Exception:
            pass
        try:
            body = ujson.dumps(payload, separators=(",", ":"))
            self._client.publish(self._cfg.MQTT_TOPIC_UP, body, qos=0)
            self._sent += 1
            return True
        except Exception as e:
            print("publish failed: %s -> 入离线队列" % e)
            self._queue.put(payload)
            self._fail += 1
            self._drop()
            return False

    def drain_backfill(self, max_items=5):
        """补报积压。任何一条失败立即停，下轮再来，避免雪崩重试。"""
        if not self.connect():
            return 0
        done = 0
        for item in self._queue.drain(max_items):
            item["backfill"] = 1
            if self.publish(item):
                done += 1
            else:
                break
        return done

    # ------------------------------------------------------------------

    def check(self):
        if self._connected:
            try:
                self._client.check_msg()
            except Exception:
                self._drop()

    def _on_cmd(self, topic, data):
        """下行指令。回调里不做重活，只回 ack。"""
        try:
            cmd = ujson.loads(data)
        except ValueError:
            return
        act = cmd.get("act")
        print("cmd: %s" % act)
        ack = {"type": "ack", "act": act, "ok": 0}
        if act == "set_threshold":
            try:
                import config
                mv = int(cmd.get("mv", 0))
                if mv > 0:
                    config.SUN_ACTIVE_MV = mv
                    ack.update({"ok": 1, "mv": mv})
            except Exception as e:
                print("set_threshold failed:", e)
        elif act == "set_divider":
            try:
                import config
                r1 = float(cmd.get("r1", config.DIVIDER_R1_KOHM))
                r2 = float(cmd.get("r2", config.DIVIDER_R2_KOHM))
                if r1 > 0 and r2 > 0:
                    config.DIVIDER_R1_KOHM = r1
                    config.DIVIDER_R2_KOHM = r2
                    ack.update({"ok": 1, "r1": r1, "r2": r2})
            except Exception as e:
                print("set_divider failed:", e)
        elif act == "ping":
            ack["ok"] = 1
        self.publish(ack)

    def close(self):
        self._drop()
