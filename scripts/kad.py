"""Arbitration cases from the court registry at kad.arbitr.ru.

Litigation is the single best free source of economic ties that no registry
records: a recurring plaintiff/defendant pair means the two companies actually
traded with each other. Cases are stored with all their parties, so
`crawl.py links` can surface counterparties shared across targets.

The endpoint is behind an anti-scraping layer that bans addresses sending many
requests, and it answers HTTP 451 with an HTML page rather than JSON when it
decides to block. That state is detected and reported explicitly instead of
being mistaken for "no cases".
"""

import argparse
import http.cookiejar
import json
import sys
import time
import urllib.error
import urllib.request

import db

BASE = "https://kad.arbitr.ru"
UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")

# Side type in the registry's own numbering.
ANY, PLAINTIFF, DEFENDANT, THIRD = -1, 1, 2, 3
SIDE_NAMES = {1: "истец", 2: "ответчик", 3: "третье лицо"}


class KadBlocked(RuntimeError):
    pass


class KadError(RuntimeError):
    pass


def _session():
    jar = http.cookiejar.CookieJar()
    op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
    op.addheaders = []
    # The API only answers to clients that already hold a session cookie.
    req = urllib.request.Request(BASE + "/", headers={
        "User-Agent": UA,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.8",
    })
    try:
        op.open(req, timeout=60).read()
    except urllib.error.HTTPError as e:
        if e.code == 451:
            raise KadBlocked(_blocked_message())
        raise
    return op


def _blocked_message():
    return (
        "kad.arbitr.ru отдал 451 «Доступ заблокирован». Сервис банит адреса, с "
        "которых идёт много запросов, и это в первую очередь касается IP "
        "дата-центров, VPN и общих выходных узлов. Запускайте с обычного "
        "домашнего или офисного подключения и снижайте темп (--pause). "
        "Разблокировка — support_kad@pravo.tech."
    )


