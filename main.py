import os
import re
import calendar
import unicodedata
from datetime import datetime, date
from flask import Flask, request
import psycopg2
import psycopg2.extras
import requests

app = Flask(__name__)

TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]
TELEGRAM_CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]
DATABASE_URL = os.environ["DATABASE_URL"]

INVEST_PERCENT = 0.10
SADAQAH_PERCENT = 0.05
BUSINESS_SAVINGS_PERCENT = 0.10

PERSONAL_ACCOUNT_MARK = "4491"   # يظهر كامل بالرسائل
BUSINESS_ACCOUNT_MARK = "851"    # آخر 3 أرقام، يظهر مقنّع بالرسائل مثل 271***851

BENZINE_SENDERS = []  # ملغى
FIXED_SENDER = "سعد"
FIXED_AMOUNT = 200
FIXED_DAY = 10

MONTHLY_PERSONAL_BUDGET = 200
SAFETY_MARGIN = 0.85  # نخلي المتاح يومياً أقل شوي من التقسيم المتساوي، عشان مصاريف عشوائية

INVISIBLE_CHARS = re.compile(r"[\u200B-\u200F\u202A-\u202E\uFEFF\u00A0]")


def db():
    return psycopg2.connect(DATABASE_URL, cursor_factory=psycopg2.extras.RealDictCursor)


def clean_text(t):
    t = INVISIBLE_CHARS.sub("", t or "")
    t = unicodedata.normalize("NFKC", t)
    return t.strip()


def fmt(n):
    return f"{float(n or 0):.2f}"


def get_balance(conn, type_):
    with conn.cursor() as cur:
        cur.execute("SELECT amount FROM balances WHERE type=%s", (type_,))
        row = cur.fetchone()
        return float(row["amount"]) if row else 0.0


def adjust_balance(conn, type_, delta):
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO balances (type, amount) VALUES (%s, %s) "
            "ON CONFLICT (type) DO UPDATE SET amount = balances.amount + EXCLUDED.amount "
            "RETURNING amount",
            (type_, delta),
        )
        row = cur.fetchone()
        conn.commit()
        return float(row["amount"])


def send_telegram(msg):
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            json={"chat_id": TELEGRAM_CHAT_ID, "text": str(msg)},
            timeout=10,
        )
        print("sendTelegram status=", r.status_code, r.text[:300])
    except Exception as e:
        print("sendTelegram error:", e)


def already_processed(conn, update_id):
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO processed_updates (update_id) VALUES (%s) ON CONFLICT DO NOTHING",
            (update_id,),
        )
        conn.commit()
        return cur.rowcount == 0


DEFAULT_CATEGORIES = {
    "🍔 مطاعم": ["مطعم", "كافيه", "برغر", "pizza", "شاورما", "مكدونالد", "كنتاكي", "starbucks", "coffee", "cafe"],
    "🛒 تسوق": ["بنده", "الدانوب", "كارفور", "لولو", "هايبر", "market", "العثيم"],
    "⛽ وقود": ["أرامكو", "الأهلية", "بنزين", "محطة", "fuel", "gas"],
    "🏥 صحة": ["صيدلية", "مستشفى", "عيادة", "pharmacy", "clinic", "دواء"],
    "📱 اتصالات": ["stc", "موبايلي", "زين", "اتصالات"],
    "🚗 مواصلات": ["أوبر", "كريم", "uber", "careem", "تاكسي"],
    "🏠 سكن": ["إيجار", "كهرباء", "ماء", "صيانة"],
}


def get_learned_category(conn, details):
    """يشوف هل سبق وصححنا تصنيف لنفس اسم المحل، ويرجع التصنيف المحفوظ إن وجد."""
    if not details:
        return None
    with conn.cursor() as cur:
        cur.execute("SELECT category FROM merchant_categories WHERE %s ILIKE '%%' || merchant_pattern || '%%' LIMIT 1", (details,))
        row = cur.fetchone()
        return row["category"] if row else None


def categorize_expense(conn, details):
    learned = get_learned_category(conn, details)
    if learned:
        return learned
    if not details:
        return "📦 أخرى"
    d = details.lower()
    for cat, kws in DEFAULT_CATEGORIES.items():
        if any(k.lower() in d for k in kws):
            return cat
    return "📦 أخرى"


def account_from_number(num):
    """يتحقق من آخر أرقام رقم حساب محدد (وليس أي مكان بالرسالة كلها)."""
    if not num:
        return None
    if num.endswith(PERSONAL_ACCOUNT_MARK):
        return "شخصي"
    if num.endswith(BUSINESS_ACCOUNT_MARK):
        return "تجاري"
    return None


