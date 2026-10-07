#!/usr/bin/env python3
"""Offline checks for the parsing and normalisation logic.

Everything here runs without network and without a database file on disk, so
the suite is safe to run anywhere and fast enough to run on every change. What
it covers is the code that silently produces *wrong* answers rather than
crashing: address keys that fail to match, units off by a thousand, parallel
lists that drift apart, payload shapes that changed upstream.

    python3 tests/smoke.py
"""

import os
import sys
import xml.etree.ElementTree as ET

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "scripts"))

import db          # noqa: E402
import egrul       # noqa: E402
import eis         # noqa: E402
import erknm       # noqa: E402
import gis_zhkh    # noqa: E402
import girbo       # noqa: E402
import kad         # noqa: E402
import people      # noqa: E402
import rostrud     # noqa: E402
import opendata    # noqa: E402

FAILURES = []


def check(name, got, want):
    if got == want:
        print(f"  ok   {name}")
    else:
        print(f"  FAIL {name}\n       получено: {got!r}\n       ожидалось: {got.__class__.__name__} {want!r}")
        FAILURES.append(name)


def section(title):
    print(f"\n{title}")


# --------------------------------------------------------------------------
section("Ключи адресов — один дом, записанный по-разному")

# Написания взяты из реальных реестров, но улица и город заменены.
# Обратите внимание на опечатки в индексе — именно поэтому он не входит в ключ.
SAME_BUILDING = [
    "111111, г. Пример, ул. Ленина, 20/6",
    "г. Пример, ул.Ленина, 20/6",
    "1111112 Примерный край, г. Пример, ул. Ленина, д.20/6",
    "111111, Примерный Край, г.о. Город Пример, г Пример, ул Ленина, д. 20/6",
    "111-1112, ПК, г. Пример, ул. Ленина , д. 20/6, помещение 1",
    "111111, ПРИМЕРНЫЙ КРАЙ, Г. ПРИМЕР, УЛ. ЛЕНИНА, Д. 20/6",
]
keys = {db.norm_addr(a) for a in SAME_BUILDING}
check("шесть написаний дают один ключ", keys, {"ЛЕНИНА 20 6"})

check("соседние дома не склеиваются",
      len({db.norm_addr(a) for a in (
          "г. Пример, ул. Ленина, д. 20/6",
          "г. Пример, ул. Ленина, д. 25/7",
          "г. Пример, ул. Ленина, д. 28/1",
          "г. Пример, ул. Ленина, д. 30/8")}),
      4)

check("улица без корпуса", db.norm_addr("111111, г. Пример, ул. Садовая, 2"),
      "САДОВАЯ 2")
check("пустой адрес не роняет", db.norm_addr(None), "")

section("ФИО и идентификаторы")
check("норма ФИО", db.norm_fio("  Иванов  Иван ёлкин "), "ИВАНОВ ИВАН ЕЛКИН")
check("pid по ИНН", db.person_id("111111111111", "Иванов И. И."), "111111111111")
check("pid без ИНН", db.person_id(None, "Иванов Иван"), "fio:ИВАНОВ ИВАН")

section("Транслитерация для rusprofile")
# ИНН здесь заведомо вымышленные; проверяется только построение слага.
for fio, inn, want in (
    ("МЕДВЕДЕВ СЕРГЕЙ МИХАЙЛОВИЧ", "111111111111", "medvedev-sm-111111111111"),
    ("ОВЧИННИКОВА ТАТЬЯНА ВАСИЛЬЕВНА", "222222222222", "ovchinnikova-tv-222222222222"),
    ("ЩЁГОЛЕВ ЮРИЙ ЯКОВЛЕВИЧ", "333333333333", "shchegolev-yuya-333333333333"),
    ("БЫКОВ АНДРЕЙ ВИКТОРОВИЧ", "444444444444", "bykov-av-444444444444"),
    ("ЦАПЛИН ЖОРЖ ЭДУАРДОВИЧ", "555555555555", "tsaplin-zhe-555555555555"),
):
    check(f"слаг {fio.split()[0].title()}", people.rusprofile_slug(fio, inn), want)

