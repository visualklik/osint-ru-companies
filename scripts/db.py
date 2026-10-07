"""SQLite store for accumulated corporate intelligence.

One database file holds every organisation, person, ownership edge and
financial line ever fetched, so repeat investigations are free and
cross-case intersections become a SQL query.
"""

import json
import os
import sqlite3
import time

DEFAULT_DB = os.environ.get(
    "RU_CORP_DB",
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "corp.db"),
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS orgs (
    inn         TEXT PRIMARY KEY,
    ogrn        TEXT,
    kpp         TEXT,
    name_short  TEXT,
    name_full   TEXT,
    status      TEXT,
    reg_date    TEXT,
    address     TEXT,
    address_key TEXT,
    okved       TEXT,
    okved_name  TEXT,
    capital     REAL,
    region      TEXT,
    fetched_at  INTEGER,
    raw         TEXT
);
CREATE INDEX IF NOT EXISTS ix_orgs_addr ON orgs(address_key);
CREATE INDEX IF NOT EXISTS ix_orgs_name ON orgs(name_short);

CREATE TABLE IF NOT EXISTS persons (
    pid      TEXT PRIMARY KEY,   -- inn if known, else 'fio:<normalised>'
    inn      TEXT,
    fio      TEXT,
    fio_key  TEXT
);
CREATE INDEX IF NOT EXISTS ix_persons_fio ON persons(fio_key);

CREATE TABLE IF NOT EXISTS roles (
    org_inn TEXT,
    pid     TEXT,
    role    TEXT,
    grn     TEXT,
    since   TEXT,
    PRIMARY KEY (org_inn, pid, role)
);
CREATE INDEX IF NOT EXISTS ix_roles_pid ON roles(pid);

-- owner is either a person (pid) or another legal entity (owner_inn)
-- pid and owner_inn use '' rather than NULL so the primary key actually dedupes.
-- kind separates equity ('owner') from the management contract ('manager'),
-- which is control without a share.
CREATE TABLE IF NOT EXISTS ownership (
    org_inn    TEXT NOT NULL,
    pid        TEXT NOT NULL DEFAULT '',
    owner_inn  TEXT NOT NULL DEFAULT '',
    kind       TEXT NOT NULL DEFAULT 'owner',
    share_pct  REAL,
    share_rub  REAL,
    grn        TEXT,
    PRIMARY KEY (org_inn, pid, owner_inn, kind)
);
CREATE INDEX IF NOT EXISTS ix_own_pid ON ownership(pid);
CREATE INDEX IF NOT EXISTS ix_own_owner ON ownership(owner_inn);

CREATE TABLE IF NOT EXISTS financials (
    inn   TEXT,
    year  INTEGER,
    code  TEXT,          -- form line code, e.g. 2110 revenue, 2400 net profit
    value REAL,
    PRIMARY KEY (inn, year, code)
);

CREATE TABLE IF NOT EXISTS crawl_queue (
    inn     TEXT PRIMARY KEY,
    depth   INTEGER,
    state   TEXT,        -- pending | done | error
    note    TEXT,
    seen_at INTEGER
);

CREATE TABLE IF NOT EXISTS cases (
    name    TEXT,
    inn     TEXT,
    added_at INTEGER,
    PRIMARY KEY (name, inn)
);

-- Arbitration (kad.arbitr.ru). Parties are stored by name because the registry
-- only carries an INN for some of them.
CREATE TABLE IF NOT EXISTS court_cases (
    case_id TEXT PRIMARY KEY,
    number  TEXT,
    date    TEXT,
    court   TEXT,
    judge   TEXT,
    type    TEXT,
    url     TEXT
);
CREATE TABLE IF NOT EXISTS court_parties (
    case_id TEXT NOT NULL,
    inn     TEXT NOT NULL DEFAULT '',
    name    TEXT NOT NULL,
    side    TEXT NOT NULL,      -- истец | ответчик | иное
    PRIMARY KEY (case_id, name, side)
);
CREATE INDEX IF NOT EXISTS ix_cp_inn ON court_parties(inn);
CREATE INDEX IF NOT EXISTS ix_cp_name ON court_parties(name);

-- Bulk FNS open data, keyed by the statement date the dataset carries.
CREATE TABLE IF NOT EXISTS headcount (
    inn TEXT, date TEXT, count INTEGER, PRIMARY KEY (inn, date)
);
CREATE TABLE IF NOT EXISTS tax_debt (
    inn TEXT, date TEXT, tax TEXT, debt REAL, penalty REAL, fine REAL,
    PRIMARY KEY (inn, date, tax)
);
CREATE TABLE IF NOT EXISTS revexp (
    inn TEXT, date TEXT, income REAL, expense REAL, PRIMARY KEY (inn, date)
);
CREATE TABLE IF NOT EXISTS tax_regime (
    inn TEXT, date TEXT, regimes TEXT, PRIMARY KEY (inn, date)
);
CREATE TABLE IF NOT EXISTS opendata_loads (
    dataset TEXT PRIMARY KEY, url TEXT, rows INTEGER, loaded_at INTEGER
);

