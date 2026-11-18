# -*- coding: utf-8 -*-
"""
BH1750 数字光照传感器采集层（QuecPython / I2C0，地址 0x23）。

为什么换它：KY-018 是光敏电阻 + ADC 的模拟链路，绝对照度的误差
天花板在器件本身——GL5528 阻值-照度是强非线性对数曲线、对蓝光敏感
对红光迟钝（光谱响应和人眼不一样）、同型号个体差异 ±20%、还要过
分压电阻和 ADC 两道转换。标定只能修其中一个点，修不了整条曲线。

BH1750 是 ROHM 的数字环境光传感器：光电二极管 + 放大 + ADC + I2C
全封装在芯片里，出厂带光电二极管特性标定，直接吐 16bit 照度值：
    lux = raw / 1.2
量程 1~65535 lx，高分辨率模式 1lx（120ms）/ 0.5lx（180ms），
典型精度 ±20%（白炽灯光源下偏差更大，datasheet 给的是修正式，
室内采光评估够用）。这是"精确数值"的性价比答案。

本类与 lib/light.py 的 LightSensor 接口完全对齐：
    sample(epoch, hour, minute) -> 分钟桶 dict | None
    snapshot() -> 近窗统计
字段名从 *_mv 换成 *_lux 语义（natural_lux / lamp_lux），
上报时按看板契约仍填 natural_mv / lamp_mv 两列（列名不动，值换成 lx
口径需在看板标注，或新表用 lx 列——见 README"换传感器后的数据口径"）。
main.py 切换只需换构造行。

两点实测相关：
1. ADDR 引脚悬空或接地 = 0x23，接高 = 0x5C。模块没印就是 0x23。
2. 65535 lx 封顶：室内窗边一般到不了，真顶了读数恒 65535——
   和当年 KY-018 的削平同一类现象，判读也一样：恒定满量程=超量程。
"""

import utime


