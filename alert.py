"""Alarm mailowy o ryzyku ujemnej ceny niezbilansowania (CEN) na polskim rynku bilansującym.

Źródło: publiczne API PSE (api.raporty.pse.pl). Reguła i progi: regula.json.
Ta sama reguła działa na stronie (index.html) – zmieniać obie naraz.

Tryby:
  python alert.py                       – stan teraz; mail, gdy poziom wzrósł / trwa 🔴 / epizod się skończył
  python alert.py --d1                  – mapa ryzyka na jutro z ceny SDAC (wysyłane wieczorem)
  python alert.py --test "2026-08-14 11:20"  – symulacja chwili z przeszłości (czas lokalny PL), bez stanu
  dodaj --wyslij, żeby w trybie --test naprawdę wysłać mail

Zmienne środowiskowe (sekrety GitHub): SMTP_HOST, SMTP_PORT, SMTP_USER, SMTP_PASS, ALERT_TO (adresy po przecinku),
PAGE_URL. Bez SMTP_* skrypt tylko wypisuje treść maila.
"""
import argparse
import datetime as dt
import json
import os
import pathlib
import smtplib
import ssl
import sys
import urllib.parse
import urllib.request
from email.message import EmailMessage

KAT = pathlib.Path(__file__).parent
API = "https://api.raporty.pse.pl/api/"
REG = json.loads((KAT / "regula.json").read_text(encoding="utf-8"))
STAN = KAT / "stan.json"
UTC = dt.timezone.utc
ZNAK = {0: "⚪", 1: "🟡", 2: "🟠", 3: "🔴"}
NAZWA = {0: "brak zagrożenia", 1: "czujność", 2: "ostrzeżenie – ujemna CEN", 3: "alarm – głęboko ujemna CEN"}


# ---------- czas polski bez bazy stref (Windows nie ma tzdata) ----------

def _ostatnia_niedziela(rok, mies):
    d = dt.date(rok, mies, 31)
    return d - dt.timedelta(days=(d.weekday() + 1) % 7)


def przesuniecie_pl(t_utc: dt.datetime) -> dt.timedelta:
    r = t_utc.year
    lato_od = dt.datetime.combine(_ostatnia_niedziela(r, 3), dt.time(1), UTC)
    lato_do = dt.datetime.combine(_ostatnia_niedziela(r, 10), dt.time(1), UTC)
    return dt.timedelta(hours=2 if lato_od <= t_utc < lato_do else 1)


def lokalnie(t_utc):
    return (t_utc + przesuniecie_pl(t_utc)).replace(tzinfo=None)


def z_lokalnego(t_lok: dt.datetime) -> dt.datetime:
    t = t_lok.replace(tzinfo=UTC)
    return t - przesuniecie_pl(t - dt.timedelta(hours=1))


# ---------- dane PSE ----------

def pobierz(endpoint, data):
    url = API + endpoint + "?" + urllib.parse.urlencode(
        {"$filter": f"business_date eq '{data}'", "$first": "200"}, quote_via=urllib.parse.quote)
    with urllib.request.urlopen(url, timeout=60) as odp:
        return json.load(odp)["value"]


def _utc(s):
    return dt.datetime.fromisoformat(s[:19]).replace(tzinfo=UTC) if s else None


def doba(data: str) -> list[dict]:
    """Okresy doby z prognozą CEN, kierunkiem, czasem publikacji i SDAC."""
    okresy = {}
    for r in pobierz("csdac-pln", data):
        k = r["dtime_utc"][:16]
        koniec = _utc(r["dtime_utc"])
        okresy[k] = {"okres": r["period"], "start": koniec - dt.timedelta(minutes=15),
                     "sdac": r["csdac_pln"]}
    for r in pobierz("price-fcst", data):
        k = r["dtime_utc"][:16]
        if k not in okresy:
            koniec = _utc(r["dtime_utc"])
            okresy[k] = {"okres": r["period"], "start": koniec - dt.timedelta(minutes=15), "sdac": None}
        okresy[k].update(cen_f=r["cen_fcst"], kier_f=r["contracting"], pub=_utc(r["publication_ts_utc"]))
    wyn = [okresy[k] for k in sorted(okresy)]
    for o in wyn:
        o["minuta"] = lokalnie(o["start"]).minute
    return wyn


