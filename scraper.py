#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Збір даних про затримки потягів з табло «Що з моїм поїздом?» АТ «Укрзалізниця».
Джерело: https://uz-vezemo.uz.gov.ua/delayform

Що робить один запуск:
  1. Забирає сторінку одним HTTP-запитом (усі дані, включно з переліком станцій,
     вже є в HTML — клік на сайті лише показує приховані рядки).
  2. Фільтрує рейси за списком станцій WATCH — окремо позначає, чи станція є
     кінцевою (endpoint), чи потяг просто проходить через неї (transit).
  3. Дописує нові рядки в data/snapshots.csv і data/stops.csv ТІЛЬКИ якщо
     щось змінилося з попереднього запуску (щоб не роздувати файли).
  4. Перезаписує data/trips.csv — по одному рядку на рейс, з поточним станом.
  5. Дописує рядок у data/run_log.csv — щоб було видно, чи запуск взагалі стався.
"""

import csv
import json
import os
import sys
import hashlib
import traceback
from datetime import datetime, timezone

import requests
from bs4 import BeautifulSoup

try:
    from zoneinfo import ZoneInfo
    KYIV = ZoneInfo("Europe/Kyiv")
except Exception:              # на випадок відсутності tzdata
    KYIV = timezone.utc

# ---------------------------------------------------------------- НАЛАШТУВАННЯ

URL = "https://uz-vezemo.uz.gov.ua/delayform"

# Ключові слова для пошуку станцій. Пишуться ВЕЛИКИМИ літерами, бо порівняння
# йде з нормалізованою назвою. "КИЇВ" зловить і КИЇВ-ПАС., і КИЇВ-ДЕМІЇВСЬКИЙ.
WATCH = [
    "КИЇВ",
    "ХАРКІВ",
    "ЛЬВІВ",
    "ДНІПРО",
    "ОДЕСА",
    "ПОЛТАВА",
]

# True — зберігати геть усі потяги з табло, ігноруючи WATCH.
KEEP_ALL = False

DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
SNAPSHOTS_CSV = os.path.join(DATA_DIR, "snapshots.csv")
STOPS_CSV = os.path.join(DATA_DIR, "stops.csv")
TRIPS_CSV = os.path.join(DATA_DIR, "trips.csv")
RUN_LOG_CSV = os.path.join(DATA_DIR, "run_log.csv")
STATE_JSON = os.path.join(DATA_DIR, "state.json")

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (compatible; uz-delay-research/1.0; "
        "research project, contact via GitHub repo)"
    ),
    "Accept-Language": "uk-UA,uk;q=0.9",
}

DASHES = {"", "—", "–", "-", "−"}

# ------------------------------------------------------------------ ДОПОМІЖНЕ


def norm(text):
    """Нормалізує назву станції: прибирає нерозривні пробіли, стрілки, регістр."""
    if text is None:
        return ""
    text = text.replace("\xa0", " ").replace("\u2192", " ").replace("→", " ")
    return " ".join(text.split()).upper()


def parse_hm(text):
    """'+11:00' -> 660 ; '-0:04' -> -4 ; '—' -> None."""
    if text is None:
        return None
    text = text.replace("\xa0", " ").strip()
    if text in DASHES:
        return None
    sign = -1 if text.startswith("-") else 1
    body = text.lstrip("+-").strip()
    if ":" not in body:
        return None
    h, m = body.split(":", 1)
    try:
        return sign * (int(h) * 60 + int(m))
    except ValueError:
        return None


def clean(text):
    """Текст комірки: нерозривні пробіли -> звичайні, тире -> порожній рядок."""
    if text is None:
        return ""
    text = " ".join(text.replace("\xa0", " ").split())
    return "" if text in DASHES else text


def digest(obj):
    return hashlib.sha1(
        json.dumps(obj, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()[:16]


def matches_watch(station_norm):
    return any(key in station_norm for key in WATCH)


# -------------------------------------------------------------------- ПАРСИНГ


def parse_stops(main_row):
    """Станції рейсу: приховані рядки .delay-row__detail одразу після основного."""
    stops = []
    node = main_row.find_next_sibling("tr")
    seq = 0
    while node is not None and "delay-row__detail" in (node.get("class") or []):
        station_el = node.select_one(".delay-detail__station")
        if station_el is not None:
            classes = node.get("class") or []
            stops.append(
                {
                    "seq": seq,
                    "station": clean(station_el.get_text()),
                    "dev_min": parse_hm(
                        node.select_one(".delay-detail__dev").get_text()
                        if node.select_one(".delay-detail__dev")
                        else None
                    ),
                    "forecast": clean(
                        node.select_one(".delay-detail__fc").get_text()
                        if node.select_one(".delay-detail__fc")
                        else None
                    ),
                    "scheduled": clean(
                        node.select_one(".delay-detail__sched").get_text()
                        if node.select_one(".delay-detail__sched")
                        else None
                    ),
                    "passed": "delay-row__detail--passed" in classes,
                }
            )
            seq += 1
        node = node.find_next_sibling("tr")
    return stops


def badge_text(td):
    if td is None:
        return ""
    badge = td.select_one(".badge")
    return clean((badge or td).get_text())


def parse_row(tr):
    tds = tr.find_all("td", recursive=False)
    if len(tds) < 8:
        return None

    num_el = tr.select_one(".js-delay-train-number")
    if num_el is None:
        return None

    date_el = tr.select_one(".supply-table-departure-date")
    conn = tr.select_one(".js-delay-connections")
    from_el = conn.select_one(".supply-table-connections__from") if conn else None
    to_el = conn.select_one(".supply-table-connections__to") if conn else None

    station_from = clean(from_el.get_text()).rstrip("→ ").strip() if from_el else ""
    station_to = clean(to_el.get_text()) if to_el else ""

    stops = parse_stops(tr)

    # Тип збігу: кінцева станція / проміжна / обидва
    endpoint_hit = matches_watch(norm(station_from)) or matches_watch(norm(station_to))
    transit_hit = any(matches_watch(norm(s["station"])) for s in stops)
    if endpoint_hit and transit_hit:
        match_type = "both"
    elif endpoint_hit:
        match_type = "endpoint"
    elif transit_hit:
        match_type = "transit"
    else:
        match_type = ""

    dep_date = clean(date_el.get_text()) if date_el else ""

    return {
        "train_number": clean(num_el.get_text()),
        "departure_date": dep_date,
        "station_from": station_from,
        "station_to": station_to,
        "delay_min": parse_hm(tds[2].get_text()),
        "forecast_arrival": clean(tds[3].get_text()),
        "planned_arrival": clean(tds[4].get_text()),
        "status": badge_text(tds[5]),
        "reliability": badge_text(tds[6]),
        "reason": clean(tds[7].get_text()),
        "has_route": "delay-row--has-route" in (tr.get("class") or []),
        "n_stops": len(stops),
        "match_type": match_type,
        "stops": stops,
        "trip_key": "|".join(
            [clean(num_el.get_text()), station_from, station_to, dep_date]
        ),
    }


def parse_page(html):
    soup = BeautifulSoup(html, "html.parser")
    upd = soup.select_one(".supply-main-last-update")
    page_updated = clean(upd.get_text()) if upd else ""
    rows = []
    for tr in soup.select("tr.delay-row"):
        row = parse_row(tr)
        if row is not None:
            rows.append(row)
    return page_updated, rows


# ---------------------------------------------------------------------- ЗАПИС

SNAP_FIELDS = [
    "snapshot_ts_kyiv", "trip_key", "train_number", "departure_date",
    "station_from", "station_to", "delay_min", "forecast_arrival",
    "planned_arrival", "status", "reliability", "reason",
    "match_type", "has_route", "n_stops", "page_updated",
]

STOP_FIELDS = [
    "snapshot_ts_kyiv", "trip_key", "seq", "station",
    "dev_min", "forecast", "scheduled", "passed", "is_watched",
]

TRIP_FIELDS = [
    "trip_key", "train_number", "departure_date", "station_from", "station_to",
    "match_type", "first_seen_kyiv", "last_seen_kyiv", "n_updates",
    "last_delay_min", "max_delay_min", "last_status", "last_reliability",
    "last_reason", "n_stops",
]

RUN_FIELDS = [
    "run_ts_kyiv", "run_ts_utc", "ok", "error", "page_updated",
    "rows_total", "rows_kept", "snapshots_added", "stops_added",
]


def append_rows(path, fields, rows):
    if not rows:
        return 0
    new_file = not os.path.exists(path)
    with open(path, "a", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        if new_file:
            w.writeheader()
        for r in rows:
            w.writerow(r)
    return len(rows)


def read_trips():
    if not os.path.exists(TRIPS_CSV):
        return {}
    out = {}
    with open(TRIPS_CSV, newline="", encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            out[row["trip_key"]] = row
    return out


def write_trips(trips):
    with open(TRIPS_CSV, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=TRIP_FIELDS, extrasaction="ignore")
        w.writeheader()
        for key in sorted(trips):
            w.writerow(trips[key])


def load_state():
    if os.path.exists(STATE_JSON):
        try:
            with open(STATE_JSON, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}
    return {}


def save_state(state):
    with open(STATE_JSON, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=0, sort_keys=True)


# ----------------------------------------------------------------------- MAIN


def main():
    os.makedirs(DATA_DIR, exist_ok=True)
    now = datetime.now(KYIV)
    ts = now.strftime("%Y-%m-%d %H:%M:%S")
    ts_utc = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")

    run = {
        "run_ts_kyiv": ts, "run_ts_utc": ts_utc, "ok": 0, "error": "",
        "page_updated": "", "rows_total": 0, "rows_kept": 0,
        "snapshots_added": 0, "stops_added": 0,
    }

    try:
        resp = requests.get(URL, headers=HEADERS, timeout=30)
        resp.raise_for_status()
        page_updated, rows = parse_page(resp.content)
    except Exception as exc:
        run["error"] = f"{type(exc).__name__}: {exc}"[:300]
        append_rows(RUN_LOG_CSV, RUN_FIELDS, [run])
        print("ПОМИЛКА:", run["error"], file=sys.stderr)
        traceback.print_exc()
        return 1

    run["ok"] = 1
    run["page_updated"] = page_updated
    run["rows_total"] = len(rows)

    kept = [r for r in rows if KEEP_ALL or r["match_type"]]
    run["rows_kept"] = len(kept)

    old_state = load_state()
    new_state = {}
    trips = read_trips()
    snap_rows, stop_rows = [], []

    for r in kept:
        key = r["trip_key"]

        main_fingerprint = digest(
            [r["delay_min"], r["forecast_arrival"], r["planned_arrival"],
             r["status"], r["reliability"], r["reason"]]
        )
        stops_fingerprint = digest(r["stops"])
        new_state[key] = {"main": main_fingerprint, "stops": stops_fingerprint}
        prev = old_state.get(key, {})

        if prev.get("main") != main_fingerprint:
            snap = {k: r.get(k) for k in SNAP_FIELDS}
            snap["snapshot_ts_kyiv"] = ts
            snap["page_updated"] = page_updated
            snap["has_route"] = int(r["has_route"])
            snap_rows.append(snap)

        if r["stops"] and prev.get("stops") != stops_fingerprint:
            for s in r["stops"]:
                stop_rows.append({
                    "snapshot_ts_kyiv": ts,
                    "trip_key": key,
                    "seq": s["seq"],
                    "station": s["station"],
                    "dev_min": s["dev_min"],
                    "forecast": s["forecast"],
                    "scheduled": s["scheduled"],
                    "passed": int(s["passed"]),
                    "is_watched": int(matches_watch(norm(s["station"]))),
                })

        # ---- зведена таблиця рейсів (перезапис одного рядка на рейс)
        prev_trip = trips.get(key)
        delay = r["delay_min"]
        if prev_trip is None:
            trips[key] = {
                "trip_key": key,
                "train_number": r["train_number"],
                "departure_date": r["departure_date"],
                "station_from": r["station_from"],
                "station_to": r["station_to"],
                "match_type": r["match_type"],
                "first_seen_kyiv": ts,
                "last_seen_kyiv": ts,
                "n_updates": 1,
                "last_delay_min": delay,
                "max_delay_min": delay,
                "last_status": r["status"],
                "last_reliability": r["reliability"],
                "last_reason": r["reason"],
                "n_stops": r["n_stops"],
            }
        else:
            try:
                prev_max = int(prev_trip.get("max_delay_min") or 0)
            except ValueError:
                prev_max = 0
            prev_trip.update({
                "last_seen_kyiv": ts,
                "n_updates": int(prev_trip.get("n_updates") or 0) + 1,
                "last_delay_min": delay,
                "max_delay_min": max(prev_max, delay if delay is not None else prev_max),
                "last_status": r["status"],
                "last_reliability": r["reliability"],
                "last_reason": r["reason"],
                "n_stops": r["n_stops"],
                "match_type": r["match_type"],
            })

    run["snapshots_added"] = append_rows(SNAPSHOTS_CSV, SNAP_FIELDS, snap_rows)
    run["stops_added"] = append_rows(STOPS_CSV, STOP_FIELDS, stop_rows)
    write_trips(trips)
    save_state(new_state)
    append_rows(RUN_LOG_CSV, RUN_FIELDS, [run])

    print(
        f"{ts} | на сторінці: {run['rows_total']} | відібрано: {run['rows_kept']} "
        f"| нових знімків: {run['snapshots_added']} | рядків станцій: {run['stops_added']}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
