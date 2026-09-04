"""Inspections from the ERKNM — the prosecutor's register of control measures.

For a housing management company this is the single most telling public record:
every inspection, warning and preventive visit by the regional housing
authority lands here. The FNS registries say nothing about how a company
actually behaves; this says exactly that.

The web search on proverki.gov.ru is behind a captcha, so this module uses the
portal's own open-data export instead — monthly XML dumps that the operator
publishes for exactly this purpose, free of charge and free of any captcha.

Dumps cover the whole country and run to hundreds of megabytes per month, so
parsing is streaming and keeps only the INNs you ask for. By default that is
every company already in your database.

Source: Генеральная прокуратура РФ, ФГИС ЕРКНМ, https://proverki.gov.ru
"""

import argparse
import json
import os
import sys
import time
import urllib.request
import zipfile

import db

API = "https://proverki.gov.ru/public/api/opendata"
UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")
CACHE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "cache")


class ErknmError(RuntimeError):
    pass


def _get_json(url):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=90) as r:
        return json.loads(r.read().decode("utf-8"))


def available(fz248=True):
    """Which years and months are published."""
    return _get_json(f"{API}/list?isFederalLaw248={'true' if fz248 else 'false'}")


def passport(year, month, fz248=True):
    return _get_json(f"{API}/{year}/{month}?isFederalLaw248={'true' if fz248 else 'false'}")


def download(year, month, fz248=True, force=False):
    p = passport(year, month, fz248)
    url = p.get("dataZipUrl")
    if not url:
        raise ErknmError(f"{year}-{month:02d}: в паспорте набора нет dataZipUrl")
    tag = "erknm" if fz248 else "erp"
    dest = os.path.join(CACHE, f"{tag}-{year}-{month:02d}.zip")
    if os.path.exists(dest) and not force:
        print(f"  кэш: {os.path.basename(dest)} ({os.path.getsize(dest)/2**20:.0f} МБ)")
        return dest
    os.makedirs(CACHE, exist_ok=True)
    print(f"  скачиваю {url}")
    _download_resumable(url, dest)
    return dest


def _download_resumable(url, dest, attempts=8):
    """These dumps run to ~300 MB and the server drops connections part-way,
    so every attempt resumes from what is already on disk via a Range header."""
    tmp = dest + ".part"
    total = None
    for attempt in range(1, attempts + 1):
        have = os.path.getsize(tmp) if os.path.exists(tmp) else 0
        headers = {"User-Agent": UA}
        if have:
            headers["Range"] = f"bytes={have}-"
        req = urllib.request.Request(url, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=600) as r:
                if total is None:
                    length = int(r.headers.get("Content-Length") or 0)
                    total = have + length if r.status == 206 else length
                mode = "ab" if r.status == 206 and have else "wb"
                if mode == "wb":
                    have = 0
                with open(tmp, mode) as f:
                    while True:
                        chunk = r.read(1 << 20)
                        if not chunk:
                            break
                        f.write(chunk)
                        have += len(chunk)
                        print(f"\r  {have/2**20:.0f}/{(total or 0)/2**20:.0f} МБ", end="", flush=True)
        except Exception as e:
            print(f"\n  обрыв на попытке {attempt}: {e}")
            time.sleep(min(30, 3 * attempt))
            continue
        if total and os.path.getsize(tmp) >= total:
            break
        print(f"\n  недокачано, повтор {attempt}/{attempts}")
        time.sleep(2)
    else:
        raise ErknmError(f"не удалось скачать за {attempts} попыток: {url}")
    print()
    if not zipfile.is_zipfile(tmp):
        raise ErknmError(f"скачанный файл не ZIP: {tmp}")
    os.replace(tmp, dest)
    return dest


def _local(tag):
    """Strip the XML namespace that the dump declares on every element."""
    return tag.rsplit("}", 1)[-1]


def _attr(parent, tag, name):
    for child in parent:
        if _local(child.tag) == tag:
            return child.get(name)
    return None


def _subject(el):
    for child in el:
        if _local(child.tag) == "SUBJECT":
            return child
    return None


def _record(el, subject, year, month):
    """Flatten one INSPECTION. Everything of interest lives in attributes."""
    obj = None
    for child in el:
        if _local(child.tag) == "OBJECT":
            obj = child
            break
    return {
        "number": el.get("ERPID"),
        "inn": subject.get("INN"),
        "ogrn": subject.get("OGRN"),
        "subject_name": subject.get("NAME"),
        "law": el.get("CLASSIFICATION"),
        "kind": el.get("TYPE_NAME"),
        "control_kind": _attr(el, "KIND_KNM", "VALUE"),
        "authority": _attr(el, "KNO_ORGANIZATION", "VALUE") or el.get("SUPERVISION_ID"),
        "prosecutor": _attr(el, "PROSECUTOR_OFFICE", "NAME"),
        "start": el.get("START_DATE"),
        "end": el.get("STOP_DATE"),
        "status": el.get("STATUS"),
        "purpose": _attr(el, "KIND_CONTROL", "VALUE"),
        "year": year,
        "month": month,
    }


