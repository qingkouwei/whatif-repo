# -*- coding: utf-8 -*-
"""
光照采集驱动 v2（KY-018 + 外部分压 + ADC）。

相对上一版修掉的三件事，每件都来自实测记录：

1. 极性。官方示例注释"光越强 ADC 越低"，实测恰好相反：
   光强读数高、捂住读数低（1702 vs 210）。极性写反叠加阈值逻辑，
   结果是"永远判暗"。这里把极性做成配置项 LIGHT_INVERT，
   默认 False（光强=读数高），上板对照 calib.py 的输出改。

2. 饱和判定。上一版拿量程上限 1200 当饱和线，但实测发现
   超量程区 ADC 仍能报出 1702 这种"看起来正常"的值——
   削平顶不在理论值上，只有实测才知道。所以 SATURATION_MV
   不写死理论值，改为"标定后填实测顶值"，并保留保守缺省。
   饱和计数（sat_count）是核心指标：一段曲线饱和率 >20%，
   意味着亮与亮之间的差异全丢了，必须降灵敏度（贴半透明遮挡
   或加大分压比），改完必须重新标定。

3. 灯光剔除。太阳不会在一分钟里把亮度抬起来，灯会。
   60 秒爬升率超阈值判"灯事件"，事件期间读数记入 lamp 分量；
   夜间（日落之后）高于灯启动线的部分同样归 lamp。
   自然光/灯光分开统计从设备端就做，上报的每个分钟桶自带两个分量。
   这是"白天开灯率"和"有效日照"两个指标都成立的前提。

ADC 返回值单位是 mV（模组管脚电压），不是 0-4095 码值。
这条被反复质疑过，判据在 adc单位判定.py：用量程内已知的四个
电压点对读数值，1.2V 量程按 mV 报就落在 300/600/820/1060 附近。
"""

import utime

import config


def cfg_divider(cfg):
    """分压比 k = R2/(R1+R2)。每次调用现读 config，
    因为下行指令会改这两个值，缓存会让改参失效。"""
    r1 = cfg.DIVIDER_R1_KOHM
    r2 = cfg.DIVIDER_R2_KOHM
    if r1 + r2 <= 0:
        return 1.0
    return r2 / (r1 + r2)


