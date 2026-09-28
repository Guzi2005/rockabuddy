"""Windows DPAPI encrypted API keys, scoped to the current OS user."""
import ctypes
from ctypes import wintypes
import json
import os
from pathlib import Path
import base64


class Blob(ctypes.Structure):
    _fields_ = [("size", wintypes.DWORD), ("data", ctypes.POINTER(ctypes.c_ubyte))]


def _crypt(data, encrypt):
    if os.name != "nt":
        raise RuntimeError("此版本的密钥存储需要 Windows")
    buffer = ctypes.create_string_buffer(data)
    source = Blob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte)))
    target = Blob()
    dll = ctypes.windll.crypt32
    fn = dll.CryptProtectData if encrypt else dll.CryptUnprotectData
    if not fn(ctypes.byref(source), None, None, None, None, 1, ctypes.byref(target)):
        raise OSError("Windows 密钥加密操作失败")
    try:
        return ctypes.string_at(target.data, target.size)
    finally:
        ctypes.windll.kernel32.LocalFree(target.data)


def _path():
    return Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "TokenSpy" / "secrets.json"


def read_secret(provider_id):
    try:
        values = json.loads(_path().read_text(encoding="utf-8"))
        return _crypt(base64.b64decode(values[provider_id]), False).decode("utf-8")
    except (OSError, ValueError, KeyError):
        return ""


def save_secret(provider_id, key):
    path = _path()
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        values = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        values = {}
    if key:
        values[provider_id] = base64.b64encode(_crypt(key.encode("utf-8"), True)).decode("ascii")
    else:
        values.pop(provider_id, None)
    temp = path.with_suffix(".tmp")
    temp.write_text(json.dumps(values), encoding="utf-8")
    temp.replace(path)
