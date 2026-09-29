"""Compact single-screen companion panel: reset countdowns first, no scrolling."""
import ctypes
import math
import os
import time
from PySide6.QtCore import Qt, QRect, QRectF, QPointF, QTimer, Signal, QSettings
from PySide6.QtGui import (QColor, QLinearGradient, QPainter, QPainterPath,
                           QPainterPathStroker, QPen, QPixmap, QImage, QBitmap)
from PySide6.QtWidgets import (QWidget, QFrame, QLabel, QPushButton, QVBoxLayout,
    QHBoxLayout, QDialog, QLineEdit, QSizePolicy, QApplication, QScrollArea,
    QScrollBar)
import paths
from credentials import save_secret
from providers import _free_status

ICON_DIR = paths.resource_path("assets", "icons")

STYLE = """
QWidget {font-family:'Microsoft YaHei UI';font-size:11px;color:#30474a;}
QLabel {background:transparent;border:0;}
QLabel#muted {color:#788784;font-size:10px;}
QLabel#mutedLight {color:#8fa3a0;font-size:10px;}
QLabel#title {font-size:14px;font-weight:700;color:#f4f6f2;}
QLabel#heroCount {font-size:18px;font-weight:800;color:#f4f6f2;}
QLabel#heroName {font-size:11px;font-weight:700;color:#9adcd0;}
QPushButton {background:#e9ede2;border:0;border-radius:8px;padding:4px 9px;font-size:10px;}
QPushButton:hover {background:#dbe4cd;}
QPushButton:pressed {background:#ccd9b9;}
QPushButton:disabled {color:#98a09a;}
QPushButton#ghost {background:#232f31;color:#cfe0d6;border-radius:8px;padding:5px 10px;}
QPushButton#ghost:hover {background:#2f3f41;}
QPushButton#ghost:disabled {color:#6b7d7a;}
QLineEdit {background:white;border:1px solid #d4d8c9;border-radius:7px;padding:6px;}
QFrame#cardLive {background:#ffffff;border:1px solid #e3e7db;border-radius:12px;}
QScrollArea {background:transparent;border:0;}
QScrollArea > QWidget > QWidget {background:transparent;}
QScrollBar:vertical {background:transparent;width:5px;margin:0;}
QScrollBar::handle:vertical {background:#c2c7ba;border-radius:2px;min-height:30px;}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {height:0;}
QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {background:transparent;}
"""

BADGES = {
    "synced": ("已同步", "#e3f2ec", "#2e7d6b"),
    "manual": ("手动", "#eef0ea", "#788784"),
    "alert": ("告急", "#fbe9e2", "#bb5b3f"),
    "drained": ("已耗尽", "#e9eae6", "#98a09a"),
    "stale": ("同步失败", "#fbe9e2", "#bb5b3f"),
    "pending": ("待连接", "#f2f2ee", "#98a09a"),
}
PALETTE = ["#5fae9f", "#e0a458", "#8a94d6", "#c9826e", "#6fa8dc", "#b58fd6", "#88b06a"]


def label(text, name=None):
    item = QLabel(text)
    if name:
        item.setObjectName(name)
    return item


def icon_pixmap(pid, size):
    path = os.path.join(ICON_DIR, pid + ".png")
    if os.path.exists(path):
        return QPixmap(path).scaled(size, size, Qt.KeepAspectRatio, Qt.SmoothTransformation)
    return None


def icon_widget(pid, color, size=28, gray=False):
    """Q 版图标, 缺失时退化为色点; gray 时灰化(额度耗尽)。"""
    pix = icon_pixmap(pid, size)
    if pix is not None:
        if gray:
            img = pix.toImage().convertToFormat(QImage.Format_ARGB32)
            for yy in range(img.height()):
                for xx in range(img.width()):
                    c = img.pixelColor(xx, yy)
                    g = round(c.red() * .299 + c.green() * .587 + c.blue() * .114)
                    img.setPixelColor(xx, yy, QColor(g, g, g, c.alpha()))
            pix = QPixmap.fromImage(img)
        w = QLabel()
        w.setPixmap(pix)
        w.setFixedSize(size, size)
        return w
    dot = QFrame()
    dot.setFixedSize(12, 12)
    dot.setStyleSheet("background:%s;border-radius:6px;" % ("#b9bfc0" if gray else color))
    return dot


