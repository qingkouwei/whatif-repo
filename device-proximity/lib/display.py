# -*- coding: utf-8 -*-
"""
设备端的"表现层"：RGB 状态灯 + 蜂鸣器提示 + REPL 文本输出。

路演现场环境嘈杂，声音提示基本无效，所以主指示走灯，蜂鸣器只在
升档瞬间响一下做强化。这一取舍直接决定了展位上的可读性：
观众隔着三米看灯色就知道"现在能不能凑近"，不需要听。

RGB 是共阳接法，低电平点亮。官方 rgb_led.py 用 active_level 参数
适配，本项目默认 0。如果按"高电平点亮"接线，代码能跑但颜色全反，
排查时很容易往驱动 bug 上想。
"""

import utime
from machine import Pin


COLORS = {
    # 档位 -> 颜色。红=别靠近，黄=可以靠近，绿=凑近看
    "red":    (1, 0, 0),
    "yellow": (1, 1, 0),
    "green":  (0, 1, 0),
    "off":    (0, 0, 0),
    "white":  (1, 1, 1),
}

TIER_COLOR = {0: "red", 1: "yellow", 2: "green"}


class RGBLED(object):

    def __init__(self, r_pin, g_pin, b_pin, active_level=0):
        self._pins = [
            Pin(r_pin, Pin.OUT, Pin.PULL_DISABLE, self._idle(active_level)),
            Pin(g_pin, Pin.OUT, Pin.PULL_DISABLE, self._idle(active_level)),
            Pin(b_pin, Pin.OUT, Pin.PULL_DISABLE, self._idle(active_level)),
        ]
        self._active = active_level

    @staticmethod
    def _idle(active_level):
        return 1 if active_level == 0 else 0

    def set(self, name):
        r, g, b = COLORS.get(name, COLORS["off"])
        for pin, on in zip(self._pins, (r, g, b)):
            level = self._active if on else (0 if self._active else 1)
            pin.write(level)

    def off(self):
        self.set("off")


class Buzzer(object):
    """蜂鸣器。套件模块是低电平触发，active_level=0。

    注意初始电平必须是"静音"电平，否则开机即长响。
    """

    def __init__(self, pin, active_level=0):
        self._active = active_level
        self._silence = 1 if active_level == 0 else 0
        self._pin = Pin(pin, Pin.OUT, Pin.PULL_DISABLE, self._silence)

    def _level(self, on):
        return self._active if on else self._silence

    def beep(self, duration_ms=120):
        self._pin.write(self._level(True))
        utime.sleep_ms(duration_ms)
        self._pin.write(self._silence)

    def pattern(self, times=2, duration_ms=100, interval_ms=90):
        for _ in range(times):
            self.beep(duration_ms)
            utime.sleep_ms(interval_ms)


class Heartbeat(object):
    """单 LED 心跳，表示采集线程活着。路演时用来区分
    '程序卡死'和'现场没人凑近'这两种灯全灭的情况。"""

    def __init__(self, pin, period_ms=1200):
        self._pin = Pin(pin, Pin.OUT, Pin.PULL_DISABLE, 0)
        self._period = period_ms
        self._next = 0

    def tick(self, now_ms):
        if now_ms >= self._next:
            self._pin.write(1)
            self._next = now_ms + self._period
            utime.sleep_ms(35)
            self._pin.write(0)


class Console(object):
    """REPL 输出。QPYcom 的 REPL 屏蔽中文输入，但打印中文输出正常，
    所以诊断信息可以放心用中文。"""

    def banner(self, device_id, topic_desc):
        print("=" * 56)
        print("  What If Lab · 距离即权限   device=%s" % device_id)
        print("  观察对象: %s" % topic_desc)
        print("=" * 56)

    def status(self, tier_name, dist, temp, lux, cached):
        print("[%s] dist=%-6s temp=%-5s lux=%-5s cache=%s"
              % (tier_name, dist, temp, lux, cached))
