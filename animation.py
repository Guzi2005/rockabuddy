"""Time-based sprite player. Render cadence is independent of sprite timing."""
import math
import os
import random
from PySide6.QtCore import Qt, QPointF, QRect, QRectF
from PySide6.QtGui import QImage, QPainter, QPixmap, QBitmap, QRegion


CLIPS = {
    "blink": [(0, .04), (1, .05), (2, .10), (3, .05), (0, .07)],
    # 注意到你:  idle -> 前倾察觉 -> 回正过渡 -> 睁大眼 -> 歪头好奇 -> 笑意渐起 -> 眯眼开心 -> 停在悬停帧
    # 打招呼(上半程): 悬停时定格在 6 号"握拳托下巴"姿势, 离开才播 greet_out 下半程
    "greet": [(0, .06), (15, .10), (30, .09), (4, .12), (6, .18)],
    "greet_out": [(31, .14), (5, .30), (4, .16), (0, .12)],
    # 招手(上半程): 定格在 17 号举手, 离开播 wave_out 放下手
    "wave": [(0, .08), (17, .22), (8, .16), (17, .24), (8, .16), (17, .28), (18, .26), (4, .12)],
    "wave_out": [(18, .22), (4, .14), (0, .10)],
    "pet": [(4, .08), (18, .10), (5, .42), (4, .14)],
    "land": [(16, .07), (1, .06), (0, .16)],
    # 猫式抖耳: 半压耳过渡 -> 双耳压下 -> 单耳一抖 -> 压回 -> 半压回弹 -> 松开
    "twitch": [(0, .06), (29, .09), (12, .10), (13, .09), (12, .09), (29, .09), (0, .12)],
    # 举起应用图标: 蓄势下蹲 -> 展臂(视频补帧) -> 半程抬手(视频补帧) -> 高举托掌(hold_frame 定格)
    "launch": [(25, .09), (34, .08), (35, .08), (26, .18)],
    # 原有底部叶子轻抬视频的 60fps 抽帧；反向播放同一序列完成放回。
    "sflaunch": [(i, 1 / 60) for i in range(25, 62)],
    "sfreturn": [(i, 1 / 60) for i in range(61, 24, -1)],
    # 办公桌眨眼: 闭眼微笑 -> 睁开(笔记本是画进帧里的, 不能借用全身眨眼帧)
    "deskblink": [(22, .10), (20, .10)],
    # 被摇晃后的眩晕: 螺旋眼定格, 配合 pet 里的摆动余韵
    "dizzy": [(23, 2.8)],
    # 伸懒腰(自主待机): 借举图标的蓄势-展臂帧, 举到最高停留再按原路放下
    "stretch": [(25, .14), (34, .11), (35, .38), (26, .60),
                (35, .11), (34, .11), (25, .15), (0, .24)],
}

# 这些剪辑走 smoothstep 倍速曲线: 起手与收尾慢、中间快, 动作更灵动丝滑
EASE_CLIPS = {"greet", "greet_out", "wave", "wave_out", "pet", "stretch", "launch"}

FADE = 0  # 帧切换不做交叉淡化: 密集帧序列下反复重开淡化会一直半透明闪白, 硬切更干净

SUNFLOWER_DANCE_FRAMES = 25
SUNFLOWER_HOLD_FRAME = 61
SUNFLOWER_LIFT_SECONDS = 37 / 60
SUNFLOWER_PALM = (.74, .835)  # 原有右叶的承托处，塔身避开脸部中心

# 跟随音乐摇摆(视频抽帧, 双侧真实帧): 左右各 4 档倾角, 按正弦相位量化选帧
ROCK_L = (37, 42, 41, 38)   # 左倾 轻->深
ROCK_R = (43, 39, 44, 40)   # 右倾 轻->深


