#!/usr/bin/env python3
"""
ID Austria Appointment Monitor (Vienna: LPD + Finanzamt Österreich)

Continuously polls:
  1. Landespolizeidirektion Wien (BMI ETV SVA) - all 14 Polizeikommissariate
  2. Finanzamt Österreich (BMF) - Wien-Mitte, Wien-Kagran (+ optional nearby NÖ)

Filters out excluded date ranges (default: 2026-12-07 to 2026-12-11) and alerts
immediately when a new slot opens on or before `max_date`.
"""

from __future__ import annotations

import argparse
import datetime
import json
import logging
import os
import re
import smtplib
import subprocess
import sys
import time
from dataclasses import dataclass
from email.message import EmailMessage
from pathlib import Path
from typing import Any

import requests

# Official booking portal URLs (for direct manual booking when notified)
LPD_BOOKING_URL = (
    "https://citizen.bmi.gv.at/at.gv.bmi.fnsetvweb-p/etv/public/sva/Terminvereinbarung"
)
BMF_BOOKING_URL = (
    "https://service.bmf.gv.at/service/anwend/terminvereinbarung/?thema=54"
)

LPD_BASE_URL = (
    "https://citizen.bmi.gv.at/at.gv.bmi.fnsetvweb-p/etv/public/sva/"
)
BMF_API_BASE = "https://termver.bmf.gv.at/AgentCockpitService/api/"

# Known Vienna Finanzamt location IDs (queried directly even when GetLocations omits them)
KNOWN_VIENNA_BMF_LOCATIONS = {
    15: "Finanzamt Wien-Mitte (1030, Marxergasse 4)",
    16: "Finanzamt Wien-Kagran (1220, Dr.-Adolf-Schärf-Platz 2)",
}

# Nearby Niederösterreich Finanzamt location IDs reachable by S-Bahn/train from Vienna
NEARBY_NOE_BMF_LOCATIONS = {
    2: "Finanzamt Mödling (2340, NÖ)",
    24: "Finanzamt Tulln an der Donau (3430, NÖ)",
    33: "Finanzamt Bruck an der Leitha (2460, NÖ)",
}

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)


@dataclass(frozen=True)
class AppointmentSlot:
    institution: str  # "LPD" or "Finanzamt"
    office_name: str
    office_id: int
    start_dt: datetime.datetime
    booking_url: str

    @property
    def key(self) -> str:
        return f"{self.institution}:{self.office_id}:{self.start_dt.strftime('%Y-%m-%dT%H:%M')}"

    def format_line(self) -> str:
        day_str = self.start_dt.strftime("%a %Y-%m-%d %H:%M")
        return f"[{self.institution}] {day_str} — {self.office_name}"


