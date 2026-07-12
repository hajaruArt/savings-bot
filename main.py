import os
import re
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
PERSONAL_ACCOUNT = "4491"
BUSINESS_ACCOUNT = "7851"
BENZINE_SENDERS = ["سعد", "أماني"]
BENZINE_AMOUNT = 300
FIXED_SENDER = "سعد"
FIXED_AMOUNT = 200
FIXED_DAY = 10

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
            "UPDATE balances SET amount = amount + %s WHERE type=%s RETURNING amount",
            (delta, type_),
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
        cur.execute("SELECT 1 FROM processed_updates WHERE update_id=%s", (update_id,))
        if cur.fetchone():
            return True
        cur.execute(
            "INSERT INTO processed_updates (update_id) VALUES (%s) ON CONFLICT DO NOTHING",
            (update_id,),
        )
        conn.commit()
        return False


def categorize_expense(details):
    cats = {
        "🍔 مطاعم": ["مطعم", "كافيه", "برغر", "pizza", "شاورما", "مكدونالد", "كنتاكي", "starbucks", "coffee", "cafe"],
        "🛒 تسوق": ["بنده", "الدانوب", "كارفور", "لولو", "هايبر", "market", "العثيم"],
        "⛽ وقود": ["أرامكو", "الأهلية", "بنزين", "محطة", "fuel", "gas"],
        "🏥 صحة": ["صيدلية", "مستشفى", "عيادة", "pharmacy", "clinic", "دواء"],
        "📱 اتصالات": ["stc", "موبايلي", "زين", "اتصالات"],
        "🚗 مواصلات": ["أوبر", "كريم", "uber", "careem", "تاكسي"],
        "🏠 سكن": ["إيجار", "كهرباء", "ماء", "صيانة"],
    }
    if not details:
        return "📦 أخرى"
    d = details.lower()
    for cat, kws in cats.items():
        if any(k.lower() in d for k in kws):
            return cat
    return "📦 أخرى"


