# -*- coding: utf-8 -*-
"""
方案 B「光曲线侦探」的核心：光时序理解。

与方案 A 的本质区别在这里，必须讲清楚：

    方案 A 用大模型**生成**内容（文案），数据只是提示词里的引用；
    方案 B 用大模型**理解**传感时序（一条光电压曲线），
    输出的是对物理世界的判断。这才是评委想看的"AI 赋能硬件"。

玩法：观众用手电筒、握住、挥手、开关台灯，15 秒内造一段
光照曲线；AI 只看数据，猜刚才发生了什么。猜错全场笑，猜对全场"哦"。
无论对错都是好演示——它对模型时序理解能力的要求恰好卡在
LLM 能做、规则难做对的区间：曲线是 10Hz 电压序列，传统阈值规则
分不开"阴天云影"和"手在光前挥"，但带世界知识的模型能。

诚实约定（同方案 A，一条底线贯穿两个作品）：
  - 每次判定带 source：nim（真模型）/ heuristic（本地规则兜底）
  - 看板与演示台词绝不把 heuristic 说成 AI
  - 本地规则刻意写得"笨"：它只能分 4 类基本型，
    演示时正好用这个落差衬托模型看错了什么、看对了什么

喂给模型的曲线怎么压缩：10Hz × 15s = 150 点太多且噪声大。
取 1 秒中位数得 15 个整数值（mV，分压后），序列本身已经足够
人类和模型读出"盖住/照射手/平稳"的形状。这同时是一个
"有限算力/有限上下文"的合理工程选择，对题方向 04。
"""

import json
import re

import httpx


# 场景类别。label 给模型，key 是协议。保持 5 类——超过 6 类
# 观众出题难度和 AI 猜中的观感都会崩。
SCENES = {
    "covered":   "手或物体把传感器完全握住/盖住",
    "torch":     "手电筒直射或近距离晃动",
    "waving":    "手在光前面来回挥动（遮挡又放开，周期性）",
    "steady":    "平稳光源（台灯、展厅顶灯），无明显变化",
    "daylight":  "自然日光方向的光，带缓慢起伏（云影/角度变化）",
}


