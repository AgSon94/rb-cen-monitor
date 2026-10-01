# Monitor ujemnej CEN

Strona i alert mailowy o ryzyku ujemnej **ceny niezbilansowania (CEN)** na polskim rynku bilansującym.
Wszystkie dane są publiczne i pochodzą z API PSE (`api.raporty.pse.pl`).

- **Strona** (`index.html`, GitHub Pages) – pobiera dane PSE w przeglądarce: prognoza CEN budowana na bieżąco,
  rozliczenie, SDAC, kierunek long/short, poziom ryzyka dla każdego kwadransa, przegląd dowolnej doby od 14.06.2024.
- **Alert mailowy** (`alert.py`) – **przygotowany, na razie nieaktywny** (brak automatu GitHub Actions). Mail, gdy poziom rośnie do 🟠/🔴, przypomnienie co 30 min przy 🔴,
  mail o końcu alertu; wieczorem mapa ryzyka na jutro z ceny SDAC.
- **Reguła i progi** – `regula.json` (wspólne dla strony i maili).

## Reguła

| Poziom | Warunek |
|---|---|
| 🔴 głęboko ujemna | ostatni znany kwadrans long i CEN < `prog_czerwony_zl_mwh` (−500) |
| 🟠 wysokie ryzyko | ostatni znany kwadrans long i CEN < 0, albo SDAC ≤ 0 |
| 🟡 czujność | SDAC < `prog_zolty_sdac_zl_mwh` (200) |

„Ostatni znany” = dwa ostatnie opublikowane kwadranse spoza xx:00; kwadrans xx:00 może poziom tylko podnieść,
bo jego prognoza bywa fałszywie „short” i dodatnia w trakcie epizodów ujemnych cen.
Prognoza PSE ukazuje się ok. 12 min po końcu kwadransu, więc alert sygnalizuje trwający epizod.

## Konfiguracja maili (sekrety repozytorium)

`SMTP_HOST`, `SMTP_PORT` (465 lub 587), `SMTP_USER`, `SMTP_PASS`, `ALERT_TO` (adresy po przecinku).
Test: zakładka Actions → „Alert CEN” → Run workflow → pole „test”, np. `2026-08-14 11:28`.

Lokalnie: `python alert.py --test "2026-08-14 11:28"` (bez wysyłki).
