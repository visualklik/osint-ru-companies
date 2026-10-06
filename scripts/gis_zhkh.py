"""Housing management licences and the houses behind them, from GIS ZhKH.

GIS ZhKH (dom.gosuslugi.ru) is the state system every housing management
company must report to. Its public REST API answers without a key or captcha,
and gives three things no other free source has:

- the licence, with the licensee's own phone and e-mail;
- every house managed under the licence, **with inclusion and exclusion
  dates** — so a house leaving one company and arriving at another is visible;
- for any house, every licence that has ever covered it.

House transfers are the strongest behavioural link between management
companies: a building handed from one firm to another inside a group leaves a
dated trail here and nowhere else.

Endpoints (all POST, JSON, discovered from the public cabinet's own traffic):

    /rls/api/rest/services/license/public/search        licences
    /rls/api/rest/services/license/public/house/search  houses of a licence
    /ppa/api/rest/services/ppa/public/organizations/searchByOrg   organisation card

The server rejects unknown fields with a list of the accepted ones, which is how
the request shapes below were established.

Source: ГИС ЖКХ, Минстрой России, https://dom.gosuslugi.ru
"""

import argparse
import json
import re
import sys
import time
import urllib.error
import urllib.request

import db

BASE = "https://dom.gosuslugi.ru"
RLS = BASE + "/rls/api/rest/services/license/public"
PPA = BASE + "/ppa/api/rest/services/ppa/public/organizations/searchByOrg"
UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")
PAGE = 100
HOUSE_STATUSES = ("INCLUDED", "EXCLUDED", "CANCELED")


class GisError(RuntimeError):
    pass


def _post(url, body, pause=0.4):
    req = urllib.request.Request(url, data=json.dumps(body).encode("utf-8"), headers={
        "User-Agent": UA,
        "Content-Type": "application/json;charset=UTF-8",
        "Accept": "application/json",
        "Referer": BASE + "/",
    })
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                data = json.loads(r.read().decode("utf-8"))
            time.sleep(pause)
            return data
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", errors="replace")[:300]
            if e.code >= 500 and attempt < 2:
                time.sleep(3 * (attempt + 1))
                continue
            raise GisError(f"HTTP {e.code} {url}: {detail}")
        except urllib.error.URLError as e:
            if attempt < 2:
                time.sleep(3 * (attempt + 1))
                continue
            raise GisError(f"{url}: {e}")


def _paged(url, body):
    page = 1
    while True:
        data = _post(url, dict(body, pageIndex=page, elementsPerPage=PAGE))
        items = data.get("items") or []
        yield from items
        if page * PAGE >= (data.get("total") or 0) or not items:
            return
        page += 1


# --- fetching --------------------------------------------------------------

def licenses_of_region(region_code):
    return list(_paged(RLS + "/search", {"regionCode": str(region_code)}))


def licenses_of_house(house_guid):
    return list(_paged(RLS + "/search", {"address": {"houseGuid": house_guid}}))


def houses_of_license(license_guid):
    out = []
    for status in HOUSE_STATUSES:
        for item in _paged(RLS + "/house/search",
                           {"licenseGuid": license_guid, "houseStatuses": [status]}):
            item["_status"] = status
            out.append(item)
    return out


def org_card(inn):
    data = _post(PPA + "?pageIndex=1&elementsPerPage=10", {"inn": inn})
    items = data.get("organizationSummaryWithNsiList") or []
    return items[0] if items else None


# --- phones ----------------------------------------------------------------

def norm_phone(raw):
    """Ten-digit national number, or None. Accepts +7, 8 and bare forms."""
    d = re.sub(r"\D", "", raw or "")
    if len(d) == 11 and d[0] in "78":
        d = d[1:]
    return d if len(d) == 10 else None


def phone_block(phone):
    """Landline block: area code plus the first two local digits.

    Companies that buy numbers together end up in the same block, often with
    'vanity' tails. Mobile numbers have no meaningful block and return None.
    """
    p = norm_phone(phone)
    if not p or p[0] == "9":
        return None
    return p[:6]


