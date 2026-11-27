# utils/error_notifier.py
"""ارسال خطاهای اجرا به کانال خطا (TELEGRAM_ERROR_CHAT_ID).

قانون (برای اجرای هر دقیقه‌ی تراکر؛ هر اجرا یک رخداد) — برای هر «خطا» جدا اعمال می‌شه؛ خطا = متن ERROR با ارقام حذف‌شده:
  ۱. خطا باید تو ERROR_NOTIFY_MIN_CONSECUTIVE اجرای «پشت‌سرهم» تکرار بشه تا پیام بره
     (خطای تک‌اجرایی، مثل reset لحظه‌ای شبکه، پیام نمی‌ده).
  ۲. بعد از ارسال پیام برای اون خطا، تا وقتی ادامه داره پیام تکراری نمی‌ره (یادآوری زمانی هم نداریم).
  ۳. اگه خطا برطرف بشه (اجرای بعدی اون خطا رو نداشته باشه؛ مثلاً اجرای سالم) شمارنده و «گزارش‌شده» اون خطا صفر
     می‌شه و اگه دوباره برگرده از نو شمرده می‌شه.
  ۴. خطای «متفاوت» همین روال رو جدا طی می‌کنه: بعد از ۲ اجرای پشت‌سرهم خودش پیام می‌ده (حتی وسط قطعیِ خطای دیگه).
وضعیت تو فایل جدای error_notify_state.json تو Gist نگه‌داری می‌شه.
اجراهایی که فقط یک‌بار تو دوره می‌ان (گزارش هفتگی/ماهانه) و FORCE_RUN با min_consecutive=1 فوری می‌رن و وضعیتی ذخیره نمی‌کنن.

نحوه‌ی کار:
  1. install() یک handler روی root logger می‌ذاره و همه‌ی لاگ‌های ERROR+ این اجرا رو جمع می‌کنه.
  2. آخر اجرا notify_run_problems(...) صدا زده می‌شه؛ خلاصه (با redact توکن‌ها) به کانال خطا می‌ره.
     خود ارسال هیچ‌وقت اجرا رو خراب نمی‌کنه.
"""

import hashlib
import logging
import os
import re
import time
from datetime import datetime

import pytz
import requests

from config import (
    TELEGRAM_BOT_TOKEN, TELEGRAM_ERROR_CHAT_ID, ERROR_NOTIFY_MIN_CONSECUTIVE,
    ERROR_PROJECT_LABEL, TIMEZONE, is_force_run,
)
from utils import gist_store
from utils.log_safety import redact

logger = logging.getLogger(__name__)

STATE_FILE = "error_notify_state.json"
MAX_COLLECTED = 30          # حداکثر تعداد خطای نگه‌داشته‌شده در حافظه
MAX_LINES_IN_MESSAGE = 5    # حداکثر خطای نمایش‌داده‌شده در پیام
MAX_LINE_CHARS = 300
MAX_MESSAGE_CHARS = 3500    # سقف تلگرام ۴۰۹۶ کاراکتره


class _ErrorCollector(logging.Handler):
    """لاگ‌های ERROR و بالاتر این اجرا رو (بدون تأثیر روی بقیه‌ی handlerها) جمع می‌کنه."""

    def __init__(self):
        super().__init__(level=logging.ERROR)
        self.records = []

    def emit(self, record):
        try:
            msg = record.getMessage()
            if record.exc_info and record.exc_info[0]:
                msg += f" [{record.exc_info[0].__name__}: {record.exc_info[1]}]"
            if len(self.records) < MAX_COLLECTED:
                self.records.append(f"{record.name}: {msg}")
        except Exception:  # noqa: BLE001 — جمع‌آوری خطا نباید خودش خطا بده
            pass


_collector = None


def install():
    """handler جمع‌آوری خطا رو (یک‌بار) روی root logger می‌ذاره و برمی‌گردونه."""
    global _collector
    if _collector is None:
        _collector = _ErrorCollector()
        logging.getLogger().addHandler(_collector)
    return _collector


