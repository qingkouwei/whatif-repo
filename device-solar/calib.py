# -*- coding: utf-8 -*-
"""
光照标定脚本 v2 —— 单独上传、单独执行，不依赖其它模块。

用法（QPYcom 文件页右键执行，或 REPL：exec(open('/usr/calib.py').read())）：
    跑 90 秒，期间按提示做三个动作。它回答三个必须实测的问题：

    Q1 极性到底朝哪边
    Q2 分压后读数的真实顶在哪（SATURATION_MV 就填这个）
    Q3 ADC 报的是 mV 还是码值（复核，防换固件后行为漂移）

    上一版数据"感觉有问题"的病根基本都在这三问里：
    极性信了官方注释、饱和线用了理论 1200、单位被 1702 这种
    超量程数字绕进过循环论证。这次全部交给实测。

烧录前把下面三个数按你的实际分压电阻改好。
"""

import time as _t          # CPython 兼容写法占位；板上用 utime
try:
    import utime
    sleep = utime.sleep
    ticks_ms = utime.ticks_ms
    ticks_diff = utime.ticks_diff
except ImportError:       # 本地空跑不至于炸
    sleep = _t.sleep
    ticks_ms = lambda: int(_t.time() * 1000)
    ticks_diff = lambda a, b: a - b

from misc import ADC

R1_KOHM = 100.0            # ← 按实际接线改
R2_KOHM = 100.0            # ← 按实际接线改
CHANNEL = ADC.ADC1         # A0→ADC0，A1→ADC1，接哪路改哪路
N = 90                     # 采样秒数

adc = ADC()
try:
    adc.open()             # 个别固件需要显式 open，容错跳过
except Exception:
    pass

DIV = R2_KOHM / (R1_KOHM + R2_KOHM)


def rd():
    v = adc.read(CHANNEL)
    return v if isinstance(v, int) else 0


def phase(seconds, label):
    print("")
    print("=== %s（%d 秒）===" % (label, seconds))
    vals = []
    for i in range(seconds):
        v = rd()
        vals.append(v)
        if i % 10 == 0:
            print("  t=%2ds 读数=%s" % (i, v))
        sleep(1)
    vals.sort()
    lo, med, hi = vals[0], vals[len(vals) // 2], vals[-1]
    print("  最低=%s 中位=%s 最高=%s 波动=%s" % (lo, med, hi, hi - lo))
    return lo, med, hi


def main():
    print("calib v2  channel=%s  分压比=%.3f (R1=%sk R2=%sk)" % (CHANNEL, DIV, R1_KOHM, R2_KOHM))
    print("准备：光敏朝天花板放好，手边备一个能完全遮住的东西和手机手电")

    lo1, med1, hi1 = phase(N // 3, "第1步 静置：传感器正常对着房间光")
    lo2, med2, hi2 = phase(N // 3, "第2步 捂死：完全遮住（贴黑胶带/握拳捂严）")
    lo3, med3, hi3 = phase(N // 3, "第3步 强光：手电贴近照 + 对着窗/灯")

    print("")
    print("================ 结论 ================")

    # Q1 极性
    if med3 > med1 > med2:
        polarity = "正常：光越强读数越高（LIGHT_INVERT=False）"
        pv = False
    elif med3 < med1 < med2:
        polarity = "反相：光越强读数越低（LIGHT_INVERT=True）"
        pv = True
    else:
        polarity = "不明确：三段中位没有单调关系，检查遮光是否彻底/手电是否够近"
        pv = None
    print("极性：%s" % polarity)

    # Q2 饱和顶
    print("三段最高读数：静置=%s 捂死=%s 强光=%s" % (hi1, hi2, hi3))
    print("如果强光 hi 和静置 hi 差不多且都明显高于 1200，")
    print("说明仍然超量程（分压比不够），把强光时的读数记下来：")
    print("   → config.SATURATION_MV = %s   （强光段顶值减 2%% 余量）"
          % (int(hi3 * 0.98) if pv is False else int((1200 if pv is None else hi3))))
    if hi3 > 1200 and pv is False:
        print("⚠ 强光读数 %s > 1200：分压后仍在理论量程外，先换电阻把顶压进量程再标定！" % hi3)
        print("   当前分压比 %.3f，强光前电压约 %.2fV，建议 R1 加到让顶值落 900~1100" % (DIV, hi3 / DIV / 1000.0))

    # Q3 单位复核
    src_mv = int(hi3 / DIV)
    print("若按 mV 理解：强光时分压前电压 ≈ %s mV（合理室内强光应在 1000~3300 之间）" % src_mv)
    print("若按 0-4095 码值理解：强光 ≈ %.0f/4095 → %.2fV" % (hi3, hi3 / 4095.0 * 1.2))
    print("两种解释哪种更符合你的万用表实测？万用表量模块 AO 对地电压是最强证据。")

    print("")
    print("→ 把结论抄进 config.py：LIGHT_INVERT=%s, SATURATION_MV=<强光顶值>" % pv)
    print("→ 换过电阻/加过遮挡后，本脚本必须重跑一遍。")
    try:
        adc.close()
    except Exception:
        pass


main()
