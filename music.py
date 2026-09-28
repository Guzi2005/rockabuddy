"""系统回环采音 + 节拍检测(纯 stdlib, 无第三方依赖)。

- WASAPI loopback 直接拿系统输出, 不需要麦克风权限;
- 跟随"最响"的渲染设备: 换耳机/换输出后 2s 内自动切换, 不挂在静音设备上;
- 能量包络 onset 检测: 自适应阈值(局部均值×1.35) + 220ms 不应期;
- BPM 用 onset 间隔中位数连续估计, 半速纠正, 不钉死档位;
- 输出两类事件: onset(强度, 拍长秒) / quiet(静默超过 2s)。
"""
import ctypes
import math
import statistics
import time
import uuid
from collections import deque
from ctypes import wintypes

from PySide6.QtCore import QThread, Signal

COINIT_MULTITHREADED = 0
CLSCTX_ALL = 23
AUDCLNT_SHAREMODE_SHARED = 0
AUDCLNT_STREAMFLAGS_LOOPBACK = 0x00020000
DEVICE_STATE_ACTIVE = 1


class GUID(ctypes.Structure):
    _fields_ = [("Data1", wintypes.DWORD), ("Data2", wintypes.WORD),
                ("Data3", wintypes.WORD), ("Data4", ctypes.c_ubyte * 8)]

    @classmethod
    def from_str(cls, text):
        guid = cls()
        ctypes.memmove(ctypes.byref(guid), uuid.UUID(text).bytes_le, 16)
        return guid


class WAVEFORMATEX(ctypes.Structure):
    _fields_ = [("wFormatTag", wintypes.WORD), ("nChannels", wintypes.WORD),
                ("nSamplesPerSec", wintypes.DWORD), ("nAvgBytesPerSec", wintypes.DWORD),
                ("nBlockAlign", wintypes.WORD), ("wBitsPerSample", wintypes.WORD),
                ("cbSize", wintypes.WORD)]


_FN = ctypes.WINFUNCTYPE
_HRESULT = ctypes.c_long
_LP = ctypes.c_void_p

# IUnknown
_QI = _FN(_HRESULT, _LP, _LP, ctypes.POINTER(_LP))      # (this, iid*, ppv*)
_ADDREF = _FN(ctypes.c_ulong, _LP)
_RELEASE = _FN(ctypes.c_ulong, _LP)
# IMMDeviceEnumerator
_GET_DEFAULT = _FN(_HRESULT, _LP, ctypes.c_int, ctypes.c_int, ctypes.POINTER(_LP))
_ENUM_ENDPOINTS = _FN(_HRESULT, _LP, ctypes.c_int, wintypes.DWORD, ctypes.POINTER(_LP))
# IMMDeviceCollection
_GET_COUNT = _FN(_HRESULT, _LP, ctypes.POINTER(ctypes.c_uint))
_ITEM = _FN(_HRESULT, _LP, ctypes.c_uint, ctypes.POINTER(_LP))
# IMMDevice
_ACTIVATE = _FN(_HRESULT, _LP, _LP, wintypes.DWORD, _LP, ctypes.POINTER(_LP))
_GET_ID = _FN(_HRESULT, _LP, ctypes.POINTER(_LP))
# IAudioClient
_INIT = _FN(_HRESULT, _LP, ctypes.c_int, wintypes.DWORD, ctypes.c_longlong,
            ctypes.c_longlong, _LP, _LP)
_GETMIX = _FN(_HRESULT, _LP, ctypes.POINTER(_LP))
_START = _FN(_HRESULT, _LP)
_STOP = _FN(_HRESULT, _LP)
_GETSERVICE = _FN(_HRESULT, _LP, _LP, ctypes.POINTER(_LP))
# IAudioCaptureClient
_GETBUFFER = _FN(_HRESULT, _LP, ctypes.POINTER(_LP), ctypes.POINTER(wintypes.DWORD),
                 ctypes.POINTER(wintypes.DWORD), _LP, _LP)
