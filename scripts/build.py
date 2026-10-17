#!/usr/bin/env python3
"""
Сборка приложения:  python scripts/build.py

1. читает все PDF из input/ (Kaspi Gold, Halyk) — банк определяется автоматически;
2. сливает операции с уже накопленными (docs/data.enc.json), дубли между
   пересекающимися выписками убираются;
3. классифицирует (категории из config/categories.json), находит переводы
   между своими счетами и семьёй (они не считаются ни доходом, ни расходом);
4. сохраняет docs/data.enc.json — зашифрованный (AES-GCM) файл, который открывает docs/index.html.

Пароль: переменная окружения APP_PASSWORD или вопрос в консоли.  --plain  — без шифрования (только локально).
"""
import argparse
import base64
import collections
import getpass
import gzip
import json
import os
import re
import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from parsers import (check_halyk, check_kaspi, detect_bank, num, parse_halyk,  # noqa: E402
                     parse_kaspi)

ROOT = Path(__file__).resolve().parent.parent
INPUT = ROOT / "input"
OUT = ROOT / "docs" / "data.enc.json"
OUT_PLAIN = ROOT / "docs" / "data.json"
CFG = json.loads((ROOT / "config" / "categories.json").read_text(encoding="utf-8"))


# ───────────────────────── 1. чтение PDF → «сырые» операции ─────────────────────────
def person_from_owner(owner):
    parts = (owner or "").title().split()
    return parts[1] if len(parts) > 1 else (parts[0] if parts else "?"), (parts[0] if parts else "")


def raw_from_kaspi(rows, meta):
    p, surname = person_from_owner(meta["owner"])
    for r in rows:
        yield {"d": r["date"], "p": p, "a": r["account"], "b": "kaspi", "s": round(r["amount"], 2),
               "k": r["kind"], "t": r["desc"], "f": r.get("fx") or ""}


def raw_from_halyk(rows, meta):
    p, surname = person_from_owner(meta["owner"])
    for r in rows:
        acc_raw = r["account"]
        cur = r["cur"]
        orig = abs(num(r["orig"])) if r["orig"] else 0.0
        val = r["income"] or r["expense"]
        # строки в иностранной валюте самого счёта/карты (EUR, USD) пропускаем: рублёвая
        # часть видна в строке «Автоконвертация» по тенговому счёту
        if cur not in ("KZT", "") and orig and abs(orig - val) < 0.01:
            continue
        base = {"d": r["date"], "p": p, "a": acc_raw, "b": "halyk", "f": "", "t": r["desc"]}
        if r["income"]:
            yield {**base, "s": round(r["income"], 2), "k": "in"}
        elif r["expense"]:
            exp = r["expense"]
            if cur == "KZT" and orig and exp >= orig - 0.01:
                yield {**base, "s": -round(orig, 2), "k": "out"}
                extra = round(exp - orig + r["fee"], 2)
                if extra > 0:
                    yield {**base, "s": -extra, "k": "fee", "t": "Комиссия банка за операцию"}
            else:
                yield {**base, "s": -round(exp + r["fee"], 2), "k": "out",
                       "f": f"{r['orig']} {cur}" if cur not in ("KZT", "") else ""}
        elif r["fee"]:
            yield {**base, "s": -round(r["fee"], 2), "k": "fee"}


def load_pdfs():
    batches = []  # (имя, [raw])
    fam = set()
    for f in sorted(INPUT.glob("**/*.pdf")):
        bank = detect_bank(f)
        if not bank:
            print(f"  ?  {f.name}: не похоже на Kaspi/Halyk — пропускаю")
            continue
        if bank == "kaspi":
            rows, meta = parse_kaspi(f)
            bad = check_kaspi(rows, meta)
            raw = list(raw_from_kaspi(rows, meta))
        else:
            rows, meta = parse_halyk(f)
            bad = check_halyk(rows, meta)
            raw = list(raw_from_halyk(rows, meta))
        if meta.get("owner"):
            p_, sur = person_from_owner(meta["owner"])
            fam.add(norm(f"{p_} {sur[:1]}."))
        status = "✓ сходится с итогами банка" if not bad else f"⚠ расхождения: {bad}"
        print(f"  {bank:5} {f.name[:38]:38} {meta['owner'][:28] if meta['owner'] else '':28} {len(raw):5} оп.  {status}")
        batches.append((f.name, raw))
    return batches, fam


