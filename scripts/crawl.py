"""Walk the ownership graph outward from a seed INN and persist everything.

Expansion is corporate-first: a company's legal-entity founders and its
managing organisation are real, checkable edges in the registry, so those get
queued automatically. Person-to-company traversal is not possible from the
free FNS endpoints — the registry has no reverse index — so people accumulate
as nodes and their shared directorships surface only once both companies are
in the local database. `links` reports those intersections.
"""

import argparse
import sys
import time
import traceback

import db
import egrul
import girbo


def store_org(con, org, case=None):
    inn = org["inn"]
    db.upsert_org(con, {
        "inn": inn,
        "ogrn": org.get("ogrn"),
        "kpp": org.get("kpp"),
        "name_short": org.get("name_short"),
        "name_full": org.get("name_full"),
        "status": org.get("status"),
        "reg_date": org.get("reg_date"),
        "address": org.get("address"),
        "okved": org.get("okved"),
        "okved_name": None,
        "capital": org.get("capital"),
        "region": org.get("region"),
        "raw": {k: v for k, v in org.items() if k != "fields"},
    })
    db.tag_case(con, case, inn)

    for d in org.get("directors") or []:
        if not d.get("fio"):
            continue
        pid = db.upsert_person(con, d.get("inn"), d.get("fio"))
        db.add_role(con, inn, pid, d.get("post") or "РУКОВОДИТЕЛЬ", d.get("grn"))

    for f in org.get("founders") or []:
        if not f.get("fio"):
            continue
        pid = db.upsert_person(con, f.get("inn"), f.get("fio"))
        db.add_ownership(con, inn, pid=pid, share_pct=f.get("share_pct"),
                         share_rub=f.get("share_rub"), grn=f.get("grn"))

    children = []
    for f in org.get("founder_orgs") or []:
        owner_inn = (f.get("inn") or "").strip()
        if not owner_inn:
            continue
        db.add_ownership(con, inn, owner_inn=owner_inn, share_pct=f.get("share_pct"),
                         share_rub=f.get("share_rub"), grn=f.get("grn"))
        children.append(owner_inn)

    # The managing organisation is a control edge, not an ownership one, so it
    # is stored with a null share rather than as a role against a fake person.
    mgr = org.get("manager_org") or {}
    if mgr.get("inn"):
        db.add_ownership(con, inn, owner_inn=mgr["inn"], grn=mgr.get("grn"), kind="manager")
        children.append(mgr["inn"])
    return children


def store_financials(con, inn):
    try:
        data = girbo.fetch(inn)
    except Exception as e:
        return f"financials unavailable: {e}"
    for year, y in data["years"].items():
        lines = dict(y["balance"])
        lines.update(y["profit"])
        db.add_financials(con, inn, year, lines)
    return None


def fetch_one(con, inn, depth, case=None, with_financials=True):
    org = egrul.extract(inn)
    children = store_org(con, org, case=case)
    note = None
    if with_financials:
        note = store_financials(con, inn)
    if depth > 0:
        for child in children:
            db.enqueue(con, child, depth - 1)
    db.mark(con, inn, "done", note)
    con.commit()
    return org, children


def cmd_add(args):
    con = db.connect(args.db)
    for inn in args.inn:
        db.enqueue(con, inn, args.depth)
    con.commit()
    cmd_expand(args, con=con)


def cmd_expand(args, con=None):
    con = con or db.connect(args.db)
    processed = 0
    while processed < args.limit:
        row = con.execute(
            "SELECT inn, depth FROM crawl_queue WHERE state='pending' ORDER BY depth DESC LIMIT 1"
        ).fetchone()
        if not row:
            break
        inn, depth = row["inn"], row["depth"]
        try:
            org, children = fetch_one(con, inn, depth, case=args.case,
                                      with_financials=not args.no_financials)
            name = org.get("name_short") or org.get("name_full") or inn
            print(f"[ok]   {inn}  {name}  (depth {depth}, +{len(children)} owners)")
        except Exception as e:
            db.mark(con, inn, "error", str(e))
            con.commit()
            print(f"[fail] {inn}  {e}", file=sys.stderr)
            if args.verbose:
                traceback.print_exc()
        processed += 1
        time.sleep(args.pause)
    print(f"processed {processed}")
    pending = con.execute("SELECT COUNT(*) c FROM crawl_queue WHERE state='pending'").fetchone()["c"]
    if pending:
        print(f"{pending} still pending — rerun expand")


