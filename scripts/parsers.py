"""Парсеры выписок: Kaspi Gold и Halyk (Народный банк). Возвращают список операций (dict)."""
import re
import subprocess
from datetime import date

import pdfplumber

NUM = r"\d{1,3}(?:[ \u00a0]\d{3})*,\d\d"


def num(s):
    return float(s.replace("\u00a0", "").replace(" ", "").replace(",", "."))


def pdf_text(path):
    return subprocess.run(["pdftotext", "-layout", str(path), "-"],
                          capture_output=True, text=True, check=True).stdout


def detect_bank(path):
    head = pdf_text(path)[:3000]
    if "Kaspi" in head:
        return "kaspi"
    if "Народный Банк" in head or "halykbank" in head:
        return "halyk"
    return None


# ───────────────────────── KASPI ─────────────────────────
KASPI_SUMMARY = {
    "Пополнения": "Пополнение",
    "Поступления со своих счетов": "Поступление со своего счета",
    "Зачисления кредитов": "Зачисление кредита",
    "Переводы": "Перевод",
    "Переводы на свои счета": "Перевод на свой счет",
    "Покупки": "Покупка",
    "Снятия": "Снятие",
    "Разное": "Разное",
}
K_ROW = re.compile(r"^\s*(\d\d\.\d\d\.\d\d)\s+([+-])\s*(" + NUM + r")\s*₸\s+(.+?)\s{2,}(.*)$")
K_ROW2 = re.compile(r"^\s*(\d\d\.\d\d\.\d\d)\s+([+-])\s*(" + NUM + r")\s*₸\s+(\S.*)$")
K_NOISE = ("Kaspi Bank", "Приложение к Справке", "Сумма заблокирована", "Раздел «Краткое",
           "счета» содержит", "Дата  ", "Дата ")


def parse_kaspi(path):
    txt = pdf_text(path)
    m = re.search(r"Номер карты:\s*\*(\d+)", txt)
    card = m.group(1) if m else "?"
    m = re.search(r"Номер счета:\s*(KZ\w+)", txt)
    acc = m.group(1) if m else "?"
    m = re.search(r"Выписка по Kaspi Gold за период с (\d\d\.\d\d\.\d\d) по (\d\d\.\d\d\.\d\d)", txt)
    period = (m.group(1), m.group(2)) if m else (None, None)
    owner = None
    m = re.search(r"справка.*?подтверждает, что (.+?), ИИН (\d+)", txt, re.S | re.I)
    if m:
        owner = " ".join(m.group(1).split())

    # итоги из шапки для проверки
    summary = {}
    body_start = txt.find("Краткое содержание операций по карте")
    for line in txt[body_start:body_start + 2500].splitlines():
        for label, op in KASPI_SUMMARY.items():
            mm = re.match(r"^\s*" + re.escape(label) + r"\s+([+-])\s*(" + NUM + r")\s*₸", line)
            if mm:
                summary[op] = (-1 if mm.group(1) == "-" else 1) * num(mm.group(2))
    for key, rx in (("open", r"Доступно на \d\d\.\d\d\.\d\d\s+\+ (" + NUM + ")"),):
        mm = re.search(rx, txt[body_start:body_start + 800])
        if mm:
            summary["open"] = num(mm.group(1))

    rows = []
    for line in txt.splitlines():
        line = line.replace("\f", "")
        if not line.strip():
            continue
        m = K_ROW.match(line)
        if m:
            d, sg, amt, op, det = m.groups()
            rows.append({"d": d, "sg": sg, "amt": num(amt), "op": op.strip(), "det": det.strip()})
            continue
        m = K_ROW2.match(line)
        if m and rows is not None:  # строка без деталей
            d, sg, amt, op = m.groups()
            rows.append({"d": d, "sg": sg, "amt": num(amt), "op": op.strip(), "det": ""})
            continue
        s = line.strip()
        if any(n in s for n in K_NOISE) or not rows:
            # «Kaspi Bank» мог прилипнуть к концу строки — игнорируем
            continue
        if re.match(r"^\(.*[A-Z]{3}\)$", s):
            rows[-1]["fx"] = s.strip("()")
        elif rows[-1]["op"] in ("Перевод на свой", "Поступление со") and s in ("счет", "своего счета"):
            rows[-1]["op"] += " " + s
        else:
            rows[-1]["det"] = (rows[-1]["det"] + " " + s).strip()

    # чистим прилипший колонтитул
    out = []
    for r in rows:
        r["det"] = re.sub(r"АО «Kaspi Bank».*$", "", r["det"]).strip()
        dd, mm_, yy = r["d"].split(".")
        iso = date(2000 + int(yy), int(mm_), int(dd)).isoformat()
        amount = r["amt"] if r["sg"] == "+" else -r["amt"]
        op = {"Поступление со своего счета": "Поступление со своего счета"}.get(r["op"], r["op"])
        out.append({"date": iso, "amount": amount, "kind": op, "desc": r["det"],
                    "fx": r.get("fx"), "bank": "kaspi", "account": f"Kaspi *{card}"})
    meta = {"bank": "kaspi", "account": f"Kaspi *{card}", "iban": acc, "owner": owner,
            "period": period, "summary": summary}
    return out, meta