CHARITY_KEYWORDS = ["خيري", "خيرية", "جمعيات", "جمعية", "تبرع", "صدقة"]


def suggest_business_split(amt):
    """يحسب المقترح فقط للعرض، بدون أي خصم فعلي من رصيد البزنس."""
    invest = round(amt * INVEST_PERCENT, 2)
    sadaqah = round(amt * SADAQAH_PERCENT, 2)
    savings = round(amt * BUSINESS_SAVINGS_PERCENT, 2)
    return invest, sadaqah, savings


def record_expense(conn, d, tm, amt, category, details, account):
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO expenses (date,time,amount,category,details,account) VALUES (%s,%s,%s,%s,%s,%s) RETURNING id",
            (d, tm, amt, category, details, account),
        )
        eid = cur.fetchone()["id"]
        conn.commit()
        return eid


def get_available_to_spend_today(conn):
    today = date.today()
    month_start = today.replace(day=1)
    with conn.cursor() as cur:
        cur.execute(
            "SELECT COALESCE(SUM(amount),0) AS total FROM expenses "
            "WHERE account='شخصي' AND date>=%s AND category NOT IN ('🔄 تحويل صادر', '📈 استثمار')",
            (month_start,),
        )
        spent = float(cur.fetchone()["total"])
    remaining_budget = max(MONTHLY_PERSONAL_BUDGET - spent, 0)
    days_in_month = calendar.monthrange(today.year, today.month)[1]
    days_remaining = max(days_in_month - today.day + 1, 1)
    daily = (remaining_budget / days_remaining) * SAFETY_MARGIN
    return daily, spent, remaining_budget


