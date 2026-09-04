"""Financial statements from the FNS accounting resource (bo.nalog.gov.ru).

Free, unauthenticated, five years deep. Returns every line of forms 1-4, plus
a small set of derived ratios that make the numbers comparable across years.

Banks and insurers do not file here (they report to the Central Bank), so a
lookup for one of those legitimately returns nothing.
"""

import argparse
import json
import sys
import urllib.parse
import urllib.request

BASE = "https://bo.nalog.gov.ru"
UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")

# Form 2 (financial result) lines worth naming.
PROFIT_LINES = {
    "2110": "Выручка",
    "2120": "Себестоимость продаж",
    "2100": "Валовая прибыль",
    "2200": "Прибыль от продаж",
    "2300": "Прибыль до налогообложения",
    "2400": "Чистая прибыль",
    "2330": "Проценты к уплате",
}
# Form 1 (balance) lines worth naming.
BALANCE_LINES = {
    "1100": "Внеоборотные активы",
    "1200": "Оборотные активы",
    "1230": "Дебиторская задолженность",
    "1250": "Денежные средства",
    "1300": "Капитал и резервы",
    "1400": "Долгосрочные обязательства",
    "1500": "Краткосрочные обязательства",
    "1520": "Кредиторская задолженность",
    "1600": "Баланс (актив)",
    "1700": "Баланс (пассив)",
}


class GirboError(RuntimeError):
    pass


def _get(url):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read().decode("utf-8"))


def find_org(inn):
    q = urllib.parse.urlencode({"query": inn, "page": 0})
    res = _get(f"{BASE}/advanced-search/organizations/search?{q}")
    content = res.get("content") or []
    return content[0] if content else None


def reports(org_id):
    return _get(f"{BASE}/nbo/organizations/{org_id}/bfo/")


# Accounting forms are filed in thousands of roubles; everything below works in
# roubles so that figures can be compared with contract sums and share capital.
UNIT = 1000


def _lines(block, prefix):
    """Pull {code: value} out of one form block, e.g. current2110 -> '2110'."""
    out = {}
    for key, value in (block or {}).items():
        if key.startswith(prefix) and key[len(prefix):].isdigit() and value is not None:
            out[key[len(prefix):]] = value * UNIT
    return out


def fetch(inn):
    org = find_org(inn)
    if not org:
        raise GirboError(f"{inn}: not present in the accounting resource "
                         f"(banks, insurers and non-filers are absent)")
    years = {}
    for period in reports(org["id"]):
        year = int(period["period"])
        corrections = period.get("typeCorrections") or []
        if not corrections:
            continue
        c = corrections[0].get("correction") or {}
        balance = _lines(c.get("balance"), "current")
        result = _lines(c.get("financialResult"), "current")
        cash = _lines(c.get("fundsMovement"), "current")
        years[year] = {
            "balance": balance,
            "profit": result,
            "cashflow": cash,
            "assets_total": None if period.get("actives") is None else period["actives"] * UNIT,
            "gain_sum": None if period.get("gainSum") is None else period["gainSum"] * UNIT,
            "msp_category": period.get("mspCategory"),
            "audited": period.get("requiredAudit") if isinstance(c, dict) else None,
        }
    return {
        "inn": inn,
        "id": org["id"],
        "name": org.get("shortName") or org.get("fullName"),
        "ogrn": org.get("ogrn"),
        "okved2": org.get("okved2"),
        "status": org.get("statusCode"),
        "years": years,
    }


def summary(data):
    """Condense to the handful of lines an analyst actually reads."""
    rows = []
    for year in sorted(data["years"], reverse=True):
        y = data["years"][year]
        p, b = y["profit"], y["balance"]
        revenue = p.get("2110")
        net = p.get("2400")
        equity = b.get("1300")
        row = {
            "year": year,
            "revenue": revenue,
            "gross_profit": p.get("2100"),
            "operating_profit": p.get("2200"),
            "net_profit": net,
            "assets": b.get("1600") or y.get("assets_total"),
            "equity": equity,
            "debt_long": b.get("1400"),
            "debt_short": b.get("1500"),
            "receivables": b.get("1230"),
            "payables": b.get("1520"),
            "cash": b.get("1250"),
        }
        if revenue:
            row["net_margin_pct"] = round(100.0 * (net or 0) / revenue, 2)
        if equity:
            row["roe_pct"] = round(100.0 * (net or 0) / equity, 2)
        assets = row["assets"]
        if assets:
            row["equity_ratio_pct"] = round(100.0 * (equity or 0) / assets, 2)
        rows.append(row)

    for i in range(len(rows) - 1):
        prev = rows[i + 1].get("revenue")
        cur = rows[i].get("revenue")
        if prev:
            rows[i]["revenue_yoy_pct"] = round(100.0 * (cur - prev) / abs(prev), 1)
    return rows


def flags(rows):
    """Cheap red flags that are worth a human look."""
    out = []
    for r in rows:
        y = r["year"]
        if r.get("equity") is not None and r["equity"] < 0:
            out.append(f"{y}: отрицательный капитал ({r['equity']:,.0f} руб) — признак технической несостоятельности")
        if r.get("net_profit") is not None and r["net_profit"] < 0:
            out.append(f"{y}: чистый убыток {r['net_profit']:,.0f} руб")
        if r.get("revenue_yoy_pct") is not None and r["revenue_yoy_pct"] < -50:
            out.append(f"{y}: выручка упала на {abs(r['revenue_yoy_pct'])}% год к году")
        if r.get("revenue") and r.get("assets") and r["assets"] > 0:
            if r["revenue"] / r["assets"] > 50:
                out.append(f"{y}: оборот в {r['revenue']/r['assets']:.0f}x активов — возможна транзитная схема")
        if r.get("revenue") is not None and r["revenue"] == 0:
            out.append(f"{y}: нулевая выручка при поданной отчетности")
    return out


def main():
    ap = argparse.ArgumentParser(description="Financial statements from bo.nalog.gov.ru")
    ap.add_argument("inn")
    ap.add_argument("--full", action="store_true", help="dump every form line")
    args = ap.parse_args()
    try:
        data = fetch(args.inn)
    except GirboError as e:
        print(f"error: {e}", file=sys.stderr)
        sys.exit(1)
    if args.full:
        print(json.dumps(data, ensure_ascii=False, indent=2))
        return
    rows = summary(data)
    print(json.dumps({
        "inn": data["inn"], "name": data["name"],
        "summary": rows, "flags": flags(rows),
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
