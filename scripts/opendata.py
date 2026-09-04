"""Bulk open data from the FNS (nalog.gov.ru/opendata).

These sets are published as one big ZIP of XML shards covering every company in
the country, so they are downloaded once and indexed into the local database.
After that, lookups are offline and free of any rate limit — which is exactly
what makes headcount and tax-debt cheap to check on every target.

    python3 opendata.py load sshr2019       # download + index
    python3 opendata.py load --all
    python3 opendata.py show 7721546864     # everything known about one INN
"""

import argparse
import io
import os
import re
import sys
import time
import urllib.request
import zipfile

import db

UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")
PAGE = "https://www.nalog.gov.ru/opendata/7707329152-{code}/"
CACHE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "cache")


def _num(s):
    if s in (None, ""):
        return None
    try:
        return float(str(s).replace(",", "."))
    except ValueError:
        return None


# --- per-dataset record extraction ----------------------------------------
# Each yields tuples ready for the dataset's target table.

def _rows_headcount(doc, date):
    np = doc.find("СведНП")
    sv = doc.find("СведССЧР")
    if np is None or sv is None:
        return
    inn = np.get("ИННЮЛ")
    if inn:
        yield (inn, date, int(_num(sv.get("КолРаб")) or 0))


def _rows_debt(doc, date):
    np = doc.find("СведНП")
    if np is None or not np.get("ИННЮЛ"):
        return
    inn = np.get("ИННЮЛ")
    for sv in doc.findall("СведНедоим"):
        yield (inn, date, sv.get("НаимНалог") or "—",
               _num(sv.get("СумНедНалог")), _num(sv.get("СумПени")), _num(sv.get("СумШтраф")))


def _rows_revexp(doc, date):
    np = doc.find("СведНП")
    sv = doc.find("СведДохРасх")
    if np is None or sv is None or not np.get("ИННЮЛ"):
        return
    yield (np.get("ИННЮЛ"), date, _num(sv.get("СумДоход")), _num(sv.get("СумРасход")))


def _rows_regime(doc, date):
    np = doc.find("СведНП")
    sv = doc.find("СведСНР")
    if np is None or sv is None or not np.get("ИННЮЛ"):
        return
    names = {"ПризнЕСХН": "ЕСХН", "ПризнУСН": "УСН", "ПризнАУСН": "АУСН", "ПризнСРП": "СРП"}
    active = [label for attr, label in names.items() if sv.get(attr) == "1"]
    yield (np.get("ИННЮЛ"), date, ", ".join(active) or "ОСНО")


DATASETS = {
    "sshr2019": {
        "title": "Среднесписочная численность работников",
        "table": "headcount",
        "columns": "(inn, date, count)",
        "rows": _rows_headcount,
    },
    "debtam": {
        "title": "Задолженность по налогам, пеням и штрафам",
        "table": "tax_debt",
        "columns": "(inn, date, tax, debt, penalty, fine)",
        "rows": _rows_debt,
    },
    "revexp": {
        "title": "Доходы и расходы по бухгалтерской отчетности",
        "table": "revexp",
        "columns": "(inn, date, income, expense)",
        "rows": _rows_revexp,
    },
    "snr": {
        "title": "Применяемые специальные налоговые режимы",
        "table": "tax_regime",
        "columns": "(inn, date, regimes)",
        "rows": _rows_regime,
    },
}


def discover_url(code):
    """Find the current data ZIP; the filename carries a publication date."""
    req = urllib.request.Request(PAGE.format(code=code), headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=90) as r:
        html = r.read().decode("utf-8", errors="replace")
    urls = re.findall(
        r"https://file\.nalog\.ru/opendata/7707329152-%s/data-\d+-structure-\d+\.zip" % re.escape(code),
        html)
    if not urls:
        raise RuntimeError(f"{code}: no data file link on {PAGE.format(code=code)}")
    return sorted(set(urls))[-1]


def download(url, dest, force=False):
    if os.path.exists(dest) and not force:
        print(f"  кэш: {dest} ({os.path.getsize(dest)/2**20:.0f} МБ)")
        return dest
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    print(f"  скачиваю {url}")
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    tmp = dest + ".part"
    with urllib.request.urlopen(req, timeout=900) as r, open(tmp, "wb") as f:
        total = int(r.headers.get("Content-Length") or 0)
        done = 0
        while True:
            chunk = r.read(1 << 20)
            if not chunk:
                break
            f.write(chunk)
            done += len(chunk)
            if total:
                print(f"\r  {done/2**20:.0f}/{total/2**20:.0f} МБ", end="", flush=True)
    print()
    os.replace(tmp, dest)
    return dest