def check_kaspi(rows, meta):
    """Сверка с итогами в шапке выписки. Возвращает список расхождений."""
    sums = {}
    for r in rows:
        k = r["kind"]
        if k.startswith("Зачисление"):
            k = "Зачисление кредита"
        sums[k] = sums.get(k, 0) + r["amount"]
    bad = []
    for op, val in meta["summary"].items():
        if op == "open":
            continue
        if abs(sums.get(op, 0) - val) > 0.5:
            bad.append((op, val, round(sums.get(op, 0), 2)))
    return bad


# ───────────────────────── HALYK ─────────────────────────
def clean_cell(s):
    return " ".join((s or "").replace("\n", " ").split())


def parse_halyk(path):
    txt = pdf_text(path)
    m = re.search(r"ФИО:\s*(.+?)\s{2,}", txt)
    owner = m.group(1).strip() if m else None
    m = re.search(r"Период выписки: с (\d\d\.\d\d\.\d{4}) по (\d\d\.\d\d\.\d{4})", txt)
    period = (m.group(1), m.group(2)) if m else (None, None)
    rows, total = [], {}
    with pdfplumber.open(path) as pdf:
        for page in pdf.pages:
            for tb in page.extract_tables({"text_x_tolerance": 1}):
                for r in tb:
                    if not r or len(r) < 9:
                        continue
                    c0 = clean_cell(r[0])
                    if re.fullmatch(r"\d\d\.\d\d\.\d{4}", c0):
                        rows.append(r)
                    elif c0 in ("KZT", "EUR", "USD", "RUB") and r[5]:
                        total[c0] = (num(r[5]), num(r[6].replace("-", "")), num(r[7].replace("-", "")))
                    elif c0.startswith("Всего") and clean_cell(r[5]):
                        total["KZT"] = (num(r[5]), num(r[6].replace("-", "")), num(r[7].replace("-", "")))
    out = []
    for r in rows:
        d, _, desc, amt, cur, inc, exp, fee, acc = [clean_cell(x) for x in r[:9]]
        dd, mm_, yy = d.split(".")
        iso = f"{yy}-{mm_}-{dd}"
        inc_v = num(inc.replace("-", "")) if inc else 0.0
        exp_v = num(exp.replace("-", "")) if exp else 0.0
        fee_v = num(fee.replace("-", "")) if fee else 0.0
        card = "карта" if "******" in acc else "счёт"
        tail = re.sub(r"\D", "", acc)[-4:]
        out.append({"date": iso, "income": inc_v, "expense": exp_v, "fee": fee_v,
                    "cur": cur, "orig": amt, "desc": desc, "bank": "halyk",
                    "account": f"Halyk …{tail}"})
    meta = {"bank": "halyk", "owner": owner, "period": period, "total": total}
    return out, meta


def check_halyk(rows, meta):
    bad = []
    def own(r):  # строки иностранного счёта/карты (EUR, USD) в тенговые итоги не входят
        if r["cur"] in ("KZT", ""):
            return True
        o = abs(num(r["orig"])) if r["orig"] else 0
        return not (o and abs(o - (r["income"] or r["expense"])) < 0.01)
    rows = [r for r in rows if own(r)]
    inc = sum(r["income"] for r in rows)
    exp = sum(r["expense"] for r in rows)
    fee = sum(r["fee"] for r in rows)
    t = meta["total"].get("KZT")
    if t:
        for name, mine, ref in (("приход", inc, t[0]), ("расход", exp, t[1]), ("комиссия", fee, t[2])):
            if abs(mine - ref) > 1:
                bad.append((name, ref, round(mine, 2)))
    return bad
