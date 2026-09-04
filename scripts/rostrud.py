"""Workplace declarations from the Rostrud SOUT register.

Every employer that runs a special assessment of working conditions files a
declaration, and the register publishes it: the **actual** place of business and
the list of job titles with a headcount per workplace.

Two things make this the most useful non-tax source in the toolkit:

- the address here is where the company really works, which regularly differs
  from the EGRUL registration address and exposes groups sharing an office;
- the job titles show what a company actually does. A management company with
  twenty repair workers runs its own maintenance; one with nothing but a
  billing desk and a lawyer has given the work to somebody else.

No personal names are published — only positions, which is what makes this
ordinary business intelligence rather than personal data.

Source: Реестр деклараций СОУТ, Роструд, https://declaration.rostrud.gov.ru
"""

import argparse
import html
import re
import sys
import time
import urllib.parse
import urllib.request

import db

BASE = "https://declaration.rostrud.gov.ru/declaration/index"
UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")

# Column order of the register table, as published.
COLS = ["idx", "date", "name", "address", "inn", "ogrn", "wp_numbers",
        "positions", "headcounts", "sout_org", "expert_doc", "valid_until", "terminated"]


class RostrudError(RuntimeError):
    pass


def _get(url):
    req = urllib.request.Request(url, headers={
        "User-Agent": UA,
        "Accept": "text/html,application/xhtml+xml",
        "Accept-Language": "ru-RU,ru;q=0.9",
    })
    with urllib.request.urlopen(req, timeout=60) as r:
        return r.read().decode("utf-8", errors="replace")


def _clean(cell):
    text = re.sub(r"<[^>]+>", "\n", cell)
    return html.unescape(text).strip()


def fetch(inn, pages=5, pause=1.5):
    """All declaration rows for one INN, following pagination."""
    rows = []
    for page in range(1, pages + 1):
        params = {
            "DeclarationSearch[inn]": inn,
            "DeclarationSearch[region_id]": "",
            "DeclarationSearch[tele]": "",
            "DeclarationSearch[verify]": "0",
        }
        if page > 1:
            params["page"] = page
        page_html = _get(BASE + "?" + urllib.parse.urlencode(params))
        body = re.search(r"<tbody>(.*?)</tbody>", page_html, re.S)
        if not body:
            break
        found = 0
        for tr in re.findall(r"<tr[^>]*>(.*?)</tr>", body.group(1), re.S):
            cells = [_clean(td) for td in re.findall(r"<td[^>]*>(.*?)</td>", tr, re.S)]
            if len(cells) < len(COLS) - 2:
                continue
            rec = dict(zip(COLS, cells))
            if rec.get("inn") and rec["inn"].strip() != str(inn):
                continue
            rows.append(rec)
            found += 1
        if found < 20:      # the register pages at 20 rows
            break
        time.sleep(pause)
    return rows


def workplaces(rec):
    """Split the parallel comma-separated lists into one row per position.

    Numbers, titles and headcounts are three separate columns holding lists of
    the same length; when they disagree the headcount is dropped rather than
    guessed, because a misaligned headcount is worse than none.
    """
    def split(field):
        return [x.strip() for x in (rec.get(field) or "").replace("\n", " ").split(",") if x.strip()]

    numbers, titles, counts = split("wp_numbers"), split("positions"), split("headcounts")
    aligned = len(titles) == len(counts)
    out = []
    for i, title in enumerate(titles):
        out.append({
            "wp_number": numbers[i] if i < len(numbers) else None,
            "position": title,
            "headcount": int(counts[i]) if aligned and counts[i].isdigit() else None,
        })
    return out


def first_address(rec):
    """The register often lists several addresses in one cell; take the first."""
    parts = [p.strip() for p in (rec.get("address") or "").split("\n") if p.strip()]
    return parts[0] if parts else None


def store(con, inn, rows):
    n = 0
    for rec in rows:
        addr = first_address(rec)
        for wp in workplaces(rec):
            con.execute(
                """INSERT OR REPLACE INTO sout
                   (inn, decl_date, address, address_key, wp_number, position,
                    headcount, sout_org, valid_until)
                   VALUES (?,?,?,?,?,?,?,?,?)""",
                (inn, rec.get("date"), addr, db.norm_addr(addr), wp["wp_number"] or "",
                 wp["position"], wp["headcount"], rec.get("sout_org"),
                 rec.get("valid_until")))
            n += 1
    con.commit()
    return n


def cmd_load(args):
    con = db.connect(args.db)
    if args.inn:
        targets = args.inn
    elif args.case:
        targets = [r["inn"] for r in
                   con.execute("SELECT inn FROM cases WHERE name=?", (args.case,))]
    else:
        targets = [r["inn"] for r in con.execute("SELECT inn FROM orgs")]
    if not targets:
        print("нет компаний — сначала crawl.py add, или задайте --inn", file=sys.stderr)
        sys.exit(1)

    total = 0
    for inn in targets:
        try:
            rows = fetch(inn, pages=args.pages)
        except Exception as e:
            print(f"[fail] {inn}: {e}", file=sys.stderr)
            continue
        n = store(con, inn, rows) if rows else 0
        total += n
        name = rows[0]["name"][:40] if rows else ""
        print(f"[ok]   {inn}  деклараций {len(rows)}, рабочих мест {n}  {name}")
        time.sleep(args.pause)
    print(f"сохранено рабочих мест: {total}")