-- Reverse index person -> companies, from a third-party aggregator. Keyed by
-- the person's own INN. Leads, not registry truth: confirm against EGRUL.
CREATE TABLE IF NOT EXISTS person_orgs (
    pid     TEXT NOT NULL,     -- personal INN
    inn     TEXT NOT NULL,     -- company INN
    name    TEXT,
    role    TEXT NOT NULL,     -- руководитель | учредитель
    share   TEXT,
    status  TEXT,
    address TEXT,
    phones  TEXT,
    okved   TEXT,
    source  TEXT,
    PRIMARY KEY (pid, inn, role)
);
CREATE INDEX IF NOT EXISTS ix_po_inn ON person_orgs(inn);

-- Inspections from the ERKNM (Генеральная прокуратура). One row per control
-- measure; the register number is unique, so re-loading a month is idempotent.
CREATE TABLE IF NOT EXISTS inspections (
    number       TEXT,
    inn          TEXT NOT NULL,
    ogrn         TEXT,
    subject_name TEXT,
    law          TEXT,
    kind         TEXT,
    control_kind TEXT,
    authority    TEXT,
    prosecutor   TEXT,
    start        TEXT,
    "end"        TEXT,
    status       TEXT,
    purpose      TEXT,
    year         INTEGER,
    month        INTEGER,
    PRIMARY KEY (inn, number, year, month)
);
CREATE INDEX IF NOT EXISTS ix_insp_inn ON inspections(inn);

-- Workplace declarations (Роструд, реестр СОУТ). One row per declared position.
-- address here is the real place of business, which often differs from EGRUL.
CREATE TABLE IF NOT EXISTS sout (
    inn         TEXT NOT NULL,
    decl_date   TEXT NOT NULL,
    address     TEXT,
    address_key TEXT,
    wp_number   TEXT NOT NULL DEFAULT '',
    position    TEXT NOT NULL,
    headcount   INTEGER,
    sout_org    TEXT,
    valid_until TEXT,
    PRIMARY KEY (inn, decl_date, position, wp_number)
);
CREATE INDEX IF NOT EXISTS ix_sout_inn ON sout(inn);
CREATE INDEX IF NOT EXISTS ix_sout_addr ON sout(address_key);

-- GIS ZhKH: housing management licences, houses under them with dates, and
-- for each house every licence that ever covered it. Dates on house records are
-- what make transfers between companies visible.
CREATE TABLE IF NOT EXISTS gzhkh_licenses (
    guid TEXT PRIMARY KEY, number TEXT, erul_number TEXT, inn TEXT, ogrn TEXT,
    name TEXT, status TEXT, start_date TEXT, expiration_date TEXT,
    house_count INTEGER, phone TEXT, email TEXT, region TEXT, fetched_at INTEGER
);
CREATE INDEX IF NOT EXISTS ix_gl_inn ON gzhkh_licenses(inn);
CREATE TABLE IF NOT EXISTS gzhkh_houses (
    license_guid   TEXT NOT NULL,
    inn            TEXT,
    house_guid     TEXT NOT NULL DEFAULT '',
    address        TEXT,
    address_key    TEXT,
    status         TEXT NOT NULL,      -- INCLUDED | EXCLUDED | CANCELED
    inclusion_date TEXT NOT NULL DEFAULT '',
    exclusion_date TEXT,
    PRIMARY KEY (license_guid, house_guid, status, inclusion_date)
);
CREATE INDEX IF NOT EXISTS ix_gh_house ON gzhkh_houses(house_guid);
CREATE INDEX IF NOT EXISTS ix_gh_inn ON gzhkh_houses(inn);
CREATE TABLE IF NOT EXISTS gzhkh_house_licensees (
    house_guid TEXT NOT NULL, license_guid TEXT NOT NULL, inn TEXT, name TEXT,
    license_status TEXT, PRIMARY KEY (house_guid, license_guid)
);
CREATE TABLE IF NOT EXISTS gzhkh_orgs (
    inn TEXT PRIMARY KEY, guid TEXT, phone TEXT, email TEXT, fact_address TEXT,
    fetched_at INTEGER
);