def cmd_links(args):
    """Intersections that only exist because several targets share the database."""
    con = db.connect(args.db)
    scope = ""
    params = []
    if args.case:
        scope = " AND o.inn IN (SELECT inn FROM cases WHERE name=?)"
        params = [args.case]

    print("## Общие физические лица\n")
    q = f"""
        SELECT p.pid, p.fio, p.inn AS person_inn,
               GROUP_CONCAT(o.name_short || ' [' || o.inn || ']', ' | ') AS orgs,
               COUNT(DISTINCT o.inn) AS n
        FROM persons p
        JOIN (SELECT org_inn, pid FROM roles
              UNION SELECT org_inn, pid FROM ownership WHERE pid <> '') l ON l.pid = p.pid
        JOIN orgs o ON o.inn = l.org_inn
        WHERE 1=1 {scope}
        GROUP BY p.pid HAVING n > 1 ORDER BY n DESC"""
    rows = con.execute(q, params * 1).fetchall()
    for r in rows:
        print(f"- **{r['fio']}** (ИНН {r['person_inn'] or '—'}) — {r['n']} компаний: {r['orgs']}")
    if not rows:
        print("_нет_")

    print("\n## Общий адрес регистрации\n")
    q = f"""
        SELECT o.address_key, COUNT(*) n,
               GROUP_CONCAT(o.name_short || ' [' || o.inn || ']', ' | ') orgs,
               MIN(o.address) addr
        FROM orgs o WHERE o.address_key <> '' {scope}
        GROUP BY o.address_key HAVING n > 1 ORDER BY n DESC"""
    rows = con.execute(q, params * 1).fetchall()
    for r in rows:
        print(f"- **{r['addr']}** — {r['n']}: {r['orgs']}")
    if not rows:
        print("_нет_")

    # The registration address is easy to share on paper; the declared place of
    # work is where a group actually sits, so it is the stronger signal.
    print("\n## Общий фактический адрес (декларации СОУТ)\n")
    sout_scope = " AND s.inn IN (SELECT inn FROM cases WHERE name=?)" if args.case else ""
    rows = con.execute(
        f"""SELECT MIN(s.address) addr, COUNT(DISTINCT s.inn) n,
                   GROUP_CONCAT(DISTINCT COALESCE(o.name_short, s.inn)) orgs
            FROM sout s LEFT JOIN orgs o ON o.inn = s.inn
            WHERE s.address_key <> '' {sout_scope}
            GROUP BY s.address_key HAVING n > 1 ORDER BY n DESC""", params).fetchall()
    for r in rows:
        print(f"- **{r['addr']}** — {r['n']}: {r['orgs']}")
    if not rows:
        print("_нет (загрузите: python3 rostrud.py load)_")

    print("\n## Корпоративное владение\n")
    q = f"""
        SELECT own.owner_inn, own.org_inn, own.share_pct, own.kind,
               COALESCE(po.name_short, own.owner_inn) owner_name,
               COALESCE(o.name_short, own.org_inn) org_name
        FROM ownership own
        JOIN orgs o ON o.inn = own.org_inn
        LEFT JOIN orgs po ON po.inn = own.owner_inn
        WHERE own.owner_inn <> '' {scope}"""
    rows = con.execute(q, params * 1).fetchall()
    for r in rows:
        if r["kind"] == "manager":
            label = "управляющая организация"
        else:
            label = f"{r['share_pct']}%" if r["share_pct"] is not None else "доля н/д"
        print(f"- {r['owner_name']} [{r['owner_inn']}] -> {r['org_name']} [{r['org_inn']}] ({label})")
    if not rows:
        print("_нет_")

    # Houses handed from one licensee to another are a dated behavioural tie;
    # only GIS ZhKH records them.
    print("\n## Передачи домов между УК (ГИС ЖКХ)\n")
    import gis_zhkh
    scope_inns = [r["inn"] for r in con.execute(
        "SELECT inn FROM cases WHERE name=?", (args.case,))] if args.case else None
    pairs = {}
    for r in gis_zhkh.transfers(con, scope_inns):
        if scope_inns and not (r["from_inn"] in scope_inns and r["to_inn"] in scope_inns):
            continue
        pairs.setdefault((r["from_inn"], r["to_inn"]), []).append(r)
    nm = gis_zhkh.names(con)
    for (a, b), items in sorted(pairs.items(), key=lambda x: -len(x[1])):
        dates = sorted(i["exclusion_date"] for i in items)
        print(f"- {nm.get(a) or a} [{a}] → {nm.get(b) or b} [{b}] — домов {len(items)}, "
              f"{dates[0]} … {dates[-1]}")
    if not pairs:
        print("_нет (загрузите: python3 gis_zhkh.py load --case <дело> --history --deep)_")

    # Two targets meeting in the same case is an economic tie no registry records.
    print("\n## Общие арбитражные дела\n")
    court_scope = ""
    court_params = []
    if args.case:
        court_scope = (" AND a.inn IN (SELECT inn FROM cases WHERE name=?)"
                       " AND b.inn IN (SELECT inn FROM cases WHERE name=?)")
        court_params = [args.case, args.case]
    q = f"""
        SELECT a.inn AS inn_a, b.inn AS inn_b,
               oa.name_short AS name_a, ob.name_short AS name_b,
               COUNT(DISTINCT a.case_id) n
        FROM court_parties a
        JOIN court_parties b ON b.case_id = a.case_id AND b.inn > a.inn
        JOIN orgs oa ON oa.inn = a.inn
        JOIN orgs ob ON ob.inn = b.inn
        WHERE a.inn <> '' AND b.inn <> '' {court_scope}
        GROUP BY a.inn, b.inn ORDER BY n DESC"""
    rows = con.execute(q, court_params).fetchall()
    for r in rows:
        print(f"- {r['name_a']} [{r['inn_a']}] ↔ {r['name_b']} [{r['inn_b']}] — {r['n']} дел")
    if not rows:
        print("_нет (загрузите дела: python3 kad.py fetch <ИНН>)_")


