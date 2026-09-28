"""Extract app icons from exe/lnk files as transparent PNGs. Pure ctypes, no deps.

PrivateExtractIconsW -> HICON -> 32bpp top-down DIB -> DrawIconEx -> pixels.
The DIB memory is little-endian BGRA, which is exactly QImage.Format_ARGB32
on Windows, so the alpha channel survives the whole way.
"""
import ctypes
import ctypes.wintypes as wt
import json
import os
import subprocess
import sys

CACHE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                         "assets", "icons", "apps")

user32 = ctypes.windll.user32
gdi32 = ctypes.windll.gdi32
kernel32 = ctypes.windll.kernel32
shell32 = ctypes.windll.shell32

user32.PrivateExtractIconsW.argtypes = [wt.LPCWSTR, ctypes.c_int, ctypes.c_int,
                                        ctypes.c_int,
                                        ctypes.POINTER(wt.HICON),
                                        ctypes.POINTER(ctypes.c_uint),
                                        ctypes.c_uint, ctypes.c_uint]
user32.PrivateExtractIconsW.restype = ctypes.c_uint
user32.DrawIconEx.argtypes = [wt.HDC, ctypes.c_int, ctypes.c_int, wt.HICON,
                              ctypes.c_int, ctypes.c_int, ctypes.c_uint,
                              wt.HBRUSH, ctypes.c_uint]
user32.DrawIconEx.restype = wt.BOOL
user32.DestroyIcon.argtypes = [wt.HICON]
user32.GetDC.argtypes = [wt.HWND]
user32.GetDC.restype = wt.HDC
user32.ReleaseDC.argtypes = [wt.HWND, wt.HDC]
# GDI 句柄在 64 位下可能超过 2^31, 必须显式声明为指针,
# 否则 ctypes 默认按 c_int 转换会 OverflowError(时好时坏)
gdi32.CreateCompatibleDC.argtypes = [wt.HDC]
gdi32.CreateCompatibleDC.restype = wt.HDC
gdi32.CreateDIBSection.argtypes = [wt.HDC, ctypes.c_void_p, ctypes.c_uint,
                                   ctypes.c_void_p, wt.HANDLE, wt.DWORD]
gdi32.CreateDIBSection.restype = wt.HBITMAP
gdi32.SelectObject.argtypes = [wt.HDC, wt.HGDIOBJ]
gdi32.SelectObject.restype = wt.HGDIOBJ
gdi32.DeleteObject.argtypes = [wt.HGDIOBJ]
gdi32.DeleteDC.argtypes = [wt.HDC]

DI_NORMAL = 0x0003
LR_DEFAULTCOLOR = 0x0000


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [("biSize", wt.DWORD), ("biWidth", wt.LONG), ("biHeight", wt.LONG),
                ("biPlanes", wt.WORD), ("biBitCount", wt.WORD),
                ("biCompression", wt.DWORD), ("biSizeImage", wt.DWORD),
                ("biXPelsPerMeter", wt.LONG), ("biYPelsPerMeter", wt.LONG),
                ("biClrUsed", wt.DWORD), ("biClrImportant", wt.DWORD)]


class BITMAPINFO(ctypes.Structure):
    _fields_ = [("bmiHeader", BITMAPINFOHEADER), ("bmiColors", wt.DWORD * 1)]


def _exe_from_shortcut(lnk_path):
    """Resolve a .lnk to an icon-bearing exe via WScript.Shell COM (PowerShell).

    注意 json.dumps 必须 ensure_ascii=False: PowerShell 不认 \\uXXXX 转义,
    中文快捷方式名会被当成字面量导致解析为空。
    """
    ps = ("$s=(New-Object -COM WScript.Shell).CreateShortcut(%s);"
          "Write-Output $s.TargetPath; Write-Output $s.IconLocation"
          % json.dumps(lnk_path, ensure_ascii=False))
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        out = subprocess.run(["powershell", "-NoProfile", "-Command", ps],
                             capture_output=True, text=True, timeout=15,
                             creationflags=flags)
    except (OSError, subprocess.SubprocessError):
        return None
    lines = [ln.strip() for ln in out.stdout.splitlines()]
    target = lines[0] if lines else ""
    if target and os.path.isfile(target):
        return target
    # 系统类快捷方式(如文件资源管理器)没有 TargetPath, 图标在 IconLocation
    if len(lines) > 1 and lines[1]:
        icon_src = os.path.expandvars(lines[1].split(",")[0].strip())
        if icon_src and os.path.isfile(icon_src):
            return icon_src
    return None