class LPDClient:
    """Client for BMI ETV SVA (Landespolizeidirektion Wien)."""

    def __init__(self, bundesland: int = 9, tag_id: int = 6, delay_sec: float = 0.35):
        self.bundesland = bundesland
        self.tag_id = tag_id
        self.delay_sec = delay_sec
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": USER_AGENT})
        self.csrf: str = ""
        self.offices: list[dict[str, Any]] = []

    def _headers(self) -> dict[str, str]:
        return {
            "FNS-CSRFVALUE": self.csrf,
            "Accept": "application/json",
            "FNS-LOCALE": "de",
        }

    def init_session(self) -> None:
        self.session.cookies.clear()
        r1 = self.session.get(LPD_BASE_URL + "Terminvereinbarung", timeout=20)
        r1.raise_for_status()
        m = re.search(r'name="applID"\s+value="([^"]+)"', r1.text)
        if not m:
            raise RuntimeError("Could not extract applID from BMI ETV landing page")
        r2 = self.session.post(
            LPD_BASE_URL + "Terminvereinbarung",
            data={"applID": m.group(1)},
            timeout=20,
        )
        r2.raise_for_status()
        self.csrf = self.session.cookies.get("FNS-CSRF", "")
        if not self.csrf:
            raise RuntimeError("FNS-CSRF cookie missing after BMI ETV session init")

        # Initialize server state
        ri = self.session.get(
            LPD_BASE_URL + "api/init", headers=self._headers(), timeout=20
        )
        ri.raise_for_status()

        # Fetch offices for bundesland + tag_id
        rs = self.session.get(
            f"{LPD_BASE_URL}api/stellen/{self.bundesland}/{self.tag_id}",
            headers=self._headers(),
            timeout=20,
        )
        rs.raise_for_status()
        data = rs.json()
        self.offices = data.get("stellen", [])
        logging.info("LPD session initialized: %d offices found", len(self.offices))

    def _shorten_office_name(self, name: str) -> str:
        name = name.replace("Landespolizeidirektion Wien ", "LPD Wien ")
        name = name.replace(" - Verwaltungspolizei und innere Organisation", "")
        return name.strip()

    def fetch_slots(
        self, office_ids_filter: list[int] | None = None
    ) -> tuple[list[AppointmentSlot], dict[str, datetime.datetime | None]]:
        if not self.csrf or not self.offices:
            self.init_session()

        offices = self.offices
        if office_ids_filter:
            allowed = set(office_ids_filter)
            offices = [o for o in offices if o["id"] in allowed]

        slots: list[AppointmentSlot] = []
        earliest_by_office: dict[str, datetime.datetime | None] = {}

        for idx, office in enumerate(offices):
            oid = office["id"]
            oname = self._shorten_office_name(office["name"])
            payload = {
                "bundesland": self.bundesland,
                "stelle": oid,
                "anzahl": 1,
                "tagId": self.tag_id,
            }
            resp = self.session.post(
                LPD_BASE_URL + "api/termine",
                headers={**self._headers(), "Content-Type": "application/json"},
                json=payload,
                timeout=20,
            )
            if resp.status_code in (401, 403):
                logging.warning("LPD session expired (HTTP %s), re-initializing...", resp.status_code)
                self.init_session()
                resp = self.session.post(
                    LPD_BASE_URL + "api/termine",
                    headers={**self._headers(), "Content-Type": "application/json"},
                    json=payload,
                    timeout=20,
                )
            resp.raise_for_status()

            raw = resp.json()
            items = raw.get("termine", []) if isinstance(raw, dict) else raw
            office_dts: list[datetime.datetime] = []
            for item in items:
                ts = item.get("termin") if isinstance(item, dict) else None
                if not ts:
                    continue
                dt = datetime.datetime.fromisoformat(ts).replace(tzinfo=None)
                office_dts.append(dt)
                slots.append(
                    AppointmentSlot(
                        institution="LPD",
                        office_name=oname,
                        office_id=oid,
                        start_dt=dt,
                        booking_url=LPD_BOOKING_URL,
                    )
                )
            earliest_by_office[oname] = min(office_dts) if office_dts else None
            if idx < len(offices) - 1:
                time.sleep(self.delay_sec)

        return slots, earliest_by_office