def search(query, side=ANY, pages=2, pause=2.0, date_from=None, date_to=None, op=None):
    """Search cases where `query` (INN or name) is a party."""
    op = op or _session()
    out = []
    for page in range(1, pages + 1):
        body = json.dumps({
            "Page": page,
            "Count": 25,
            "Courts": [],
            "DateFrom": date_from,
            "DateTo": date_to,
            "Sides": [{"Name": query, "Type": side, "ExactMatch": False}],
            "Judges": [],
            "CaseNumbers": [],
            "WithVKSInstances": False,
        }, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(BASE + "/Kad/SearchInstances", data=body, headers={
            "User-Agent": UA,
            "Content-Type": "application/json",
            "Accept": "application/json, text/javascript, */*; q=0.01",
            "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.8",
            "X-Requested-With": "XMLHttpRequest",
            "x-date-format": "iso",
            "Origin": BASE,
            "Referer": BASE + "/",
        })
        try:
            with op.open(req, timeout=90) as r:
                payload = r.read()
        except urllib.error.HTTPError as e:
            if e.code == 451:
                raise KadBlocked(_blocked_message())
            raise KadError(f"HTTP {e.code} от kad.arbitr.ru")
        if payload.lstrip()[:1] in (b"<", b""):
            raise KadBlocked(_blocked_message())
        try:
            data = json.loads(payload.decode("utf-8"))
        except ValueError:
            raise KadError("ответ kad.arbitr.ru не разобрался как JSON")

        result = data.get("Result") or {}
        items = result.get("Items") or []
        out.extend(items)
        if not items or page * 25 >= (result.get("TotalCount") or 0):
            break
        time.sleep(pause)
    return out


def normalise(item):
    """Flatten one registry item into a case plus its parties."""
    case = {
        "case_id": item.get("CaseId"),
        "number": item.get("CaseNumber"),
        "date": (item.get("Date") or "")[:10],
        "court": (item.get("Court") or {}).get("Name") if isinstance(item.get("Court"), dict)
                 else item.get("CourtName"),
        "judge": (item.get("Judge") or {}).get("Name") if isinstance(item.get("Judge"), dict)
                 else item.get("Judge"),
        "type": (item.get("Type") or {}).get("Name") if isinstance(item.get("Type"), dict)
                else item.get("Type"),
    }
    case["url"] = f"{BASE}/Card/{case['case_id']}" if case["case_id"] else None

    parties = []
    # Newer payloads split the sides; older ones use one Sides list with Type.
    buckets = [("истец", item.get("Plaintiffs")), ("ответчик", item.get("Respondents")),
               ("иное", item.get("Others"))]
    if not any(b for _, b in buckets):
        buckets = [(None, item.get("Sides"))]
    for side, group in buckets:
        for entry in group or []:
            for org in (entry.get("Organizations") or [entry]):
                name = org.get("Name") or entry.get("Name")
                if not name:
                    continue
                parties.append({
                    "name": name.strip(),
                    "inn": (org.get("Inn") or entry.get("Inn") or "").strip(),
                    "side": side or SIDE_NAMES.get(entry.get("Type"), "иное"),
                })
    return case, parties


def store(con, items):
    cases = 0
    for item in items:
        case, parties = normalise(item)
        if not case["case_id"]:
            continue
        con.execute(
            """INSERT OR REPLACE INTO court_cases (case_id, number, date, court, judge, type, url)
               VALUES (:case_id,:number,:date,:court,:judge,:type,:url)""", case)
        for p in parties:
            con.execute(
                "INSERT OR REPLACE INTO court_parties (case_id, inn, name, side) VALUES (?,?,?,?)",
                (case["case_id"], p["inn"], p["name"], p["side"]))
        cases += 1
    con.commit()
    return cases


def summary(con, inn):
    """What the stored cases say about one company."""
    rows = con.execute(
        """SELECT c.case_id, c.number, c.date, c.court, c.type, c.url, p.side
           FROM court_parties p JOIN court_cases c ON c.case_id = p.case_id
           WHERE p.inn = ? ORDER BY c.date DESC""", (inn,)).fetchall()
    if not rows:
        return None

    by_side, by_type, by_year = {}, {}, {}
    for r in rows:
        by_side[r["side"]] = by_side.get(r["side"], 0) + 1
        by_type[r["type"] or "—"] = by_type.get(r["type"] or "—", 0) + 1
        by_year[(r["date"] or "")[:4]] = by_year.get((r["date"] or "")[:4], 0) + 1

    counterparties = con.execute(
        """SELECT o.name, o.inn, COUNT(DISTINCT o.case_id) n
           FROM court_parties me
           JOIN court_parties o ON o.case_id = me.case_id AND o.name <> me.name
           WHERE me.inn = ?
           GROUP BY o.name ORDER BY n DESC LIMIT 15""", (inn,)).fetchall()

    return {
        "total": len({r["case_id"] for r in rows}),
        "by_side": by_side,
        "by_type": by_type,
        "by_year": dict(sorted(by_year.items(), reverse=True)),
        "counterparties": [dict(c) for c in counterparties],
        "recent": [dict(r) for r in rows[:15]],
    }


def cmd_fetch(args):
    con = db.connect(args.db)
    try:
        items = search(args.inn, pages=args.pages, pause=args.pause)
    except KadBlocked as e:
        print(f"заблокировано: {e}", file=sys.stderr)
        sys.exit(2)
    except KadError as e:
        print(f"ошибка: {e}", file=sys.stderr)
        sys.exit(1)
    n = store(con, items)
    print(f"{args.inn}: сохранено дел — {n}")
    cmd_show(args, con=con)


def cmd_show(args, con=None):
    con = con or db.connect(args.db)
    s = summary(con, args.inn)
    if not s:
        print("дел в базе нет — сначала: python3 kad.py fetch " + args.inn)
        return
    print(f"\n## Арбитраж: {s['total']} дел\n")
    print("Роли: " + ", ".join(f"{k} {v}" for k, v in s["by_side"].items()))
    print("Категории: " + ", ".join(f"{k} {v}" for k, v in s["by_type"].items()))
    print("По годам: " + ", ".join(f"{k} — {v}" for k, v in s["by_year"].items()))
    if s["counterparties"]:
        print("\n### Частые процессуальные оппоненты\n")
        for c in s["counterparties"]:
            print(f"- {c['name']} [{c['inn'] or '—'}] — {c['n']} дел")
    print("\n### Последние дела\n")
    for r in s["recent"]:
        print(f"- {r['date']} {r['number']} ({r['side']}, {r['type'] or '—'}) {r['url']}")


def main():
    ap = argparse.ArgumentParser(description="Arbitration cases from kad.arbitr.ru")
    ap.add_argument("--db", default=db.DEFAULT_DB)
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("fetch", help="download cases for an INN and store them")
    p.add_argument("inn")
    p.add_argument("--pages", type=int, default=2, help="25 cases per page")
    p.add_argument("--pause", type=float, default=2.0)
    p.set_defaults(func=cmd_fetch)

    p = sub.add_parser("show", help="report on already stored cases")
    p.add_argument("inn")
    p.set_defaults(func=cmd_show)

    args = ap.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