def _hicon_to_png(hicon, size):
    """Draw an HICON onto a 32bpp top-down DIB and save the pixels as PNG."""
    from PySide6.QtGui import QImage
    screen = user32.GetDC(None)
    memdc = gdi32.CreateCompatibleDC(screen)
    info = BITMAPINFO()
    info.bmiHeader.biSize = ctypes.sizeof(BITMAPINFOHEADER)
    info.bmiHeader.biWidth = size
    info.bmiHeader.biHeight = -size          # 顶向下: 行序从上到下
    info.bmiHeader.biPlanes = 1
    info.bmiHeader.biBitCount = 32
    info.bmiHeader.biCompression = 0         # BI_RGB
    bits = ctypes.c_void_p()
    hbmp = gdi32.CreateDIBSection(memdc, ctypes.byref(info), 0,
                                  ctypes.byref(bits), None, 0)
    if not hbmp:
        gdi32.DeleteDC(memdc)
        user32.ReleaseDC(None, screen)
        return None
    old = gdi32.SelectObject(memdc, hbmp)
    # 透明底: 全部清零后 DrawIconEx 只画图标本身
    ctypes.memset(bits, 0, size * size * 4)
    ok = user32.DrawIconEx(memdc, 0, 0, hicon, size, size, 0, None, DI_NORMAL)
    data = ctypes.string_at(bits, size * size * 4)
    gdi32.SelectObject(memdc, old)
    gdi32.DeleteObject(hbmp)
    gdi32.DeleteDC(memdc)
    user32.ReleaseDC(None, screen)
    if not ok:
        return None
    image = QImage(data, size, size, size * 4, QImage.Format_ARGB32).copy()
    return image


def extract_icon_png(source, size=64, out_path=None):
    """Extract the main icon of an exe (or a .lnk's target) to a transparent PNG.

    Returns the PNG path on success, None otherwise. Caches by exe basename.
    """
    if source.lower().endswith(".lnk"):
        target = _exe_from_shortcut(source)
        if not target:
            return None
        source = target
    if not os.path.isfile(source):
        return None
    name = os.path.splitext(os.path.basename(source))[0].lower()
    os.makedirs(CACHE_DIR, exist_ok=True)
    png = out_path or os.path.join(CACHE_DIR, name + ".png")
    if os.path.isfile(png):
        return png
    hicon = wt.HICON()
    icon_id = ctypes.c_uint()
    got = user32.PrivateExtractIconsW(source, 0, size, size,
                                      ctypes.byref(hicon), ctypes.byref(icon_id),
                                      1, LR_DEFAULTCOLOR)
    if not got or not hicon:
        # 这个 exe 没有内嵌大图标, 退回 Shell 关联图标
        return None
    try:
        image = _hicon_to_png(hicon, size)
    finally:
        user32.DestroyIcon(hicon)
    if image is None:
        return None
    # 全透明说明提取失败
    if not image.hasAlphaChannel():
        pass
    image.save(png, "PNG")
    return png if os.path.isfile(png) else None


def taskbar_shortcuts():
    """Pinned taskbar shortcuts: the apps the user actually uses daily."""
    folder = os.path.join(os.environ.get("APPDATA", ""),
                          "Microsoft", "Internet Explorer", "Quick Launch",
                          "User Pinned", "TaskBar")
    if not os.path.isdir(folder):
        return []
    return [os.path.join(folder, f) for f in os.listdir(folder)
            if f.lower().endswith(".lnk")]


def harvest_taskbar_icons(size=64):
    """Extract icons for every pinned taskbar app into the cache dir."""
    results = {}
    for lnk in taskbar_shortcuts():
        name = os.path.splitext(os.path.basename(lnk))[0]
        png = extract_icon_png(lnk, size=size)
        results[name] = png
    return results


# 常用网站: 窗口标题关键词(小写) -> (站点 id, 域名)
SITES = {
    "canva": ("canva", "canva.com"), "可画": ("canva", "canva.com"),
    "bilibili": ("bilibili", "bilibili.com"), "哔哩哔哩": ("bilibili", "bilibili.com"),
    "youtube": ("youtube", "youtube.com"),
    "github": ("github", "github.com"),
    "知乎": ("zhihu", "zhihu.com"),
    "figma": ("figma", "figma.com"),
    "notion": ("notion", "notion.so"),
    "微博": ("weibo", "weibo.com"),
    "小红书": ("xiaohongshu", "xiaohongshu.com"),
    "chatgpt": ("chatgpt", "chatgpt.com"),
    "claude": ("claude", "claude.ai"),
    "deepseek": ("deepseek", "deepseek.com"),
    "抖音": ("douyin", "douyin.com"),
    "淘宝": ("taobao", "taobao.com"), "天猫": ("taobao", "tmall.com"),
    "京东": ("jd", "jd.com"),
    "百度": ("baidu", "baidu.com"),
    "贴吧": ("tieba", "tieba.baidu.com"),
    "网易云音乐": ("cloudmusic-web", "music.163.com"),
    "腾讯视频": ("qqvideo", "v.qq.com"), "爱奇艺": ("iqiyi", "iqiyi.com"),
    "优酷": ("youku", "youku.com"), "acfun": ("acfun", "acfun.cn"),
    "掘金": ("juejin", "juejin.cn"), "csdn": ("csdn", "csdn.net"),
    "pixiv": ("pixiv", "pixiv.net"),
    "reddit": ("reddit", "reddit.com"),
    "steam": ("steam", "store.steampowered.com"),
    "飞书": ("feishu", "feishu.cn"), "钉钉": ("dingtalk", "dingtalk.com"),
    "hoyolab": ("hoyolab", "hoyolab.com"), "米游社": ("hoyolab", "miyoushe.com"),
    "stackoverflow": ("stackoverflow", "stackoverflow.com"),
    "kimi": ("kimi-web", "kimi.com"),
}


