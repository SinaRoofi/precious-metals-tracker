"""ارسال گزارش به‌صورت Telegram Rich Message (Bot API 10.1+): یک پیام با دو عکس جاسازی‌شده + متن بلند.

مستقل از مسیر آلبوم فعلی: هیچ‌چیز تو Gist/Sheet ذخیره نمی‌کنه، پین نمی‌کنه، و پیام قبلی رو ویرایش نمی‌کنه
(هر بار یه پیام جدید). فقط وقتی صدا زده می‌شه که FORCE_RUN و RICH_REPORT هر دو true باشن.
"""

import html as _html
import io
import json
import logging
import re
import time

import requests

logger = logging.getLogger(__name__)

RICH_MESSAGE_LIMIT = 32768  # کاراکتر، طبق مستندات Bot API
RICH_TIMEOUT = 90
USE_SLIDESHOW = True  # دو عکس تو یه اسلایدشو (<tg-slideshow>) که ورق می‌خوره؛ False = دو عکس زیر هم
_HTML_TAG_RE = re.compile(r"</?[a-zA-Z][^>]*>")


def caption_to_rich_html(caption):
    """تبدیل کپشن HTML فعلی (بلاک‌ها با خط خالی جدا شدن) به HTML Rich: هر بلاک یه <p> با <br/> بین خطوط،
    و یه جداکننده‌ی واقعی <hr/> قبل از بلاک آخر (هندل کانال)."""
    blocks = [b for b in re.split(r"\n\s*\n", caption.strip()) if b.strip()]
    parts = []
    for i, block in enumerate(blocks):
        lines = [ln for ln in block.split("\n") if ln.strip()]
        if i == len(blocks) - 1 and len(blocks) > 1 and len(lines) == 1 and "@" in lines[0]:
            parts.append("<hr/>")
        parts.append("<p>" + "<br/>".join(lines) + "</p>")
    return "\n".join(parts)


def _e(text):
    return _html.escape(str(text), quote=False)


def _table(headers, rows):
    head = "".join(f"<th>{_e(h)}</th>" for h in headers)
    body = "".join("<tr>" + "".join(f"<td>{_e(c) if str(c).strip() else '—'}</td>" for c in r) + "</tr>" for r in rows)
    return f"<table bordered striped><tr>{head}</tr>{body}</table>"


def _footer(channel):
    """هندل کانال چپ‌چین (سلول جدول بدون border با align=left) و قابل کلیک."""
    link = f'<a href="https://t.me/{_e(channel.lstrip("@"))}">{_e(channel)}</a>'
    return f'<table><tr><td align="left">🔗 {link}</td></tr></table>'


def render_rich_caption(ctx):
    """چیدمان Rich گزارش از روی ctx (همه‌ی مقدارها از قبل فرمت‌شده‌ان؛ این تابع فقط ساختار HTML می‌سازه).

    ctx: update, dollar{head, rows, valuation}, ounce{title, rows}, funds{title, rows}|None,
         assets[{title, time, rows, tables:[{title, headers, rows}]}], channel
    """
    d, o = ctx["dollar"], ctx["ounce"]
    parts = [f"<p>🔄 <b>آپدیت: {_e(ctx['update'])}</b></p>"]

    parts.append("<h3>💵 دلار</h3>")
    parts.append(_table(d["head"], d["rows"]))
    if d.get("valuation"):
        parts.append(_table(["ارزش‌گذاری دلار", "قیمت (تومان)", "فاصله از آخرین معامله"], d["valuation"]))

    t = f" 🕐 {_e(o['time'])}" if o.get("time") else ""
    parts.append(f"<h3>{_e(o['title'])}{t}</h3>")
    parts.append(_table(o["head"], o["rows"]))

    f = ctx.get("funds")
    if f:
        parts.append(f"<h3>{_e(f['title'])}</h3>")
        parts.append(_table(["شاخص", "مقدار", "نسبت"], f["rows"]))

    for a in ctx["assets"]:
        t = f" 🕐 {_e(a['time'])}" if a.get("time") else ""
        parts.append(f"<h3>{_e(a['title'])}{t}</h3>")
        parts.append(_table(["شاخص", "مقدار"], a["rows"]))
        for tb in a.get("tables", []):
            parts.append(f"<h4>{_e(tb['title'])}</h4>")
            parts.append(_table(tb["headers"], tb["rows"]))

    parts.append("<hr/>")
    parts.append(_footer(ctx["channel"]))
    return "\n".join(parts)


