"""گزارش کانال = یک Telegram Rich Message (Bot API 10.1+) برای طلا + نقره، با عکس‌های جاسازی‌شده.

این ماژول فقط «چیدمان» و «ارسال/ویرایش خام» رو انجام می‌ده؛ تصمیم «پیام جدید یا ویرایش پیام امروز»
(و ذخیره‌ی message_id تو Gist) تو telegram_sender.flush_rich_report گرفته می‌شه.

جریان: هر کالا بعد از پردازش با stage_rich_part نگه داشته می‌شه → آخر حلقه‌ی main با take_staged
هر دو کالا تو «یک» پیام رندر می‌شن (دلار یک بار).

چیدمان (از بالا به پایین):
  آپدیت  →  ⚡ خلاصه بازار (دلار/دلار درهم/اونس/شمش/صندوق‌ها)  →  💵 دلار  →  برای هر کالا: عنوان، اسلایدشوی عکس‌ها،
  اونس، صندوق‌ها، «قیمت دارایی‌ها» (یک جدول جمع‌وجور)، 🔬 دلار/اونس ضمنی (تاشو)، ⚖️ R/R شمش، 🏦 عسکه  →  هندل کانال
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

_LABELS = {"gold": "طلا", "silver": "نقره"}
_STAGED = {}  # commodity → {"commodity", "images": [img1, img2], "ctx": dict|None, "html": str|None}


# ───────────────────────── HTML سازنده‌ها ─────────────────────────
class H(str):
    """رشته‌ی HTML آماده (escape نمی‌شه). بقیه‌ی رشته‌ها تو جدول‌ها escape می‌شن."""


def _e(text):
    return _html.escape(str(text), quote=False)


def _cell(c, empty="—"):
    if isinstance(c, H):
        return str(c)
    return _e(c) if str(c).strip() else empty


class Span(H):
    """سلول HTML آماده که چند ستون رو می‌گیره (colspan). مثلاً Span("خرید | فروش", 3)."""

    def __new__(cls, text, n=2):
        obj = super().__new__(cls, text)
        obj.n = n
        return obj


def _table(headers, rows, aligns=None):
    """جدول با حاشیه و ردیف‌های راه‌راه. aligns: لیست align برای هر ستون (مثلاً [None, 'center', 'center']).

    سلول Span (colspan) همیشه وسط‌چین می‌شه.
    """
    def attr(i, span=1):
        # تلگرام دسکتاپ سلول‌های بدون align رو چپ‌چین نشون می‌داد (is_rtl فقط جهت متن رو عوض می‌کنه): همه‌ی ستون‌ها (از جمله اول) وسط‌چین
        if span > 1:
            return f' colspan="{span}" align="center"'
        al = (aligns[i] if aligns and aligns[i] else None) or "center"
        return f' align="{al}"'

    def row_html(cells, tag, empty):
        out, col = [], 0
        for c in cells:
            span = c.n if isinstance(c, Span) else 1
            out.append(f"<{tag}{attr(col, span)}>{_cell(c, empty=empty)}</{tag}>")
            col += span
        return "<tr>" + "".join(out) + "</tr>"

    head = row_html(headers, "th", "")
    body = "".join(row_html(r, "td", "—") for r in rows)
    return f"<table bordered striped>{head}{body}</table>"


def _collapsible(title, inner_parts):
    """بلاک تاشو (بسته به‌صورت پیش‌فرض). title متن ساده‌ست (escape می‌شه)."""
    # \u200F (RLM) اول تیتر: جهت راست‌به‌چپ و راست‌چین‌شدنِ تیتر تاشو (حتی وقتی با ایموجی/عدد شروع می‌شه)
    return [f"<details><summary>\u200F{_e(title)}</summary>", *inner_parts, "</details>"]


def caption_to_rich_html(caption):
    """(فقط fallback) تبدیل کپشن متنی HTML (بلاک‌ها با خط خالی جدا شدن) به HTML Rich: هر بلاک یه <p> با <br/>."""
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


def _slideshow(ids):
    imgs = "".join(f'<img src="tg://photo?id={i}"/>' for i in ids)
    return f"<tg-slideshow>{imgs}</tg-slideshow>"


def _plain_title(title):
    """اسم دارایی بدون ایموجی ابتدایی (تو جدول دارایی‌ها و دلار/اونس ضمنی)."""
    return re.sub(r"^[^\w\u0600-\u06FF]+", "", str(title)).strip()


def _time_tag(t):
    return f" 🕐 {_e(t)}" if t else ""


def _quick_html(ctxs):
    """⚡ خلاصه بازار: دلار، دلار درهم، اونس و شمش هر کالا تو یک جدول سه‌ستونه (روی گوشی بدون اسکرول افقی جا می‌شه)."""
    rows = []
    d = ctxs[0]["dollar"].get("last")
    if d:
        rows.append([H("<b>💵 دلار</b> <sub>تومان</sub>"), d[0], d[1]])
    dh = next((c["dollar"].get("dirham") for c in ctxs if c["dollar"].get("dirham")), None)
    if dh:  # زیر دلار؛ dh = (قیمت, تغییر روزانه|None). بدون تغییر روزانه، قیمت رو دو ستون می‌گیره (بدون خط‌تیره‌ی خالی)
        label = H("<b>🇦🇪 دلار درهم</b> <sub>تومان</sub>")
        rows.append([label, dh[0], dh[1]] if dh[1] else [label, Span(_e(dh[0]), 2)])
    for c in ctxs:
        for row in c.get("quick", []):
            rows.append([H(row[0]), *row[1:]])
    if not rows:
        return []
    return ["<h3>⚡ خلاصه بازار</h3>", _table(["", "قیمت", "تغییر روزانه"], rows, [None, "center", "center"])]


def _dollar_html(ctx):
    d = ctx["dollar"]
    parts = ["<h2>💵 دلار</h2>", _table(d["head"], d["rows"])]
    if d.get("valuation"):  # ارزش‌گذاری (کف/ارزش/سقف/درهم) تاشوئه
        parts += _collapsible("🧮 ارزش‌گذاری دلار",
                              [_table(["", "قیمت (تومان)", "فاصله از آخرین معامله"], d["valuation"])])
    return parts


def _assets_html(ctx):
    """یک جدول جمع‌وجور برای همه‌ی دارایی‌ها (به‌جای یک جدول جدا برای هرکدوم) + دلار/اونس ضمنی تو بلاک تاشو."""
    assets = ctx["assets"]
    if not assets:
        return []
    price_rows, implied_rows = [], []
    for a in assets:
        name = H(f"{_e(_plain_title(a['title']))}" + (f" <sub>{_e(a['time'])}</sub>" if a.get("time") else ""))
        price_rows.append([name, a["price"], a["change"], a["bubble"]])
        implied_rows.append([H(_e(_plain_title(a["title"]))), a["dollar"], a["ounce"]])
    parts = ["<h3>🏷 قیمت دارایی‌ها</h3>",
             _table(["دارایی", "قیمت (تومان)", "تغییر", "حباب"], price_rows, [None, "center", "center", "center"]),
             "<details><summary>\u200F🔬 دلار و اونس ضمنی هر دارایی</summary>",
             _table(["دارایی", H("دلار ضمنی <sub>تومان</sub>"), "اونس ضمنی"], implied_rows, [None, "center", "center"]),
             "</details>"]
    for a in assets:
        for tb in a.get("tables", []):
            parts += _collapsible(tb["title"], [_table(tb["headers"], tb["rows"], [None, "center", "center"])])
    return parts


def _eskeh_html(ctx):
    e = ctx.get("eskeh")
    if not e:
        return []
    return _collapsible(f"{e['title']}{_time_tag(e.get('time'))}",
                        [_table(["شاخص", "مقدار"], e["rows"], [None, "center"])])


def _commodity_html(ctx):
    """اونس، صندوق‌ها، دارایی‌ها، R/R و عسکه."""
    o = ctx["ounce"]
    parts = [f"<h3>{_e(o['title'])}{_time_tag(o.get('time'))}</h3>",
             _table(o["head"], o["rows"], [None, "center", "center", "center"])]

    f = ctx.get("funds")
    if f:
        parts.append(f"<h3>{_e(f['title'])}</h3>")
        for tb in f["tables"]:  # دو جدول: «قیمت و حباب» و «معاملات و پول»
            headers = tb.get("headers") or ["شاخص", "مقدار", "نسبت", "مبنای نسبت"]
            parts += [f"<h4>{_e(tb['title'])}</h4>",
                      _table(headers, tb["rows"], [None] + ["center"] * (len(headers) - 1))]
        top = f.get("top")
        if top and top["rows"]:  # ۵ صندوق پرمعامله: تاشو
            parts += _collapsible(top["summary"], [_table(top["headers"], top["rows"], [None, "center", "center", "center"])])

    parts += _assets_html(ctx)
    parts += _eskeh_html(ctx)
    return parts


def render_rich_merged(parts_list):
    """یک پیام برای چند کالا: آپدیت + خلاصه بازار + دلار (یک بار) → برای هر کالا: عنوان، اسلایدشوی عکس‌هاش و جزئیات → هندل.

    parts_list: لیست مرتب {"commodity", "images", "ctx"|"html"}؛ عکس n‌ام (۱..) با id=img{n} تو media می‌ره.
    """
    ctxs = [p["ctx"] for p in parts_list if p.get("ctx")]
    out = []
    if ctxs:
        out.append("<h1>📊 طلا و نقره لحظه‌ای</h1>")
        out.append(f"<p>🔄 <b>آپدیت: {_e(ctxs[0]['update'])}</b></p>")
        out.append("<hr/>")
        out += _quick_html(ctxs)
        out += _dollar_html(ctxs[0])  # دلار بین طلا و نقره مشترکه؛ نسخه‌ی طلا (با کف/ارزش/سقف) اولویت داره
    n = 0
    for p in parts_list:
        ids = [f"img{n + i + 1}" for i in range(len(p["images"]))]
        n += len(p["images"])
        title = p["ctx"]["section_title"] if p.get("ctx") else f"بازار {_LABELS.get(p['commodity'], p['commodity'])}"
        out += ["<hr/>", f"<h2>{_e(title)}</h2>", _slideshow(ids)]
        out += _commodity_html(p["ctx"]) if p.get("ctx") else [p.get("html") or ""]
    channel = ctxs[0]["channel"] if ctxs else ""
    if channel:
        out += ["<hr/>", _footer(channel)]
    return "\n".join(out)


def _degrade(html):
    """قالب امن‌تر برای وقتی که تلگرام ۴۰۰ می‌ده: اسلایدشو → عکس‌های ساده، دکمه‌ی هندل → یه پاراگراف (mention خودکار)،
    بلاک تاشو → عنوان + محتوای باز."""
    html = re.sub(r"<tg-slideshow>(.*?)</tg-slideshow>", lambda m: m.group(1).replace("/><", "/>\n<"), html, flags=re.S)
    html = re.sub(r"<tg-button-row[^>]*>.*?<tg-button[^>]*>(.*?)</tg-button>.*?</tg-button-row>", r"<p>\1</p>", html, flags=re.S)
    html = re.sub(r"<details[^>]*>\s*<summary>(.*?)</summary>", r"<h4>\1</h4>", html, flags=re.S)
    html = html.replace("</details>", "")
    return html


# ───────────────────────── ارسال / ویرایش ─────────────────────────
def _rich_payload(html, n_images):
    return {
        "html": html,
        "media": [{"id": f"img{i + 1}", "media": {"type": "photo", "media": f"attach://photo{i + 1}"}}
                  for i in range(n_images)],
        "is_rtl": True,
    }


def _post(method, bot_token, fields, images, html, label):
    """یک فراخوانی Bot API با payload Rich. برمی‌گردونه (وضعیت، message_id|توضیح):
        ("ok", message_id)   موفق
        ("400", توضیح)       رد شدن payload (قابل تلاش مجدد با قالب ساده‌تر)
        ("same", توضیح)      «message is not modified» (برای ویرایش یعنی چیزی نیاز به تغییر نبود)
        ("fail", توضیح)      شکست قطعی (شبکه/۴۲۹/...)
    """
    if len(html) > RICH_MESSAGE_LIMIT:
        logger.error(f"❌ [{label}] متن Rich {len(html)} کاراکتره (حد {RICH_MESSAGE_LIMIT})")
        return "fail", "too long"

    rich = _rich_payload(html, len(images))
    url = f"https://api.telegram.org/bot{bot_token}/{method}"
    data = dict(fields, rich_message=json.dumps(rich, ensure_ascii=False))

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
            # ویرایش idempotent ـه (همون محتوا دوباره) → یک بار با کمی مکث تلاش مجدد (مثلاً Connection reset).
            # ارسال پیام جدید عمداً تکرار نمی‌شه: اگه تلگرام پیام رو گرفته باشه ولی جوابش گم شده باشه، پیام تکراری می‌شد.
            if method == "editMessageText" and attempt == 1:
                logger.warning(f"⚠️ [{label}] Rich/{method}: خطای شبکه ({e}) — ۲ ثانیه بعد تلاش مجدد")
                time.sleep(2)
                continue
            logger.error(f"❌ [{label}] Rich/{method}: خطای شبکه: {e}")
            return "fail", str(e)

        elapsed = time.time() - t0
        if resp.status_code == 200 and body.get("ok"):
            result = body.get("result")
            mid = result.get("message_id") if isinstance(result, dict) else None
            logger.info(f"✅ [{label}] Rich/{method} موفق (ID: {mid}, {elapsed:.1f}s, "
                        f"{sum(len(b) for b in images) / 1e6:.2f}MB, {len(html)} کاراکتر)")
            return "ok", mid

        desc = str(body.get("description", resp.text))[:300]
        retry_after = (body.get("parameters") or {}).get("retry_after")
        if resp.status_code == 429 and retry_after and attempt == 1:
            logger.warning(f"⏳ [{label}] Rich/{method}: 429 — {retry_after}s صبر و تلاش مجدد")
            time.sleep(int(retry_after) + 1)
            continue

        if resp.status_code == 400 and "not modified" in desc.lower():
            return "same", desc
        logger.error(f"❌ [{label}] Rich/{method} ناموفق: HTTP {resp.status_code} — {desc}")
        return ("400" if resp.status_code == 400 else "fail"), desc
    return "fail", "retries exhausted"


def _with_fallback(call, html, label):
    """اول قالب کامل؛ اگه ۴۰۰ شد یک بار با قالب ساده (فقط وقتی اولی اعمال نشده، پس پیام تکراری نمی‌شه)."""
    if not USE_SLIDESHOW:
        html = _degrade(html)
    status, info = call(html)
    if status == "400":
        degraded = _degrade(html)
        if degraded != html:
            logger.warning(f"⚠️ [{label}] Rich با قالب کامل رد شد — تلاش مجدد با قالب ساده (بدون اسلایدشو/دکمه/تاشو)")
            status, info = call(degraded)
    return status, info


def send_rich(bot_token, chat_id, images, html, label=""):
    """پیام Rich جدید. message_id برمی‌گردونه، یا None اگه نرفت."""
    status, info = _with_fallback(
        lambda h: _post("sendRichMessage", bot_token, {"chat_id": chat_id}, images, h, label), html, label)
    return info if status == "ok" else None


def edit_rich(bot_token, chat_id, message_id, images, html, label=""):
    """ویرایش پیام Rich قبلی (متن + عکس‌ها). خروجی یکی از:
        "ok"      ویرایش شد (یا «message is not modified»: چیزی برای تغییر نبود)
        "retry"   خطای موقت (شبکه/timeout/۴۲۹/۵xx): پیام سالمه؛ دقیقه‌ی بعد دوباره ویرایش کن، پیام جدید نفرست
        "resend"  تلگرام این ویرایش رو رد کرد (۴۰۰: پیام پیدا نشد/قابل ویرایش نیست/payload ناسازگار): پیام جدید لازمه
    """
    status, _ = _with_fallback(
        lambda h: _post("editMessageText", bot_token, {"chat_id": chat_id, "message_id": message_id}, images, h, label),
        html, label)
    if status in ("ok", "same"):
        return "ok"
    return "retry" if status == "fail" else "resend"


def delete_message(bot_token, chat_id, message_id):
    """حذف پیام (best-effort)؛ برای وقتی که ویرایش نشد و پیام جدید جاش رفت تا کانال شلوغ نشه."""
    try:
        r = requests.post(f"https://api.telegram.org/bot{bot_token}/deleteMessage",
                          data={"chat_id": chat_id, "message_id": message_id}, timeout=30)
        return r.status_code == 200
    except requests.RequestException as e:
        logger.warning(f"⚠️ حذف پیام قدیمی ناموفق: {e}")
        return False


PIN_DAILY_REPORT = True    # پیام امروز (اولین ارسال هر روز) پین بشه و پین دیروز برداشته بشه
DELETE_PIN_NOTICE = True   # پیام سرویسی «… یک پیام را پین کرد» پاک بشه (False = بمونه)


def _api(bot_token, method, **data):
    """فراخوانی ساده‌ی Bot API (بدون فایل). (ok, توضیح)."""
    try:
        r = requests.post(f"https://api.telegram.org/bot{bot_token}/{method}", data=data, timeout=30)
        try:
            body = r.json()
        except ValueError:
            body = {}
        return (r.status_code == 200 and bool(body.get("ok"))), str(body.get("description", ""))[:200]
    except requests.RequestException as e:
        return False, str(e)[:200]


def pin_report(bot_token, chat_id, message_id, prev_id=None):
    """پیام گزارش رو پین می‌کنه (بدون اعلان صوتی)، پین قبلی (دیروز) رو برمی‌داره و پیام سرویسی «پین شد» رو پاک می‌کنه.

    پیام سرویسی بلافاصله بعد از پین ساخته می‌شه و شناسه‌ش «شناسه‌ی پیام پین‌شده + ۱»ـه (Bot API شناسه‌ش رو برنمی‌گردونه).
    اگه تو همون چند ثانیه کسی تو کانال پیام بذاره، این فرض غلط می‌شه؛ برای همین فقط یه بار و فقط بعد از پینِ موفق
    پاک می‌کنیم و با DELETE_PIN_NOTICE=False خاموش می‌شه. حقوق ادمین: پین = can_edit_messages، حذف = can_delete_messages.
    """
    if not PIN_DAILY_REPORT or not message_id:
        return False
    if prev_id and prev_id != message_id:
        ok, desc = _api(bot_token, "unpinChatMessage", chat_id=chat_id, message_id=prev_id)
        logger.info(f"📌 پین قبلی (ID: {prev_id}) برداشته شد" if ok else f"⚠️ برداشتن پین قبلی ناموفق: {desc}")
    ok, desc = _api(bot_token, "pinChatMessage", chat_id=chat_id, message_id=message_id, disable_notification=True)
    if not ok:
        logger.warning(f"⚠️ پین ناموفق (ربات ادمین با حق «ویرایش پیام‌ها» هست؟): {desc}")
        return False
    logger.info(f"📌 پیام (ID: {message_id}) پین شد")
    if DELETE_PIN_NOTICE:
        for attempt in (1, 2):
            ok, desc = _api(bot_token, "deleteMessage", chat_id=chat_id, message_id=int(message_id) + 1)
            if ok:
                logger.info(f"🧹 پیام سرویسی «پین شد» (ID: {int(message_id) + 1}) پاک شد")
                return True
            if attempt == 1:
                time.sleep(1)
        logger.warning(f"⚠️ پاک‌کردن پیام سرویسی پین ناموفق (ربات حق «حذف پیام‌ها» داره؟): {desc}")
    return True


# ───────────────────────── مرحله‌بندی (طلا + نقره = یک پیام) ─────────────────────────
def stage_rich_part(commodity, img1_bytes, img2_bytes, ctx=None, html=None):
    """بخش یک کالا رو برای پیام ادغام‌شده نگه می‌داره (هنوز ارسال نمی‌کنه)."""
    _STAGED[commodity] = {"commodity": commodity, "images": [img1_bytes, img2_bytes], "ctx": ctx, "html": html}
    logger.info(f"📦 [{commodity}] بخش Rich آماده‌ی ادغام شد ({len(_STAGED)} بخش)")


def take_staged():
    """بخش‌های stage‌شده رو به شکل (images, html, label) برمی‌گردونه و خالی‌شون می‌کنه؛ None اگه چیزی نبود."""
    parts = list(_STAGED.values())
    _STAGED.clear()
    if not parts:
        return None
    images = [b for p in parts for b in p["images"]]
    return images, render_rich_merged(parts), "+".join(p["commodity"] for p in parts)
