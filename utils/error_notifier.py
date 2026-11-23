# utils/error_notifier.py
"""ارسال خطاهای اجرا به کانال خطا (TELEGRAM_ERROR_CHAT_ID).

قبلاً هیچ مسیری برای اعلام خطا نبود و فقط لاگ GitHub Actions بود. چون تریگر هر ۱ دقیقه‌ست، یک خطای پایدار
بدون cooldown ۱ پیام در دقیقه می‌فرستاد؛ پس هر «اثر انگشتِ خطا» (متن خطا با ارقام حذف‌شده) تا
ERROR_NOTIFY_COOLDOWN_MIN دقیقه فقط یک‌بار گزارش می‌شه (وضعیتش تو فایل جدای Gist نگه‌داری می‌شه).

نحوه‌ی کار:
  1. install() یک handler روی root logger می‌ذاره و همه‌ی لاگ‌های ERROR+ این اجرا رو جمع می‌کنه.
  2. آخر اجرا notify_run_problems(...) صدا زده می‌شه: اگه اجرا شکست خورده یا خطای لاگ شده باشه، خلاصه‌اش
     (با redact توکن‌ها) به کانال خطا می‌ره. خود ارسال هیچ‌وقت اجرا رو خراب نمی‌کنه.
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
    TELEGRAM_BOT_TOKEN, TELEGRAM_ERROR_CHAT_ID, ERROR_NOTIFY_COOLDOWN_MIN, ERROR_PROJECT_LABEL, TIMEZONE, is_force_run,
)
from utils import gist_store
from utils.log_safety import redact

logger = logging.getLogger(__name__)

STATE_FILE = "error_notify_state.json"
MAX_COLLECTED = 30          # حداکثر تعداد خطای نگه‌داشته‌شده در حافظه
MAX_LINES_IN_MESSAGE = 5    # حداکثر خطای نمایش‌داده‌شده در پیام
MAX_LINE_CHARS = 300
MAX_MESSAGE_CHARS = 3500    # سقف تلگرام ۴۰۹۶ کاراکتره
STATE_TTL_SECONDS = 24 * 3600


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


def _fingerprint(run_name, lines):
    """اثر انگشت خطا: نام اجرا + اولین خطا با ارقام حذف‌شده (تا زمان/تعداد/قیمت باعث کلید جدید نشه)."""
    first = re.sub(r"\d+", "#", lines[0])[:120] if lines else "no-error-lines"
    return hashlib.sha1(f"{run_name}|{first}".encode("utf-8")).hexdigest()[:12]


def _in_cooldown(key):
    """True اگه همین خطا تو cooldown گزارش شده. اگه Gist خونده نشد False (ترجیح: گزارش بدی تا خطا گم نشه)."""
    if is_force_run():
        return False
    state, status = gist_store.read_json(STATE_FILE, "خواندن error_notify_state")
    if status not in ("ok", "missing") or not isinstance(state, dict):
        return False
    last = state.get(key)
    return bool(last) and (time.time() - float(last)) < ERROR_NOTIFY_COOLDOWN_MIN * 60


def _record_sent(key):
    if is_force_run():
        return
    state, status = gist_store.read_json(STATE_FILE, "خواندن error_notify_state")
    if status not in ("ok", "missing") or not isinstance(state, dict):
        state = {}
    now = time.time()
    state = {k: v for k, v in state.items() if now - float(v) < STATE_TTL_SECONDS}
    state[key] = now
    gist_store.write_json(STATE_FILE, state, "ذخیره error_notify_state")


def _run_url():
    server, repo, run_id = (os.getenv(k) for k in ("GITHUB_SERVER_URL", "GITHUB_REPOSITORY", "GITHUB_RUN_ID"))
    return f"{server}/{repo}/actions/runs/{run_id}" if server and repo and run_id else None


def build_message(run_name, failed, fatal, lines):
    now = datetime.now(pytz.timezone(TIMEZONE)).strftime("%Y-%m-%d %H:%M:%S")
    # برچسب پروژه اول پیامه چون این کانال با ریپوهای دیگه (مثلاً بورس) مشترکه
    parts = [f"🚨 [{ERROR_PROJECT_LABEL}] خطا در {run_name}"]
    repo = os.getenv("GITHUB_REPOSITORY")
    if repo:
        parts.append(f"📦 ریپو: {repo}")
    parts.append(f"🕐 {now} (تهران)")
    if fatal is not None:
        parts.append(f"💥 خطای بحرانی: {type(fatal).__name__}: {fatal}"[:MAX_LINE_CHARS])
    parts.append("وضعیت: اجرا ناموفق بود" if failed else "وضعیت: اجرا ادامه پیدا کرد ولی خطا لاگ شد")
    if lines:
        parts.append(f"آخرین خطاها ({min(len(lines), MAX_LINES_IN_MESSAGE)} از {len(lines)}):")
        parts += [f"• {line[:MAX_LINE_CHARS]}" for line in lines[-MAX_LINES_IN_MESSAGE:]]
    url = _run_url()
    if url:
        parts.append(f"🔗 {url}")
    return redact("\n".join(parts))[:MAX_MESSAGE_CHARS]


def notify_run_problems(run_name, failed=False, fatal=None):
    """اگه اجرا شکست خورده یا ERROR لاگ شده، خلاصه رو به کانال خطا می‌فرسته. True = ارسال شد.
    هیچ exceptionی بیرون نمی‌ده."""
    try:
        lines = list(_collector.records) if _collector else []
        if not failed and fatal is None and not lines:
            return False

        if not (TELEGRAM_BOT_TOKEN and TELEGRAM_ERROR_CHAT_ID):
            logger.warning("⚠️ TELEGRAM_BOT_TOKEN/TELEGRAM_ERROR_CHAT_ID تنظیم نشده — خطا به کانال خطا نرفت")
            return False

        key = _fingerprint(run_name, lines if lines else [str(fatal or "failed")])
        if _in_cooldown(key):
            logger.info(f"🔕 خطای مشابه تو {ERROR_NOTIFY_COOLDOWN_MIN} دقیقه‌ی اخیر گزارش شده — ارسال نشد")
            return False

        resp = requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            json={
                "chat_id": TELEGRAM_ERROR_CHAT_ID,
                "text": build_message(run_name, failed, fatal, lines),
                "disable_web_page_preview": True,
            },
            timeout=15,
        )
        if not resp.ok:
            logger.warning(f"⚠️ ارسال به کانال خطا ناموفق (HTTP {resp.status_code}): {redact(resp.text[:200])}")
            return False

        _record_sent(key)
        logger.info("📣 خطا به کانال خطا ارسال شد")
        return True
    except Exception as e:  # noqa: BLE001 — اعلام خطا نباید اجرا رو خراب کنه
        logger.warning(f"⚠️ خطا در ارسال به کانال خطا: {redact(str(e))}")
        return False