section("Единицы ГИР БО — отчётность подаётся в тысячах рублей")
check("множитель", girbo.UNIT, 1000)
lines = girbo._lines({"current2110": 184384, "current2400": 33162, "previous2110": 70537}, "current")
check("выручка переведена в рубли", lines["2110"], 184384000)
check("previous не попадает в current", "2110" in lines and len(lines), 2)

section("Красные флаги ГИР БО")
flags = girbo.flags([
    {"year": 2025, "revenue": 100.0, "net_profit": -5.0, "equity": -10.0,
     "assets": 50.0, "revenue_yoy_pct": -80.0},
])
joined = " | ".join(flags)
check("отрицательный капитал", "отрицательный капитал" in joined, True)
check("чистый убыток", "убыток" in joined, True)
check("обвал выручки", "упала" in joined, True)

section("Выписка ЕГРЮЛ — колоночный разбор")
EXTRACT = """
                                        Наименование
   1      Полное наименование на русском языке   ОБЩЕСТВО С ОГРАНИЧЕННОЙ
                                                 ОТВЕТСТВЕННОСТЬЮ "ПРИМЕР"
   2      ГРН и дата внесения в ЕГРЮЛ записи,    1067746062449
          содержащей указанные сведения          17.01.2006
   3      Сокращенное наименование на русском                   ООО "ПРИМЕР"
          языке
                     Место нахождения и адрес юридического лица
   7      Адрес юридического лица                               142181,
                                                                МОСКОВСКАЯ ОБЛАСТЬ,
                                                                Д. 6, СТР. 1
                              Сведения об уставном капитале
  26 Вид                                     УСТАВНЫЙ КАПИТАЛ
  27 Размер (в рублях)                       10110000
      Сведения о лице, имеющем право без доверенности действовать от имени юридического
                                        лица
  18    Фамилия                                    ИВАНОВ
        Имя                                        ИВАН
        Отчество                                   ИВАНОВИЧ
  19    ИНН                                        999999999999
  21    Должность                                  ГЕНЕРАЛЬНЫЙ ДИРЕКТОР
                 Сведения об участниках / учредителях юридического лица
  29    ГРН и дата внесения в ЕГРЮЛ сведений о 1067746062449
        данном лице                             17.01.2006
  30    Фамилия                                     ПЕТРОВ
        Имя                                         ПЁТР
        Отчество                                    ПЕТРОВИЧ
  31    ИНН                                         111111111111
  36    Номинальная стоимость доли (в рублях)       3000000
  37    Размер доли (в процентах)                   30
  38    ГРН и дата внесения в ЕГРЮЛ сведений о      2255006878476
        данном лице                                 10.12.2025
  39    ОГРН                                        1147746150309
  40    ИНН                                         3333333333
  41    Полное наименование                         ОБЩЕСТВО С ОГРАНИЧЕННОЙ
                                                    ОТВЕТСТВЕННОСТЬЮ "ВЛАДЕЛЕЦ"
  42    Размер доли (в процентах)                   70
"""
org = egrul.parse_extract(EXTRACT, inn="1111111111")
check("полное наименование склеено из переносов", org["name_full"],
      'ОБЩЕСТВО С ОГРАНИЧЕННОЙ ОТВЕТСТВЕННОСТЬЮ "ПРИМЕР"')
check("краткое наименование", org["name_short"], 'ООО "ПРИМЕР"')
check("адрес склеен", org["address"], "142181, МОСКОВСКАЯ ОБЛАСТЬ, Д. 6, СТР. 1")
check("уставный капитал", org["capital"], 10110000.0)
check("директор", org["director"]["fio"], "ИВАНОВ ИВАН ИВАНОВИЧ")
check("ИНН директора", org["director"]["inn"], "999999999999")
check("должность", org["director"]["post"], "ГЕНЕРАЛЬНЫЙ ДИРЕКТОР")
# ё сохраняется как в источнике; сведение ё к е делает db.norm_fio при дедупликации
check("учредитель-физлицо", [(f["fio"], f["share_pct"]) for f in org["founders"]],
      [("ПЕТРОВ ПЁТР ПЕТРОВИЧ", 30.0)])
