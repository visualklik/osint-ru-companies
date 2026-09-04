"""Client for the official FNS registry at egrul.nalog.ru.

Two levels of detail:

  search(query)  -> cheap. Name, OGRN, address, current director as a string.
  extract(inn)   -> full EGRUL PDF, parsed into founders (with their personal
                    INN and share), director, OKVED, capital, status.

The PDF is the only free route to the founders list; the search endpoint only
carries the director. Both are unauthenticated and captcha-free as of the last
verification, but they are rate limited, so pace requests and cache results.
"""

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.parse
import urllib.request
import http.cookiejar

BASE = "https://egrul.nalog.ru"
UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")
PAUSE = float(os.environ.get("EGRUL_PAUSE", "1.5"))


class EgrulError(RuntimeError):
    pass


def _opener():
    jar = http.cookiejar.CookieJar()
    return urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))


def _req(op, url, data=None, raw=False, timeout=60):
    headers = {
        "User-Agent": UA,
        "X-Requested-With": "XMLHttpRequest",
        "Referer": BASE + "/index.html",
    }
    body = urllib.parse.urlencode(data).encode() if data is not None else None
    req = urllib.request.Request(url, data=body, headers=headers)
    with op.open(req, timeout=timeout) as r:
        payload = r.read()
    if raw:
        return payload
    return json.loads(payload.decode("utf-8"))


def search(query, region=""):
    """Look up by INN, OGRN or name. Returns a list of row dicts."""
    op = _opener()
    start = _req(op, BASE + "/", data={
        "vyp3CaptchaToken": "",
        "page": "",
        "query": query,
        "region": region,
        "PreventChromeAutocomplete": "",
    })
    if start.get("captchaRequired"):
        raise EgrulError("egrul.nalog.ru requires a captcha — slow down or change IP")
    token = start.get("t")
    if not token:
        raise EgrulError(f"no search token in response: {start}")

    for _ in range(10):
        time.sleep(PAUSE)
        res = _req(op, f"{BASE}/search-result/{token}")
        if "rows" in res:
            return res["rows"], op
    raise EgrulError("search timed out")


def download_pdf(op, row_token, dest):
    v = _req(op, f"{BASE}/vyp-request/{row_token}")
    if v.get("captchaRequired"):
        raise EgrulError("captcha required for the extract — slow down or change IP")
    vt = v.get("t")
    if not vt:
        raise EgrulError(f"no extract token: {v}")
    for _ in range(20):
        time.sleep(PAUSE)
        st = _req(op, f"{BASE}/vyp-status/{vt}")
        if st.get("status") == "ready":
            break
        if st.get("status") == "error":
            raise EgrulError(f"extract generation failed: {st}")
    else:
        raise EgrulError("extract generation timed out")
    pdf = _req(op, f"{BASE}/vyp-download/{vt}", raw=True, timeout=120)
    if not pdf.startswith(b"%PDF"):
        raise EgrulError("downloaded file is not a PDF")
    with open(dest, "wb") as f:
        f.write(pdf)
    return dest


# --------------------------------------------------------------------------
# PDF parsing
# --------------------------------------------------------------------------

# The extract is a three-column table: number | label | value. Both label and
# value wrap onto following lines, and the wrap is only distinguishable by the
# column it starts in, so the parser tracks column offsets rather than shapes.
FIELD_RE = re.compile(r"^(\s{0,8})(\d{1,4})\s+(\S.*?)\s{2,}(\S.*?)\s*$")
CONT_RE = re.compile(r"^\s{2,}(\S.*?)\s{2,}(\S.*?)\s*$")
PAGE_NOISE = re.compile(
    r"^(Выписка из ЕГРЮЛ|Сведения с сайта|\d{2}\.\d{2}\.\d{4}\s+\d{2}:\d{2}"
    r"|.*Страница \d+ из \d+|№ п/п|1\s+2\s+3$)"
)

# Section header text -> key. Matched as a prefix of a non-numbered line.
SECTION_MARKERS = [
    ("Наименование", "name"),
    ("Место нахождения и адрес", "address"),
    ("Сведения о регистрации в качестве страхователя", "insurer"),
    ("Сведения о регистрации", "registration"),
    ("Сведения о регистрирующем органе", "reg_authority"),
    ("Сведения о состоянии юридического лица", "status"),
    ("Сведения о прекращении", "termination"),
    ("Сведения о лице, имеющем право без доверенности", "director"),
    ("Сведения об управляющей организации", "manager_org"),
    ("Сведения об уставном капитале", "capital"),
    ("Сведения об участниках / учредителях", "founders"),
    ("Сведения о держателе реестра акционеров", "registrar"),
    ("Сведения об учете в налоговом органе", "tax"),
    ("Сведения о видах экономической деятельности", "okved"),
    ("Сведения о лицензиях", "licenses"),
    ("Сведения о филиалах", "branches"),
    ("Сведения о представительствах", "representations"),
    ("Сведения о правопредшественник", "predecessors"),
    ("Сведения о правопреемник", "successors"),
    ("Сведения о специальных налоговых режимах", "tax_regimes"),
    ("Сведения о среднесписочной численности", "headcount"),
]