def match_site(title):
    """窗口标题命中常用网站则返回站点 id。"""
    low = title.lower()
    for keyword, (site_id, _domain) in SITES.items():
        if keyword in low:
            return site_id
    return None


def fetch_domain_icon(domain, size=64):
    """任意域名的 favicon: 先读缓存, 再 google s2 / /favicon.ico 联网补。"""
    os.makedirs(CACHE_DIR, exist_ok=True)
    from sitehist import icon_cache_path
    png = icon_cache_path(CACHE_DIR, domain)
    if os.path.isfile(png):
        return png
    import urllib.request
    from PySide6.QtGui import QPixmap
    urls = ["https://www.google.com/s2/favicons?domain=%s&sz=%d" % (domain, size),
            "https://%s/favicon.ico" % domain]
    for url in urls:
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = resp.read()
            pix = QPixmap()
            if pix.loadFromData(data) and not pix.isNull():
                pix.scaled(size, size).save(png, "PNG")
                if os.path.isfile(png):
                    return png
        except Exception:
            continue
    return None


def fetch_site_icon(site_id, size=64):
    """下载站点 favicon 并缓存为 PNG; 返回路径或 None。site_id 也接受裸域名。"""
    if "." in site_id:  # 通用域名(历史库反查出来的网站)
        return fetch_domain_icon(site_id, size)
    os.makedirs(CACHE_DIR, exist_ok=True)
    png = os.path.join(CACHE_DIR, "site-%s.png" % site_id)
    if os.path.isfile(png):
        return png
    domain = None
    for _kw, (sid, dom) in SITES.items():
        if sid == site_id:
            domain = dom
            break
    if not domain:
        return None
    import urllib.request
    from PySide6.QtGui import QPixmap
    urls = ["https://www.google.com/s2/favicons?domain=%s&sz=%d" % (domain, size),
            "https://%s/favicon.ico" % domain]
    for url in urls:
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = resp.read()
            pix = QPixmap()
            if pix.loadFromData(data) and not pix.isNull():
                pix.scaled(size, size).save(png, "PNG")
                if os.path.isfile(png):
                    return png
        except Exception:
            continue
    return None


# 站点显示名(气泡用)
SITE_NAMES = {
    "canva": "Canva", "bilibili": "B站", "youtube": "YouTube",
    "github": "GitHub", "zhihu": "知乎", "figma": "Figma",
    "notion": "Notion", "weibo": "微博", "xiaohongshu": "小红书",
    "chatgpt": "ChatGPT", "claude": "Claude", "deepseek": "DeepSeek",
    "douyin": "抖音", "taobao": "淘宝", "jd": "京东", "baidu": "百度",
    "tieba": "贴吧", "cloudmusic-web": "网易云音乐", "qqvideo": "腾讯视频",
    "iqiyi": "爱奇艺", "youku": "优酷", "acfun": "AcFun",
    "juejin": "掘金", "csdn": "CSDN", "pixiv": "Pixiv",
    "reddit": "Reddit", "steam": "Steam", "feishu": "飞书",
    "dingtalk": "钉钉", "hoyolab": "米游社", "stackoverflow": "StackOverflow",
    "kimi-web": "Kimi",
}

# 常见 exe 显示名(气泡用), 未收录的用 exe 名原样
APP_NAMES = {
    "cloudmusic": "网易云音乐", "weixin": "微信", "qq": "QQ",
    "msedge": "Edge", "ksolaunch": "WPS", "wps": "WPS",
    "notepad": "记事本", "explorer": "资源管理器", "eagle": "Eagle",
    "obsidian": "Obsidian", "zotero": "Zotero", "kimi": "Kimi",
    "telegram": "Telegram", "code": "VS Code", "cursor": "Cursor",
}


def app_display_name(exe_path):
    import os as _os
    stem = _os.path.splitext(_os.path.basename(exe_path))[0]
    return APP_NAMES.get(stem.lower(), stem)


def site_display_name(site_id):
    return SITE_NAMES.get(site_id, site_id)


if __name__ == "__main__":
    # 手动收割: python icons.py  [exe_or_lnk ...]
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
    app = QApplication(sys.argv[:1])
    if len(sys.argv) > 1:
        for src in sys.argv[1:]:
            print(src, "->", extract_icon_png(src))
    else:
        for name, png in harvest_taskbar_icons().items():
            print(name, "->", png)