def handle_command(conn, text):
    t = clean_text(text)
    now = datetime.now()
    d, tm = now.strftime("%Y-%m-%d"), now.strftime("%H:%M")

    if re.match(r"^(مهام|المهام)$", t, re.I):
        with conn.cursor() as cur:
            cur.execute("SELECT task, date FROM tasks WHERE status='⏳ قيد التنفيذ' ORDER BY id DESC")
            rows = cur.fetchall()
        if rows:
            lines = "\n".join(f"• {r['task']} ({r['date']})" for r in rows)
            send_telegram(f"📋 مهامك المعلقة ({len(rows)}):\n\n{lines}")
        else:
            send_telegram("✅ ما عندك مهام معلقة")
        return

    if re.match(r"^(أوامر|اوامر|الأوامر|أمر|مساعدة|help)$", t, re.I):
        send_telegram(
            "🤖 كل الأوامر المتاحة:\n\n"
            "💰 الأرصدة والحركات:\n"
            "• تحديث [شخصي/بزنس/مدخرات/استثمار/صدقة] [مبلغ] (يحدد الرصيد كامل)\n"
            "• خصم [شخصي/بزنس/مدخرات/استثمار/صدقة] [مبلغ]\n"
            "• اضافة [شخصي/بزنس/مدخرات/استثمار/صدقة] [مبلغ]\n"
            "• رصيد شخصي [مبلغ]\n"
            "• رصيد بزنس [مبلغ]\n"
            "• سحب شخصي [مبلغ]\n"
            "• سحب بزنس [مبلغ]\n"
            "• ادخار [مبلغ] (من البزنس للمدخرات)\n"
            "• استثمر [مبلغ] (من صندوق الاستثمار)\n\n"
            "🏷️ تصحيح تصنيف آخر مصروف:\n"
            "• صحح: [التصنيف الصح]\n\n"
            "📋 المهام:\n"
            "• مهمة: [اسم المهمة]\n"
            "• انتهت: [اسم المهمة]\n"
            "• مهام (لعرض كل المهام المعلقة)\n\n"
            "📊 التقارير:\n"
            "• تقرير\n"
            "• أرصدة\n\n"
            "❓ لعرض هذي القائمة دايماً: أوامر"
        )
        return

    BALANCE_LABELS = {
        "شخصي": "personal", "بزنس": "business", "مدخرات": "savings",
        "استثمار": "investment", "صدقة": "sadaqah",
    }

    m = re.match(r"^تحديث\s+(شخصي|بزنس|مدخرات|استثمار|صدقة)\s+(-?[\d.]+)", t, re.I)
    if m:
        db_type = BALANCE_LABELS[m.group(1)]
        new_val = float(m.group(2))
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO balances (type, amount) VALUES (%s,%s) "
                "ON CONFLICT (type) DO UPDATE SET amount=EXCLUDED.amount",
                (db_type, new_val),
            )
            conn.commit()
        send_telegram(f"✅ تم تحديث رصيد {m.group(1)} إلى: {fmt(new_val)} ر.س")
        return

    m = re.match(r"^خصم\s+(شخصي|بزنس|مدخرات|استثمار|صدقة)\s+([\d.]+)", t, re.I)
    if m:
        db_type = BALANCE_LABELS[m.group(1)]
        new_bal = adjust_balance(conn, db_type, -float(m.group(2)))
        send_telegram(f"✅ تم خصم {fmt(m.group(2))} ر.س من {m.group(1)}\nالرصيد الحالي: {fmt(new_bal)} ر.س")
        return

    m = re.match(r"^اضافة\s+(شخصي|بزنس|مدخرات|استثمار|صدقة)\s+([\d.]+)", t, re.I)
    if m:
        db_type = BALANCE_LABELS[m.group(1)]
        new_bal = adjust_balance(conn, db_type, float(m.group(2)))
        send_telegram(f"✅ تم إضافة {fmt(m.group(2))} ر.س لـ{m.group(1)}\nالرصيد الحالي: {fmt(new_bal)} ر.س")
        return

    m = re.match(r"^رصيد شخصي\s+([\d.]+)", t, re.I)
    if m:
        amt = float(m.group(1))
        new_bal = adjust_balance(conn, "personal", amt)
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO incomes (date,time,amount,type,details,account,invest) VALUES (%s,%s,%s,%s,%s,%s,0)",
                (d, tm, amt, "دخل", "أضيف يدوياً", "شخصي"),
            )
            conn.commit()
        send_telegram(f"✅ أضفت {fmt(amt)} ر.س\n👤 رصيدك الشخصي: {fmt(new_bal)} ر.س")
        return

    m = re.match(r"^رصيد بزنس\s+([\d.]+)", t, re.I)
    if m:
        amt = float(m.group(1))
        new_bal = adjust_balance(conn, "business", amt)
        invest, sadaqah, savings = suggest_business_split(amt)
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO incomes (date,time,amount,type,details,account,invest) VALUES (%s,%s,%s,%s,%s,%s,0)",
                (d, tm, amt, "دخل عمل", "أضيف يدوياً", "تجاري"),
            )
            conn.commit()
        send_telegram(
            f"✅ دخل بزنس جديد: {fmt(amt)} ر.س\n"
            f"💼 رصيد البزنس الحالي: {fmt(new_bal)} ر.س\n\n"
            f"💡 مقترح (اختياري، ما يتخصم تلقائياً):\n"
            f"📈 استثمار: {fmt(invest)} ر.س | 🤲 صدقة: {fmt(sadaqah)} ر.س | 💰 مدخرات: {fmt(savings)} ر.س\n"
            f"لو نفذتيها فعلياً، سجليها بـ: اضافة استثمار/صدقة/مدخرات [مبلغ]"
        )
        return

    m = re.match(r"^سحب شخصي\s+([\d.]+)", t, re.I)
    if m:
        amt = float(m.group(1))
        current = get_balance(conn, "personal")
        if amt > current:
            send_telegram(f"⚠️ الرصيد غير كافٍ\n👤 رصيدك الشخصي: {fmt(current)} ر.س")
        else:
            new_bal = adjust_balance(conn, "personal", -amt)
            record_expense(conn, d, tm, amt, "💸 سحب شخصي", "سحب يدوي", "شخصي")
            send_telegram(f"💸 سحبت {fmt(amt)} ر.س\n👤 رصيدك المتبقي: {fmt(new_bal)} ر.س")
        return

    m = re.match(r"^سحب بزنس\s+([\d.]+)", t, re.I)
    if m:
        amt = float(m.group(1))
        current = get_balance(conn, "business")
        if amt > current:
            send_telegram(f"⚠️ الرصيد غير كافٍ\n💼 رصيد البزنس: {fmt(current)} ر.س")
        else:
            new_bal = adjust_balance(conn, "business", -amt)
            record_expense(conn, d, tm, amt, "🔄 سحب بزنس", "سحب يدوي", "تجاري")
            send_telegram(f"💸 سحبت {fmt(amt)} ر.س من البزنس\n💼 المتبقي: {fmt(new_bal)} ر.س")
        return

    m = re.match(r"^مهمة[:\s]+(.+)", t, re.I)
    if m:
        task = m.group(1).strip()
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO tasks (date,task,priority,status) VALUES (%s,%s,%s,%s)",
                (d, task, "متوسطة", "⏳ قيد التنفيذ"),
            )
            conn.commit()
        send_telegram(f"📌 تمت إضافة المهمة:\n{task}\n\nللإنهاء: انتهت: {task}")
        return

    m = re.match(r"^انتهت[:\s]+(.+)", t, re.I)
    if m:
        task_name = m.group(1).strip()
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE tasks SET status='✅ مكتملة' WHERE task ILIKE %s AND status='⏳ قيد التنفيذ' RETURNING id",
                (f"%{task_name}%",),
            )
            done = cur.fetchone()
            conn.commit()
        send_telegram(
            f"✅ اكتملت المهمة:\n{task_name}" if done else f"⚠️ ما لقيت المهمة:\n{task_name}\n\nتأكد من الكتابة بالضبط"
        )
        return

    # تصحيح تصنيف آخر مصروف + تعليم البوت اسم المحل للمستقبل
    m = re.match(r"^صحح[:\s]+(.+)", t, re.I)
    if m:
        new_cat = m.group(1).strip()
        with conn.cursor() as cur:
            cur.execute("SELECT id, details FROM expenses ORDER BY id DESC LIMIT 1")
            last = cur.fetchone()
            if not last:
                send_telegram("⚠️ ما فيه مصروف مسجّل لتصحيحه")
                return
            cur.execute("UPDATE expenses SET category=%s WHERE id=%s", (new_cat, last["id"]))
            if last["details"]:
                cur.execute(
                    "INSERT INTO merchant_categories (merchant_pattern, category) VALUES (%s,%s) "
                    "ON CONFLICT (merchant_pattern) DO UPDATE SET category=EXCLUDED.category",
                    (last["details"], new_cat),
                )
            conn.commit()
        send_telegram(f"✅ تم تصحيح التصنيف إلى: {new_cat}\n(وحفظته عشان يصنّف تلقائياً بالمرات الجاية)")
        return

    if re.match(r"^(أرصدة|رصيد)$", t, re.I):
        p = get_balance(conn, "personal")
        b = get_balance(conn, "business")
        s = get_balance(conn, "savings")
        inv = get_balance(conn, "investment")
        sdq = get_balance(conn, "sadaqah")
        daily, spent, remaining = get_available_to_spend_today(conn)
        send_telegram(
            f"💰 أرصدتك الحالية:\n\n"
            f"👤 الشخصي: {fmt(p)} ر.س\n"
            f"💼 البزنس: {fmt(b)} ر.س\n"
            f"📈 المدخرات: {fmt(s)} ر.س\n"
            f"📊 الاستثمار: {fmt(inv)} ر.س\n"
            f"🤲 الصدقة: {fmt(sdq)} ر.س\n"
            f"━━━━━━━━━\n"
            f"الإجمالي: {fmt(p+b+s+inv+sdq)} ر.س\n\n"
            f"💵 المتاح للصرف اليوم: {fmt(daily)} ر.س"
        )
        return

    if t == "تقرير":
        send_daily_report(conn)
        return

    send_telegram(
        "🤖 الأوامر المتاحة:\n\n"
        "💰 الأرصدة:\n• رصيد شخصي 500\n• رصيد بزنس 500\n• سحب شخصي 200\n• سحب بزنس 200\n• ادخار 500\n• استثمر 300\n\n"
        "🏷️ تصحيح تصنيف آخر مصروف:\n• صحح: مطاعم\n\n"
        "📋 المهام:\n• مهمة: اسم المهمة\n• انتهت: اسم المهمة\n\n"
        "📊 التقارير:\n• تقرير\n• أرصدة"
    )


