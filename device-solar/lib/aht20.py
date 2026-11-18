# -*- coding: utf-8 -*-
"""
AHT20 温湿度（I2C0, 0x38）—— 带 busy 位与容错。

要点（实测教训）：
- bit[7]=1 表示测量中，此时 6 字节全是脏数据，不检查会拿到 0.0 假值
- 模块没接/I2C 拉低时 read 会抛异常，这里返回 None 让主循环降级，
  温湿度缺失不该阻断光照主线上报
"""

import utime
from machine import I2C


class AHT20(object):

    ADDR = 0x38

    def __init__(self):
        self._i2c = I2C(I2C.I2C0, I2C.STANDARD_MODE)
        self.reset()

    def reset(self):
        try:
            # 命令序列照套件官方 AHT20.py（实测可用），不自创写法：
            # RESET 0xBA；INIT 只发单字节 0xE1；测量 0xAC 0x33 0x00
            self._i2c.writeto(self.ADDR, b'\xBA')
            utime.sleep_ms(40)
            self._i2c.writeto(self.ADDR, b'\xE1')
            utime.sleep_ms(10)
            return True
        except Exception as e:
            print("AHT20 初始化失败（将跳过温湿度）：%s" % e)
            return False

    def read(self):
        """返回 (temp_c, rh) 或 (None, None)。"""
        try:
            self._i2c.writeto(self.ADDR, b'\xAC\x33\x00')
            # busy 轮询，最多等 300ms
            for _ in range(6):
                utime.sleep_ms(50)
                st = self._i2c.readfrom(self.ADDR, 1)
                if not (st[0] & 0x80):
                    break
            else:
                return None, None
            r = self._i2c.readfrom(self.ADDR, 6)
            rh_raw = ((r[1] << 12) | (r[2] << 4) | (r[3] >> 4))
            t_raw = (((r[3] & 0x0F) << 16) | (r[4] << 8) | r[5])
            rh = round(rh_raw / 1048576.0 * 100.0, 1)
            temp = round(t_raw / 1048576.0 * 200.0 - 50.0, 1)
            return temp, rh
        except Exception:
            return None, None
