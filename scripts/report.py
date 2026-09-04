"""Turn the accumulated database into something a human reads.

  dossier <INN>   markdown profile of one company
  graph           Graphviz DOT of the ownership and control network
  person <query>  every company in the database touched by one person
"""

import argparse
import json
import sys

import db
import girbo
import kad

PROFIT_CODES = [("2110", "Выручка"), ("2200", "Прибыль от продаж"), ("2400", "Чистая прибыль")]
BALANCE_CODES = [("1600", "Активы"), ("1300", "Капитал"), ("1500", "Краткосрочные обяз.")]


def money(v):
    if v is None:
        return "—"
    for unit, div in (("млрд", 1e9), ("млн", 1e6), ("тыс", 1e3)):
        if abs(v) >= div:
            return f"{v/div:,.1f} {unit}".replace(",", " ")
    return f"{v:,.0f}".replace(",", " ")


def cmd_dossier(args):
    con = db.connect(args.db)
    org = con.execute("SELECT * FROM orgs WHERE inn=?", (args.inn,)).fetchone()
    if not org:
        print(f"{args.inn} нет в базе — сначала: python3 crawl.py add {args.inn}", file=sys.stderr)
        sys.exit(1)

    raw = json.loads(org["raw"] or "{}")
    out = []
    out.append(f"# {org['name_short'] or org['name_full']}\n")
    out.append(f"**ИНН** {org['inn']} · **ОГРН** {org['ogrn']} · **КПП** {org['kpp'] or '—'}  ")
    out.append(f"**Статус** {org['status']}  ")
    out.append(f"**Зарегистрировано** {org['reg_date']} · **Регион** {org['region'] or '—'}  ")
    out.append(f"**Адрес** {org['address']}  ")
    out.append(f"**Уставный капитал** {money(org['capital'])} руб  ")
    out.append(f"**Основной ОКВЭД** {org['okved'] or '—'}\n")

    out.append("## Руководство\n")
    roles = con.execute(
        """SELECT r.role, p.fio, p.inn FROM roles r JOIN persons p ON p.pid=r.pid
           WHERE r.org_inn=?""", (args.inn,)).fetchall()
    if roles:
        for r in roles:
            out.append(f"- **{r['fio']}** — {r['role']} (ИНН {r['inn'] or '—'})")
    else:
        out.append("_нет данных_")

    out.append("\n## Владельцы\n")
    owners = con.execute(
        """SELECT own.kind, own.share_pct, own.share_rub, p.fio, p.inn AS pinn,
                  own.owner_inn, o2.name_short AS owner_name
           FROM ownership own
           LEFT JOIN persons p ON p.pid = own.pid AND own.pid <> ''
           LEFT JOIN orgs o2 ON o2.inn = own.owner_inn AND own.owner_inn <> ''
           WHERE own.org_inn=?""", (args.inn,)).fetchall()
    if owners:
        for o in owners:
            who = o["fio"] or o["owner_name"] or o["owner_inn"] or "—"
            ident = o["pinn"] or o["owner_inn"] or "—"
            if o["kind"] == "manager":
                out.append(f"- **{who}** [{ident}] — управляющая организация")
            else:
                share = f"{o['share_pct']}%" if o["share_pct"] is not None else "доля н/д"
                out.append(f"- **{who}** [{ident}] — {share} ({money(o['share_rub'])} руб)")
    else:
        out.append("_нет данных (для АО состав акционеров в ЕГРЮЛ не раскрывается)_")

    fin = con.execute(
        "SELECT year, code, value FROM financials WHERE inn=? ORDER BY year DESC", (args.inn,)
    ).fetchall()
    if fin:
        by_year = {}
        for r in fin:
            by_year.setdefault(r["year"], {})[r["code"]] = r["value"]
        years = sorted(by_year, reverse=True)[:5]
        out.append("\n## Финансы\n")
        out.append("| Показатель | " + " | ".join(str(y) for y in years) + " |")
        out.append("|---" * (len(years) + 1) + "|")
        for code, label in PROFIT_CODES + BALANCE_CODES:
            cells = [money(by_year[y].get(code)) for y in years]
            out.append(f"| {label} | " + " | ".join(cells) + " |")

        rows = [{"year": y,
                 "revenue": by_year[y].get("2110"),
                 "net_profit": by_year[y].get("2400"),
                 "equity": by_year[y].get("1300"),
                 "assets": by_year[y].get("1600")} for y in years]
        for i in range(len(rows) - 1):
            prev, cur = rows[i + 1]["revenue"], rows[i]["revenue"]
            if prev:
                rows[i]["revenue_yoy_pct"] = round(100.0 * (cur - prev) / abs(prev), 1)
        red = girbo.flags(rows)
        if red:
            out.append("\n### Красные флаги\n")
            out.extend(f"- {f}" for f in red)
    else:
        out.append("\n## Финансы\n\n_отчетность в ГИР БО отсутствует (банк, страховщик или не сдавал)_")

    heads = con.execute(
        "SELECT date, count FROM headcount WHERE inn=? ORDER BY date DESC", (args.inn,)).fetchall()
    if heads:
        out.append("\n## Численность работников\n")
        for h in heads:
            out.append(f"- на {h['date']}: **{h['count']}** чел")
        rev = con.execute(
            "SELECT value FROM financials WHERE inn=? AND code='2110' ORDER BY year DESC LIMIT 1",
            (args.inn,)).fetchone()
        if rev and heads[0]["count"]:
            out.append(f"- выручка на сотрудника: {money(rev['value']/heads[0]['count'])} руб")

    debts = con.execute(
        """SELECT date, tax, debt, penalty, fine FROM tax_debt
           WHERE inn=? ORDER BY date DESC""", (args.inn,)).fetchall()
    if debts:
        total = sum((d["debt"] or 0) + (d["penalty"] or 0) + (d["fine"] or 0) for d in debts)
        out.append(f"\n## Налоговая задолженность — {money(total)} руб\n")
        for d in debts[:15]:
            out.append(f"- {d['date']} · {d['tax']}: недоимка {money(d['debt'])}, "
                       f"пени {money(d['penalty'])}, штраф {money(d['fine'])}")

    sout = con.execute(
        """SELECT decl_date, address, position, headcount FROM sout
           WHERE inn=? ORDER BY decl_date DESC""", (args.inn,)).fetchall()
    if sout:
        latest = sout[0]["decl_date"]
        addrs = sorted({r["address"] for r in sout if r["address"]})
        out.append(f"\n## Рабочие места (декларации СОУТ) — {len(sout)}\n")
        for a in addrs:
            out.append(f"- фактический адрес: {a}")
        if org["address_key"] and db.norm_addr(addrs[0] if addrs else "") != org["address_key"]:
            out.append("- **фактический адрес не совпадает с ЕГРЮЛ**")
        counts = {}
        for r in sout:
            if r["decl_date"] == latest:
                counts[r["position"]] = counts.get(r["position"], 0) + (r["headcount"] or 1)
        out.append(f"\n### Должности по декларации от {latest}\n")
        for pos, n in sorted(counts.items(), key=lambda x: -x[1])[:20]:
            out.append(f"- {pos} — {n}")

    insp = con.execute(
        """SELECT year, month, number, kind, control_kind, authority, status
           FROM inspections WHERE inn=? ORDER BY year DESC, month DESC""", (args.inn,)).fetchall()
    if insp:
        by_year, by_auth = {}, {}
        for r in insp:
            by_year[r["year"]] = by_year.get(r["year"], 0) + 1
            by_auth[r["authority"] or "—"] = by_auth.get(r["authority"] or "—", 0) + 1
        out.append(f"\n## Проверки (ЕРКНМ) — {len(insp)}\n")
        out.append("- по годам: " + ", ".join(f"{y} — {n}" for y, n in sorted(by_year.items(), reverse=True)))
        for a, n in sorted(by_auth.items(), key=lambda x: -x[1])[:5]:
            out.append(f"- {a}: {n}")
        out.append("\n### Последние мероприятия\n")
        for r in insp[:10]:
            out.append(f"- {r['year']}-{r['month']:02d} №{r['number'] or '—'} · "
                       f"{r['kind'] or '—'} · {r['status'] or '—'}")

    courts = kad.summary(con, args.inn)
    if courts:
        out.append(f"\n## Арбитраж — {courts['total']} дел\n")
        out.append("- роли: " + ", ".join(f"{k} {v}" for k, v in courts["by_side"].items()))
        out.append("- категории: " + ", ".join(f"{k} {v}" for k, v in courts["by_type"].items()))
        out.append("- по годам: " + ", ".join(f"{k} — {v}" for k, v in courts["by_year"].items()))
        if any("анкрот" in (t or "") for t in courts["by_type"]):
            out.append("- **есть банкротные дела** — проверить, в каком качестве")
        if courts["counterparties"]:
            out.append("\n### Частые процессуальные оппоненты\n")
            for c in courts["counterparties"][:10]:
                out.append(f"- {c['name']} [{c['inn'] or '—'}] — {c['n']} дел")

    lic = raw.get("licenses") or []
    if lic:
        out.append("\n## Лицензии\n")
        out.extend(f"- {x}" for x in lic[:20])

    okveds = raw.get("okved_all") or []
    if len(okveds) > 1:
        out.append(f"\n## Виды деятельности ({len(okveds)})\n")
        out.extend(f"- {x}" for x in okveds[:25])
        if len(okveds) > 25:
            out.append(f"- … ещё {len(okveds)-25}")

    # Anything the local database already knows that touches this company.
    out.append("\n## Связи по базе\n")
    linked = con.execute(
        """SELECT DISTINCT o.inn, o.name_short, p.fio
           FROM roles r JOIN persons p ON p.pid=r.pid
           JOIN roles r2 ON r2.pid=r.pid AND r2.org_inn<>r.org_inn
           JOIN orgs o ON o.inn=r2.org_inn
           WHERE r.org_inn=?""", (args.inn,)).fetchall()
    for r in linked:
        out.append(f"- через **{r['fio']}** → {r['name_short']} [{r['inn']}]")
    same_addr = con.execute(
        """SELECT inn, name_short FROM orgs
           WHERE address_key = (SELECT address_key FROM orgs WHERE inn=?)
             AND inn <> ? AND address_key <> ''""", (args.inn, args.inn)).fetchall()
    for r in same_addr:
        out.append(f"- общий адрес → {r['name_short']} [{r['inn']}]")
    if not linked and not same_addr:
        out.append("_пересечений в накопленной базе нет — расширьте охват (`crawl.py add`)_")

    text = "\n".join(out)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            f.write(text + "\n")
        print(f"написано в {args.out}")
    else:
        print(text)