def load(con, code, force=False):
    spec = DATASETS[code]
    url = discover_url(code)
    zip_path = download(url, os.path.join(CACHE, f"{code}.zip"), force=force)

    sql = f"INSERT OR REPLACE INTO {spec['table']} {spec['columns']} VALUES " \
          f"({','.join('?' * (spec['columns'].count(',') + 1))})"
    total = 0
    batch = []
    with zipfile.ZipFile(zip_path) as z:
        names = [n for n in z.namelist() if n.lower().endswith(".xml")]
        for i, name in enumerate(names, 1):
            data = z.read(name)
            for _, doc in db.safe_iterparse(io.BytesIO(data)):
                if doc.tag != "Документ":
                    continue
                date = doc.get("ДатаСост") or doc.get("ДатаДок") or ""
                batch.extend(spec["rows"](doc, date))
                doc.clear()
            if len(batch) >= 50000:
                con.executemany(sql, batch)
                total += len(batch)
                batch = []
            if i % 200 == 0:
                print(f"\r  {i}/{len(names)} файлов, {total + len(batch)} записей", end="", flush=True)
    if batch:
        con.executemany(sql, batch)
        total += len(batch)
    con.execute(
        "INSERT OR REPLACE INTO opendata_loads (dataset, url, rows, loaded_at) VALUES (?,?,?,?)",
        (code, url, total, int(time.time())))
    con.commit()
    print(f"\r  {spec['title']}: {total} записей                      ")
    return total


def cmd_load(args):
    con = db.connect(args.db)
    codes = list(DATASETS) if args.all else args.dataset
    if not codes:
        print("укажите набор данных или --all. Доступно: " + ", ".join(DATASETS), file=sys.stderr)
        sys.exit(1)
    for code in codes:
        if code not in DATASETS:
            print(f"неизвестный набор: {code}", file=sys.stderr)
            continue
        print(f"[{code}] {DATASETS[code]['title']}")
        load(con, code, force=args.force)


def cmd_show(args):
    con = db.connect(args.db)
    inn = args.inn
    print(f"# ИНН {inn}\n")

    rows = con.execute(
        "SELECT date, count FROM headcount WHERE inn=? ORDER BY date DESC", (inn,)).fetchall()
    print("## Среднесписочная численность\n")
    for r in rows:
        print(f"- {r['date']}: **{r['count']}** чел")
    if not rows:
        print("_нет данных (набор sshr2019 не загружен или организация отсутствует)_")

    rows = con.execute(
        "SELECT date, tax, debt, penalty, fine FROM tax_debt WHERE inn=? ORDER BY date DESC",
        (inn,)).fetchall()
    if rows:
        print("\n## Налоговая задолженность\n")
        for r in rows:
            print(f"- {r['date']} · {r['tax']}: недоимка {r['debt'] or 0:,.0f}, "
                  f"пени {r['penalty'] or 0:,.0f}, штраф {r['fine'] or 0:,.0f}".replace(",", " "))

    rows = con.execute(
        "SELECT date, income, expense FROM revexp WHERE inn=? ORDER BY date DESC", (inn,)).fetchall()
    if rows:
        print("\n## Доходы и расходы (по данным ФНС)\n")
        for r in rows:
            print(f"- {r['date']}: доход {r['income'] or 0:,.0f}, расход "
                  f"{r['expense'] or 0:,.0f}".replace(",", " "))

    rows = con.execute(
        "SELECT date, regimes FROM tax_regime WHERE inn=? ORDER BY date DESC", (inn,)).fetchall()
    if rows:
        print("\n## Налоговые режимы\n")
        for r in rows:
            print(f"- {r['date']}: {r['regimes']}")


def cmd_status(args):
    con = db.connect(args.db)
    rows = con.execute("SELECT * FROM opendata_loads").fetchall()
    if not rows:
        print("ничего не загружено. Начните с: python3 opendata.py load sshr2019")
        return
    for r in rows:
        when = time.strftime("%Y-%m-%d", time.localtime(r["loaded_at"]))
        print(f"{r['dataset']:12} {r['rows']:>10} записей  загружено {when}")
        print(f"             {r['url']}")


def main():
    ap = argparse.ArgumentParser(description="FNS bulk open data")
    ap.add_argument("--db", default=db.DEFAULT_DB)
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("load", help="download and index a dataset")
    p.add_argument("dataset", nargs="*", metavar="НАБОР",
                   help="один или несколько из: " + ", ".join(DATASETS))
    p.add_argument("--all", action="store_true")
    p.add_argument("--force", action="store_true", help="re-download even if cached")
    p.set_defaults(func=cmd_load)

    p = sub.add_parser("show", help="everything indexed for one INN")
    p.add_argument("inn")
    p.set_defaults(func=cmd_show)

    p = sub.add_parser("status", help="what has been loaded")
    p.set_defaults(func=cmd_status)

    args = ap.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