check("ключ ФИО сводит ё к е", db.norm_fio(org["founders"][0]["fio"]),
      "ПЕТРОВ ПЕТР ПЕТРОВИЧ")
check("учредитель-юрлицо", [(f["inn"], f["share_pct"]) for f in org["founder_orgs"]],
      [("3333333333", 70.0)])
check("статус по умолчанию", org["status"], "действующее")

section("Арбитраж — оба формата ответа")
CASES = [
    {"CaseId": "c1", "CaseNumber": "А40-1/2025", "Date": "2025-03-04T00:00:00",
     "Court": {"Name": "АС города Москвы"}, "Type": {"Name": "Гражданские"},
     "Plaintiffs": [{"Organizations": [{"Name": 'ООО "АЛЬФА"', "Inn": "1111111111"}]}],
     "Respondents": [{"Organizations": [{"Name": 'ООО "БЕТА"', "Inn": "2222222222"}]}]},
    {"CaseId": "c2", "CaseNumber": "А41-9/2024", "Date": "2024-11-20T00:00:00",
     "CourtName": "АС Московской области", "Type": "Банкротные",
     "Sides": [{"Name": 'ООО "БЕТА"', "Inn": "2222222222", "Type": 1},
               {"Name": 'ООО "АЛЬФА"', "Inn": "1111111111", "Type": 2}]},
]
case1, parties1 = kad.normalise(CASES[0])
check("раздельные списки: номер", case1["number"], "А40-1/2025")
check("раздельные списки: стороны",
      sorted((p["inn"], p["side"]) for p in parties1),
      [("1111111111", "истец"), ("2222222222", "ответчик")])
case2, parties2 = kad.normalise(CASES[1])
check("единый Sides: тип разобран", case2["type"], "Банкротные")
check("единый Sides: роли по номеру",
      sorted((p["inn"], p["side"]) for p in parties2),
      [("1111111111", "ответчик"), ("2222222222", "истец")])
check("ссылка на карточку", case1["url"], "https://kad.arbitr.ru/Card/c1")

section("ЕРКНМ — данные лежат в атрибутах, не в тексте")
INSPECTION = """<INSPECTION xmlns="https://proverki.gov.ru/opendata/3.0/20220115"
    ERPID="26260942600022469358" STATUS="Предостережение объявлено"
    TYPE_NAME="Профилактическое мероприятие" CLASSIFICATION="ПМ"
    START_DATE="2026-07-27" STOP_DATE="2026-07-27">
  <PROSECUTOR_OFFICE NAME="Прокуратура Примерного края"/>
  <KIND_CONTROL VALUE="Региональный государственный лицензионный контроль"/>
  <KIND_KNM VALUE="Объявление предостережения"/>
  <KNO_ORGANIZATION VALUE="ГОСУДАРСТВЕННАЯ ЖИЛИЩНАЯ ИНСПЕКЦИЯ"/>
  <SUBJECT INN="1111111111" OGRN="1111111111111" NAME="ООО ПРИМЕР" TYPE="ЮЛ"/>
</INSPECTION>"""
el = ET.fromstring(INSPECTION)
check("namespace снимается", erknm._local(el.tag), "INSPECTION")
subj = erknm._subject(el)
check("SUBJECT найден", subj is not None, True)
rec = erknm._record(el, subj, 2026, 7)
check("номер мероприятия", rec["number"], "26260942600022469358")
check("ИНН из атрибута", rec["inn"], "1111111111")
check("орган контроля", rec["authority"], "ГОСУДАРСТВЕННАЯ ЖИЛИЩНАЯ ИНСПЕКЦИЯ")
check("вид мероприятия", rec["control_kind"], "Объявление предостережения")
check("прокуратура", rec["prosecutor"], "Прокуратура Примерного края")
check("статус", rec["status"], "Предостережение объявлено")