def cmd_status(args):
    con = db.connect(args.db)
    for table in ("orgs", "persons", "roles", "ownership", "financials"):
        n = con.execute(f"SELECT COUNT(*) c FROM {table}").fetchone()["c"]
        print(f"{table:12} {n}")
    print("\nочередь:")
    for r in con.execute("SELECT state, COUNT(*) c FROM crawl_queue GROUP BY state"):
        print(f"  {r['state']:8} {r['c']}")
    errs = con.execute("SELECT inn, note FROM crawl_queue WHERE state='error' LIMIT 10").fetchall()
    for r in errs:
        print(f"  ! {r['inn']}: {r['note']}")


def main():
    ap = argparse.ArgumentParser(description="Ownership-graph crawler over the FNS registry")
    ap.add_argument("--db", default=db.DEFAULT_DB)
    sub = ap.add_subparsers(dest="cmd", required=True)

    for name, fn, helptext in (("add", cmd_add, "queue INNs and crawl them"),
                               ("expand", cmd_expand, "drain the pending queue")):
        p = sub.add_parser(name, help=helptext)
        if name == "add":
            p.add_argument("inn", nargs="+")
        p.add_argument("--depth", type=int, default=1,
                       help="how many ownership hops to follow (default 1)")
        p.add_argument("--limit", type=int, default=25, help="max fetches this run")
        p.add_argument("--pause", type=float, default=2.0, help="seconds between fetches")
        p.add_argument("--case", help="tag every fetched company with this case name")
        p.add_argument("--no-financials", action="store_true")
        p.add_argument("--verbose", action="store_true")
        p.set_defaults(func=fn)

    p = sub.add_parser("links", help="report intersections across the database")
    p.add_argument("--case")
    p.set_defaults(func=cmd_links)

    p = sub.add_parser("status", help="database and queue counters")
    p.set_defaults(func=cmd_status)

    args = ap.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