-- Register of persons disqualified by a court (FNS open data). There is no
-- personal INN in it, so matches against our people are made by name and
-- graded by whether the organisation also matches.
CREATE TABLE IF NOT EXISTS disqualified (
    record_no TEXT PRIMARY KEY, fio TEXT, fio_key TEXT, birth_date TEXT,
    birth_place TEXT, org_name TEXT, org_inn TEXT, position TEXT, article TEXT,
    authority TEXT, term TEXT, start_date TEXT, end_date TEXT, loaded_at INTEGER
);
CREATE INDEX IF NOT EXISTS ix_disq_fio ON disqualified(fio_key);

-- GIS ZhKH register of disqualified managers of housing companies (Housing Code
-- art. 195). Unlike the FNS register it carries the organisation's INN, and
-- it keeps excluded records, so past disqualifications stay visible.
CREATE TABLE IF NOT EXISTS gzhkh_disqualified (
    guid TEXT PRIMARY KEY, registry_number TEXT, status TEXT, fio TEXT,
    fio_key TEXT, position TEXT, birth_date TEXT, birth_place TEXT,
    org_name TEXT, org_inn TEXT, org_ogrn TEXT, court TEXT, case_number TEXT,
    decision_date TEXT, included TEXT, excluded TEXT, loaded_at INTEGER
);
CREATE INDEX IF NOT EXISTS ix_gd_fio ON gzhkh_disqualified(fio_key);
CREATE INDEX IF NOT EXISTS ix_gd_inn ON gzhkh_disqualified(org_inn);