_RELEASEBUFFER = _FN(_HRESULT, _LP, wintypes.DWORD)
_NEXTPACKET = _FN(_HRESULT, _LP, ctypes.POINTER(wintypes.DWORD))
# IAudioMeterInformation
_GET_PEAK = _FN(_HRESULT, _LP, ctypes.POINTER(ctypes.c_float))

_IID_METER = GUID.from_str("C02216F6-8C67-4B5B-9D00-D008E73E0064")

_COTASKFREE = ctypes.windll.ole32.CoTaskMemFree
_COTASKFREE.argtypes = [_LP]
_COTASKFREE.restype = None


def _methods(obj, protos):
    """按 vtable 顺序取出 COM 方法。"""
    vt_ptr = ctypes.cast(obj, ctypes.POINTER(_LP)).contents.value
    table = ctypes.cast(vt_ptr, ctypes.POINTER(_LP * len(protos))).contents
    return [proto(addr) for proto, addr in zip(protos, table)]


def _vt_slot(obj, slot, proto):
    vt_ptr = ctypes.cast(obj, ctypes.POINTER(_LP)).contents.value
    addr = ctypes.cast(vt_ptr, ctypes.POINTER(_LP * (slot + 1))).contents[slot]
    return proto(addr)


def _release(obj):
    if obj:
        try:
            _vt_slot(obj, 2, _RELEASE)(obj)
        except Exception:
            pass


def _device_id(device):
    """IMMDevice.GetId (vtable slot 5: Activate/OpenPropertyStore 之后)。"""
    p = _LP()
    if _vt_slot(device, 5, _GET_ID)(device, ctypes.byref(p)) < 0 or not p:
        return None
    text = ctypes.wstring_at(p.value)
    _COTASKFREE(p.value)
    return text


def _render_peaks(enumerator):
    """所有 active 渲染端点的当前峰值 {device_id: peak 0-1}。"""
    peaks = {}
    coll = _LP()
    hr = _vt_slot(enumerator, 3, _ENUM_ENDPOINTS)(
        enumerator, 0, DEVICE_STATE_ACTIVE, ctypes.byref(coll))   # eRender
    if hr < 0 or not coll:
        return peaks
    try:
        count = ctypes.c_uint(0)
        if _vt_slot(coll, 3, _GET_COUNT)(coll, ctypes.byref(count)) < 0:
            return peaks
        for i in range(count.value):
            dev = _LP()
            if _vt_slot(coll, 4, _ITEM)(coll, i, ctypes.byref(dev)) < 0 or not dev:
                continue
            try:
                meter = _LP()
                if _vt_slot(dev, 3, _ACTIVATE)(dev, ctypes.byref(_IID_METER),
                                               CLSCTX_ALL, None,
                                               ctypes.byref(meter)) == 0 and meter:
                    try:
                        peak = ctypes.c_float(0.0)
                        if _vt_slot(meter, 3, _GET_PEAK)(meter, ctypes.byref(peak)) == 0:
                            did = _device_id(dev)
                            if did:
                                peaks[did] = peak.value
                    finally:
                        _release(meter)
            finally:
                _release(dev)
    finally:
        _release(coll)
    return peaks


