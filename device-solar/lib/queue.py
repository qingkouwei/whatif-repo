# -*- coding: utf-8 -*-
"""
断网离线队列：JSON Lines 追加写。

设计要点（都来自 NOR flash 的脾气）：

1. 追加写，不整读整写。队列若用 JSON 数组存，每加一条要
   读全文-改-写全文，写放大随文件增长，NOR flash 寿命和耗时都亏。
   NDJSON 一行一条，追加就是 open(path, "a")。

2. 落盘节流。上报失败先进内存队列，每 QUEUE_FLUSH_S 秒批量写一次盘。

3. 容量硬上限。文件超过 QUEUE_MAX_KB 丢最旧的——断网几天的数据
   全存下来也没意义，宁可丢历史也要保最近。

4. 掉电安全：文件末尾可能有一个写了一半的行，恢复时逐行解析，
   解不了的行直接跳过，不要让半行坏数据把整个队列堵死。
"""

import ujson
import uos
import utime


class OfflineQueue(object):

    def __init__(self, cfg):
        self._cfg = cfg
        self._path = cfg.QUEUE_PATH
        self._mem = []              # 待落盘的行（字符串）
        self._last_flush = 0
        self._file_bytes = 0
        self._scan_file()

    # ------------------------------------------------------------------

    def _scan_file(self):
        """启动时统计现有队列大小，逐行校验并丢弃坏尾行。"""
        try:
            st = uos.stat(self._path)
            self._file_bytes = st[6]
        except OSError:
            self._file_bytes = 0

    def _trim(self, lines):
        limit = self._cfg.QUEUE_MAX_KB * 1024
        total = sum(len(l) + 1 for l in lines)
        if total <= limit:
            return lines
        out = lines
        while total > limit and len(out) > 1:
            total -= len(out[0]) + 1
            out = out[1:]           # 丢最旧
        return out

    # ------------------------------------------------------------------

    def put(self, payload_dict):
        """上报失败时入队。内存里先攒着，到点批量落盘。"""
        try:
            line = ujson.dumps(payload_dict, separators=(",", ":"))
        except Exception:
            return                  # 序列化不了的样本不值得重试
        self._mem.append(line)
        # 内存也设上限，防止长时间断网吃光 RAM
        if len(self._mem) > 200:
            self._mem = self._mem[-200:]

    def tick(self, now_ms):
        """主循环每帧调用：到落盘节拍就把内存队列追加进文件。"""
        if not self._mem:
            return
        if utime.ticks_diff(now_ms, self._last_flush) < self._cfg.QUEUE_FLUSH_S * 1000:
            return
        self.flush()

    def flush(self):
        if not self._mem:
            return
        try:
            f = open(self._path, "a")
            for line in self._mem:
                f.write(line + "\n")
            f.close()
            self._file_bytes += sum(len(l) + 1 for l in self._mem)
            self._mem = []
        except Exception as e:
            print("队列落盘失败：%s" % e)
            # 文件写不进去（比如满了），保留内存队列，下轮再试
        self._last_flush = utime.ticks_ms()   # 成败都推进节拍，
        self._maybe_trim()                     # 防失败时每帧重试狂写 flash

    def _maybe_trim(self):
        limit = self._cfg.QUEUE_MAX_KB * 1024
        if self._file_bytes <= limit:
            return
        lines = self.read_all_raw()
        kept = self._trim(lines)
        tmp = self._path + ".tmp"
        f = open(tmp, "w")
        for l in kept:
            f.write(l + "\n")
        f.close()
        try:
            uos.remove(self._path)
        except OSError:
            pass
        uos.rename(tmp, self._path)
        self._file_bytes = sum(len(l) + 1 for l in kept)
        print("队列超容量，已丢最旧，剩 %d 条" % len(kept))

    # ------------------------------------------------------------------

    def read_all_raw(self):
        """读全部合法行。坏行跳过。"""
        out = []
        try:
            f = open(self._path, "r")
            data = f.read()
            f.close()
        except OSError:
            return out
        for line in data.split("\n"):
            line = line.strip()
            if not line:
                continue
            try:
                ujson.loads(line)     # 只保留能解析的行
                out.append(line)
            except ValueError:
                pass                  # 掉电半行，跳过
        return out

    def drain(self, max_items=20):
        """补报时取最旧的一批。返回 list[dict]，可能为空。"""
        if not self._mem:
            self.flush()
        lines = self.read_all_raw()
        take = lines[:max_items]
        rest = lines[max_items:]
        if take:
            self._rewrite(rest)
        result = []
        for l in take:
            try:
                result.append(ujson.loads(l))
            except ValueError:
                pass
        return result

    def _rewrite(self, lines):
        tmp = self._path + ".tmp"
        f = open(tmp, "w")
        for l in lines:
            f.write(l + "\n")
        f.close()
        try:
            uos.remove(self._path)
        except OSError:
            pass
        uos.rename(tmp, self._path)
        self._file_bytes = sum(len(l) + 1 for l in lines)

    def depth(self):
        return len(self._mem) + len(self.read_all_raw())
