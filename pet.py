"""Transparent character companion. UI preferences are separate from API config."""
import ctypes
import math
import os
import random
import statistics
import sys
import time
import paths
from animation import SpritePlayer, SUNFLOWER_HOLD_FRAME, SUNFLOWER_PALM, SUNFLOWER_LIFT_SECONDS

from PySide6.QtCore import Qt, QPoint, QPointF, QRectF, QProcess, QSettings, QTimer, Signal
from PySide6.QtGui import (QAction, QActionGroup, QColor, QCursor, QFont, QFontMetrics,
                           QImage, QPainter, QPainterPath, QPen, QPixmap, QTransform)
from PySide6.QtWidgets import (QApplication, QHBoxLayout, QMenu, QPushButton,
                               QWidget, QWidgetAction,
                               QGraphicsDropShadowEffect)

ASSET = paths.resource_path("assets", "companion.png")
DESK_FIT = 0.89  # 办公桌帧构图偏大, 按头发宽度对齐站立体型(133px/150px)
SPRITE_PAD_X = 24  # 窗口左右透明安全区: 音符/星火外飘不被隐形边缘裁掉
SHAKE_WIN, SHAKE_AMP, SHAKE_NEED = 1.5, 35, 3   # 摇晕: 1.5s 内 3 次满幅反向
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
RUN_NAME = "Rockabuddy"

# 听音乐模式(延续向日葵节拍器的划分):
# off        安静待命, 不采音
# metronome  节拍器: 相位锁定的预测拍点正中轻轻点头, 摆幅限制在最轻两档
# rock       摇滚甩头: 全档摇摆帧, 低频重拍(鼓点)触发左右交替甩头冲量
# opera      美声跟唱: 眯眼笑帧, 身体以乐句(约两拍)为单位长音缓动, 幅度随响度呼吸
# singalong  跟唱说话: 检测到音频就"张嘴"跟唱, 无声立刻闭嘴待机(师范向日葵方案)
MUSIC_MODES = ("off", "metronome", "rock", "opera", "singalong")
MUSIC_MENU = {"off": "关闭",
              "metronome": "节拍器 · 卡点点头",
              "rock": "摇滚甩头 · 重拍甩头",
              "opera": "美声跟唱 · 长音缓动",
              "singalong": "跟唱说话 · 有声才动"}

# 右键菜单: 纸质吊牌质感, 圆角行 + 互斥圆点/方形开关指示
MENU_STYLE = """
QMenu {
  background: #f6f7f3;
  border: 1px solid #d9dcd2;
  border-radius: 10px;
  padding: 5px;
  font-size: 12px;
}
QMenu::item {
  padding: 7px 26px 7px 22px;
  border-radius: 7px;
  color: #26383a;
}
QMenu::item:selected { background: #cfe4dc; color: #10201f; }
QMenu::item:disabled { color: #a4aca8; }
QMenu::item:checked { font-weight: 700; }
QMenu::separator { height: 1px; background: #e0e4da; margin: 5px 10px; }
QMenu::indicator:exclusive:unchecked {
  width: 13px; height: 13px;
  border: 1.5px solid #a9b2ad; border-radius: 7px; background: #ffffff;
  left: 5px;
}
QMenu::indicator:exclusive:checked {
  width: 13px; height: 13px;
  border: 1.5px solid #5fae9f; border-radius: 7px;
  background: qradialgradient(cx:0.5, cy:0.5, radius:1, fx:0.5, fy:0.5,
                              stop:0 #5fae9f, stop:0.42 #5fae9f,
                              stop:0.55 #ffffff, stop:1 #ffffff);
  left: 5px;
}
QMenu::indicator:non-exclusive:unchecked {
  width: 13px; height: 13px;
  border: 1.5px solid #a9b2ad; border-radius: 3px; background: #ffffff;
  left: 5px;
}
QMenu::indicator:non-exclusive:checked {
  width: 13px; height: 13px;
  border: 1.5px solid #5fae9f; border-radius: 3px; background: #5fae9f;
  left: 5px;
}
"""


def autostart_enabled():
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
            winreg.QueryValueEx(key, RUN_NAME)
            return True
    except OSError:
        return False


def set_autostart(enable):
    """开机自启开关: HKCU Run 键指向本环境的 pythonw + app.py。"""
    import sys
    import winreg
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
        if not enable:
            try:
                winreg.DeleteValue(key, RUN_NAME)
            except FileNotFoundError:
                pass
            return
        if getattr(sys, "frozen", False):
            # 打包版: 自启指向 exe 本身
            cmd = '"%s"' % sys.executable
        else:
            exe = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
            if not os.path.isfile(exe):
                exe = sys.executable
            script = os.path.join(paths.DATA_DIR, "app.py")
            cmd = '"%s" "%s"' % (exe, script)
        winreg.SetValueEx(key, RUN_NAME, 0, winreg.REG_SZ, cmd)


def ease_out_back(t):
    u = min(1, max(0, t)) - 1
    return max(0.0, 1 + 2.70158 * u ** 3 + 1.70158 * u ** 2)


