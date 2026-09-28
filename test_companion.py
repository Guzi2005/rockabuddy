"""Offline checks for companion status, interactions and input validation."""
import os
import tempfile
import time
import unittest
from PySide6.QtCore import Qt, QPoint
from PySide6.QtTest import QTest
from app import QApplication, Dashboard, ManualEditDialog, PetWidget, ASSET
import providers

qt = QApplication.instance() or QApplication([])


class CompanionTests(unittest.TestCase):
    def test_lowest_remaining_and_unknown(self):
        pet = PetWidget()
        cfg = [{"id": "a", "name": "A"}, {"id": "b", "name": "B"}]
        pet.set_status({"a": {"ok": True, "remaining": 90, "total": 100},
                        "b": {"ok": True, "remaining": 10, "total": 100}}, cfg)
        self.assertEqual(pet.pct, 10)
        self.assertTrue(pet.alert)
        pet.set_status({"a": {"ok": False}}, cfg)
        self.assertIsNone(pet.pct)
        self.assertFalse(pet.alert)
        pet.close()

    def test_click_opens_and_drag_does_not(self):
        pet = PetWidget()
        clicks = []
        pet.clicked.connect(lambda: clicks.append(True))
        QTest.mouseClick(pet, Qt.LeftButton, pos=QPoint(50, 80))
        self.assertEqual(len(clicks), 1)
        QTest.mousePress(pet, Qt.LeftButton, pos=QPoint(50, 80))
        QTest.mouseMove(pet, QPoint(90, 80))
        QTest.mouseRelease(pet, Qt.LeftButton, pos=QPoint(90, 80))
        self.assertEqual(len(clicks), 1)
        pet.close()

    def test_invalid_numbers_are_not_saved(self):
        for rem, total in [("nan", ""), ("-1", "10"), ("4", "0"), ("11", "10"), ("abc", "")]:
            dlg = ManualEditDialog("test", {})
            dlg.ed_rem.setText(rem)
            dlg.ed_total.setText(total)
            dlg.validate()
            self.assertEqual(dlg.result(), 0)
            self.assertTrue(dlg.error_label.text())
        dlg.ed_rem.setText("5")
        dlg.ed_total.setText("10")
        dlg.validate()
        self.assertEqual(dlg.values(), (5, 10))
        self.assertEqual(dlg.result(), 1)

    def test_cursor_dispatch(self):
        self.assertIs(providers.ADAPTERS["cursor"], providers.fetch_cursor)

    def test_sitehist_title_parsing(self):
        import sitehist
        self.assertEqual(sitehist.strip_browser_suffix(
            "原神 - 哔哩哔哩 - Microsoft Edge"), "原神 - 哔哩哔哩")
        self.assertEqual(sitehist.strip_browser_suffix(
            "DeepSeek - Google Chrome"), "DeepSeek")
        self.assertEqual(sitehist.strip_browser_suffix("没有后缀的标题"),
                         "没有后缀的标题")
        # 显示名: 取最后一段, 太长退到域名主体
        self.assertEqual(sitehist.page_display_name(
            "来点梗图 - 小红书", "www.xiaohongshu.com"), "小红书")
        self.assertEqual(sitehist.page_display_name(
            "无分隔符标题", "example.com"), "example")

    def test_launch_log_roundtrip_and_dedup(self):
        import os
        import tempfile
        import launchlog
        with tempfile.TemporaryDirectory() as tmp:
            old = os.environ.get("LOCALAPPDATA")
            os.environ["LOCALAPPDATA"] = tmp
            try:
                launchlog.record_launch("app", r"C:\a\code.exe", "VS Code")
                launchlog.record_launch("site", "bilibili", "B站")
                launchlog.record_launch("app", r"C:\a\code.exe", "VS Code")
                entries = launchlog.load_launches()
                self.assertEqual(len(entries), 3)
                uniq = launchlog.recent_unique(8)
                self.assertEqual([e["id"] for e in uniq],
                                 [r"C:\a\code.exe", "bilibili"])
                self.assertEqual(uniq[0]["name"], "VS Code")
            finally:
                if old is None:
                    del os.environ["LOCALAPPDATA"]
                else:
                    os.environ["LOCALAPPDATA"] = old


