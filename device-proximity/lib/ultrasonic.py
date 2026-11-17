# -*- coding: utf-8 -*-
"""
HC-SR04 测距驱动（护眼哨兵一文的修正版，改动原因见注释）

相对官方 ultrasonic_gpio.py 修了三处，每一处都有实测依据：

1. 量程上限。官方写 800cm，是照抄了模块标称值。HC-SR04 实际稳定上限
   在 400~450cm，超过之后回波极弱，读数会随机跳到 2~3m 之外的假值。
   demo 场景里最远也就展厅纵深十几米，收到 450cm 之外一律判无效。

2. 超时基于时间而不是循环次数。官方的 `t_out < 30000` 计数循环，
   实际耗时取决于主频、MicroPython 循环开销和当时有没有别的线程抢 CPU，
   空载和满载能差好几倍。改用 ticks_add/ticks_diff 后，超时是真实时间。

3. 除数随温度刷新。58.0 这个常数里藏着一个 20℃ 的声速假设：
       声速 v = 331.3 + 0.606 × T(℃)   [m/s]
       除数 k = 20000 / v               [us/cm]
   20℃ 是 58.24，35℃（挤满人的展厅）是 55.67。按 58.0 算，
   450cm 处约 7.8cm 偏差，而 40cm 的近档判定线上偏差约 1.4cm。
   1.4cm 听着不多，但它正好压在观众反复凑近的那条边界上。

距离滤波取窗口最小值：本场景要的是"离设备最近的物体"。
若用均值，远处背景墙的读数会把结果拉高，观众已经贴到 35cm
还可能被算进 60cm，档位直接上不去。
"""

import utime
from machine import Pin


class UltrasonicSensor(object):

    RANGE_MIN = 20.0        # 20cm 以内发射余响未衰减，读数不可信
    RANGE_MAX = 450.0       # 模块物理上限，不照抄示例的 800
    ECHO_WAIT_US = 30000    # 等 Echo 拉高的超时（约对应 5m 往返）
    ECHO_MAX_US = 52000     # Echo 高电平上限：450cm ≈ 26ms，留 2 倍余量

    def __init__(self, trig_pin, echo_pin, filter_window=5):
        self._trig = Pin(trig_pin, Pin.OUT, Pin.PULL_DISABLE, 0)
        self._echo = Pin(echo_pin, Pin.IN, Pin.PULL_DISABLE, 0)
        self._window = filter_window
        self._buf = []
        self.k_divisor = 58.0        # 常温值，由 set_temperature 刷新
        self.last_error = None       # 供主循环打印诊断

    # ------------------------------------------------------------------

    @staticmethod
    def divisor_for_temp(temp_c):
        return 20000.0 / (331.3 + 0.606 * temp_c)

    def set_temperature(self, temp_c):
        self.k_divisor = self.divisor_for_temp(temp_c)

    # ------------------------------------------------------------------

    def _trigger(self):
        self._trig.off()
        utime.sleep_us(2)
        self._trig.on()
        utime.sleep_us(12)           # 规格要求 >=10us，给足余量
        self._trig.off()

    def read_raw(self):
        """单次测距，超时基于真实时间。失败返回 None。"""
        self._trigger()

        # 等 Echo 拉高
        deadline = utime.ticks_add(utime.ticks_us(), self.ECHO_WAIT_US)
        while self._echo.value() == 0:
            if utime.ticks_diff(utime.ticks_us(), deadline) >= 0:
                self.last_error = "no_echo"
                return None

        start = utime.ticks_us()
        # 等 Echo 拉低
        deadline = utime.ticks_add(start, self.ECHO_MAX_US)
        while self._echo.value() == 1:
            if utime.ticks_diff(utime.ticks_us(), deadline) >= 0:
                self.last_error = "echo_stuck"
                return None

        duration = utime.ticks_diff(utime.ticks_us(), start)
        dist = duration / self.k_divisor
        if not (self.RANGE_MIN <= dist <= self.RANGE_MAX):
            self.last_error = "out_of_range"
            return None
        self.last_error = None
        return round(dist, 2)

    def read(self):
        """滑窗最小值滤波。"""
        raw = self.read_raw()
        if raw is None:
            return None
        self._buf.append(raw)
        if len(self._buf) > self._window:
            self._buf.pop(0)
        return round(min(self._buf), 1)