class LoopbackCapture:
    """WASAPI 回环采集; feed(mono_floats, now) 回调送 DSP。"""

    def __init__(self, feed):
        self.feed = feed
        self.running = True
        self.log = []
        self.fast_restart = False   # 因设备切换退出: 外层应立刻重开而不是等 5s
        self.ole32 = ctypes.windll.ole32

    def run(self):
        self.ole32.CoInitializeEx(None, COINIT_MULTITHREADED)
        enumerator = device = client = capture = None
        try:
            clsid = GUID.from_str("BCDE0395-E52F-467C-8E3D-C4579291692E")   # MMDeviceEnumerator
            iid_enum = GUID.from_str("A95664D2-9614-4F35-A746-DE8DB63617E6")
            iid_client = GUID.from_str("1CB9AD4C-DBFA-4c32-B178-C2F568A703B2")
            iid_capture = GUID.from_str("C8ADBD64-E71E-48a0-A4DE-185C395CD317")

            enumerator = _LP()
            hr = self.ole32.CoCreateInstance(ctypes.byref(clsid), None, CLSCTX_ALL,
                                             ctypes.byref(iid_enum), ctypes.byref(enumerator))
            if hr < 0:
                self.log.append("cocreate=%x" % (hr & 0xFFFFFFFF)); return
            device = _LP()
            if _vt_slot(enumerator, 4, _GET_DEFAULT)(
                    enumerator, 0, 0, ctypes.byref(device)) < 0:  # eRender, eConsole
                self.log.append("get_default"); return
            client = _LP()
            if _vt_slot(device, 3, _ACTIVATE)(device, ctypes.byref(iid_client),
                                              CLSCTX_ALL, None,
                                              ctypes.byref(client)) < 0:
                self.log.append("activate"); return
            vt = ctypes.cast(ctypes.cast(client, ctypes.POINTER(_LP)).contents.value,
                             ctypes.POINTER(_LP * 16)).contents
            init = _INIT(vt[3])
            get_mix = _GETMIX(vt[8])
            start = _START(vt[10])   # IAudioClient vtable: 9=GetDevicePeriod, 10=Start, 11=Stop
            stop = _STOP(vt[11])
            get_service = _GETSERVICE(vt[14])   # 必须在 Initialize 之后调用, 提前探测必返回 NOT_INITIALIZED

            fmt_p = _LP()
            if get_mix(client, ctypes.byref(fmt_p)) < 0:
                self.log.append("get_mix"); return
            fmt = ctypes.cast(fmt_p.value, ctypes.POINTER(WAVEFORMATEX)).contents
            channels = fmt.nChannels or 2
            align = fmt.nBlockAlign or channels * 4
            bits = fmt.wBitsPerSample
            if bits == 32 and fmt.wFormatTag in (3, 0xFFFE):
                mode = "float"          # IEEE float32(耳机/声卡最常见)
            elif bits == 16:
                mode = "int16"
            elif bits == 24:
                mode = "int24"          # USB DAC 常见
            elif bits == 32:
                mode = "int32"
            else:
                self.log.append("fmt tag=%x bits=%d" % (fmt.wFormatTag, bits)); return

            if init(client, AUDCLNT_SHAREMODE_SHARED, AUDCLNT_STREAMFLAGS_LOOPBACK,
                    200000, 0, fmt_p, None) < 0:
                self.log.append("init"); return
            capture = _LP()
            if get_service(client, ctypes.byref(iid_capture), ctypes.byref(capture)) < 0:
                self.log.append("get_service2"); return
            get_buffer = _vt_slot(capture, 3, _GETBUFFER)
            release_buffer = _vt_slot(capture, 4, _RELEASEBUFFER)
            next_packet = _vt_slot(capture, 5, _NEXTPACKET)
            hr = start(client)
            if hr < 0:
                self.log.append("start=%x" % (hr & 0xFFFFFFFF)); return
            self.log.append("started ch=%d align=%d mode=%s" % (channels, align, mode))

            opened_id = _device_id(device)
            next_poll = time.monotonic() + 2.0
            data = _LP()
            frames = wintypes.DWORD(0)
            flags = wintypes.DWORD(0)
            packet = wintypes.DWORD(0)
            while self.running:
                time.sleep(0.008)
                now_mono = time.monotonic()
                if now_mono >= next_poll:
                    # 跟随最响的渲染设备: 插耳机/换输出后自动换采集源
                    next_poll = now_mono + 2.0
                    peaks = _render_peaks(enumerator)
                    if peaks:
                        current = peaks.get(opened_id, 0.0)
                        loud_id, loud = max(peaks.items(), key=lambda kv: kv[1])
                        if (loud_id != opened_id and loud > 0.0015
                                and loud > current * 1.5 + 0.0005):
                            self.fast_restart = True
                            break
                if next_packet(capture, ctypes.byref(packet)) < 0:
                    break
                while packet.value and self.running:
                    if get_buffer(capture, ctypes.byref(data), ctypes.byref(frames),
                                  ctypes.byref(flags), None, None) < 0:
                        break
                    if frames.value:
                        raw = ctypes.string_at(data.value, frames.value * align)
                        release_buffer(capture, frames.value)
                        self._emit_mono(raw, frames.value, channels, mode)
                    else:
                        release_buffer(capture, 0)
                    if next_packet(capture, ctypes.byref(packet)) < 0:
                        break
            stop(client)
        finally:
            for obj in (capture, client, device, enumerator):
                _release(obj)
            self.ole32.CoUninitialize()

    def _emit_mono(self, raw, frames, channels, mode):
        now = time.monotonic()
        count = frames * channels
        if mode == "float":
            vals = list(memoryview(raw).cast("f")[:count])
        elif mode == "int16":
            vals = [v / 32768.0 for v in memoryview(raw).cast("h")[:count]]
        elif mode == "int32":
            vals = [v / 2147483648.0 for v in memoryview(raw).cast("i")[:count]]
        elif mode == "int24":
            mv = memoryview(raw)
            vals = [int.from_bytes(mv[i * 3:i * 3 + 3], "little", signed=True)
                    / 8388608.0 for i in range(count)]
        else:
            return
        if channels == 1:
            self.feed(vals, now)
            return
        mono = [0.0] * frames
        for ch in range(channels):
            for i in range(frames):
                mono[i] += vals[i * channels + ch]
        inv = 1.0 / channels
        self.feed([m * inv for m in mono], now)


