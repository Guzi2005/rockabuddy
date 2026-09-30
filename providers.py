# -*- coding: utf-8 -*-
"""
providers.py — 各服务商余额/余量查询适配器

约定:
- 每个适配器返回 dict:
    {
      "ok": bool,
      "remaining": float | None,   # 剩余量(主单位)
      "total": float | None,       # 总量, 无则 None
      "unit": str,                 # "¥" / "次" / "requests" ...
      "note": str,                 # 附加说明, 如 "含赠送金 ¥3.2"
      "error": str | None,
    }
- windows 只包含服务实际返回的额度；禁止推算套餐窗口、余额或重置日。
- type == "manual" 的服务不发起网络请求, 直接读 config 里的 remaining/total。
- 全部用标准库 urllib, 不引入第三方依赖。
"""

import json
import math
import os
import shutil
import sqlite3
import tempfile
import time
import urllib.parse
import urllib.request
import urllib.error

TIMEOUT = 10


def _get_json(url, headers=None):
    req = urllib.request.Request(url, headers=headers or {})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _bearer(key):
    return {"Authorization": "Bearer " + key, "Content-Type": "application/json"}


# ---------------- 通用: 重置时间 + 双轨窗口(全平台统一模板) ----------------
def _monthly_reset_ts(reset_day, now=None):
    """根据每月重置日(1-31)算出下次重置时间戳; 本月已过则推到下月。"""
    from datetime import datetime
    now = time.time() if now is None else now
    dt = datetime.fromtimestamp(now)
    day = max(1, min(28, int(reset_day)))
    try:
        this_month = dt.replace(day=day, hour=0, minute=0, second=0, microsecond=0)
    except ValueError:
        this_month = dt.replace(day=28, hour=0, minute=0, second=0, microsecond=0)
    if this_month.timestamp() > now:
        return int(this_month.timestamp())
    nxt = this_month
    while nxt.timestamp() <= now:
        if nxt.month == 12:
            nxt = nxt.replace(year=nxt.year + 1, month=1)
        else:
            nxt = nxt.replace(month=nxt.month + 1)
    return int(nxt.timestamp())