# ───────────────────────── 2. слияние с накопленным архивом ─────────────────────────
def key_of(t):
    return (t["d"], t["p"], t["a"], t["s"], t["k"], t["t"], t["f"])


def merge(ledger, batches):
    best = collections.Counter(collections.Counter(key_of(t) for t in ledger))
    keep = {key_of(t): t for t in ledger}
    for _, raw in batches:
        c = collections.Counter(key_of(t) for t in raw)
        for t in raw:
            keep.setdefault(key_of(t), t)
        for k, n in c.items():
            best[k] = max(best[k], n)
    out = []
    for k, n in best.items():
        out.extend([keep[k]] * n)
    out.sort(key=lambda t: (t["d"], t["p"], t["a"], t["s"]))
    return out


# ───────────────────────── 3. классификация ─────────────────────────
def norm(s):
    return re.sub(r"\s+", " ", (s or "").lower().replace("ё", "е"))


def match_rules(text, rules):
    for r in rules:
        for kw in r["kw"]:
            if kw in text:
                return r["cat"]
    return None


def clean_merchant(t):
    t = re.sub(r"^Операция оплаты (?:у )?(?:коммерсанта\s*)?", "", t)
    t = re.sub(r"^Снятие денег (?:через банкомат|без карты|через отделение банка)\s*", "", t)
    t = re.sub(r"\s+", " ", t).strip(" -")
    return t or "—"


def classify(txs, family_names):
    """Заполняет cls (income/expense/internal/loan_in), cat, m."""
    res = []
    for i, t in enumerate(txs):
        s = t["s"]
        k, desc, text = t["k"], t["t"], norm(t["t"])
        cls = "income" if s > 0 else "expense"
        cat, m = None, desc
        if t["b"] == "kaspi":
            m = desc
            if k == "Покупка":
                cat = match_rules(norm(desc), CFG["merchant_rules"]) or "Прочие покупки"
            elif k == "Перевод":
                if norm(desc) in family_names:
                    cls, cat = "internal", "Между своими (семья)"
                else:
                    cat = "Переводы людям"
            elif k == "Пополнение":
                if norm(desc) in family_names:
                    cls, cat = "internal", "Между своими (семья)"
                elif "отбасы" in text:
                    cat = "Квартира (Отбасы)"
                elif "с карты другого банка" in text or "kaspi терминал" in text or "банкомат" in text:
                    cat = "Пополнение с карт/наличными"
                else:
                    cat = "Переводы от людей"
            elif k == "Снятие":
                cat, m = "Снятие наличных", desc
            elif k == "Разное":
                cat = "Комиссии банка"
            elif k == "Перевод на свой счет":
                if "кредит" in text:
                    cat = "Кредиты и долги"
                else:
                    cls, cat = "internal", "Депозиты/свои счета"
            elif k == "Поступление со своего счета":
                cls, cat = "internal", "Депозиты/свои счета"
            elif k.startswith("Зачисление"):
                cls, cat = "loan_in", "Кредит получен"
            else:
                cat = "Прочее"
        else:  # halyk
            if k == "fee":
                cat, m = "Комиссии банка", "Комиссия Halyk"
            elif "каражанбасмунай" in text:
                cls, cat, m = "income", "Зарплата", "АО Каражанбасмунай"
            elif "депозит" in text and "оплата" not in text and "коммерсант" not in text:
                cls, cat, m = "internal", "Депозиты/свои счета", "Депозит Halyk"
            elif "hcsbk deposit" in text or "deposit" in text and "коммерсант" in text:
                cls, cat, m = "internal", "Депозиты/свои счета", "Депозит (HCSBK)"
            elif "жилстройсбербанк" in text and "пополнение депозита" in text:
                cls, cat, m = "internal", "Депозиты/свои счета", "Депозит ЖССБ"
            elif "жилстройсбербанк" in text:
                cat, m = "Кредиты и долги", "ЖилстройСберБанк (кредит)"
            elif any(x in text for x in CFG["debt_markers"]):
                cat, m = "Кредиты и долги", clean_merchant(desc)
            elif "автоконвертация" in text:
                cat, m = "Покупки за рубежом", "Конвертация валюты"
            elif s > 0 and "поступление перевода" in text:
                cat, m = "Переводы от людей", "Перевод на карту/счёт"
            elif s > 0 and ("cash to card" in text or "на карту" in text):
                cat, m = "Пополнение с карт/наличными", "Пополнение наличными"
            elif s > 0 and "поступление на счет" in text:
                if s >= 1_000_000:  # крупные «Поступление на счёт» — как правило, возврат с депозита
                    cls, cat, m = "internal", "Крупные поступления на счёт", "Поступление на счёт (депозит?)"
                else:
                    cat, m = "Прочие поступления", "Поступление на счёт"
            elif "перевод на другую карту" in text or "openapi перевод" in text:
                cat, m = "Переводы людям", "Перевод на карту"
            elif "снятие денег" in text or text.startswith("atm"):
                cat, m = "Снятие наличных", clean_merchant(desc)
            elif "коммерсант" in text or "платеж с фл" in text:
                m = clean_merchant(desc)
                cat = match_rules(norm(m), CFG["merchant_rules"]) or "Прочие покупки"
            else:
                cat = "Прочее"
            if s > 0 and cat == "Прочие покупки":  # возврат
                cat = "Возвраты"
        if s < 0 and cat in CFG.get("caps", {}) and abs(s) > CFG["caps"][cat]:
            cat = "Прочие покупки"
        if s > 0 and cls == "income" and (cat in CFG["purchase_cats"] or cat in CFG["groups"]["money"]):
            cat = "Возвраты"
        res.append({"i": i, "d": t["d"], "p": t["p"], "a": t["a"], "b": t["b"], "amt": abs(s),
                    "dir": "in" if s > 0 else "out", "cls": cls, "cat": cat, "m": m,
                    "k": k if t["b"] == "kaspi" else "", "f": t["f"]})
    return res


