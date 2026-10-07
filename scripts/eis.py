"""Public procurement (EIS, zakupki.gov.ru): contracts and the dishonest-supplier register.

Contracts answer "who pays this company", which no registry does: a shared
customer, or a supplier that keeps winning the same customer's tenders, is an
economic tie with amounts and dates. The register of dishonest suppliers
(РНП, 44-ФЗ and 223-ФЗ) is a red flag in its own right.

zakupki.gov.ru presents a certificate issued by the Russian Trusted Root CA
(Ministry of Digital Development), which most operating systems do not trust,
so plain HTTPS fails with "unable to get local issuer certificate". This
module fetches that root and its intermediate from the ministry's own site,
**pins the root by its SHA-256 fingerprint**, and uses them in an SSL context
for zakupki.gov.ru only. The system trust store is never touched.

Two filters on the contract search look plausible and do nothing: anything the
server does not recognise is silently dropped and all 42 million contracts come
back. The one that works for "this company was the supplier" is
`supplierTitle=<INN>`; `searchString` only matches the INN as text, for example
inside a contract number.

Source: Единая информационная система в сфере закупок, https://zakupki.gov.ru
"""

import argparse
import hashlib
import html
import os
import re
import ssl
import sys
import time
import urllib.parse
import urllib.request

import db

BASE = "https://zakupki.gov.ru"
UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")
CACHE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "cache")

ROOT_URL = "https://gu-st.ru/content/lending/russian_trusted_root_ca_pem.crt"
SUB_URL = "https://gu-st.ru/content/lending/russian_trusted_sub_ca_pem.crt"
# SHA-256 of the DER form of "Russian Trusted Root CA", valid to 2032-02-27.
ROOT_SHA256 = "D26D2D0231B7C39F92CC738512BA54103519E4405D68B5BD703E9788CA8ECF31"

_ctx = None


class EisError(RuntimeError):
    pass


def _download(url):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=60) as r:
        return r.read().decode("ascii", errors="replace")


def ssl_context():
    """SSL context trusting the Russian root for this module only, pinned by fingerprint."""
    global _ctx
    if _ctx:
        return _ctx
    bundle = os.path.join(CACHE, "russian_trusted_ca_bundle.pem")
    if not os.path.exists(bundle):
        os.makedirs(CACHE, exist_ok=True)
        root, sub = _download(ROOT_URL), _download(SUB_URL)
        der = ssl.PEM_cert_to_DER_cert(root.strip())
        got = hashlib.sha256(der).hexdigest().upper()
        if got != ROOT_SHA256:
            raise EisError(f"отпечаток корневого сертификата не совпал: {got} — файл не используется")
        with open(bundle, "w") as f:
            # The files come without a trailing newline; glued together they
            # make an unreadable PEM, so each is terminated explicitly.
            f.write(root.strip() + "\n" + sub.strip() + "\n")
    _ctx = ssl.create_default_context(cafile=bundle)
    return _ctx


def _get(path, params):
    url = BASE + path + "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept-Language": "ru-RU,ru;q=0.9"})
    with urllib.request.urlopen(req, context=ssl_context(), timeout=90) as r:
        return r.read().decode("utf-8", errors="replace")


def _text(fragment):
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", fragment or ""))).strip()


def _cards(page):
    return page.split('class="search-registry-entry-block')[1:]


def _field(card, title):
    """Value following a body-title or data-block title."""
    m = re.search(r'(?:registry-entry__body-title|data-block__title|price-block__title|lots-wrap-content__body__title)">\s*'
                  + re.escape(title) + r'\s*</div>\s*<div[^>]*>(.*?)</div>', card, re.S)
    return _text(m.group(1)) if m else None


def _total(page):
    i = page.find("search-results__total")
    m = re.search(r"(более\s*)?([\d\s ]+)\s*запис", _text(page[i:i + 400])) if i >= 0 else None
    return int(re.sub(r"\D", "", m.group(2))) if m else 0


def _iso(s):
    """dd.mm.yyyy to yyyy-mm-dd, so dates sort and compare as text."""
    m = re.search(r"(\d{2})\.(\d{2})\.(\d{4})", s or "")
    return f"{m.group(3)}-{m.group(2)}-{m.group(1)}" if m else s


def _money(s):
    if not s:
        return None
    s = re.sub(r"[^\d,.]", "", s).replace(",", ".")
    try:
        return float(s)
    except ValueError:
        return None


def parse_contracts(body, law="fz44"):
    out = []
    for c in _cards(body):
        num = re.search(r"reestrNumber=(\d+)", c)
        cust = re.search(r'Заказчик</div>\s*<div[^>]*>\s*<a[^>]*organizationId=(\d+)[^>]*>(.*?)</a>', c, re.S)
        out.append({
            "number": num.group(1) if num else None,
            "status": _text((re.search(r'registry-entry__header-mid__title[^"]*">(.*?)</div>', c, re.S)
                             or [None, ""])[1]),
            "customer": _text(cust.group(2)) if cust else None,
            "customer_eis_id": cust.group(1) if cust else None,
            "object": _field(c, "Объекты закупки"),
            "price": _money(_field(c, "Цена контракта")),
            "signed": _iso(_field(c, "Заключение контракта")),
            "until": _iso(_field(c, "Срок исполнения")),
            "law": "44-ФЗ" if law == "fz44" else "223-ФЗ",
        })
    return out


def contracts_as_supplier(inn, pages=3, pause=1.5, law="fz44"):
    out, total = [], 0
    for page in range(1, pages + 1):
        body = _get("/epz/contract/search/results.html", {
            "supplierTitle": inn, law: "on", "pageNumber": page,
            "recordsPerPage": "_50", "sortBy": "UPDATE_DATE", "sortDirection": "false"})
        if page == 1:
            total = _total(body)
        cards = parse_contracts(body, law)
        out += cards
        if len(cards) < 50:
            break
        time.sleep(pause)
    return total, out


