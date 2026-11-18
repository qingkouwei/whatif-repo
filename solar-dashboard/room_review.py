# -*- coding: utf-8 -*-
"""
项目二「AI房评师」的评估生成层。

定位讲清楚，避免自吹：
    硬件只测一样东西——一个房间连续几天的光照时序（自然光/灯光分列）。
    朝向、楼层、窗地比、遮挡这些房屋结构数据来自房源平台录入，不是传感来的。
    本模块把"测到的光况统计 + 录入的结构信息"交给云端模型，
    产出一份能进房源页和质检流程的评估。三者的来源在输出里分列标注。

模型在这里干的三件活（都是 LLM 真正擅长的，不是套壳）：
    1. 把一串光况统计翻译成买家/经纪人能读懂的居住建议（晾衣服、补光、摆位）
    2. 生成房源挂牌用的一句光照结论
    3. 数据可信度诊断：曲线是不是被台灯污染、传感器是不是装反、缺了哪天

两条硬边界，写进系统提示词也写进本模块注释：
    · 光敏无照度标定。输出只允许"光况活跃度分级、时长、占比"这类相对结果，
      严禁出现 lux 绝对值或"符合 GB 50034"这类合规结论。分级名就叫
      "光况活跃度"，不叫"照度等级"，从用词上掐死误读。
    · 单点测量。一个盒子一个房间，不许外推"整屋采光"。

分级规则本身在设备/服务端算（grade_from_stats），模型只负责把它说成人话。
规则是确定的、可复现的；措辞是生成的。这两件事分开，评估才可审计——
房源平台不会因为模型某次发挥不稳定就拿到两个不同的分级。
"""

import json
import re

import httpx


GRADE_DEF = {
    "A":  "全天光照充沛，多数时段不需要开灯",
    "B":  "光照良好，个别时段或个别天偏弱",
    "C":  "光照偏弱，白天多数时间需要补光",
    "D":  "基本无有效日照，长期依赖人工照明",
}

# 分级只看两件事：有效日照时长的中位数（稳定性）+ 三天里最好那天能到多少（上限）。
# 中位数抗单日异常（阴天），上限防止"每天都还行但都不突出"被高估。
# 阈值是经验值，且明确不是照度阈值——它分的是"这个盒子记录的活跃程度"。
def grade_from_stats(median_min, best_min, lamp_share):
    """median_min/best_min: 有效日照分钟；lamp_share: 白天灯光占记录总光的比例0~1"""
    if median_min >= 240 and best_min >= 300 and lamp_share < 0.15:
        return "A"
    if median_min >= 150 and best_min >= 200:
        return "B"
    if median_min >= 60 or best_min >= 150:
        return "C"
    return "D"


def data_quality(days):
    """days: [{"date","sun_min","peak_mv","lamp_min","saturated"}]
    返回可信度诊断字符串列表。这些是本地规则算的，不依赖模型，
    因为"数据能不能信"必须是确定结论，不能是模型的概率判断。
    """
    issues = []
    if len(days) < 2:
        issues.append("有效天数不足 2 天，分级仅供参考")
    if any(d.get("saturated") for d in days):
        issues.append("部分时段读数贴量程上限，疑分压比不当或超量程，峰值可能失真")
    peaks = [d.get("peak_mv", 0) for d in days if d.get("peak_mv") is not None]
    if len(peaks) >= 2 and max(peaks) > 0:
        if (max(peaks) - min(peaks)) / max(peaks) < 0.05:
            issues.append("各天峰值几乎一致，疑长期恒定光源直射（传感器可能对着灯或装反），"
                          "自然光成分存疑")
    lamp_total = sum(d.get("lamp_min", 0) for d in days)
    sun_total = sum(d.get("sun_min", 0) for d in days)
    if sun_total > 0 and lamp_total / (sun_total + lamp_total) > 0.6:
        issues.append("白天灯光时长超过总光时 60%，该房间高度依赖人工照明，"
                      "已剔除灯光后的有效日照更可信")
    if not issues:
        issues.append("未见明显数据异常")
    return issues


def lamp_share(days):
    sun = sum(d.get("sun_min", 0) for d in days)
    lamp = sum(d.get("lamp_min", 0) for d in days)
    return (lamp / (sun + lamp)) if (sun + lamp) else 0.0


