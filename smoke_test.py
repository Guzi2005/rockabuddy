# -*- coding: utf-8 -*-
"""离屏冒烟测试: 不弹窗, 把看板和悬浮球渲染成 PNG 供检查。"""
import os
import sys

os.environ.pop("QT_QPA_PLATFORM", None)  # 沙箱默认 offscreen 无字体, 用真平台渲染但不 show()
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import providers as prov  # noqa: E402
from app import (App, Dashboard, PetWidget, load_config,  # noqa: E402
                 QApplication, QFont, FONT)


def main():
    qt = QApplication(sys.argv)
    qt.setFont(QFont(FONT, 10))
    cfg = load_config()

    # 1) 适配器冒烟: 无 key 时应快速返回"未配置", manual 正常读出
    print("== adapter smoke ==")
    for p in cfg["providers"]:
        safe = dict(p, api_key="")
        if safe.get("type") != "manual":
            continue  # Offline preview never reads login credentials.
        r = prov.fetch_one(safe)
        print("%-12s ok=%s rem=%s err=%s" % (p["id"], r["ok"],
                                             r["remaining"], r["error"]))

    # 2) 用模拟数据渲染看板
    mock = {
        "codex":       {"ok": True, "remaining": 8, "total": 100, "unit": "%",
                        "note": "Codex 账户实时额度", "source": "Codex app-server",
                        "error": None,
                        "windows": [
                            {"id": "codex:primary", "label": "5 小时", "bucket": "codex",
                             "remaining_percent": 8, "used_percent": 92,
                             "duration_minutes": 300,
                             "resets_at": int(__import__("time").time()) + 4710},
                            {"id": "codex:secondary", "label": "本周", "bucket": "codex",
                             "remaining_percent": 84, "used_percent": 16,
                             "duration_minutes": 10080,
                             "resets_at": int(__import__("time").time()) + 250000}]},
        "kimi":        {"ok": True, "remaining": 49, "total": 100, "unit": "%",
                        "note": "Allegretto ¥199 档 · 并发上限 20",
                        "source": "Kimi Code 额度 API", "error": None,
                        "windows": [
                            {"id": "kimi:week", "label": "周配额", "bucket": "Kimi",
                             "remaining_percent": 49, "used_percent": 51,
                             "duration_minutes": 10080,
                             "resets_at": int(__import__("time").time()) + 172000}]},
        "zcode":       {"ok": True, "remaining": 6.7, "total": 30, "unit": "万tok",
                        "note": "今日 · GLM-5.3-Flash 200万 · GLM-5.3 30万 · 50 次请求",
                        "source": "ZCode 本地会话库", "error": None,
                        "windows": [
                            {"id": "zcode:day", "label": "今日预算", "bucket": "ZCode",
                             "remaining_percent": 22.3, "used_percent": 77.7,
                             "duration_minutes": 1440, "daily": True,
                             "resets_at": int(__import__("time").time()) + 50300}]},
        "cursor":      {"ok": True, "remaining": 320, "total": 500,
                        "unit": "次请求", "note": "", "error": None},
        "trae":        {"ok": True, "remaining": 96, "total": 600,
                        "unit": "次请求", "note": "", "error": None},
        "workbuddy":   {"ok": True, "remaining": 100, "total": None,
                        "unit": "积分", "note": "体验版 · 奖励积分 09/30 到期", "error": None},
    }
    history = []
    base = 1758800000
    for i in range(24):
        for pid, pct in [("cursor", 95 - i * 1.5), ("trae", 100 - i * 3.2),
                         ("codex", 90 - i * 1.8)]:
            history.append({"ts": base + i * 3600 * 13, "id": pid,
                            "pct": max(2, pct)})

    board = Dashboard()
    board._finish_init()
    board.set_data(cfg, mock, history)
    qt.processEvents()
    dest = sys.argv[1] if len(sys.argv) > 1 else os.path.join(os.path.dirname(__file__), "work")
    os.makedirs(dest, exist_ok=True)
    board.grab().save(os.path.join(dest, "preview_dashboard.png"))
    print("dashboard png saved")

    pet = PetWidget()
    pet.pct = 58
    qt.processEvents()
    pet.grab().save(os.path.join(dest, "preview_pet.png"))
    print("pet png saved")


if __name__ == "__main__":
    main()
