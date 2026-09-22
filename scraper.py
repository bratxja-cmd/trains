#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Збір даних про затримки потягів з табло «Що з моїм поїздом?» АТ «Укрзалізниця».
Джерело: https://uz-vezemo.uz.gov.ua/delayform

Версія 2. Зміни проти першої:
  * РЕЙСИ-ПРИМІРНИКИ (instance_id). Той самий номер потяга на тому самому
    маршруті їздить щодня, а дата відправлення на сайті показується не
    завжди. Тому новий примірник визначається не датою, а трьома ознаками:
    рейс зник із табло надовго і повернувся / затримка різко впала /
    перелік пройдених станцій «відкотився» на початок маршруту.
    Завдяки цьому вчорашній рейс не перезаписується сьогоднішнім.
  * Захист від формул у CSV (значення, що починаються з = + @, екрануються).
  * max_delay_min коректно працює з від'ємними значеннями та порожнечею.
  * n_updates перейменовано на n_seen — воно рахує саме появи в табло.
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
    TZ_OK = True
except Exception:              # немає tzdata — працюємо, але чесно це фіксуємо
    KYIV = timezone.utc
    TZ_OK = False

# ---------------------------------------------------------------- НАЛАШТУВАННЯ

URL = "https://uz-vezemo.uz.gov.ua/delayform"

# Ключові слова для пошуку станцій. ВЕЛИКИМИ літерами.
# "КИЇВ" зловить і КИЇВ-ПАС., і КИЇВ-ДЕМІЇВСЬКИЙ.
WATCH = [
    "КИЇВ",
    "ХАРКІВ",
    "ЛЬВІВ",
    "ДНІПРО",
    "ОДЕСА",
    "ПОЛТАВА",
]

# True — зберігати геть усі потяги з табло, ігноруючи WATCH.
# На час пілота рекомендовано True: даних небагато, зате фільтр можна буде
# переграти заднім числом, нічого не втративши.
KEEP_ALL = True

# --- параметри визначення нового примірника рейсу
GAP_HOURS = 4          # зник із табло довше, ніж на стільки годин -> новий рейс
DELAY_DROP_MIN = 90    # затримка впала більше ніж на стільки хвилин -> новий рейс
RETENTION_HOURS = 60   # скільки тримати в пам'яті рейси, яких зараз немає в табло

DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
SNAPSHOTS_CSV = os.path.join(DATA_DIR, "snapshots.csv")
STOPS_CSV = os.path.join(DATA_DIR, "stops.csv")
TRIPS_CSV = os.path.join(DATA_DIR, "trips.csv")
RUN_LOG_CSV = os.path.join(DATA_DIR, "run_log.csv")
STATE_JSON = os.path.join(DATA_DIR, "state.json")

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (compatible; uz-delay-research/2.0; "
        "non-commercial research project)"
    ),
    "Accept-Language": "uk-UA,uk;q=0.9",
}

DASHES = {"", "—", "–", "-", "−"}

# ------------------------------------------------------------------ ДОПОМІЖНЕ


def norm(text):
    """Нормалізує назву станції: нерозривні пробіли, стрілки, верхній регістр."""
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


def csv_safe(value):
    """Захист від формул: Excel виконує значення, що починається з = + @ тощо."""
    if isinstance(value, str) and value[:1] in ("=", "+", "@", "\t", "\r"):
        return "'" + value
    return value