def _rolling_5h_reset_ts(now=None):
    """5 小时滚动窗口 = 当前时刻后向取整到 5h 倍数再 +5h。"""
    now = time.time() if now is None else now
    slot = int(now // (5 * 3600))
    return int((slot + 1) * 5 * 3600)


def _history_5h_delta(pid):
    """读取 tokenspy history.jsonl, 估算近 5 小时的 pct 下降值; 无历史返回 None。
    用于把"仅有月度"的接口补齐为 5h+月度 双轨。"""
    hist = os.path.join(os.path.dirname(os.path.abspath(__file__)), "history.jsonl")
    if not os.path.exists(hist):
        return None
    try:
        rows = []
        with open(hist, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    rows.append(json.loads(line))
    except Exception:
        return None
    cutoff = time.time() - 5 * 3600
    samples = sorted(
        [(r.get("ts", 0), float(r.get("pct") or 0))
         for r in rows if r.get("id") == pid and r.get("ts", 0) >= cutoff]
    )
    if len(samples) < 2:
        return None
    drop = max(0.0, samples[0][1] - samples[-1][1])
    return max(0.0, min(100.0, 100.0 - drop))


def _fallback_pct_from_cfg(cfg):
    """从 config.json 的 remaining/total 提取月度 pct 兜底。"""
    rem = cfg.get("remaining")
    tot = cfg.get("total")
    if isinstance(rem, (int, float)):
        if isinstance(tot, (int, float)) and tot > 0:
            return max(0.0, min(100.0, float(rem) / float(tot) * 100.0))
        return max(0.0, min(100.0, float(rem)))
    return None


def _dual_windows(bucket, monthly_pct, monthly_reset_ts,
                  fiveh_pct=None, now=None):
    """按 Codex 卡片标准构造「5 小时 + 月度」双轨窗口列表。
    5h 窗口: duration_minutes=300, 沿时间滚; 月度: duration_minutes=None。"""
    now = time.time() if now is None else now
    if fiveh_pct is None:
        fiveh_pct = monthly_pct
    fiveh_pct = max(0.0, min(100.0, float(fiveh_pct)))
    monthly_pct = max(0.0, min(100.0, float(monthly_pct)))
    return [
        {
            "id": "%s:5h" % bucket.lower(),
            "label": "5 小时窗口",
            "bucket": bucket,
            "remaining_percent": fiveh_pct,
            "used_percent": 100.0 - fiveh_pct,
            "duration_minutes": 300,
            "resets_at": _rolling_5h_reset_ts(now),
        },
        {
            "id": "%s:monthly" % bucket.lower(),
            "label": "月度额度",
            "bucket": bucket,
            "remaining_percent": monthly_pct,
            "used_percent": 100.0 - monthly_pct,
            "duration_minutes": None,
            "resets_at": int(monthly_reset_ts),
        },
    ]


# ---------------- 硅基流动 SiliconFlow ----------------
def fetch_siliconflow(cfg):
    key = get_api_key(cfg, "SILICONFLOW_API_KEY")
    if not key:
        return _no_key()
    try:
        data = _get_json("https://api.siliconflow.cn/v1/user/info", _bearer(key))
        d = data.get("data", {})
        total = float(d["totalBalance"])
        if not math.isfinite(total):
            raise ValueError("余额格式无效")
        note = "充值 ¥%s · 赠送 ¥%s" % (d.get("chargeBalance", "--"), d.get("balance", "--"))
        return {"ok": True, "remaining": total, "total": None,
                "unit": "¥", "note": note, "source": "硅基流动 API", "error": None}
    except urllib.error.HTTPError as e:
        if e.code == 410:
            return {"ok": False, "remaining": None, "total": None, "unit": "¥",
                    "note": "", "retired": True,
                    "error": "硅基流动已于 2026-08-14 停用余额查询接口，暂请手动记账"}
        return _fail(e)
    except Exception as e:  # noqa: BLE001
        return _fail(e)


# ---------------- Cursor(读本机登录态, 参考 token-usage-widget) ----------------
CURSOR_USAGE_URL = \
    "https://api2.cursor.sh/aiserver.v1.DashboardService/GetCurrentPeriodUsage"


def _cursor_state_db():
    override = os.environ.get("CURSOR_STATE_DB", "").strip()
    if override:
        return override
    appdata = os.environ.get("APPDATA") or \
        os.path.join(os.path.expanduser("~"), "AppData", "Roaming")
    return os.path.join(appdata, "Cursor", "User", "globalStorage",
                        "state.vscdb")


def _clean_stale_copies(prefix):
    tmp = tempfile.gettempdir()
    try:
        for name in os.listdir(tmp):
            if name.startswith(prefix):
                shutil.rmtree(os.path.join(tmp, name), ignore_errors=True)
    except OSError:
        pass


def _cursor_db_value(key):
    src = _cursor_state_db()
    if not os.path.exists(src):
        raise RuntimeError("未找到 Cursor(state.vscdb 不存在)")
    _clean_stale_copies("tokenspy-cursor-")
    uri = "file:" + urllib.parse.quote(src.replace("\\", "/")) + "?mode=ro&immutable=1"
    try:
        conn = sqlite3.connect(uri, uri=True)
        try:
            row = conn.execute(
                "SELECT value FROM ItemTable WHERE key=?", (key,)
            ).fetchone()
            return row[0] if row else None
        finally:
            conn.close()
    except sqlite3.Error:
        pass
    size = os.path.getsize(src)
    if size > 2 * 1024 ** 3:
        raise RuntimeError("Cursor 数据库过大(%.0f GB), 已跳过复制; 请关闭 Cursor 让其压缩后重试"
                           % (size / 1024 ** 3))
    tmp_dir = tempfile.mkdtemp(prefix="tokenspy-cursor-")
    try:
        dst = os.path.join(tmp_dir, "state.vscdb")
        shutil.copy2(src, dst)
        for suffix in ("-wal", "-shm"):
            if os.path.exists(src + suffix):
                shutil.copy2(src + suffix, dst + suffix)
        conn = sqlite3.connect(dst)
        try:
            row = conn.execute(
                "SELECT value FROM ItemTable WHERE key=?", (key,)
            ).fetchone()
            return row[0] if row else None
        finally:
            conn.close()
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


def fetch_cursor(cfg):
    try:
        token = _cursor_db_value("cursorAuth/accessToken")
        if not token:
            return {"ok": False, "remaining": None, "total": None, "unit": "%",
                    "note": "", "error": "Cursor 未登录"}
        try:
            membership = _cursor_db_value("cursorAuth/stripeMembershipType")
        except Exception:  # noqa: BLE001
            membership = None

        req = urllib.request.Request(
            CURSOR_USAGE_URL, data=b"{}", method="POST",
            headers={"Authorization": "Bearer " + token,
                     "Accept": "application/json",
                     "Content-Type": "application/json",
                     "Connect-Protocol-Version": "1",
                     "User-Agent": "tokenspy"})
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            data = json.loads(resp.read().decode("utf-8"))

        plan = data.get("planUsage") or {}
        used = plan.get("totalPercentUsed")
        if not isinstance(used, (int, float)):
            return {"ok": False, "remaining": None, "total": None, "unit": "%",
                    "note": "", "error": "Cursor 未返回用量"}
        monthly_pct = max(0.0, 100.0 - float(used))
        reset_at = None
        end = data.get("billingCycleEnd")
        try:
            ms = float(end)
            if ms < 1e12:
                ms *= 1000
            reset_at = int(ms / 1000)
        except (TypeError, ValueError):
            reset_at = None
        windows = [{"id":"cursor:billing", "label":"账单周期", "bucket":"Cursor",
                    "remaining_percent":monthly_pct,"used_percent":float(used),
                    "duration_minutes":None,"resets_at":reset_at}]
        note_parts = [membership] if membership else []
        return {
            "ok": True, "remaining": float(monthly_pct), "total": 100.0, "unit": "%",
            "note": " · ".join(note_parts), "error": None,
            "source": "Cursor 用量 API", "plan": membership or "",
            "windows": windows,
        }
    except Exception as e:  # noqa: BLE001
        return _fail(e)


# ---------------- Kimi / Moonshot ----------------
def fetch_moonshot(cfg):
    key = get_api_key(cfg, "MOONSHOT_API_KEY")
    if not key:
        return _no_key()
    try:
        data = _get_json("https://api.moonshot.cn/v1/users/me/balance", _bearer(key))
        d = data.get("data", {})
        avail = float(d.get("available_balance", 0) or 0)
        voucher = float(d.get("voucher_balance", 0) or 0)
        cash = float(d.get("cash_balance", 0) or 0)
        note = "现金 ¥%.2f / 代金券 ¥%.2f" % (cash, voucher)
        return {"ok": True, "remaining": avail, "total": None,
                "unit": "¥", "note": note, "error": None}
    except Exception as e:  # noqa: BLE001
        return _fail(e)


# ---------------- Kimi Code(Coding Plan 会员套餐, 199 档 Allegretto 等) ----------------
KIMI_USAGES_URL = "https://api.kimi.com/coding/v1/usages"
KIMI_LEVELS = {"andante": "¥49", "moderato": "¥99",
               "allegretto": "¥199", "allegro": "¥699"}
KIMI_UNITS = {"TIME_UNIT_MINUTE": 60, "TIME_UNIT_HOUR": 3600, "TIME_UNIT_DAY": 86400}


def _iso_to_ts(text):
    from datetime import datetime
    if not text:
        return None
    try:
        return int(datetime.fromisoformat(str(text).replace("Z", "+00:00")).timestamp())
    except ValueError:
        return None


def fetch_kimi_coding(cfg):
    key = get_api_key(cfg, "KIMI_API_KEY") or get_api_key(cfg, "MOONSHOT_API_KEY")
    if not key:
        return _no_key()
    if not key.startswith("sk-kimi-"):
        return fetch_moonshot(cfg)
    try:
        req = urllib.request.Request(KIMI_USAGES_URL, headers={
            "Authorization": "Bearer " + key,
            "Accept": "application/json",
            "User-Agent": "KimiCLI/1.6",
        })
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            data = json.loads(resp.read().decode("utf-8"))

        windows_raw = []
        usages = data.get("usages") or {}
        for bucket_id, label, minutes in (
            ("limit_5h", "5 小时窗口", 300),
            ("limit_month_total", "月度总额度", None),
            ("limit_month_code", "月度 Code", None),
        ):
            bucket = usages.get(bucket_id)
            if not isinstance(bucket, dict):
                continue
            try:
                used_ratio = float(bucket.get("used_ratio") or 0)
            except (TypeError, ValueError):
                continue
            used_pct = max(0.0, min(100.0, used_ratio * 100))
            windows_raw.append({
                "id": "kimi:" + bucket_id, "label": label, "bucket": "Kimi",
                "remaining_percent": 100 - used_pct, "used_percent": used_pct,
                "duration_minutes": minutes,
                "resets_at": _iso_to_ts(bucket.get("reset_time")),
            })
        if not windows_raw:
            for index, lim in enumerate(data.get("limits") or []):
                window = lim.get("window") or {}
                detail = lim.get("detail") or {}
                try:
                    limit = float(detail.get("limit") or 0)
                    remaining = float(
                        detail.get("remaining") if detail.get("remaining") is not None
                        else limit - float(detail.get("used") or 0)
                    )
                except (TypeError, ValueError):
                    continue
                if limit <= 0:
                    continue
                seconds = (window.get("duration") or 0) * KIMI_UNITS.get(window.get("timeUnit"), 60)
                minutes = seconds // 60
                if minutes >= 1440 and minutes % 1440 == 0:
                    label = "%d 天窗口" % (minutes // 1440)
                elif minutes >= 60 and minutes % 60 == 0:
                    label = "%d 小时窗口" % (minutes // 60)
                else:
                    label = "%d 分钟窗口" % minutes
                remaining_pct = max(0.0, min(100.0, remaining / limit * 100))
                windows_raw.append({
                    "id": "kimi:limit:%d" % index, "label": label, "bucket": "Kimi",
                    "remaining_percent": remaining_pct,
                    "used_percent": 100 - remaining_pct,
                    "duration_minutes": minutes or None,
                    "resets_at": _iso_to_ts(detail.get("resetTime")),
                })
        if not windows_raw:
            return {"ok": False, "remaining": None, "total": None, "unit": "%",
                    "note": "",
                    "error": "Kimi 未返回额度窗口（可能不是 Coding Plan 会员 Key）"}

        level = str(((data.get("user") or {}).get("membership") or {}).get("level") or "")
        level = level.replace("LEVEL_", "").strip().lower()
        tier = "%s 档" % KIMI_LEVELS[level] if level in KIMI_LEVELS else ""
        plan = ("%s %s" % (level.capitalize(), tier)).strip()
        notes = [plan or "Coding Plan"]
        if data.get("limited"):
            notes.append("当前限流中")
        parallel = (data.get("parallel") or {}).get("limit")
        if isinstance(parallel, (int, float)):
            notes.append("并发上限 %d" % parallel)
        windows = windows_raw
        monthly_pct = min(w["remaining_percent"] for w in windows)
        return {
            "ok": True, "remaining": float(monthly_pct), "total": 100.0, "unit": "%",
            "note": " · ".join(notes), "source": "Kimi Code 额度 API", "plan": plan,
            "windows": windows, "error": None,
        }
    except urllib.error.HTTPError as e:
        if e.code == 401:
            return {"ok": False, "remaining": None, "total": None, "unit": "%",
                    "note": "",
                    "error": "Key 无效或已过期（需要 sk-kimi- 开头的 Coding Plan Key）"}
        return _fail(e)
    except Exception as e:  # noqa: BLE001
        return _fail(e)


# ---------------- ZCode(读本机会话库, 统计实际 token 消耗) ----------------
ZCODE_PLAN_LABELS = {
    "individual-coding-plan": "个人 Coding Plan",
    "pro": "Pro", "max": "Max", "team": "团队版", "enterprise": "企业版",
}


def _zcode_settings_path():
    override = os.environ.get("ZCODE_SETTINGS", "").strip()
    if override:
        return override
    return os.path.join(os.path.expanduser("~"), ".zcode", "v2", "setting.json")


def _zcode_plan_label():
    try:
        with open(_zcode_settings_path(), encoding="utf-8") as f:
            data = json.load(f)
        kind = (((data.get("providerFamilyConnectionSelections") or {})
                 .get("zai") or {}).get("kind") or "")
        return ZCODE_PLAN_LABELS.get(kind, kind)
    except (OSError, ValueError):
        return ""


def _zcode_db_path():
    override = os.environ.get("ZCODE_DB", "").strip()
    if override:
        return override
    return os.path.join(os.path.expanduser("~"), ".zcode", "cli", "db", "db.sqlite")


def _zcode_query(sql, params=()):
    src = _zcode_db_path()
    if not os.path.isfile(src):
        raise RuntimeError("未找到 ZCode 会话库(~/.zcode/cli/db/db.sqlite)")
    _clean_stale_copies("tokenspy-zcode-")
    tmp_dir = tempfile.mkdtemp(prefix="tokenspy-zcode-")
    try:
        dst = os.path.join(tmp_dir, "db.sqlite")
        shutil.copy2(src, dst)
        for suffix in ("-wal", "-shm"):
            if os.path.exists(src + suffix):
                shutil.copy2(src + suffix, dst + suffix)
        conn = sqlite3.connect(dst)
        try:
            return conn.execute(sql, params).fetchall()
        finally:
            conn.close()
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


def _fmt_tokens(n):
    if n >= 1e8:
        return "%.1f亿" % (n / 1e8)
    if n >= 1e4:
        return "%.0f万" % (n / 1e4)
    if n >= 1000:
        return "%.1fk" % (n / 1e3)
    return "%d" % n


def fetch_zcode(cfg):
    try:
        from datetime import datetime
        now = time.time()
        midnight = datetime.combine(datetime.now().date(), datetime.min.time()).timestamp()
        rows = _zcode_query(
            "SELECT model_id, COUNT(*), SUM(computed_total_tokens) "
            "FROM model_usage WHERE status='completed' AND started_at >= ? "
            "GROUP BY model_id ORDER BY SUM(computed_total_tokens) DESC",
            (int(midnight * 1000),))
        used_day = sum(int(r[2] or 0) for r in rows)
        rows_5h = _zcode_query(
            "SELECT SUM(computed_total_tokens) FROM model_usage "
            "WHERE status='completed' AND started_at >= ?",
            (int((now - 5 * 3600) * 1000),))
        used_5h = int((rows_5h and rows_5h[0][0]) or 0)
        active = False
        try:
            active = bool(_zcode_query(
                "SELECT COUNT(*) FROM model_usage "
                "WHERE status='running' AND started_at >= ?",
                (int(midnight * 1000),))[0][0])
        except Exception:  # noqa: BLE001
            pass
        plan = _zcode_plan_label()
        base = {"ok": True, "unit": "万tok", "plan": plan, "active": active,
                "source": "ZCode 本地会话库", "error": None}
        budget = cfg.get("daily_budget_tokens")
        if budget:
            try:
                budget = float(budget)
            except (TypeError, ValueError):
                return _fail(ValueError("daily_budget_tokens 配置无效"))
            remaining_tokens = max(0.0, budget - used_day)
            pct = max(0.0, min(100.0, remaining_tokens / budget * 100))
            from datetime import timedelta
            base.update({
                "remaining": round(remaining_tokens / 1e4, 2),
                "total": round(budget / 1e4, 2),
                "note": "今日预算 %s · 5小时 %s · %s"
                        % (_fmt_tokens(used_day), _fmt_tokens(used_5h),
                           " / ".join(str(r[0]) for r in rows[:3])),
                "windows": [{"id": "zcode:day", "label": "今日预算", "bucket": "ZCode",
                             "remaining_percent": pct, "used_percent": 100 - pct,
                             "duration_minutes": 1440, "daily": True,
                             "resets_at": midnight + timedelta(days=1).total_seconds()}]})
            return base
        base.update({
            "remaining": round(used_day / 1e4, 2), "total": None,
            "metric_kind": "usage", "metric_label": "今日已用",
            "note": "仅本机已完成请求，非套餐剩余额度 · " + " / ".join(str(r[0]) for r in rows)})
        return base
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "remaining": None, "total": None, "unit": "%",
                "note": "",
                "error": "ZCode 会话库不可用(" + str(e) + ")"}


# ---------------- 手动记账(IDE / 无公开接口的服务) ----------------
def fetch_manual(cfg):
    remaining = cfg.get("remaining")
    total = cfg.get("total")
    ok = remaining is not None
    if ok:
        return {"ok":True,"remaining":remaining,"total":total,"unit":cfg.get("unit","次"),
                "note":cfg.get("note","手动记账，双击更新"),"source":"手动记账","error":None}
    return {
        "ok": False,
        "remaining": None,
        "total": None,
        "unit": cfg.get("unit", "次"),
        "note": cfg.get("note", "手动记账, 双击卡片更新"),
        "source": "手动记账",
        "error": "尚未填写余量, 双击卡片填写",
    }


# ---------------- WorkBuddy 免费模型清单(本地策略表, 不联网) ----------------
def _parse_dt(text):
    if not text:
        return None
    for fmt in ("%Y-%m-%dT%H:%M", "%Y-%m-%d %H:%M",
                "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
        try:
            return time.mktime(time.strptime(str(text), fmt))
        except (TypeError, ValueError):
            continue
    return None


def _in_night_window(window, now=None):
    now = time.time() if now is None else now
    try:
        start_s, end_s = str(window).split("-")
        sh, sm = (int(x) for x in start_s.split(":"))
        eh, em = (int(x) for x in end_s.split(":"))
    except (ValueError, AttributeError):
        return False
    cur = time.localtime(now)
    cur_min = cur.tm_hour * 60 + cur.tm_min
    start_min, end_min = sh * 60 + sm, eh * 60 + em
    if start_min <= end_min:
        return start_min <= cur_min < end_min
    return cur_min >= start_min or cur_min < end_min


def _free_status(m, now=None):
    now = time.time() if now is None else now
    kind = m.get("kind")
    if kind == "night":
        if _in_night_window(m.get("window", "23:00-08:00"), now):
            return True, "夜间免费中", "#e3f2ec", "#2e7d6b"
        return False, "夜间 %s 起" % m.get("window", "23:00-08:00"), "#eef0ea", "#788784"
    if kind == "limited":
        until = _parse_dt(m.get("until"))
        if until is None:
            return False, "未知时段", "#eef0ea", "#788784"
        if now <= until:
            return True, "限免中", "#e3f2ec", "#2e7d6b"
        return False, "已过期", "#e9eae6", "#98a09a"
    return False, "—", "#eef0ea", "#788784"


def fetch_workbuddy_free(cfg):
    models = [dict(m) for m in (cfg.get("free_models") or [])]
    now = time.time()
    free_count = 0
    for m in models:
        free_now = _free_status(m, now)[0]
        m["free_now"] = free_now
        if free_now:
            free_count += 1
    return {
        "ok": True,
        "free_models": models,
        "free_count": free_count,
        "free_total": len(models),
        "unit": "",
        "note": "策略表维护 · 政策变动改 config 一处",
        "source": "本地策略表",
        "error": None,
    }


# ---------------- ChatGPT (Plus: 登录态DB扫描 + 兜底双轨) ----------------
def _chatgpt_state_db_paths():
    appdata = os.environ.get("APPDATA") or os.path.join(
        os.path.expanduser("~"), "AppData", "Roaming")
    return [
        os.path.join(appdata, "ChatGPT", "User", "globalStorage", "state.vscdb"),
    ]


def _chatgpt_read_db(db_path, key):
    if not os.path.exists(db_path):
        return None
    uri = "file:" + urllib.parse.quote(db_path.replace("\\", "/")) + "?mode=ro&immutable=1"
    try:
        conn = sqlite3.connect(uri, uri=True)
        try:
            row = conn.execute("SELECT value FROM ItemTable WHERE key=?", (key,)).fetchone()
            return row[0] if row else None
        finally:
            conn.close()
    except Exception:
        return None


def fetch_chatgpt(cfg):
    return {"ok":False,"remaining":None,"total":None,"unit":"%",
            "note":"ChatGPT 对话额度与 Codex 额度分开统计",
            "error":"ChatGPT 对话套餐尚无已验证的自动用量来源", "source":"未接入"}


# ---------------- Workbuddy (本机 globalStorage json 扫描 + 兜底双轨) ----------------
def _workbuddy_storage_paths():
    appdata = os.environ.get("APPDATA") or os.path.join(
        os.path.expanduser("~"), "AppData", "Roaming")
    return [
        os.path.join(appdata, "Workbuddy", "User", "globalStorage"),
        os.path.expanduser(r"~\.workbuddy"),
    ]


def _walk_extract_pct(obj):
    """递归遍历 dict/list, 抽取 (pct_val | None)。优先走 remain/usage_pct/percent 字段。"""
    out = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            kl = k.lower()
            is_num = isinstance(v, (int, float)) and not isinstance(v, bool)
            if is_num and ("remain" in kl or "pct" in kl or "percent" in kl):
                val = float(v)
                if 0 <= val <= 100:
                    out.append(val)
            elif is_num and ("used" in kl) and 0 <= float(v) <= 100:
                out.append(100.0 - float(v))
            out.extend(_walk_extract_pct(v))
    elif isinstance(obj, list):
        for x in obj:
            out.extend(_walk_extract_pct(x))
    return out


def fetch_workbuddy(cfg):
    return {"ok":False,"remaining":None,"total":None,"unit":"积分",
            "note":"请在 WorkBuddy 设置 → 套餐与积分查看",
            "error":"尚未接入 WorkBuddy 官方积分数据", "source":"未接入"}


# ---------------- Trae (国际版 / 国内版双路径, 支持本机登录态 + 多账号手动 Token) ----------------
TRAE_ENTITLEMENT_URL = "https://www.trae.ai/api/user_current_entitlement_list"
TRAE_CN_ENTITLEMENT_URL = "https://www.trae.cn/api/user_current_entitlement_list"


def _trae_state_db_paths():
    appdata = os.environ.get("APPDATA") or \
        os.path.join(os.path.expanduser("~"), "AppData", "Roaming")
    override_global = os.environ.get("TRAE_STATE_DB", "").strip()
    override_cn = os.environ.get("TRAE_CN_STATE_DB", "").strip()
    paths = []
    if override_global:
        paths.append((override_global, "global"))
    if override_cn:
        paths.append((override_cn, "cn"))
    paths += [
        (os.path.join(appdata, "Trae", "User", "globalStorage", "state.vscdb"), "global"),
        (os.path.join(appdata, "Trae CN", "User", "globalStorage", "state.vscdb"), "cn"),
    ]
    return paths


def _trae_db_value(db_path, key):
    if not os.path.exists(db_path):
        return None
    uri = "file:" + urllib.parse.quote(db_path.replace("\\", "/")) + "?mode=ro&immutable=1"
    try:
        conn = sqlite3.connect(uri, uri=True)
        try:
            row = conn.execute(
                "SELECT value FROM ItemTable WHERE key=?", (key,)
            ).fetchone()
            return row[0] if row else None
        finally:
            conn.close()
    except sqlite3.Error:
        pass
    size = os.path.getsize(db_path)
    if size > 2 * 1024 ** 3:
        return None
    _clean_stale_copies("tokenspy-trae-")
    tmp_dir = tempfile.mkdtemp(prefix="tokenspy-trae-")
    try:
        dst = os.path.join(tmp_dir, "state.vscdb")
        shutil.copy2(db_path, dst)
        for suffix in ("-wal", "-shm"):
            if os.path.exists(db_path + suffix):
                shutil.copy2(db_path + suffix, dst + suffix)
        conn = sqlite3.connect(dst)
        try:
            row = conn.execute(
                "SELECT value FROM ItemTable WHERE key=?", (key,)
            ).fetchone()
            return row[0] if row else None
        finally:
            conn.close()
    except Exception:
        return None
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


def _trae_auto_token():
    candidate_keys = [
        "traeAuth/accessToken", "trae/auth/access_token",
        "accessToken", "auth/accessToken",
        "trae.jwt.accessToken", "icube/token", "iCubeServerData",
    ]
    for db_path, region in _trae_state_db_paths():
        for key in candidate_keys:
            val = _trae_db_value(db_path, key)
            if not val:
                continue
            token = None
            if isinstance(val, str) and len(val) > 20:
                if val.startswith("eyJ") or "Cloud-IDE-JWT" in val:
                    token = val
            if token is None:
                try:
                    d = json.loads(val)
                    for field in ("accessToken", "access_token", "token", "jwt", "value"):
                        v = d.get(field)
                        if isinstance(v, str) and len(v) > 20 and (
                            v.startswith("eyJ") or "Cloud-IDE-JWT" in v
                        ):
                            token = v
                            break
                except (ValueError, TypeError):
                    pass
            if token:
                token = token.replace("Cloud-IDE-JWT ", "").strip()
                return token, region
    return None, None


def _trae_entitlement_endpoint(region_hint):
    if region_hint == "cn":
        return TRAE_CN_ENTITLEMENT_URL, TRAE_ENTITLEMENT_URL
    return TRAE_ENTITLEMENT_URL, TRAE_CN_ENTITLEMENT_URL


def _parse_entitlement(data):
    if not isinstance(data, dict):
        return None
    payload = data.get("data") if isinstance(data.get("data"), dict) else data

    entitlements = payload.get("entitlements") or payload.get("list") or []
    if not isinstance(entitlements, list):
        entitlements = []

    windows = []
    plan_total = 0.0
    plan_used = 0.0
    plan_name = ""
    reset_at = None

    for idx, ent in enumerate(entitlements):
        if not isinstance(ent, dict):
            continue
        try:
            total = float(ent.get("total") or ent.get("limit") or ent.get("quota") or 0)
            used = float(ent.get("used") or ent.get("consumed") or 0)
        except (TypeError, ValueError):
            continue
        remaining = max(0.0, total - used)
        if total <= 0:
            continue
        reset_raw = (ent.get("resetAt") or ent.get("reset_time") or
                     ent.get("billingCycleEnd") or ent.get("endTime"))
        is_plan = idx == 0 and not plan_name
        if is_plan:
            plan_total = total
            plan_used = used
            e_type = str(ent.get("type") or ent.get("entitlementType") or "").lower()
            label = str(ent.get("name") or ent.get("label") or "")
            if "fast" in e_type or "fast" in label.lower():
                plan_name = "Pro Plan · 快速请求"
            else:
                plan_name = label or "Pro Plan"
            reset_raw = (ent.get("resetAt") or ent.get("reset_time") or
                         ent.get("billingCycleEnd") or ent.get("endTime"))
            if reset_raw:
                try:
                    ms = float(reset_raw)
                    if ms < 1e12:
                        ms *= 1000
                    reset_at = int(ms / 1000)
                except (TypeError, ValueError):
                    pass
        pct = max(0.0, min(100.0, remaining / total * 100.0))
        used_pct = 100 - pct
        win_reset = None
        if reset_raw:
            try:
                ms = float(reset_raw)
                if ms < 1e12:
                    ms *= 1000
                win_reset = int(ms / 1000)
            except (TypeError, ValueError):
                pass
        windows.append({
            "id": "trae:entitlement:%d" % idx,
            "label": plan_name if is_plan else ("加油包%d" % (idx + 1)),
            "bucket": "Trae",
            "remaining_percent": pct, "used_percent": used_pct,
            "duration_minutes": None,
            "resets_at": win_reset,
        })

    if plan_total == 0:
        for field_pair in (("used", "total"), ("usedAmount", "totalAmount"), ("consumed", "quota")):
            try:
                t = float(payload.get(field_pair[1]) or 0)
                u = float(payload.get(field_pair[0]) or 0)
                if t > 0:
                    plan_total = t
                    plan_used = u
                    break
            except (TypeError, ValueError):
                continue
        for raw_field in ("plan", "planName", "membership", "tier"):
            v = payload.get(raw_field)
            if v:
                plan_name = str(v)
                break
        if reset_at is None:
            for reset_field in ("billingCycleEnd", "resetAt", "reset_time", "endTime"):
                reset_raw = payload.get(reset_field)
                if reset_raw:
                    try:
                        ms = float(reset_raw)
                        if ms < 1e12:
                            ms *= 1000
                        reset_at = int(ms / 1000)
                        break
                    except (TypeError, ValueError):
                        continue
        if plan_total > 0:
            pct = max(0.0, min(100.0, (plan_total - plan_used) / plan_total * 100))
            windows.append({
                "id": "trae:entitlement:top", "label": plan_name or "套餐",
                "bucket": "Trae", "remaining_percent": pct,
                "used_percent": 100 - pct, "duration_minutes": None,
                "resets_at": reset_at,
            })

    if plan_total <= 0:
        return None
    remaining_primary = max(0.0, plan_total - plan_used)
    note_main = plan_name or "Pro Plan"
    if reset_at:
        note_main += time.strftime(" · %m-%d 重置", time.localtime(reset_at))
    return remaining_primary / plan_total * 100, 100.0, "%", note_main, windows, reset_at


def fetch_trae(cfg):
    now = time.time()
    region_hint = (cfg.get("region") or "auto").strip().lower()
    token = ""
    region = None
    source_tag = "本机登录态"

    pid = str(cfg.get("id") or "").replace("-", "_").replace(".", "_").upper()
    scoped_env_names = []
    if pid:
        scoped_env_names.append("TRAE_TOKEN_" + pid)
        scoped_env_names.append("TRAE_CN_TOKEN_" + pid)
    explicit_token = ""
    for env_name in scoped_env_names + ["TRAE_TOKEN", "TRAE_CN_TOKEN"]:
        v = get_api_key(cfg, env_name)
        if v:
            explicit_token = v
            break
    if explicit_token:
        token = explicit_token.replace("Cloud-IDE-JWT ", "").strip()
        region = region_hint if region_hint in ("global", "cn") else "global"
        source_tag = "手动 Token"
    if not token:
        auto_token, auto_region = _trae_auto_token()
        if auto_token:
            token = auto_token
            region = auto_region

    raw_result = None
    used_url = None
    last_err = None
    if token:
        primary_url, fallback_url = _trae_entitlement_endpoint(region or "global")
        headers = {
            "Authorization": "Cloud-IDE-JWT " + token,
            "Accept": "application/json",
            "Content-Type": "application/json",
            "User-Agent": "tokenspy",
        }
        for url in (primary_url, fallback_url):
            try:
                req = urllib.request.Request(url, data=b"{}", method="POST", headers=headers)
                with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
                    raw_result = json.loads(resp.read().decode("utf-8"))
                    used_url = url
                    break
            except Exception as e:  # noqa: BLE001
                last_err = e
    if raw_result is not None:
        parsed = _parse_entitlement(raw_result)
        if parsed is not None:
            monthly_pct, tot, unit, note_top, _raw_wins, reset_at = parsed
            windows = _raw_wins
            region_display = "国内版" if used_url and "trae.cn" in used_url else "国际版"
            return {
                "ok": True, "remaining": float(monthly_pct), "total": 100.0, "unit": "%",
                "note": note_top, "plan": note_top,
                "source": "Trae %s · %s" % (region_display, source_tag),
                "windows": windows, "error": None,
            }
    err_msg = "Trae 未返回额度；请先打开并登录 Trae"
    if last_err is not None:
        err_msg += " (%s)" % last_err
    return {
        "ok": False, "remaining": None, "total": None, "unit": "%", "note": "",
        "error": err_msg,
    }


# ---------------- 废弃类型: aideskpet(原 mock 类型) ----------------
def fetch_aideskpet(cfg):
    return {
        "ok": False, "remaining": None, "total": None, "unit": "%", "note": "",
        "error": "aideskpet 类型已废弃；请改为 chatgpt/cursor/trae/zcode/workbuddy 真实类型",
    }


# ---------------- ADAPTERS 注册 ----------------
ADAPTERS = {
    "cursor": fetch_cursor,
    "siliconflow": fetch_siliconflow,
    "moonshot": fetch_moonshot,
    "kimi": fetch_kimi_coding,
    "kimi_coding": fetch_kimi_coding,
    "zcode": fetch_zcode,
    "trae": fetch_trae,
    "manual": fetch_manual,
    "workbuddy_free": fetch_workbuddy_free,
    "chatgpt": fetch_chatgpt,
    "workbuddy": fetch_workbuddy,
    "aideskpet": fetch_aideskpet,
}


def fetch_one(cfg):
    """Each response owns its observation timestamp; missing data is never zero."""
    started = time.monotonic()
    kind = cfg.get("type", "manual")
    if kind == "codex":
        try:
            from codex_usage import read_limits, normalize_limits
            result = normalize_limits(read_limits())
        except Exception as exc:
            result = _fail(exc)
    elif kind in ADAPTERS:
        result = ADAPTERS[kind](cfg)
    else:
        result = _fail(RuntimeError("该服务尚无自动同步适配器"))
    result["schema_version"] = 2
    result["fetched_at"] = int(time.time())
    result["latency_ms"] = round((time.monotonic() - started) * 1000)
    result.setdefault("source", "手动记账" if kind == "manual" else "自动同步")
    return result


def get_api_key(cfg, env_name):
    from credentials import read_secret
    return (os.environ.get(env_name) or read_secret(cfg.get("id", "")) or cfg.get("api_key", "")).strip()


def _no_key():
    return {"ok": False, "remaining": None, "total": None, "unit": "",
            "note": "", "error": "尚未连接，点击「连接」填写 API Key"}


def _fail(e):
    msg = str(e)
    if isinstance(e, urllib.error.HTTPError):
        msg = "HTTP %s" % e.code
    return {"ok": False, "remaining": None, "total": None, "unit": "",
            "note": "", "error": "查询失败: " + msg}
