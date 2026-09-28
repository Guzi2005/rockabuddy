# -*- coding: utf-8 -*-
"""任意网页的站点识别: 用 Chromium 系浏览器的历史库把窗口标题反查成 URL,
进而拿到域名和真 favicon(Favicons 库里的 PNG blob), 不必再维护站点白名单。

浏览器运行时 History/Favicons 被独占锁定, 先连 WAL 一起复制到临时目录再只读查询。
"""
import glob
import hashlib
import os
import shutil
import sqlite3
import tempfile
import time
from urllib.parse import urlparse

# 窗口标题尾巴上的浏览器名: "页面标题 - Microsoft Edge"
BROWSER_SUFFIXES = (
    "Microsoft Edge", "Google Chrome", "Mozilla Firefox", "Arc",
    "Brave", "Opera", "Vivaldi", "360安全浏览器", "QQ浏览器",
)
# 这些标题不是网页, 不反查
JUNK_TITLES = {
    "新标签页", "新建标签页", "New Tab", "about:blank", "主页", "Home",
    "设置", "Settings", "扩展程序", "Extensions", "历史记录", "History",
    "下载内容", "Downloads", "书签", "Bookmarks",
}

_PROFILE_ROOTS = [
    ("Microsoft", "Edge"), ("Google", "Chrome"), ("BraveSoftware", "Brave-Browser"),
    ("Vivaldi", ""), ("360Chrome", "Chrome"), ("Tencent", "QQBrowser"),
]

_tmp = None


def strip_browser_suffix(title):
    """去掉窗口标题尾部的浏览器名, 得到纯页面标题。"""
    t = title.strip()
    for suf in BROWSER_SUFFIXES:
        for sep in (" - ", " — ", " – "):
            tail = sep + suf
            if t.lower().endswith(tail.lower()):
                return t[:-len(tail)].strip()
    return t


def page_display_name(page_title, domain):
    """气泡显示名: 页面标题的最后一段(网站常把站名放最后), 太长就退到域名主体。"""
    for sep in (" - ", " — ", " – ", " | ", " · ", "_"):
        if sep in page_title:
            last = page_title.rsplit(sep, 1)[-1].strip()
            if 1 < len(last) <= 14:
                return last
    stem = domain.split(".")[0] if domain else page_title[:12]
    return stem or page_title[:12]


def icon_cache_path(cache_dir, domain):
    """域名图标的统一缓存路径(sitehist 与 icons.fetch_site_icon 共用)。"""
    digest = hashlib.md5(domain.encode("utf-8")).hexdigest()[:10]
    return os.path.join(cache_dir, "site-dom-%s.png" % digest)


def _profiles():
    """所有 Chromium 配置目录: [(profile_dir, history_db)]。"""
    local = os.environ.get("LOCALAPPDATA", "")
    out = []
    for vendor, product in _PROFILE_ROOTS:
        root = os.path.join(local, vendor, product, "User Data") if product \
            else os.path.join(local, vendor, "User Data")
        for hist in glob.glob(os.path.join(root, "*", "History")):
            out.append(os.path.dirname(hist))
    return out


def _copy_db(path):
    """复制 sqlite(连同 WAL/SHM)到临时目录, 返回副本路径。"""
    global _tmp
    if _tmp is None:
        _tmp = tempfile.mkdtemp(prefix="tokenspy-hist-")
    dest = os.path.join(_tmp, "db%d" % abs(hash(path + str(time.time()))))
    shutil.copy2(path, dest)
    for suffix in ("-wal", "-shm"):
        if os.path.isfile(path + suffix):
            shutil.copy2(path + suffix, dest + suffix)
    return dest


def _query(db_path, sql, args):
    copy = _copy_db(db_path)
    conn = sqlite3.connect(copy)
    try:
        return conn.execute(sql, args).fetchall()
    finally:
        conn.close()


def _history_lookup(profile, page_title):
    """在单个配置的 History 里按页面标题找最近的 http(s) 访问记录。"""
    hist = os.path.join(profile, "History")
    if not os.path.isfile(hist):
        return None
    try:
        rows = _query(hist,
                      "SELECT url, title FROM urls WHERE title = ? "
                      "ORDER BY last_visit_time DESC LIMIT 1", (page_title,))
        if not rows:  # 窗口标题偶有截断/装饰, 退一步前缀匹配
            rows = _query(hist,
                          "SELECT url, title FROM urls WHERE title LIKE ? "
                          "ORDER BY last_visit_time DESC LIMIT 1",
                          (page_title[:24] + "%",))
    except (OSError, sqlite3.Error):
        return None
    for url, _title in rows:
        host = urlparse(url).hostname or ""
        if url.startswith(("http://", "https://")) and "." in host:
            return url, host.lower()
    return None


def _favicon_from_db(profile, url, cache_png, size):
    """从 Favicons 库取该页面的 favicon 位图(按尺寸取最大的一张)。"""
    fav = os.path.join(profile, "Favicons")
    if not os.path.isfile(fav):
        return None
    try:
        rows = _query(fav,
                      "SELECT fb.image_data FROM favicon_bitmaps fb "
                      "JOIN icon_mapping m ON m.icon_id = fb.icon_id "
                      "WHERE m.page_url = ? OR m.page_url LIKE ? "
                      "ORDER BY fb.width DESC LIMIT 3",
                      (url, url + "%"))
    except (OSError, sqlite3.Error):
        return None
    from PySide6.QtGui import QPixmap
    for (blob,) in rows:
        if not blob:
            continue
        pix = QPixmap()
        if pix.loadFromData(bytes(blob)) and not pix.isNull():
            pix.scaled(size, size).save(cache_png, "PNG")
            if os.path.isfile(cache_png):
                return cache_png
    return None


def lookup_page(window_title, cache_dir, size=64):
    """窗口标题 -> dict(key=域名, name=显示名, icon=favicon路径) 或 None。"""
    page_title = strip_browser_suffix(window_title)
    if len(page_title) < 3 or page_title in JUNK_TITLES:
        return None
    for profile in _profiles():
        hit = _history_lookup(profile, page_title)
        if not hit:
            continue
        url, domain = hit
        os.makedirs(cache_dir, exist_ok=True)
        png = icon_cache_path(cache_dir, domain)
        if not os.path.isfile(png):
            got = _favicon_from_db(profile, url, png, size)
            if not got:
                from icons import fetch_domain_icon  # 库里没有就联网补
                got = fetch_domain_icon(domain, size)
            if not got:
                png = None
        return {"key": domain, "name": page_display_name(page_title, domain),
                "icon": png}
    return None