-- Public procurement (EIS, zakupki.gov.ru): contracts where the company was
-- the supplier, and the register of dishonest suppliers (44-FZ and 223-FZ).
CREATE TABLE IF NOT EXISTS eis_contracts (
    number TEXT PRIMARY KEY, supplier_inn TEXT, customer TEXT,
    customer_eis_id TEXT, object TEXT, price REAL, status TEXT, signed TEXT,
    until TEXT, law TEXT
);
CREATE INDEX IF NOT EXISTS ix_eis_sup ON eis_contracts(supplier_inn);
CREATE INDEX IF NOT EXISTS ix_eis_cust ON eis_contracts(customer_eis_id);
CREATE TABLE IF NOT EXISTS eis_rnp (
    number TEXT PRIMARY KEY, inn TEXT, name TEXT, law TEXT, status TEXT,
    included TEXT
);
CREATE INDEX IF NOT EXISTS ix_rnp_inn ON eis_rnp(inn);
"""


def safe_iterparse(fileobj, events=("end",)):
    """iterparse with document type declarations refused.

    The bulk feeds are XML from government portals, but they arrive over the
    network as multi-hundred-megabyte archives, and the stdlib parser will
    happily expand entities declared in a DTD — the billion-laughs and external
    entity problems. None of these feeds use a DTD, so the cheapest correct
    defence is to reject any document that declares one, which needs no
    third-party parser.
    """
    import xml.etree.ElementTree as ET

    head = fileobj.read(4096)
    if b"<!DOCTYPE" in head or b"<!ENTITY" in head:
        raise ValueError("XML объявляет DTD или сущности — разбор отклонён")
    parser = ET.XMLPullParser(events)
    parser.feed(head)
    yield from parser.read_events()
    while True:
        chunk = fileobj.read(1 << 20)
        if not chunk:
            break
        parser.feed(chunk)
        yield from parser.read_events()
    parser.close()
    yield from parser.read_events()


def connect(path=DEFAULT_DB):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    con = sqlite3.connect(path)
    con.row_factory = sqlite3.Row
    con.executescript(SCHEMA)
    return con


def norm_fio(fio):
    return " ".join((fio or "").upper().replace("Ё", "Е").split())


def norm_addr(addr):
    """Address key for co-location pivots — street name plus building number.

    The same building is written a dozen ways across registries: with and
    without the postal index, with the region present or missing, the
    premises number appended or not, even typos in the index. Everything before
    the street name is therefore discarded and only the tail is kept, which is
    the part that actually identifies the place.
    """
    import re

    a = (addr or "").upper().replace("Ё", "Е")
    a = re.sub(r"\b(РОССИЙСКАЯ ФЕДЕРАЦИЯ|РОССИЯ|РФ)\b", " ", a)
    a = re.sub(r"\b(Г|ГОР|ГОРОД|УЛ|УЛИЦА|Д|ДОМ|ПОМЕЩ|ПОМЕЩЕНИЕ|ПОМ|КВ|КВАРТИРА|КОМ|КОМНАТА|"
               r"ЭТАЖ|ОБЛ|ОБЛАСТЬ|КРАЙ|ОФИС|СТР|СТРОЕНИЕ|КОРП|КОРПУС|К|ЛИТЕРА|ЛИТ|"
               r"ПР-КТ|ПРОСПЕКТ|ПЕР|ПЕРЕУЛОК|ПЛ|ПЛОЩАДЬ|ТЕР|ШОССЕ|Ш|"
               r"ВН\.?ТЕР\.?Г\.?|МУНИЦИПАЛЬНЫЙ ОКРУГ|Р-Н|РАЙОН|С|СЕЛО|СТ|СТАНИЦА|"
               r"Г\.?О\.?|ГОРОДСКОЙ ОКРУГ)\b", " ", a)
    a = re.sub(r"[^А-ЯA-Z0-9]+", " ", a)
    tokens = a.split()

    # The street is the last token carrying no digits; what follows is the
    # building, and at most two of those matter (house and block).
    street_at = None
    for i, tok in enumerate(tokens):
        if not any(c.isdigit() for c in tok) and len(tok) > 2:
            street_at = i
    if street_at is None:
        return " ".join(tokens)
    tail = [t for t in tokens[street_at + 1:] if any(c.isdigit() for c in t)][:2]
    return " ".join([tokens[street_at]] + tail)


def person_id(inn, fio):
    inn = (inn or "").strip()
    return inn if inn else "fio:" + norm_fio(fio)


def upsert_org(con, org):
    con.execute(
        """INSERT INTO orgs (inn, ogrn, kpp, name_short, name_full, status, reg_date,
                             address, address_key, okved, okved_name, capital, region, fetched_at, raw)
           VALUES (:inn,:ogrn,:kpp,:name_short,:name_full,:status,:reg_date,
                   :address,:address_key,:okved,:okved_name,:capital,:region,:fetched_at,:raw)
           ON CONFLICT(inn) DO UPDATE SET
             ogrn=excluded.ogrn, kpp=excluded.kpp, name_short=excluded.name_short,
             name_full=excluded.name_full, status=excluded.status, reg_date=excluded.reg_date,
             address=excluded.address, address_key=excluded.address_key, okved=excluded.okved,
             okved_name=excluded.okved_name, capital=excluded.capital, region=excluded.region,
             fetched_at=excluded.fetched_at, raw=excluded.raw""",
        {
            "inn": org["inn"],
            "ogrn": org.get("ogrn"),
            "kpp": org.get("kpp"),
            "name_short": org.get("name_short"),
            "name_full": org.get("name_full"),
            "status": org.get("status"),
            "reg_date": org.get("reg_date"),
            "address": org.get("address"),
            "address_key": norm_addr(org.get("address")),
            "okved": org.get("okved"),
            "okved_name": org.get("okved_name"),
            "capital": org.get("capital"),
            "region": org.get("region"),
            "fetched_at": int(time.time()),
            "raw": json.dumps(org.get("raw") or {}, ensure_ascii=False),
        },
    )


def upsert_person(con, inn, fio):
    pid = person_id(inn, fio)
    con.execute(
        """INSERT INTO persons (pid, inn, fio, fio_key) VALUES (?,?,?,?)
           ON CONFLICT(pid) DO UPDATE SET
             fio=COALESCE(excluded.fio, persons.fio),
             inn=COALESCE(excluded.inn, persons.inn),
             fio_key=COALESCE(excluded.fio_key, persons.fio_key)""",
        (pid, inn or None, fio, norm_fio(fio)),
    )
    return pid


def add_role(con, org_inn, pid, role, grn=None, since=None):
    con.execute(
        "INSERT OR REPLACE INTO roles (org_inn, pid, role, grn, since) VALUES (?,?,?,?,?)",
        (org_inn, pid, role, grn, since),
    )


def add_ownership(con, org_inn, pid=None, owner_inn=None, share_pct=None,
                  share_rub=None, grn=None, kind="owner"):
    con.execute(
        """INSERT OR REPLACE INTO ownership
             (org_inn, pid, owner_inn, kind, share_pct, share_rub, grn)
           VALUES (?,?,?,?,?,?,?)""",
        (org_inn, pid or "", owner_inn or "", kind, share_pct, share_rub, grn),
    )


def add_financials(con, inn, year, lines):
    con.executemany(
        "INSERT OR REPLACE INTO financials (inn, year, code, value) VALUES (?,?,?,?)",
        [(inn, int(year), str(code), float(val)) for code, val in lines.items() if val is not None],
    )


def enqueue(con, inn, depth):
    con.execute(
        "INSERT OR IGNORE INTO crawl_queue (inn, depth, state, seen_at) VALUES (?,?,'pending',?)",
        (inn, depth, int(time.time())),
    )


def mark(con, inn, state, note=None):
    con.execute("UPDATE crawl_queue SET state=?, note=? WHERE inn=?", (state, note, inn))


def tag_case(con, case, inn):
    if case:
        con.execute(
            "INSERT OR IGNORE INTO cases (name, inn, added_at) VALUES (?,?,?)",
            (case, inn, int(time.time())),
        )