section("Роструд — три параллельных списка")
rec = {"wp_numbers": "1, 2, 3", "positions": "Директор, Бухгалтер, Дворник",
       "headcounts": "1, 1, 4",
       "address": "111111, г. Пример, ул. Ленина, 20/6\nвторой адрес"}
wps = rostrud.workplaces(rec)
check("три рабочих места", len(wps), 3)
check("должности и численность совпали",
      [(w["position"], w["headcount"]) for w in wps],
      [("Директор", 1), ("Бухгалтер", 1), ("Дворник", 4)])
check("первый адрес из нескольких", rostrud.first_address(rec),
      "111111, г. Пример, ул. Ленина, 20/6")

drift = rostrud.workplaces({"wp_numbers": "1, 2", "positions": "Директор, Бухгалтер, Дворник",
                            "headcounts": "1, 1"})
check("при расхождении длин численность не выдумывается",
      [w["headcount"] for w in drift], [None, None, None])
check("должности при этом сохранены", len(drift), 3)

section("ГИС ЖКХ — телефоны и блоки номеров")
# ЕИС: разбор карточек выдачи
EIS_CONTRACT = """<div class="search-results__total">12 записей</div>
<div class="search-registry-entry-block box-shadow-search-input">
<div class="registry-entry__header-mid__number"><a href="/epz/contract/contractCard/common-info.html?reestrNumber=1111111111111000001">№ 1</a></div>
<div class="registry-entry__header-mid__title">Исполнение</div>
<div class="registry-entry__body-title">Заказчик</div>
<div class="registry-entry__body-href"><a href="/epz/organization/view/info.html?organizationId=42">МБОУ &quot;Школа&quot;</a></div>
<div class="price-block__title">Цена контракта</div>
<div class="price-block__value">31 085,99&nbsp;&#8381;</div>
<div class="data-block__title">Заключение контракта</div><div class="data-block__value">01.02.2024</div>
</div>"""
_c = eis._cards(EIS_CONTRACT)
check("ЕИС: одна карточка", len(_c), 1)
check("ЕИС: всего записей", eis._total(EIS_CONTRACT), 12)
check("ЕИС: цена с копейками", eis._money(eis._field(_c[0], "Цена контракта")), 31085.99)
check("ЕИС: дата заключения", eis._field(_c[0], "Заключение контракта"), "01.02.2024")

check("+7(xxxx) в десять цифр", gis_zhkh.norm_phone("+7(8652)998080"), "8652998080")
check("одиннадцать цифр с 7", gis_zhkh.norm_phone("78652451717"), "8652451717")
check("мусор не телефон", gis_zhkh.norm_phone("12-34"), None)
check("блок городского номера", gis_zhkh.phone_block("78652451717"), "865245")
check("у мобильного блока нет", gis_zhkh.phone_block("+7(928)3016220"), None)
check("зеркальный хвост", gis_zhkh.is_vanity("+7(8652)997070"), True)
check("обычный хвост", gis_zhkh.is_vanity("78652992428"), False)
check("сокращение формы", gis_zhkh.short_name(
      'ОБЩЕСТВО С ОГРАНИЧЕННОЙ ОТВЕТСТВЕННОСТЬЮ "УПРАВЛЯЮЩАЯ КОМПАНИЯ "ПРИМЕР"'), 'ООО "УК "ПРИМЕР"')

