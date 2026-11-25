# ID Austria Appointment Monitor (Vienna)

Continuously monitors live appointment availability for **ID Austria registration** across:
- **LPD Wien (BMI ETV SVA):** All 14 Wiener Polizeikommissariate
- **Finanzamt Österreich (BMF):** Wien-Mitte (1030) and Wien-Kagran (1220), plus optional nearby Niederösterreich offices (Mödling, Tulln, Bruck an der Leitha)

## Quick Start

Run a single check to see the earliest slot at every Vienna office right now:

```powershell
python monitor.py --once
```

Also include nearby Lower Austria (NÖ) Finanzämter (e.g., Mödling, ~25 min from Vienna by S-Bahn):

```powershell
python monitor.py --once --include-nearby
```

Run continuously in the background (checks every 90 seconds by default):

```powershell
python monitor.py
```

## Configuration (`config.json`)

- `exclude_dates`: Defaults to `2026-12-07` through `2026-12-11` (inclusive).
- `max_date`: Only triggers alerts for slots on or before this date (default `2027-02-28`, so you don't get spammed by slots 5 months away).
- `finanzamt.extra_location_ids`: Add `[2]` for Mödling, `[2, 24, 33]` for Mödling + Tulln + Bruck an der Leitha.
- `notifications`:
  - **Windows beep + desktop toast** are enabled by default.
  - **Phone push in 10 seconds (`ntfy`):** Install the free `ntfy` app on your phone, subscribe to a random secret topic name, set `ntfy.enabled: true` and `ntfy.topic_url: "https://ntfy.sh/<your-secret-topic>"`.
  - **Email (Gmail):** Create a Gmail App Password in your Google Account security settings, then set `email.enabled: true` and fill in `username`, `password`, and `to_addr`.
  - **Telegram / Discord:** Set `enabled: true` and fill in your bot token/chat ID or webhook URL.

Test your notification setup anytime:

```powershell
python monitor.py --test-notify
```

## Direct Booking Links

- **LPD Wien (Polizeikommissariate):** https://citizen.bmi.gv.at/at.gv.bmi.fnsetvweb-p/etv/public/sva/Terminvereinbarung
- **Finanzamt Österreich (ID Austria):** https://service.bmf.gv.at/service/anwend/terminvereinbarung/?thema=54
