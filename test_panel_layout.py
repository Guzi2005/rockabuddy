import os
import time
import unittest
from PySide6.QtCore import QRect, QRectF
from PySide6.QtWidgets import QApplication, QLabel, QWidget
from panel import (Dashboard, UsageCard, SEG_REMAIN, SEG_TODAY, SEG_BEFORE,
                   provider_series, burn_rate)
from providers import _free_status, _in_night_window
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
        cfg={'providers':[{'id':'codex','name':'Codex','type':'codex'}]}
        data={'codex':{'ok':True,'remaining':8,'total':100,'windows':[
            {'label':'5 小时','remaining_percent':8,'resets_at':2000000000},
            {'label':'本周','remaining_percent':84,'resets_at':2000000000}]}}
        board=Dashboard()
        board.set_data(cfg,data)
        board.show()
        qt.processEvents()
        card=board.cards[0]
        # seg_bar 的 holder: 固定高 6px 且带布局(色块 chunk 没有布局, 被排除)
        bars=[w for w in card.findChildren(QWidget)
              if w.height()==6 and w.layout() is not None]
        self.assertEqual(len(bars),2)
        board.close()

    def test_provider_series_and_burn_rate(self):
        now=1_800_000_000
        hist=[{"id":"a","ts":now-7200,"pct":80},
              {"id":"a","ts":now-3600,"pct":74},
              {"id":"a","ts":now-600,"pct":70},
              {"id":"b","ts":now-600,"pct":90}]
        pts=provider_series(hist,"a",now=now)
        self.assertEqual([p for _,p in pts],[80,74,70])
        rate=burn_rate(pts,now=now)
        self.assertAlmostEqual(rate, 10/6600*3600, places=2)
        # 只有一个点算不出速率
        self.assertIsNone(burn_rate(provider_series(hist,"b",now=now),now=now))
        # 恢复(比例上升)是负速率
        up=[(now-3600,50),(now-600,60)]
        self.assertLess(burn_rate(up,now=now),0)

    def test_dynamics_row_renders_sparkline_and_rate(self):
        now = time.time()
        hist=[{"id":"codex","ts":now-i*1800,"pct":80-i*2} for i in range(10)]
        cfg={'providers':[{'id':'codex','name':'Codex','type':'codex'}]}
        data={'codex':{'ok':True,'remaining':8,'total':100,'windows':[
            {'label':'5 小时','remaining_percent':8,'resets_at':2000000000}]}}
        board=Dashboard()
        board.set_data(cfg,data,hist)
        board.show()
        qt.processEvents()
        spark=[w for w in board.cards[0].findChildren(QWidget)
               if w.width()==74 and w.height()==22]
        self.assertEqual(len(spark),1)   # 趋势图就位
        board.close()

    def test_reposition_keeps_panel_on_screen_clear_of_pet(self):
        scr=QApplication.primaryScreen().availableGeometry()
        pet=PetWidget()
        pet.move(scr.center())
        board=Dashboard()
        board.set_data({'providers':[{'id':'x','name':'X','type':'manual'}]}, {})
        board.reposition_for(pet)
        rect=QRect(board.pos(),board.size())
        self.assertTrue(scr.contains(rect))
        self.assertFalse(rect.intersects(pet.frameGeometry()))
        board.close()
        pet.close()

    def test_glass_toggle_stores_state(self):
        board=Dashboard()
        board.apply_glass(45)
        self.assertEqual(board._glass,45)
        self.assertTrue(board._surface.glass)
        board.apply_glass(0)
        self.assertEqual(board._glass,0)
        self.assertFalse(board._surface.glass)
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

    def test_usable_providers_pinned_as_cards(self):
        cfg={'providers':[{'id':'a','name':'A','type':'manual'},
                          {'id':'b','name':'B','type':'manual'},
                          {'id':'c','name':'C','type':'codex'}]}
        data={'a':{'ok':True,'remaining':50,'total':100},
              'b':{'ok':True,'remaining':0,'total':100},
              'c':{'ok':False}}
        board=Dashboard()
        board.set_data(cfg,data)
        board.show()
        qt.processEvents()
        # 可用的 A 排最前并带卡片强调; 耗尽的 B/未连接的 C 扁平沉底
        self.assertEqual(board.cards[0].cfg['id'],'a')
        self.assertEqual(board.cards[0].objectName(),'cardLive')
        self.assertEqual(board.cards[1].objectName(),'')
        self.assertEqual(board.cards[2].objectName(),'')
        self.assertLess(board.cards[0].y(),board.cards[1].y())
        board.close()

    def test_halo_includes_drained_and_missing_as_gray(self):
        pet=PetWidget()
        cfg=[{"id":"a","name":"A"},{"id":"b","name":"B"},{"id":"c","name":"C"}]
        pet.set_status({"a":{"ok":True,"remaining":0,"total":100},
                        "b":{"ok":False}}, cfg)
        self.assertEqual([e[0] for e in pet.orbit],["a","b","c"])
        self.assertTrue(all(e[2] for e in pet.orbit))
        pet.close()

    def test_provider_icon_click_emits_activate(self):
        from PySide6.QtCore import Qt, QPoint
        from PySide6.QtTest import QTest
        from panel import ProviderIcon
        cfg={'providers':[{'id':'zcode','name':'ZCode','type':'zcode',
                           'processes':['ZCode.exe']}]}
        data={'zcode':{'ok':True,'remaining':5,'total':30}}
        board=Dashboard()
        board.set_data(cfg,data)
        got=[]
        board.provider_activated.connect(got.append)
        board.show()
        qt.processEvents()
        icon=board.cards[0].findChildren(ProviderIcon)[0]
        QTest.mouseClick(icon, Qt.LeftButton, pos=QPoint(5,5))
        self.assertEqual(got,['zcode'])
        board.close()


    def test_workbuddy_free_model_list_renders(self):
        cfg={'providers':[{'id':'workbuddy','name':'WorkBuddy','type':'workbuddy_free',
                           'free_models':[
                               {'name':'混元 Hy3','kind':'limited','until':'2099-01-01T00:00'},
                               {'name':'夜间模型','kind':'night','window':'00:00-23:59'}]}]}
        data={'workbuddy':{'ok':True,'free_models':[
                               {'name':'混元 Hy3','kind':'limited','until':'2099-01-01T00:00','free_now':True},
                               {'name':'夜间模型','kind':'night','window':'00:00-23:59','free_now':True}],
                           'free_count':2,'free_total':2,'source':'本地策略表'}}
        board=Dashboard()
        board.set_data(cfg,data)
        board.show()
        qt.processEvents()
        card=board.cards[0]
        self.assertEqual(len(card.free_rows),2)
        # 免费计数只保留徽章, 不再有大标题
        self.assertFalse(hasattr(card,'free_headline'))
        # 限免/夜间免费中应被标绿(底色 e3f2ec)
        self.assertIn("e3f2ec", card.free_rows[0][1].styleSheet())
        board.close()

    def test_free_status_helper(self):
        self.assertEqual(_free_status({'kind':'limited','until':'2099-01-01'})[0], True)
        self.assertEqual(_free_status({'kind':'limited','until':'2000-01-01'})[1], "已过期")
        t=time.mktime(time.strptime("2026-09-29 23:30","%Y-%m-%d %H:%M"))
        self.assertTrue(_in_night_window("23:00-08:00", now=t))
        noon=time.mktime(time.strptime("2026-09-29 12:00","%Y-%m-%d %H:%M"))
        self.assertFalse(_in_night_window("23:00-08:00", now=noon))


if __name__=='__main__':
    unittest.main()