def cmd_graph(args):
    con = db.connect(args.db)
    where, params = "", []
    if args.case:
        where = " WHERE inn IN (SELECT inn FROM cases WHERE name=?)"
        params = [args.case]

    orgs = con.execute(f"SELECT inn, name_short, status FROM orgs{where}", params).fetchall()
    known = {o["inn"] for o in orgs}
    # Owners referenced by an edge but never fetched: draw them as stubs so the
    # graph shows where the chain continues rather than silently ending.
    stubs = {r["owner_inn"] for r in con.execute(
        "SELECT DISTINCT owner_inn FROM ownership WHERE owner_inn <> ''")} - known

    lines = ["digraph corp {", '  rankdir=LR; node [fontname="sans-serif", style=filled];']
    for o in orgs:
        dead = o["status"] and o["status"] != "действующее"
        colour = "#f8d7da" if dead else "#d1e7dd"
        label = (o["name_short"] or o["inn"]).replace('"', "'")
        lines.append(f'  "{o["inn"]}" [label="{label}\\n{o["inn"]}", shape=box, fillcolor="{colour}"];')
    for inn in sorted(stubs):
        lines.append(f'  "{inn}" [label="{inn}\\n(не загружено)", shape=box, fillcolor="#e2e3e5"];')

    persons = con.execute(
        """SELECT DISTINCT p.pid, p.fio FROM persons p
           WHERE p.fio IS NOT NULL AND (
             p.pid IN (SELECT pid FROM roles) OR p.pid IN (SELECT pid FROM ownership))"""
    ).fetchall()
    for p in persons:
        lines.append(f'  "{p["pid"]}" [label="{p["fio"]}", shape=ellipse, fillcolor="#fff3cd"];')

    for r in con.execute("SELECT org_inn, pid, role FROM roles"):
        if r["org_inn"] in known:
            lines.append(f'  "{r["pid"]}" -> "{r["org_inn"]}" [label="{r["role"].lower()}", style=dashed];')
    for r in con.execute("SELECT org_inn, pid, owner_inn, kind, share_pct FROM ownership"):
        if r["org_inn"] not in known and r["org_inn"] not in stubs:
            continue
        src = r["pid"] or r["owner_inn"]
        if not src:
            continue
        label = "управление" if r["kind"] == "manager" else (
            f"{r['share_pct']}%" if r["share_pct"] is not None else "владение")
        style = ", style=dotted" if r["kind"] == "manager" else ""
        lines.append(f'  "{src}" -> "{r["org_inn"]}" [label="{label}"{style}];')
    lines.append("}")

    if args.format == "mermaid":
        out = ["graph LR"]
        for o in orgs:
            label = (o["name_short"] or o["inn"]).replace('"', "'")
            out.append(f'  n{o["inn"]}["{label}<br/>{o["inn"]}"]')
        for inn in sorted(stubs):
            out.append(f'  n{inn}["{inn}<br/>(не загружено)"]')
        for p in persons:
            out.append(f'  n{p["pid"]}(("{p["fio"]}"))')
        for r in con.execute("SELECT org_inn, pid, role FROM roles"):
            if r["org_inn"] in known:
                out.append(f'  n{r["pid"]} -.{r["role"].lower()}.-> n{r["org_inn"]}')
        for r in con.execute("SELECT org_inn, pid, owner_inn, kind, share_pct FROM ownership"):
            src = r["pid"] or r["owner_inn"]
            if r["org_inn"] not in known or not src:
                continue
            label = "управление" if r["kind"] == "manager" else (
                f"{r['share_pct']}%" if r["share_pct"] is not None else "владение")
            out.append(f'  n{src} --{label}--> n{r["org_inn"]}')
        text = "\n".join(out)
        hint = "вставьте в markdown-блок ```mermaid"
    else:
        text = "\n".join(lines)
        hint = f"отрисовать: dot -Tsvg {args.out} -o graph.svg  (нужен graphviz)"

    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            f.write(text + "\n")
        print(f"написано в {args.out}\n{hint}")
    else:
        print(text)


