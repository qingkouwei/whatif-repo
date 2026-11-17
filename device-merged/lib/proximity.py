# -*- coding: utf-8 -*-
"""
把连续的距离读数变成一个稳定的档位判定。

这个模块是整个 demo 的语义核心。硬件读到的是噪声，观众看到的是
「我凑近了它才肯说」，中间必须有状态机把噪声吃掉。

档位定义：
    0  OUTSIDE   远距离    设备不理人
    1  MID       中距离    AI 给一句话
    2  CLOSE     近距离    AI 给完整内容

设计要点（都是踩过的坑）：
1. 双阈值死区。单阈值在换能器抖动下会在临界点反复横跳，
   进入线和离开线分开之后，抖动落在死区里就不影响判定。
2. 升档慢、降档快。升档要连续确认帧数是为了防误触发；
   降档快是刻意留下的，被切断的瞬间是观众会主动重复动作的理由。
3. 距离滤波取窗口最小值而不是均值。这个场景要的是"离设备最近的物体"，
   均值会被远处背景拉高，导致观众已经凑近却还判在低档。
"""


class ProximityJudge(object):

    OUTSIDE = 0
    MID = 1
    CLOSE = 2

    LABEL = {0: "OUTSIDE", 1: "MID", 2: "CLOSE"}

    def __init__(self, cfg):
        self._t2_in = cfg["tier2_enter"]
        self._t2_out = cfg["tier2_leave"]
        self._t1_in = cfg["tier1_enter"]
        self._t1_out = cfg["tier1_leave"]
        self._raise = cfg["raise_frames"]
        self._lower = cfg["lower_frames"]

        self._tier = self.OUTSIDE
        self._pending = self.OUTSIDE
        self._count = 0
        self._last_seen_ms = 0

    # ------------------------------------------------------------------
    # 距离 -> 裸档位（不带滞回，先算出"这一次读数该在哪档"）
    # ------------------------------------------------------------------
    def _raw_tier(self, dist_cm):
        if dist_cm <= self._t2_in:
            return self.CLOSE
        if dist_cm <= self._t1_in:
            return self.MID
        return self.OUTSIDE

    # ------------------------------------------------------------------
    # 滞回：已经进入高档时，只有退过"离开线"才降档
    # ------------------------------------------------------------------
    def _raw_tier_with_hysteresis(self, dist_cm, current):
        if current == self.CLOSE:
            # 已在近档，退到 T2_LEAVE 以上才降级
            if dist_cm <= self._t2_out:
                return self.CLOSE
            return self.MID if dist_cm <= self._t1_out else self.OUTSIDE
        if current == self.MID:
            # 已在中档，可以更近升档，但要退过 T1_LEAVE 才出中档
            if dist_cm <= self._t2_in:
                return self.CLOSE
            if dist_cm >= self._t1_out:
                return self.OUTSIDE
            return self.MID
        # 当前 OUTSIDE：只能往上走
        return self._raw_tier(dist_cm)

    # ------------------------------------------------------------------
    # 主入口
    # ------------------------------------------------------------------
    def feed(self, dist_cm, now_ms):
        """喂入一帧距离读数，返回当前稳定档位。

        dist_cm 为 None 表示这一帧没有有效回波（无人或超量程）。
        """
        if dist_cm is None:
            self._last_seen_ms = 0
            target = self.OUTSIDE
        else:
            self._last_seen_ms = now_ms
            target = self._raw_tier_with_hysteresis(dist_cm, self._tier)

        if target == self._pending:
            self._count += 1
        else:
            self._pending = target
            self._count = 1

        # 升档需要更多帧，降档一帧就够
        need = self._raise if target > self._tier else self._lower
        if self._count >= need and target != self._tier:
            self._tier = target
            return True      # 档位发生变化

        return False

    @property
    def tier(self):
        return self._tier

    @property
    def tier_name(self):
        return self.LABEL[self._tier]

    def timeout(self, now_ms, no_echo_timeout_ms):
        """长时间收不到回波则强制回 0 档。返回是否发生档位变化。"""
        if self._tier == self.OUTSIDE:
            return False
        if self._last_seen_ms and (now_ms - self._last_seen_ms) > no_echo_timeout_ms:
            self._tier = self.OUTSIDE
            self._pending = self.OUTSIDE
            self._count = 0
            self._last_seen_ms = 0
            return True
        return False
