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
    """清理上次异常退出留下的临时副本(cursor 副本曾吃掉 52 GB)。"""
    tmp = tempfile.gettempdir()
    try:
        for name in os.listdir(tmp):
            if name.startswith(prefix):
                shutil.rmtree(os.path.join(tmp, name), ignore_errors=True)
    except OSError:
        pass


def _cursor_db_value(key):
    """优先以 immutable 只读模式原地读 state.vscdb(不加锁、不复制);
    失败才复制副本, 且库超过 2 GB 时拒绝整库复制。"""
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
        # 顺带复制 WAL/SHM, 保证读到最新写入
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
                    "note": "", "error": "Cursor 未登录(打开 Cursor 登录后重试)"}
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
                    "note": "", "error": "Cursor 未返回用量(可能无套餐)"}
        remaining = max(0.0, 100.0 - float(used))
        # 重置时间(unix 秒或毫秒)
        reset = ""
        reset_at = None
        end = data.get("billingCycleEnd")
        try:
            ms = float(end)
            if ms < 1e12:
                ms *= 1000
            reset_at = int(ms / 1000)
            reset = time.strftime(" · %m-%d 重置", time.localtime(ms / 1000))
        except (TypeError, ValueError):
            pass
        api_used = plan.get("apiPercentUsed")
        note = "%s%s" % (
            ("API %d%% · " % api_used) if isinstance(api_used, (int, float))
            else "",
            (membership or "plan") + reset)
        return {"ok": True, "remaining": remaining, "total": 100,
                "unit": "%", "note": note.strip(), "error": None,
                "source": "Cursor 本机登录态",
                "windows": [{"id": "cursor:plan", "label": "账单周期", "bucket": "Cursor",
                             "remaining_percent": remaining, "used_percent": float(used),
                             "duration_minutes": None, "resets_at": reset_at}]}
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
            "User-Agent": "KimiCLI/1.6",  # 不带此 UA 接口会 404
        })
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            data = json.loads(resp.read().decode("utf-8"))

        windows = []
        # 主路径: usages 三个桶(5 小时滚动 / 月度总额 / 月度 Code)
        usages = data.get("usages") or {}
        for bucket_id, label, minutes in (("limit_5h", "5 小时窗口", 300),
                                          ("limit_month_total", "月度总额度", None),
                                          ("limit_month_code", "月度 Code", None)):
            bucket = usages.get(bucket_id)
            if not isinstance(bucket, dict):
                continue
            try:
                used_ratio = float(bucket.get("used_ratio") or 0)
            except (TypeError, ValueError):
                continue
            used_pct = max(0.0, min(100.0, used_ratio * 100))
            windows.append({"id": "kimi:" + bucket_id, "label": label, "bucket": "Kimi",
                            "remaining_percent": 100 - used_pct, "used_percent": used_pct,
                            "duration_minutes": minutes,
                            "resets_at": _iso_to_ts(bucket.get("reset_time"))})
        # 兜底: 旧版 limits[] 结构(remaining/limit)
        if not windows:
            for index, lim in enumerate(data.get("limits") or []):
                window = lim.get("window") or {}
                detail = lim.get("detail") or {}
                try:
                    limit = float(detail.get("limit") or 0)
                    remaining = float(detail.get("remaining") if detail.get("remaining") is not None
                                      else limit - float(detail.get("used") or 0))
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
                windows.append({"id": "kimi:limit:%d" % index, "label": label, "bucket": "Kimi",
                                "remaining_percent": remaining_pct, "used_percent": 100 - remaining_pct,
                                "duration_minutes": minutes or None,
                                "resets_at": _iso_to_ts(detail.get("resetTime"))})
        if not windows:
            return {"ok": False, "remaining": None, "total": None, "unit": "%",
                    "note": "", "error": "Kimi 未返回额度窗口（可能不是 Coding Plan 会员 Key）"}

        level = str(((data.get("user") or {}).get("membership") or {}).get("level") or "")
        level = level.replace("LEVEL_", "").strip().lower()
        tier = "%s 档" % KIMI_LEVELS[level] if level in KIMI_LEVELS else ""
        plan = ("%s %s" % (level.capitalize(), tier)).strip()
        notes = [plan or "Coding Plan"]
        if data.get("limited"):
            notes.append("当前限流中，等窗口恢复")
        parallel = (data.get("parallel") or {}).get("limit")
        if isinstance(parallel, (int, float)):
            notes.append("并发上限 %d" % parallel)
        core = min(w["remaining_percent"] for w in windows)
        return {"ok": True, "remaining": core, "total": 100, "unit": "%",
                "note": " · ".join(notes), "windows": windows,
                "source": "Kimi Code 额度 API", "error": None}
    except urllib.error.HTTPError as e:
        if e.code == 401:
            return {"ok": False, "remaining": None, "total": None, "unit": "%",
                    "note": "", "error": "Key 无效或已过期（需要 sk-kimi- 开头的 Coding Plan Key）"}
        if e.code == 404:
            return {"ok": False, "remaining": None, "total": None, "unit": "%",
                    "note": "", "error": "该 Key 没有 Coding Plan 额度（开放平台 Key 请用余额模式）"}
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
    """读 ZCode 设置里当前连接的订阅档位(没有则空串)。"""
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
    """复制 ZCode 的 WAL 库到临时目录再查询: 主库被 ZCode 持续写入,
    immutable 原地读会跳过 -wal 丢最近记录; 副本用完即删。"""
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
    """ZCode 没有公开的套餐配额接口, 但本地会话库记了每次请求的真实 token 数。
    统计今日(本地零点起)已完成请求的消耗; 配置 daily_budget_tokens 后
    额外给出「今日预算剩余百分比」, 并以零点为重置点接入复活/告急提醒。"""
    try:
        from datetime import datetime, timedelta
        midnight = datetime.combine(datetime.now().date(),
                                    datetime.min.time()).timestamp()
        rows = _zcode_query(
            "SELECT model_id, COUNT(*), SUM(computed_total_tokens) "
            "FROM model_usage WHERE status='completed' AND started_at >= ? "
            "GROUP BY model_id ORDER BY SUM(computed_total_tokens) DESC",
            (int(midnight * 1000),))
    except Exception as e:  # noqa: BLE001
        return _fail(e)
    used = sum(int(r[2] or 0) for r in rows)
    requests = sum(int(r[1] or 0) for r in rows)
    active = False
    try:
        active = bool(_zcode_query(
            "SELECT COUNT(*) FROM model_usage "
            "WHERE status='running' AND started_at >= ?",
            (int((midnight - 0) * 1000),))[0][0])
    except Exception:  # noqa: BLE001
        pass
    notes = ["%s %s" % (r[0], _fmt_tokens(int(r[2] or 0))) for r in rows[:3]]
    notes.append("%d 次请求" % requests)
    plan = _zcode_plan_label()
    # 订阅档位来自 ~/.zcode/v2/setting.json; 30 天累计顺带汇报, 补足"没有配额接口"的体感
    month_note = ""
    try:
        month_rows = _zcode_query(
            "SELECT SUM(computed_total_tokens), COUNT(DISTINCT date(started_at/1000, 'unixepoch', 'localtime')) "
            "FROM model_usage WHERE status='completed' AND started_at >= ?",
            (int((midnight - 29 * 86400) * 1000),))
        month_used = int(month_rows[0][0] or 0)
        month_days = int(month_rows[0][1] or 0)
        if month_used > 0:
            month_note = " · 30天 %s(%d 天用)" % (_fmt_tokens(month_used), month_days)
    except Exception:  # noqa: BLE001
        pass
    budget = cfg.get("daily_budget_tokens")
    result = {"ok": True, "unit": "万tok", "error": None, "active": active,
              "source": "ZCode 本地会话库" + (" · " + plan if plan else ""),
              "note": ("[%s] " % plan if plan else "") + "今日 · " + " · ".join(notes) + month_note}
    if budget:
        try:
            budget = float(budget)
        except (TypeError, ValueError):
            return _fail(ValueError("daily_budget_tokens 配置无效"))
        remaining_tokens = max(0.0, budget - used)
        pct = max(0.0, min(100.0, remaining_tokens / budget * 100))
        result["remaining"] = round(remaining_tokens / 1e4, 2)
        result["total"] = round(budget / 1e4, 2)
        result["windows"] = [{"id": "zcode:day", "label": "今日预算", "bucket": "ZCode",
                              "remaining_percent": pct, "used_percent": 100 - pct,
                              "duration_minutes": 1440, "daily": True,
                              "resets_at": midnight + timedelta(days=1).total_seconds()}]
    else:
        result["remaining"] = round(used / 1e4, 2)
        result["total"] = None
    return result


