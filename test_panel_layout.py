import unittest
from PySide6.QtCore import QRectF
from PySide6.QtWidgets import QApplication, QLabel
from panel import Dashboard, UsageCard, SEG_REMAIN, SEG_TODAY, SEG_BEFORE
from animation import SpritePlayer, SUNFLOWER_DANCE_FRAMES, SUNFLOWER_HOLD_FRAME
from pet import PetWidget, ASSET
import os

qt=QApplication.instance() or QApplication([])

class CleanPanelTests(unittest.TestCase):
    def test_single_column_rows_are_preserved(self):
        cfg={'providers':[{'id':'codex','name':'Codex','type':'codex'},
                          {'id':'manual','name':'Manual','type':'manual'}]}
        data={'codex':{'ok':True,'windows':[
            {'label':'5 小时','remaining_percent':75,'resets_at':2000000000}]},
              'manual':{'ok':True,'remaining':12,'unit':'次'}}
        board=Dashboard()
        board.set_data(cfg,data)
        board.show()
        qt.processEvents()
        self.assertEqual(len(board.cards),2)
        self.assertEqual(board.width(),320)
        # 一排一个: 两行上下排开, 不再等高对齐
        self.assertLess(board.cards[0].y(),board.cards[1].y())
        self.assertEqual(board.items.count(),2)
        board.set_data(cfg,data)
        qt.processEvents()
        self.assertEqual(len(board.cards),2)
        board.close()

    def test_bar_segments_split_today_and_before(self):
        card=UsageCard({'id':'x','name':'X','type':'manual'},
                       {'ok':True,'remaining':60,'total':100}, baseline=80.0)
        self.assertEqual([(round(p), c) for p, c in card._segments(60.0, daily=False)],
                         [(60, SEG_REMAIN), (20, SEG_TODAY), (20, SEG_BEFORE)])
        # 日窗(如 ZCode 预算): 消耗全部算今天
        self.assertEqual([(round(p), c) for p, c in card._segments(40.0, daily=True)],
                         [(40, SEG_REMAIN), (60, SEG_TODAY)])
        # 没有历史基线: 分不出今天, 只有 剩余+已耗 两段
        card.baseline=None
        self.assertEqual([(round(p), c) for p, c in card._segments(60.0, daily=False)],
                         [(60, SEG_REMAIN), (40, SEG_BEFORE)])
        card.close()

    def test_multi_window_quota_renders_one_bar_per_window(self):
        from PySide6.QtWidgets import QWidget
        cfg={'providers':[{'id':'codex','name':'Codex','type':'codex'}]}
        data={'codex':{'ok':True,'remaining':8,'total':100,'windows':[
            {'label':'5 小时','remaining_percent':8,'resets_at':2000000000},
            {'label':'本周','remaining_percent':84,'resets_at':2000000000}]}}
        board=Dashboard()
        board.set_data(cfg,data)
        board.show()
        qt.processEvents()
        card=board.cards[0]
        # seg_bar 的 holder: 固定高 5px 且带布局(色块 chunk 没有布局, 被排除)
        bars=[w for w in card.findChildren(QWidget)
              if w.height()==5 and w.layout() is not None]
        self.assertEqual(len(bars),2)
        board.close()

    def test_sunflower_never_loads_or_dances_bad_frames(self):
        player=SpritePlayer(os.path.dirname(ASSET))
        player.set_character('sunflower')
        self.assertEqual(len(player.frames),62)
        self.assertTrue(all(player.sample(i/60)<25 for i in range(600)))
        player.rocking=True
        self.assertTrue(all(player.sample(i/60)<25 for i in range(600)))
        player.play('sflaunch',10)
        player.hold_frame=SUNFLOWER_HOLD_FRAME
        player.hold_until=12
        lift=[player.sample(10+i/60) for i in range(37)]
        self.assertEqual(lift[0],25)
        self.assertGreater(lift[-1],59)
        self.assertEqual(player.sample(11),61)
        self.assertEqual(player.sample(12),61)
        self.assertLess(player.sample(12.3),50)
        self.assertLess(player.sample(12.7),25)
        player.play('greet',13)
        self.assertLess(player.sample(13.2),25)

    def test_sunflower_anchor_is_on_bottom_leaves(self):
        pet=PetWidget()
        pet.character='sunflower'
        x,y=pet._palm_xy(QRectF(10,20,350,360))
        self.assertEqual(x,269)
        self.assertGreater(y,20+360*.75)
        self.assertLess(y,20+360*.95)
        pet.close()

if __name__=='__main__':
    unittest.main()