def tkey(r):
    a = f"{r['amt']:.2f}".rstrip("0").rstrip(".")
    return f"{r['d']}|{a}|{r['m']}|{r['a']}"


def cls_for(cat, direction):
    if cat in CFG["internal_cats"]:
        return "internal"
    if cat in CFG["loan_cats"]:
        return "loan_in"
    return "income" if direction == "in" else "expense"


def apply_overrides(rows, ov):
    n = 0
    for r in rows:
        new = ov.get("tx", {}).get(tkey(r)) or ov.get("merchant", {}).get(f"{r['m']}|{r['dir']}")
        if new:
            r["cat"], r["cls"], r["ov"] = new, cls_for(new, r["dir"]), 1
            n += 1
    return n


def pair_internal(rows):
    """Находит пары «ушло со счёта А — пришло на счёт Б» (та же сумма, ±3 дня) и помечает internal."""
    by_amt = collections.defaultdict(list)
    for r in rows:
        if r["cls"] in ("income", "expense") and r["amt"] >= 1000 and r["cat"] not in ("Зарплата",):
            by_amt[round(r["amt"], 2)].append(r)
    n = 0
    for amt, lst in by_amt.items():
        outs = [r for r in lst if r["dir"] == "out" and r["cat"] in
                ("Переводы людям", "Депозиты/свои счета", "Прочие покупки", "Снятие наличных")]
        ins = [r for r in lst if r["dir"] == "in" and r["cat"] in
               ("Пополнение с карт/наличными", "Переводы от людей", "Прочие поступления", "Возвраты")]
        used = set()
        for o in outs:
            od = date.fromisoformat(o["d"])
            for j, x in enumerate(ins):
                if j in used:
                    continue
                if abs((date.fromisoformat(x["d"]) - od).days) <= 3 and (x["a"] != o["a"]):
                    used.add(j)
                    for r in (o, x):
                        r["cls"], r["cat"] = "internal", "Между своими (семья)"
                    n += 1
                    break
    return n


# ───────────────────────── 4. шифрование ─────────────────────────
def encrypt(payload: bytes, password: str):
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
    salt, iv = os.urandom(16), os.urandom(12)
    key = PBKDF2HMAC(algorithm=hashes.SHA256(), length=32, salt=salt, iterations=250_000).derive(password.encode())
    ct = AESGCM(key).encrypt(iv, payload, None)
    b = lambda x: base64.b64encode(x).decode()  # noqa: E731
    return {"v": 1, "iter": 250000, "salt": b(salt), "iv": b(iv), "ct": b(ct)}


