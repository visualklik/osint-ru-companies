"""Reverse lookup person -> companies, across two aggregators.

The FNS registries have no reverse index: you can ask "who owns this company"
but never "what else does this person own". Two public aggregators publish that
index and both key people by personal INN, which is what lets a candidate found
by surname be pinned to the right individual instead of a namesake.

  rusprofile  addressed directly by a slug built from the name and INN, so no
              search step and no rate limit to trip over. Primary.
  list-org    richer (shares, phones, per-company addresses) but throttles hard
              and then blocks the address outright. Secondary.

Everything here is a lead, not registry truth — aggregators lag the register by
months or years. Confirm each company with `crawl.py add`, and treat a mismatch
as information: it usually means the person left, and when.
"""

import argparse
import html
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

import db

UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")

TRANSLIT = {
    "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ё": "e",
    "ж": "zh", "з": "z", "и": "i", "й": "y", "к": "k", "л": "l", "м": "m",
    "н": "n", "о": "o", "п": "p", "р": "r", "с": "s", "т": "t", "у": "u",
    "ф": "f", "х": "kh", "ц": "ts", "ч": "ch", "ш": "sh", "щ": "shch",
    "ъ": "", "ы": "y", "ь": "", "э": "e", "ю": "yu", "я": "ya",
}


class LookupError_(RuntimeError):
    pass


class Throttled(LookupError_):
    pass


def _get(url, timeout=45):
    req = urllib.request.Request(url, headers={
        "User-Agent": UA,
        "Accept": "text/html,application/xhtml+xml",
        "Accept-Language": "ru-RU,ru;q=0.9",
    })
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", errors="replace")


def translit(word):
    return "".join(TRANSLIT.get(c, c) for c in word.lower())


def rusprofile_slug(fio, inn):
    """rusprofile addresses people as <surname>-<initials>-<inn>."""
    parts = [p for p in re.split(r"\s+", (fio or "").strip()) if p]
    if len(parts) < 2:
        raise LookupError_(f"нужны фамилия и имя: «{fio}»")
    surname = translit(parts[0])
    initials = "".join(translit(p[0]) for p in parts[1:3])
    return f"{surname}-{initials}-{inn}"


def _strip(page):
    t = re.sub(r"<script.*?</script>", " ", page, flags=re.S)
    t = re.sub(r"<style.*?</style>", " ", t, flags=re.S)
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", t)))


def from_rusprofile(fio, inn):
    slug = rusprofile_slug(fio, inn)
    url = f"https://www.rusprofile.ru/person/{slug}"
    try:
        page = _get(url)
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None
        raise
    if inn not in page:
        return None                      # slug resolved to somebody else

    text = _strip(page)
    companies = []
    seen = set()
    for _, label in re.findall(r'href="(/id/\d+)"[^>]*>(.*?)</a>', page, re.S):
        name = html.unescape(re.sub(r"<[^>]+>", "", label)).strip()
        # The page repeats a company between its summary and history blocks,
        # differing only in case, so names are compared case-insensitively.
        key = re.sub(r"[^А-ЯA-Z0-9]", "", name.upper().replace("Ё", "Е"))
        if len(name) < 4 or key in seen:
            continue
        seen.add(key)
        # The card for each company follows its heading in reading order.
        at = text.find(name)
        card = text[at:at + 900] if at >= 0 else ""
        c_inn = re.search(r"ИНН:\s*(\d{10})", card)
        companies.append({
            "name": name,
            "inn": c_inn.group(1) if c_inn else None,
            "ogrn": (re.search(r"ОГРН:\s*(\d{13})", card) or [None, None])[1]
                    if re.search(r"ОГРН:\s*(\d{13})", card) else None,
            "reg": (re.search(r"Дата регистрации:\s*([\d.]+)", card) or [None, None])[1]
                   if re.search(r"Дата регистрации:\s*([\d.]+)", card) else None,
            "revenue": (re.search(r"Выручка в \d{4} г\.\s*([\d\s,.]+(?:млн|млрд|тыс)?\s*руб)", card)
                        or [None, None])[1]
                       if re.search(r"Выручка в \d{4} г\.\s*([\d\s,.]+(?:млн|млрд|тыс)?\s*руб)", card) else None,
            "address": (re.search(r"(\d{6},\s*[^И]{10,120}?)\s*ИНН:", card) or [None, None])[1]
                       if re.search(r"(\d{6},\s*[^И]{10,120}?)\s*ИНН:", card) else None,
        })

    counts = {}
    for role, pat in (("руководитель", r"Руководитель\s+(?:В настоящее время\s+)?.{0,80}?(\d+)\s+организаци"),
                      ("учредитель", r"Учредитель\s+(?:В настоящее время\s+)?.{0,80}?(\d+)\s+организаци")):
        m = re.search(pat, text)
        if m:
            counts[role] = int(m.group(1))
    if "Нет организаций" in text and "руководитель" not in counts:
        counts["руководитель"] = 0

    events = re.findall(r"(\d{2}\.\d{2}\.\d{4})\s+(Лицо больше не является [^.0-9]{0,90})", text)
    return {"source": "rusprofile", "url": url, "fio": fio, "inn": inn,
            "companies": companies, "counts": counts,
            "past_events": [{"date": d, "what": w.strip()} for d, w in events[:12]]}


