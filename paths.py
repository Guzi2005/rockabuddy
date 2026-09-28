"""资源与数据路径解析。

源码运行:两者都等于项目根目录。
PyInstaller onefile:只读资源(assets 图集/图标)在 _MEIPASS 解包目录,
config/history 等可写数据放在 exe 旁边,升级换 exe 不丢数据。
"""
import os
import sys

if getattr(sys, "frozen", False):
    BUNDLE_DIR = getattr(sys, "_MEIPASS", os.path.dirname(sys.executable))
    DATA_DIR = os.path.dirname(os.path.abspath(sys.executable))
else:
    BUNDLE_DIR = DATA_DIR = os.path.dirname(os.path.abspath(__file__))


def resource_path(*parts):
    """只读资源路径(随 exe 打包)。"""
    return os.path.join(BUNDLE_DIR, *parts)


def data_path(*parts):
    """可写数据路径(config/history/缓存, 在 exe 或源码目录旁)。"""
    return os.path.join(DATA_DIR, *parts)
