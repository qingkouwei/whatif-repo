# -*- coding: utf-8 -*-
"""
方案 A「凑近才说」的行为判定层。

这一层解决一个真实问题：原设计里模型只在启动时写三档文案，
档位切换全靠状态机，模型并不参与决策。评委问"AI 在哪"，
诚实的答案只有"生成了文本"，这在"AI 赋能实体硬件"的赛题下站不住。

改成：模型读观众这段时间的距离轨迹，先判断这个人在干什么
（路过 / 犹豫 / 试探 / 结伴 / 驻足），再决定对这种人说什么。
内容随人不同，是因为 AI 读到的行为不同，不是因为模板换了变量。

顺带解决一个超声波的物理限制：HC-SR04 只有一个换能器，
读到的是最近的反射面。两个人一起凑近时，读数会在两个距离之间跳，
规则法会判成"档位抖动"，模型能从 spread（每秒最大最小差）
看出这是两个人。这是规则做不到、需要世界知识的地方。

轨迹压缩：设备 10Hz 采样，30 秒就是 300 点，喂模型太多也噪声大。
每秒取 (min_cm, spread_cm) 两个数，30 秒压成 60 个数。
min 反映这个人到底凑到多近，spread 反映是不是多人或手在晃。
"""

import json
import re

import httpx


# 行为类别。给模型的清单，也是看板显示的标签。
BEHAVIORS = {
    "passing":   "路过，没有停下来，距离一直很远",
    "hesitant":  "犹豫，靠近又退开，反复几次才决定凑近",
    "probing":   "试探，先停在中距离观察，然后一次性凑到最近",
    "group":     "结伴，轨迹抖动大，像是两个人以上一起凑近",
    "lingering": "驻足，凑到很近并且待了很久不走",
}


def heuristic_behavior(trace, dwell_s):
    """本地兜底判定。刻意只分四类，且判不出"结伴"——spread 要和轨迹
    形状一起理解，规则写死阈值在真实数据上会乱判。断网或模型失败时
    用它，source 标 heuristic，看板如实显示。

    trace: [{"min": cm, "spread": cm}, ...] 每秒一项
    返回: (behavior_key, reason)
    """
    if not trace:
        return None, "没有轨迹数据"

    mins = [p["min"] for p in trace if p.get("min")]
    if not mins:
        return None, "轨迹里没有有效距离"

    nearest = min(mins)
    valid_ratio = len(mins) / float(len(trace))

    if nearest > 150 or valid_ratio < 0.2:
        return "passing", "最近 %.0fcm，全程远距离" % nearest

    # 反复进出：统计"进中距离再退回"的转折次数
    swings = 0
    in_near = False
    for m in mins:
        if m <= 115 and not in_near:
            in_near = True
        elif m > 115 and in_near:
            in_near = False
            swings += 1

    if swings >= 2:
        return "hesitant", "进出中距离 %d 次，最近 %.0fcm" % (swings, nearest)

    if nearest <= 45 and dwell_s and dwell_s >= 20:
        return "lingering", "凑到 %.0fcm 并停留 %d 秒" % (nearest, int(dwell_s))

    if nearest <= 45:
        return "probing", "一次性凑到 %.0fcm" % nearest

    return "hesitant", "最近 %.0fcm，未进入最近档" % nearest


def trace_text(trace):
    """把轨迹转成给模型看的文本。逐秒一项，无读数标注出来。"""
    if not trace:
        return "（无轨迹）"
    parts = []
    for i, p in enumerate(trace, 1):
        mn = p.get("min")
        sp = p.get("spread", 0)
        if mn is None:
            parts.append("%ds:无读数" % i)
        else:
            parts.append("%ds:%dcm(抖%d)" % (i, int(mn), int(sp)))
    return " ".join(parts)