def countdown_parts(timestamp, now=None):
    """(简短倒计时, 是否已到期)"""
    if not timestamp:
        return None, False
    now = time.time() if now is None else now
    seconds = timestamp - now
    if seconds <= 0:
        return "马上", True
    minutes = math.ceil(seconds / 60)
    if minutes >= 1440:
        text = "%d天%d小时" % (minutes // 1440, minutes % 1440 // 60)
    elif minutes >= 60:
        text = "%d小时%d分" % (minutes // 60, minutes % 60)
    else:
        text = "%d分" % minutes
    return text, False


def reset_text(timestamp, now=None):
    if not timestamp:
        return "恢复时间暂不可用"
    now = time.time() if now is None else now
    if timestamp - now <= 0:
        return "已到恢复时间 · 等待同步确认"
    text, _ = countdown_parts(timestamp, now)
    return "%s后恢复 · %s" % (text, time.strftime("%m/%d %H:%M", time.localtime(timestamp)))


def min_window_pct(data):
    windows = data.get("windows") or []
    if not windows:
        return None
    return min(w["remaining_percent"] for w in windows)


def service_pct(data):
    if not data.get("ok"):
        return None
    pct = min_window_pct(data)
    if pct is not None:
        return pct
    if data.get("total") and data.get("remaining") is not None:
        return max(0.0, min(100.0, data["remaining"] / data["total"] * 100))
    return None


def next_reset(data):
    """该服务最近一次的额度重置/恢复时间点。"""
    future = [w["resets_at"] for w in (data.get("windows") or [])
              if w.get("resets_at") and w["resets_at"] > time.time()
              and w.get("remaining_percent", 100) < 100]
    return min(future) if future else None


def lowest_window(data):
    windows = [w for w in (data.get("windows") or [])]
    return min(windows, key=lambda w: w["remaining_percent"]) if windows else None


class ManualEditDialog(QDialog):
    def __init__(self, name, cfg, parent=None):
        super().__init__(parent)
        self.setWindowTitle("更新 " + name)
        self.setStyleSheet(STYLE)
        self.setMinimumWidth(280)
        root = QVBoxLayout(self)
        root.addWidget(label("剩余量（%s）" % cfg.get("unit", "")))
        self.ed_rem = QLineEdit(str(cfg.get("remaining") or ""))
        root.addWidget(self.ed_rem)
        root.addWidget(label("总量（选填）"))
        self.ed_total = QLineEdit(str(cfg.get("total") or ""))
        root.addWidget(self.ed_total)
        self.error_label = label("")
        self.error_label.setWordWrap(True)
        root.addWidget(self.error_label)
        save = QPushButton("保存")
        save.clicked.connect(self.validate)
        root.addWidget(save)

    def validate(self):
        try:
            remaining, total = self.values()
            if not math.isfinite(remaining) or remaining < 0:
                raise ValueError()
            if total is not None and (not math.isfinite(total) or total <= 0 or total < remaining):
                raise ValueError()
        except ValueError:
            self.error_label.setText("请输入有效余量；总量应大于零且不小于余量。")
            return
        self.accept()

    def values(self):
        return float(self.ed_rem.text()), float(self.ed_total.text()) if self.ed_total.text().strip() else None


class ConnectDialog(QDialog):
    def __init__(self, cfg, parent=None):
        super().__init__(parent)
        self.setWindowTitle("连接 " + cfg.get("name", "服务"))
        self.setStyleSheet(STYLE)
        self.resize(330, 190)
        self.cfg = cfg
        root = QVBoxLayout(self)
        hint_text = "输入 API Key，仅通过 Windows 加密保存在本机。留空不会修改现有密钥。"
        if cfg.get("type") in ("kimi", "kimi_coding"):
            hint_text = ("Kimi Code 控制台创建 sk-kimi- 开头的 Key（会员套餐额度）；"
                         "开放平台 sk- 开头的 Key 则自动查询余额。仅加密保存在本机。")
        hint = label(hint_text, "muted")
        hint.setWordWrap(True)
        root.addWidget(hint)
        self.key = QLineEdit()
        self.key.setEchoMode(QLineEdit.Password)
        self.key.setPlaceholderText("API Key")
        root.addWidget(self.key)
        self.error = label("")
        root.addWidget(self.error)
        save = QPushButton("连接并同步余额")
        save.clicked.connect(self.save)
        root.addWidget(save)

    def save(self):
        key = self.key.text().strip()
        if not key:
            self.error.setText("请在这里填写密钥，无需发送到聊天。")
            return
        try:
            save_secret(self.cfg["id"], key)
        except OSError:
            self.error.setText("保存失败，请检查 Windows 用户权限")
            return
        self.key.clear()
        self.accept()


SEG_REMAIN, SEG_TODAY, SEG_BEFORE = "#5fae9f", "#e0a458", "#c2c7ba"


# ---------------- 额度动态: 序列/燃烧速率 ----------------
def provider_series(history, pid, hours=24, now=None):
    """该服务最近 hours 小时的 (时刻, 剩余%) 序列, 时间升序。"""
    now = time.time() if now is None else now
    cutoff = now - hours * 3600
    pts = [(r["ts"], r["pct"]) for r in history
           if r.get("id") == pid and cutoff <= r.get("ts", 0) <= now]
    pts.sort()
    return pts


def burn_rate(pts, now=None):
    """燃烧速率 %/小时, 正=在烧, 负=恢复中。近 3h 优先, 点少退化到 12h/全程。"""
    if len(pts) < 2:
        return None
    now = pts[-1][0] if now is None else now
    for span in (3 * 3600, 12 * 3600, None):
        window = [p for p in pts if now - p[0] <= span] if span else pts
        if len(window) >= 2:
            (t0, p0), (t1, p1) = window[0], window[-1]
            dt = t1 - t0
            if dt >= 1200:                      # 至少 20 分钟跨度才可信
                return (p0 - p1) / dt * 3600.0
    return None


class Sparkline(QWidget):
    """剩余比例迷你趋势: 折线 + 渐变填充 + 末端亮点。"""

    def __init__(self, pts, color="#5fae9f", parent=None):
        super().__init__(parent)
        self.setFixedSize(74, 22)
        now = pts[-1][0] if pts else time.time()
        self._pts = [(max(0.0, (ts - (now - 24 * 3600)) / (24 * 3600.0)), pct)
                     for ts, pct in pts]
        self._color = QColor(color)

    def paintEvent(self, event):
        if len(self._pts) < 2:
            return
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        w, h = self.width(), self.height()
        path = QPainterPath()
        for i, (fx, pct) in enumerate(self._pts):
            x = 1.5 + max(0.0, min(1.0, fx)) * (w - 3)
            y = 2 + (1 - max(0.0, min(100.0, pct)) / 100.0) * (h - 5)
            path.moveTo(x, y) if i == 0 else path.lineTo(x, y)
        fill = QPainterPath(path)
        fill.lineTo(w - 1.5, h - 1)
        fill.lineTo(1.5, h - 1)
        fill.closeSubpath()
        g = QLinearGradient(0, 0, 0, h)
        top = QColor(self._color)
        top.setAlpha(70)
        g.setColorAt(0, top)
        g.setColorAt(1, QColor(0, 0, 0, 0))
        p.setPen(Qt.NoPen)
        p.setBrush(g)
        p.drawPath(fill)
        p.setPen(QPen(self._color, 1.4))
        p.setBrush(Qt.NoBrush)
        p.drawPath(path)
        p.setBrush(self._color)
        p.setPen(Qt.NoPen)
        p.drawEllipse(path.currentPosition(), 2.2, 2.2)


class ProviderIcon(QWidget):
    """可点击的服务图标: 点击把对应应用窗口提到最前(没开就启动)。"""

    clicked = Signal()

    def __init__(self, pid, color, size=22, gray=False, tip="", clickable=True):
        super().__init__()
        self.setCursor(Qt.PointingHandCursor if clickable else Qt.ArrowCursor)
        self.setToolTip(tip)
        self.setFixedSize(size, size)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.addWidget(icon_widget(pid, color, size, gray))

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.LeftButton and self.rect().contains(event.position().toPoint()):
            self.clicked.emit()


def seg_bar(segments, drained=False):
    """三段横条: 剩余 / 今日已耗 / 此前已耗, 宽度按百分比分配, 零段不画。"""
    holder = QWidget()
    holder.setFixedHeight(6)
    row = QHBoxLayout(holder)
    row.setContentsMargins(0, 0, 0, 0)
    row.setSpacing(1)
    for pct, color in segments:
        if pct <= 0.05:
            continue
        chunk = QFrame()
        chunk.setFixedHeight(6)
        chunk.setStyleSheet("background:%s;border-radius:2px;"
                            % ("#b9bfc0" if drained else color))
        row.addWidget(chunk, max(1, int(round(pct))))
    return holder


# ---------------- 挂牌头部 + 毛玻璃 ----------------
HOLE_W, HOLE_H, HOLE_Y = 96, 20, 10     # 顶部胶囊挂孔的几何
HOLE_STRIP_H = 42                        # 孔条高度: 只有纸面与气眼, 不放别的
PLATE_MX, PLATE_H = 6, 64                # 铭牌与左右边的留白 / 铭牌高
HEADER_H = HOLE_STRIP_H + PLATE_H + 4    # 挂牌头总高(孔条 + 铭牌)

ACCENT_DISABLED = 0
ACCENT_ACRYLIC = 4
WCA_ACCENT_POLICY = 19


class _AccentPolicy(ctypes.Structure):
    _fields_ = [("AccentState", ctypes.c_uint), ("AccentFlags", ctypes.c_uint),
                ("GradientColor", ctypes.c_uint), ("AnimationId", ctypes.c_uint)]


class _CompAttrData(ctypes.Structure):
    _fields_ = [("Attribute", ctypes.c_int), ("Data", ctypes.c_void_p),
                ("SizeOfData", ctypes.c_size_t)]


def _set_window_acrylic(hwnd, tint_abgr):
    """Windows 亚克力毛玻璃; tint_abgr=None 关闭。失败静默(旧系统/离屏)。"""
    if os.name != "nt" or not hwnd:
        return False
    try:
        if tint_abgr is None:
            accent = _AccentPolicy(ACCENT_DISABLED, 0, 0, 0)
        else:
            accent = _AccentPolicy(ACCENT_ACRYLIC, 2, tint_abgr, 0)
        data = _CompAttrData(WCA_ACCENT_POLICY,
                             ctypes.cast(ctypes.byref(accent), ctypes.c_void_p),
                             ctypes.sizeof(accent))
        return bool(ctypes.windll.user32.SetWindowCompositionAttribute(
            ctypes.c_void_p(int(hwnd)), ctypes.byref(data)))
    except Exception:
        return False


def _apply_backdrop(hwnd, transparency):
    """看板毛玻璃: Win11 用 DWM 系统背景(丙烯酸=TRANSIENTWINDOW),
    旧系统回退到 ACCENT_ACRYLIC。transparency=0 关闭。失败静默。"""
    if os.name != "nt" or not hwnd:
        return
    if transparency <= 0:
        # 关闭: 关掉 DWM 背景, 也清掉可能残留的亚克力合成属性
        try:
            off = ctypes.c_int(1)  # DWMSBT_NONE
            ctypes.windll.dwmapi.DwmSetWindowAttribute(
                ctypes.c_void_p(int(hwnd)), 38, ctypes.byref(off), 4)
        except Exception:
            pass
        _set_window_acrylic(hwnd, None)
        return
    # Win11: 先开宿主背景刷, 再指定丙烯酸背景类型(否则丙烯酸常常不显示)
    try:
        enable = ctypes.c_int(1)
        ctypes.windll.dwmapi.DwmSetWindowAttribute(
            ctypes.c_void_p(int(hwnd)), 16, ctypes.byref(enable), 4)
        acrylic = ctypes.c_int(3)  # DWMSBT_TRANSIENTWINDOW
        ctypes.windll.dwmapi.DwmSetWindowAttribute(
            ctypes.c_void_p(int(hwnd)), 38, ctypes.byref(acrylic), 4)
        return
    except Exception:
        pass
    # Win10 回退: 亚克力合成属性
    alpha = round(255 * (1 - transparency / 100.0))
    tint = (alpha << 24) | (0xF0 << 16) | (0xF4 << 8) | 0xF4  # #f4f4f0 → ABGR
    _set_window_acrylic(hwnd, tint)


def _hole_path(parent_w):
    """挂牌胶囊挂孔: 顶部中央, 真透明穿透窗口。"""
    hole = QPainterPath()
    hole.addRoundedRect(QRectF(parent_w / 2 - HOLE_W / 2, HOLE_Y, HOLE_W, HOLE_H),
                        HOLE_H / 2, HOLE_H / 2)
    return hole


class HoleStrip(QWidget):
    """孔条: 只有纸面挂孔 + 金属气眼圈, 别的什么都不放。"""

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        hole = _hole_path(self.width())
        # 气眼(grommet): 外亮环 + 内深环 + 孔缘阴影, 像铆在纸上的金属圈
        outer = QPainterPath(hole)
        stroked = QPainterPathStroker()
        stroked.setWidth(5)
        ring_outer = stroked.createStroke(outer)
        p.setPen(Qt.NoPen)
        p.fillPath(ring_outer, QColor("#e7e9e0"))          # 亮圈(金属高光)
        stroked.setWidth(2.2)
        ring_inner = stroked.createStroke(outer)
        p.fillPath(ring_inner, QColor("#b6bdb2"))          # 深圈(金属暗部)
        stroked.setWidth(1.0)
        rim = stroked.createStroke(outer)
        p.fillPath(rim, QColor("#8f988c"))                 # 孔缘(阴影/厚度)


class HeaderPlate(QWidget):
    """铭牌: 贴在吊牌上的深色圆角信息牌(品牌行 + 复活信息), 不再挖孔。"""

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        w, h = self.width(), self.height()
        plate = QPainterPath()
        plate.addRoundedRect(QRectF(0.5, 0.5, w - 1, h - 1), 12, 12)
        p.setPen(Qt.NoPen)
        p.fillPath(plate, QColor("#121b1d"))
        p.setPen(QPen(QColor("#2c3a3c"), 1))
        p.setBrush(Qt.NoBrush)
        p.drawPath(plate)


class PanelSurface(QFrame):
    """面板底: 圆角实底, 中上挖穿挂孔(配合毛玻璃时只画边框)。"""

    def __init__(self):
        super().__init__()
        self.glass = False
        self.glass_alpha = 255

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        w, h = self.width(), self.height()
        body = QPainterPath()
        body.addRoundedRect(QRectF(0.5, 0.5, w - 1, h - 1), 18, 18)
        body = body.subtracted(_hole_path(w))
        if self.glass:
            # 毛玻璃: 透出 DWM/亚克力背景, 叠一层半透明白雾控制雾化浓度
            p.fillPath(body, QColor(244, 244, 240, self.glass_alpha))
        else:
            p.fillPath(body, QColor("#f4f4f0"))
        p.setPen(QPen(QColor("#d9dcd2"), 1))
        p.setBrush(Qt.NoBrush)
        p.drawPath(body)


class UsageCard(QFrame):
    """一排一个服务: 大行值 + 动态行(趋势/速率/倒计时) + 每个额度窗口一条三段横条。
    在线的可用服务带白色卡片底置顶强调, 耗尽/失败的保持扁平沉底。"""
    edited = Signal(str, float, object)
    connected = Signal()
    activate = Signal(str)          # 点了服务图标: 前置窗口/启动应用

    def __init__(self, cfg, data, color="#5fae9f", baseline=None, series=None,
                 emphasized=False, parent=None):
        super().__init__(parent)
        self.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Fixed)
        self.cfg, self.data = cfg, data
        self.baseline = baseline   # 今日零点前最后一次同步到的服务剩余%(分不出就 None)
        self.countdowns = []
        self.free_rows = []         # 免费模型卡的 (色点, 状态标签, 规则) 行
        if emphasized:
            self.setObjectName("cardLive")
        root = QVBoxLayout(self)
        root.setContentsMargins(8, 6, 8, 5) if emphasized \
            else root.setContentsMargins(4, 5, 4, 3)
        root.setSpacing(4)

        pct = service_pct(data)
        drained = (data.get("ok") and not data.get("stale")
                   and pct is not None and pct <= 0.5)
        kind = cfg.get("type")

        # 免费模型清单卡: 直接渲染策略表, 不走主值/窗口条逻辑
        if data.get("free_models") is not None:
            self._render_free(data, color)
            self.tick()
            return

        # ---- 行 1: 图标 名称 徽章 …… 大号主值 ----
        top = QHBoxLayout()
        top.setSpacing(5)
        clickable = bool(cfg.get("processes") or cfg.get("launch"))
        tip = "点击前置窗口 / 启动" if clickable else ""
        icon = ProviderIcon(cfg["id"], color, 22, gray=drained, tip=tip,
                            clickable=clickable)
        if clickable:
            icon.clicked.connect(lambda: self.activate.emit(self.cfg["id"]))
        top.addWidget(icon)
        name = label(cfg.get("name", "?"))
        name.setStyleSheet("font-weight:700;font-size:12px" +
                           (";color:#98a09a" if drained else ""))
        top.addWidget(name)
        if kind == "manual":
            key = "manual"
        elif data.get("stale"):
            key = "stale"
        elif drained:
            key = "drained"
        elif data.get("ok") and pct is not None and pct < 30:
            key = "alert"
        elif data.get("ok"):
            key = "synced"
        else:
            key = "pending"
        text, bg, fg = BADGES[key]
        badge = label(text)
        badge.setStyleSheet("background:%s;color:%s;border-radius:6px;padding:0px 4px;font-size:9px;" % (bg, fg))
        badge.setFixedHeight(16)
        top.addWidget(badge, 0, Qt.AlignVCenter)
        top.addStretch()
        strong = "#98a09a" if drained else "#1d2a2b"
        resets = next_reset(data)
        if resets:
            soon = resets - time.time() < 2 * 3600
            fmt = "%H:%M" if resets - time.time() < 20 * 3600 else "%m/%d %H:%M"
            when = label(time.strftime(fmt, time.localtime(resets)))
            when.setStyleSheet("font-size:14px;font-weight:800;color:%s;"
                               % ("#c96a4a" if soon and not drained else strong))
            top.addWidget(when)
            cap = label(" 复活", "muted")
            top.addWidget(cap)
        elif data.get("ok") and data.get("remaining") is not None:
            unit = data.get("unit", "")
            remaining = data["remaining"]
            if not drained and pct is not None and pct < 10:
                big = "#bb5b3f"
            elif not drained and pct is not None and pct < 30:
                big = "#c96a4a"
            else:
                big = strong
            if unit == "%":
                big_v = label("%.0f%%" % remaining)
            elif unit == "¥":
                big_v = label("¥ %.2f" % remaining)
            elif data.get("total"):
                big_v = label("%g" % remaining)
            else:
                big_v = label("%g %s" % (remaining, unit))
            big_v.setStyleSheet("font-size:16px;font-weight:800;color:%s;" % big)
            top.addWidget(big_v)
            if data.get("total") and unit != "%":
                top.addWidget(label("/%g %s" % (data["total"], unit), "muted"))
        else:
            top.addWidget(label("待连接", "muted"))
        root.addLayout(top)

        # ---- 行 2: 动态行 —— 趋势 · 燃烧速率 · 倒计时 · 操作按钮 ----
        series = series or []
        dyn = QHBoxLayout()
        dyn.setSpacing(8)
        if len(series) >= 2 and pct is not None:
            dyn.addWidget(Sparkline(series, SEG_REMAIN))
        rate = burn_rate(series) if len(series) >= 2 else None
        if rate is not None and abs(rate) >= 0.15:
            if rate > 0:
                hot = "#c96a4a" if rate > 1.5 else "#e0a458"
                chip = label("↓ %.1f%%/h" % rate)
                chip.setStyleSheet("font-size:11px;font-weight:800;color:%s;" % hot)
            else:
                chip = label("↑ %.1f%%/h" % (-rate))
                chip.setStyleSheet("font-size:11px;font-weight:800;color:#5fae9f;")
            dyn.addWidget(chip)
        dyn.addStretch()
        if resets:
            self._cd = label("")
            self._cd.setStyleSheet("font-size:11px;font-weight:700;color:#1d2a2b;")
            self.countdowns.append((self._cd, resets))
            dyn.addWidget(self._cd)
        if kind == "manual":
            action = QPushButton("更新")
            action.setFixedHeight(18)
            action.clicked.connect(self.configure)
            dyn.addWidget(action)
        elif kind in ("moonshot", "kimi", "kimi_coding", "siliconflow") and not data.get("ok"):
            action = QPushButton("连接")
            action.setFixedHeight(18)
            action.clicked.connect(self.configure)
            dyn.addWidget(action)
        if dyn.count():
            root.addLayout(dyn)

        # ---- 错误行(同步失败/接口停用才占一行, 静态附注进 tooltip) ----
        if data.get("error"):
            err = label(data["error"], "muted")
            err.setWordWrap(True)
            err.setStyleSheet("color:#bb5b3f;font-size:10px;")
            root.addWidget(err)

        # ---- 额度窗口横条: 一窗一条, 三段=剩余/今日已耗/此前已耗 ----
        windows = (data.get("windows") or [])[:3]
        for window in windows:
            remain = max(0.0, min(100.0, window.get("remaining_percent", 100)))
            bar_row = QHBoxLayout()
            bar_row.setSpacing(5)
            tag = label(window.get("label", ""), "muted")
            tag.setFixedWidth(46)
            bar_row.addWidget(tag)
            bar_row.addWidget(seg_bar(self._segments(remain, bool(window.get("daily"))),
                                      drained), 1)
            show = label("%.0f%%" % remain, "muted")
            show.setFixedWidth(30)
            show.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
            bar_row.addWidget(show)
            root.addLayout(bar_row)
        if not windows and pct is not None:
            root.addWidget(seg_bar(self._segments(pct, False), drained))

        tip_lines = []
        for w in data.get("windows") or []:
            line = "%s 剩 %.0f%%" % (w.get("label", ""), w.get("remaining_percent", 0))
            if w.get("resets_at"):
                line += " · %s 重置" % time.strftime("%m/%d %H:%M", time.localtime(w["resets_at"]))
            tip_lines.append(line)
        tip_lines += [data.get("source", ""), data.get("note") or ""]
        tip_lines.append(("更新于 " + time.strftime("%m/%d %H:%M:%S", time.localtime(data["fetched_at"])))
                         if data.get("fetched_at") else "尚无同步记录")
        self.setToolTip("\n".join(t for t in tip_lines if t))
        self.tick()

    def _segments(self, remain_pct, daily):
        """三段拆分: 日窗的消耗全算今天; 其余按今日零点前的历史基线拆
        (没有基线就只有 剩余+已耗 两段)。"""
        used = 100.0 - remain_pct
        if daily:
            return [(remain_pct, SEG_REMAIN), (used, SEG_TODAY)]
        if self.baseline is None:
            return [(remain_pct, SEG_REMAIN), (used, SEG_BEFORE)]
        used_today = max(0.0, min(used, self.baseline - remain_pct))
        return [(remain_pct, SEG_REMAIN), (used_today, SEG_TODAY),
                (used - used_today, SEG_BEFORE)]

    def tick(self):
        for widget, timestamp in self.countdowns:
            text, due = countdown_parts(timestamp)
            widget.setText(("已到期 · 待同步" if due else "%s后" % text))
        if getattr(self, "free_rows", None):
            self._refresh_free_status()

    def _render_free(self, data, color):
        """免费模型清单: 列出限免/夜间免费模型, 实时标注当前是否免费。"""
        lay = self.layout()
        models = data.get("free_models") or []
        count = data.get("free_count", 0)
        total = data.get("free_total", len(models))
        top = QHBoxLayout()
        top.setSpacing(5)
        clickable = bool(self.cfg.get("processes") or self.cfg.get("launch"))
        icon = ProviderIcon(self.cfg["id"], color, 22,
                            tip="点击前置窗口 / 启动" if clickable else "",
                            clickable=clickable)
        if clickable:
            icon.clicked.connect(lambda: self.activate.emit(self.cfg["id"]))
        top.addWidget(icon)
        name = label(self.cfg.get("name", "?"))
        name.setStyleSheet("font-weight:700;font-size:12px;")
        top.addWidget(name)
        badge = label("%d/%d 免费" % (count, total))
        badge.setStyleSheet("background:#e3f2ec;color:#2e7d6b;border-radius:6px;"
                            "padding:0px 4px;font-size:9px;")
        badge.setFixedHeight(16)
        top.addWidget(badge, 0, Qt.AlignVCenter)
        top.addStretch()
        lay.addLayout(top)
        for m in models:
            row = QHBoxLayout()
            row.setSpacing(6)
            dot = QFrame()
            dot.setFixedSize(8, 8)
            row.addWidget(dot)
            mname = label(m.get("name", "?"))
            mname.setStyleSheet("font-size:11px;font-weight:600;")
            row.addWidget(mname, 1)
            status = label("")
            status.setFixedHeight(16)
            row.addWidget(status)
            lay.addLayout(row)
            self.free_rows.append((dot, status, m))
        self._refresh_free_status()
        self.setToolTip(data.get("note", "") or "")

    def _refresh_free_status(self):
        """按当前时间重算每个模型的免费状态(夜间时段/限免截止), 实时更新着色。"""
        if not self.free_rows:
            return
        now = time.time()
        count = 0
        for dot, status, m in self.free_rows:
            _free_now, text, bg, fg = _free_status(m, now)
            dot.setStyleSheet("background:%s;border-radius:4px;" % fg)
            status.setText(text)
            status.setStyleSheet("background:%s;color:%s;border-radius:6px;"
                                "padding:0px 5px;font-size:9px;" % (bg, fg))

    def configure(self):
        if self.cfg.get("type") == "manual":
            dialog = ManualEditDialog(self.cfg.get("name", ""), self.cfg, self)
            if dialog.exec() == QDialog.Accepted:
                rem, total = dialog.values()
                self.edited.emit(self.cfg["id"], rem, total)
        else:
            if ConnectDialog(self.cfg, self).exec() == QDialog.Accepted:
                self.connected.emit()

    def mouseDoubleClickEvent(self, event):
        if self.cfg.get("type") == "manual":
            self.configure()


class Dashboard(QWidget):
    refresh_requested = Signal()
    manual_edited = Signal(str, float, object)
    provider_activated = Signal(str)    # 点了某服务的图标, 请求前置/启动

    def __init__(self):
        super().__init__()
        self.setWindowTitle("Rockabuddy · 用量小管家")
        self.setWindowFlags(Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self._drag_pos = None
        self.cfg, self.results, self.history = {"providers": []}, {}, []
        self.cards = []
        self._hero_reset = None
        self._hero_pid = None
        self._settings = QSettings("Rockabuddy", "Dashboard")
        self._collapsed = self._settings.value("panel_collapsed", False, type=bool)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        surface = PanelSurface()
        self._surface = surface
        self._glass = 0
        outer.addWidget(surface)
        self.setStyleSheet(STYLE)
        root = QVBoxLayout(surface)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # ---- 挂牌头: 孔条(纸面+气眼)在上, 深色铭牌(品牌/按钮/复活)贴在下面 ----
        strip = HoleStrip()
        strip.setFixedHeight(HOLE_STRIP_H)
        root.addWidget(strip)

        plate_holder = QWidget()
        plate_holder.setContentsMargins(PLATE_MX, 0, PLATE_MX, 4)
        plate_lay = QVBoxLayout(plate_holder)
        plate_lay.setContentsMargins(0, 0, 0, 0)
        plate = HeaderPlate()
        plate.setFixedHeight(PLATE_H)
        plate_root = QVBoxLayout(plate)
        plate_root.setContentsMargins(10, 5, 10, 6)
        plate_root.setSpacing(2)

        rowa = QHBoxLayout()
        rowa.setContentsMargins(2, 0, 2, 0)
        rowa.setSpacing(6)
        brand = label("ROCKABUDDY")
        brand.setStyleSheet("color:#8fa3a0;font-size:9px;font-weight:700;letter-spacing:3px;")
        rowa.addWidget(brand)
        rowa.addStretch(1)
        self.btn_collapse = QPushButton("仅可用")
        self.btn_collapse.setObjectName("ghost")
        self.btn_collapse.setFixedHeight(20)
        self.btn_collapse.setToolTip("精简模式：只显示可用的 AI 服务")
        self.btn_collapse.clicked.connect(self.toggle_collapse)
        rowa.addWidget(self.btn_collapse)
        self.update_collapse_label()
        self.btn_refresh = QPushButton("⟳ 刷新")
        self.btn_refresh.setObjectName("ghost")
        self.btn_refresh.setFixedHeight(20)
        self.btn_refresh.clicked.connect(self.refresh_requested)
        rowa.addWidget(self.btn_refresh)
        close = QPushButton("×")
        close.setObjectName("ghost")
        close.setAccessibleName("收起看板")
        close.setFixedSize(20, 20)
        close.setStyleSheet("padding:0px;")
        close.clicked.connect(self.hide)
        rowa.addWidget(close)
        plate_root.addLayout(rowa)

        # ---- 复活信息行(铭牌下半) ----
        hero_row = QWidget()
        hero_layout = QHBoxLayout(hero_row)
        hero_layout.setContentsMargins(2, 0, 2, 0)
        hero_layout.setSpacing(8)
        self.hero_icon = QLabel()
        self.hero_icon.setFixedSize(28, 28)
        self.hero_icon.setAlignment(Qt.AlignCenter)
        hero_layout.addWidget(self.hero_icon)
        mid = QVBoxLayout()
        mid.setSpacing(0)
        self.hero_name = label("下次复活", "heroName")
        mid.addWidget(self.hero_name)
        self.hero_count = label("--", "heroCount")
        mid.addWidget(self.hero_count)
        hero_layout.addLayout(mid, 1)
        right = QVBoxLayout()
        right.setSpacing(1)
        right.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        self.hero_clock = label("")
        self.hero_clock.setStyleSheet("color:#f4f6f2;font-size:12px;font-weight:700;")
        self.hero_clock.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        right.addWidget(self.hero_clock)
        self.hero_hint = label("", "mutedLight")
        self.hero_hint.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        right.addWidget(self.hero_hint)
        hero_layout.addLayout(right)
        plate_root.addWidget(hero_row, 1)
        plate_lay.addWidget(plate)
        root.addWidget(plate_holder)

        # ---- 响应式内容区: 屏幕放不下时内部滚动而不是被截断 ----
        self._scroll = QScrollArea()
        self._scroll.setWidgetResizable(True)
        self._scroll.setFrameShape(QFrame.NoFrame)
        self._scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self._scroll.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        body = QWidget()
        self._body = body
        body_root = QVBoxLayout(body)
        body_root.setContentsMargins(10, 8, 10, 8)
        body_root.setSpacing(8)
        self._scroll.setWidget(body)
        root.addWidget(self._scroll, 1)

        # ---- 服务行: 一排一个, 无卡片底, 一屏放下 ----
        self.items = QVBoxLayout()
        self.items.setContentsMargins(0, 0, 0, 0)
        self.items.setSpacing(9)
        body_root.addLayout(self.items)
        body_root.addStretch()

        self.footer = label("", "muted")
        self.footer.setWordWrap(True)
        body_root.addWidget(self.footer)
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.tick)
        self.timer.start(1000)
        self._fit_height()

    def _update_mask(self):
        """窗口真实裁剪: 圆角矩形挖穿孔, 孔与边角从窗口里被切掉(真正透明),
        不被毛玻璃底衬填满。尺寸变化时需重算。"""
        w, h = self.width(), self.height()
        if w <= 0 or h <= 0:
            return
        mask = QBitmap(w, h)
        p = QPainter(mask)
        p.setBrush(Qt.color1)
        p.setPen(Qt.color1)
        path = QPainterPath()
        path.addRoundedRect(QRectF(0, 0, w, h), 18, 18)
        path = path.subtracted(_hole_path(w))
        p.fillPath(path, Qt.color1)
        p.end()
        self.setMask(mask)

    def _fit_height(self):
        """固定宽 320; 高度按内容, 超过屏幕可用高度就收进屏幕(内容转内部滚动)。"""
        content = HEADER_H + self._body.sizeHint().height() + 2
        scr = QApplication.primaryScreen().availableGeometry()
        self.setFixedWidth(320)
        self.setFixedHeight(min(content, int(scr.height() * .92)))
        self._update_mask()

    def reposition_for(self, pet):
        """贴着桌宠选位: 左/右/上/下, 钳进屏幕可用区且不遮桌宠;
        放不下时先收缩高度(内容转内部滚动)再试, 都不行退到重叠最小的方位。"""
        petg = pet.frameGeometry()
        scr = (QApplication.screenAt(petg.center())
               or QApplication.primaryScreen()).availableGeometry()
        content = HEADER_H + self._body.sizeHint().height() + 2
        self.setFixedWidth(320)
        self.setFixedHeight(min(content, int(scr.height() * .92)))
        self._update_mask()
        bw, bh, gap = self.width(), self.height(), 10
        candidates = [
            (petg.left() - gap - bw, petg.center().y() - bh // 2),   # 左
            (petg.right() + gap, petg.center().y() - bh // 2),       # 右
            (petg.center().x() - bw // 2, petg.top() - gap - bh),    # 上
            (petg.center().x() - bw // 2, petg.bottom() + gap),      # 下
        ]
        best = None
        for cx, cy in candidates:
            x = min(max(scr.left(), cx), scr.right() - bw + 1)
            y = min(max(scr.top(), cy), scr.bottom() - bh + 1)
            rect = QRect(x, y, bw, bh)
            inter = rect.intersected(petg)
            overlap = inter.width() * inter.height()
            if scr.contains(rect) and overlap == 0:
                best = rect
                break
            score = (0 if scr.contains(rect) else 1, overlap)
            if best is None or score < best[0]:
                best = (score, rect)
        if not isinstance(best, QRect):
            best = best[1]
        self.move(best.topLeft())

    def apply_glass(self, transparency):
        """看板毛玻璃透明度: 0=不透明关闭, 其余为透过比例(1-85)。"""
        transparency = max(0, min(85, int(transparency)))
        self._glass = transparency
        self._surface.glass = transparency > 0
        # 透过比例越大(alpha 越小)越通透, 但留个下限保证文字可读
        self._surface.glass_alpha = max(60, round(255 * (1 - transparency / 100.0)))
        self._surface.update()
        hwnd = self.winId()
        _apply_backdrop(hwnd, transparency)
        self.update()

    def showEvent(self, event):
        super().showEvent(event)
        self._update_mask()                 # 窗口句柄重建后重投裁剪遮罩
        if self._glass > 0:
            self.apply_glass(self._glass)   # 窗口句柄可能被重建, 重投一次

    def _finish_init(self):
        pass

    def set_data(self, cfg, results, history=None):
        self.cfg, self.results = cfg, results
        if history is not None:
            self.history = history
        self.rebuild()

    def set_sync_info(self, text):
        self.footer.setText(text)

    def _is_usable(self, cfg):
        """在线且未耗尽: 精简模式下只展示这类卡片。"""
        d = self.results.get(cfg["id"], {})
        if not d.get("ok") or d.get("stale"):
            return False
        p = service_pct(d)
        return p is None or p > 0.5

    def _empty_state(self):
        """没有卡片可展示时的占位(精简态无可用服务 / 还没配置服务)。"""
        box = QWidget()
        lay = QVBoxLayout(box)
        lay.setContentsMargins(14, 22, 14, 22)
        lay.setSpacing(6)
        if self._collapsed:
            t1, t2 = "没有可用的 AI 服务", "点「全部」展开查看所有"
        else:
            t1, t2 = "还没有添加任何服务", "在 config.json 的 providers 里加上"
        a = label(t1, "muted")
        a.setStyleSheet("font-size:13px;font-weight:700;color:#788784;")
        a.setAlignment(Qt.AlignCenter)
        b = label(t2, "mutedLight")
        b.setAlignment(Qt.AlignCenter)
        lay.addWidget(a)
        lay.addWidget(b)
        return box

    def update_collapse_label(self):
        # 按钮直接显示当前态: 收缩时高亮「仅可用」, 展开时普通「全部」
        if self._collapsed:
            self.btn_collapse.setText("仅可用")
            self.btn_collapse.setStyleSheet(
                "background:#5fae9f;color:#0f1a1a;border-radius:8px;"
                "padding:5px 10px;font-weight:700;")
        else:
            self.btn_collapse.setText("全部")
            self.btn_collapse.setStyleSheet("")

    def toggle_collapse(self):
        self._collapsed = not self._collapsed
        self._settings.setValue("panel_collapsed", self._collapsed)
        self.update_collapse_label()
        self.rebuild()

    def _day_baseline(self, pid, midnight):
        """该服务今日零点前最后一次记录的剩余%(history 按时间追加, 取最后一个)。"""
        base = None
        for row in self.history:
            if row.get("id") == pid and row.get("ts", 0) <= midnight:
                base = row.get("pct")
        return base

    def _pick_hero(self):
        """下次复活 = 所有窗口里最近的重置点; 没有则退到余量最低的服务。"""
        best = None
        providers = self.cfg.get("providers", [])
        if self._collapsed:
            providers = [c for c in providers if self._is_usable(c)]
        for cfg in providers:
            data = self.results.get(cfg["id"], {})
            ts = next_reset(data)
            if ts and (best is None or ts < best[0]):
                best = (ts, cfg, data)
        if best:
            return best, False
        lowest = None
        for cfg in self.cfg.get("providers", []):
            data = self.results.get(cfg["id"], {})
            pct = service_pct(data)
            if pct is not None and (lowest is None or pct < lowest[0]):
                lowest = (pct, cfg, data)
        return (lowest, True) if lowest else (None, False)

    def rebuild(self):
        while self.items.count():
            item = self.items.takeAt(0)
            widget = item.widget()
            if widget:
                widget.setParent(None)
                widget.deleteLater()
        self.cards = []
        # 今日零点: 用于把消耗拆成「今日/此前」两段(取零点前最后一次同步的剩余%)
        midnight = time.mktime(time.strptime(
            time.strftime("%Y-%m-%d") + " 00:00:00", "%Y-%m-%d %H:%M:%S"))
        providers = list(self.cfg.get("providers", []))

        def usable(cfg):
            """在线且未耗尽: 卡片强调置顶; 耗尽/失败/没数据的扁平沉底。"""
            return self._is_usable(cfg)

        if self._collapsed:
            # 精简态: 丢弃不可用的 AI, 只保留可用卡片
            providers = [c for c in providers if self._is_usable(c)]

        providers.sort(key=usable, reverse=True)   # 稳定排序: 组内保持配置顺序
        for index, cfg in enumerate(providers):
            card = UsageCard(cfg, self.results.get(cfg["id"], {}),
                             PALETTE[index % len(PALETTE)],
                             baseline=self._day_baseline(cfg["id"], midnight),
                             series=provider_series(self.history, cfg["id"]),
                             emphasized=usable(cfg))
            card.edited.connect(self.manual_edited)
            card.connected.connect(self.refresh_requested)
            card.activate.connect(self.provider_activated)
            self.items.addWidget(card)
            self.cards.append(card)

        if not self.cards:
            self.items.addWidget(self._empty_state())

        pick, is_lowest = self._pick_hero()
        if pick is None:
            self.hero_name.setText("下次复活")
            self.hero_count.setText("--")
            self.hero_clock.setText("")
            self.hero_hint.setText("等待首次同步")
            self.hero_icon.clear()
            self._hero_reset = None
        elif not is_lowest:
            ts, cfg, data = pick
            window = lowest_window(data) or {}
            self.hero_name.setText("下次复活 · %s %s" % (cfg["name"], window.get("label", "")))
            pix = icon_pixmap(cfg["id"], 30)
            if pix is not None:
                self.hero_icon.setPixmap(pix)
            self.hero_clock.setText(time.strftime("%m/%d %H:%M", time.localtime(ts)))
            self.hero_hint.setText("剩余 %.0f%%" % window.get("remaining_percent", 0))
            self._hero_reset = ts
            self._hero_pid = cfg["id"]
            text, due = countdown_parts(ts)
            self.hero_count.setText("已到期 · 同步确认中" if due else text)
        else:
            pct, cfg, data = pick
            self.hero_name.setText("余量最低 · " + cfg["name"])
            self.hero_count.setText("%.0f%%" % pct)
            pix = icon_pixmap(cfg["id"], 30)
            if pix is not None:
                self.hero_icon.setPixmap(pix)
            self.hero_clock.setText("")
            self.hero_hint.setText("各窗口暂无到期")
            self._hero_reset = None
        self._fit_height()
        self.tick()

    def tick(self):
        if not self.isVisible():
            return
        for card in self.cards:
            card.tick()
        if self._hero_reset:
            text, due = countdown_parts(self._hero_reset)
            self.hero_count.setText("已到期 · 同步确认中" if due else text)

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton and event.position().y() < 34:
            self._drag_pos = event.globalPosition().toPoint() - self.pos()

    def mouseMoveEvent(self, event):
        if self._drag_pos is not None and event.buttons() & Qt.LeftButton:
            self.move(event.globalPosition().toPoint() - self._drag_pos)

    def mouseReleaseEvent(self, event):
        self._drag_pos = None

    def keyPressEvent(self, event):
        if event.key() == Qt.Key_Escape:
            self.hide()
        else:
            super().keyPressEvent(event)