def handle_command(conn, text):
    t = clean_text(text)
    now = datetime.now()
    d, tm = now.strftime("%Y-%m-%d"), now.strftime("%H:%M")

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
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO incomes (date,time,amount,type,details,account,invest) VALUES (%s,%s,%s,%s,%s,%s,0)",
                (d, tm, amt, "دخل عمل", "أضيف يدوياً", "تجاري"),
            )
            conn.commit()
        send_telegram(f"✅ أضفت {fmt(amt)} ر.س للبزنس\n💼 رصيد البزنس: {fmt(new_bal)} ر.س")
        return

    m = re.match(r"^سحب شخصي\s+([\d.]+)", t, re.I)
    if m:
        amt = float(m.group(1))
        current = get_balance(conn, "personal")
        if amt > current:
            send_telegram(f"⚠️ الرصيد غير كافٍ\n👤 رصيدك الشخصي: {fmt(current)} ر.س")
        else:
            new_bal = adjust_balance(conn, "personal", -amt)
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO expenses (date,time,amount,category,details,account) VALUES (%s,%s,%s,%s,%s,%s)",
                    (d, tm, amt, "💸 سحب شخصي", "سحب يدوي", "شخصي"),
                )
                conn.commit()
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
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO expenses (date,time,amount,category,details,account) VALUES (%s,%s,%s,%s,%s,%s)",
                    (d, tm, amt, "🔄 سحب بزنس", "سحب يدوي", "تجاري"),
                )
                conn.commit()
            send_telegram(f"💸 سحبت {fmt(amt)} ر.س من البزنس\n💼 المتبقي: {fmt(new_bal)} ر.س")
        return

    m = re.match(r"^ادخار\s+([\d.]+)", t, re.I)
    if m:
        amt = float(m.group(1))
        current = get_balance(conn, "personal")
        if amt > current:
            send_telegram(f"⚠️ الرصيد غير كافٍ للادخار\n👤 رصيدك الشخصي: {fmt(current)} ر.س")
        else:
            new_per = adjust_balance(conn, "personal", -amt)
            new_sav = adjust_balance(conn, "savings", amt)
            send_telegram(
                f"📈 تم نقل {fmt(amt)} ر.س للمدخرات\n👤 الشخصي المتبقي: {fmt(new_per)} ر.س\n📈 صندوق المدخرات: {fmt(new_sav)} ر.س"
            )
        return

    m = re.match(r"^استثمر\s+([\d.]+)", t, re.I)
    if m:
        amt = float(m.group(1))
        current = get_balance(conn, "savings")
        if amt > current:
            send_telegram(f"⚠️ رصيد المدخرات غير كافٍ\n📈 المدخرات: {fmt(current)} ر.س")
        else:
            new_sav = adjust_balance(conn, "savings", -amt)
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO expenses (date,time,amount,category,details,account) VALUES (%s,%s,%s,%s,%s,%s)",
                    (d, tm, amt, "📈 استثمار", "خصم من المدخرات", "المدخرات"),
                )
                conn.commit()
            send_telegram(f"📈 تم استثمار {fmt(amt)} ر.س\n📈 المدخرات المتبقية: {fmt(new_sav)} ر.س")
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

    if re.match(r"^(أرصدة|رصيد)$", t, re.I):
        p = get_balance(conn, "personal")
        b = get_balance(conn, "business")
        s = get_balance(conn, "savings")
        send_telegram(
            f"💰 أرصدتك الحالية:\n\n👤 الشخصي: {fmt(p)} ر.س\n💼 البزنس: {fmt(b)} ر.س\n📈 المدخرات: {fmt(s)} ر.س\n━━━━━━━━━\nالإجمالي: {fmt(p+b+s)} ر.س"
        )
        return

    if t == "تقرير":
        send_daily_report(conn)
        return

    send_telegram(
        "🤖 الأوامر المتاحة:\n\n"
        "💰 الأرصدة:\n• رصيد شخصي 500\n• رصيد بزنس 500\n• سحب شخصي 200\n• سحب بزنس 200\n• ادخار 500\n• استثمر 300\n\n"
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

    msg = (
        "✨زاد الله أموالك سيدتي✨\n\n"
        f"📊 تقريرك اليومي\n📅 {today}\n\n"
        f"💰 ملخص الشهر:\n• المداخيل: {fmt(total_income)} ر.س\n• المصاريف: {fmt(total_exp)} ر.س\n• الفرق: {fmt(total_income-total_exp)} ر.س\n\n"
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
    msg += f"━━━━━━━━━━━━━━\n🏦 أرصدتك الحالية:\n👤 الشخصي: {fmt(p)} ر.س\n💼 البزنس: {fmt(b)} ر.س\n📈 المدخرات: {fmt(s)} ر.س"
    send_telegram(msg)


def parse_rajhi_sms(sms):
    result = {"type": None, "amount": None, "details": "", "account": "شخصي"}
    if not sms or "حوالة بين حساباتك" in sms:
        return result
    if BUSINESS_ACCOUNT in sms:
        result["account"] = "تجاري"
    elif PERSONAL_ACCOUNT in sms:
        result["account"] = "شخصي"

    m = re.search(r"(?:SAR|SR)\s*([\d,]+\.?\d*)|([\d,]+\.?\d*)\s*ريال|مبلغ[:\s]*([\d,]+\.?\d*)", sms, re.I)
    if m:
        raw = m.group(1) or m.group(2) or m.group(3)
        result["amount"] = float(raw.replace(",", ""))

    name_m = re.search(r"من\s*\d+\s*;([^;\n]+)", sms)
    from_m = re.search(r"من[\d;:]*([^;\n،,0-9][^\n،,]*)", sms)
    at_m = re.search(r"لدى\s+([^\n،,]+)", sms)
    match = name_m or from_m or at_m
    result["details"] = match.group(1).strip() if match else ""

    if re.search(r"شراء\s*PoS|POS", sms, re.I):
        result["type"] = "مصروف"
        return result
    if re.search(r"تم سحب|سحب.*تحويل|حوالة محلية صادرة|خصم", sms, re.I):
        result["type"] = "سحب_صادر"
        return result
    if re.search(r"تم ايداع|تم الإيداع|إيداع", sms, re.I):
        result["type"] = "مداخيل"
        return result
    if re.search(r"حوالة داخلية واردة|تحويل وارد", sms, re.I):
        is_benzine = any(n in result["details"] for n in BENZINE_SENDERS) and result["amount"] == BENZINE_AMOUNT
        today_day = date.today().day
        is_fixed = (
            FIXED_SENDER in result["details"]
            and result["amount"] == FIXED_AMOUNT
            and abs(today_day - FIXED_DAY) <= 2
        )
        result["type"] = "بنزين" if is_benzine else "ثابت" if is_fixed else "مداخيل"
        return result
    return result


def handle_sms(conn, sms):
    parsed = parse_rajhi_sms(sms)
    if not parsed["type"] or not parsed["amount"]:
        return
    now = datetime.now()
    d, tm, amt = now.strftime("%Y-%m-%d"), now.strftime("%H:%M"), parsed["amount"]

    if parsed["type"] == "مصروف":
        cat = categorize_expense(parsed["details"])
        new_bal = adjust_balance(conn, "personal", -amt)
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO expenses (date,time,amount,category,details,account) VALUES (%s,%s,%s,%s,%s,%s)",
                (d, tm, amt, cat, parsed["details"], "شخصي"),
            )
            conn.commit()
        send_telegram(f"💸 مصروف جديد\nالمبلغ: {fmt(amt)} ر.س\nالتصنيف: {cat}\n👤 رصيدك الشخصي: {fmt(new_bal)} ر.س")

    elif parsed["type"] == "مداخيل":
        invest = round(amt * INVEST_PERCENT, 2)
        acct = parsed["account"]
        bal_type = "business" if acct == "تجاري" else "personal"
        new_bal = adjust_balance(conn, bal_type, amt)
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO incomes (date,time,amount,type,details,account,invest) VALUES (%s,%s,%s,%s,%s,%s,%s)",
                (d, tm, amt, "دخل عمل", parsed["details"], acct, invest),
            )
            conn.commit()
        send_telegram(f"✅ دخل جديد\nالمبلغ: {fmt(amt)} ر.س\nالحساب: {acct}\n📈 للاستثمار: {fmt(invest)} ر.س")

    elif parsed["type"] == "سحب_صادر":
        acct = parsed["account"]
        bal_type = "business" if acct == "تجاري" else "personal"
        new_bal = adjust_balance(conn, bal_type, -amt)
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO expenses (date,time,amount,category,details,account) VALUES (%s,%s,%s,%s,%s,%s)",
                (d, tm, amt, "🔄 تحويل صادر", f"سحب من {acct}", acct),
            )
            conn.commit()
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
