# -*- coding: utf-8 -*-
"""
档位内容的渲染层。

把"文字往哪里显示"从主逻辑里剥出来，是因为手上套件的显示介质
信息量有限（单位数码管只能出 0-9），而 demo 的三档差异必须让人
一眼看出"内容变多了"。这一层做成可插拔，接 OLED 时只改这里。

当前实现：
    ConsoleRenderer  REPL 打印，开发期用
    LedgerRenderer   数码管，退化成"距离尾数 + 字数"这类弱表达
    OledRenderer     SSD1306 128x64，路演主介质（需外接，见 docs/物料.md）

三档的信息量落差是整个作品的表达核心。渲染时不只是"显示多少字"，
而是让观众看见"它正在斟酌要不要告诉你"：
    档 0  拒绝句 + 红灯
    档 1  一句话，行宽截断，明显没说完 + 黄灯
    档 2  完整段落逐行滚动 + 绿灯
"""

import utime


class BaseRenderer(object):

    def show(self, tier, text, meta):
        raise NotImplementedError

    def tick(self, now_ms):
        pass


class ConsoleRenderer(BaseRenderer):

    def __init__(self, out=None):
        self._lines_shown = 0

    def show(self, tier, text, meta):
        if tier == 0:
            print("\n\x1b[31m[OUTSIDE] %s\x1b[0m" % text)
        elif tier == 1:
            print("\n\x1b[33m[MID] %s\x1b[0m" % text)
            print("      （还有 %d 字，退远了不给看）" % max(0, meta.get("full_len", 0) - len(text)))
        else:
            print("\n\x1b[32m[CLOSE] %s\x1b[0m" % text)

    def tick(self, now_ms):
        pass


class LedgerRenderer(BaseRenderer):
    """数码管退化版：近档时把字数按位滚动显示，中档只显示一位。

    这是没有屏时的兜底表达，说实话很弱——它能传达"信息变多了"，
    但传达不了内容本身。所以真上路演必须配屏。
    """

    def __init__(self, tube):
        self._tube = tube
        self._seq = []
        self._idx = 0
        self._next = 0

    def show(self, tier, text, meta):
        if tier == 0:
            self._seq = []
            return
        n = len(text)
        if tier == 1:
            self._seq = [n % 10]
        else:
            self._seq = [int(c) for c in str(n)]
        self._idx = 0

    def tick(self, now_ms):
        if not self._seq or now_ms < self._next:
            return
        self._tube.show(self._seq[self._idx % len(self._seq)])
        self._idx += 1
        self._next = now_ms + 600


class OledRenderer(BaseRenderer):
    """SSD1306。驱动留接口，接到具体库后再填。

    近档逐行滚动的节奏（每行停留 ms）是可调的：太快观众读不完，
    太慢显得设备卡。按中文 4 字/秒的阅读速度取 2200ms 比较稳。
    """

    LINE_MS = 2200

    def __init__(self, device):
        self._dev = device
        self._lines = []
        self._idx = 0
        self._next = 0

    def show(self, tier, text, meta):
        if not text:
            self._lines = []
            return
        self._lines = _wrap(text, 16)
        if tier == 1:
            self._lines = self._lines[:1]
        self._idx = 0
        self._next = 0

    def tick(self, now_ms):
        if not self._lines or now_ms < self._next:
            return
        self._dev.clear()
        self._dev.drawstr(0, 0, self._lines[self._idx % len(self._lines)])
        self._dev.display()
        self._idx += 1
        self._next = now_ms + self.LINE_MS


def _wrap(s, width):
    out = []
    while s:
        out.append(s[:width])
        s = s[width:]
    return out
