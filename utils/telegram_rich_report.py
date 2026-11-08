"""ارسال گزارش به‌صورت Telegram Rich Message (Bot API 10.1+): یک پیام با عکس‌های جاسازی‌شده + متن بلند.

مستقل از مسیر آلبوم فعلی: هیچ‌چیز تو Gist/Sheet ذخیره نمی‌کنه، پین نمی‌کنه، و پیام قبلی رو ویرایش نمی‌کنه
(هر بار یه پیام جدید). فقط وقتی صدا زده می‌شه که FORCE_RUN و RICH_REPORT هر دو true باشن.

دو حالت (RICH_MERGE):
  True  → طلا و نقره تو «یک» پیام (دلار یک بار): هر کالا بعد از پردازش stage می‌شه و آخرِ حلقه‌ی main با flush_rich_report یک‌جا می‌ره.
  False → برای هر کالا یک پیام جدا (send_rich_report).
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
USE_SLIDESHOW = True  # هر جفت عکس تو یه اسلایدشو (<tg-slideshow>) که ورق می‌خوره؛ False = عکس‌ها زیر هم
RICH_MERGE = True     # طلا + نقره تو یک پیام
_HTML_TAG_RE = re.compile(r"</?[a-zA-Z][^>]*>")

_LABELS = {"gold": "طلا", "silver": "نقره"}
_STAGED = {}  # commodity → {"images": [img1, img2], "ctx": dict|None, "html": str|None}


# ───────────────────────── HTML سازنده‌ها ─────────────────────────
def _e(text):
    return _html.escape(str(text), quote=False)


def _table(headers, rows):
    head = "".join(f"<th>{_e(h)}</th>" for h in headers)
    body = "".join("<tr>" + "".join(f"<td>{_e(c) if str(c).strip() else '—'}</td>" for c in r) + "</tr>" for r in rows)
    return f"<table bordered striped><tr>{head}</tr>{body}</table>"


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


def _footer(channel):
    """هندل کانال: چپ‌چین و قابل کلیک. دکمه‌ی url با style=link (لینک بدون border) تو ردیف align=left."""
    handle = _e(channel)
    url = f"https://t.me/{_e(channel.lstrip('@'))}"
    return (f'<tg-button-row align="left"><tg-button type="url" style="link" url="{url}">'
            f'🔗 {handle}</tg-button></tg-button-row>')


def _dollar_html(ctx):
    d = ctx["dollar"]
    parts = ["<h3>💵 دلار</h3>", _table(d["head"], d["rows"])]
    if d.get("valuation"):
        parts.append(_table(["ارزش‌گذاری دلار", "قیمت (تومان)", "فاصله از آخرین معامله"], d["valuation"]))
    return parts


def _commodity_html(ctx):
    """اونس، صندوق‌ها، دارایی‌ها (و جدول ریسک به ریوارد زیر شمش)."""
    o = ctx["ounce"]
    t = f" 🕐 {_e(o['time'])}" if o.get("time") else ""
    parts = [f"<h3>{_e(o['title'])}{t}</h3>", _table(o["head"], o["rows"])]

    f = ctx.get("funds")
    if f:
        parts += [f"<h3>{_e(f['title'])}</h3>", _table(["شاخص", "مقدار", "نسبت"], f["rows"])]

    for a in ctx["assets"]:
        t = f" 🕐 {_e(a['time'])}" if a.get("time") else ""
        parts += [f"<h3>{_e(a['title'])}{t}</h3>", _table(["شاخص", "مقدار"], a["rows"])]
        for tb in a.get("tables", []):
            parts += [f"<h4>{_e(tb['title'])}</h4>", _table(tb["headers"], tb["rows"])]
    return parts


def _slideshow(ids):
    imgs = "".join(f'<img src="tg://photo?id={i}"/>' for i in ids)
    return f"<tg-slideshow>{imgs}</tg-slideshow>"


def render_rich_caption(ctx):
    """چیدمان Rich یک کالا از روی ctx (مقدارها از قبل فرمت‌شده‌ان). عکس‌ها جدا (build_rich_message) اضافه می‌شن."""
    parts = [f"<p>🔄 <b>آپدیت: {_e(ctx['update'])}</b></p>"]
    parts += _dollar_html(ctx)
    parts += _commodity_html(ctx)
    parts += ["<hr/>", _footer(ctx["channel"])]
    return "\n".join(parts)


def render_rich_merged(parts_list):
    """یک پیام برای چند کالا: آپدیت و جدول دلار (یک بار) → برای هر کالا: عنوان، اسلایدشوی عکس‌هاش، اونس/صندوق‌ها/دارایی‌ها → هندل.

    parts_list: لیست مرتب {"commodity", "images", "ctx"|"html"}؛ عکس n‌ام (۱..) با id=img{n} تو media می‌ره.
    """
    ctxs = [p["ctx"] for p in parts_list if p.get("ctx")]
    out = []
    if ctxs:
        out.append(f"<p>🔄 <b>آپدیت: {_e(ctxs[0]['update'])}</b></p>")
        out += _dollar_html(ctxs[0])  # دلار بین طلا و نقره مشترکه؛ نسخه‌ی طلا (با کف/ارزش/سقف) اولویت داره
    n = 0
    for p in parts_list:
        ids = [f"img{n + i + 1}" for i in range(len(p["images"]))]
        n += len(p["images"])
        title = p["ctx"]["section_title"] if p.get("ctx") else f"بازار {_LABELS.get(p['commodity'], p['commodity'])}"
        out += [f"<h2>{_e(title)}</h2>", _slideshow(ids)]
        out += _commodity_html(p["ctx"]) if p.get("ctx") else [p.get("html") or ""]
    channel = ctxs[0]["channel"] if ctxs else ""
    if channel:
        out += ["<hr/>", _footer(channel)]
    return "\n".join(out)


def _degrade(html):
    """قالب امن‌تر برای وقتی که تلگرام ۴۰۰ می‌ده: اسلایدشو → عکس‌های ساده، دکمه‌ی هندل → یه پاراگراف (mention خودکار)."""
    html = re.sub(r"<tg-slideshow>(.*?)</tg-slideshow>", lambda m: m.group(1).replace("/><", "/>\n<"), html, flags=re.S)
    html = re.sub(r"<tg-button-row[^>]*>.*?<tg-button[^>]*>(.*?)</tg-button>.*?</tg-button-row>", r"<p>\1</p>", html, flags=re.S)
    return html


# ───────────────────────── ارسال ─────────────────────────
def _rich_payload(html, n_images):
    return {
        "html": html,
        "media": [{"id": f"img{i + 1}", "media": {"type": "photo", "media": f"attach://photo{i + 1}"}}
                  for i in range(n_images)],
        "is_rtl": True,
    }


def build_rich_message(caption, slideshow=True, body_is_html=False):
    """(کنار گذاشته‌شده برای سازگاری) عکس‌ها بالا و بعدش متن؛ فقط برای مسیر تک‌کالایی."""
    body = caption if body_is_html else caption_to_rich_html(caption)
    imgs = _slideshow(["img1", "img2"]) if slideshow else '<img src="tg://photo?id=img1"/>\n<img src="tg://photo?id=img2"/>'
    return _rich_payload(imgs + "\n" + body, 2)


def _send(bot_token, chat_id, images, html, label):
    """True = ارسال شد | False = شکست قطعی (شبکه/۴۲۹/...) | None = ۴۰۰ (رد شدن payload؛ قابل تلاش مجدد با قالب ساده‌تر)."""
    if len(html) > RICH_MESSAGE_LIMIT:
        logger.error(f"❌ [{label}] متن Rich {len(html)} کاراکتره (حد {RICH_MESSAGE_LIMIT})")
        return False

    rich = _rich_payload(html, len(images))
    url = f"https://api.telegram.org/bot{bot_token}/sendRichMessage"
    data = {"chat_id": chat_id, "rich_message": json.dumps(rich, ensure_ascii=False)}

    for attempt in (1, 2):
        files = {f"photo{i + 1}": (f"img{i + 1}.png", io.BytesIO(b), "image/png") for i, b in enumerate(images)}
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
                        f"{sum(len(b) for b in images) / 1e6:.2f}MB, {len(html)} کاراکتر)")
            return True

        retry_after = (body.get("parameters") or {}).get("retry_after")
        if resp.status_code == 429 and retry_after and attempt == 1:
            logger.warning(f"⏳ [{label}] Rich: 429 — {retry_after}s صبر و تلاش مجدد")
            time.sleep(int(retry_after) + 1)
            continue

        logger.error(f"❌ [{label}] Rich ناموفق: HTTP {resp.status_code} — {str(body.get('description', resp.text))[:300]}")
        return None if resp.status_code == 400 else False
    return False


def _send_with_fallback(bot_token, chat_id, images, html, label):
    """اول قالب کامل؛ اگه ۴۰۰ شد یک بار با قالب ساده (فقط وقتی اولی ارسال نشده، پس پیام تکراری نمی‌شه)."""
    if not USE_SLIDESHOW:
        html = _degrade(html)
    ok = _send(bot_token, chat_id, images, html, label)
    if ok is None:
        degraded = _degrade(html)
        if degraded != html:
            logger.warning(f"⚠️ [{label}] Rich با قالب کامل رد شد — تلاش مجدد با قالب ساده (بدون اسلایدشو/دکمه)")
            ok = _send(bot_token, chat_id, images, degraded, label)
    return bool(ok)


def send_rich_report(bot_token, chat_id, img1_bytes, img2_bytes, caption, label="", body_is_html=False):
    """یک پیام Rich جدید برای یک کالا. True/False؛ بدون ذخیره‌ی هیچ‌چیز."""
    body = caption if body_is_html else caption_to_rich_html(caption)
    html = _slideshow(["img1", "img2"]) + "\n" + body
    return _send_with_fallback(bot_token, chat_id, [img1_bytes, img2_bytes], html, label)


# ───────────────────────── حالت ادغام (طلا + نقره = یک پیام) ─────────────────────────
def stage_rich_part(commodity, img1_bytes, img2_bytes, ctx=None, html=None):
    """بخش یک کالا رو برای پیام ادغام‌شده نگه می‌داره (هنوز ارسال نمی‌کنه)."""
    _STAGED[commodity] = {"commodity": commodity, "images": [img1_bytes, img2_bytes], "ctx": ctx, "html": html}
    logger.info(f"📦 [{commodity}] بخش Rich آماده‌ی ادغام شد ({len(_STAGED)} بخش)")


def flush_rich_report(bot_token, chat_id):
    """همه‌ی بخش‌های stage‌شده رو تو «یک» پیام Rich می‌فرسته. True اگه ارسال شد (یا چیزی برای ارسال نبود)."""
    parts = list(_STAGED.values())
    _STAGED.clear()
    if not parts:
        logger.warning("⚠️ RICH_REPORT: هیچ بخشی برای ارسال stage نشده بود")
        return False
    images = [b for p in parts for b in p["images"]]
    html = render_rich_merged(parts)
    label = "+".join(p["commodity"] for p in parts)
    return _send_with_fallback(bot_token, chat_id, images, html, label)