def _build_messages(trace, dwell_s, env, stats):
    """构造提示词。拆成独立函数，避免把多层括号塞进一个字面量里。"""
    system_text = (
        "你是一台放在展会摊位上的装置，只有一只超声波测距、一只温湿度计"
        "和一只光敏电阻。有人在你面前活动了一段时间，你只看到自己的距离读数"
        "轨迹。你要判断这个人在干什么，然后决定对他说什么。不要客套，不要"
        "解释你是谁，输出会被程序解析，所以严格按要求出 JSON，不要多余文字。"
    )
    behavior_lines = "\n".join(
        '%s = "%s"' % (k, v) for k, v in BEHAVIORS.items())
    trace_lines = trace_text(trace)
    user_text = (
        "距离轨迹（每秒两项：最近距离 cm、当秒最大最小差 cm，抖动大说明可能"
        "有两个人或手在晃；无读数表示那一秒没测到反射）：\n" + trace_lines +
        "\n\n这个人在近档停留了约 %d 秒。\n"
        "当前环境温度约 %s℃，光敏读数 %s mV。\n"
        "这台装置今天已经接待过 %s 人次，其中 %s 人凑到过最近档。\n\n"
        "第一步，从下面 5 种行为里选一种最符合轨迹的：\n%s\n\n"
        "第二步，基于你判断出的行为，写这个人凑到最近时屏幕上显示的话。"
        "要求 80 到 160 字，两三句，必须引用轨迹里的至少一个真实数字"
        "（他凑到多少厘米、停留多久、抖动多大），可以有一句不好听的判断。"
        "不要加引号，不要 Markdown，直接是给人看的中文。\n\n"
        "严格 JSON 输出，不要任何多余文字：\n"
        '{"behavior": "key", "confidence": 0.0, '
        '"reason": "判断依据不超过30字", "close_text": "..."}'
    ) % (dwell_s,
         env.get("temp_c", "?"), env.get("lux_mv", "?"),
         stats.get("visits", "?"), stats.get("close_visits", "?"),
         behavior_lines)
    return [{"role": "system", "content": system_text},
            {"role": "user", "content": user_text}]


async def judge(client, base, key, model, payload, timeout=12):
    """读轨迹 + 当天统计，返回模型的行为判定和它决定说的话。

    payload: {"trace": [...], "dwell_s": int, "env": {...}, "stats": {...}}
    返回: {"behavior", "reason", "close_text", "source", "confidence", ...}
    """
    trace = payload.get("trace") or []
    dwell_s = payload.get("dwell_s") or 0
    env = payload.get("env") or {}
    stats = payload.get("stats") or {}

    # 兜底永远先算好，模型不可用时直接用
    h_beh, h_reason = heuristic_behavior(trace, dwell_s)

    if not (key and model):
        return {
            "behavior": h_beh, "reason": h_reason,
            "source": "heuristic", "confidence": 0,
            "note": "未配置模型，本地规则判定（判不出结伴）",
        }

    messages = _build_messages(trace, dwell_s, env, stats)
    import llm_provider
    body = {"model": model, "messages": messages,
            "max_tokens": 400, "temperature": 0.6, "top_p": 0.95}
    # provider 名由调用方（app.py）经参数传入最干净，但 judge 的签名
    # 已被两处使用，不改签名：maas/aliyuncs 域名只有 DashScope 系会用
    body.update(llm_provider.extra_body(
        "dashscope" if ("maas.aliyuncs" in base or "dashscope" in base) else ""))
    try:
        r = await client.post(
            base + "/chat/completions",
            headers={"Authorization": "Bearer " + key,
                     "Content-Type": "application/json",
                     "Accept": "application/json"},
            json=body,
            timeout=timeout)
        if r.status_code == 429:
            raise RuntimeError("NIM 限流 429")
        r.raise_for_status()
        raw = r.json()["choices"][0]["message"]["content"].strip()
        parsed = json.loads(_strip_fence(raw))

        beh = parsed.get("behavior")
        if beh not in BEHAVIORS:
            raise RuntimeError("模型返回了清单外的行为: %s" % beh)
        close_text = str(parsed.get("close_text", "")).strip().strip('"')
        if len(close_text) < 10:
            raise RuntimeError("close_text 太短不可用")

        return {
            "behavior": beh,
            "behavior_label": BEHAVIORS[beh],
            "confidence": float(parsed.get("confidence", 0) or 0),
            "reason": str(parsed.get("reason", ""))[:80],
            "close_text": _clean(close_text),
            "source": "nim",
            # 留着对照，讲评时能说清模型和规则差在哪
            "local_behavior": h_beh,
        }
    except Exception as e:
        return {
            "behavior": h_beh, "reason": h_reason,
            "source": "heuristic", "confidence": 0,
            "note": "模型调用失败退回本地规则：%s: %s" % (type(e).__name__, e),
        }


def _clean(t):
    t = re.sub(r"^\**", "", t.strip())
    t = re.sub(r"^(答|回答|观察|装置|文案|屏幕)\s*[：:]\s*", "", t)
    return t[:400]


def _strip_fence(s):
    s = re.sub(r"^```[a-z]*\s*", "", s.strip())
    s = re.sub(r"```\s*$", "", s)
    m = re.search(r"\{.*\}", s, re.S)
    return m.group(0) if m else s