def send_daily_report(conn):
    today = date.today()
    month_start = today.replace(day=1)
    with conn.cursor() as cur:
        cur.execute(
            "SELECT COALESCE(SUM(amount),0) AS total, category FROM expenses WHERE date>=%s GROUP BY category",
            (month_start,),
        )
        cats = cur.fetchall()
        total_exp = sum(float(r["total"]) for r in cats)

        cur.execute("SELECT COALESCE(SUM(amount),0) AS total FROM incomes WHERE date>=%s", (month_start,))
        total_income = float(cur.fetchone()["total"])

        cur.execute("SELECT task FROM tasks WHERE status='⏳ قيد التنفيذ' ORDER BY id DESC LIMIT 5")
        pending = [r["task"] for r in cur.fetchall()]

    top_cats = sorted(cats, key=lambda r: -float(r["total"]))[:4]
    p = get_balance(conn, "personal")
    b = get_balance(conn, "business")
    s = get_balance(conn, "savings")
    inv = get_balance(conn, "investment")
    sdq = get_balance(conn, "sadaqah")
    daily, spent, remaining = get_available_to_spend_today(conn)

    msg = (
        "✨زاد الله أموالك سيدتي✨\n\n"
        f"📊 تقريرك اليومي\n📅 {today}\n\n"
        f"💰 ملخص الشهر:\n• المداخيل: {fmt(total_income)} ر.س\n• المصاريف: {fmt(total_exp)} ر.س\n• الفرق: {fmt(total_income-total_exp)} ر.س\n\n"
        f"💵 ميزانية الصرف الشخصي الشهرية: {fmt(MONTHLY_PERSONAL_BUDGET)} ر.س\n"
        f"• المصروف فعلياً: {fmt(spent)} ر.س\n"
        f"• المتبقي: {fmt(remaining)} ر.س\n"
        f"• 🎯 المتاح للصرف اليوم: {fmt(daily)} ر.س\n\n"
    )
    if top_cats:
        msg += "📂 أكثر ما صرفت فيه:\n"
        for r in top_cats:
            msg += f"• {r['category']}: {fmt(r['total'])} ر.س\n"
        msg += "\n"
    if pending:
        msg += f"📋 مهامك المعلقة ({len(pending)}):\n" + "\n".join(f"• {p_}" for p_ in pending) + "\n\n"
    else:
        msg += "✅ ما عندك مهام معلقة\n\n"
    msg += (
        f"━━━━━━━━━━━━━━\n🏦 أرصدتك الحالية:\n"
        f"👤 الشخصي: {fmt(p)} ر.س\n💼 البزنس: {fmt(b)} ر.س\n📈 المدخرات: {fmt(s)} ر.س\n"
        f"📊 الاستثمار: {fmt(inv)} ر.س\n🤲 الصدقة: {fmt(sdq)} ر.س"
    )
    send_telegram(msg)