def is_vanity(phone):
    """Tail of two repeated pairs: 99-80-80, 45-17-17."""
    p = norm_phone(phone)
    return bool(p) and p[-4:-2] == p[-2:]


def pretty(phone):
    p = norm_phone(phone)
    return f"+7 {p[:3]} {p[3:6]}-{p[6:8]}-{p[8:]}" if p else (phone or "—")


# --- storage ---------------------------------------------------------------

def store_license(con, lic):
    lee = lic.get("licensee") or {}
    reg = lic.get("licenseRegistration") or {}
    con.execute(
        """INSERT OR REPLACE INTO gzhkh_licenses
           (guid, number, erul_number, inn, ogrn, name, status, start_date,
            expiration_date, house_count, phone, email, region, fetched_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (lic.get("guid"), lic.get("licenseNumber"), lic.get("licenseErulNumber"),
         lee.get("inn"), lee.get("ogrn"), lee.get("name"), lic.get("status"),
         lic.get("licenseStartDate"),
         lic.get("licenseExpirationDate") or reg.get("licenseExpirationDate"),
         lic.get("houseCount"), lee.get("phone"), lee.get("email"),
         lic.get("regionName"), int(time.time())))


def store_houses(con, license_guid, inn, houses):
    con.execute("DELETE FROM gzhkh_houses WHERE license_guid=?", (license_guid,))
    for h in houses:
        a = h.get("address") or {}
        con.execute(
            """INSERT OR REPLACE INTO gzhkh_houses
               (license_guid, inn, house_guid, address, address_key, status,
                inclusion_date, exclusion_date)
               VALUES (?,?,?,?,?,?,?,?)""",
            (license_guid, inn, a.get("houseGuid") or "", a.get("formattedAddress"),
             db.norm_addr(a.get("formattedAddress")), h["_status"],
             h.get("inclusionDate") or "", h.get("exclusionDate")))


def store_org(con, inn, card):
    if not card:
        return
    con.execute(
        """INSERT OR REPLACE INTO gzhkh_orgs (inn, guid, phone, email, fact_address, fetched_at)
           VALUES (?,?,?,?,?,?)""",
        (inn, card.get("guid"), card.get("phone"), card.get("orgEmail"),
         card.get("factualAddress") if isinstance(card.get("factualAddress"), str)
         else json.dumps(card.get("factualAddress"), ensure_ascii=False)
         if card.get("factualAddress") else None,
         int(time.time())))


def load_history(con, inns, pause_note=True):
    """For every house of the given companies, find every licence that covered it."""
    q = ",".join("?" * len(inns))
    guids = [r["house_guid"] for r in con.execute(
        f"SELECT DISTINCT house_guid FROM gzhkh_houses WHERE inn IN ({q}) AND house_guid<>''",
        list(inns))]
    if pause_note:
        print(f"история домов: {len(guids)} домов, по запросу на каждый")
    for i, g in enumerate(guids, 1):
        for lic in licenses_of_house(g):
            lee = lic.get("licensee") or {}
            con.execute(
                """INSERT OR REPLACE INTO gzhkh_house_licensees
                   (house_guid, license_guid, inn, name, license_status)
                   VALUES (?,?,?,?,?)""",
                (g, lic.get("guid"), lee.get("inn"), lee.get("name"), lic.get("status")))
            # A licence we have not loaded yet: keep its card so names, phones
            # and dates can be filled in by `load --deep`.
            if not con.execute("SELECT 1 FROM gzhkh_licenses WHERE guid=?",
                               (lic.get("guid"),)).fetchone():
                store_license(con, lic)
        if i % 25 == 0:
            con.commit()
            print(f"\r  {i}/{len(guids)}", end="", flush=True)
    con.commit()
    print()


# --- commands --------------------------------------------------------------

def _targets(con, args):
    if args.inn:
        return list(args.inn)
    if args.case:
        return [r["inn"] for r in con.execute("SELECT inn FROM cases WHERE name=?", (args.case,))]
    return [r["inn"] for r in con.execute("SELECT inn FROM orgs")]


def cmd_load(args):
    con = db.connect(args.db)
    inns = _targets(con, args)
    if not inns:
        print("нет компаний — задайте --inn или --case", file=sys.stderr)
        sys.exit(1)

    print(f"лицензии региона {args.region}…")
    region = licenses_of_region(args.region)
    for lic in region:
        store_license(con, lic)
    con.commit()
    print(f"  в реестре региона {len(region)} лицензий")

    by_inn = {}
    for lic in region:
        by_inn.setdefault((lic.get("licensee") or {}).get("inn"), []).append(lic)

    for inn in inns:
        try:
            store_org(con, inn, org_card(inn))
        except GisError as e:
            print(f"[warn] {inn}: карточка организации недоступна: {e}", file=sys.stderr)
        lics = by_inn.get(inn, [])
        n = 0
        for lic in lics:
            houses = houses_of_license(lic["guid"])
            store_houses(con, lic["guid"], inn, houses)
            n += len(houses)
        con.commit()
        name = lics[0]["licensee"]["name"] if lics else "лицензии в регионе нет"
        print(f"[ok]   {inn}  лицензий {len(lics)}, записей о домах {n}  {name[:50]}")

    if args.history:
        load_history(con, inns)

    if args.deep:
        # Licensees met only through house history: pull their houses too, so
        # transfers to and from them get dates.
        known = set(inns)
        extra = [r["guid"] for r in con.execute(
            """SELECT DISTINCT l.guid FROM gzhkh_house_licensees hl
               JOIN gzhkh_licenses l ON l.guid = hl.license_guid
               WHERE l.inn NOT IN (%s)""" % ",".join("?" * len(known)), list(known))]
        print(f"углублённо: домов ещё {len(extra)} лицензий")
        for g in extra:
            inn = con.execute("SELECT inn FROM gzhkh_licenses WHERE guid=?", (g,)).fetchone()["inn"]
            store_houses(con, g, inn, houses_of_license(g))
            con.commit()


def cmd_show(args):
    con = db.connect(args.db)
    org = con.execute("SELECT * FROM gzhkh_orgs WHERE inn=?", (args.inn,)).fetchone()
    lics = con.execute("SELECT * FROM gzhkh_licenses WHERE inn=?", (args.inn,)).fetchall()
    if not lics and not org:
        print("в базе нет — сначала: python3 gis_zhkh.py load --inn " + args.inn)
        return
    print(f"# ГИС ЖКХ — ИНН {args.inn}\n")
    if org:
        print(f"телефон в карточке организации: {pretty(org['phone'])}")
    for l in lics:
        print(f"\n## Лицензия {l['number']} ({l['erul_number'] or '—'}) — {l['status']}")
        print(f"с {l['start_date']} до {l['expiration_date'] or '—'}, домов {l['house_count']}")
        print(f"телефон {pretty(l['phone'])} · email {l['email'] or '—'}")
        for status, title in (("INCLUDED", "в управлении"), ("EXCLUDED", "исключены")):
            rows = con.execute(
                """SELECT address, inclusion_date, exclusion_date FROM gzhkh_houses
                   WHERE license_guid=? AND status=? ORDER BY inclusion_date""",
                (l["guid"], status)).fetchall()
            if rows:
                print(f"\n### {title} — {len(rows)}\n")
                for r in rows[:args.limit]:
                    span = f"{r['inclusion_date']} — {r['exclusion_date'] or 'н.в.'}"
                    print(f"- {span}  {r['address']}")
                if len(rows) > args.limit:
                    print(f"- … ещё {len(rows) - args.limit}")


def transfers(con, inns=None):
    """House moves between licensees: exclusion at A, inclusion at B within 120 days.

    Built only from licences whose houses were loaded, because only those carry
    dates. Licensees seen through history alone appear in `co_managers`.
    """
    where, params = "", []
    if inns:
        q = ",".join("?" * len(inns))
        where = f" AND (a.inn IN ({q}) OR b.inn IN ({q}))"
        params = list(inns) + list(inns)
    return con.execute(
        f"""SELECT a.inn AS from_inn, b.inn AS to_inn, a.house_guid, a.address,
                   a.exclusion_date, b.inclusion_date,
                   CAST(julianday(b.inclusion_date) - julianday(a.exclusion_date) AS INTEGER) gap
            FROM gzhkh_houses a
            JOIN gzhkh_houses b ON b.house_guid = a.house_guid AND b.inn <> a.inn
            WHERE a.status = 'EXCLUDED' AND a.exclusion_date IS NOT NULL
              AND b.status IN ('INCLUDED','EXCLUDED') AND b.inclusion_date <> ''
              AND julianday(b.inclusion_date) >= julianday(a.exclusion_date) - 31
              AND julianday(b.inclusion_date) <= julianday(a.exclusion_date) + 120
              AND a.house_guid <> '' {where}
            ORDER BY a.exclusion_date""", params).fetchall()


def co_managers(con, inns):
    """Other licensees that have managed any house of the given companies."""
    q = ",".join("?" * len(inns))
    return con.execute(
        f"""SELECT hl.inn, MAX(hl.name) name, COUNT(DISTINCT hl.house_guid) houses
            FROM gzhkh_house_licensees hl
            WHERE hl.house_guid IN (SELECT house_guid FROM gzhkh_houses WHERE inn IN ({q}))
              AND hl.inn NOT IN ({q})
            GROUP BY hl.inn ORDER BY houses DESC""", list(inns) + list(inns)).fetchall()


def short_name(name):
    if not name:
        return name
    for full, short in (("ОБЩЕСТВО С ОГРАНИЧЕННОЙ ОТВЕТСТВЕННОСТЬЮ", "ООО"),
                        ("УПРАВЛЯЮЩАЯ КОМПАНИЯ", "УК"),
                        ("АКЦИОНЕРНОЕ ОБЩЕСТВО", "АО")):
        name = name.replace(full, short)
    return " ".join(name.split())


def names(con):
    out = {r["inn"]: r["name_short"] for r in con.execute("SELECT inn, name_short FROM orgs")}
    for r in con.execute("SELECT inn, name FROM gzhkh_licenses"):
        out.setdefault(r["inn"], short_name(r["name"]))
    return out


def cmd_transfers(args):
    con = db.connect(args.db)
    inns = _targets(con, args) if (args.inn or args.case) else None
    nm = names(con)
    rows = transfers(con, inns)
    pairs = {}
    for r in rows:
        pairs.setdefault((r["from_inn"], r["to_inn"]), []).append(r)
    print("## Передачи домов между управляющими компаниями\n")
    if not pairs:
        print("_нет — загрузите дома: python3 gis_zhkh.py load --case <дело> --history --deep_")
    for (a, b), items in sorted(pairs.items(), key=lambda x: -len(x[1])):
        print(f"- **{(nm.get(a) or a)[:45]}** → **{(nm.get(b) or b)[:45]}** — домов {len(items)}")
        for r in items[:args.limit]:
            print(f"    {r['exclusion_date']} → {r['inclusion_date']}  {r['address']}")
    if inns:
        others = co_managers(con, inns)
        if others:
            print("\n## Кто ещё управлял домами этих компаний\n")
            for r in others[:args.limit]:
                print(f"- {short_name(r['name'] or r['inn'])[:60]} [{r['inn']}] — общих домов {r['houses']}")


def phones_of(con, inns=None):
    """Every phone known for each company: licence card and organisation card."""
    rows = con.execute("SELECT inn, phone FROM gzhkh_licenses WHERE phone IS NOT NULL "
                       "UNION SELECT inn, phone FROM gzhkh_orgs WHERE phone IS NOT NULL").fetchall()
    out = {}
    for r in rows:
        if inns and r["inn"] not in inns:
            continue
        p = norm_phone(r["phone"])
        if p:
            out.setdefault(r["inn"], set()).add(p)
    return out


def cmd_contacts(args):
    con = db.connect(args.db)
    inns = _targets(con, args) if (args.inn or args.case) else None
    nm = names(con)
    mine = phones_of(con, inns)

    print("## Общий телефон\n")
    by_phone = {}
    for inn, ps in mine.items():
        for p in ps:
            by_phone.setdefault(p, set()).add(inn)
    shared = {p: s for p, s in by_phone.items() if len(s) > 1}
    for p, s in shared.items():
        print(f"- {pretty(p)} — " + ", ".join((nm.get(i) or i)[:40] for i in sorted(s)))
    if not shared:
        print("_нет_")

    print("\n## Общий e-mail\n")
    emails = {}
    for r in con.execute("SELECT inn, lower(email) e FROM gzhkh_licenses WHERE email IS NOT NULL"):
        if not inns or r["inn"] in inns:
            emails.setdefault(r["e"], set()).add(r["inn"])
    hit = {e: s for e, s in emails.items() if len(s) > 1}
    for e, s in hit.items():
        print(f"- {e} — " + ", ".join((nm.get(i) or i)[:40] for i in sorted(s)))
    if not hit:
        print("_нет_")

    print("\n## Один блок городских номеров\n")
    blocks = {}
    for inn, ps in mine.items():
        for p in ps:
            b = phone_block(p)
            if b:
                blocks.setdefault(b, {}).setdefault(inn, p)
    multi = {b: m for b, m in blocks.items() if len(m) > 1}
    for b, m in sorted(multi.items()):
        print(f"- блок {b}… — " + ", ".join(
            f"{(nm.get(i) or i)[:30]} {pretty(p)[-9:]}{' ★' if is_vanity(p) else ''}"
            for i, p in sorted(m.items())))
    if not multi:
        print("_нет_")

    # Base rate from every licence in the region, so a "shared block" can be
    # judged against how common it is among unrelated companies.
    print("\n## Фон по региону (контрольная выборка)\n")
    region = phones_of(con)
    pool = {i: ps for i, ps in region.items() if not inns or i not in inns}
    total = len(pool)
    if total < 20:
        print("_мало данных для фона — загрузите регион: gis_zhkh.py load --region 26 …_")
        return
    vanity = sum(1 for ps in pool.values() if any(is_vanity(p) for p in ps))
    print(f"компаний с телефоном вне выборки: {total}")
    print(f"«зеркальный» номер (XX-YY-YY) у {vanity} из {total} — {100*vanity/total:.0f}%")
    for b in sorted(multi):
        n = sum(1 for ps in pool.values() if any(phone_block(p) == b for p in ps))
        print(f"блок {b}… встречается у {n} из {total} — {100*n/total:.0f}%")
    if inns:
        mine_v = sum(1 for ps in mine.values() if any(is_vanity(p) for p in ps))
        print(f"в выборке «зеркальный» номер у {mine_v} из {len(mine)}")


def main():
    ap = argparse.ArgumentParser(description="Housing licences, houses and contacts from GIS ZhKH")
    ap.add_argument("--db", default=db.DEFAULT_DB)
    sub = ap.add_subparsers(dest="cmd", required=True)

    def scope(p):
        p.add_argument("--inn", nargs="*")
        p.add_argument("--case")

    p = sub.add_parser("load", help="licences of a region, houses and history for chosen companies")
    scope(p)
    p.add_argument("--region", required=True, help="код региона по ОКАТО/ФИАС, например 77 — Москва")
    p.add_argument("--history", action="store_true",
                   help="по каждому дому найти все лицензии, которыми он покрывался")
    p.add_argument("--deep", action="store_true",
                   help="догрузить дома лицензиатов, найденных через историю (даёт даты передач)")
    p.set_defaults(func=cmd_load)

    p = sub.add_parser("show", help="licence, contacts and houses of one company")
    p.add_argument("inn")
    p.add_argument("--limit", type=int, default=30)
    p.set_defaults(func=cmd_show)

    p = sub.add_parser("transfers", help="houses moved between management companies")
    scope(p)
    p.add_argument("--limit", type=int, default=10)
    p.set_defaults(func=cmd_transfers)

    p = sub.add_parser("contacts", help="shared phones, e-mails, number blocks, with regional base rate")
    scope(p)
    p.set_defaults(func=cmd_contacts)

    args = ap.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