class FinanzamtClient:
    """Client for Finanzamt Österreich (BMF Terminvereinbarung)."""

    def __init__(self, skill_id: int = 54, duration: int = 10, delay_sec: float = 1.0):
        self.skill_id = skill_id
        self.duration = duration
        self.delay_sec = delay_sec
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": USER_AGENT})

    def _discover_vienna_locations(self) -> dict[int, str]:
        locations = dict(KNOWN_VIENNA_BMF_LOCATIONS)
        try:
            resp = self.session.get(
                BMF_API_BASE + "GetLocations",
                params={
                    "calledFromWeb": "true",
                    "lang": "de",
                    "skillID": str(self.skill_id),
                    "barrierFree": "false",
                    "mediaType": "ON_SITE",
                },
                timeout=20,
            )
            if resp.status_code == 200 and resp.text.strip():
                data = resp.json()
                for loc in data.get("locations", []):
                    if loc.get("bundesland") == "W":
                        lid = int(loc["id"])
                        locations[lid] = (
                            f"Finanzamt {loc.get('name')} "
                            f"({loc.get('plz', '')}, {loc.get('adresse1', '')})"
                        )
        except Exception as exc:
            logging.warning("BMF GetLocations discovery failed (using known Vienna IDs): %s", exc)
        return locations

    def fetch_slots(
        self,
        include_vienna: bool = True,
        extra_location_ids: list[int] | None = None,
        lookahead_days: int = 90,
    ) -> tuple[list[AppointmentSlot], dict[str, datetime.datetime | None]]:
        locations: dict[int, str] = {}
        if include_vienna:
            locations.update(self._discover_vienna_locations())
        for lid in extra_location_ids or []:
            if lid not in locations:
                locations[lid] = NEARBY_NOE_BMF_LOCATIONS.get(
                    lid, f"Finanzamt Location #{lid}"
                )

        today = datetime.date.today()
        end_date = today + datetime.timedelta(days=lookahead_days)

        slots: list[AppointmentSlot] = []
        earliest_by_office: dict[str, datetime.datetime | None] = {}

        loc_items = list(locations.items())
        for idx, (lid, lname) in enumerate(loc_items):
            if idx > 0:
                time.sleep(self.delay_sec)
            resp = self.session.get(
                BMF_API_BASE + "GetFreeSlots",
                params={
                    "calledFromWeb": "true",
                    "lang": "de",
                    "skillID": str(self.skill_id),
                    "locationID": str(lid),
                    "duration": str(self.duration),
                    "startDate": today.strftime("%Y-%m-%d"),
                    "endDate": end_date.strftime("%Y-%m-%d"),
                    "mediaType": "ON_SITE",
                },
                timeout=20,
            )
            if resp.status_code == 403:
                logging.warning("BMF rate-limited (HTTP 403) on %s; backing off", lname)
                earliest_by_office[lname] = None
                time.sleep(5)
                continue
            resp.raise_for_status()
            data = resp.json()
            raw_slots = data.get("timeslots") or []
            office_dts: list[datetime.datetime] = []
            for item in raw_slots:
                st = item.get("startTime")
                if not st:
                    continue
                dt = datetime.datetime.strptime(st, "%Y-%m-%d %H:%M:%S")
                office_dts.append(dt)
                slots.append(
                    AppointmentSlot(
                        institution="Finanzamt",
                        office_name=lname,
                        office_id=lid,
                        start_dt=dt,
                        booking_url=BMF_BOOKING_URL,
                    )
                )
            earliest_by_office[lname] = min(office_dts) if office_dts else None

        return slots, earliest_by_office


def is_slot_matching(
    slot: AppointmentSlot,
    exclude_start: datetime.date | None,
    exclude_end: datetime.date | None,
    max_date: datetime.date | None,
) -> bool:
    d = slot.start_dt.date()
    if d < datetime.date.today():
        return False
    if exclude_start and exclude_end and exclude_start <= d <= exclude_end:
        return False
    if max_date and d > max_date:
        return False
    return True


def notify_windows_beep() -> None:
    if os.name != "nt":
        return
    try:
        import winsound

        for freq, dur in [(880, 180), (1175, 180), (1568, 300)]:
            winsound.Beep(freq, dur)
    except Exception:
        pass


def notify_windows_toast(title: str, body: str) -> None:
    if os.name != "nt":
        return
    # Escape single quotes for PowerShell
    t_esc = title.replace("'", "''")
    b_esc = body.replace("'", "''").replace("\n", " | ")[:220]
    ps_script = (
        "[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, "
        "ContentType = WindowsRuntime] > $null; "
        "$template = [Windows.UI.Notifications.ToastNotificationManager]::GetTemplateContent("
        "[Windows.UI.Notifications.ToastTemplateType]::ToastText02); "
        "$text = $template.GetElementsByTagName('text'); "
        f"$text[0].AppendChild($template.CreateTextNode('{t_esc}')) > $null; "
        f"$text[1].AppendChild($template.CreateTextNode('{b_esc}')) > $null; "
        "$toast = [Windows.UI.Notifications.ToastNotification]::new($template); "
        "$notifier = [Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier('ID Austria Monitor'); "
        "$notifier.Show($toast)"
    )
    try:
        subprocess.run(
            ["powershell", "-NoProfile", "-Command", ps_script],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=10,
        )
    except Exception as exc:
        logging.debug("Windows toast failed: %s", exc)