# Everything from here on is the change log, which repeats every field ever
# recorded and would otherwise swamp the current-state parse.
HISTORY_MARKER = "Сведения о записях, внесенных в Единый государственный реестр"


def pdf_to_text(pdf_path):
    if not shutil.which("pdftotext"):
        raise EgrulError("pdftotext not found — install poppler-utils")
    out = pdf_path + ".txt"
    subprocess.run(["pdftotext", "-layout", pdf_path, out], check=True)
    with open(out, encoding="utf-8", errors="replace") as f:
        return f.read()


def _match_section(stripped):
    for marker, key in SECTION_MARKERS:
        if stripped.startswith(marker):
            return key
    return None


def parse_fields(text):
    """Parse the extract body into [(section, num, label, value)].

    Wrapped values are rejoined by comparing a line's indent to the column the
    open field's value started in; anything at or right of that column belongs
    to the value, anything left of it is a wrapped label or a sub-field.
    """
    rows = []
    section = "header"
    open_field = None      # [section, num, label, value]
    label_col = value_col = 0
    skip_until_field = False

    for raw in text.splitlines():
        stripped = raw.strip()
        if not stripped or PAGE_NOISE.match(stripped):
            continue

        m = FIELD_RE.match(raw)
        if m:
            skip_until_field = False
            open_field = [section, int(m.group(2)), m.group(3).strip(), m.group(4).strip()]
            rows.append(open_field)
            label_col, value_col = m.start(3), m.start(4)
            continue

        if HISTORY_MARKER in stripped:
            break

        key = _match_section(stripped)
        if key:
            section = key
            open_field = None
            skip_until_field = True   # swallow the header's own wrapped lines
            continue
        if skip_until_field or open_field is None:
            continue

        indent = len(raw) - len(raw.lstrip())
        sub = CONT_RE.match(raw)
        if sub and sub.start(1) < value_col - 5 and sub.start(2) >= value_col - 6:
            # Two blocks in the label and value columns: either a further
            # label/value pair under the same number (Имя / Отчество), or a
            # label that wrapped and dragged its value along. Real labels are
            # capitalised, so a lowercase start means it is a wrap.
            head, tail = sub.group(1).strip(), sub.group(2).strip()
            if head[:1].islower():
                open_field[2] = (open_field[2] + " " + head).strip()
                open_field[3] = (open_field[3] + " " + tail).strip()
            else:
                open_field = [section, open_field[1], head, tail]
                rows.append(open_field)
        elif indent >= value_col - 4:
            open_field[3] = (open_field[3] + " " + stripped).strip()
        else:
            open_field[2] = (open_field[2] + " " + stripped).strip()

    return rows


def _num(s):
    if not s:
        return None
    s = s.replace(" ", "").replace(" ", "").replace(",", ".")
    try:
        return float(s)
    except ValueError:
        return None


def _collect_people(fields):
    """Group a founders/director section into person or company records.

    A new record starts at 'ГРН и дата внесения в ЕГРЮЛ сведений о данном лице'
    or when a Фамилия/наименование appears after the previous record is filled.
    """
    records, cur = [], {}

    def flush():
        if cur and (cur.get("fio") or cur.get("name") or cur.get("inn")):
            records.append(dict(cur))
        cur.clear()

    for _, label, value in fields:
        L = label.lower()
        if "сведений о данном лице" in L:
            flush()
        elif L.startswith("фамилия"):
            if cur.get("last"):
                flush()
            cur["last"] = value
        elif L.startswith("имя"):
            cur["first"] = value
        elif L.startswith("отчество"):
            cur["middle"] = value
        elif L.startswith("инн"):
            if cur.get("inn"):
                flush()
            cur["inn"] = value
        elif L.startswith("огрн"):
            if cur.get("ogrn"):
                flush()
            cur["ogrn"] = value
        elif "полное наименование" in L or L.startswith("наименование"):
            cur["name"] = value
        elif "должность" in L:
            cur["post"] = value
        elif "размер доли (в процентах)" in L:
            cur["share_pct"] = _num(value)
        elif "номинальная стоимость доли" in L:
            cur["share_rub"] = _num(value)
        elif "размер доли в процентах" in L:
            cur["share_pct"] = _num(value)
        elif "грн и дата внесения" in L and "grn" not in cur:
            cur["grn"] = value
        if {"last", "first"} <= cur.keys():
            cur["fio"] = " ".join(
                x for x in (cur.get("last"), cur.get("first"), cur.get("middle")) if x
            )
    flush()
    return records


