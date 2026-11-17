# -*- coding: utf-8 -*-
"""
一次学习会话内的状态记录（同桌项目核心）。

椅子上一坐就是一二十分钟，这段时长里要回答四个问题，
每个都只能从距离时序里读：

1. 人在不在：坐姿距离有正常区间，站起离开会超上界，
   趴桌凑近会低于下界——但"趴桌"和"离座"在单只超声波眼里
   都是"读数离开正常带"，方向相反恰好可以把两者分开。
2. 动得多不多：在座正常带内的逐秒抖动幅度（当秒最大最小差）。
3. 什么时候开始坐不住：给"移动频繁"打时间戳，是点评里最有用的数。
4. 离开过几次、每次多久：区分"去倒水"和"玩了一圈"靠的是时长分布，
   这段原始时间线要原样交给云端模型，别在设备侧先下结论。

时间全部由调用方以毫秒传入（now_ms），本模块不 import utime。
一来 QuecPython 的 ticks_diff 防回绕该在主循环算好再喂进来；
二来纯函数模块可以本地仿真，路演前一晚改阈值不用烧板子。

诚实约束：本模块只记"在座/前倾/离开"三种机械状态和抖动统计。
"困了""烦了""走神"这些词不许出现在这里——那是云端模型基于
状态时间线做的推断，推断错了是模型的措辞问题，
不能让设备假装测到了生理状态。
"""


class SessionRecorder(object):
    """一次学习会话的状态机。

    距离带（cm，传感器朝椅子方向的反射距离，需按实际桌面安装标定）：
        前倾/趴桌    < sit_lo（身体靠近桌面一侧的传感器）
        正常在座     sit_lo ~ sit_hi
        离开         > sit_hi，或连续超过 no_person_ms 没有有效读数
    """

    IN_SEAT = "in_seat"
    LEAN = "lean"
    AWAY = "away"
    UNKNOWN = "unknown"      # 尚无有效读数，维持上一状态，不切段

    def __init__(self, sit_lo=25.0, sit_hi=110.0, no_person_ms=8000,
                 move_spread_cm=12.0, fidget_window_s=30, fidget_min_moves=12):
        self._lo = sit_lo
        self._hi = sit_hi
        self._no_person_ms = no_person_ms

        # 移动判定用 30 秒滑动窗口内的"明显移动秒数"，不要求连续。
        # 孩子写作业的烦躁从来不是连续扭动 20 秒，而是这半分钟里
        # 累计十秒以上停不下来——要求连续会把典型场景整段漏掉。
        self._move_spread = move_spread_cm
        self._win_max = fidget_window_s
        self._fidget_min = fidget_min_moves

        self._win = []                 # 1=当秒明显移动，0=基本不动
        self._fidget = False
        self._fidget_at = None         # 首次进入"频繁移动"的时刻（ms）

        self._state = self.UNKNOWN
        self._seg_state = self.UNKNOWN
        self._seg_start = 0            # 当前时间段起点（ms）
        self._segments = []            # [(state, dur_s), ...]
        self._session_start = None
        self._last_valid = 0

        self._sec_start = None           # 当前"秒"的起点（ms）。
                                         # 哨兵必须是 None：now_ms 可以合法地为 0
        self._sec_moved = False        # 这一秒内是否出现过超标抖动
        self._dwell_sum = 0.0
        self._dwell_n = 0

    # ------------------------------------------------------------------

    def start(self, now_ms):
        """会话开始。由主循环在"读数稳定进入正常带"时调用，不用按钮。"""
        self._session_start = now_ms
        self._seg_start = now_ms
        self._state = self.IN_SEAT
        self._seg_state = self.IN_SEAT
        self._last_valid = now_ms

    def _close_segment(self, now_ms):
        dur_s = max(0.0, (now_ms - self._seg_start) / 1000.0)
        if dur_s > 0.5:                # 半秒以下的毛刺不记
            self._segments.append((self._seg_state, round(dur_s, 1)))
        self._seg_start = now_ms

    def _transition(self, new_state, now_ms):
        if new_state == self._state:
            return
        self._close_segment(now_ms)
        self._seg_state = new_state
        self._state = new_state

    def _classify(self, dist_cm, now_ms):
        if dist_cm is None:
            if now_ms - self._last_valid > self._no_person_ms:
                return self.AWAY
            return self.UNKNOWN
        self._last_valid = now_ms
        if dist_cm < self._lo:
            return self.LEAN
        if dist_cm <= self._hi:
            return self.IN_SEAT
        return self.AWAY

    # ------------------------------------------------------------------

    def feed(self, dist_cm, spread_cm, now_ms):
        """主循环每帧调用。

        dist_cm:   当帧滤波后距离（cm），无有效回波传 None
        spread_cm: 当秒内最大最小差，用于移动判定；没有就传 None
        now_ms:    utime.ticks_ms()（差值由调用方保证单调即可）
        返回当前状态字符串。
        """
        self._transition(self._classify(dist_cm, now_ms), now_ms)

        # 按秒结算滑动窗口：feed 是 10Hz 调用，攒满 1000ms 推进一格
        if self._sec_start is None:
            self._sec_start = now_ms
        if now_ms - self._sec_start >= 1000:
            moved = self._sec_moved or (
                spread_cm is not None and spread_cm >= self._move_spread)
            self._win.append(1 if moved else 0)
            if len(self._win) > self._win_max:
                self._win.pop(0)
            self._sec_start = now_ms
            self._sec_moved = False

            if self._state in (self.IN_SEAT, self.LEAN) and dist_cm is not None:
                self._dwell_sum += dist_cm
                self._dwell_n += 1

            if (not self._fidget and len(self._win) >= self._win_max
                    and sum(self._win) >= self._fidget_min):
                self._fidget = True
                if self._fidget_at is None:
                    self._fidget_at = now_ms
            elif self._fidget and sum(self._win[-10:]) <= 2:
                self._fidget = False     # 安静下来了，允许再次触发计时
                self._win = self._win    # （fidget_at 保留首次值不变）

        elif spread_cm is not None and spread_cm >= self._move_spread:
            self._sec_moved = True

        return self._state

    @property
    def state(self):
        return self._state

    @property
    def fidget(self):
        return self._fidget

    # ------------------------------------------------------------------

    def summary(self, now_ms):
        """会话结束（或随时）取时间线。原样交给云端，别在这里写结论。"""
        self._close_segment(now_ms)
        total = sum(d for _, d in self._segments)
        by_state = {}
        for st, d in self._segments:
            by_state[st] = round(by_state.get(st, 0.0) + d, 1)
        return {
            "total_s": round(total, 1),
            "in_seat_s": by_state.get(self.IN_SEAT, 0),
            "lean_s": by_state.get(self.LEAN, 0),
            "away_s": by_state.get(self.AWAY, 0),
            "away_times": sum(1 for st, _ in self._segments if st == self.AWAY),
            "lean_times": sum(1 for st, _ in self._segments if st == self.LEAN),
            "ever_fidgety": self._fidget_at is not None,
            # 从坐下到第一次"频繁移动"的秒数。点评里最有用的一个数：
            # 十分钟的孩子和四十分钟的孩子，差别在这一个字段上。
            "fidget_after_s": round((self._fidget_at - self._session_start) / 1000.0, 1)
                              if (self._fidget_at is not None
                                  and self._session_start is not None) else None,
            "avg_dist_cm": round(self._dwell_sum / self._dwell_n, 1)
                           if self._dwell_n else None,
            "timeline": [(st, d) for st, d in self._segments],
        }