def from_listorg(fio, inn):
    base = "https://www.list-org.com"
    page = _get(f"{base}/search?type=boss&val={urllib.parse.quote(fio)}", timeout=30)
    if "Найдено" not in page and "/man/" not in page:
        raise Throttled("list-org вернул страницу без блока «Найдено» — лимит или блокировка адреса")
    found = re.findall(r"<a href='/man/(\d+)'>([^<]+)</a>\s*\(инн:\s*(\d+)\)", page)
    match = next((f for f in found if f[2] == inn), None)
    if not match:
        return None
    time.sleep(2)
    card = _get(f"{base}/man/{match[0]}", timeout=30)
    if "ИНН" not in card:
        raise Throttled("list-org отдал пустую карточку — лимит")
    out = {"source": "list-org", "url": f"{base}/man/{match[0]}",
           "fio": fio, "inn": inn, "companies": [], "counts": {}, "past_events": []}
    for heading, role in (("Руководитель организаций", "руководитель"),
                          ("Учредитель организаций", "учредитель")):
        block = re.search(re.escape(heading) + r":</div></h6><div>(.*?)</div></div>", card, re.S)
        if not block:
            continue
        items = re.findall(r"<p><label>.*?</label></p>", block.group(1), re.S)
        out["counts"][role] = len(items)
        for chunk in items:
            link = re.search(r"<a href='/company/\d+'>([^<]+)</a>", chunk)
            if not link:
                continue
            def grab(p):
                m = re.search(p, chunk)
                return html.unescape(m.group(1)).strip() if m else None
            out["companies"].append({
                "name": html.unescape(link.group(1)).strip(),
                "inn": grab(r"<i>инн</i>:\s*(\d+)"),
                "role": role,
                "share": grab(r"<i>доля</i>:\s*([^<]+)"),
                "phones": grab(r"<i>телефон</i>:\s*([^<]+)"),
                "address": grab(r"<i>адрес</i>:\s*([^<]+)"),
                "active": "не действующее" not in chunk,
            })
    return out


def lookup(fio, inn, sources=("rusprofile", "list-org")):
    errors = []
    for name in sources:
        fn = from_rusprofile if name == "rusprofile" else from_listorg
        try:
            data = fn(fio, inn)
        except Throttled as e:
            errors.append(f"{name}: {e}")
            continue
        except Exception as e:
            errors.append(f"{name}: {type(e).__name__} {e}")
            continue
        if data:
            data["errors"] = errors
            return data
        errors.append(f"{name}: точного совпадения по ИНН нет")
    return {"fio": fio, "inn": inn, "companies": [], "counts": {},
            "past_events": [], "errors": errors, "source": None}


