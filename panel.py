"""Compact single-screen companion panel: reset countdowns first, no scrolling."""
import math
import os
import time
from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import QColor, QImage, QPixmap
from PySide6.QtWidgets import (QWidget, QFrame, QLabel, QPushButton, QVBoxLayout,
    QHBoxLayout, QDialog, QLineEdit, QSizePolicy)
from credentials import save_secret

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
ICON_DIR = os.path.join(BASE_DIR, "assets", "icons")

STYLE = """
QWidget {font-family:'Microsoft YaHei UI';font-size:11px;color:#30474a;}
QWidget#panel {background:#f4f4f0;border:1px solid #d9dcd2;border-radius:18px;}
QWidget#header {background:#121b1d;border-top-left-radius:17px;border-top-right-radius:17px;}
QFrame#hero {background:#141a1a;border:0;border-radius:14px;}
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
QProgressBar {background:#edf1ea;border:0;border-radius:3px;height:5px;}
QProgressBar::chunk {background:#5fae9f;border-radius:3px;}
QLineEdit {background:white;border:1px solid #d4d8c9;border-radius:7px;padding:6px;}
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


def seg_bar(segments, drained=False):
    """三段横条: 剩余 / 今日已耗 / 此前已耗, 宽度按百分比分配, 零段不画。"""
    holder = QWidget()
    holder.setFixedHeight(5)
    row = QHBoxLayout(holder)
    row.setContentsMargins(0, 0, 0, 0)
    row.setSpacing(1)
    for pct, color in segments:
        if pct <= 0.05:
            continue
        chunk = QFrame()
        chunk.setFixedHeight(5)
        chunk.setStyleSheet("background:%s;border-radius:1px;"
                            % ("#b9bfc0" if drained else color))
        row.addWidget(chunk, max(1, int(round(pct))))
    return holder


class UsageCard(QFrame):
    """一排一个服务: 图标+名称+徽章+强调值一行, 下方每个额度窗口一条三段横条。
    不加卡片底色, 靠排版分层。"""
    edited = Signal(str, float, object)
    connected = Signal()

    def __init__(self, cfg, data, color="#5fae9f", baseline=None, parent=None):
        super().__init__(parent)
        self.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Fixed)
        self.cfg, self.data = cfg, data
        self.baseline = baseline   # 今日零点前最后一次同步到的服务剩余%(分不出就 None)
        self.countdowns = []
        root = QVBoxLayout(self)
        root.setContentsMargins(4, 5, 4, 3)
        root.setSpacing(3)

        pct = service_pct(data)
        drained = (data.get("ok") and not data.get("stale")
                   and pct is not None and pct <= 0.5)
        kind = cfg.get("type")

        # ---- 行 1: 图标 名称 徽章 …… 强调值 ----
        top = QHBoxLayout()
        top.setSpacing(5)
        top.addWidget(icon_widget(cfg["id"], color, 20, gray=drained))
        name = label(cfg.get("name", "?"))
        name.setStyleSheet("font-weight:700;font-size:11px" + (";color:#98a09a" if drained else ""))
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
            soon = resets - time.time() < 20 * 3600
            fmt = "%H:%M" if soon else "%m/%d %H:%M"
            value = label(time.strftime(fmt, time.localtime(resets)) + " 复活")
            value.setStyleSheet("font-size:12px;font-weight:800;color:%s;" % strong)
            top.addWidget(value)
            self._cd = label("", "muted")
            self.countdowns.append((self._cd, resets))
        elif data.get("ok") and data.get("remaining") is not None:
            unit = data.get("unit", "")
            remaining = data["remaining"]
            if unit == "¥":
                value = label("¥ %.2f" % remaining)
            elif data.get("total"):
                value = label("%g/%g %s" % (remaining, data["total"], unit))
            else:
                value = label("%g %s" % (remaining, unit))
            value.setStyleSheet("font-size:12px;font-weight:800;color:%s;" % strong)
            top.addWidget(value)
        else:
            top.addWidget(label("待连接", "muted"))
        root.addLayout(top)

        # ---- 行 2: muted 说明(倒计时/附注) + 手动更新/连接按钮 ----
        note = data.get("error") or data.get("note") or ""
        second = QHBoxLayout()
        second.setSpacing(6)
        if resets:
            second.addWidget(self._cd)
        elif note:
            muted = label(note, "muted")
            muted.setWordWrap(True)
            second.addWidget(muted, 1)
        if kind == "manual":
            second.addStretch()
            action = QPushButton("更新")
            action.setFixedHeight(18)
            action.clicked.connect(self.configure)
            second.addWidget(action)
        elif kind in ("moonshot", "kimi", "kimi_coding", "siliconflow") and not data.get("ok"):
            second.addStretch()
            action = QPushButton("连接")
            action.setFixedHeight(18)
            action.clicked.connect(self.configure)
            second.addWidget(action)
        if second.count():
            root.addLayout(second)

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
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        surface = QWidget()
        surface.setObjectName("panel")
        outer.addWidget(surface)
        self.setStyleSheet(STYLE)
        root = QVBoxLayout(surface)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # ---- 深色顶栏: 只有刷新/收起, 无标题无头像 ----
        header = QWidget()
        header.setObjectName("header")
        header.setFixedHeight(34)
        head = QHBoxLayout(header)
        head.setContentsMargins(10, 0, 10, 0)
        head.setSpacing(6)
        head.addStretch()
        self.btn_refresh = QPushButton("⟳ 刷新")
        self.btn_refresh.setObjectName("ghost")
        self.btn_refresh.setFixedHeight(22)
        self.btn_refresh.clicked.connect(self.refresh_requested)
        head.addWidget(self.btn_refresh)
        close = QPushButton("×")
        close.setObjectName("ghost")
        close.setAccessibleName("收起看板")
        close.setFixedSize(22, 22)
        close.setStyleSheet("padding:0px;")
        close.clicked.connect(self.hide)
        head.addWidget(close)
        root.addWidget(header)

        body = QWidget()
        body_root = QVBoxLayout(body)
        body_root.setContentsMargins(10, 8, 10, 8)
        body_root.setSpacing(8)
        root.addWidget(body, 1)

        # ---- 英雄区: 下次复活(图标 + 大倒计时) ----
        hero = QFrame()
        hero.setObjectName("hero")
        hero.setFixedHeight(54)
        hero_layout = QHBoxLayout(hero)
        hero_layout.setContentsMargins(10, 6, 10, 6)
        hero_layout.setSpacing(8)
        self.hero_icon = QLabel()
        self.hero_icon.setFixedSize(30, 30)
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
        body_root.addWidget(hero)

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

    def _fit_height(self):
        self.setFixedWidth(320)
        self.layout().activate()
        self.setFixedSize(320, self.sizeHint().height())

    def _finish_init(self):
        pass

    def set_data(self, cfg, results, history=None):
        self.cfg, self.results = cfg, results
        if history is not None:
            self.history = history
        self.rebuild()

    def set_sync_info(self, text):
        self.footer.setText(text)

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
        for cfg in self.cfg.get("providers", []):
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
        for index, cfg in enumerate(self.cfg.get("providers", [])):
            card = UsageCard(cfg, self.results.get(cfg["id"], {}),
                             PALETTE[index % len(PALETTE)],
                             baseline=self._day_baseline(cfg["id"], midnight))
            card.edited.connect(self.manual_edited)
            card.connected.connect(self.refresh_requested)
            self.items.addWidget(card)
            self.cards.append(card)

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