def _median(vals):
    v = sorted(x for x in vals if x is not None)
    return v[len(v) // 2] if v else 0


def summarize(days):
    suns = [d.get("sun_min") for d in days]
    return {
        "days_count": len(days),
        "sun_median_min": _median(suns),
        "sun_best_min": max([s for s in suns if s is not None] or [0]),
        "sun_worst_min": min([s for s in suns if s is not None] or [0]),
        "lamp_share": round(lamp_share(days), 2),
        "peak_mv_max": max([d.get("peak_mv", 0) for d in days] or [0]),
    }


def build_prompt(stats, room, quality):
    """把光况统计 + 录入结构数据 + 可信度拼成给模型的事实块。"""
    struct_line = "朝向 %s / %s 层 / 窗地比 %s / 遮挡：%s" % (
        room.get("orientation", "未填"), room.get("floor", "未填"),
        room.get("window_floor_ratio", "未填"), room.get("obstruction", "未填"))
    day_lines = "\n".join(
        "  %s：有效日照 %s 分钟，峰值 %s mV，白天灯光 %s 分钟" % (
            d.get("date", "?"), d.get("sun_min", "?"),
            d.get("peak_mv", "?"), d.get("lamp_min", "?"))
        for d in stats.get("raw_days", []))
    return [
        {"role": "system",
         "content": "你是房源平台的光照评估助手。输入是一个房间连续几天实测的"
                    "光况统计（已由设备剔除灯光、算好分级），以及房源录入的结构信息。"
                    "你的任务是把它们写成买家和经纪人看得懂的评估。"
                    "硬规则：不得输出照度绝对值（lux）或任何国家标准合规结论；"
                    "不得把'光况活跃度'说成'采光照度'；单点测量不得外推为整屋采光；"
                    "结构信息来自录入不是实测，措辞上区分'测到的'和'登记的'。"
                    "不要客套，不要 Markdown，输出会被程序解析。"},
        {"role": "user",
         "content": (
            "光况活跃度分级：%s（依据：有效日照中位 %s 分钟，最好一天 %s 分钟，"
            "白天灯光占比 %s%%）\n"
            "分日记录：\n%s\n"
            "房源登记结构：%s\n"
            "数据可信度：%s\n\n"
            "请输出 JSON：\n"
            '{"headline": "一句给房源挂牌用的光照结论，不超过25字，只说测到的光况",'
            ' "laundry": "晾晒建议一句话",'
            ' "plant_light": "补光或摆位建议一句话",'
            ' "caveat": "给买家的一句实话，这个房间光照的短板",'
            ' "grade": "必须原样填 %s"}'
            % (stats["grade"], stats["sun_median_min"], stats["sun_best_min"],
               int(stats["lamp_share"] * 100), day_lines, struct_line,
               "；".join(quality), stats["grade"]))},
    ]


# 兜底评估：模型不可用时用本地统计直接生成。它不是"假扮 AI"——
# source 标 heuristic，看板写"本地统计生成，非模型评估"，分级本就由确定规则算得。
def heuristic_report(stats, room, quality):
    g = stats["grade"]
    return {
        "headline": {"A": "白天基本不用开灯的亮堂房间",
                     "B": "光照不错，个别时段偏弱",
                     "C": "白天多数时间需要补光",
                     "D": "长期依赖灯光的暗房间"}[g],
        "laundry": "有效日照中位 %s 分钟，%s" % (
            stats["sun_median_min"],
            "晾晒条件较好" if stats["sun_median_min"] >= 180
            else "晾晒建议借助烘干机或阳台"),
        "plant_light": "喜阳植物放靠窗侧，%s" % (
            "阴天或北向白天可开补光灯约 1 小时" if g in ("C", "D") else "自然光基本够用"),
        "caveat": "测的是单点光况，未覆盖整屋；结构信息为平台登记值",
        "grade": g,
        "source": "heuristic",
        "quality": quality,
    }


async def review(client, base, key, model, days, room, timeout=20):
    """days: 光况日账列表；room: 房源结构录入。返回评估 dict。"""
    stats = summarize(days)
    stats["raw_days"] = days
    stats["grade"] = grade_from_stats(
        stats["sun_median_min"], stats["sun_best_min"], stats["lamp_share"])
    quality = data_quality(days)

    if not (key and model):
        r = heuristic_report(stats, room, quality)
        r["model"] = None
        r["stats"] = stats
        return r

    try:
        resp = await client.post(
            base + "/chat/completions",
            headers={"Authorization": "Bearer " + key,
                     "Content-Type": "application/json"},
            json={"model": model, "messages": build_prompt(stats, room, quality),
                  "max_tokens": 500, "temperature": 0.4},
            timeout=timeout)
        if resp.status_code == 429:
            raise RuntimeError("NIM 限流 429")
        resp.raise_for_status()
        raw = resp.json()["choices"][0]["message"]["content"].strip()
        p = json.loads(_strip_fence(raw))
        # 模型可能擅自改分级——以规则算出的为准，绝不采信模型的 grade
        p["grade"] = stats["grade"]
        # 扫一遍输出，防止模型把 lux 或国标字样写进文案
        leak = _scan_forbidden(p)
        p["source"] = "nim" if not leak else "heuristic"
        if leak:
            # 出现禁用措辞，丢弃模型结果退回本地，保证合规结论不流入房源页
            r = heuristic_report(stats, room, quality)
            r["note"] = "模型输出含禁用措辞（%s），已退回本地统计" % "、".join(leak)
            r["stats"] = stats
            r["model"] = model
            return r
        p["quality"] = quality
        p["stats"] = stats
        p["model"] = model
        return p
    except Exception as e:
        r = heuristic_report(stats, room, quality)
        r["note"] = "模型调用失败退回本地统计：%s: %s" % (type(e).__name__, e)
        r["stats"] = stats
        r["model"] = model
        return r


_FORBIDDEN = ["lux", "勒克斯", "GB 50034", "GB50034", "国家标准", "采光系数"]


def _scan_forbidden(obj):
    hits = []
    text = json.dumps(obj, ensure_ascii=False)
    for w in _FORBIDDEN:
        if w.lower() in text.lower():
            hits.append(w)
    return hits


def _strip_fence(s):
    s = re.sub(r"^```[a-z]*\s*", "", s.strip())
    s = re.sub(r"```\s*$", "", s)
    m = re.search(r"\{.*\}", s, re.S)
    return m.group(0) if m else s