def notify_email(cfg: dict[str, Any], subject: str, body: str) -> None:
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = cfg.get("from_addr") or cfg["username"]
    msg["To"] = cfg["to_addr"]
    msg.set_content(body)

    with smtplib.SMTP(cfg["smtp_host"], int(cfg.get("smtp_port", 587)), timeout=20) as smtp:
        smtp.starttls()
        smtp.login(cfg["username"], cfg["password"])
        smtp.send_message(msg)
    logging.info("Email notification sent to %s", cfg["to_addr"])


def notify_ntfy(cfg: dict[str, Any], title: str, body: str) -> None:
    requests.post(
        cfg["topic_url"],
        data=body.encode("utf-8"),
        headers={"Title": title, "Priority": "high", "Tags": "calendar,rotating_light"},
        timeout=15,
    ).raise_for_status()
    logging.info("ntfy notification sent")


def notify_telegram(cfg: dict[str, Any], text: str) -> None:
    url = f"https://api.telegram.org/bot{cfg['bot_token']}/sendMessage"
    requests.post(
        url,
        json={"chat_id": cfg["chat_id"], "text": text, "disable_web_page_preview": True},
        timeout=15,
    ).raise_for_status()
    logging.info("Telegram notification sent")


def notify_discord(cfg: dict[str, Any], text: str) -> None:
    requests.post(
        cfg["webhook_url"],
        json={"content": text[:1950]},
        timeout=15,
    ).raise_for_status()
    logging.info("Discord notification sent")


def send_notifications(cfg: dict[str, Any], new_slots: list[AppointmentSlot]) -> None:
    notif = cfg.get("notifications", {})
    sorted_slots = sorted(new_slots, key=lambda s: s.start_dt)
    title = f"ID Austria Slot Found! ({len(sorted_slots)} new)"

    lines = [
        f"Found {len(sorted_slots)} new matching ID Austria appointment slot(s):",
        "",
    ]
    for s in sorted_slots[:15]:
        lines.append(f"  - {s.format_line()}")
    if len(sorted_slots) > 15:
        lines.append(f"  ... and {len(sorted_slots) - 15} more")

    lines.append("")
    lines.append("Book immediately:")
    if any(s.institution == "LPD" for s in sorted_slots):
        lines.append(f"  LPD Wien:  {LPD_BOOKING_URL}")
    if any(s.institution == "Finanzamt" for s in sorted_slots):
        lines.append(f"  Finanzamt: {BMF_BOOKING_URL}")

    body = "\n".join(lines)

    if notif.get("console", True):
        print("\n" + "=" * 72)
        print(body)
        print("=" * 72 + "\n", flush=True)

    if notif.get("windows_beep", True):
        notify_windows_beep()

    if notif.get("windows_toast", True):
        first = sorted_slots[0].format_line()
        notify_windows_toast(title, first)

    email_cfg = notif.get("email", {})
    if email_cfg.get("enabled"):
        try:
            notify_email(email_cfg, title, body)
        except Exception as exc:
            logging.error("Failed to send email: %s", exc)

    ntfy_cfg = notif.get("ntfy", {})
    if ntfy_cfg.get("enabled") and ntfy_cfg.get("topic_url"):
        try:
            notify_ntfy(ntfy_cfg, title, body)
        except Exception as exc:
            logging.error("Failed to send ntfy: %s", exc)

    tg_cfg = notif.get("telegram", {})
    if tg_cfg.get("enabled") and tg_cfg.get("bot_token") and tg_cfg.get("chat_id"):
        try:
            notify_telegram(tg_cfg, body)
        except Exception as exc:
            logging.error("Failed to send Telegram: %s", exc)

    dc_cfg = notif.get("discord", {})
    if dc_cfg.get("enabled") and dc_cfg.get("webhook_url"):
        try:
            notify_discord(dc_cfg, body)
        except Exception as exc:
            logging.error("Failed to send Discord: %s", exc)


