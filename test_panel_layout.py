import unittest
from PySide6.QtCore import QRectF
from PySide6.QtWidgets import QApplication, QLabel
from panel import Dashboard, UsageCard
from animation import SpritePlayer, SUNFLOWER_DANCE_FRAMES, SUNFLOWER_HOLD_FRAME
from pet import PetWidget, ASSET
import os

qt=QApplication.instance() or QApplication([])

class CleanPanelTests(unittest.TestCase):
    def test_original_grid_and_refresh_are_preserved(self):
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
        self.assertEqual(board.cards[0].height(),board.cards[1].height())
        self.assertEqual(board.cards[0].y(),board.cards[1].y())
        self.assertIs(board.items.itemAtPosition(0,1).widget(),board.cards[1])
        board.set_data(cfg,data)
        qt.processEvents()
        self.assertEqual(len(board.cards),2)
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