def digest(obj):
    return hashlib.sha1(
        json.dumps(obj, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()[:16]


def matches_watch(station_norm):
    return any(key in station_norm for key in WATCH)


def as_int(value, default=None):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


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
            dev_el = node.select_one(".delay-detail__dev")
            fc_el = node.select_one(".delay-detail__fc")
            sched_el = node.select_one(".delay-detail__sched")
            stops.append({
                "seq": seq,
                "station": clean(station_el.get_text()),
                "dev_min": parse_hm(dev_el.get_text() if dev_el else None),
                "forecast": clean(fc_el.get_text() if fc_el else None),
                "scheduled": clean(sched_el.get_text() if sched_el else None),
                "passed": "delay-row__detail--passed" in classes,
            })
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
    passed_count = sum(1 for s in stops if s["passed"])

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

    train_number = clean(num_el.get_text())
    dep_date = clean(date_el.get_text()) if date_el else ""

    return {
        "train_number": train_number,
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
        "passed_count": passed_count,
        "match_type": match_type,
        "stops": stops,
        "trip_key": "|".join([train_number, station_from, station_to, dep_date]),
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


# ------------------------------------------------- ВИЗНАЧЕННЯ ПРИМІРНИКА РЕЙСУ


def is_new_instance(prev, row, now_epoch):
    """Чи це новий рейс, а не продовження вже відомого? -> (bool, причина)."""
    if prev is None:
        return True, "first_seen"

    gap_h = (now_epoch - float(prev.get("last_seen_epoch") or 0)) / 3600.0
    if gap_h >= GAP_HOURS:
        return True, f"gap_{gap_h:.1f}h"

    prev_delay = prev.get("delay_min")
    cur_delay = row["delay_min"]
    if prev_delay is not None and cur_delay is not None:
        if prev_delay - cur_delay >= DELAY_DROP_MIN:
            return True, f"delay_drop_{prev_delay - cur_delay}"

    # маршрут «відкотився» на початок: пройдених станцій стало менше
    if row["n_stops"] and row["passed_count"] < (as_int(prev.get("passed_count"), 0) or 0):
        return True, "route_reset"

    return False, ""


def make_instance_id(trip_key, now):
    return f"{trip_key}@{now.strftime('%Y%m%d-%H%M')}"


# ---------------------------------------------------------------------- ЗАПИС

SNAP_FIELDS = [
    "snapshot_ts_kyiv", "instance_id", "trip_key", "train_number",
    "departure_date", "station_from", "station_to", "delay_min",
    "forecast_arrival", "planned_arrival", "status", "reliability", "reason",
    "match_type", "has_route", "n_stops", "passed_count", "page_updated",
]

STOP_FIELDS = [
    "snapshot_ts_kyiv", "instance_id", "trip_key", "seq", "station",
    "dev_min", "forecast", "scheduled", "passed", "is_watched",
]

TRIP_FIELDS = [
    "instance_id", "trip_key", "train_number", "departure_date",
    "station_from", "station_to", "match_type", "first_seen_kyiv",
    "last_seen_kyiv", "n_seen", "last_delay_min", "max_delay_min",
    "min_delay_min", "last_status", "last_reliability", "last_reason",
    "n_stops", "instance_reason",
]

RUN_FIELDS = [
    "run_ts_kyiv", "run_ts_utc", "ok", "error", "tz_ok", "page_updated",
    "rows_total", "rows_kept", "new_instances", "snapshots_added", "stops_added",
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
            w.writerow({k: csv_safe(v) for k, v in r.items()})
    return len(rows)


def read_trips():
    if not os.path.exists(TRIPS_CSV):
        return {}
    out = {}
    with open(TRIPS_CSV, newline="", encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            if row.get("instance_id"):
                out[row["instance_id"]] = row
    return out


def write_trips(trips):
    with open(TRIPS_CSV, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=TRIP_FIELDS, extrasaction="ignore")
        w.writeheader()
        for key in sorted(trips, key=lambda k: trips[k].get("first_seen_kyiv") or ""):
            w.writerow({k: csv_safe(v) for k, v in trips[key].items()})


def load_state():
    if os.path.exists(STATE_JSON):
        try:
            with open(STATE_JSON, encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict) and "trips" in data:
                return data
        except Exception:
            pass
    return {"trips": {}}


def save_state(state):
    with open(STATE_JSON, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=0, sort_keys=True)


# ----------------------------------------------------------------------- MAIN


def main():
    os.makedirs(DATA_DIR, exist_ok=True)
    now = datetime.now(KYIV)
    now_epoch = now.timestamp()
    ts = now.strftime("%Y-%m-%d %H:%M:%S")
    ts_utc = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")

    run = {
        "run_ts_kyiv": ts, "run_ts_utc": ts_utc, "ok": 0, "error": "",
        "tz_ok": int(TZ_OK), "page_updated": "", "rows_total": 0, "rows_kept": 0,
        "new_instances": 0, "snapshots_added": 0, "stops_added": 0,
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

    state = load_state()
    trips = read_trips()
    snap_rows, stop_rows = [], []
    seen_now = set()

    for r in kept:
        key = r["trip_key"]

        # Колізія в межах одного зрізу (той самий рейс двічі на сторінці):
        # другий екземпляр отримує власний ключ, щоб не затерти перший.
        while key in seen_now:
            key += "#"
        r["trip_key"] = key
        seen_now.add(key)

        prev = state["trips"].get(key)
        new_inst, reason = is_new_instance(prev, r, now_epoch)

        if new_inst:
            instance_id = make_instance_id(key, now)
            # страховка: якщо такий id вже існує (два примірники в межах
            # однієї хвилини), робимо його унікальним
            while instance_id in trips:
                instance_id += "b"
            run["new_instances"] += 1
        else:
            instance_id = prev.get("instance_id") or make_instance_id(key, now)

        main_fingerprint = digest([
            r["delay_min"], r["forecast_arrival"], r["planned_arrival"],
            r["status"], r["reliability"], r["reason"],
        ])
        stops_fingerprint = digest(r["stops"])

        # знімок пишемо, якщо це новий рейс АБО щось змінилось
        if new_inst or (prev or {}).get("main") != main_fingerprint:
            snap = {k: r.get(k) for k in SNAP_FIELDS}
            snap["snapshot_ts_kyiv"] = ts
            snap["instance_id"] = instance_id
            snap["page_updated"] = page_updated
            snap["has_route"] = int(r["has_route"])
            snap_rows.append(snap)

        if r["stops"] and (new_inst or (prev or {}).get("stops") != stops_fingerprint):
            for s in r["stops"]:
                stop_rows.append({
                    "snapshot_ts_kyiv": ts,
                    "instance_id": instance_id,
                    "trip_key": key,
                    "seq": s["seq"],
                    "station": s["station"],
                    "dev_min": s["dev_min"],
                    "forecast": s["forecast"],
                    "scheduled": s["scheduled"],
                    "passed": int(s["passed"]),
                    "is_watched": int(matches_watch(norm(s["station"]))),
                })

        # ---- зведена таблиця: рядок на ПРИМІРНИК рейсу
        delay = r["delay_min"]
        trip = trips.get(instance_id)
        if trip is None:
            trips[instance_id] = {
                "instance_id": instance_id,
                "trip_key": key,
                "train_number": r["train_number"],
                "departure_date": r["departure_date"],
                "station_from": r["station_from"],
                "station_to": r["station_to"],
                "match_type": r["match_type"],
                "first_seen_kyiv": ts,
                "last_seen_kyiv": ts,
                "n_seen": 1,
                "last_delay_min": delay,
                "max_delay_min": delay,
                "min_delay_min": delay,
                "last_status": r["status"],
                "last_reliability": r["reliability"],
                "last_reason": r["reason"],
                "n_stops": r["n_stops"],
                "instance_reason": reason,
            }
        else:
            prev_max = as_int(trip.get("max_delay_min"))
            prev_min = as_int(trip.get("min_delay_min"))
            new_max = prev_max if delay is None else (
                delay if prev_max is None else max(prev_max, delay))
            new_min = prev_min if delay is None else (
                delay if prev_min is None else min(prev_min, delay))
            trip.update({
                "last_seen_kyiv": ts,
                "n_seen": (as_int(trip.get("n_seen"), 0) or 0) + 1,
                "last_delay_min": delay,
                "max_delay_min": new_max,
                "min_delay_min": new_min,
                "last_status": r["status"],
                "last_reliability": r["reliability"],
                "last_reason": r["reason"],
                "n_stops": r["n_stops"],
                "match_type": r["match_type"],
            })

        state["trips"][key] = {
            "instance_id": instance_id,
            "last_seen_epoch": now_epoch,
            "main": main_fingerprint,
            "stops": stops_fingerprint,
            "delay_min": delay,
            "passed_count": r["passed_count"],
        }

    # прибираємо з пам'яті рейси, яких давно немає в табло
    cutoff = now_epoch - RETENTION_HOURS * 3600
    state["trips"] = {
        k: v for k, v in state["trips"].items()
        if float(v.get("last_seen_epoch") or 0) >= cutoff
    }

    run["snapshots_added"] = append_rows(SNAPSHOTS_CSV, SNAP_FIELDS, snap_rows)
    run["stops_added"] = append_rows(STOPS_CSV, STOP_FIELDS, stop_rows)
    write_trips(trips)
    save_state(state)
    append_rows(RUN_LOG_CSV, RUN_FIELDS, [run])

    print(
        f"{ts} | на сторінці: {run['rows_total']} | відібрано: {run['rows_kept']} "
        f"| нових рейсів: {run['new_instances']} "
        f"| знімків: {run['snapshots_added']} | рядків станцій: {run['stops_added']}"
    )
    if not TZ_OK:
        print("УВАГА: не вдалося завантажити Europe/Kyiv, час пишеться в UTC",
              file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
