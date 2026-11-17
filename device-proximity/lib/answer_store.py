# -*- coding: utf-8 -*-
"""
三档文案的本地存储。

这里有一个刻意的架构选择，是整个 demo 能扛住现场网络的原因：

    设备启动时一次性把某个话题的三档答案全部拉回来，落盘。
    之后演示过程中不再依赖网络。

理由很直白：路演厅里 20 支队伍加几百号人抢网络，蜂窝侧信道同样拥挤。
如果每次升档都现场发一次 HTTP 请求、等一次模型生成，那么评委最不想
看到的画面——"观众凑近了，屏幕转圈，然后什么都没发生"——一定会发生。

所以模型调用发生在云端预热阶段，设备只做本地渲染。网络的角色从
"演示链路的一环"降级为"可选的刷新通道"。故障域直接消失。

写入用先临时文件再 rename：/usr 是 NOR flash，直接覆写若遇掉电
会留下半个 JSON，下次启动解析失败，缓存全废。rename 在同一文件系统
内是原子操作，要么完整成功要么完全没变。
"""

import uos
import ujson


BUILTIN_FALLBACK = {
    1: "有人在靠近。",
    2: "你凑得很近了。我读到距离 38 厘米，温度 26 度，展位二氧化碳大概正在往上走。"
       "这种距离上人类不会待超过二十秒，所以你的问题是好奇，不是需要信息。",
}

TIERS = (1, 2)


class AnswerStore(object):

    def __init__(self, path):
        self._path = path
        self._data = {"topic_id": None, "topic": "", "answers": {}, "ts": 0}
        self.load()

    # ------------------------------------------------------------------

    def load(self):
        try:
            if self._path in uos.listdir("/usr"):
                f = open(self._path, "r")
                raw = f.read()
                f.close()
                self._data = ujson.loads(raw)
        except Exception as e:
            print("缓存读取失败，忽略：%s" % e)
            self._data = {"topic_id": None, "topic": "", "answers": {}, "ts": 0}
        return self._data

    def save(self):
        tmp = self._path + ".tmp"
        f = open(tmp, "w")
        f.write(ujson.dumps(self._data))
        f.close()
        # 目标已存在则先删，uos.rename 在部分平台不覆盖
        try:
            uos.remove(self._path)
        except OSError:
            pass
        uos.rename(tmp, self._path)

    # ------------------------------------------------------------------

    def replace(self, topic_id, topic, answers, ts):
        """整体替换。answers 形如 {"1": "...", "2": "..."}"""
        merged = {}
        for k in TIERS:
            val = answers.get(str(k)) or answers.get(k)
            if val:
                merged[str(k)] = val
        if not merged:
            return False
        self._data = {"topic_id": topic_id, "topic": topic,
                      "answers": merged, "ts": ts}
        self.save()
        return True

    def get(self, tier):
        val = self._data.get("answers", {}).get(str(tier))
        if val:
            return val, False          # (文本, 是否走内置兜底)
        return BUILTIN_FALLBACK.get(tier, "……"), True

    @property
    def topic_id(self):
        return self._data.get("topic_id")

    @property
    def topic(self):
        return self._data.get("topic", "")

    @property
    def ts(self):
        return self._data.get("ts", 0)

    def coverage(self):
        """已缓存的档位数，用于启动时判断是否需要强制拉取。"""
        return len(self._data.get("answers", {}))