class LightGuesser(object):

    def __init__(self, nim_base, nim_key, model):
        self._base = nim_base
        self._key = nim_key
        self._model = model
        self.last_error = None

    # ------------------------------------------------------------------
    # 特征提取：先算几个数，既给兜底规则用，也附在提示词里
    # 帮模型对齐"它看到的数字"和"它的直觉"
    # ------------------------------------------------------------------
    @staticmethod
    def features(series):
        """series: 每秒一个 mV 中位数，长度约 10~20。"""
        if not series:
            return None
        n = len(series)
        lo, hi = min(series), max(series)
        mean = sum(series) / n
        # 过零率：相邻差符号翻转次数 / n，用来区分"周期挥手"和"单次遮挡"
        diffs = [series[i + 1] - series[i] for i in range(n - 1)]
        flips = 0
        for i in range(len(diffs) - 1):
            if diffs[i] * diffs[i + 1] < 0 and abs(diffs[i]) > 40:
                flips += 1
        # 低占比：低于 (lo+hi)/2 的采样比例
        mid = (lo + hi) / 2.0
        low_ratio = sum(1 for v in series if v < mid) / float(n)
        # 首尾变化：缓慢爬升/下降（日光角度）与突变（遮挡）的分界
        edge = abs(series[-1] - series[0])
        return {
            "n": n, "min_mv": lo, "max_mv": hi, "mean_mv": round(mean, 1),
            "range_mv": hi - lo, "flips": flips, "flip_rate": round(flips / float(n), 2),
            "low_ratio": round(low_ratio, 2), "edge_delta_mv": edge,
        }

    # ------------------------------------------------------------------
    # 本地兜底规则（刻意笨，只当基础分型器用）
    # ------------------------------------------------------------------
    # 规则顺序是有讲究的，合成曲线测出两版误判才定下来：
    #   先判平稳（range 小），再判手电（峰值顶到量程高位——这条必须放在
    #   covered 前面，否则"基线低值占比高"会把直射误吞成握住），
    #   covered 必须叠加"最低值真的贴地"，waving 看过零率，
    #   最后剩余的大幅缓慢变化才落 daylight。
    # 这五条规则能分对典型合成曲线，但对真实手抖、斜入手电的光斑
    # 会分错——这是故意的。演示时用它和模型判定做对照。
    # ------------------------------------------------------------------
    @staticmethod
    def heuristic(f):
        if f is None:
            return None
        if f["range_mv"] < 120:
            return "steady"
        if f["max_mv"] >= 900:
            return "torch"
        if f["low_ratio"] > 0.55 and f["flip_rate"] < 0.25 and f["min_mv"] <= 150:
            return "covered"
        if f["flip_rate"] >= 0.35:
            return "waving"
        if f["range_mv"] >= 150:
            return "daylight"
        return "steady"

    # ------------------------------------------------------------------
    # 模型判定
    # ------------------------------------------------------------------
    async def guess(self, series):
        """series -> {"scene":..., "reason":..., "confidence":..., "source":...}"""
        f = self.features(series)
        if f is None:
            return {"scene": None, "source": "invalid", "reason": "空曲线"}

        if not (self._key and self._model):
            scene = self.heuristic(f)
            return {"scene": scene, "confidence": 0,
                    "reason": "本地规则判定（未配置模型，非 AI）",
                    "source": "heuristic", "features": f}

        prompt = [
            {"role": "system",
             "content": "你是一台只有一只光敏电阻的硬件装置。有人在你面前用"
                        "手、手电筒、台灯制造了一段 15 秒左右的遮挡与光照变化，"
                        "你只看到自己传感器的读数序列，要推断刚才发生了什么。"
                        "读数单位是分压后的电压 mV（大约 0=全黑，600+ = 强光直射，"
                        "展厅台灯一般 300-500，手电贴近能顶满量程）。"
                        "不要客套，不要解释你能看到什么，直接判断。"},
            {"role": "user",
             "content": "读数序列（每 1 秒一个中位数，mV）：\n%s\n\n"
                        "辅助统计：range=%smV，过零翻转率=%.2f，低值占比=%.2f，"
                        "首尾差=%smV，峰值=%smV\n\n"
                        "从下面 5 种场景里选一种最可能的，并给 0-1 的置信度：\n%s\n\n"
                        "严格 JSON 输出：\n"
                        '{"scene": "key", "confidence": 0.0, "reason": "不超过40字的判断依据"}'
                        % (json.dumps(series),
                           f["range_mv"], f["flip_rate"], f["low_ratio"],
                           f["edge_delta_mv"], f["max_mv"],
                           "\n".join('%s = "%s"' % (k, v) for k, v in SCENES.items()))},
        ]
        try:
            async with httpx.AsyncClient() as client:
                r = await client.post(
                    "%s/chat/completions" % self._base,
                    headers={"Authorization": "Bearer %s" % self._key,
                             "Content-Type": "application/json"},
                    json={"model": self._model, "messages": prompt,
                          "max_tokens": 200, "temperature": 0.2},
                    timeout=20)
                if r.status_code == 429:
                    raise RuntimeError("NIM 限流 429")
                r.raise_for_status()
                text = r.json()["choices"][0]["message"]["content"].strip()
                parsed = json.loads(_strip_fence(text))
                if parsed.get("scene") in SCENES:
                    return {"scene": parsed["scene"],
                            "confidence": float(parsed.get("confidence", 0)),
                            "reason": str(parsed.get("reason", ""))[:120],
                            "source": "nim", "features": f}
                raise RuntimeError("模型返回了不在清单里的场景: %s" % parsed.get("scene"))
        except Exception as e:
            self.last_error = "%s: %s" % (type(e).__name__, e)
            scene = self.heuristic(f)
            return {"scene": scene, "confidence": 0,
                    "reason": "模型调用失败退回本地规则（非 AI）：%s" % self.last_error,
                    "source": "heuristic", "features": f}


def _strip_fence(s):
    s = re.sub(r"^```[a-z]*\s*", "", s.strip())
    s = re.sub(r"```\s*$", "", s)
    m = re.search(r"\{.*\}", s, re.S)
    return m.group(0) if m else s
