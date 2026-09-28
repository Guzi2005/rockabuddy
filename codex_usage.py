"""Read-only Codex app-server client; never starts a turn or reads auth tokens."""
import json
import os
from pathlib import Path
import queue
import shutil
import subprocess
import threading
import time


def executable():
    found = shutil.which("codex")
    if found and found.lower().endswith(".exe"):
        return found
    root = Path(os.environ.get("LOCALAPPDATA", "")) / "OpenAI" / "Codex" / "bin"
    candidates = list(root.glob("*/codex.exe"))
    if candidates:
        return str(max(candidates, key=lambda p: p.stat().st_mtime))
    raise RuntimeError("未找到 Codex，请先安装并登录 Codex 桌面端")


def read_limits(timeout=25):
    process = subprocess.Popen([executable(), "app-server", "--stdio"],
                               stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                               stderr=subprocess.DEVNULL, text=True, encoding="utf-8",
                               creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    messages = queue.Queue()

    def reader():
        try:
            for line in process.stdout:
                try:
                    messages.put(json.loads(line))
                except json.JSONDecodeError:
                    continue
        finally:
            messages.put(None)

    thread = threading.Thread(target=reader, daemon=True)
    thread.start()
    deadline = time.monotonic() + timeout

    def send(message):
        process.stdin.write(json.dumps(message) + "\n")
        process.stdin.flush()

    def receive(request_id):
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("Codex 用量查询超时")
            try:
                message = messages.get(timeout=remaining)
            except queue.Empty:
                raise TimeoutError("Codex 用量查询超时") from None
            if message is None:
                raise RuntimeError("Codex 查询进程提前结束")
            if message.get("id") == request_id:
                if "error" in message:
                    # Do not surface raw upstream errors that might contain account data.
                    raise RuntimeError("Codex 额度查询失败，请确认已使用 ChatGPT 账户登录")
                return message.get("result", {})

    try:
        send({"id": 1, "method": "initialize", "params": {
            "clientInfo": {"name": "tokenspy", "title": "TokenSpy", "version": "0.2.0"}}})
        receive(1)
        send({"method": "initialized"})
        send({"id": 2, "method": "account/rateLimits/read", "params": {"excludeResetCreditDetails": True}})
        return receive(2)
    finally:
        process.stdin.close()
        if process.poll() is None:
            process.terminate()
        try:
            process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=3)
        thread.join(timeout=1)
        process.stdout.close()


def normalize_limits(payload):
    buckets = payload.get("rateLimitsByLimitId")
    if not buckets:
        old = payload.get("rateLimits")
        buckets = {old.get("limitId") or "codex": old} if old else {}
    windows = []
    for key, bucket in buckets.items():
        if not isinstance(bucket, dict):
            continue
        for field in ("primary", "secondary"):
            window = bucket.get(field)
            if not window or not isinstance(window.get("usedPercent"), (int, float)):
                continue
            minutes = window.get("windowDurationMins")
            label = {300: "5 小时", 10080: "本周"}.get(minutes)
            if not label:
                label = ("%g 小时" % (minutes / 60)) if minutes else "额度窗口"
            windows.append({"id": key + ":" + field, "label": label,
                            "bucket": bucket.get("limitName") or key,
                            "remaining_percent": max(0, min(100, 100 - window["usedPercent"])),
                            "used_percent": window["usedPercent"], "duration_minutes": minutes,
                            "resets_at": window.get("resetsAt")})
    if not windows:
        raise RuntimeError("账户尚未返回额度窗口（API Key 登录不提供订阅额度）")
    core = [w for w in windows if w["id"].startswith("codex:")] or windows
    return {"ok": True, "remaining": min(w["remaining_percent"] for w in core),
            "total": 100, "unit": "%", "windows": windows,
            "note": "Codex 账户实时额度", "source": "Codex app-server", "error": None}