def parse_rajhi_sms(sms):
    result = {"type": None, "amount": None, "details": "", "account": None, "charity": False, "fixed": False}
    if not sms or "حوالة بين حساباتك" in sms:
        return result

    m = re.search(r"(?:SAR|SR)\s*([\d,]+\.?\d*)|([\d,]+\.?\d*)\s*ريال|مبلغ[:\s]*([\d,]+\.?\d*)", sms, re.I)
    if m:
        raw = m.group(1) or m.group(2) or m.group(3)
        result["amount"] = float(raw.replace(",", ""))

    name_m = re.search(r"من\s*\d+\s*;([^;\n]+)", sms)
    from_m = re.search(r"من[\d;:]*([^;\n،,0-9][^\n،,]*)", sms)
    at_m = re.search(r"لدى\s+([^\n،,]+)", sms)
    match = name_m or from_m or at_m
    result["details"] = match.group(1).strip() if match else ""

    # سحب من حسابك (شراء PoS أو سحب/تحويل صادر)
    acct_m = re.search(r"حسابك\s*[:\s]*([\d*]+)", sms)
    withdraw_account = account_from_number(acct_m.group(1)) if acct_m else None

    if re.search(r"شراء\s*PoS|POS", sms, re.I):
        result["type"] = "مصروف"
        result["account"] = withdraw_account or "شخصي"
        return result

    if re.search(r"تم سحب|سحب.*تحويل|حوالة محلية صادرة|خصم", sms, re.I):
        result["type"] = "سحب_صادر"
        result["account"] = withdraw_account or "شخصي"
        if any(k in sms for k in CHARITY_KEYWORDS):
            result["charity"] = True
        return result

    if re.search(r"تم ايداع|تم الإيداع|إيداع", sms, re.I):
        result["type"] = "مداخيل"
        result["account"] = withdraw_account or "شخصي"  # نفس "حسابك" تنطبق على الإيداع أيضاً
        return result

    if re.search(r"حوالة داخلية واردة|تحويل وارد", sms, re.I):
        # نحدد الحساب المُستقبل تحديداً (بعد "لـ")، مو أي رقم يظهر بالرسالة كلها
        dest_m = re.search(r"لـ\s*(\d+)", sms)
        dest_account = account_from_number(dest_m.group(1)) if dest_m else None
        if not dest_account:
            # الحساب المُستقبل مو حسابنا المتابَع (يمكن حساب ثالث أو طرف ثاني) — نتجاهلها
            return result
        result["account"] = dest_account
        today_day = date.today().day
        result["fixed"] = (
            FIXED_SENDER in result["details"]
            and result["amount"] == FIXED_AMOUNT
            and abs(today_day - FIXED_DAY) <= 2
        )
        result["type"] = "مداخيل"
        return result
    return result