section("Схема базы создаётся начисто")
con = db.connect(":memory:") if False else None
import sqlite3, tempfile  # noqa: E402
with tempfile.TemporaryDirectory() as tmp:
    path = os.path.join(tmp, "t.db")
    con = db.connect(path)
    tables = {r[0] for r in con.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    expected = {"orgs", "persons", "roles", "ownership", "financials", "crawl_queue",
                "cases", "court_cases", "court_parties", "headcount", "tax_debt",
                "revexp", "tax_regime", "opendata_loads", "person_orgs",
                "inspections", "sout", "gzhkh_licenses", "gzhkh_houses",
                "gzhkh_house_licensees", "gzhkh_orgs", "disqualified", "gzhkh_disqualified"}
    check("все таблицы созданы", expected - tables, set())

    db.upsert_org(con, {"inn": "1", "name_short": "А",
                        "address": "г. Пример, ул. Ленина, д. 20/6"})
    db.upsert_org(con, {"inn": "1", "name_short": "Б", "address": "г. Пример, ул. Ленина, д. 20/6"})
    check("upsert не плодит дубли",
          con.execute("SELECT COUNT(*) FROM orgs").fetchone()[0], 1)
    check("upsert обновляет имя",
          con.execute("SELECT name_short FROM orgs").fetchone()[0], "Б")
    check("ключ адреса посчитан при вставке",
          con.execute("SELECT address_key FROM orgs").fetchone()[0], "ЛЕНИНА 20 6")

    db.add_ownership(con, "1", owner_inn="2", share_pct=100.0)
    db.add_ownership(con, "1", owner_inn="2", kind="manager")
    check("участник и управляющая организация — разные связи",
          con.execute("SELECT COUNT(*) FROM ownership").fetchone()[0], 2)

    # Реестр дисквалифицированных: сила совпадения зависит от организации
    pid = db.upsert_person(con, "777777777777", "Сидоров Сидор Сидорович")
    db.add_role(con, "1", pid, "ДИРЕКТОР")
    pid2 = db.upsert_person(con, "888888888888", "Кузнецов Кузьма Кузьмич")
    db.add_role(con, "1", pid2, "ДИРЕКТОР")
    for no, fio, org in (("1", "СИДОРОВ СИДОР СИДОРОВИЧ", "1"), ("2", "КУЗНЕЦОВ КУЗЬМА КУЗЬМИЧ", "9")):
        con.execute("INSERT INTO disqualified (record_no, fio, fio_key, org_inn) VALUES (?,?,?,?)",
                    (no, fio, db.norm_fio(fio), org))
    grades = sorted((g, r["fio"]) for g, r, _ in opendata.disq_matches(con))
    check("совпадение ФИО и организации — strong, только ФИО — name", grades,
          [("name", "Кузнецов Кузьма Кузьмич"), ("strong", "Сидоров Сидор Сидорович")])

    # Отраслевой реестр ГИС ЖКХ: совпадение по ИНН организации — отдельный класс
    con.execute("INSERT INTO gzhkh_disqualified (guid, fio, fio_key, org_inn) VALUES (?,?,?,?)",
                ("g1", "Петров Петр", db.norm_fio("Петров Петр"), "1"))
    con.execute("INSERT INTO gzhkh_disqualified (guid, fio, fio_key, org_inn) VALUES (?,?,?,?)",
                ("g2", "Сидоров Сидор Сидорович", db.norm_fio("Сидоров Сидор Сидорович"), "1"))
    got = sorted(g for g, _, _ in gis_zhkh.disq_matches(con))
    check("ГИС ЖКХ: компания и человек дают org и strong", got, ["org", "org", "strong"])

    # ЕИС: общий заказчик у двух поставщиков — связь
    eis.store(con, "1", [{"number": "100", "customer": "Заказчик А", "customer_eis_id": "5",
                          "object": None, "price": 10.0, "status": None, "signed": None,
                          "until": None, "law": "44-ФЗ"}], [])
    eis.store(con, "2", [{"number": "200", "customer": "Заказчик А", "customer_eis_id": "5",
                          "object": None, "price": 5.0, "status": None, "signed": None,
                          "until": None, "law": "44-ФЗ"}], [])
    sc = eis.shared_customers(con, ["1", "2"])
    check("ЕИС: общий заказчик найден", [(r["n"], r["total"]) for r in sc], [(2, 15.0)])
    con.commit()

print()
if FAILURES:
    print(f"ПРОВАЛЕНО: {len(FAILURES)} — " + ", ".join(FAILURES))
    sys.exit(1)
print("все проверки пройдены")
