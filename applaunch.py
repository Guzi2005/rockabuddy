"""Watch for newly opened apps and known websites in browser tabs.

Polls visible top-level windows. New app exes emit `launched`; browser window
titles matching known sites (icons.SITES) emit `site_opened` after fetching the
site favicon (download happens here in the worker thread, GUI stays snappy).

Baseline snapshot at start; per-exe / per-site cooldown to avoid spam.
Pure ctypes EnumWindows, no dependencies.
"""
import ctypes
import ctypes.wintypes as wt
import os
import time
from PySide6.QtCore import QThread, Signal

user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32

# 壳进程/系统窗口: 弹出来不算「打开新应用」
IGNORE = {
    "python.exe", "pythonw.exe", "explorer.exe", "svchost.exe",
    "applicationframehost.exe", "textinputhost.exe", "searchhost.exe",
    "shellexperiencehost.exe", "startmenuexperiencehost.exe", "systemsettings.exe",
    "lockapp.exe", "wmiprvse.exe", "backgroundtaskhost.exe", "runtimebroker.exe",
    "dllhost.exe", "conhost.exe", "powershell.exe", "cmd.exe", "wt.exe",
    "windowsterminal.exe", "tabtip.exe", "ctfmon.exe", "securityhealthsystray.exe",
    "taskmgr.exe", "snippingtool.exe",
}
# 浏览器: 标题变化要追踪网站
BROWSERS = {"msedge.exe", "chrome.exe", "firefox.exe", "arc.exe", "brave.exe",
            "opera.exe", "vivaldi.exe", "360se.exe", "qqbrowser.exe"}
COOLDOWN_S = 300          # 同一应用/网站冷却期, 防刷屏
POLL_S = 1.0


def _visible_windows():
    """可见、带标题顶层窗口: [(hwnd, exe 路径, 标题)]。"""
    wins = []
    EnumProc = ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)
    GetText = user32.GetWindowTextW

    def visit(hwnd, _):
        if not user32.IsWindowVisible(hwnd):
            return True
        length = user32.GetWindowTextLengthW(hwnd)
        if length == 0:
            return True
        buf = ctypes.create_unicode_buffer(length + 1)
        GetText(hwnd, buf, length + 1)
        title = buf.value
        pid = wt.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        handle = kernel32.OpenProcess(0x1000, False, pid.value)  # QUERY_LIMITED_INFORMATION
        if not handle:
            return True
        try:
            pbuf = ctypes.create_unicode_buffer(wt.MAX_PATH * 2)
            size = wt.DWORD(len(pbuf))
            if kernel32.QueryFullProcessImageNameW(handle, 0, pbuf, ctypes.byref(size)):
                wins.append((hwnd, pbuf.value, title))
        finally:
            kernel32.CloseHandle(handle)
        return True

    user32.EnumWindows(EnumProc(visit), 0)
    return wins


class AppWatcher(QThread):
    """后台轮询; 新应用发射 exe 全路径, 常用网站发射站点 id,
    白名单外的网页经历史库反查后发射 (域名, 显示名, favicon 路径)。"""

    launched = Signal(str)
    site_opened = Signal(str)
    page_opened = Signal(str, str, str)

    def __init__(self, parent=None, extra_ignore=()):
        super().__init__(parent)
        self._running = True
        self._ignore = IGNORE | {n.lower() for n in extra_ignore}
        self._seen = {}            # exe basename / "site:<id>" / "lookup:<标题>" -> 上次触发时刻
        self._baseline = set()
        self._browser_site = {}    # hwnd -> 当前页面键(站点 id 或 "page:"+标题)

    def _fresh(self, key, now, cooldown=COOLDOWN_S):
        if now - self._seen.get(key, -1e9) < cooldown:
            return False
        self._seen[key] = now
        return True

    @staticmethod
    def _browser_key(title):
        """浏览器窗口当前页面键: 白名单站点 id 或去掉浏览器后缀的页面标题。"""
        from icons import match_site
        from sitehist import strip_browser_suffix
        site = match_site(title)
        return site if site else "page:" + strip_browser_suffix(title)

    def run(self):
        from icons import fetch_site_icon, CACHE_DIR
        from sitehist import lookup_page
        try:
            base = _visible_windows()
        except Exception:
            base = []
        self._baseline = {os.path.basename(p).lower() for _h, p, _t in base}
        for hwnd, path, title in base:
            name = os.path.basename(path).lower()
            if name in BROWSERS:
                self._browser_site[hwnd] = self._browser_key(title)
        while self._running:
            time.sleep(POLL_S)
            if not self._running:
                break
            try:
                wins = _visible_windows()
            except Exception:
                continue
            now = time.monotonic()
            live_hwnds = set()
            for hwnd, path, title in wins:
                name = os.path.basename(path).lower()
                if name in BROWSERS:
                    live_hwnds.add(hwnd)
                    key = self._browser_key(title)
                    if key == self._browser_site.get(hwnd):
                        continue
                    self._browser_site[hwnd] = key
                    if not key.startswith("page:"):
                        # 白名单网站: 先下图标(线程内), 再通知桌宠
                        if self._fresh("site:" + key, now) and fetch_site_icon(key):
                            self.site_opened.emit(key)
                    elif self._fresh("lookup:" + key, now, 60):
                        # 任意网页: 历史库反查域名+favicon, 域名级冷却防刷屏
                        info = lookup_page(title, CACHE_DIR)
                        if info and self._fresh("site:" + info["key"], now):
                            self.page_opened.emit(info["key"], info["name"],
                                                  info["icon"] or "")
                    continue
                if name in self._baseline or name in self._ignore:
                    continue
                if not os.path.isfile(path):
                    continue
                self._baseline.add(name)   # 见过即入基线, 不反复触发
                if self._fresh(name, now):
                    self.launched.emit(path)
            # 关掉的浏览器窗口从追踪里移除
            for hwnd in [h for h in self._browser_site if h not in live_hwnds]:
                del self._browser_site[hwnd]

    def stop(self):
        self._running = False


if __name__ == "__main__":
    # 手动试跑: 之后打开的新应用/常用网站会打印出来
    import sys
    from PySide6.QtWidgets import QApplication
    app = QApplication(sys.argv)
    w = AppWatcher()
    w.launched.connect(lambda p: print("LAUNCHED:", p))
    w.site_opened.connect(lambda s: print("SITE:", s))
    w.page_opened.connect(lambda k, n, i: print("PAGE:", k, n, i))
    w.start()
    sys.exit(app.exec())
