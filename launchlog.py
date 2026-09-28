# -*- coding: utf-8 -*-
"""「XX，启动！」瞬间的 JSON 记录: 每次举图标都记下来, 供「快端上来罢」叠叠乐回放。"""
import json
import os
import time
from pathlib import Path

MAX_ENTRIES = 1000


def _path():
    return Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "TokenSpy" / "launches.json"


def record_launch(kind, key, name):
    """kind: 'app'(key=exe 路径) / 'site'(key=站点 id)。"""
    entry = {"ts": round(time.time(), 3), "kind": kind, "id": key, "name": name}
    try:
        entries = load_launches()
        entries.append(entry)
        if len(entries) > MAX_ENTRIES:
            entries = entries[-MAX_ENTRIES:]
        path = _path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(entries, ensure_ascii=False, indent=1), encoding="utf-8")
    except OSError:
        pass


def load_launches():
    try:
        data = json.loads(_path().read_text(encoding="utf-8"))
        return [e for e in data if isinstance(e, dict) and e.get("id")]
    except (OSError, ValueError):
        return []


def recent_unique(limit=8):
    """按最近使用去重, 最新的排最前。"""
    seen = set()
    out = []
    for entry in reversed(load_launches()):
        if entry["id"] in seen:
            continue
        seen.add(entry["id"])
        out.append(entry)
        if len(out) >= limit:
            break
    return out