def load_config(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def parse_date(val: str | None) -> datetime.date | None:
    if not val:
        return None
    return datetime.date.fromisoformat(val)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Monitor ID Austria appointment availability (LPD Wien + Finanzamt Österreich)"
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=Path(__file__).with_name("config.json"),
        help="Path to config.json",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="Run a single check, print summary table, and exit",
    )
    parser.add_argument(
        "--test-notify",
        action="store_true",
        help="Send a test notification through all enabled channels and exit",
    )
    parser.add_argument(
        "--max-date",
        type=str,
        default=None,
        help="Override max_date filter (YYYY-MM-DD)",
    )
    parser.add_argument(
        "--include-nearby",
        action="store_true",
        help="Also check nearby NÖ Finanzämter (Mödling, Tulln, Bruck an der Leitha)",
    )
    parser.add_argument(
        "--state-file",
        type=Path,
        default=None,
        help="Optional JSON file to persist seen slot keys across --once runs (e.g. GitHub Actions)",
    )
    parser.add_argument(
        "--interval",
        type=int,
        default=None,
        help="Override poll_interval_seconds from config.json",
    )
    parser.add_argument(
        "--duration-seconds",
        type=int,
        default=None,
        help="Run continuously for at most this many seconds, then exit cleanly (useful for CI/Actions)",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    if not args.config.exists():
        logging.error("Config file not found: %s", args.config)
        return 1

    cfg = load_config(args.config)

    env_ntfy = os.environ.get("NTFY_TOPIC_URL")
    if env_ntfy:
        cfg.setdefault("notifications", {}).setdefault("ntfy", {})
        cfg["notifications"]["ntfy"]["enabled"] = True
        cfg["notifications"]["ntfy"]["topic_url"] = env_ntfy

    if args.test_notify:
        dummy = AppointmentSlot(
            institution="LPD",
            office_name="LPD Wien PK Margareten (TEST)",
            office_id=2282,
            start_dt=datetime.datetime.now() + datetime.timedelta(days=1),
            booking_url=LPD_BOOKING_URL,
        )
        send_notifications(cfg, [dummy])
        return 0

    ex = cfg.get("exclude_dates", {})
    exclude_start = parse_date(ex.get("start"))
    exclude_end = parse_date(ex.get("end"))
    max_date = parse_date(args.max_date or cfg.get("max_date"))
    poll_interval = int(args.interval or cfg.get("poll_interval_seconds", 90))
    run_deadline = (
        time.monotonic() + args.duration_seconds if args.duration_seconds else None
    )

    lpd_cfg = cfg.get("lpd", {})
    fa_cfg = cfg.get("finanzamt", {})

    lpd_client = (
        LPDClient(
            bundesland=int(lpd_cfg.get("bundesland", 9)),
            tag_id=int(lpd_cfg.get("tag_id", 6)),
        )
        if lpd_cfg.get("enabled", True)
        else None
    )
    fa_client = (
        FinanzamtClient(skill_id=int(fa_cfg.get("skill_id", 54)))
        if fa_cfg.get("enabled", True)
        else None
    )

    extra_fa_ids = list(fa_cfg.get("extra_location_ids", []))
    if args.include_nearby:
        for lid in NEARBY_NOE_BMF_LOCATIONS:
            if lid not in extra_fa_ids:
                extra_fa_ids.append(lid)

    seen_keys: set[str] = set()
    if args.state_file and args.state_file.exists():
        try:
            loaded = json.loads(args.state_file.read_text(encoding="utf-8"))
            if isinstance(loaded, list):
                seen_keys = set(str(x) for x in loaded)
                logging.info("Loaded %d seen slot(s) from %s", len(seen_keys), args.state_file)
        except Exception as exc:
            logging.warning("Could not load state file %s: %s", args.state_file, exc)

    log_path = Path(__file__).with_name("found_slots.log")

    logging.info(
        "Starting ID Austria Monitor | exclude=%s..%s | max_date=%s | interval=%ds",
        exclude_start,
        exclude_end,
        max_date or "none",
        poll_interval,
    )

    cycle = 0
    while True:
        cycle += 1
        all_slots: list[AppointmentSlot] = []
        earliest_summary: dict[str, datetime.datetime | None] = {}

        if lpd_client:
            try:
                lpd_slots, lpd_earliest = lpd_client.fetch_slots(
                    office_ids_filter=lpd_cfg.get("office_ids") or None
                )
                all_slots.extend(lpd_slots)
                earliest_summary.update(lpd_earliest)
            except Exception as exc:
                logging.error("LPD check failed: %s", exc)

        if fa_client:
            try:
                fa_slots, fa_earliest = fa_client.fetch_slots(
                    include_vienna=bool(fa_cfg.get("include_vienna", True)),
                    extra_location_ids=extra_fa_ids,
                    lookahead_days=int(fa_cfg.get("lookahead_days", 90)),
                )
                all_slots.extend(fa_slots)
                earliest_summary.update(fa_earliest)
            except Exception as exc:
                logging.error("Finanzamt check failed: %s", exc)

        matching = [
            s
            for s in all_slots
            if is_slot_matching(s, exclude_start, exclude_end, max_date)
        ]
        new_matches = [s for s in matching if s.key not in seen_keys]

        if args.once or cycle == 1:
            print("\n--- Earliest Available Slot per Office ---")
            for office, dt in sorted(
                earliest_summary.items(),
                key=lambda kv: (kv[1] is None, kv[1] or datetime.datetime.max),
            ):
                dt_str = dt.strftime("%a %Y-%m-%d %H:%M") if dt else "No open slots"
                print(f"  {office:<48} {dt_str}")
            print(
                f"\nMatching slots (excluding {exclude_start}..{exclude_end}, "
                f"<= {max_date or 'any'}): {len(matching)}\n",
                flush=True,
            )

        if new_matches:
            for s in new_matches:
                seen_keys.add(s.key)
            with log_path.open("a", encoding="utf-8") as lf:
                ts = datetime.datetime.now().isoformat(timespec="seconds")
                for s in sorted(new_matches, key=lambda x: x.start_dt):
                    lf.write(f"{ts} | {s.format_line()} | {s.booking_url}\n")
            send_notifications(cfg, new_matches)
        else:
            logging.info(
                "Cycle #%d complete: %d total slots fetched, %d matching, 0 new.",
                cycle,
                len(all_slots),
                len(matching),
            )

        if args.state_file:
            # Keep only currently open matching slots so re-opened cancellations alert again
            current_matching_keys = {s.key for s in matching}
            seen_keys = (seen_keys & current_matching_keys) | {s.key for s in new_matches}
            try:
                args.state_file.write_text(
                    json.dumps(sorted(seen_keys), indent=2), encoding="utf-8"
                )
            except Exception as exc:
                logging.warning("Could not save state file %s: %s", args.state_file, exc)

        if args.once:
            return 0

        if run_deadline is not None:
            remaining = run_deadline - time.monotonic()
            if remaining <= 0:
                logging.info("Reached duration limit (%ds); exiting cleanly.", args.duration_seconds)
                return 0
            sleep_sec = min(poll_interval, remaining)
        else:
            sleep_sec = poll_interval

        try:
            time.sleep(sleep_sec)
        except KeyboardInterrupt:
            logging.info("Stopped by user.")
            return 0

        if run_deadline is not None and time.monotonic() >= run_deadline:
            logging.info("Reached duration limit (%ds); exiting cleanly.", args.duration_seconds)
            return 0


if __name__ == "__main__":
    sys.exit(main())