def decrypt(blob, password: str):
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
    d = lambda x: base64.b64decode(x)  # noqa: E731
    key = PBKDF2HMAC(algorithm=hashes.SHA256(), length=32, salt=d(blob["salt"]), iterations=blob["iter"]).derive(password.encode())
    return AESGCM(key).decrypt(d(blob["iv"]), d(blob["ct"]), None)


# ───────────────────────── main ─────────────────────────
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--plain", action="store_true", help="без шифрования → docs/data.json (локальный просмотр)")
    ap.add_argument("--fresh", action="store_true", help="игнорировать накопленный архив, взять только PDF из input/")
    args = ap.parse_args()

    password = None
    if not args.plain:
        password = os.environ.get("APP_PASSWORD") or getpass.getpass("Пароль для шифрования данных: ")

    ledger, old_fam, old_ov = [], set(), {}
    store = OUT_PLAIN if args.plain else OUT
    if store.exists() and not args.fresh:
        try:
            raw = json.loads(store.read_text(encoding="utf-8"))
            if args.plain:
                data = raw
            else:
                data = json.loads(gzip.decompress(decrypt(raw, password)))
            ledger, old_fam, old_ov = data["ledger"], set(data.get("family", [])), data.get("overrides", {})
            print(f"Архив: {len(ledger)} операций")
        except Exception as e:  # noqa: BLE001
            print(f"Не смог открыть архив ({e.__class__.__name__}) — неверный пароль? Стартую с нуля только если --fresh")
            sys.exit(1)

    print("Читаю PDF из input/ …")
    batches, new_fam = load_pdfs()
    if not batches and not ledger:
        sys.exit("Нет данных: положите PDF в папку input/")
    ledger = merge(ledger, batches)

    people = sorted({t["p"] for t in ledger})
    # имена семьи вида «Марат К.»  (в Kaspi так подписаны переводы между своими)
    fam_names = {norm(x) for x in CFG.get("family_names", [])} | old_fam | new_fam
    rows = classify(ledger, fam_names)
    paired = pair_internal(rows)
    ov = {"tx": {}, "merchant": {}}
    for src in (old_ov, json.loads((ROOT / "config" / "overrides.json").read_text(encoding="utf-8")) if (ROOT / "config" / "overrides.json").exists() else {}):
        ov["tx"].update(src.get("tx", {}))
        ov["merchant"].update(src.get("merchant", {}))
    print(f"Ручных правок применено: {apply_overrides(rows, ov)}")

    for r in rows:
        r.pop("i", None)
    payload = {
        "generated": date.today().isoformat(),
        "people": people,
        "family": sorted(fam_names),
        "ledger": ledger,        # сырые данные — для будущих слияний
        "tx": rows,              # классифицированные
        "categories": CFG["category_style"],
        "groups": CFG["groups"],
        "kinds": {"internal": CFG["internal_cats"], "loan": CFG["loan_cats"], "income": CFG["income_cats"]},
        "overrides": ov,
    }
    blob = gzip.compress(json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode())
    OUT.parent.mkdir(exist_ok=True)
    if args.plain:
        OUT_PLAIN.write_text(json.dumps(payload, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
        print(f"→ {OUT_PLAIN}")
    else:
        OUT.write_text(json.dumps(encrypt(blob, password)), encoding="utf-8")
        print(f"→ {OUT}  ({OUT.stat().st_size // 1024} КБ, зашифровано)")
    inc = sum(r["amt"] for r in rows if r["cls"] == "income")
    exp = sum(r["amt"] for r in rows if r["cls"] == "expense")
    print(f"Операций: {len(rows)} · пар «между своими» найдено: {paired} · доход {inc:,.0f} · расход {exp:,.0f} ₸".replace(",", " "))
    unk = collections.Counter(r["m"] for r in rows if r["cat"] == "Прочие покупки")
    print("Не распознано (топ):", unk.most_common(8))


if __name__ == "__main__":
    main()