def parse_extract(text, inn=None):
    rows = parse_fields(text)
    by_section = {}
    for section, num, label, value in rows:
        by_section.setdefault(section, []).append((num, label, value))

    def find(section, *needles, default=None):
        for _, label, value in by_section.get(section, []):
            low = label.lower()
            if all(n in low for n in needles):
                return value
        return default

    org = {
        "inn": inn or find("tax", "инн юридического лица"),
        "ogrn": find("registration", "огрн"),
        "name_full": find("name", "полное наименование"),
        "name_short": find("name", "сокращенное наименование"),
        "status": find("status", "состояние") or find("termination", "способ прекращения"),
        "reg_date": find("registration", "дата регистрации")
                    or find("registration", "дата присвоения огрн"),
        "kpp": find("tax", "кпп"),
        "capital_kind": find("capital", "вид"),
        "capital": _num(find("capital", "размер")),
        "address": find("address", "адрес юридического лица")
                   or find("address", "место нахождения"),
        "location": find("address", "место нахождения"),
        "director": None,
        "directors": [],
        "founders": [],
        "founder_orgs": [],
        "manager_org": None,
    }

    # A managing organisation is filed under the same "acts without a power of
    # attorney" heading as a human director, but carries a name and OGRN
    # instead of a surname — that absence is what tells the two apart.
    for rec in _collect_people(by_section.get("director", [])):
        if rec.get("fio"):
            org["directors"].append(rec)
        elif rec.get("inn") or rec.get("ogrn"):
            org["manager_org"] = org["manager_org"] or rec
    if org["directors"]:
        org["director"] = org["directors"][0]

    for rec in _collect_people(by_section.get("manager_org", [])):
        if rec.get("inn") or rec.get("ogrn"):
            org["manager_org"] = org["manager_org"] or rec

    for rec in _collect_people(by_section.get("founders", [])):
        if rec.get("fio"):
            org["founders"].append(rec)
        elif rec.get("ogrn") or rec.get("name") or rec.get("inn"):
            org["founder_orgs"].append(rec)

    okveds = []
    for _, label, value in by_section.get("okved", []):
        if "код и наименование" in label.lower():
            okveds.append(value)
    org["okved_all"] = okveds
    org["okved"] = okveds[0] if okveds else None

    org["licenses"] = [v for _, l, v in by_section.get("licenses", [])
                       if "вид" in l.lower() and "деятельност" in l.lower()]
    org["branches"] = [v for _, l, v in by_section.get("branches", [])
                       if "наименование" in l.lower()]
    org["headcount"] = _num(find("headcount", "среднесписочная численность"))
    org["tax_regimes"] = [v for _, l, v in by_section.get("tax_regimes", [])]
    # The status section only appears once something has happened to the entity;
    # its absence is how the extract says "still trading".
    if not org["status"]:
        org["status"] = "действующее"
    org["fields"] = rows
    return org


def extract(inn, keep_pdf=None):
    """Full pipeline: search by INN, download the extract, parse it."""
    rows, op = search(inn)
    if not rows:
        raise EgrulError(f"nothing found for {inn}")
    row = rows[0]
    tmpdir = keep_pdf or tempfile.mkdtemp(prefix="egrul-")
    os.makedirs(tmpdir, exist_ok=True)
    pdf = os.path.join(tmpdir, f"{inn}.pdf")
    download_pdf(op, row["t"], pdf)
    org = parse_extract(pdf_to_text(pdf), inn=row.get("i") or inn)
    org.setdefault("name_short", row.get("c"))
    org.setdefault("name_full", row.get("n"))
    org["ogrn"] = org.get("ogrn") or row.get("o")
    org["kpp"] = row.get("p")
    org["region"] = row.get("rn")
    org["search_row"] = row
    org["pdf_path"] = pdf if keep_pdf else None
    if not keep_pdf:
        shutil.rmtree(tmpdir, ignore_errors=True)
    return org


def main():
    ap = argparse.ArgumentParser(description="EGRUL lookup via egrul.nalog.ru")
    ap.add_argument("query", help="INN, OGRN or company name")
    ap.add_argument("--search-only", action="store_true", help="skip the PDF extract")
    ap.add_argument("--keep-pdf", metavar="DIR", help="save the extract PDF into DIR")
    args = ap.parse_args()

    if args.search_only:
        rows, _ = search(args.query)
        print(json.dumps(rows, ensure_ascii=False, indent=2))
        return
    try:
        print(json.dumps(extract(args.query, keep_pdf=args.keep_pdf), ensure_ascii=False, indent=2))
    except EgrulError as e:
        print(f"error: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