def _fingerprint(run_name, text):
    """کلید خطا: نام اجرا + متن خطا با ارقام حذف‌شده (تا زمان/تعداد/قیمت باعث «خطای جدید» نشه). فرمت: «اجرا#هش»."""
    norm = re.sub(r"\d+", "#", text)[:120]
    return f"{run_name}#" + hashlib.sha1(f"{run_name}|{norm}".encode("utf-8")).hexdigest()[:10]


def _problems(run_name, lines, failed, fatal):
    """خطاهای «متمایز» این اجرا: {کلید: متن}. اجرای شکست‌خورده بدون خط ERROR هم یک خطای مستقل حساب می‌شه."""
    found = {}
    for line in lines:
        found.setdefault(_fingerprint(run_name, line), line)
    if fatal is not None:
        text = f"{type(fatal).__name__}: {fatal}"
        found.setdefault(_fingerprint(run_name, "FATAL " + text), "💥 " + text)
    if not found and failed:
        found[_fingerprint(run_name, "failed")] = "اجرا ناموفق بود (بدون خط ERROR)"
    return found


def _load_state():
    """وضعیت ذخیره‌شده تو Gist: {"streak": {کلید خطا: تعداد اجرای پشت‌سرهم}, "notified": {کلید خطا: زمان ارسال پیام}}.
    notified یعنی برای این خطا پیام رفته و تا رفع شدنش تکرار نمی‌شه.
    Returns: (state, ok). ok=False یعنی Gist خونده نشد (caller باید fail-open عمل کنه: خطا رو پنهان نکنه).
    کلیدهای فرمت‌های قدیمی (sent و ...) نادیده گرفته می‌شن."""
    raw, status = gist_store.read_json(STATE_FILE, "خواندن error_notify_state")
    if status not in ("ok", "missing") or (raw is not None and not isinstance(raw, dict)):
        return {"streak": {}, "notified": {}}, False
    raw = raw or {}
    streak = raw.get("streak") if isinstance(raw.get("streak"), dict) else {}
    notified = raw.get("notified") if isinstance(raw.get("notified"), dict) else {}
    return {"streak": dict(streak), "notified": dict(notified)}, True


def _save_state(state):
    state["streak"] = {k: v for k, v in state["streak"].items() if v}
    gist_store.write_json(STATE_FILE, {"streak": state["streak"], "notified": state["notified"]},
                          "ذخیره error_notify_state")


def _run_url():
    server, repo, run_id = (os.getenv(k) for k in ("GITHUB_SERVER_URL", "GITHUB_REPOSITORY", "GITHUB_RUN_ID"))
    return f"{server}/{repo}/actions/runs/{run_id}" if server and repo and run_id else None


def build_message(run_name, failed, fatal, lines, streak=1):
    now = datetime.now(pytz.timezone(TIMEZONE)).strftime("%Y-%m-%d %H:%M:%S")
    # برچسب پروژه اول پیامه چون این کانال با ریپوهای دیگه (مثلاً بورس) مشترکه
    parts = [f"🚨 [{ERROR_PROJECT_LABEL}] خطا در {run_name}"]
    repo = os.getenv("GITHUB_REPOSITORY")
    if repo:
        parts.append(f"📦 ریپو: {repo}")
    parts.append(f"🕐 {now} (تهران)")
    if fatal is not None and not any(str(l).startswith("💥") for l in lines):
        parts.append(f"💥 خطای بحرانی: {type(fatal).__name__}: {fatal}"[:MAX_LINE_CHARS])
    parts.append("وضعیت: اجرا ناموفق بود" if failed else "وضعیت: اجرا ادامه پیدا کرد ولی خطا لاگ شد")
    if streak > 1:
        parts.append(f"🔁 {streak} اجرای پشت‌سرهم با خطا (تا رفع خطا پیام تکراری نمی‌رسه)")
    if lines:
        parts.append(f"خطاها ({min(len(lines), MAX_LINES_IN_MESSAGE)} از {len(lines)}):")
        parts += [f"• {line[:MAX_LINE_CHARS]}" for line in lines[-MAX_LINES_IN_MESSAGE:]]
    url = _run_url()
    if url:
        parts.append(f"🔗 {url}")
    return redact("\n".join(parts))[:MAX_MESSAGE_CHARS]