def cmd_show(args):
    con = db.connect(args.db)
    rows = con.execute(
        "SELECT * FROM sout WHERE inn=? ORDER BY decl_date DESC", (args.inn,)).fetchall()
    if not rows:
        print("деклараций в базе нет — сначала: python3 rostrud.py load --inn " + args.inn)
        return

    print(f"# Рабочие места по декларациям СОУТ — ИНН {args.inn}\n")
    addrs = {}
    for r in rows:
        addrs.setdefault(r["address"], set()).add(r["decl_date"])
    print("## Фактические адреса\n")
    for a, dates in sorted(addrs.items(), key=lambda x: -len(x[1])):
        print(f"- {a}  ({', '.join(sorted(dates))})")

    reg = con.execute("SELECT address FROM orgs WHERE inn=?", (args.inn,)).fetchone()
    if reg and reg["address"]:
        keys = {r["address_key"] for r in rows}
        if db.norm_addr(reg["address"]) not in keys:
            print(f"\n**Расходится с ЕГРЮЛ:** там {reg['address']}")

    by_decl = {}
    for r in rows:
        by_decl.setdefault(r["decl_date"], []).append(r)
    for date in sorted(by_decl, reverse=True):
        group = by_decl[date]
        total = sum(r["headcount"] or 0 for r in group)
        print(f"\n## Декларация от {date} — рабочих мест {len(group)}"
              f"{f', работников {total}' if total else ''}\n")
        counts = {}
        for r in group:
            counts[r["position"]] = counts.get(r["position"], 0) + (r["headcount"] or 1)
        for pos, n in sorted(counts.items(), key=lambda x: -x[1]):
            print(f"- {pos} — {n}")


def cmd_addresses(args):
    """Organisations sharing a real place of business."""
    con = db.connect(args.db)
    print("## Общий фактический адрес\n")
    rows = con.execute(
        """SELECT s.address_key, MIN(s.address) addr, COUNT(DISTINCT s.inn) n,
                  GROUP_CONCAT(DISTINCT COALESCE(o.name_short, s.inn)) orgs
           FROM sout s LEFT JOIN orgs o ON o.inn = s.inn
           WHERE s.address_key <> ''
           GROUP BY s.address_key HAVING n > 1 ORDER BY n DESC""").fetchall()
    for r in rows:
        print(f"- **{r['addr']}** — {r['n']}: {r['orgs']}")
    if not rows:
        print("_нет_")

    print("\n## Фактический адрес расходится с ЕГРЮЛ\n")
    rows = con.execute(
        """SELECT DISTINCT s.inn, o.name_short, s.address, o.address AS reg
           FROM sout s JOIN orgs o ON o.inn = s.inn
           WHERE o.address_key <> '' AND s.address_key <> ''
             AND s.address_key <> o.address_key""").fetchall()
    seen = set()
    for r in rows:
        if r["inn"] in seen:
            continue
        seen.add(r["inn"])
        print(f"- **{r['name_short'] or r['inn']}**\n    фактически: {r['address']}\n    ЕГРЮЛ: {r['reg']}")
    if not seen:
        print("_нет_")


def cmd_profile(args):
    """Who has their own workforce and who does not."""
    con = db.connect(args.db)
    worker = re.compile(r"рабоч|дворник|разнорабоч|слесар|электрик|сантехник|уборщ|"
                        r"техслуж|тех\.?\s*служ|мастер|монтажник|маляр|плотник", re.I)
    print(f"{'компания':34} {'мест':>5} {'рабочих':>8} {'офис':>6}  профиль")
    for r in con.execute(
            """SELECT s.inn, COALESCE(o.name_short, s.inn) name
               FROM sout s LEFT JOIN orgs o ON o.inn=s.inn GROUP BY s.inn"""):
        latest = con.execute(
            "SELECT MAX(decl_date) d FROM sout WHERE inn=?", (r["inn"],)).fetchone()["d"]
        rows = con.execute(
            "SELECT position, headcount FROM sout WHERE inn=?", (r["inn"],)).fetchall()
        total = len(rows)
        blue = sum(1 for x in rows if worker.search(x["position"] or ""))
        white = total - blue
        verdict = "свой персонал" if blue >= max(3, total * 0.3) else "только офис"
        print(f"{r['name'][:34]:34} {total:5} {blue:8} {white:6}  {verdict}")


def main():
    ap = argparse.ArgumentParser(description="Rostrud SOUT workplace declarations")
    ap.add_argument("--db", default=db.DEFAULT_DB)
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("load", help="fetch declarations for companies")
    p.add_argument("--inn", nargs="*")
    p.add_argument("--case")
    p.add_argument("--pages", type=int, default=5)
    p.add_argument("--pause", type=float, default=1.5)
    p.set_defaults(func=cmd_load)

    p = sub.add_parser("show", help="workplaces of one company")
    p.add_argument("inn")
    p.set_defaults(func=cmd_show)

    p = sub.add_parser("addresses", help="shared real addresses and mismatches with EGRUL")
    p.set_defaults(func=cmd_addresses)

    p = sub.add_parser("profile", help="own workforce vs office-only, per company")
    p.set_defaults(func=cmd_profile)

    args = ap.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
