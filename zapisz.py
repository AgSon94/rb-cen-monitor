"""Zapis na żywo kolejnych wersji raportów PSE – żeby sprawdzić, co było widać w danej chwili.

API PSE podaje tylko ostatnią wersję raportu. Historia ograniczeń OZE (poze-redoze) jest nadpisywana
tygodnie później, a prognoza CEN (price-fcst) może być poprawiana. Ten skrypt uruchamiany co kilka minut
dopisuje do CSV każdą nową lub zmienioną wersję wiersza wraz z czasem publikacji PSE i czasem odczytu.

  python zapisz.py --cel <katalog>      (domyślnie: zapis/)

Pliki: <cel>/<raport>/RRRR-MM.csv
"""
import argparse
import csv
import datetime as dt
import json
import pathlib
import urllib.parse
import urllib.request

API = "https://api.raporty.pse.pl/api/"
UTC = dt.timezone.utc
RAPORTY = {
    # raport: (pola wartości, ile dób do przodu pobierać)
    "poze-redoze": (["pv_red_balance", "wi_red_balance", "pv_red_network", "wi_red_network"], 1),
    "price-fcst": (["cen_fcst", "contracting", "imb_energy", "ceb_sr_fcst"], 0),
}
KOLUMNY_STALE = ["odczyt_utc", "business_date", "period", "dtime_utc", "publication_ts_utc"]


def dzis_pl():
    """Data w Polsce bez bazy stref czasowych (letni: ostatnia niedziela III – ostatnia niedziela X)."""
    t = dt.datetime.now(UTC)
    def ost_niedz(m):
        d = dt.date(t.year, m, 31)
        return d - dt.timedelta(days=(d.weekday() + 1) % 7)
    lato = (dt.datetime.combine(ost_niedz(3), dt.time(1), UTC) <= t
            < dt.datetime.combine(ost_niedz(10), dt.time(1), UTC))
    return (t + dt.timedelta(hours=2 if lato else 1)).date()


def pobierz(raport, data):
    url = API + raport + "?" + urllib.parse.urlencode(
        {"$filter": f"business_date eq '{data}'", "$first": "200"}, quote_via=urllib.parse.quote)
    with urllib.request.urlopen(url, timeout=60) as odp:
        return json.load(odp)["value"]


def main():
    a = argparse.ArgumentParser()
    a.add_argument("--cel", default="zapis")
    cel = pathlib.Path(a.parse_args().cel)
    odczyt = dt.datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S")
    dzis = dzis_pl()

    for raport, (pola, do_przodu) in RAPORTY.items():
        kolumny = KOLUMNY_STALE + pola
        nowe = 0
        for dzien in range(do_przodu + 1):
            data = (dzis + dt.timedelta(days=dzien)).isoformat()
            try:
                wiersze = pobierz(raport, data)
            except Exception as e:  # PSE bywa niedostępne – następny przebieg spróbuje ponownie
                print(f"{raport} {data}: błąd pobrania ({e})")
                continue
            plik = cel / raport / f"{data[:7]}.csv"
            plik.parent.mkdir(parents=True, exist_ok=True)
            # znane wersje: ostatnia wersja każdego kwadransu
            ostatnie = {}
            if plik.exists():
                with plik.open(encoding="utf-8", newline="") as f:
                    for r in csv.DictReader(f):
                        ostatnie[r["dtime_utc"]] = tuple(r[k] for k in ["publication_ts_utc"] + pola)
            nowy_plik = not plik.exists()
            with plik.open("a", encoding="utf-8", newline="") as f:
                w = csv.writer(f)
                if nowy_plik:
                    w.writerow(kolumny)
                for r in wiersze:
                    wart = tuple("" if r.get(k) is None else str(r.get(k)) for k in ["publication_ts_utc"] + pola)
                    if ostatnie.get(r["dtime_utc"]) == wart:
                        continue
                    w.writerow([odczyt, r["business_date"], r["period"], r["dtime_utc"]] + list(wart))
                    ostatnie[r["dtime_utc"]] = wart
                    nowe += 1
        print(f"{raport}: {nowe} nowych wersji wierszy")


if __name__ == "__main__":
    main()
