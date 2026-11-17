# -*- coding: utf-8 -*-
"""
AHT20 温湿度（I2C0, 0x38）。

只保留本项目需要的读取 + 状态位处理，官方 AHT20.py 里的舒适度分级
在这里用不上，没抄过来。

关键点是 bit[7]：测量进行中该位为 1，此时读出的 6 字节全是 0，
不检查就会拿到 0.0℃ 的假值。官方示例把这条注释写在中间，容易被跳过。

温度在这里的用途不是"显示环境温度"，而是给超声除数做修正，
所以它属于测距链路的一部分，读不到温度时超声仍按上一次除数工作。
"""

import utime


class AHT20(object):

    ADDR = 0x38
    _CMD_RESET = b'\xBA'
    _CMD_INIT = b'\xE1'
    _CMD_MEASURE = b'\xAC\x33\x00'

    def __init__(self, i2c):
        self._i2c = i2c
        self._last = (None, None)
        self.init()

    def init(self):
        try:
            self._i2c.writeto(self.ADDR, self._CMD_RESET)
            utime.sleep_ms(40)
            self._i2c.writeto(self.ADDR, self._CMD_INIT)
            utime.sleep_ms(10)
        except Exception as e:
            print("AHT20 初始化失败：%s" % e)

    def read(self):
        """返回 (湿度%, 温度℃)。失败返回上一次的值。"""
        try:
            self._i2c.writeto(self.ADDR, self._CMD_MEASURE)
            utime.sleep_ms(80)
            raw = self._i2c.readfrom(self.ADDR, 6)
            # bit[7]=1 表示测量中，数据无效
            if raw[0] & 0x80:
                utime.sleep_ms(40)
                raw = self._i2c.readfrom(self.ADDR, 6)
                if raw[0] & 0x80:
                    return self._last
            rh_raw = ((raw[1] << 12) | (raw[2] << 4) | (raw[3] >> 4))
            t_raw = (((raw[3] & 0x0F) << 16) | (raw[4] << 8) | raw[5])
            rh = round(rh_raw * 100.0 / 1048576, 1)
            temp = round(t_raw * 200.0 / 1048576 - 50.0, 1)
            self._last = (rh, temp)
            return rh, temp
        except Exception as e:
            print("AHT20 读取异常：%s" % e)
            return self._last