def cmd_person(args):
    con = db.connect(args.db)
    q = f"%{db.norm_fio(args.query)}%"
    rows = con.execute(
        """SELECT p.fio, p.inn AS pinn, o.inn, o.name_short, o.status,
                  COALESCE(r.role, 'УЧРЕДИТЕЛЬ') AS role, own.share_pct
           FROM persons p
           LEFT JOIN roles r ON r.pid = p.pid
           LEFT JOIN ownership own ON own.pid = p.pid
           JOIN orgs o ON o.inn = COALESCE(r.org_inn, own.org_inn)
           WHERE p.fio_key LIKE ? OR p.inn = ?""", (q, args.query)).fetchall()
    if not rows:
        print("не найдено в накопленной базе")
        return
    print(f"# {rows[0]['fio']} (ИНН {rows[0]['pinn'] or '—'})\n")
    for r in rows:
        share = f", доля {r['share_pct']}%" if r["share_pct"] is not None else ""
        print(f"- **{r['name_short']}** [{r['inn']}] — {r['role']}{share} · {r['status']}")


def main():
    ap = argparse.ArgumentParser(description="Reports over the accumulated database")
    ap.add_argument("--db", default=db.DEFAULT_DB)
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("dossier", help="markdown profile of one company")
    p.add_argument("inn")
    p.add_argument("--out")
    p.set_defaults(func=cmd_dossier)

    p = sub.add_parser("graph", help="ownership and control network")
    p.add_argument("--case")
    p.add_argument("--out")
    p.add_argument("--format", choices=("dot", "mermaid"), default="dot")
    p.set_defaults(func=cmd_graph)

    p = sub.add_parser("person", help="companies touched by one person")
    p.add_argument("query", help="FIO fragment or personal INN")
    p.set_defaults(func=cmd_person)

    args = ap.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