# ---------- reguła (ta sama co w index.html i w analizie historii) ----------

def poziom(O, n, chwila):
    """Poziom ryzyka dla okresu O[n], gdy wiemy tylko to, co PSE opublikowało do `chwila`.

    Histereza: stan liczony z k (regula.json: koniec_alertu_po_kwadransach) ostatnich znanych kwadransów spoza xx:00 –
    alarm zaczyna się przy pierwszym sygnale, kończy dopiero po k kolejnych bez ujemnej CEN; kwadrans xx:00
    może stan tylko pogorszyć (long i CEN < 0), bo jego prognoza bywa fałszywie „short” i dodatnia.
    Zwraca (poziom, najniższa znana CEN long albo None, lista znanych okresów)."""
    k = REG.get("koniec_alertu_po_kwadransach", 2)
    znane = sorted((o for i, o in enumerate(O) if o.get("pub") and o["pub"] <= chwila and i >= n - 8),
                   key=lambda o: o["pub"])[-(k + 4):]
    zwykle = [h for h in znane if h["minuta"] != 0][-k:]
    pelne = [h for h in znane[-(k + 1):] if h["minuta"] == 0 and h["kier_f"] == "long" and h["cen_f"] < 0]
    ceny = [h["cen_f"] for h in zwykle + pelne if h["kier_f"] == "long"]
    c = min(ceny) if ceny else None
    sdac = O[n].get("sdac")
    if c is not None and c < REG["prog_czerwony_zl_mwh"]:
        p = 3
    elif c is not None and c < 0:
        p = 2
    elif sdac is not None and sdac <= 0:
        p = 2
    elif sdac is not None and sdac < REG["prog_zolty_sdac_zl_mwh"]:
        p = 1
    else:
        p = 0
    return p, c, znane


def ocena_teraz(chwila_utc):
    data = lokalnie(chwila_utc).date().isoformat()
    O = doba(data)
    nast = [i for i, o in enumerate(O) if o["start"] >= chwila_utc]
    if not nast:
        return None
    n = nast[0]
    p, c, znane = poziom(O, n, chwila_utc)
    return {"data": data, "O": O, "n": n, "poziom": p, "cena": c, "znane": znane}


# ---------- mail ----------

def tresc(oc, powod):
    O, n, p = oc["O"], oc["n"], oc["poziom"]
    z = lambda x: "–" if x is None else f"{x:,.2f}".replace(",", " ")  # noqa: E731
    k = REG.get("koniec_alertu_po_kwadransach", 2)
    wiersze = [f"{ZNAK[p]} {NAZWA[p].upper()} – {powod}", "",
               f"Doba {oc['data']}, najbliższy kwadrans {O[n]['okres']}.",
               f"Alarm skończy się po {k} kolejnych zwykłych kwadransach bez ujemnej CEN – przyjdzie wtedy mail "
               "„koniec alarmu”. Pojedynczy dodatni kwadrans (zwłaszcza xx:00) nie kończy alarmu.", ""]
    if oc["znane"]:
        wiersze.append("Ostatnie opublikowane prognozy PSE (kwadrans | CEN zł/MWh | kierunek):")
        for h in oc["znane"][-4:]:
            uw = "  ← xx:00, mało wiarygodny" if h["minuta"] == 0 else ""
            wiersze.append(f"  {h['okres']} | {z(h['cen_f'])} | {h['kier_f']}{uw}")
        wiersze.append("")
    wiersze.append("SDAC na najbliższą godzinę: " + ", ".join(
        f"{o['okres'][:5]} {z(o['sdac'])}" for o in O[n:n + 4]))
    wiersze += ["", "Co mówi historia (VI 2024–IX 2026, godz. 7–18):",
                "  🟠 ostatni znany kwadrans long i CEN < 0 → następny ujemny w ok. 72% przypadków, mediana ok. −160 zł/MWh",
                f"  🔴 ostatni znany kwadrans long i CEN < {REG['prog_czerwony_zl_mwh']} → ujemny w ok. 81%, "
                "w co drugim poniżej −500, 2% najgorszych poniżej −9 000 zł/MWh",
                "", "Prognoza PSE ukazuje się ok. 12 min po końcu kwadransu – alarm mówi o trwającym epizodzie, "
                "nie przewiduje pierwszego ujemnego kwadransu."]
    if os.environ.get("PAGE_URL"):
        wiersze += ["", "Wykres na żywo: " + os.environ["PAGE_URL"]]
    return "\n".join(wiersze)