class BH1750Sensor(object):

    ADDR = 0x23

    PWR_DOWN = 0x00
    PWR_ON = 0x01
    RESET = 0x07
    CONT_HRES1 = 0x10      # 连续高分辨率 1lx，约 120ms
    CONT_HRES2 = 0x11      # 连续高分辨率 0.5lx，约 180ms

    SATURATION_LUX = 65535

    def __init__(self, i2c, cfg):
        self._i2c = i2c
        self._cfg = cfg
        self._window = []
        self._dead = 0
        # 分钟桶（字段语义同 light.py，mV→lux；单位换算在出口做）
        self._sec_in_bucket = 0
        self._bucket = self._new_bucket()
        self._prev_lux = None
        self._lamp_event = False
        self._completed = []
        self.ok = self.init()

    # ------------------------------------------------------------------
    # 底层
    # ------------------------------------------------------------------

    def init(self):
        try:
            self._i2c.writeto(self.ADDR, bytes([self.PWR_ON]))
            utime.sleep_ms(10)
            self._i2c.writeto(self.ADDR, bytes([self.RESET]))
            utime.sleep_ms(10)
            self._i2c.writeto(self.ADDR, bytes([self.CONT_HRES1]))
            utime.sleep_ms(180)
            print("BH1750 ready")
            return True
        except Exception as e:
            print("BH1750 初始化失败：%s（查 I2C 接线/地址 0x23）" % e)
            return False

    def read_lux(self):
        """单次读数。失败 None；连续失败 10 次自动重初始化。"""
        try:
            data = self._i2c.readfrom(self.ADDR, 2)
            raw = (data[0] << 8) | data[1]
            self._dead = 0
            return raw / 1.2
        except Exception:
            self._dead += 1
            if self._dead >= 10:
                print("BH1750 连续无响应，重初始化")
                self.init()
                self._dead = 0
            return None

    def read_filtered(self):
        """滑窗均值（lux 线性，均值合法；KY-018 那边才需要看中位）。"""
        v = self.read_lux()
        if v is None:
            return None
        self._window.append(v)
        if len(self._window) > self._cfg.FILTER_WINDOW:
            self._window.pop(0)
        return sum(self._window) / len(self._window)

    # ------------------------------------------------------------------
    # 灯事件判定：与 light.py 同构，阈值走 config 的 *_LUX 组
    # ------------------------------------------------------------------

    def _detect_lamp(self, lux, hour):
        rise = getattr(self._cfg, "LAMP_RISE_LUX", 300)
        night_on = getattr(self._cfg, "LAMP_NIGHT_ON_LUX", 50)
        rising = self._prev_lux is not None and lux - self._prev_lux >= rise
        if rising:
            self._lamp_event = True
        elif self._lamp_event and lux <= night_on:
            self._lamp_event = False
        if not (self._cfg.DAY_START_HOUR <= hour < self._cfg.DAY_END_HOUR):
            if lux >= night_on:
                return True
        return self._lamp_event

    @staticmethod
    def _new_bucket():
        return {"sun_sec": 0, "lamp_sec": 0, "dark_sec": 0, "sat": 0,
                "n": 0, "sum": 0.0, "max": 0.0, "min": 1e9,
                "n_nat": 0, "sum_nat": 0.0, "n_lamp": 0, "sum_lamp": 0.0}

    # ------------------------------------------------------------------
    # 每秒一次
    # ------------------------------------------------------------------

    def sample(self, now_epoch, hour, minute):
        lux = self.read_filtered()
        b = self._bucket
        if lux is None:
            self._sec_in_bucket += 1
            if self._sec_in_bucket >= 60:
                return self._finish_bucket(hour * 60 + minute)
            return None

        b["n"] += 1
        b["sum"] += lux
        if lux >= self.SATURATION_LUX * 0.99:
            b["sat"] += 1
        if lux > b["max"]:
            b["max"] = lux
        if lux < b["min"]:
            b["min"] = lux

        active = getattr(self._cfg, "SUN_ACTIVE_LUX", 200)
        is_day = self._cfg.DAY_START_HOUR <= hour < self._cfg.DAY_END_HOUR
        lamp = self._detect_lamp(lux, hour)
        if lamp:
            b["lamp_sec"] += 1
            b["n_lamp"] += 1
            b["sum_lamp"] += lux
        elif is_day and lux >= active:
            b["sun_sec"] += 1
            b["n_nat"] += 1
            b["sum_nat"] += lux
        else:
            b["dark_sec"] += 1

        self._prev_lux = lux
        self._sec_in_bucket += 1
        if self._sec_in_bucket >= 60:
            return self._finish_bucket(hour * 60 + minute)
        return None

    def _finish_bucket(self, hm):
        b = self._bucket
        n = b["n"] or 1
        out = {
            "hm": hm,
            "sun_min": round(b["sun_sec"] / 60.0, 3),
            "lamp_min": round(b["lamp_sec"] / 60.0, 3),
            "dark_min": round(b["dark_sec"] / 60.0, 3),
            "sat_ratio": round(b["sat"] / float(n), 2),
            "avg_lux": round(b["sum"] / n, 1),
            "max_lux": round(b["max"], 1),
            "natural_lux": round(b["sum_nat"] / b["n_nat"], 1) if b["n_nat"] else 0,
            "lamp_lux": round(b["sum_lamp"] / b["n_lamp"], 1) if b["n_lamp"] else 0,
        }
        self._completed.append(out)
        keep = self._cfg.STATS_WINDOW_S
        if len(self._completed) > keep:
            self._completed = self._completed[-keep:]
        return out

    def snapshot(self):
        w = self._completed
        if not w:
            return {"buckets": 0, "sat_ratio_max": 0, "sun_min_sum": 0,
                    "lamp_min_sum": 0, "max_lux": 0,
                    "natural_lux": 0, "lamp_lux": 0}
        nat = [b["natural_lux"] for b in w if b["natural_lux"]]
        lampv = [b["lamp_lux"] for b in w if b["lamp_lux"]]
        return {
            "buckets": len(w),
            "sat_ratio_max": max(b["sat_ratio"] for b in w),
            "sun_min_sum": round(sum(b["sun_min"] for b in w), 1),
            "lamp_min_sum": round(sum(b["lamp_min"] for b in w), 1),
            "max_lux": max(b["max_lux"] for b in w),
            "natural_lux": max(nat) if nat else 0,
            "lamp_lux": max(lampv) if lampv else 0,
        }
