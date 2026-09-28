"""BeatDetector 合成信号测试: 节拍网格相位锁、切分音免疫、低频重拍加权、语音活动。"""
import math
import unittest
from music import BeatDetector

SR = 48000
HOP = 480   # 10ms 每包, 与 LoopbackCapture 的喂入节奏一致


def run_feed(det, t, until, events=None):
    """喂静音直到 until。"""
    while t < until - 1e-9:
        for e in det.feed([0.0] * HOP, t):
            if events is not None:
                events.append((round(t, 3),) + e)
        t += 0.01
    return t


def burst(det, t, freq, amp=0.01, dur=0.06, events=None):
    """在时刻 t 送一段正弦爆发(10ms 一包), 返回结束时刻。"""
    for i in range(int(dur / 0.01)):
        start = t + i * 0.01
        samples = [amp * math.sin(2 * math.pi * freq * (start + j / SR))
                   for j in range(HOP)]
        for e in det.feed(samples, start):
            if events is not None:
                events.append((round(start, 3),) + e)
    return t + dur


def onsets(events):
    return [(ts, s) for (ts, kind, s, *rest) in events if kind == "onset"]


class BeatGridTests(unittest.TestCase):
    JITTER = [0.0, .010, -.008, .004, .012, -.006, .002, .009, -.011, .005]

    def test_pll_locks_phase_and_period(self):
        det = BeatDetector()
        t, beats = 0.0, []
        for i in range(14):
            target = 0.6 + i * 0.5 + self.JITTER[i % len(self.JITTER)]
            t = run_feed(det, t, target)
            t = burst(det, t, 110.0)
            beats.append(target)
        self.assertGreater(len(det.onsets), 6)
        self.assertAlmostEqual(det.period, 0.5, delta=0.03)
        # 相位锁: 最后一拍与网格预测点偏差 < 30ms
        k = round((beats[-1] - det.anchor) / det.period)
        predicted = det.anchor + k * det.period
        self.assertLess(abs(beats[-1] - predicted), 0.03)

    def test_grid_survives_two_syncopations(self):
        # 2 个落在两拍正中间的切分音, 距任何预测拍点都有 0.25s: 网格纹丝不动
        det = BeatDetector()
        t = 0.0
        beats = []
        for i in range(8):
            target = 0.6 + i * 0.5 + self.JITTER[i % len(self.JITTER)]
            t = run_feed(det, t, target)
            t = burst(det, t, 110.0)
            beats.append(target)
        anchor_before, period_before = det.anchor, det.period
        for offset in (0.25, 0.75):           # 明确偏离网格 ±15% 窗口
            t = run_feed(det, t, beats[-1] + offset)
            t = burst(det, t, 110.0)
        self.assertEqual(det.anchor, anchor_before)
        self.assertAlmostEqual(det.period, period_before)
        # 之后的正拍依然命中(网格没被带歪)
        t = run_feed(det, t, beats[-1] + 1.0)
        t = burst(det, t, 110.0)
        self.assertEqual(det._miss, 0)

    def test_grid_resets_and_relocks_after_offbeat_run(self):
        # 5 个连续半拍音强制脱网重置; 之后 6 个正拍应重新锁回 0.5s
        det = BeatDetector()
        t = 0.0
        for i in range(8):
            target = 0.6 + i * 0.5
            t = run_feed(det, t, target)
            t = burst(det, t, 110.0)
        t = run_feed(det, t, t + 0.19)
        for _ in range(5):
            t = burst(det, t, 110.0)          # 半拍错位连击
            t = run_feed(det, t, t + 0.19)
        for _ in range(6):                    # 正拍回归
            t = burst(det, t, 110.0)
            t = run_feed(det, t, t + 0.44)
        self.assertAlmostEqual(det.period, 0.5, delta=0.03)

    def test_low_freq_beats_hit_harder(self):
        events = []
        det = BeatDetector()
        t = run_feed(det, 0.0, 0.6)
        t = burst(det, t, 110.0, events=events)      # 低频鼓
        t = run_feed(det, t, t + 0.8)
        t = burst(det, t, 4000.0, events=events)     # 同响度高频
        hits = onsets(events)
        self.assertEqual(len(hits), 2)
        self.assertGreater(hits[0][1], hits[1][1])   # 低频强度更高

    def test_voice_on_during_sound_and_off_after_silence(self):
        det = BeatDetector()
        events = []
        t = run_feed(det, 0.0, 0.4)
        t = burst(det, t, 220.0, amp=0.08, dur=1.0, events=events)
        t = run_feed(det, t, t + 2.5, events)
        voice = [(e[0], e[2]) for e in events if len(e) >= 4 and e[1] == "voice"]
        self.assertTrue(any(on for _, on in voice))  # 出声期间有"在唱"
        self.assertFalse(voice[-1][1])               # 收尾"闭嘴"
        self.assertFalse(det.voice_active)


if __name__ == "__main__":
    unittest.main()