def build_rich_message(caption, slideshow=True, body_is_html=False):
    """عکس‌ها بالا (همون ترتیب آلبوم فعلی) و بعدش متن. slideshow=True: هر دو عکس تو یه اسلایدشوی ورق‌خور."""
    imgs = '<img src="tg://photo?id=img1"/><img src="tg://photo?id=img2"/>'
    images_html = f"<tg-slideshow>{imgs}</tg-slideshow>" if slideshow else imgs.replace("/><", "/>\n<")
    body = caption if body_is_html else caption_to_rich_html(caption)
    html = images_html + "\n" + body
    return {
        "html": html,
        "media": [
            {"id": "img1", "media": {"type": "photo", "media": "attach://photo1"}},
            {"id": "img2", "media": {"type": "photo", "media": "attach://photo2"}},
        ],
        "is_rtl": True,
    }


def send_rich_report(bot_token, chat_id, img1_bytes, img2_bytes, caption, label="", body_is_html=False):
    """یک پیام Rich جدید می‌فرسته. True/False برمی‌گردونه؛ هیچ fallback و هیچ ذخیره‌ای نداره."""
    ok = _send(bot_token, chat_id, img1_bytes, img2_bytes, build_rich_message(caption, USE_SLIDESHOW, body_is_html), label)
    if ok is None and USE_SLIDESHOW:
        # اسلایدشو رد شد (۴۰۰) → یک بار با دو عکس ساده‌ی زیر هم؛ فقط وقتی اولی ارسال نشده، پس پیام تکراری نمی‌شه
        logger.warning(f"⚠️ [{label}] Rich با اسلایدشو رد شد — تلاش مجدد با عکس‌های ساده")
        ok = _send(bot_token, chat_id, img1_bytes, img2_bytes, build_rich_message(caption, False, body_is_html), label)
    return bool(ok)


def _send(bot_token, chat_id, img1_bytes, img2_bytes, rich, label):
    """True = ارسال شد | False = شکست قطعی (شبکه/۴۲۹/...) | None = ۴۰۰ (رد شدن payload؛ قابل تلاش مجدد با قالب دیگه)."""
    if len(rich["html"]) > RICH_MESSAGE_LIMIT:
        logger.error(f"❌ [{label}] متن Rich {len(rich['html'])} کاراکتره (حد {RICH_MESSAGE_LIMIT})")
        return False

    url = f"https://api.telegram.org/bot{bot_token}/sendRichMessage"
    data = {"chat_id": chat_id, "rich_message": json.dumps(rich, ensure_ascii=False)}

    for attempt in (1, 2):
        files = {
            "photo1": ("treemap.png", io.BytesIO(img1_bytes), "image/png"),
            "photo2": ("charts.png", io.BytesIO(img2_bytes), "image/png"),
        }
        t0 = time.time()
        try:
            resp = requests.post(url, data=data, files=files, timeout=RICH_TIMEOUT)
            try:
                body = resp.json()
            except ValueError:
                body = {}
        except requests.RequestException as e:
            logger.error(f"❌ [{label}] Rich: خطای شبکه: {e}")
            return False

        elapsed = time.time() - t0
        if resp.status_code == 200 and body.get("ok"):
            mid = (body.get("result") or {}).get("message_id")
            logger.info(f"✅ [{label}] Rich Message ارسال شد (ID: {mid}, {elapsed:.1f}s, "
                        f"{(len(img1_bytes) + len(img2_bytes)) / 1e6:.2f}MB)")
            return True

        retry_after = (body.get("parameters") or {}).get("retry_after")
        if resp.status_code == 429 and retry_after and attempt == 1:
            logger.warning(f"⏳ [{label}] Rich: 429 — {retry_after}s صبر و تلاش مجدد")
            time.sleep(int(retry_after) + 1)
            continue

        logger.error(f"❌ [{label}] Rich ناموفق: HTTP {resp.status_code} — {str(body.get('description', resp.text))[:300]}")
        return None if resp.status_code == 400 else False
    return False