def tresc_koniec(oc):
    O, n, p = oc["O"], oc["n"], oc["poziom"]
    k = REG.get("koniec_alertu_po_kwadransach", 2)
    ost = [h for h in oc["znane"] if h["minuta"] != 0][-k:]
    wiersze = [f"⚪ KONIEC ALARMU – od kwadransu {O[n]['okres']}.", "",
               f"{k} ostatnie znane zwykłe kwadranse bez ujemnej CEN przy kierunku long:"]
    wiersze += [f"  {h['okres']} | {h['cen_f']:.2f} zł/MWh | {h['kier_f']}" for h in ost]
    wiersze += ["", f"Poziom teraz: {ZNAK[p]} {NAZWA[p]}. Jeśli CEN znów spadnie, przyjdzie nowy alarm."]
    if os.environ.get("PAGE_URL"):
        wiersze += ["", "Wykres: " + os.environ["PAGE_URL"]]
    return "\n".join(wiersze)


def wyslij(temat, tekst, naprawde=True):
    print(f"--- MAIL: {temat}\n{tekst}\n---")
    host, do = os.environ.get("SMTP_HOST"), os.environ.get("ALERT_TO")
    if not naprawde or not host or not do:
        print("(mail nie wysłany – brak SMTP_HOST/ALERT_TO albo tryb próbny)")
        return
    m = EmailMessage()
    m["Subject"], m["From"], m["To"] = temat, os.environ["SMTP_USER"], do
    m.set_content(tekst)
    port = int(os.environ.get("SMTP_PORT", "465"))
    if port == 465:
        with smtplib.SMTP_SSL(host, port, context=ssl.create_default_context()) as s:
            s.login(os.environ["SMTP_USER"], os.environ["SMTP_PASS"])
            s.send_message(m)
    else:
        with smtplib.SMTP(host, port) as s:
            s.starttls(context=ssl.create_default_context())
            s.login(os.environ["SMTP_USER"], os.environ["SMTP_PASS"])
            s.send_message(m)
    print("wysłano do:", do)


# ---------- tryby ----------

def tryb_biezacy():
    teraz = dt.datetime.now(UTC)
    oc = ocena_teraz(teraz)
    if oc is None:
        return
    stan = json.loads(STAN.read_text(encoding="utf-8")) if STAN.exists() else {}
    if stan.get("data") != oc["data"]:
        stan = {"data": oc["data"], "poziom": 0, "ostatni_mail": None, "alert_od": None, "maks": 0}
    p, poprz, prog = oc["poziom"], stan["poziom"], REG["mail_od_poziomu"]
    k = REG.get("koniec_alertu_po_kwadransach", 2)
    start_n = oc["O"][oc["n"]]["start"]
    # alarm trwa co najmniej k kwadransów – bez tego alarm z powodu SDAC ≤ 0 potrafił się skończyć po jednym kwadransie
    if p < prog <= poprz and stan.get("alert_od") and start_n - _utc(stan["alert_od"]) < dt.timedelta(minutes=15 * k):
        print(f"alarm podtrzymany (trwa krócej niż {k} kwadranse)")
        p = poprz
    print(f"{lokalnie(teraz):%Y-%m-%d %H:%M} poziom {p} (poprzednio {poprz}), najniższa znana CEN long: {oc['cena']}")

    powod = None
    if p >= prog and poprz < prog:
        powod = "początek"
    elif p >= prog and p > stan.get("maks", 0):  # tylko poziom wyższy niż dotąd w tym epizodzie
        powod = "pogorszenie"
    elif p == 3 and poprz >= prog and stan.get("ostatni_mail"):
        if teraz - _utc(stan["ostatni_mail"]) >= dt.timedelta(minutes=REG["przypomnienie_czerwony_min"]):
            powod = "trwa"
    if powod:
        od = oc["O"][oc["n"]]["okres"][:5]
        temat = {"początek": f"{ZNAK[p]} CEN: {NAZWA[p]} od {od}",
                 "pogorszenie": f"{ZNAK[p]} CEN: pogorszenie – {NAZWA[p]} ({od})",
                 "trwa": f"{ZNAK[p]} CEN: {NAZWA[p]} trwa ({od})"}[powod]
        wyslij(temat, tresc(oc, powod))
        stan["ostatni_mail"] = teraz.strftime("%Y-%m-%d %H:%M:%S")
        if powod == "początek":
            stan["alert_od"] = start_n.strftime("%Y-%m-%d %H:%M:%S")
    elif p < prog <= poprz:
        wyslij(f"⚪ CEN: koniec alarmu od {oc['O'][oc['n']]['okres'][:5]}", tresc_koniec(oc))
        stan["ostatni_mail"] = teraz.strftime("%Y-%m-%d %H:%M:%S")
        stan["alert_od"] = None
        stan["maks"] = 0
    stan["poziom"] = p
    if p >= prog:
        stan["maks"] = max(stan.get("maks", 0), p)
    nowy = json.dumps(stan, ensure_ascii=False, indent=1)
    if not STAN.exists() or STAN.read_text(encoding="utf-8") != nowy:
        STAN.write_text(nowy, encoding="utf-8")