class LightSensor(object):

    def __init__(self, adc, cfg=config):
        self._adc = adc
        self._cfg = cfg
        self._channel = getattr(adc, cfg.ADC_CHANNEL)   # ADC0 / ADC1
        self._window = []
        # 分钟桶状态
        self._sec_in_bucket = 0
        self._bucket = self._new_bucket()
        self._prev_mv = None
        self._lamp_event = False          # 当前处于"灯事件"中
        self._completed = []              # 已完成的分钟桶（最多留 STATS_WINDOW_S 个）

    @staticmethod
    def _new_bucket():
        # 桶内一律按"秒"计数（1Hz 采样一秒归一类），出口才换算分钟。
        # 上一版这里字段名就叫 *_min、装的却是秒数，日账虚高 60 倍。
        # n_*_mv / sum_*_mv：按自然光/灯光归类的电压和与计数，
        # 上报时算出 natural_mv / lamp_mv 两分量，看板契约靠它。
        return {"sun_sec": 0, "lamp_sec": 0, "dark_sec": 0, "sat": 0,
                "n": 0, "sum": 0, "max": 0, "min": 99999,
                "n_nat": 0, "sum_nat": 0, "n_lamp": 0, "sum_lamp": 0}

    # ------------------------------------------------------------------
    # 底层读数
    # ------------------------------------------------------------------

    def _raw_mv(self):
        mv = self._adc.read(self._channel)
        if self._cfg.LIGHT_INVERT:
            # 反接模块：把量程内的读数翻转一下。顶值按饱和线取，
            # 不用理论 1200（真实顶在哪由标定说了算）。
            top = self._cfg.SATURATION_MV
            mv = top - mv if mv <= top else 0
        return mv

    def read_mv(self):
        """滑动均值后的分压节点电压（mV）。"""
        self._window.append(self._raw_mv())
        if len(self._window) > self._cfg.FILTER_WINDOW:
            self._window.pop(0)
        return sum(self._window) // len(self._window)

    def recover_source_mv(self, node_mv):
        """还原分压前模块输出电压：Vsource = Vnode / k（k 为分压比）。

        分压比实时从 config 读，不缓存——云端可以下发 set_divider
        改电阻参数，缓存值会跟着旧配置一起失效。
        """
        k = cfg_divider(self._cfg)
        return int(node_mv / k) if k > 0 else node_mv

    # ------------------------------------------------------------------
    # 灯事件判定：60 秒爬升率
    # ------------------------------------------------------------------

    def _detect_lamp(self, mv, hour):
        rising = (self._prev_mv is not None and
                  mv - self._prev_mv >= self._cfg.LAMP_RISE_MV)
        if rising:
            self._lamp_event = True
        elif self._lamp_event and mv <= self._cfg.LAMP_NIGHT_ON_MV:
            self._lamp_event = False      # 灯灭了，事件结束
        if not (self._cfg.DAY_START_HOUR <= hour < self._cfg.DAY_END_HOUR):
            if mv >= self._cfg.LAMP_NIGHT_ON_MV:
                return True               # 夜里有光，必是灯
        return self._lamp_event

    # ------------------------------------------------------------------
    # 每 1 秒调用一次
    # ------------------------------------------------------------------

    def sample(self, now_epoch, hour, minute):
        mv = self.read_mv()
        b = self._bucket
        b["n"] += 1
        b["sum"] += mv
        if mv >= self._cfg.SATURATION_MV:
            b["sat"] += 1
        if mv > b["max"]:
            b["max"] = mv
        if mv < b["min"]:
            b["min"] = mv

        is_day = self._cfg.DAY_START_HOUR <= hour < self._cfg.DAY_END_HOUR
        lamp = self._detect_lamp(mv, hour)
        # 三选一归类，一秒只进一个桶：
        #   灯事件 → lamp 分量；白天且亮度在活动线上 → 自然光；其余 → 暗
        # 同时把电压按类别累加，出口算 natural_mv / lamp_mv 两个分量，
        # 看板的 readings 表就是双列存储，缺一个分量它永远算不出灯光虚增。
        if lamp:
            b["lamp_sec"] += 1
            b["n_lamp"] += 1
            b["sum_lamp"] += mv
        elif is_day and mv >= self._cfg.SUN_ACTIVE_MV:
            b["sun_sec"] += 1
            b["n_nat"] += 1
            b["sum_nat"] += mv
        else:
            b["dark_sec"] += 1

        self._prev_mv = mv
        self._sec_in_bucket += 1

        if self._sec_in_bucket >= 60:
            done = self._finish_bucket(hour * 60 + minute)
            self._bucket = self._new_bucket()
            self._sec_in_bucket = 0
            return done
        return None

    def _finish_bucket(self, hm):
        b = self._bucket
        # 对外输出"分钟"口径（看板契约不变），内部计数一律按秒。
        # 60 秒一个桶，秒数除以 60 得该桶的分钟贡献（分数值）。
        n = b["n"] or 1
        out = {
            "hm": hm,
            "sun_min": round(b["sun_sec"] / 60.0, 3),
            "lamp_min": round(b["lamp_sec"] / 60.0, 3),
            "dark_min": round(b["dark_sec"] / 60.0, 3),
            "sat_ratio": round(b["sat"] / float(n), 2),
            "avg_mv": b["sum"] // n,
            "max_mv": b["max"] if b["n"] else 0,
            # 双电压分量：看板 readings 表按 natural_mv / lamp_mv 分列存，
            # 没有这两列就永远算不出"灯光虚增了多少日照"。
            "natural_mv": int(b["sum_nat"] / b["n_nat"]) if b["n_nat"] else 0,
            "lamp_mv": int(b["sum_lamp"] / b["n_lamp"]) if b["n_lamp"] else 0,
        }
        self._completed.append(out)
        keep = self._cfg.STATS_WINDOW_S
        if len(self._completed) > keep:
            self._completed = self._completed[-keep:]
        return out

    # ------------------------------------------------------------------

    def snapshot(self):
        """上报用的近窗统计。

        近窗 60 个桶、每桶至多贡献 1 分钟，所以 sun_min_sum
        的语义就是"最近一小时的自然光分钟数"，看板和告警都按这个口径用。
        """
        w = self._completed
        if not w:
            return {"buckets": 0, "sat_ratio_max": 0, "sun_min_sum": 0,
                    "lamp_min_sum": 0, "max_mv": 0,
                    "natural_mv": 0, "lamp_mv": 0}
        nat = [b["natural_mv"] for b in w if b.get("natural_mv")]
        lampv = [b["lamp_mv"] for b in w if b.get("lamp_mv")]
        return {
            "buckets": len(w),
            "sat_ratio_max": max(b["sat_ratio"] for b in w),
            "sun_min_sum": round(sum(b["sun_min"] for b in w), 1),
            "lamp_min_sum": round(sum(b["lamp_min"] for b in w), 1),
            "max_mv": max(b["max_mv"] for b in w),
            "natural_mv": max(nat) if nat else 0,
            "lamp_mv": max(lampv) if lampv else 0,
        }