def notify_run_problems(run_name, failed=False, fatal=None, min_consecutive=None):
    """اگه اجرا شکست خورده یا ERROR لاگ شده، طبق قانون بالای فایل (برای هر خطا جدا) به کانال خطا می‌فرسته.
    True = ارسال شد. هیچ exceptionی بیرون نمی‌ده.

    min_consecutive: تعداد اجرای پشت‌سرهمِ لازم (پیش‌فرض ERROR_NOTIFY_MIN_CONSECUTIVE). 1 = فوری و بدون وضعیت
    (گزارش هفتگی/ماهانه که یک‌بار تو دوره می‌ان). FORCE_RUN هم همیشه فوری و بدون وضعیته."""
    try:
        if min_consecutive is None:
            min_consecutive = ERROR_NOTIFY_MIN_CONSECUTIVE
        one_shot = min_consecutive <= 1 or is_force_run()

        lines = list(_collector.records) if _collector else []
        current = _problems(run_name, lines, failed, fatal)   # {کلید: متن}

        # --- وضعیت: خطاهای این اجرا ادامه پیدا می‌کنن، بقیه‌ی خطاهای این اجرا (برطرف‌شده) صفر می‌شن ---
        state, ok = ({"streak": {}, "notified": {}}, False)
        if not one_shot:
            state, ok = _load_state()
            if ok:
                mine = lambda k: k == run_name or k.startswith(f"{run_name}#")  # noqa: E731
                for k in [k for k in list(state["streak"]) + list(state["notified"]) if mine(k) and k not in current]:
                    state["streak"].pop(k, None)
                    state["notified"].pop(k, None)

        if not current:
            if ok:
                _save_state(state)  # فقط وقتی چیزی صفر شده نوشته می‌شه (بعد از تغییر)
            return False

        if not (TELEGRAM_BOT_TOKEN and TELEGRAM_ERROR_CHAT_ID):
            logger.warning("⚠️ TELEGRAM_BOT_TOKEN/TELEGRAM_ERROR_CHAT_ID تنظیم نشده — خطا به کانال خطا نرفت")
            return False

        if one_shot or not ok:
            # فوری، یا Gist خونده نشد (نمی‌تونیم بشماریم؛ خطا رو پنهان نمی‌کنیم)
            report, streak = dict(current), (1 if one_shot else min_consecutive)
        else:
            report, streak = {}, 1
            for k, text in current.items():
                state["streak"][k] = int(state["streak"].get(k, 0)) + 1
                if k in state["notified"]:
                    continue                      # این خطا قبلاً گزارش شده و هنوز ادامه داره
                if state["streak"][k] >= min_consecutive:
                    report[k] = text
                    streak = max(streak, state["streak"][k])
                else:
                    logger.info(f"⏳ یک خطا تو {state['streak'][k]} اجرا از {min_consecutive} اجرای لازم — هنوز گزارش نشد")

        if not report:
            if ok:
                _save_state(state)
            if not one_shot:
                logger.info("🔕 خطای تازه‌ای برای گزارش نیست (یا هنوز ۲ اجرای پشت‌سرهم نشده، یا قبلاً گزارش شده)")
            return False

        resp = requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            json={
                "chat_id": TELEGRAM_ERROR_CHAT_ID,
                "text": build_message(run_name, failed, fatal, list(report.values()), streak),
                "disable_web_page_preview": True,
            },
            timeout=15,
        )
        if not resp.ok:
            logger.warning(f"⚠️ ارسال به کانال خطا ناموفق (HTTP {resp.status_code}): {redact(resp.text[:200])}")
            if ok:
                _save_state(state)  # شمارنده‌ها ذخیره می‌شن؛ «گزارش‌شده» نه، پس اجرای بعدی دوباره تلاش می‌کنه
            return False

        if ok:
            for k in report:
                state["notified"][k] = time.time()
            _save_state(state)
        logger.info("📣 خطا به کانال خطا ارسال شد")
        return True
    except Exception as e:  # noqa: BLE001 — اعلام خطا نباید اجرا رو خراب کنه
        logger.warning(f"⚠️ خطا در ارسال به کانال خطا: {redact(str(e))}")
        return False