class PetWidget(QWidget):
    clicked = Signal()
    refresh_requested = Signal()
    moved = Signal()               # 拖动结束, 面板据此让位
    glass_changed = Signal(int)    # 看板毛玻璃透明度调整

    def __init__(self):
        super().__init__()
        self.setWindowTitle("Rockabuddy · 猫头鹰桌宠")
        self.setWindowFlags(Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setCursor(Qt.PointingHandCursor)
        self.settings = QSettings("TokenSpy", "Companion")
        self.sprite = QPixmap(ASSET)
        self.player = SpritePlayer(os.path.dirname(ASSET))
        self.character = self.settings.value("character", "owl", type=str)
        self.player.set_character(self.character)
        self._epoch = time.monotonic()
        self._now = 0.0
        self._last_tick = 0.0
        self._tilt = 0.0
        self._target_tilt = 0.0
        self._bounce_at = -10.0
        self._reaction_until = 0.0
        self._reaction = ""
        self.stale = False
        self.pct = None
        self.alert = False
        self.busy = False
        self.message = "用量小管家，待命中"
        self.revive_text = "点我查看用量"
        self._alert_text = ""
        self.orbit = []          # hover 时脑后光环: [(pid, 简短数字)]
        self._icons = {}         # pid -> QPixmap 缓存
        self._hover_since = -10.0
        self._bubble_pop = -10.0
        self._drag_pos = None
        self._press = None
        self._moved = False
        self._phase = 0.0
        self._hover = False
        # 击键同步: 每个按键按下沿触发一次, 左右爪交替
        self._paw = 0            # 最近一次击键用的爪子: 1=左 2=右
        self._paw_until = 0.0    # 爪子抬起保持到这个时刻
        # 新应用启动: 举起该应用图标(原神启动式)
        self._launch_pix = None
        self._launch_at = -10.0
        self._launch_hold = 2.4     # 定格时长(秒), 之后淡出
        self._launch_rect = None    # f26 帧的实际落位, 用于掌心锚定
        # 「快端上来罢」叠叠乐: 启动记录图标叠一塔捧出来
        self._trophy = []           # [QPixmap] 从下到上的层
        self._trophy_at = -10.0
        self._trophy_pad = 0        # 塔身向上生长时窗口临时加高的像素
        self._trophy_geom = None
        self._watcher = None
        self._watch_apps = self.settings.value("watch_apps", True, type=bool)
        # 摇晃眩晕: 方向反转且满幅才算一次有效晃动, 1.5s 内攒满 3 次就晕
        self._shake = 0.0
        self._shake_marks = []      # 有效晃动的时刻
        self._shake_dir = 0         # 当前计向(0=未定)
        self._shake_ref_x = 0       # 上一个转向点
        self._last_drag = None      # 上次移动的 x
        self._dizzy_until = -10.0
        # 听音乐模式: 旧版 rock 布尔设置迁移为模式名
        legacy_rock = self.settings.value("rock", True, type=bool)
        self._music_mode = self.settings.value("music_mode", "", type=str)
        if self._music_mode not in MUSIC_MODES:
            self._music_mode = "rock" if legacy_rock else "off"
        self._music_on = False
        self._beat_at = -10.0
        self._beat_strength = 0.0
        self._beat_period = 0.0
        self._beat_dir = 1
        # 语音活动(跟唱/美声): 快/慢响度包络, 由 BeatListener.voice 更新
        self._voice_level = 0.0
        self._voice_slow = 0.0
        self._voice_at = -10.0
        self._opera_anchor = 0.0
        self._opera_side = 1
        self._opera_note_at = -1.0    # 当前乐句开始时刻(>=0 表示嘴边正蓄着音符)
        # 训练模式: 10 秒敲击采集(八字双铅笔), 结束按敲击风格适配模式
        self._train_until = -1.0
        self._train_taps = []         # [开始时刻, 时长或None, 虚拟键]
        self._train_held = {}         # 仍按着的键: vk -> (按下时刻, 笔序号)
        self._pen_press = [0.0, 0.0]  # 每支笔的按压量 0-1(长按伏地)
        self._pen_kick = [0.0, 0.0]   # 敲击冲量(短促一敲)
        self._pen_side = 0
        self._train_demo_until = -1.0
        self._notes = []           # 音符粒子: [birth, x0, y0, drift, glyph, size]
        self._last_note = -10.0
        self._listener = None
        self._animate = self.settings.value("animate", True, type=bool)
        self._desk = self.settings.value("desk", False, type=bool)
        self._panel_glass = self.settings.value("panel_glass", 45, type=int)
        if not self.settings.contains("panel_glass"):
            # 一次性迁移: 旧版默认 0(关闭)升级为默认 45% 毛玻璃, 已手动改过的不动
            self.settings.setValue("panel_glass", 45)
        # 光标跟踪: 身体朝光标方向的微小偏移(px)
        self._gaze_x = 0.0
        self._gaze_y = 0.0
        # 自主待机: 伸懒腰/抖耳朵/打盹, 无人理她时触发
        self._doze_until = -10.0
        self._next_z = 0.0            # 下一个 Zzz 粒子的时刻
        self._next_quirk = 25.0 + random.random() * 20
        self._last_interaction = 0.0
        self.set_size(self.settings.value("height", 210, type=int))
        screen = QApplication.primaryScreen().availableGeometry()
        pos = self.settings.value("position", QPoint(screen.right() - self.width() - 30,
                                                     screen.bottom() - self.height() - 30))
        self.move(pos)
        self.clamp_position()
        self._maybe_snap_desk()  # 启动时按位置决定站姿/办公桌
        self.timer = QTimer(self)
        self.timer.timeout.connect(self._tick)
        self.timer.setTimerType(Qt.PreciseTimer)
        self.timer.start(16)
        if self._music_mode != "off":
            self._start_listener()
        if self._watch_apps:
            self._start_watcher()

    def _start_watcher(self):
        if self._watcher is not None:
            return
        from applaunch import AppWatcher
        self._watcher = AppWatcher(self)
        self._watcher.launched.connect(self._on_app_launch)
        self._watcher.site_opened.connect(self._on_site_open)
        self._watcher.page_opened.connect(self._on_page_open)
        self._watcher.start()

    def _stop_watcher(self):
        if self._watcher is not None:
            self._watcher.stop()
            self._watcher.wait(3000)
            self._watcher = None

    def toggle_watch_apps(self, enabled):
        self._watch_apps = enabled
        self.settings.setValue("watch_apps", enabled)
        if enabled:
            self._start_watcher()
        else:
            self._stop_watcher()

    def _on_app_launch(self, path):
        """侦测到新应用窗口: 提取它的 exe 图标并举起来。"""
        try:
            from icons import extract_icon_png, app_display_name
            png = extract_icon_png(path, size=64)
            pix = QPixmap(png) if png else QPixmap()
            name = app_display_name(path)
        except Exception as exc:
            print("launch icon failed:", path, exc)
            return
        if pix.isNull():
            return
        from launchlog import record_launch
        record_launch("app", path, name)
        self._start_launch(pix, name)

    def _on_site_open(self, site_id):
        """浏览器打开了常用网站: 举它的 favicon(图标已在后台线程下好)。"""
        try:
            from icons import fetch_site_icon, site_display_name
            png = fetch_site_icon(site_id)
            pix = QPixmap(png) if png else QPixmap()
            name = site_display_name(site_id)
        except Exception as exc:
            print("site icon failed:", site_id, exc)
            return
        if pix.isNull():
            return
        from launchlog import record_launch
        record_launch("site", site_id, name)
        self._start_launch(pix, name)

    def _on_page_open(self, key, name, png):
        """白名单外的网页: 图标(历史库 favicon)与站名都已在后台线程备好。"""
        pix = QPixmap(png) if png else QPixmap()
        if pix.isNull():
            return
        from launchlog import record_launch
        record_launch("site", key, name)
        self._start_launch(pix, name)

    def _start_launch(self, pix, name=""):
        now = time.monotonic() - self._epoch
        self._launch_pix = pix
        self._launch_at = now
        if name:
            self._reaction = "%s，启动！" % name
            self._reaction_until = now + self._launch_hold
            self._bubble_pop = now
        if self._animate:
            if self.character == "sunflower":
                self.player.play("sflaunch", now)
                self.player.hold_frame = SUNFLOWER_HOLD_FRAME
            else:
                self.player.play("launch", now)
                self.player.hold_frame = 26
            self.player.hold_kind = "launch"
            self.player.hold_next = None
            # 定格覆盖到图标淡出结束, 手不提前放下
            self.player.hold_until = now + self._launch_hold + .45

    def _palm_xy(self, r):
        """向日葵只以底部原有的两片叶子承托，图标底边贴叶面。"""
        if self.character == "sunflower":
            return r.x() + r.width() * SUNFLOWER_PALM[0], r.y() + r.height() * SUNFLOWER_PALM[1]
        return r.x() + r.width() * .19, r.y() + r.height() * .07

    def show_trophy(self):
        """「快端上来罢」: 把启动记录里的图标去重后叠成一塔捧出来。"""
        from launchlog import recent_unique
        pixmaps = []
        for entry in recent_unique(8):
            try:
                if entry["kind"] == "site":
                    from icons import fetch_site_icon
                    png = fetch_site_icon(entry["id"])
                else:
                    from icons import extract_icon_png
                    if not os.path.isfile(entry["id"]):
                        continue
                    png = extract_icon_png(entry["id"], size=64)
                pix = QPixmap(png) if png else QPixmap()
            except Exception:
                continue
            if not pix.isNull():
                pixmaps.append(pix)
        now = time.monotonic() - self._epoch
        if not pixmaps:
            self.react("pet", "还没有启动记录…")
            return
        # 最新的放塔尖, 塔底是最久的那层
        self._trophy = list(reversed(pixmaps))
        self._trophy_at = now
        self._launch_pix = None       # 与举图标 overlay 互斥
        self._reaction = "快端上来罢！"
        self._reaction_until = now + 2.6
        self._bubble_pop = now
        if self._animate:
            if self.character == "sunflower":
                self.player.play("sflaunch", now)
                self.player.hold_frame = SUNFLOWER_HOLD_FRAME
            else:
                self.player.play("launch", now)
                self.player.hold_frame = 26
            self.player.hold_kind = "launch"
            self.player.hold_next = None
            self.player.hold_until = now + 4.2

    def _start_listener(self):
        if self._listener is not None:
            return
        from music import BeatListener
        self._listener = BeatListener(self)
        self._listener.onset.connect(self._on_beat)
        self._listener.voice.connect(self._on_voice)
        self._listener.quiet.connect(self._on_quiet)
        self._listener.start()

    def _stop_listener(self):
        if self._listener is not None:
            self._listener.stop()
            self._listener = None
        self._music_on = False
        self.player.rocking = False
        self.player.talking = False
        self.player.opera = False
        self._opera_note_at = -1.0

    def _on_beat(self, strength, period):
        now = time.monotonic() - self._epoch
        self._beat_strength = strength
        self._beat_dir *= -1            # 左右交替摇摆
        self._music_on = True
        if self.player.dozing:          # 音乐响起自然醒, 不出声
            self.player.dozing = False
            self._doze_until = -10.0
        if period > 0:
            self._beat_period = period
        mode = self._music_mode
        if mode in ("rock", "metronome") and self._animate and self._drag_pos is None:
            # 摇摆用视频抽帧循环(正弦相位); 锚点提前半拍, 正弦极值=倾角最深正好卡在拍点
            if not self.player.rocking:
                self.player.rocking = True
                self.player.rock_anchor = now - (period if period > 0 else .5) / 2
            if period > 0:
                self.player.rock_period = period
            self.player.rock_cap = 1 if mode == "metronome" else 3
            self._beat_at = now
            if mode == "rock" and strength >= .65:
                # 摇滚甩头: 低频重拍(鼓)给一次左右交替的甩头冲量, 拍间自行衰减
                self._target_tilt = 9.0 * self._beat_dir
        else:
            self._beat_at = now
        # 节拍催生音符(仅节拍器/甩头): 美声模式的音符由乐句结束释放, 跟唱不出音符
        if self._music_mode in ("rock", "metronome") \
                and now - self._last_note > .22 and len(self._notes) < 8:
            self._last_note = now
            side = self._beat_dir
            self._notes.append([
                now,
                self.width() / 2 + side * (self.width() - 16 - 2 * SPRITE_PAD_X) * .30,   # 出生在她耳侧
                min(40 + (self._full_h - 44) * .28, self.height() * .55),  # 头部高度(办公桌模式压低)
                side * (4 + 5 * strength),                      # 向外漂移
                "♫" if strength > .55 else "♪",
                38 + round(18 * strength),
                34.0])

    def _on_voice(self, active, fast, slow):
        """语音活动: 跟唱说话模式驱动张嘴帧; 美声模式记录响度做长音缓动。"""
        self._voice_level = fast
        self._voice_slow = slow
        self._voice_at = time.monotonic() - self._epoch
        if self._music_mode == "singalong":
            self.player.talking = active
        elif self._music_mode == "opera":
            if active and not self.player.opera:
                # 新乐句开始: 换边缓动, 嘴边开始蓄一个音符
                self._opera_anchor = self._now
                self._opera_side *= -1
                self._opera_note_at = self._now
            elif not active and self.player.opera:
                # 长音结束: 把蓄着的那个音符放走
                self._release_opera_note()
            self.player.opera = active

    def _release_opera_note(self):
        """乐句结束: 嘴边蓄着的音符化作粒子升走, 越长的乐句音符越大。"""
        if self._opera_note_at < 0:
            return
        dur = max(0.2, self._now - self._opera_note_at)
        if len(self._notes) < 8:
            self._notes.append([
                self._now,
                self.width() / 2 + self._opera_side * (self.width() - 16 - 2 * SPRITE_PAD_X) * .10,
                min(40 + (self._full_h - 44) * .26, self.height() * .42),
                self._opera_side * (4 + 4 * min(1.0, dur)),
                "♫" if dur >= 1.0 else "♪",
                min(46.0, 26.0 + dur * 10.0),
                30.0])
        self._opera_note_at = -1.0

    def _on_quiet(self):
        self._music_on = False
        self.player.rocking = False
        self.player.talking = False         # 无声立刻闭嘴待机
        self.player.opera = False
        self._release_opera_note()          # 还蓄着的音符就地放走
        if self._drag_pos is None:
            self._target_tilt = 0.0

    def set_music_mode(self, mode):
        if mode not in MUSIC_MODES:
            return
        self._music_mode = mode
        self.settings.setValue("music_mode", mode)
        self.player.rocking = False
        self.player.talking = False
        self.player.opera = False
        self._opera_note_at = -1.0
        if mode == "off":
            self._stop_listener()
            self.react("pet", "好，安静待命")
        else:
            self._start_listener()
            self.react("pet", "%s，开唱！" % MUSIC_MENU[mode].split(" ·")[0])

    def closeEvent(self, event):
        self._stop_listener()
        self._stop_watcher()
        super().closeEvent(event)

    def set_size(self, height):
        """height 是期望的精灵身高。窗宽按当前角色首帧内容的宽高比算,
        向日葵这类方正形象不会再被 0.8 的固定窗宽压扁成小个子;
        左右各留 SPRITE_PAD_X 透明安全区, 外飘元素不被窗口边缘裁掉。"""
        height = max(180, min(320, height))
        self._full_h = height + 56
        sprite_h = self._full_h - 44   # 排版区: 顶部留 40 给气泡, 脚底贴窗口底
        aspect = max(0.4, getattr(self.player, "sprite_aspect", 0.67))
        width = int(height * aspect) + 28 + 2 * SPRITE_PAD_X
        if self._desk and len(self.player.frames) > 22:
            # 办公桌帧(笔记本烤进图里): 整帧露出,
            # 乘 DESK_FIT 让她的体型和站立模式一致
            fr = self.player.frames[20]
            ratio = fr.height() / fr.width()
            visible = 40 + int((width - 16 - 2 * SPRITE_PAD_X) * DESK_FIT * ratio) + 2
        elif self._desk:
            visible = 40 + int(sprite_h * .52)
        else:
            visible = self._full_h
        # 保持精灵中心与脚底屏幕位置不动
        old_bottom = self.y() + self.height()
        old_cx = self.x() + self.width() / 2
        self.setFixedSize(width, visible)
        self.move(round(old_cx - width / 2), old_bottom - visible)
        self.settings.setValue("height", height)
        self.update()

    def clamp_position(self):
        screen = QApplication.screenAt(self.frameGeometry().center()) or QApplication.primaryScreen()
        area = screen.availableGeometry()
        self.move(max(area.left(), min(self.x(), area.right() - self.width() + 1)),
                  max(area.top(), min(self.y(), area.bottom() - self.height() + 1)))

    def _maybe_snap_desk(self):
        """位置驱动模式切换: 贴近底栏吸附进办公桌, 拖离则切回全身(带滞回防抖)。"""
        if self.character != "owl":
            return  # 向日葵没有办公桌帧, 始终站姿
        screen = QApplication.screenAt(self.frameGeometry().center()) or QApplication.primaryScreen()
        dist = screen.availableGeometry().bottom() - (self.y() + self.height())
        if self._desk:
            if dist > 52:
                self.toggle_desk(False)
        elif abs(dist) <= 36:
            self.toggle_desk(True)

    def toggle_desk(self, enabled):
        if enabled and self.character != "owl":
            self.react("pet", "向日葵不去办公桌哦")
            return
        bottom = self.y() + self.height()
        self._desk = enabled
        self.settings.setValue("desk", enabled)
        self.set_size(self.settings.value("height", 210, type=int))
        if enabled:
            screen = QApplication.screenAt(self.frameGeometry().center()) or QApplication.primaryScreen()
            area = screen.availableGeometry()
            self.move(self.x(), area.bottom() - self.height() + 1)  # 底栏当办公桌
        else:
            self.move(self.x(), bottom - self.height())
        self.clamp_position()
        self.settings.setValue("position", self.pos())
        self.update()

    def _tick(self):
        if self.isVisible():
            self._now = time.monotonic() - self._epoch
            delta = min(.05, self._now - self._last_tick)
            self._last_tick = self._now
            self._phase = self._now * 2
            self._shake_marks = [t for t in self._shake_marks
                                 if time.monotonic() - t <= SHAKE_WIN]
            self._shake = float(len(self._shake_marks))
            self._tilt += (self._target_tilt - self._tilt) * (1 - math.exp(-14 * delta))
            if self._now < self._dizzy_until and self._drag_pos is None:
                # 眩晕余韵: 脚底锚点不变, 上身快速摆动并衰减
                decay = (self._dizzy_until - self._now) / 2.8
                self._target_tilt = math.sin(self._now * 9.5) * 7 * decay
            self._track_cursor(delta)
            if self._now < self._train_until:
                self._scan_training()
                self._tick_pens(delta)
            elif self._train_until > 0:
                self._finish_training()
            if self._animate and self._drag_pos is None and self._now >= self._dizzy_until:
                if self._music_mode == "opera":
                    # 美声跟唱: 以乐句(约两拍)为单位的正弦缓动, 幅度随人声响度呼吸
                    amp = max(0.0, min(1.0, self._voice_slow * 6.0))
                    half = max(1.2, self._beat_period * 2.0)
                    glide = math.sin(math.pi * min(1.0, (self._now - self._opera_anchor) / half))
                    self._target_tilt = 10.0 * amp * glide * self._opera_side
                elif self._music_mode == "rock" and self.player.rocking:
                    # 甩头冲量拍间自然回落, 不然一直歪着
                    self._target_tilt *= math.exp(-1.4 * delta)
            if self._train_demo_until > 0 and self._now >= self._train_demo_until:
                self._train_demo_until = -1.0
                if not self._music_on:
                    self.player.rocking = False
            self._idle_quirks()
            if self._doze_until > self._now and self._animate:
                self._doze_zzz()
            if self._desk and self._animate and self._now >= self._train_until:
                self._scan_typing()
            self._adapt_fps()
            self.update()

    def _track_cursor(self, delta):
        """光标跟踪: 靠得近时身体微微转向光标方向, 走远了慢慢回正。"""
        if not self._animate or self._drag_pos is not None or self._now < self._dizzy_until:
            return
        local = self.mapFromGlobal(QCursor.pos())
        dx = local.x() - self.width() / 2
        dy = local.y() - self.height() * .42
        near = math.hypot(dx, dy) < 170
        gx = max(-1.0, min(1.0, dx / 170.0))
        gy = max(-1.0, min(1.0, dy / 170.0))
        ease = 1 - math.exp(-9 * delta)
        self._gaze_x += ((gx * 3.0 if near else 0.0) - self._gaze_x) * ease
        self._gaze_y += ((gy * 2.2 if near else 0.0) - self._gaze_y) * ease
        if not self.player.rocking and not self.player.opera:
            if near and not self.player.dozing:
                self._target_tilt = max(-4.0, min(4.0, gx * 4.0))
            else:
                self._target_tilt += (0.0 - self._target_tilt) * ease

    def _idle_quirks(self):
        """自主待机: 无人理她时随机伸懒腰/抖耳朵, 太久没互动就打盹冒 Zzz。"""
        if self._doze_until > 0 and self._now >= self._doze_until:
            self._doze_until = -10.0          # 睡够自己醒, 不出声
            self.player.dozing = False
        if not (self._animate and self._drag_pos is None and not self._hover
                and self._now > self._reaction_until
                and self.player.clip == "idle" and self.player.hold_frame is None
                and not self.player.rocking and not self._music_on
                and self._now >= self._dizzy_until
                and self._doze_until <= self._now
                and self._now >= self._next_quirk):
            return
        idle_for = self._now - self._last_interaction
        roll = random.random()
        if idle_for > 240 and roll < .45:
            self._doze_until = self._now + 45 + random.random() * 40
            self.player.dozing = True
            self._next_z = self._now + .8
        elif roll < .60:
            self.player.play("stretch", self._now)
        else:
            self.player.play("twitch", self._now)
        self._next_quirk = self._now + 35 + random.random() * 45

    def _doze_zzz(self):
        """打盹时从头顶慢慢冒 Z 粒子。"""
        if self._now >= self._next_z and len(self._notes) < 8:
            self._next_z = self._now + 1.1 + random.random() * .5
            self._notes.append([
                self._now,
                self.width() / 2 + (self.width() - 16 - 2 * SPRITE_PAD_X) * .12 + random.random() * 8,
                min(40 + (self._full_h - 44) * .26, self.height() * .42),
                5 + random.random() * 4,          # 向右缓漂
                "Z",
                15 + round(random.random() * 7),
                15.0])                            # 上升速度(px/s), 比音符慢

    def _wake(self, text="嗯?我在!"):
        if self._doze_until > 0 and self._now < self._doze_until:
            self._doze_until = -10.0
            self.player.dozing = False
            self.react("pet", text)

    def start_training(self):
        """10 秒节奏采集: 八字双铅笔跟着鼠标/键盘敲击起落, 长按伏地。"""
        self._train_until = self._now + 10.0
        self._train_taps = []
        self._train_held = {}
        self._pen_press = [0.0, 0.0]
        self._pen_kick = [0.0, 0.0]
        self._pen_side = 0
        self._last_interaction = self._now
        self._wake("来,跟着我敲!")
        self.react("pet", "鼠标键盘都行, 敲 10 秒!")

    def _scan_training(self):
        """任意键/鼠标按下沿 = 一敲(双笔交替); 按着的键让笔保持伏地。"""
        get = ctypes.windll.user32.GetAsyncKeyState
        now = self._now
        for vk in range(1, 255):
            state = get(vk)
            if state & 0x0001 and vk not in self._train_held:
                self._train_held[vk] = (now, self._pen_side)
                self._train_taps.append([now, None, vk])
                self._pen_side = 1 - self._pen_side
                self._pen_kick[self._pen_side] = 1.0
                self._last_interaction = now
            elif vk in self._train_held and not (state & 0x8000):
                start, _side = self._train_held.pop(vk)
                for tap in reversed(self._train_taps):
                    if tap[2] == vk and tap[1] is None:
                        tap[1] = max(0.0, now - start)
                        break

    def _tick_pens(self, delta):
        get = ctypes.windll.user32.GetAsyncKeyState
        held_sides = set()
        for vk, (_start, side) in list(self._train_held.items()):
            if get(vk) & 0x8000:
                held_sides.add(side)
        ease = 1 - math.exp(-16 * delta)
        for i in (0, 1):
            target = 1.0 if i in held_sides else 0.0
            self._pen_press[i] += (target - self._pen_press[i]) * ease
            self._pen_kick[i] *= math.exp(-9 * delta)

    def _finish_training(self):
        end = self._train_until
        taps = self._train_taps
        self._train_until = -1.0
        self._train_taps = []
        self._train_held = {}
        for t in taps:
            if t[1] is None:                    # 到点还按着的, 按 10s 线收尾
                t[1] = max(0.0, end - t[0])
        starts = [t[0] for t in taps]
        if len(starts) < 6:
            self.react("pet", "敲得太少啦, 再来一次?")
            return
        iois = [b - a for a, b in zip(starts, starts[1:]) if 0.08 <= b - a <= 3.0]
        if len(iois) < 5:
            self.react("pet", "节奏没抓准, 再来一次?")
            return
        period = statistics.median(iois)
        while period < 0.28:                    # 半速纠正, 折进可唱的区间
            period *= 2
        while period > 1.30:
            period /= 2
        mean = statistics.fmean(iois)
        cv = statistics.pstdev(iois) / mean if mean > 0 else 1.0
        holds = sum(1 for t in taps if t[1] >= 0.30) / len(taps)
        # 风格适配: 长按多→美声长音; 稳定均匀→节拍器卡点; 快慢参差→甩头
        if holds >= 0.30:
            mode = "opera"
        elif cv <= 0.22:
            mode = "metronome"
        else:
            mode = "rock"
        self.set_music_mode(mode)
        self._beat_period = period
        self.player.rock_period = period
        if mode in ("rock", "metronome") and not self._music_on:
            # 学以致用: 按学到的节奏点头三下
            self.player.rocking = True
            self.player.rock_anchor = self._now - period / 2
            self.player.rock_cap = 3
            self._train_demo_until = self._now + period * 3
        bpm = round(60.0 / period)
        self.react("pet", "学会了!%s · %d BPM" % (MUSIC_MENU[mode].split(" ·")[0], bpm))

    def _adapt_fps(self):
        """静息降帧: 没有任何动画/交互时降到 ~8fps, 省下透明窗口的重绘开销。"""
        if not self._animate:
            return
        active = bool(
            self.player.clip != "idle" or self.player.hold_frame is not None
            or self.player.rocking or self._music_on or self._notes
            or self._launch_pix is not None or self._trophy
            or self._hover or self._drag_pos is not None
            or self._now < self._reaction_until or self._now < self._dizzy_until
            or self._doze_until > self._now or self._desk
            or abs(self._gaze_x) > .05 or abs(self._gaze_y) > .05
            or abs(self._tilt) > .05 or self._shake > .02)
        interval = 16 if active else 120
        if interval != self.timer.interval():
            self.timer.setInterval(interval)

    def _scan_typing(self):
        """办公桌模式侦测主人击键。GetAsyncKeyState 低位=自上次查询后按下过,
        是按下沿事件: 按一个键她只按一下, 长按不连发。"""
        get = ctypes.windll.user32.GetAsyncKeyState
        for vk in range(8, 255):
            if get(vk) & 0x0001:
                self._paw = 2 if self._paw == 1 else 1  # 左右爪交替
                self._paw_until = self._now + .22
                self._last_interaction = self._now
                self._wake("嗯,在干活!")
                return

    def _typing(self):
        return self._now < self._paw_until

    def react(self, clip, text):
        self._now = time.monotonic() - self._epoch
        self.player.play(clip, self._now)
        self.player.hold_frame = None      # 新剪辑接管, 清掉悬停定格
        self.player.hold_next = None
        self.player.hold_kind = None
        self._reaction = text
        self._reaction_until = self._now + 2.2
        self._bubble_pop = self._now

    def _greet_clip(self):
        """打招呼: 一半概率迎视, 一半概率招手(图集 17 号招手帧)。"""
        return "wave" if random.random() < .5 else "greet"

    def say_hi(self):
        """菜单触发: 打完整招呼——上半程播完定格半拍, 再自动播收尾。"""
        from animation import CLIPS
        clip = self._greet_clip()
        self.react(clip, self.revive_text)
        self.player.hold_frame = 6 if clip == "greet" else 17
        self.player.hold_kind = clip
        self.player.hold_next = "greet_out" if clip == "greet" else "wave_out"
        # 定格从上半程播完才开始计时, 而不是从动画开始
        self.player.hold_until = self._now + sum(d for _i, d in CLIPS[clip]) + 0.55

    def enterEvent(self, event):
        self._hover = True
        now = time.monotonic() - self._epoch
        self._last_interaction = now
        self._hover_since = now
        self._bubble_pop = now
        if self.player.dozing:
            self._wake()
        else:
            clip = self._greet_clip()
            self.react(clip, self.revive_text)
            # 悬停保持: greet 定格在握拳托下巴, wave 定格在举手, 离开才播下半程
            self.player.hold_frame = 6 if clip == "greet" else 17
            self.player.hold_until = now + 9999.0
            self.player.hold_kind = clip
        self.update()

    def leaveEvent(self, event):
        self._hover = False
        kind = self.player.hold_kind
        if self.player.hold_frame is not None and kind in ("greet", "wave"):
            out = "greet_out" if kind == "greet" else "wave_out"
            self.player.hold_frame = None
            self.player.hold_kind = None
            self.player.hold_next = None
            self.player.play(out, time.monotonic() - self._epoch)
        self.update()

    def _icon(self, pid, gray=False):
        key = (pid, gray)
        if key not in self._icons:
            path = os.path.join(os.path.dirname(ASSET), "icons", pid + ".png")
            pix = QPixmap(path) if os.path.exists(path) else QPixmap()
            if gray and not pix.isNull():
                img = pix.toImage().convertToFormat(QImage.Format_ARGB32)
                for y in range(img.height()):
                    for x in range(img.width()):
                        c = img.pixelColor(x, y)
                        g = round(c.red() * .299 + c.green() * .587 + c.blue() * .114)
                        img.setPixelColor(x, y, QColor(g, g, g, c.alpha()))
                pix = QPixmap.fromImage(img)
            self._icons[key] = pix
        return self._icons[key]

    @staticmethod
    def _orbit_text(data):
        """光环数字: 有重置窗口的给复活时刻(超出今明两天带日期), 其余给剩余比例/数量。
        返回 (文字, 是否已耗尽); 没有可用数据的返回 None(由调用方灰显占位)。"""
        if not data.get("ok"):
            return None
        wins = [w for w in (data.get("windows") or [])
                if w.get("resets_at") and w["resets_at"] > time.time()]
        if wins:
            soonest = min(w["resets_at"] for w in wins)
            drained = min(w.get("remaining_percent", 100) for w in
                          (data.get("windows") or [])) <= 0.5
            if soonest - time.time() < 48 * 3600:
                return time.strftime("%H:%M", time.localtime(soonest)), drained
            tm = time.localtime(soonest)
            return "%d/%d %02d:%02d" % (tm.tm_mon, tm.tm_mday, tm.tm_hour, tm.tm_min), drained
        if data.get("total") and data.get("remaining") is not None:
            pct = round(data["remaining"] / data["total"] * 100)
            return "%d%%" % pct, pct <= 0
        if data.get("remaining") is not None:
            v = float(data["remaining"])
            text = "%d" % v if v.is_integer() else "%.1f" % v
            return (("¥" + text) if data.get("unit") == "¥" else text), v <= 0
        return None

    def set_status(self, results, configs):
        ratios = []
        self.stale = any(d.get("stale") for d in results.values())
        orbit = []
        soonest = None
        soonest_name = ""
        for cfg in configs:
            data = results.get(cfg["id"], {})
            entry = self._orbit_text(data)
            if entry is None:
                # 同步失败/未连接/没数据的也进光环, 半透明灰色占位
                entry = ("--", True)
            orbit.append((cfg["id"], entry[0], entry[1]))
            if cfg.get("type") == "manual":
                continue
            if data.get("ok") and not data.get("stale") and data.get("total", 0) and data.get("remaining") is not None:
                ratios.append((max(0, min(100, data["remaining"] / data["total"] * 100)), cfg["name"]))
            for window in data.get("windows") or []:
                ts = window.get("resets_at")
                if ts and ts > time.time() and window.get("remaining_percent", 100) < 100:
                    if soonest is None or ts < soonest:
                        soonest, soonest_name = ts, cfg["name"]
        self.orbit = orbit[:8]
        self.pct = min(ratios)[0] if ratios else None
        self.alert = self.pct is not None and self.pct < 30
        lowest = min(ratios) if ratios else None
        if soonest is not None:
            stamp = time.strftime("%H:%M", time.localtime(soonest))
            self.revive_text = "下次复活 %s %s" % (soonest_name, stamp)
        elif lowest:
            self.revive_text = "余量最低 %s %.0f%%" % (lowest[1], lowest[0])
        else:
            self.revive_text = "点我查看用量"
        if self.alert and lowest:
            self._alert_text = "告急 %s 剩 %.0f%%" % (lowest[1], lowest[0])
            if soonest is not None:
                self._alert_text += " · %s 复活" % time.strftime("%H:%M", time.localtime(soonest))
        self.message = self.revive_text
        self.busy = False
        self.update()

    def _bubble_text(self):
        if self.busy:
            return "正在同步…"
        if self._now < self._reaction_until and self._reaction:
            text = self._reaction
        elif self._hover:
            text = self.revive_text
        else:
            return ""
        if self._desk and len(text) > 12:
            # 办公桌模式窗口矮、气泡不能超宽: 只留最要紧的
            text = text.replace("下次复活 ", "").replace("告急 ", "!")
        return text

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHints(QPainter.Antialiasing | QPainter.SmoothPixmapTransform)
        elapsed = self._now - self._bounce_at
        bounce = math.sin(elapsed * 18) * math.exp(-elapsed * 5) * 10 if 0 <= elapsed < 1.2 and self._animate else 0
        # 节拍下沉: 节拍器用相位锁定的预测拍点(无状态高斯, 不受 onset 延迟抖动);
        # 摇滚/默认沿用 onset 触发的高斯脉冲, 拍前 150ms 微提预备
        rock = 0.0
        dip = 0.0            # 节拍压扁量 0-1(触地瞬间身体微压扁)
        sway_shear = 0.0     # 摇摆斜拉系数(pet 侧 SAI 变形, 替代逐帧摇摆)
        if self._animate and self._music_on:
            if (self._music_mode in ("metronome", "rock") and self.player.rocking
                    and self.player.rock_period > 0):
                period = self.player.rock_period
                s = math.sin(math.pi * (self._now - self.player.rock_anchor)
                             / max(.2, period))
                sway_shear = (0.13 if self._music_mode == "rock" else 0.075) * s
                if self._music_mode == "metronome":
                    phase = ((self._now - self.player.rock_anchor) / period) % 1.0
                    dist = min(phase, 1.0 - phase) * period      # 距最近预测拍点
                    dip = math.exp(-((dist / .05) ** 2))
                    rock -= (1.1 + 2.0 * min(1.0, self._beat_strength)) * dip
                else:
                    since_beat = self._now - self._beat_at
                    if since_beat >= 0:
                        dip = math.exp(-((since_beat / .09) ** 2))
                        rock -= (2.0 + 4.0 * self._beat_strength) * dip
                        if self._beat_period > 0:
                            to_next = self._beat_at + self._beat_period - self._now
                            if 0 < to_next < .15:
                                rock += 1.6 * self._beat_strength * (1 - to_next / .15)
            else:
                since_beat = self._now - self._beat_at
                if since_beat >= 0:
                    dip = math.exp(-((since_beat / .09) ** 2))
                    rock -= (2.0 + 4.0 * self._beat_strength) * dip
                    if self._beat_period > 0:
                        to_next = self._beat_at + self._beat_period - self._now
                        if 0 < to_next < .15:
                            rock += 1.6 * self._beat_strength * (1 - to_next / .15)
        # 跟唱说话: 张嘴帧交替时身体随语声小幅颠动
        bob = math.sin(self._now * 12.6) * 1.3 \
            if (self._animate and self.player.talking) else 0.0
        # 精灵脚底贴窗口底边(留 4px), 不做 idle 上下浮动, 站着就有落地感;
        # 办公桌模式用带笔记本的整帧时, 排版区收缩到窗口高度且底边齐平,
        # 笔记本底座直接坐在底栏上(底栏=桌面)
        desk_baked = self._desk and len(self.player.frames) > 22
        rect_h = (self.height() + 4 if desk_baked else self._full_h) - 44
        # 叠叠乐把窗口向上加高过时, 排版区整体下移, 她始终贴窗口底(屏幕位置不动)
        rect = QRectF(8 + SPRITE_PAD_X, 40 + self._trophy_pad,
                      self.width() - 16 - 2 * SPRITE_PAD_X, rect_h)
        halo_active = self._hover and self.orbit
        halo = []
        if halo_active:
            n = len(self.orbit)
            cx = self.width() / 2
            # 办公桌模式窗口矮: 半径收小、扫角放宽, 圆弧更圆才能排开
            sweep = 170 if self._desk else 140
            radius = min(self.width() * .42, 56) if self._desk else min(self.width() * .46, 74)
            cy = rect.top() + 46 if self._desk else rect.top() + rect.height() * .30
            for i, (pid, text, drained) in enumerate(self.orbit):
                angle = math.radians(90 + (i - (n - 1) / 2) * (sweep / (n - 1)) if n > 1 else 90)
                t = min(1, max(0, (self._now - self._hover_since - i * .06) / .25))
                if t <= 0:
                    continue
                scale = ease_out_back(t)
                x, y = cx + radius * math.cos(angle), cy - radius * math.sin(angle)
                x = min(max(14, x), self.width() - 14)  # 图标不飞出窗口左右边缘
                pix = self._icon(pid, gray=drained)
                if not pix.isNull():
                    size = (22 if self._desk else 26) * scale
                    p.save()
                    p.setOpacity(min(1, t * 1.4) * (.55 if drained else 1))
                    p.drawPixmap(QRectF(x - size / 2, y - size / 2, size, size).toRect(), pix)
                    p.restore()
                halo.append((x, y - (14 if self._desk else 16), text, t, drained))
        if not self.sprite.isNull():
            p.save()
            # SAI 式自由变形: 斜拉(顶边平移、底边钉死在地平线)+ 轻微压缩/拉长,
            # 不再做绕脚底的刚体旋转; 摇摆/眩晕/拖拽斜倾/美声缓动全走同一套变形
            feet_x, feet_y = rect.center().x(), rect.bottom()
            breath = math.sin(self._phase) * .008 if self._animate else 0
            k = math.tan(math.radians(self._tilt if self._animate else 0.0)) + sway_shear
            k = max(-0.22, min(0.22, k))
            squash = min(1.0, dip * 0.7 + abs(k) * 0.45)
            t = QTransform()
            t.translate(feet_x, feet_y)
            t.shear(k, 0.0)
            t.scale((1 + 0.05 * squash) * (1 - breath * .5),
                    (1 - 0.07 * squash) * (1 + breath))
            t.translate(-feet_x, -feet_y)
            p.setTransform(t)
            # 举图标时的身体起伏: 蓄势微蹲 -> 上托回弹 -> 定格微浮
            lift = 0.0
            if self._launch_pix is not None and self._animate:
                lt = self._now - self._launch_at
                if 0 <= lt < self._launch_hold + .45:
                    if lt < .09:
                        lift = -3.0 * (lt / .09)            # 蓄势下蹲
                    else:
                        t2 = lt - .09
                        lift = 5.0 * ease_out_back(min(1.0, t2 / .25))  # 上托
                        if t2 > .25:
                            lift = 2.0 + 1.2 * math.sin(t2 * 3)         # 定格微浮
            draw_rect = QRectF(rect)
            # 光标跟踪: 整体朝光标方向偏几像素, 看起来在"转向你"; 跟唱时叠加颠动
            draw_rect.translate(self._gaze_x,
                                self._gaze_y - (bounce + rock + lift) + bob)
            index = self.player.sample(self._now, self._hover, self._animate, self._desk,
                                       paw=self._paw if self._typing() else 0,
                                       dizzy=self._now < self._dizzy_until)
            if desk_baked and index in (20, 21, 22, 24):
                # 办公桌帧按 DESK_FIT 收一档, 头宽与站立模式对齐
                dw = draw_rect.width() * DESK_FIT
                draw_rect.setX(draw_rect.center().x() - dw / 2)
                draw_rect.setWidth(dw)
            self.player.draw(p, draw_rect, index, self.sprite, now=self._now)
            # 记录头顶位置(含斜拉偏移), 气泡与光环据此贴合当前角色身高
            head_rect = self.player.drawn_rect(draw_rect, index, self.sprite)
            self._head_top = head_rect.top()
            self._head_cx = head_rect.center().x() - k * (feet_y - head_rect.top())
            if self.player.character == "owl":
                self._launch_rect = self.player.drawn_rect(draw_rect, 26, self.sprite)
            elif len(self.player.frames) > SUNFLOWER_HOLD_FRAME:
                self._launch_rect = self.player.drawn_rect(draw_rect, SUNFLOWER_HOLD_FRAME, self.sprite)
            else:
                self._launch_rect = None
            p.restore()
        else:
            p.setPen(QColor("#304a52"))
            p.drawText(rect, Qt.AlignCenter, "Rockabuddy\n桌宠素材未找到")
        if self._desk and not desk_baked:
            self._draw_laptop(p, self._typing())
        if self._now < self._train_until:
            self._draw_pens(p, rect)
        if self._notes:
            # 音符/Zzz 粒子: 从耳侧(或头顶)升起, 漂移淡出; 强拍金色 ♫, 弱拍青色 ♪, 打盹灰绿 Z;
            # 白描边垫底, 在深色衣服上也能看清
            alive = []
            for note in self._notes:
                birth, x0, y0, drift, glyph, size, rise = note
                age = self._now - birth
                if age > 2.2 or age < 0:
                    continue
                alive.append(note)
                pop = ease_out_back(min(1.0, age / .22))
                x = x0 + drift * age + math.sin(age * 3.1 + birth * 7) * 6
                # pop up 出场: 从下方 22px 弹上来(回弹过冲会略超再回落), 尺寸同步过冲
                y = y0 - rise * age + 22 * (1 - pop)
                alpha = min(1.0, age * 6) * max(0.0, 1 - (age / 2.2) ** 1.6)
                px = max(6, round(size * pop))
                area = QRectF(x - 40, y - 40, 80, 80)
                p.save()
                p.setOpacity(alpha)
                p.setFont(QFont("Microsoft YaHei UI", px + 2, QFont.Bold))
                p.setPen(QColor(255, 255, 255))
                p.drawText(area, Qt.AlignCenter, glyph)
                p.setFont(QFont("Microsoft YaHei UI", px, QFont.Bold))
                p.setPen(QColor(232, 182, 76) if glyph == "♫"
                         else QColor(95, 174, 159) if glyph == "♪"
                         else QColor(154, 168, 164))
                p.drawText(area, Qt.AlignCenter, glyph)
                p.restore()
            self._notes = alive
        if self._music_mode == "opera" and self._opera_note_at >= 0 and self._animate:
            # 美声蓄音: 乐句进行中音符悬在嘴边, 越唱越大、随响度呼吸, 唱完才放走
            dur = max(0.0, self._now - self._opera_note_at)
            glyph = "♫" if dur >= 1.0 else "♪"
            size = min(46.0, 26.0 + dur * 10.0)
            pulse = 1.0 + .08 * math.sin(self._now * 9) \
                * min(1.0, self._voice_level * 5 + .3)
            hx = self.width() / 2 + self._opera_side * (self.width() - 16 - 2 * SPRITE_PAD_X) * .10
            hy = min(40 + (self._full_h - 44) * .26, self.height() * .42) + 14 \
                + 1.5 * math.sin(self._now * 3)
            area = QRectF(hx - size, hy - size, size * 2, size * 2)
            p.save()
            p.setFont(QFont("Microsoft YaHei UI", round(size * pulse), QFont.Bold))
            p.setPen(QColor(255, 255, 255))
            p.drawText(area, Qt.AlignCenter, glyph)
            p.setFont(QFont("Microsoft YaHei UI", round(size * pulse) - 2, QFont.Bold))
            p.setPen(QColor(232, 182, 76) if glyph == "♫" else QColor(95, 174, 159))
            p.drawText(area, Qt.AlignCenter, glyph)
            p.restore()
        if self._launch_pix is not None:
            # 举图标 overlay: 蓄势期图标不露头, 托掌瞬间从掌心啵出
            # (回弹放大+转正+星星迸溅), 定格时随呼吸微浮, 末尾上浮淡出
            lt = self._now - self._launch_at
            total = self._launch_hold + .45
            if lt >= total or lt < 0:
                self._launch_pix = None
            else:
                if self._launch_rect is not None:
                    px, py = self._palm_xy(self._launch_rect)
                else:
                    px, py = self.width() * .30, rect.top() + 10
                base = 32 if self._desk else 42
                # 托举姿势就位后弹图标，向日葵仍在底部叶面承托。
                t2 = lt - (SUNFLOWER_LIFT_SECONDS if self.character == "sunflower" else .34)
                if t2 >= 0:
                    pop = ease_out_back(t2 / .22)
                    rot = -25 * (1 - min(1.0, t2 / .22))
                    rise = 10 * (1 - min(1.0, t2 / .22))
                    bob = 1.5 * math.sin(t2 * 3) if t2 > .30 else 0.0
                    fade = min(1.0, max(0.0, (total - lt) / .4))
                    if lt > total - .4:          # 收尾上浮
                        bob -= (lt - (total - .4)) / .4 * 8
                    size = base * max(.01, pop)
                    x = min(max(size / 2 + 2, px), self.width() - size / 2 - 2)
                    y = min(max(size / 2 + 2, py - size * .45 + rise + bob),
                            self.height() - size / 2 - 2)
                    p.save()
                    p.setOpacity(min(1.0, t2 * 6) * fade)
                    p.translate(x, y)
                    p.rotate(rot)
                    p.drawPixmap(QRectF(-size / 2, -size / 2, size, size).toRect(),
                                 self._launch_pix)
                    p.restore()
                    # 弹出瞬间的星星迸溅
                    if 0 < t2 < .38:
                        st = t2 / .38
                        p.setPen(Qt.NoPen)
                        for ang in (-125, -90, -55, -20, -155):
                            d = 6 + 26 * st
                            sx = min(max(4.0, px + d * math.cos(math.radians(ang))),
                                     self.width() - 4.0)
                            sy = min(max(4.0, py + d * math.sin(math.radians(ang)) * .8),
                                     self.height() - 4.0)
                            sr = 3.2 * (1 - st) + .6
                            p.setBrush(QColor(255, 214, 90, int(255 * (1 - st))))
                            p.drawEllipse(QPointF(sx, sy), sr, sr)
        if self._trophy:
            # 叠叠乐: 图标从下到上逐层啵出, 每层带固定小偏移和倾角,
            # 整塔随时间微微摇摆(越高层越晃), 收尾整体淡出
            lt = self._now - self._trophy_at
            total = 4.2
            if lt < 0 or lt >= total:
                self._trophy = []
                self._restore_top()
            else:
                n = len(self._trophy)
                size = 34.0                       # 固定大尺寸, 不随层数缩水
                pitch = size * .80
                # 塔底那层钉在托举帧的掌心(与举图标 overlay 同一锚点),
                # 向上空间不限: 塔顶要出画就把窗口向上加高, 绝不下移脱手
                if self._launch_rect is not None:
                    cx, cy = self._palm_xy(self._launch_rect)
                    y_base = cy - size * .45
                else:
                    cx = self.width() * .30
                    y_base = rect.top() + 26
                cx = min(max(size / 2 + 2, cx), self.width() - size / 2 - 2)
                top = y_base - (n - 1) * pitch - size / 2
                if top < 0:
                    self._grow_top(-top)
                sway = math.sin(lt * 2.2) * 1.8 if lt > .3 else 0.0
                fade = min(1.0, max(0.0, (total - lt) / .4))
                for i, pix in enumerate(self._trophy):
                    ready = SUNFLOWER_LIFT_SECONDS if self.character == "sunflower" else .55
                    it = (lt - ready - i * .13) / .22   # 等托掌帧就位后再逐层啵出
                    if it <= 0:
                        break  # 这层还没啵出来, 上面的层更不到时候
                    pop = ease_out_back(min(1.0, it))
                    drop = 14 * (1 - min(1.0, it))        # 从上方落进塔里
                    jitter = ((i * 37) % 11 - 5) * .8     # 每层固定的横向小偏移
                    rot = ((i * 53) % 17 - 8) * .6        # 每层固定的小倾角
                    x = cx + jitter
                    y = y_base - i * pitch - drop
                    p.save()
                    p.setOpacity(min(1.0, it * 5) * fade)
                    p.translate(x, y)
                    p.rotate(rot + sway * (i + 1) / n)
                    s = size * max(.01, pop)
                    p.drawPixmap(QRectF(-s / 2, -s / 2, s, s).toRect(), pix)
                    p.restore()
        bubble = "" if halo_active else self._bubble_text()
        if bubble:
            # pop 对话气泡: 悬在当前角色头顶(随身高/帧形变自适应), 斜尾巴指向头顶;
            # 框体与尾巴做路径并集, 交界没有描边隔断;
            # 举着图标/叠叠乐时气泡靠右站, 让出左上掌心区域不遮图标
            t = min(1, max(0, (self._now - self._bubble_pop) / .18))
            scale = ease_out_back(t) or 0.01
            holding = self._launch_pix is not None or bool(self._trophy)
            font = QFont("Microsoft YaHei UI", 8, QFont.Bold)
            bw = min(QFontMetrics(font).horizontalAdvance(bubble) + 28,
                     self.width() - 8)
            if holding:
                bx = max(self.width() * .40, self.width() - 4 - bw)
            else:
                bx = (self.width() - bw) / 2
            p.save()
            by = max(2.0, self._head_top - 36.0)
            tail_tip = QPointF(min(max(self._head_cx + self.width() * .16, 40.0),
                                   self.width() - 40.0), by + 26.0)
            p.translate(tail_tip)
            p.scale(scale, scale)
            p.translate(-tail_tip)
            base_x = min(max(tail_tip.x(), bx + 12), bx + bw - 12)  # 尾根收在框底内
            box = QPainterPath()
            box.addRoundedRect(QRectF(bx, by, bw, 28), 14, 14)
            spike = QPainterPath()
            spike.moveTo(base_x - 10, by + 26)
            spike.lineTo(base_x + 8, by + 26)
            spike.lineTo(tail_tip)
            spike.closeSubpath()
            p.setPen(QPen(QColor("#d9d9c9"), 1))
            p.setBrush(QColor("#141a1a") if self.alert else QColor("#304a52"))
            p.drawPath(box.united(spike))
            p.setPen(QColor("#f4f6f2"))
            p.setFont(font)
            p.drawText(QRectF(bx + 2, by, bw - 4, 28), Qt.AlignCenter, bubble)
            p.restore()
        placed = []  # 已放置的胶囊, 碰撞就往上一排让位, 互不重叠
        for x, y, text, t, drained in halo:
            if t < .4:
                continue
            w = max(30, 7 * len(text) + 10) if self._desk else max(34, 8 * len(text) + 12)
            x = min(max(w / 2 + 2, x), self.width() - w / 2 - 2)  # 胶囊整体收进窗口
            box = QRectF(x - w / 2, y - 14, w, 14)
            for _ in range(3):
                if not any(box.adjusted(-3, 0, 3, 0).intersects(o) for o in placed):
                    break
                box.translate(0, -15)
            if box.top() < 2:
                box.moveTop(2)
            placed.append(box)
            p.setPen(Qt.NoPen)
            p.setBrush(QColor(110, 118, 116, int(220 * min(1, t))) if drained
                       else QColor(20, 26, 26, int(220 * min(1, t))))
            p.drawRoundedRect(box, 7, 7)
            p.setPen(QColor("#dfe5e2") if drained else QColor("#f4f6f2"))
            p.setFont(QFont("Microsoft YaHei UI", 7, QFont.Bold))
            p.drawText(box, Qt.AlignCenter, text)

    def _draw_pens(self, p, rect):
        """训练模式: 八字双铅笔悬在脚前, 跟敲击交替敲下, 长按伏地, 上方倒计时。"""
        cx, base_y = rect.center().x(), rect.bottom() + 2
        L = min(92.0, self.width() * .40)
        pivot_y = base_y - L * math.cos(math.radians(6)) - 2
        for i, side in enumerate((-1, 1)):
            press = max(0.0, min(1.0, max(self._pen_press[i], self._pen_kick[i])))
            ang = math.radians(24 - 18 * press)
            p.save()
            p.translate(cx + side * 10, pivot_y)
            p.rotate(-side * math.degrees(ang))
            p.setPen(Qt.NoPen)
            p.setBrush(QColor("#e8a0a0"))                        # 橡皮
            p.drawRoundedRect(QRectF(-3.5, -4, 7, 7), 3, 3)
            p.setBrush(QColor("#b8c0c4"))                        # 金属箍
            p.drawRoundedRect(QRectF(-3.5, 2, 7, 5), 1.5, 1.5)
            p.setBrush(QColor("#e8c06a"))                        # 笔杆
            p.drawRoundedRect(QRectF(-3.5, 6, 7, L - 16), 2.5, 2.5)
            tip = QPainterPath()                                 # 笔尖
            tip.moveTo(-3.5, L - 10)
            tip.lineTo(3.5, L - 10)
            tip.lineTo(0, L + 2)
            tip.closeSubpath()
            p.setBrush(QColor("#4a4a4a"))
            p.drawPath(tip)
            p.restore()
        remain = max(0.0, (self._train_until - self._now) / 10.0)
        bw = rect.width() * .55
        bar_y = pivot_y - L - 10
        p.setPen(Qt.NoPen)
        p.setBrush(QColor("#e2e5dc"))
        p.drawRoundedRect(QRectF(cx - bw / 2, bar_y, bw, 4), 2, 2)
        p.setBrush(QColor("#5fae9f"))
        p.drawRoundedRect(QRectF(cx - bw / 2, bar_y, max(2, bw * remain), 4), 2, 2)

    def _draw_laptop(self, p, typing):
        """办公桌模式的笔记本道具: 画在精灵之后, 挡住被裁掉的下半身。"""
        w, h = self.width(), self.height()
        lw = w * .74
        x0 = (w - lw) / 2
        base_h, screen_h = 9, 34
        yb = h - 2  # 底座贴窗口底(= 任务栏顶)
        screen_rect = QRectF(x0 + 5, yb - base_h - screen_h, lw - 10, screen_h)
        p.setPen(QPen(QColor("#d9d9c9"), 1))
        p.setBrush(QColor("#1c2529"))
        p.drawRoundedRect(screen_rect, 4, 4)
        # 屏幕上的代码行, 打字时微微发亮
        glow = .55 + .35 * math.sin(self._phase * 6) if typing else .45
        p.setPen(Qt.NoPen)
        for i, (cw, color) in enumerate(((.42, QColor(94, 168, 143)),
                                         (.58, QColor(127, 168, 201)),
                                         (.30, QColor(210, 180, 120)))):
            color.setAlphaF(glow)
            p.setBrush(color)
            p.drawRoundedRect(QRectF(screen_rect.left() + 7, screen_rect.top() + 7 + i * 8,
                                     (screen_rect.width() - 14) * cw, 3.4), 1.7, 1.7)
        # 键盘底座(梯形)
        base = QPainterPath()
        base.moveTo(x0, yb)
        base.lineTo(x0 + lw, yb)
        base.lineTo(x0 + lw - 4, yb - base_h)
        base.lineTo(x0 + 4, yb - base_h)
        base.closeSubpath()
        p.setPen(QPen(QColor("#d9d9c9"), 1))
        p.setBrush(QColor("#39454a"))
        p.drawPath(base)

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            self._last_interaction = self._now
            if self.player.dozing:
                self._wake()
            self._press = event.globalPosition().toPoint()
            self._drag_pos = self._press - self.pos()
            self._moved = False
            self._shake_marks = []
            self._shake = 0.0
            self._shake_dir = 0
            self._shake_ref_x = self._press.x()
            self._last_drag = None
            self.setCursor(Qt.ClosedHandCursor)

    def mouseMoveEvent(self, event):
        if self._drag_pos is not None and event.buttons() & Qt.LeftButton:
            current = event.globalPosition().toPoint()
            if (current - self._press).manhattanLength() >= QApplication.startDragDistance():
                self._moved = True
            if self._moved:
                self._register_shake_move(current.x(), time.monotonic())
                next_pos = current - self._drag_pos
                self._target_tilt = max(-9, min(9, (next_pos.x() - self.x()) * .35))
                self.move(current - self._drag_pos)

    def _register_shake_move(self, x, now):
        """摇晃检测核心(独立可测): 方向反转且两转向点相距满幅才算一次晃动。
        不依赖瞬时速度——离散鼠标采样在拐点必然过零, 旧的速度判据永远凑不齐。"""
        dx = x - self._last_drag if self._last_drag is not None else 0
        if dx:
            direction = 1 if dx > 0 else -1
            if direction != self._shake_dir:
                if self._shake_dir:
                    if abs(x - self._shake_ref_x) >= SHAKE_AMP:
                        self._shake_marks.append(now)
                    self._shake_ref_x = x
                else:
                    self._shake_ref_x = x      # 首次移动: 只定方向与起点
                self._shake_dir = direction
        self._last_drag = x
        self._shake_marks = [t for t in self._shake_marks if now - t <= SHAKE_WIN]
        self._shake = float(len(self._shake_marks))

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.LeftButton and self._drag_pos is not None:
            self._last_interaction = self._now
            dizzy = self._moved and len(self._shake_marks) >= SHAKE_NEED and self._animate
            if not self._moved:
                self.clicked.emit()
            self._drag_pos = None
            self._target_tilt = 0
            self._bounce_at = self._now
            if dizzy:
                # 晃晕了: 螺旋眼帧随摆动相位左右歪头(sample 的 dizzy 分支) + 气泡
                now = time.monotonic() - self._epoch
                self._dizzy_until = now + min(4.0, 2.4 + 0.5 * len(self._shake_marks))
                self._reaction = "好晕好晕…"
                self._reaction_until = now + 2.4
                self._bubble_pop = now
            else:
                self.react("land" if self._moved else "pet", self.revive_text)
            self.clamp_position()
            if self._moved:
                self._maybe_snap_desk()  # 松手时贴近底栏则吸附, 远离则切回
                self.moved.emit()
            self.settings.setValue("position", self.pos())
            self.setCursor(Qt.PointingHandCursor)

    @property
    def panel_glass(self):
        return self._panel_glass

    def set_panel_glass(self, transparency):
        """看板毛玻璃透明度(0=不透明), 持久化并通知面板。"""
        self._panel_glass = max(0, min(90, int(transparency)))
        self.settings.setValue("panel_glass", self._panel_glass)
        self.glass_changed.emit(self._panel_glass)

    def _grow_top(self, extra):
        """叠叠乐塔顶要长出窗口: 向上加高窗口(她钉在底部不动), 给塔身无限空间。"""
        if self._trophy_pad:
            return
        extra = int(extra) + 8
        self._trophy_pad = extra
        self._trophy_geom = (self.y(), self.height())
        self.setFixedSize(self.width(), self.height() + extra)  # 窗口是 fixedSize, 先改尺寸
        self.move(self.x(), self.y() - extra)                   # 再整体上移, 脚底屏幕位置不变

    def _restore_top(self):
        if not self._trophy_pad:
            return
        y, h = self._trophy_geom
        self._trophy_pad = 0
        self._trophy_geom = None
        self.setFixedSize(self.width(), h)
        self.move(self.x(), y)

    def switch_character(self, target=None):
        """切换角色: 不带参则在奥尔(猫头鹰娘)和向日葵之间轮换, 菜单传指定目标。"""
        if target is None:
            target = "sunflower" if self.character == "owl" else "owl"
        if target not in ("owl", "sunflower") or target == self.character:
            return
        self.character = target
        self.settings.setValue("character", self.character)
        self.settings.sync()
        if self.character != "owl" and self._desk:
            # 先退出办公桌再换角色, 避免贴着底栏调站姿尺寸
            self._desk = False
            self.settings.setValue("desk", False)
        self.player.set_character(self.character)
        self.set_size(self.settings.value("height", 210, type=int))
        self.clamp_position()
        self.react("pet", "我是%s啦" % ("向日葵" if self.character == "sunflower" else "奥尔"))

    def contextMenuEvent(self, event):
        menu = QMenu(self)
        menu.setAttribute(Qt.WA_TranslucentBackground)
        menu.setStyleSheet(MENU_STYLE)
        shadow = QGraphicsDropShadowEffect(self)
        shadow.setBlurRadius(28)
        shadow.setOffset(0, 6)
        shadow.setColor(QColor(18, 27, 29, 90))
        menu.setGraphicsEffect(shadow)

        open_act = menu.addAction("打开 / 收起看板")
        bold = open_act.font()
        bold.setBold(True)
        open_act.setFont(bold)
        open_act.triggered.connect(self.clicked.emit)
        refresh_act = menu.addAction("立即刷新")
        refresh_act.triggered.connect(self.refresh_requested.emit)

        fun = menu.addMenu("互动")
        fun.addAction("摸摸头", lambda: self.react("pet", self.revive_text))
        fun.addAction("打个招呼", self.say_hi)
        fun.addAction("快端上来罢（启动记录）", self.show_trophy)
        desk = fun.addAction("底栏办公桌模式")
        desk.setCheckable(True)
        desk.setChecked(self._desk)
        desk.triggered.connect(self.toggle_desk)

        music = menu.addMenu("听音乐模式")
        train = music.addAction("训练 10 秒（学你的敲击）")
        train.triggered.connect(self.start_training)
        music.addSeparator()
        music_group = QActionGroup(music)
        music_group.setExclusive(True)
        for mode in MUSIC_MODES:
            act = QAction(MUSIC_MENU[mode], music)
            act.setCheckable(True)
            act.setChecked(self._music_mode == mode)
            music_group.addAction(act)
            act.triggered.connect(lambda _checked, m=mode: self.set_music_mode(m))
            music.addAction(act)

        cfg = menu.addMenu("设置")
        motion = cfg.addAction("动画与互动动作")
        motion.setCheckable(True)
        motion.setChecked(self._animate)
        motion.triggered.connect(self.toggle_motion)
        watch = cfg.addAction("新应用启动提醒")
        watch.setCheckable(True)
        watch.setChecked(self._watch_apps)
        watch.triggered.connect(self.toggle_watch_apps)
        autostart = cfg.addAction("开机自启")
        autostart.setCheckable(True)
        autostart.setChecked(autostart_enabled())
        autostart.triggered.connect(set_autostart)
        size = cfg.addMenu("桌宠大小")
        size_group = QActionGroup(size)
        size_group.setExclusive(True)
        current_h = self.settings.value("height", 210, type=int)
        for text, height in (("小 · 180", 180), ("中 · 240", 240), ("大 · 320", 320)):
            act = QAction(text, size)
            act.setCheckable(True)
            act.setChecked(abs(current_h - height) < 30)
            size_group.addAction(act)
            act.triggered.connect(lambda _checked, h=height: self.resize_pet(h))
            size.addAction(act)
        glass = cfg.addMenu("看板毛玻璃")
        glass_group = QActionGroup(glass)
        glass_group.setExclusive(True)
        for value, text in ((0, "不透明"), (25, "25% 透明"), (45, "45% 透明"),
                            (65, "65% 透明"), (85, "85% 透明")):
            act = QAction(text, glass)
            act.setCheckable(True)
            act.setChecked(self._panel_glass == value)
            glass_group.addAction(act)
            act.triggered.connect(lambda _checked, v=value: self.set_panel_glass(v))
            glass.addAction(act)
        role = cfg.addMenu("角色")
        role_group = QActionGroup(role)
        role_group.setExclusive(True)
        for cid, text in (("owl", "奥尔（猫头鹰娘）"), ("sunflower", "向日葵")):
            act = QAction(text, role)
            act.setCheckable(True)
            act.setChecked(self.character == cid)
            role_group.addAction(act)
            act.triggered.connect(lambda _checked, target=cid: self.switch_character(target))
            role.addAction(act)

        menu.addSeparator()
        menu.addAction(self._menu_bottom_row(menu))
        menu.exec(event.globalPos())

    def _menu_bottom_row(self, menu):
        """胶囊菜单收底: 重启 / 退出 同行平分, 两枚等宽按钮。"""
        bottom = QWidget(menu)
        bottom.setStyleSheet(
            "QPushButton {background:transparent;border:0;border-radius:7px;"
            "padding:7px 0;font-size:12px;font-weight:700;color:#26383a;}"
            "QPushButton:hover {background:#cfe4dc;}"
            "QPushButton:pressed {background:#b9d8cd;}")
        row = QHBoxLayout(bottom)
        row.setContentsMargins(4, 2, 4, 4)
        row.setSpacing(4)
        restart_btn = QPushButton("重启")
        quit_btn = QPushButton("退出")

        def _restart_and_close():
            menu.close()
            self.restart_app()

        def _quit_and_close():
            menu.close()
            QApplication.quit()

        restart_btn.clicked.connect(_restart_and_close)
        quit_btn.clicked.connect(_quit_and_close)
        row.addWidget(restart_btn, 1)
        row.addWidget(quit_btn, 1)
        holder = QWidgetAction(menu)
        holder.setDefaultWidget(bottom)
        return holder

    def restart_app(self):
        """重启: 分离式起一个新进程再退出当前实例。
        aboutToQuit 会自动触发 App.shutdown 做退出同步, 无需在此重复处理。
        兼容源码运行(python app.py)和 PyInstaller onefile(frozen exe)两种形态。"""
        if getattr(sys, "frozen", False):
            QProcess.startDetached(sys.executable, [])
        else:
            QProcess.startDetached(sys.executable, [os.path.abspath(sys.argv[0])])
        QApplication.quit()

    def resize_pet(self, height):
        self.set_size(height)
        self.clamp_position()

    def toggle_motion(self, enabled):
        self._animate = enabled
        self.timer.setInterval(16 if enabled else 100)
        self.settings.setValue("animate", enabled)
        self.update()