def store(con, data):
    if not data.get("companies"):
        return 0
    n = 0
    for c in data["companies"]:
        if not c.get("inn"):
            continue
        con.execute(
            """INSERT OR REPLACE INTO person_orgs
               (pid, inn, name, role, share, status, address, phones, okved, source)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (data["inn"], c["inn"], c["name"], c.get("role") or "связь",
             c.get("share"), "действующее" if c.get("active", True) else "не действующее",
             c.get("address"), c.get("phones"), None, data.get("source")))
        n += 1
    con.commit()
    return n


def cmd_person(args):
    con = db.connect(args.db)
    fio = args.fio
    if not fio and args.inn:
        row = con.execute("SELECT fio FROM persons WHERE inn=?", (args.inn,)).fetchone()
        fio = row["fio"] if row else None
    if not fio or not args.inn:
        print("нужны ФИО и --inn (ИНН отсекает однофамильцев)", file=sys.stderr)
        sys.exit(1)

    sources = (args.source,) if args.source else ("rusprofile", "list-org")
    data = lookup(fio, args.inn, sources)
    if not data.get("source"):
        print(f"{fio}: не найдено")
        for e in data["errors"]:
            print(f"  {e}")
        sys.exit(2)

    print(f"# {data['fio']} (ИНН {data['inn']})")
    print(f"источник: {data['source']} · {data['url']}")
    if data["counts"]:
        print("сейчас: " + ", ".join(f"{k} — {v}" for k, v in data["counts"].items()))
    print(f"\n## Компании — {len(data['companies'])}\n")
    for c in data["companies"]:
        bits = [x for x in (c.get("role"), c.get("share"), c.get("revenue")) if x]
        print(f"- **{c['name']}** [{c.get('inn') or '—'}]" + (f" · {', '.join(bits)}" if bits else ""))
        if c.get("address"):
            print(f"    {c['address']}")
        if c.get("phones"):
            print(f"    тел: {c['phones']}")
    if data["past_events"]:
        print("\n## Выходы из состава\n")
        for e in data["past_events"]:
            print(f"- {e['date']} — {e['what']}")
    for e in data["errors"]:
        print(f"\n_{e}_")
    print(f"\nсохранено связей: {store(con, data)}")


def cmd_shared(args):
    con = db.connect(args.db)
    print("## Компании, общие для нескольких фигурантов\n")
    rows = con.execute(
        """SELECT po.inn, MIN(po.name) name, COUNT(DISTINCT po.pid) n,
                  GROUP_CONCAT(DISTINCT p.fio) who
           FROM person_orgs po LEFT JOIN persons p ON p.inn = po.pid
           GROUP BY po.inn HAVING n > 1 ORDER BY n DESC""").fetchall()
    for r in rows:
        print(f"- **{r['name']}** [{r['inn']}] — {r['n']}: {r['who']}")
    if not rows:
        print("_нет_")

    print("\n## Компании фигурантов, ещё не загруженные в базу\n")
    rows = con.execute(
        """SELECT DISTINCT po.inn, po.name, p.fio
           FROM person_orgs po LEFT JOIN persons p ON p.inn = po.pid
           WHERE po.inn NOT IN (SELECT inn FROM orgs)""").fetchall()
    for r in rows:
        print(f"- {r['name']} [{r['inn']}] — через {r['fio'] or '—'}")
    if not rows:
        print("_все загружены_")


def main():
    ap = argparse.ArgumentParser(description="Reverse lookup person -> companies")
    ap.add_argument("--db", default=db.DEFAULT_DB)
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("person", help="companies of one individual")
    p.add_argument("fio", nargs="?", help="ФАМИЛИЯ ИМЯ ОТЧЕСТВО")
    p.add_argument("--inn", required=True, help="личный ИНН — обязателен, отсекает однофамильцев")
    p.add_argument("--source", choices=("rusprofile", "list-org"))
    p.set_defaults(func=cmd_person)

    p = sub.add_parser("shared", help="intersections and companies not yet crawled")
    p.set_defaults(func=cmd_shared)

    args = ap.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