def parse(zip_path, wanted, year, month, progress_every=200000):
    """Stream every INSPECTION out of the archive, keeping only wanted INNs."""
    out, seen = [], 0
    with zipfile.ZipFile(zip_path) as z:
        for name in [n for n in z.namelist() if n.lower().endswith(".xml")]:
            with z.open(name) as fh:
                for _, el in db.safe_iterparse(fh):
                    if _local(el.tag) != "INSPECTION":
                        continue
                    seen += 1
                    subject = _subject(el)
                    inn = subject.get("INN") if subject is not None else None
                    if inn and (not wanted or inn in wanted):
                        out.append(_record(el, subject, year, month))
                    el.clear()
                    if seen % progress_every == 0:
                        print(f"\r  просмотрено {seen}, найдено {len(out)}", end="", flush=True)
    print(f"\r  просмотрено {seen}, найдено {len(out)}          ")
    return out


def store(con, records):
    con.executemany(
        """INSERT OR REPLACE INTO inspections
           (number, inn, ogrn, subject_name, law, kind, control_kind, authority,
            prosecutor, start, "end", status, purpose, year, month)
           VALUES (:number,:inn,:ogrn,:subject_name,:law,:kind,:control_kind,:authority,
                   :prosecutor,:start,:end,:status,:purpose,:year,:month)""",
        records)
    con.commit()
    return len(records)


def target_inns(con, explicit=None, case=None):
    if explicit:
        return set(explicit)
    if case:
        rows = con.execute("SELECT inn FROM cases WHERE name=?", (case,)).fetchall()
    else:
        rows = con.execute("SELECT inn FROM orgs").fetchall()
    return {r["inn"] for r in rows}


def cmd_load(args):
    con = db.connect(args.db)
    wanted = target_inns(con, args.inn, args.case)
    if not wanted and not args.all_companies:
        print("в базе нет компаний — сначала crawl.py add, или задайте --inn", file=sys.stderr)
        sys.exit(1)
    print(f"фильтр: {len(wanted) if wanted else 'без фильтра, все компании'} ИНН")

    months = []
    if args.month:
        months = [(args.year, args.month)]
    else:
        pub = available(not args.erp)
        for y in sorted(pub["years"]):
            if args.year and y != args.year:
                continue
            if args.since and y < args.since:
                continue
            months += [(y, m) for m in pub["months"][str(y)]]

    total = 0
    for year, month in months:
        print(f"[{year}-{month:02d}]")
        try:
            path = download(year, month, fz248=not args.erp, force=args.force)
        except Exception as e:
            print(f"  пропуск: {e}", file=sys.stderr)
            continue
        records = parse(path, None if args.all_companies else wanted, year, month)
        total += store(con, records) if records else 0
        if not args.keep_zip:
            os.remove(path)
    print(f"сохранено записей: {total}")


def cmd_show(args):
    con = db.connect(args.db)
    rows = con.execute(
        """SELECT * FROM inspections WHERE inn=? ORDER BY year DESC, month DESC""",
        (args.inn,)).fetchall()
    if not rows:
        print("проверок в базе нет — сначала: python3 erknm.py load")
        return
    print(f"# Проверки: {rows[0]['subject_name'] or args.inn} — {len(rows)}\n")
    by_authority, by_year = {}, {}
    for r in rows:
        by_authority[r["authority"] or "—"] = by_authority.get(r["authority"] or "—", 0) + 1
        by_year[r["year"]] = by_year.get(r["year"], 0) + 1
    print("По годам: " + ", ".join(f"{y} — {n}" for y, n in sorted(by_year.items(), reverse=True)))
    print("\nОрганы контроля:")
    for a, n in sorted(by_authority.items(), key=lambda x: -x[1]):
        print(f"  {n:4}  {a}")
    print("\nПоследние:\n")
    for r in rows[:20]:
        print(f"- {r['year']}-{r['month']:02d} №{r['number'] or '—'} · {r['kind'] or '—'}"
              f" · {r['status'] or '—'}")
        if r["control_kind"]:
            print(f"    {r['control_kind']}")


def cmd_available(args):
    pub = available(not args.erp)
    print("годы:", ", ".join(map(str, pub["years"])))
    for y in sorted(pub["years"]):
        print(f"  {y}: месяцы {pub['months'][str(y)]}")


def main():
    ap = argparse.ArgumentParser(description="Inspections from the ERKNM open data")
    ap.add_argument("--db", default=db.DEFAULT_DB)
    ap.add_argument("--erp", action="store_true",
                    help="старый ЕРП (294/131/184-ФЗ) вместо ЕРКНМ (248-ФЗ)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("load", help="download monthly dumps and index the wanted INNs")
    p.add_argument("--year", type=int)
    p.add_argument("--month", type=int)
    p.add_argument("--since", type=int, help="с какого года грузить")
    p.add_argument("--inn", nargs="*", help="конкретные ИНН вместо всех из базы")
    p.add_argument("--case", help="только компании этого дела")
    p.add_argument("--all-companies", action="store_true",
                   help="без фильтра — сохранит проверки по всей стране, это очень много")
    p.add_argument("--keep-zip", action="store_true", help="не удалять архивы после разбора")
    p.add_argument("--force", action="store_true")
    p.set_defaults(func=cmd_load)

    p = sub.add_parser("show", help="inspections of one company")
    p.add_argument("inn")
    p.set_defaults(func=cmd_show)

    p = sub.add_parser("available", help="what the portal publishes")
    p.set_defaults(func=cmd_available)

    args = ap.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