def rnp(inn):
    body = _get("/epz/dishonestsupplier/search/results.html",
                {"searchString": inn, "recordsPerPage": "_50"})
    return parse_rnp(body, inn)


def parse_rnp(body, inn):
    out = []
    for c in _cards(body):
        rec_inn = _field(c, "ИНН (аналог ИНН)")
        # searchString is full-text: keep only records about this very INN.
        if rec_inn and re.sub(r"\D", "", rec_inn) != inn:
            continue
        num = re.search(r"reestrNumber=([^&\"]+)", c)
        out.append({
            "number": urllib.parse.unquote(num.group(1)) if num else None,
            "law": _text((re.search(r'registry-entry__header-top__title[^"]*">(.*?)</div>', c, re.S)
                          or [None, ""])[1]),
            "status": _text((re.search(r'registry-entry__header-mid__title[^"]*">(.*?)</div>', c, re.S)
                             or [None, ""])[1]),
            "name": _field(c, "Наименование (ФИО) недобросовестного поставщика"),
            "inn": rec_inn,
            "included": _iso(_field(c, "Включено")),
        })
    return out


def store(con, inn, contracts, rnp_rows):
    for c in contracts:
        if not c["number"]:
            continue
        con.execute(
            """INSERT OR REPLACE INTO eis_contracts
               (number, supplier_inn, customer, customer_eis_id, object, price, status,
                signed, until, law) VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (c["number"], inn, c["customer"], c["customer_eis_id"], c["object"], c["price"],
             c["status"], c["signed"], c["until"], c["law"]))
    for r in rnp_rows:
        con.execute(
            """INSERT OR REPLACE INTO eis_rnp (number, inn, name, law, status, included)
               VALUES (?,?,?,?,?,?)""",
            (r["number"], inn, r["name"], r["law"], r["status"], r["included"]))
    con.commit()


def _targets(con, args):
    if args.inn:
        return list(args.inn)
    if args.case:
        return [r["inn"] for r in con.execute("SELECT inn FROM cases WHERE name=?", (args.case,))]
    return [r["inn"] for r in con.execute("SELECT inn FROM orgs")]


def cmd_load(args):
    con = db.connect(args.db)
    for inn in _targets(con, args):
        try:
            total, rows = contracts_as_supplier(inn, pages=args.pages)
            if args.fz223:
                t2, r2 = contracts_as_supplier(inn, pages=args.pages, law="fz223")
                total += t2
                rows += r2
            flags = rnp(inn)
        except EisError as e:
            print(f"ошибка: {e}", file=sys.stderr)
            sys.exit(2)
        except Exception as e:
            print(f"[fail] {inn}: {type(e).__name__} {e}", file=sys.stderr)
            continue
        store(con, inn, rows, flags)
        print(f"[ok]   {inn}  контрактов как поставщик: {total} (загружено {len(rows)}), "
              f"в РНП: {len(flags)}")
        time.sleep(args.pause)


def cmd_show(args):
    con = db.connect(args.db)
    flags = con.execute("SELECT * FROM eis_rnp WHERE inn=?", (args.inn,)).fetchall()
    rows = con.execute(
        "SELECT * FROM eis_contracts WHERE supplier_inn=? ORDER BY price DESC", (args.inn,)).fetchall()
    print(f"# Закупки — ИНН {args.inn}\n")
    if flags:
        print("## Реестр недобросовестных поставщиков\n")
        for r in flags:
            print(f"- {r['law']} № {r['number']} — {r['status']}, включено {r['included']}")
    if not rows:
        print("контрактов в базе нет")
        return
    by_cust = {}
    for r in rows:
        k = r["customer"] or "—"
        by_cust.setdefault(k, [0, 0.0])
        by_cust[k][0] += 1
        by_cust[k][1] += r["price"] or 0
    print(f"\n## Заказчики — {len(by_cust)}\n")
    for k, (n, s) in sorted(by_cust.items(), key=lambda x: -x[1][1])[:20]:
        print(f"- {k[:80]} — {n} контр., {s:,.0f} ₽".replace(",", " "))


def shared_customers(con, inns=None):
    """Customers that buy from more than one company in scope."""
    where, params = "", []
    if inns:
        where = "WHERE supplier_inn IN (%s)" % ",".join("?" * len(inns))
        params = list(inns)
    return con.execute(
        f"""SELECT customer, COUNT(DISTINCT supplier_inn) n,
                   GROUP_CONCAT(DISTINCT supplier_inn) suppliers, SUM(price) total
            FROM eis_contracts {where}
            GROUP BY customer_eis_id HAVING n > 1 ORDER BY n DESC, total DESC""", params).fetchall()


def main():
    ap = argparse.ArgumentParser(description="EIS: contracts as supplier and dishonest-supplier register")
    ap.add_argument("--db", default=db.DEFAULT_DB)
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("load", help="контракты (поставщик) и РНП по компаниям")
    p.add_argument("--inn", nargs="*")
    p.add_argument("--case")
    p.add_argument("--pages", type=int, default=3, help="по 50 контрактов на страницу")
    p.add_argument("--fz223", action="store_true", help="добавить договоры по 223-ФЗ")
    p.add_argument("--pause", type=float, default=2.0)
    p.set_defaults(func=cmd_load)

    p = sub.add_parser("show", help="закупки одной компании")
    p.add_argument("inn")
    p.set_defaults(func=cmd_show)

    args = ap.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