class ShakeTests(unittest.TestCase):
    def _shake_series(self, xs, dt=0.06):
        pet = PetWidget()
        t = 100.0
        for x in xs:
            t += dt
            pet._register_shake_move(x, t)
        marks = len(pet._shake_marks)
        pet.close()
        return marks

    def test_vigorous_shake_makes_dizzy(self):
        # 快速大幅左右摇晃: 4 个满幅反向 → 超过 3 次阈值
        marks = self._shake_series([0, 45, 5, 50, 5, 50, 5, 50])
        self.assertGreaterEqual(marks, 3)

    def test_small_wiggle_and_drag_do_not(self):
        # 小幅蠕动(<35px)不算晃动
        self.assertEqual(self._shake_series([0, 20, 0, 20, 0, 20, 0]), 0)
        # 单方向拖拽也不算
        self.assertEqual(self._shake_series([0, 60, 150, 250, 350]), 0)

    def test_marks_expire_over_time(self):
        pet = PetWidget()
        self.addCleanup(pet.close)
        t0 = time.monotonic() - 5.0             # 5 秒前的历史晃动, 早已出窗
        pet._shake_marks = [t0, t0 + 0.3, t0 + 0.6]
        pet._shake_dir = -1
        pet._shake_ref_x = 0
        pet._last_drag = 0
        pet._register_shake_move(-50, time.monotonic())
        self.assertEqual(len(pet._shake_marks), 0)


class SpriteCanvasTests(unittest.TestCase):
    def test_all_frames_share_one_ground_canvas(self):
        from animation import SpritePlayer
        player = SpritePlayer(os.path.dirname(ASSET))
        sizes = {(f.width(), f.height()) for f in player.frames}
        self.assertEqual(len(sizes), 1, "帧尺寸必须统一, 否则站位会飘")
        self.assertGreater(player.sprite_aspect, 0.4)

    def test_sunflower_gets_wider_window_than_owl(self):
        pet = PetWidget()
        self.addCleanup(pet.close)
        pet.character = "owl"
        pet.player.set_character("owl")
        pet.set_size(210)
        w_owl = pet.width()
        pet.character = "sunflower"
        pet.player.set_character("sunflower")
        pet.set_size(210)
        self.assertGreaterEqual(pet.width(), w_owl)

    def test_wave_clip_plays_the_waving_frames(self):
        from animation import SpritePlayer
        player = SpritePlayer(os.path.dirname(ASSET))
        player.play("wave", 0.0)
        seen = {player.sample(i / 60) for i in range(int(1.6 * 60))}
        self.assertIn(17, seen)   # 招手帧
        self.assertIn(8, seen)    # 收半帧
        self.assertEqual(seen & {18}, {18})


class TrainingTests(unittest.TestCase):
    def test_steady_taps_fit_metronome(self):
        pet = PetWidget()
        self.addCleanup(pet.close)
        pet._music_mode = "off"
        pet._train_taps = [[i * 0.5, 0.05, 65 + i] for i in range(10)]
        pet._train_until = 10.0
        pet._finish_training()
        self.assertEqual(pet._music_mode, "metronome")
        self.assertAlmostEqual(pet.player.rock_period, 0.5, delta=0.03)
        self.assertGreater(pet._train_demo_until, 0)

    def test_long_holds_fit_opera(self):
        pet = PetWidget()
        self.addCleanup(pet.close)
        pet._music_mode = "rock"
        pet._train_taps = [[i * 0.7, 0.45, 65 + i] for i in range(10)]
        pet._train_until = 10.0
        pet._finish_training()
        self.assertEqual(pet._music_mode, "opera")
        self.assertFalse(pet.player.rocking)   # 美声不摇帧, 由乐句缓动

    def test_too_few_taps_keeps_mode(self):
        pet = PetWidget()
        self.addCleanup(pet.close)
        pet._music_mode = "rock"
        pet._train_taps = [[0.0, 0.05, 65], [0.5, 0.05, 66]]
        pet._train_until = 10.0
        pet._finish_training()
        self.assertEqual(pet._music_mode, "rock")


class OperaNoteTests(unittest.TestCase):
    def test_release_creates_note_scaled_by_phrase(self):
        pet = PetWidget()
        pet._now = 10.0
        pet._opera_note_at = 10.0 - 1.5          # 1.5 秒长音
        pet._release_opera_note()
        self.assertEqual(len(pet._notes), 1)
        birth, x0, y0, drift, glyph, size, rise = pet._notes[0]
        self.assertEqual(glyph, "♫")             # 超过一秒换双符干
        self.assertAlmostEqual(size, 41.0)
        pet._opera_note_at = -1.0
        pet._release_opera_note()                # 没蓄音就不放
        self.assertEqual(len(pet._notes), 1)
        pet.close()

    def test_beat_notes_suppressed_in_opera_mode(self):
        pet = PetWidget()
        pet._music_mode = "opera"
        pet._on_beat(0.8, 0.5)
        self.assertEqual(pet._notes, [])         # 美声的音符只由乐句释放
        pet._music_mode = "rock"
        pet._on_beat(0.8, 0.5)
        self.assertEqual(len(pet._notes), 1)
        pet.close()


if __name__ == "__main__":
    unittest.main()