def handle_sms(conn, sms):
    parsed = parse_rajhi_sms(sms)
    if not parsed["type"] or not parsed["amount"]:
        return
    now = datetime.now()
    d, tm, amt = now.strftime("%Y-%m-%d"), now.strftime("%H:%M"), parsed["amount"]

    if parsed["type"] == "مصروف":
        cat = categorize_expense(conn, parsed["details"])
        new_bal = adjust_balance(conn, "personal", -amt)
        record_expense(conn, d, tm, amt, cat, parsed["details"], "شخصي")
        send_telegram(f"💸 مصروف جديد\nالمبلغ: {fmt(amt)} ر.س\nالتصنيف: {cat}\n👤 رصيدك الشخصي: {fmt(new_bal)} ر.س\n\n(لو التصنيف غلط، صححيه بأمر: صحح: التصنيف الصح)")

    elif parsed["type"] == "مداخيل":
        acct = parsed["account"]
        if acct == "تجاري":
            new_bal = adjust_balance(conn, "business", amt)
            invest, sadaqah, savings = suggest_business_split(amt)
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO incomes (date,time,amount,type,details,account,invest) VALUES (%s,%s,%s,%s,%s,%s,0)",
                    (d, tm, amt, "دخل عمل", parsed["details"], acct),
                )
                conn.commit()
            send_telegram(
                f"✅ دخل بزنس جديد: {fmt(amt)} ر.س\n"
                f"💼 رصيد البزنس الحالي: {fmt(new_bal)} ر.س\n\n"
                f"💡 مقترح (اختياري):\n"
                f"📈 استثمار: {fmt(invest)} ر.س | 🤲 صدقة: {fmt(sadaqah)} ر.س | 💰 مدخرات: {fmt(savings)} ر.س"
            )
        else:
            new_bal = adjust_balance(conn, "personal", amt)
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO incomes (date,time,amount,type,details,account,invest) VALUES (%s,%s,%s,%s,%s,%s,0)",
                    (d, tm, amt, "دخل", parsed["details"], acct),
                )
                conn.commit()
            if parsed.get("fixed"):
                send_telegram(f"📌 المصروف الثابت الشهري وصل\nالمبلغ: {fmt(amt)} ر.س\n👤 رصيدك الشخصي: {fmt(new_bal)} ر.س")
            else:
                send_telegram(f"✅ دخل جديد\nالمبلغ: {fmt(amt)} ر.س\n👤 رصيدك الشخصي: {fmt(new_bal)} ر.س")

    elif parsed["type"] == "سحب_صادر":
        acct = parsed["account"]
        if parsed.get("charity"):
            new_sdq = adjust_balance(conn, "sadaqah", -amt)
            record_expense(conn, d, tm, amt, "🤲 صدقة/تبرع", parsed["details"] or "تحويل خيري", "الصدقة")
            send_telegram(f"🤲 صدقة/تبرع تم صرفها\nالمبلغ: {fmt(amt)} ر.س\n🤲 صندوق الصدقة المتبقي: {fmt(new_sdq)} ر.س")
        else:
            bal_type = "business" if acct == "تجاري" else "personal"
            new_bal = adjust_balance(conn, bal_type, -amt)
            record_expense(conn, d, tm, amt, "🔄 تحويل صادر", f"سحب من {acct}", acct)
            send_telegram(f"🔄 سحب من {acct}\nالمبلغ: {fmt(amt)} ر.س\nالمتبقي: {fmt(new_bal)} ر.س")


@app.route("/webhook", methods=["POST"])
def webhook():
    conn = db()
    try:
        data = request.get_json(silent=True)
        if data and "message" in data:
            update_id = data.get("update_id")
            if update_id is not None:
                if already_processed(conn, update_id):
                    return "OK"
            text = data.get("message", {}).get("text")
            if text:
                handle_command(conn, text)
        else:
            raw = request.get_data(as_text=True)
            if raw:
                handle_sms(conn, raw)
    except Exception as e:
        print("webhook error:", e)
    finally:
        conn.close()
    return "OK"


@app.route("/", methods=["GET"])
def home():
    return "Bot is running"


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
