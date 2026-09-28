"""TokenSpy desktop companion. Run python app.py.

同步策略:
- 窗口恢复倒计时由面板按 resets_at 本地推算, 到期后才真正拉取;
- 心跳同步(heartbeat_minutes)只拉「活跃应用名单」里进程在跑的服务;
- 出错按 refresh_minutes 起步指数退避; 手动刷新始终同步全部;
- 退出时对活跃/到期服务做最后一次快速同步并落盘缓存。
"""
import json
import os
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed, wait
from PySide6.QtCore import Qt, QRect, QThread, QTimer, Signal, QObject
from PySide6.QtGui import QFont, QIcon
from PySide6.QtWidgets import QApplication, QMenu, QSystemTrayIcon
import providers as prov
from pet import PetWidget, ASSET

ICON = os.path.join(os.path.dirname(ASSET), "icon.png")  # 大脸 app 图标(托盘/任务栏)
from panel import Dashboard, ManualEditDialog

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE_DIR, "config.json")
HISTORY_PATH = os.path.join(BASE_DIR, "history.jsonl")
CACHE_PATH = os.path.join(BASE_DIR, "usage-cache.json")
SYNC_STATE_PATH = os.path.join(BASE_DIR, "sync-state.json")
FONT = "Microsoft YaHei UI"
RESET_GRACE = 20          # 窗口到期后宽限几秒再拉取
QUIT_SYNC_BUDGET = 6.0    # 退出同步总预算(秒)

def load_config():
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def save_config(cfg):
    tmp = CONFIG_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
    os.replace(tmp, CONFIG_PATH)


def append_history(pid, pct):
    try:
        with open(HISTORY_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps({"ts": int(time.time()), "id": pid,
                                "pct": round(pct, 1)}) + "\n")
    except OSError:
        pass