class BeatDetector:
    """能量包络 onset 检测 + 连续 BPM 估计(可测, 与采集解耦)。"""

    def __init__(self):
        self.energy = deque(maxlen=100)   # 最近 ~1s 的 10ms hop 能量
        self.onsets = deque(maxlen=16)
        self.last_onset = 0.0
        self.active = False
        self.floor = 3e-3                 # 静音底噪(float32 满幅 1.0)

    def feed(self, samples, now):
        if not samples:
            return []
        level = max(abs(s) for s in samples)
        if level > 1.5:                   # int16 原始值, 归一化
            samples = [s / 32768.0 for s in samples]
        e = math.sqrt(sum(s * s for s in samples) / len(samples))
        events = []
        if len(self.energy) >= 20:
            avg = statistics.fmean(self.energy)
            threshold = max(self.floor, avg * 1.35)
            ioi = now - self.last_onset
            if e > threshold and e > self.floor and ioi > 0.22:
                strength = min(1.0, (e / threshold - 1) / 1.2)
                self.onsets.append(now)
                self.last_onset = now
                self.active = True
                events.append(("onset", round(strength, 3), self.period()))
        self.energy.append(e)
        if self.active and now - self.last_onset > 2.0:
            self.active = False
            events.append(("quiet",))
        return events

    def period(self):
        """连续 BPM 拍长(秒); 样本不足返回 0。"""
        if len(self.onsets) < 4:
            return 0.0
        ticks = list(self.onsets)
        iois = [b - a for a, b in zip(ticks, ticks[1:]) if 0.24 <= b - a <= 2.0]
        if not iois:
            return 0.0
        p = statistics.median(iois)
        while p < 0.30:      # 半速纠正: 检出密拍通常是双倍速
            p *= 2
        return p


class BeatListener(QThread):
    onset = Signal(float, float)   # 强度 0-1, 拍长秒(未知为 0)
    quiet = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.detector = BeatDetector()
        self._capture = None

    def run(self):
        while not self.isInterruptionRequested():
            capture = LoopbackCapture(self._feed)
            self._capture = capture
            capture.run()          # 阻塞直到 running=False / 采集失败 / 设备切换
            self._capture = None
            if not self.isInterruptionRequested():
                self.quiet.emit()  # 采集中断: 先安静
                # 设备切换立刻重开; 异常失败等 5s(设备可能还没就绪)
                self.sleep(1 if capture.fast_restart else 5)

    def _feed(self, samples, now):
        for event in self.detector.feed(samples, now):
            if event[0] == "onset":
                self.onset.emit(event[1], event[2])
            else:
                self.quiet.emit()

    def stop(self):
        self.requestInterruption()
        if self._capture is not None:
            self._capture.running = False
        self.wait(1500)