class SpritePlayer:
    def __init__(self, directory):
        self.directory = directory
        self.character = "owl"
        self.frames = []
        self.sprite_aspect = 0.67   # 角色首帧内容宽高比, 供 pet 按身形定窗宽
        self._load_frames()
        self.clip = "idle"
        self.started = 0.0
        self.next_blink = 2.5 + 4.0 * random.random()   # 眨眼间隔随机化
        self._blink_twice = False        # 连眨两下
        self.dozing = False              # 打盹(自主待机, 由 pet 置位/唤醒)
        self.frame_index = 0
        self._fade_from = None   # 上一帧编号, 用于交叉淡入
        self._fade_at = 0.0
        self._fade_strength = 1.0  # 被打断时旧帧的实际可见度, 从此处继续淡出(防闪烁)
        self.hold_frame = None   # 剪辑播完后定格的帧(举图标姿势)
        self.hold_until = 0.0
        self.hold_next = None    # 定格结束后自动接力的剪辑(打招呼收尾等)
        self.hold_kind = None    # 定格来源剪辑名(greet/wave/launch)
        self.hold_next = None    # 定格结束后自动接力的剪辑(打招呼收尾等)
        self.rocking = False     # 音乐节拍摇摆中(循环 ROCK_CYCLE)
        self.rock_period = 0.5   # 拍长秒, 由 BeatListener 连续更新
        self.rock_anchor = 0.0   # 循环相位起点
        self.rock_cap = 3        # 摇摆帧档位上限(节拍器只轻点 0-1 档)
        self.talking = False     # 跟唱说话: 有声张嘴帧交替, 无声闭嘴待机
        self.opera = False       # 美声跟唱: 眯眼笑帧, 摇摆在 pet 侧按乐句缓动

    def set_character(self, character):
        """切换角色(owl/sunflower), 重载图集并重置播放状态。"""
        if character == self.character:
            return
        self.character = character
        self._load_frames()
        self.clip = "idle"
        self.hold_frame = None
        self.rocking = False
        self.talking = False
        self.opera = False
        self.dozing = False
        self.hold_next = None
        self._fade_from = None
        self._fade_strength = 1.0
        self.frame_index = 0

    def _load_frames(self):
        self.frames = []
        if self.character == "sunflower":
            atlas = QPixmap(os.path.join(self.directory, "sunflower-leaf-v2.png"))
            if atlas.isNull():
                return
            w, h = atlas.width() // 8, atlas.height() // 8
            for index in range(SUNFLOWER_HOLD_FRAME + 1):
                # 保留统一画布，叶片运动不会改变角色与图标的锚点。
                self.frames.append(atlas.copy(index % 8 * w, index // 8 * h, w, h))
            return
        atlas = QPixmap(os.path.join(self.directory, "owl-frames.png"))
        if atlas.isNull():
            return
        w = atlas.width() // 4
        # 单元格宽高比约 2:3, 按行数自适应(旧 4x2 / 补帧后 4x3)
        rows = max(2, round(atlas.height() / (w * 1.5)))
        h = atlas.height() // rows
        # 逐帧量内容框, 再取并集统一裁切: 所有帧共享同一地平线与画布尺寸,
        # 摇摆/展臂/眨眼时精灵比例恒定, 脚底钉死不飘
        boxes = []
        cells = []
        for index in range(4 * rows):
            frame = atlas.copy(index % 4 * w, index // 4 * h, w, h)
            bounds = QRegion(QBitmap.fromImage(frame.toImage().createAlphaMask())).boundingRect()
            cells.append((frame, bounds))
            if not bounds.isEmpty():
                boxes.append(bounds)
        if boxes:
            left = min(b.left() for b in boxes)
            top = min(b.top() for b in boxes)
            right = max(b.right() for b in boxes)
            bottom = max(b.bottom() for b in boxes)
            for frame, _bounds in cells:
                self.frames.append(frame.copy(left, top, right - left + 1,
                                              bottom - top + 1))
        self._measure_aspect()

    def _measure_aspect(self):
        """量首帧内容宽高比: 不同角色(奥尔细高/向日葵方正)同档身高观感一致。"""
        self.sprite_aspect = 0.67
        if self.frames:
            img = self.frames[0].toImage()
            bounds = QRegion(QBitmap.fromImage(img.createAlphaMask())).boundingRect()
            if not bounds.isEmpty() and bounds.height():
                self.sprite_aspect = max(0.4, min(1.4, bounds.width() / bounds.height()))

    def play(self, clip, now):
        self.clip, self.started = clip, now

    def _set(self, index, now, fade=True):
        if FADE <= 0:
            fade = False        # 淡化关闭: 全部硬切, 跳过淡化簿记(也避免除零)
        if index != self.frame_index:
            if not fade:
                # 短于淡入时长的快帧硬切, 交叉淡化反而因透明度叠加不足露底色(闪烁)
                self._fade_from = None
                self._fade_strength = 1.0
                self.frame_index = index
                return
            # 若上一次淡入还没完成, 当前帧的实际可见度是 t 而不是 1;
            # 从它的真实可见度继续淡出, 避免「正在显示的帧瞬间归零重淡入」的闪烁
            if self._fade_from is not None and now - self._fade_at < FADE:
                self._fade_strength = min(1.0, max(0.0, (now - self._fade_at) / FADE))
            else:
                self._fade_strength = 1.0
            self._fade_from = self.frame_index
            self._fade_at = now
            self.frame_index = index

    # 被晃晕: 螺旋眼随摆动相位左右歪头(与 pet._tilt 的 sin(now*9.5) 同步)
    def _dizzy_frame(self, now):
        s = math.sin(now * 9.5)
        return 33 if s > .35 else (32 if s < -.35 else 23)

    def _after_clip(self, finished, now):
        """小剪辑播完: 连眨的眨眼立即补第二次, 其余随机排下一次眨眼(2.5-6.5s)。"""
        if finished == "blink" and self._blink_twice:
            self._blink_twice = False
            self.play("blink", now)
        else:
            self.next_blink = now + 2.5 + 4.0 * random.random()

    def _sample_sunflower(self, now):
        """向日葵(原版愿望): 整支 25 帧舞蹈循环, 一拍跳完整支; 安静时 60bpm 轻摆。
        举图标/叠叠乐只轻抬原有底部叶子，结束后平滑放回。"""
        n = len(self.frames)
        if not n:
            self._set(0, now)
            return 0
        # 猫头鹰互动剪辑的编号不能用于向日葵，否则会误播托举帧。
        if self.clip not in ("idle", "sflaunch", "sfreturn"):
            self.clip = "idle"
        if self.clip != "idle":
            elapsed = now - self.started
            for index, duration in CLIPS[self.clip]:
                if elapsed < duration:
                    index = index % n
                    self._set(index, now, fade=duration >= FADE)
                    return index
                elapsed -= duration
            if self.clip == "sfreturn":
                self.rock_anchor = self.started + SUNFLOWER_LIFT_SECONDS
            self.clip = "idle"
        if self.hold_frame is not None and now < self.hold_until:
            # 定格(举图标托举): 叶子平展举着, 不跳舞
            index = self.hold_frame % n
            self._set(index, now)
            return index
        if self.hold_frame is not None:
            self.hold_frame = None
            self.play("sfreturn", self.hold_until)
            return self._sample_sunflower(now)
        dance_count = min(SUNFLOWER_DANCE_FRAMES, n)
        if self.rocking:
            step = max(.02, self.rock_period / dance_count)
        else:
            step = 1.0 / dance_count
        index = int((now - self.rock_anchor) / step) % dance_count
        self._set(index, now)
        return index

    def sample(self, now, hover=False, enabled=True, desk=False, paw=0, dizzy=False):
        if not enabled:
            self._set(0, now)
            return 0
        if self.character == "sunflower":
            return self._sample_sunflower(now)
        if dizzy and not desk and len(self.frames) > 33:
            index = self._dizzy_frame(now)
            self._set(index, now)
            return index
        # 定格到期: 在剪辑处理前拦截, 接力 hold_next(打招呼收尾等)
        if self.hold_frame is not None and now >= self.hold_until:
            self.hold_frame = None
            if self.hold_next:
                nxt, self.hold_next = self.hold_next, None
                self.play(nxt, now)
        if self.clip != "idle":
            seq = CLIPS[self.clip]
            elapsed = now - self.started
            if self.clip in EASE_CLIPS:
                # 倍速曲线: smoothstep 重映射时间轴, 起手/收尾慢、中间快
                total = sum(d for _i, d in seq)
                if total > 0:
                    p = max(0.0, min(1.0, elapsed / total))
                    elapsed = (p * p * (3 - 2 * p)) * total
            for index, duration in seq:
                if elapsed < duration:
                    index = index % len(self.frames) if self.frames else index
                    # 剪辑内的帧步进: 帧够长才淡入, 快帧硬切(眨眼这类快速序列硬切更利落)
                    self._set(index, now, fade=duration >= FADE)
                    return index
                elapsed -= duration
            finished, self.clip = self.clip, "idle"
            self._after_clip(finished, now)
        if self.hold_frame is not None and now < self.hold_until:
            # 定格(如举图标/悬停托下巴): 期间不眨眼不抖耳
            index = self.hold_frame % len(self.frames) if self.frames else self.hold_frame
            self._set(index, now)
            return index
        if self.opera:
            # 美声跟唱: 眯眼笑的表情定住, 摇摆由 pet 侧按乐句缓动
            self._set(5, now)
            return self.frame_index
        if self.talking:
            # 汤姆猫式跟唱: 笑/闭嘴帧快速交替当"张嘴", 无声时 pet 侧已关掉
            self._set(4 if int(now / .14) % 2 == 0 else 0, now)
            return self.frame_index
        if self.rocking and not desk and len(self.frames) > 40:
            # 音乐节拍摇摆: 相位走正弦——两极(倾角最深)S 缓动停留, 过中线快,
            # 单摆式来回, 最深点正好压在拍点上; 期间不眨眼
            s = math.sin(math.pi * (now - self.rock_anchor)
                         / max(.2, self.rock_period))
            a = abs(s)
            # 量化带顶宽底窄: 正弦在极值附近慢, 最深档自然占 ~1/3 时间(两极 S 缓动停留)
            if a < .28:
                index = 0
            elif a < .50:
                level = 0
            elif a < .72:
                level = 1
            elif a < .87:
                level = 2
            else:
                level = 3
            if a >= .28:
                index = (ROCK_L if s < 0 else ROCK_R)[min(level, self.rock_cap)]
            self._set(index, now)
            return index
        if self.dozing:
            # 打盹: 闭眼帧定住, 不眨眼不抖耳(pet 侧负责唤醒与 Zzz 粒子)
            if desk and len(self.frames) > 22:
                self._set(22, now)
            else:
                self._set(2, now)
            return self.frame_index
        if now >= self.next_blink:
            if desk and len(self.frames) > 22:
                # 办公桌模式: 用带笔记本的专用眨眼帧, 不切回全身
                self.play("deskblink", now)
            else:
                # 空闲小动作轮换: 约 1/4 概率抖耳朵, 其余眨眼(偶发连眨两下)
                if math.sin(now * 7.31) > 0.62:
                    self.play("twitch", now)
                else:
                    self._blink_twice = random.random() < .14
                    self.play("blink", now)
        if desk and len(self.frames) > 24:
            # 办公桌模式(笔记本烤进帧): 每个按键按一下, 左右爪交替, 停手搭键盘
            if paw == 1:
                self._set(21, now)
            elif paw == 2:
                self._set(24, now)
            else:
                self._set(20, now)
            return self.frame_index
        if desk and len(self.frames) > 22:
            if paw:
                self._set(21, now)
            else:
                self._set(20, now)
            return self.frame_index
        if desk and not hover and len(self.frames) > 9:
            # 旧图集回退: 程序画的笔记本挡下半身
            if paw:
                self._set(8 if int(now / .12) % 2 == 0 else 9, now)
            else:
                self._set(8, now)
            return self.frame_index
        self._set(4 if hover else 0, now)
        return self.frame_index

    def _draw_one(self, painter, rect, index, fallback, opacity):
        sprite = self.frames[index] if self.frames else fallback
        if sprite.isNull() or opacity <= 0:
            return
        target = self.drawn_rect(rect, index, fallback)
        painter.save()
        painter.setOpacity(max(0.0, min(1.0, opacity)))
        painter.drawPixmap(target, sprite, QRectF(sprite.rect()))
        painter.restore()

    def drawn_rect(self, rect, index, fallback):
        """与 _draw_one 相同的落位计算, 供 overlay(如图标)锚定到帧内位置。"""
        sprite = self.frames[index] if self.frames else fallback
        height = rect.height()
        width = height * sprite.width() / sprite.height()
        if width > rect.width():
            height *= rect.width() / width
            width = rect.width()
        return QRectF(rect.center().x() - width / 2, rect.bottom() - height,
                      width, height)

    def _draw_blend(self, painter, rect, old, new, fallback, strength, t):
        """真·交叉淡化: old*(1-t)+new*t 预乘相加, 重叠区 alpha 不掉,
        不会把透明窗口背后的白色透出来(闪白的根因)。
        注意 QPainter 的 opacity 会让 Plus 退化成 SourceOver,
        所以新帧先以透明度画进独立缓冲, 再用 Plus(不透明)相加。"""
        old_sprite = self.frames[old] if self.frames else fallback
        new_sprite = self.frames[new] if self.frames else fallback
        if old_sprite.isNull() or new_sprite.isNull():
            self._draw_one(painter, rect, new, fallback, 1.0)
            return
        old_rect = self.drawn_rect(rect, old, fallback)
        new_rect = self.drawn_rect(rect, new, fallback)
        area = old_rect.united(new_rect).toAlignedRect()
        if area.isEmpty():
            return
        offset = QPointF(-area.x(), -area.y())
        img = QImage(area.size(), QImage.Format_ARGB32_Premultiplied)
        img.fill(Qt.transparent)
        ip = QPainter(img)
        ip.setRenderHint(QPainter.SmoothPixmapTransform)
        ip.setOpacity(max(0.0, min(1.0, strength * (1 - t))))
        ip.drawPixmap(old_rect.translated(offset), old_sprite, QRectF(old_sprite.rect()))
        ip.end()
        layer = QImage(area.size(), QImage.Format_ARGB32_Premultiplied)
        layer.fill(Qt.transparent)
        lp = QPainter(layer)
        lp.setRenderHint(QPainter.SmoothPixmapTransform)
        lp.setOpacity(max(0.0, min(1.0, t)))
        lp.drawPixmap(new_rect.translated(offset), new_sprite, QRectF(new_sprite.rect()))
        lp.end()
        ip = QPainter(img)
        ip.setCompositionMode(QPainter.CompositionMode_Plus)
        ip.drawImage(0, 0, layer)
        ip.end()
        painter.drawImage(area.topLeft(), img)

    def draw(self, painter, rect, index, fallback, now=None):
        # 帧切换瞬间做交叉淡化: 像素级线性插值, 不再是两帧各自降透明度叠加
        if (now is not None and self._fade_from is not None
                and self._fade_from != index and 0 <= now - self._fade_at < FADE):
            t = (now - self._fade_at) / FADE
            self._draw_blend(painter, rect, self._fade_from, index, fallback,
                             self._fade_strength, t)
            return
        self._draw_one(painter, rect, index, fallback, 1.0)