def load_history():
    rows = []
    if os.path.exists(HISTORY_PATH):
        try:
            with open(HISTORY_PATH, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line:
                        rows.append(json.loads(line))
        except (OSError, json.JSONDecodeError):
            pass
    return rows


def fmt_num(v):
    if v is None:
        return "--"
    if abs(v) >= 1000:
        return "{:,.0f}".format(v)
    if abs(v) >= 100:
        return "{:.0f}".format(v)
    return "{:.2f}".format(v).rstrip("0").rstrip(".")


def running_processes():
    """本机正在运行的进程名(小写)集合; 失败时返回空集。"""
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        out = subprocess.run(["tasklist", "/FO", "CSV", "/NH"],
                             capture_output=True, text=True, timeout=10,
                             creationflags=flags)
    except (OSError, subprocess.SubprocessError):
        return set()
    names = set()
    for line in out.stdout.splitlines():
        if line.startswith('"'):
            names.add(line.split('","')[0].strip('"').lower())
    return names


def provider_active(cfg, procs):
    """没有绑定进程名单的服务视为常驻; 绑定了的看进程是否在跑。"""
    wanted = [p.lower() for p in cfg.get("processes") or []]
    if not wanted:
        return True
    return any(name in procs for name in wanted)


class FetchWorker(QThread):
    done = Signal(dict, object)

    def __init__(self, configs):
        super().__init__()
        self.configs = configs

    def run(self):
        procs = running_processes()
        results = {}
        with ThreadPoolExecutor(max_workers=4) as pool:
            jobs = {pool.submit(prov.fetch_one, cfg): cfg["id"] for cfg in self.configs}
            for job in as_completed(jobs):
                try:
                    results[jobs[job]] = job.result()
                except Exception:
                    results[jobs[job]] = {"ok": False, "error": "查询失败，请重试"}
        self.done.emit(results, procs)


def merge_results(previous, current):
    merged = {}
    for pid, value in current.items():
        old = previous.get(pid, {})
        if not value.get("ok") and old.get("ok"):
            merged[pid] = dict(old, stale=True, error=value.get("error"),
                               last_attempt_at=value.get("fetched_at"))
        else:
            merged[pid] = dict(value, stale=False)
    return merged


def next_reset_after(data, now):
    """未到期的窗口重置时间中最近的一个。"""
    future = [w["resets_at"] for w in (data.get("windows") or [])
              if w.get("resets_at") and w["resets_at"] > now]
    return min(future) if future else None


class SyncScheduler:
    """为每个服务计算下次同步时间; 状态持久化到 sync-state.json。"""

    def __init__(self, path):
        self.path = path
        self.state = {}
        try:
            with open(path, encoding="utf-8") as stream:
                self.state = json.load(stream)
        except (OSError, ValueError):
            pass

    def save(self):
        try:
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as stream:
                json.dump(self.state, stream, ensure_ascii=False)
            os.replace(tmp, self.path)
        except OSError:
            pass

    def next_at(self, pid):
        return (self.state.get(pid) or {}).get("next_at", 0)

    def soonest(self):
        times = [s.get("next_at") for s in self.state.values() if s.get("next_at")]
        return min(times) if times else None

    def record(self, pid, result, cfg, heartbeat_s, base_s):
        """一次同步结束后排定下次时间。"""
        now = time.time()
        entry = {"fails": 0, "next_at": now + heartbeat_s}
        if result.get("ok"):
            resets = next_reset_after(result, now)
            if resets:
                # 倒计时驱动: 窗口恢复后宽限片刻再拉, 同时不超过心跳上限
                entry["next_at"] = min(resets + RESET_GRACE, now + heartbeat_s)
        else:
            fails = (self.state.get(pid) or {}).get("fails", 0) + 1
            entry["fails"] = fails
            entry["next_at"] = now + min(base_s * (2 ** (fails - 1)), heartbeat_s)
        self.state[pid] = entry

    def due(self, configs, results, procs, heartbeat_s, active_only):
        """到期该同步的服务 id 列表。窗口恢复到期始终同步; 心跳到期只同步活跃应用。"""
        now = time.time()
        selected = []
        for cfg in configs:
            if cfg.get("type") == "manual":
                continue
            pid = cfg["id"]
            data = results.get(pid)
            if data is None or not data.get("fetched_at"):
                selected.append(pid)  # 从未同步过
                continue
            resets = [w["resets_at"] for w in (data.get("windows") or [])
                      if w.get("resets_at") and data["fetched_at"] < w["resets_at"] <= now]
            if resets:
                selected.append(pid)  # 有窗口刚恢复, 确认余量
                continue
            if now < self.next_at(pid):
                continue
            if active_only and not provider_active(cfg, procs):
                continue  # 应用在休息, 余额不会被消耗, 跳过心跳
            selected.append(pid)
        return selected


class App(QObject):
    def __init__(self):
        super().__init__()
        self.qt = QApplication(sys.argv)
        self.qt.setFont(QFont(FONT, 10))
        self.qt.setWindowIcon(QIcon(ICON))  # 任务栏/Alt-Tab 也用大脸图标
        self.qt.setQuitOnLastWindowClosed(False)
        self.qt.aboutToQuit.connect(self.shutdown)
        self.cfg = load_config()
        self.pet = PetWidget()
        self.board = Dashboard()
        self.board._finish_init()
        self.worker = None
        self._closing = False
        self._refresh_pending = False
        self.active_procs = set()
        self.scheduler = SyncScheduler(SYNC_STATE_PATH)
        self.results = {}
        try:
            with open(CACHE_PATH, encoding="utf-8") as stream:
                self.results = json.load(stream)
            for value in self.results.values():
                value["stale"] = True
        except (OSError, ValueError):
            pass
        self.board.set_data(self.cfg, self.results)
        self.pet.set_status(self.results, self.cfg.get("providers", []))

        self.pet.clicked.connect(self.toggle_board)
        self.pet.refresh_requested.connect(self.refresh_all)
        self.board.refresh_requested.connect(self.refresh_all)
        self.board.manual_edited.connect(self.on_manual_edit)

        self.timer = QTimer()           # 调度器: 每 15 秒检查一次谁到期
        self.timer.timeout.connect(self.schedule_tick)
        self.timer.start(15 * 1000)

        self.tray = QSystemTrayIcon(QIcon(ICON), self.pet)
        self.tray.setToolTip("TokenSpy · AI 用量小管家")
        menu = QMenu()
        menu.addAction("打开 / 收起看板", self.toggle_board)
        menu.addAction("找回桌宠", self.restore_pet)
        menu.addAction("立即刷新", self.refresh_all)
        menu.addSeparator()
        menu.addAction("退出", self.qt.quit)
        self.tray.setContextMenu(menu)
        self.tray_menu = menu
        self.tray.activated.connect(lambda reason: self.toggle_board()
                                   if reason == QSystemTrayIcon.Trigger else None)
        self.tray.show()
        self.pet.show()
        self.refresh_all()              # 启动先全量同步一次, 学习各窗口重置时间

    def heartbeat_s(self):
        return max(5, int(self.cfg.get("heartbeat_minutes", 30) or 30)) * 60

    def backoff_base_s(self):
        return max(1, int(self.cfg.get("refresh_minutes", 2) or 2)) * 60

    def toggle_board(self):
        if self.board.isVisible():
            self.board.hide()
            return
        pet = self.pet.frameGeometry()
        scr = (QApplication.screenAt(pet.center()) or QApplication.primaryScreen()).availableGeometry()
        bw, bh, gap = self.board.width(), self.board.height(), 10
        # 候选方位: 优先左/右侧贴边, 其次上/下; 钳进屏幕且不遮桌宠
        candidates = [
            (pet.left() - gap - bw, pet.center().y() - bh // 2),   # 左
            (pet.right() + gap, pet.center().y() - bh // 2),       # 右
            (pet.center().x() - bw // 2, pet.top() - gap - bh),    # 上
            (pet.center().x() - bw // 2, pet.bottom() + gap),      # 下
        ]
        best = None
        for cx, cy in candidates:
            x = min(max(scr.left(), cx), scr.right() - bw + 1)
            y = min(max(scr.top(), cy), scr.bottom() - bh + 1)
            rect = QRect(x, y, bw, bh)
            inter = rect.intersected(pet)
            overlap = inter.width() * inter.height()
            fits = scr.contains(rect)
            if fits and overlap == 0:
                best = rect
                break
            score = (0 if fits else 1, overlap)
            if best is None or score < best[0]:
                best = (score, rect)
        if not isinstance(best, QRect):
            best = best[1]
        self.board.move(best.topLeft())
        self.board.show()
        self.board.raise_()

    def refresh_all(self):
        """手动刷新: 始终同步全部服务。"""
        self.refresh(manual=True)

    def schedule_tick(self):
        if self._closing:
            return
        try:
            self.cfg = load_config()
        except (OSError, json.JSONDecodeError):
            pass
        due = self.scheduler.due(self.cfg.get("providers", []), self.results,
                                 self.active_procs, self.heartbeat_s(),
                                 bool(self.cfg.get("sync_only_active", True)))
        self.update_footer()
        if due:
            self.refresh(only=due)

    def refresh(self, manual=False, only=None):
        if self.worker is not None and self.worker.isRunning():
            self._refresh_pending = True
            return
        try:
            self.cfg = load_config()
        except (OSError, json.JSONDecodeError):
            pass
        configs = [c for c in self.cfg.get("providers", []) if c.get("type") != "manual"]
        if not manual and only is not None:
            configs = [c for c in configs if c["id"] in only]
        if not configs:
            return
        self.pet.busy = True
        self.pet.update()
        self.board.btn_refresh.setText("同步中…")
        self.board.btn_refresh.setEnabled(False)
        self.worker = FetchWorker(configs)
        self.worker.done.connect(self.on_results)
        self.worker.finished.connect(self.after_refresh)
        self.worker.start()

    def on_results(self, results, procs):
        if self._closing:
            return
        self.active_procs = procs or self.active_procs
        self.board.btn_refresh.setText("⟳ 刷新")
        self.board.btn_refresh.setEnabled(True)
        try:
            cfg = load_config()
        except (OSError, json.JSONDecodeError):
            cfg = self.cfg
        # A manual edit may have happened during an API refresh.
        for pcfg in cfg.get("providers", []):
            if pcfg.get("type") == "manual":
                results[pcfg["id"]] = prov.fetch_manual(pcfg)
        self.results = merge_results(self.results, results)
        for pcfg in cfg.get("providers", []):
            if pcfg["id"] in results:
                self.scheduler.record(pcfg["id"], results[pcfg["id"]], pcfg,
                                      self.heartbeat_s(), self.backoff_base_s())
        self.scheduler.save()
        self.save_cache()
        for p in cfg.get("providers", []):
            d = results.get(p["id"], {})
            if d.get("ok") and d.get("total") and d.get("remaining") is not None:
                append_history(p["id"], d["remaining"] / d["total"] * 100.0)
        history = load_history()
        self.board.set_data(cfg, self.results, history)
        self.pet.set_status(self.results, cfg.get("providers", []))
        self.update_footer()

    def save_cache(self):
        try:
            tmp = CACHE_PATH + ".tmp"
            with open(tmp, "w", encoding="utf-8") as stream:
                json.dump(self.results, stream, ensure_ascii=False)
            os.replace(tmp, CACHE_PATH)
        except OSError:
            pass

    def update_footer(self):
        soonest = self.scheduler.soonest()
        when = time.strftime("%H:%M:%S", time.localtime(soonest)) if soonest else "--"
        if self.cfg.get("sync_only_active", True):
            bound = [c for c in self.cfg.get("providers", []) if c.get("processes")]
            active = sum(1 for c in bound if provider_active(c, self.active_procs))
            scope = "活跃应用 %d/%d 个在运行" % (active, len(bound)) if bound else "全部服务"
        else:
            scope = "全部服务"
        self.board.set_sync_info("下次同步 %s · %s · 窗口恢复自动提前" % (when, scope))

    def after_refresh(self):
        if self._refresh_pending and not self._closing:
            self._refresh_pending = False
            QTimer.singleShot(0, self.refresh_all)

    def on_manual_edit(self, pid, remaining, total):
        cfg = load_config()
        for p in cfg.get("providers", []):
            if p["id"] == pid:
                p["remaining"] = remaining
                p["total"] = total
        save_config(cfg)
        self.cfg = cfg
        self.refresh_all()

    def restore_pet(self):
        area = QApplication.primaryScreen().availableGeometry()
        self.pet.move(area.right() - self.pet.width() - 30,
                      area.bottom() - self.pet.height() - 30)
        self.pet.show()
        self.pet.raise_()

    def final_sync(self):
        """退出前: 对活跃/到期的服务做一次快速同步并落盘, 不拖慢退出。"""
        procs = running_processes()
        self.active_procs = procs or self.active_procs
        due = self.scheduler.due(self.cfg.get("providers", []), self.results,
                                 self.active_procs, self.heartbeat_s(),
                                 bool(self.cfg.get("sync_only_active", True)))
        if not due:
            return
        configs = [c for c in self.cfg.get("providers", []) if c["id"] in due]
        results = {}
        with ThreadPoolExecutor(max_workers=4) as pool:
            jobs = {pool.submit(prov.fetch_one, cfg): cfg["id"] for cfg in configs}
            done, _ = wait(jobs, timeout=QUIT_SYNC_BUDGET)
            for job in done:
                try:
                    results[jobs[job]] = job.result(timeout=0)
                except Exception:
                    pass
        if results:
            self.results = merge_results(self.results, results)
            for pcfg in configs:
                if pcfg["id"] in results:
                    self.scheduler.record(pcfg["id"], results[pcfg["id"]], pcfg,
                                          self.heartbeat_s(), self.backoff_base_s())
            self.scheduler.save()
            self.save_cache()

    def shutdown(self):
        self._closing = True
        self.timer.stop()
        self.tray.hide()
        try:
            self.final_sync()
        except Exception:
            pass
        if self.worker is not None and self.worker.isRunning():
            self.worker.wait()

    def run(self):
        sys.exit(self.qt.exec())


if __name__ == "__main__":
    App().run()