# ---------------- 手动记账(IDE / 无公开接口的服务) ----------------
def fetch_manual(cfg):
    remaining = cfg.get("remaining")
    total = cfg.get("total")
    ok = remaining is not None
    return {
        "ok": ok,
        "remaining": remaining,
        "total": total,
        "unit": cfg.get("unit", "次"),
        "note": cfg.get("note", "手动记账, 双击卡片更新"),
        "error": None if ok else "尚未填写余量, 双击卡片填写",
    }


# ---------------- WorkBuddy 免费模型清单(本地策略表, 不联网) ----------------
def _parse_dt(text):
    """'2026-09-30T23:59' / '2026-09-30 23:59' / 纯日期 -> 本地 epoch 秒; 解析不出返回 None。"""
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
    """window: 'HH:MM-HH:MM' 跨午夜, 如 '23:00-08:00'。当前处于该时段返回 True。"""
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
    if start_min <= end_min:                 # 同日时段(不跨午夜)
        return start_min <= cur_min < end_min
    return cur_min >= start_min or cur_min < end_min   # 跨午夜


def _free_status(m, now=None):
    """返回 (当前是否免费, 文案, 底, 字) 四元组, 供面板实时着色。"""
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
    """WorkBuddy 免费模型清单: 读 config 里的 free_models 规则, 按当前时间算出
    「此刻哪些免费」。免费政策由运营公告决定、频繁变动, 故用本地策略表维护,
    而不是去拉不存在的公开接口。"""
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


ADAPTERS = {
    "cursor": fetch_cursor,
    "siliconflow": fetch_siliconflow,
    "moonshot": fetch_moonshot,
    "kimi": fetch_kimi_coding,
    "kimi_coding": fetch_kimi_coding,
    "zcode": fetch_zcode,
    "manual": fetch_manual,
    "workbuddy_free": fetch_workbuddy_free,
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