def tryb_d1():
    jutro = (lokalnie(dt.datetime.now(UTC)) + dt.timedelta(days=1)).date().isoformat()
    O = doba(jutro)
    g0, g1 = REG["godziny_pv"]
    ryz = [o for o in O if o["sdac"] is not None and g0 <= lokalnie(o["start"]).hour < g1
           and o["sdac"] < REG["prog_zolty_sdac_zl_mwh"]]
    if not O:
        print("brak SDAC na jutro")
        return
    if not ryz:
        print(f"{jutro}: SDAC ≥ {REG['prog_zolty_sdac_zl_mwh']} zł w godz. {g0}–{g1} – bez maila")
        return
    pom = sum(o["sdac"] <= 0 for o in ryz)
    wiersze = [f"Jutro ({jutro}) w godz. {g0}–{g1} jest {len(ryz)} kwadransów z SDAC < "
               f"{REG['prog_zolty_sdac_zl_mwh']} zł/MWh, w tym {pom} z SDAC ≤ 0.", "",
               "W takich kwadransach CEN była historycznie ujemna w ok. 31–78% przypadków (im niższa SDAC, tym częściej).",
               "To sygnał do czujności – właściwy alarm przychodzi w ciągu dnia.", "", "Kwadranse (SDAC zł/MWh):"]
    wiersze += [f"  {o['okres']}  {o['sdac']:8.2f}  {'🟠' if o['sdac'] <= 0 else '🟡'}" for o in ryz]
    if os.environ.get("PAGE_URL"):
        wiersze += ["", "Wykres: " + os.environ["PAGE_URL"]]
    znak = "🟠" if pom else "🟡"
    wyslij(f"{znak} CEN jutro: {len(ryz)} kwadransów z tanią SDAC ({jutro})", "\n".join(wiersze))


def tryb_test(chwila_lok: str, naprawde: bool):
    chwila = z_lokalnego(dt.datetime.fromisoformat(chwila_lok))
    oc = ocena_teraz(chwila)
    if oc is None:
        print("brak danych")
        return
    p = oc["poziom"]
    wyslij(f"[TEST] {ZNAK[p]} CEN: {NAZWA[p]} – symulacja {chwila_lok}", tresc(oc, "test"), naprawde)


if __name__ == "__main__":
    a = argparse.ArgumentParser()
    a.add_argument("--d1", action="store_true")
    a.add_argument("--test")
    a.add_argument("--wyslij", action="store_true")
    x = a.parse_args()
    if x.test:
        tryb_test(x.test, x.wyslij)
    elif x.d1:
        tryb_d1()
    else:
        tryb_biezacy()
    sys.exit(0)
