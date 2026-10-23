"""Realtime overlay and departure board construction for a rail station or bus stop."""
from __future__ import annotations

import json
import logging
import math
import threading
import time
from bisect import bisect_left, bisect_right
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests
from google.transit import gtfs_realtime_pb2

from .gtfs_static import BUS, TRAIN, StaticIndex, TripStop
from .stock import is_ngr, is_three_car

log = logging.getLogger(__name__)

# The combined SEQ feed covers bus, ferry, train and tram. (Previously only .../TripUpdates/Rail.)
RT_URL = "https://gtfsrt.api.translink.com.au/api/realtime/SEQ/TripUpdates"
POLL_INTERVAL_S = 60
STALE_AFTER_S = 5 * 60
ROWS_PER_SECTION = 3
CITY_ROWS = 6  # Roma Street shows one list instead of two sections
BUS_ROWS = 6  # bus stops show one chronological list
# The 7th line of the Roma Street board is reserved for a scrolling message (no other board has
# one); the messages take turns each minute.
CITY_MESSAGES_PATH = Path(__file__).with_name("city_messages.json")
MESSAGE_ROTATE_S = 60
LOOKAHEAD_S = 4 * 3600

# Queensland has no daylight saving, so a fixed UTC+10 offset is exact for Brisbane.
BRISBANE = timezone(timedelta(hours=10), "AEST")

# Names as shown on the physical sign.
DESTINATION_ALIASES = {"Kippa-Ring": "Redcliffe"}

# Line colours sampled from the real sign photo (images/QLD RAil.jpeg).
T1_GREEN = "#3A8014"       # Caboolture / Sunshine Coast / Ipswich
T2_LIGHT_BLUE = "#4A80E0"  # Redcliffe Peninsula / Springfield: softer cornflower blue as rendered on the sign
T4_BLUE = "#0101B6"        # Shorncliffe / Cleveland: deep blue sampled from a swatch of the sign
FERNY_GROVE_RED = "#B81212"  # Ferny Grove / Beenleigh: a pure, slightly dark red (GTFS D53E45 is too muted)
GOLD_COAST_GOLD = "#8C7A1E"  # Gold Coast / Airport: muted olive-gold as rendered on the sign
DOOMBEN_PURPLE = "#9A56C2"  # Doomben: purple as rendered on the sign
SHORT_WORKING_GRAY = "#888888"
# Short workings that end at these stations are drawn gray whatever their line.
GRAY_DESTINATIONS = {"Bowen Hills", "Roma Street"}
# Keyed by the GTFS route_color of each route.
MAP_COLORS = {
    "27855A": T1_GREEN,
    "6CC6E9": T2_LIGHT_BLUE,
    "4778B2": T4_BLUE,
    "D53E45": FERNY_GROVE_RED,
    "FFC425": GOLD_COAST_GOLD,
    "9B60A6": DOOMBEN_PURPLE,
}
# The Northgate-Shorncliffe shuttle is tagged light blue in GTFS but is part of the T4 line.
ROUTE_COLOR_OVERRIDES = {"SHSH": T4_BLUE}

# Section titles as shown on the physical sign, by station: (inbound, outbound).
SECTION_TITLES = {"Northgate": ("City & South/West", "North")}
DEFAULT_SECTION_TITLES = ("City", "Outbound")

# Buses share one colour: a teal that no rail line uses (rail is green, blues, red, gold, purple
# and grey), and that keeps white text legible (contrast against white is about 4.9:1).
BUS_COLOR = "#007C89"

# Whether a trip is an NGR comes from backend/stock.py (run-code rule + trainspotter sightings).


def _line_color(trip: TripStop) -> str:
    if trip.headsign.removesuffix(" station") in GRAY_DESTINATIONS:
        return SHORT_WORKING_GRAY
    return ROUTE_COLOR_OVERRIDES.get(trip.route_short) or MAP_COLORS.get(trip.color.upper()) or "#" + trip.color

CANCELED = gtfs_realtime_pb2.TripDescriptor.CANCELED
SKIPPED = gtfs_realtime_pb2.TripUpdate.StopTimeUpdate.SKIPPED


class RealtimeCache:
    """Polls the GTFS-Realtime trip updates feed and keeps the latest good snapshot."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._trips: dict[tuple[str, str], gtfs_realtime_pb2.TripUpdate] = {}
        self.feed_timestamp: int | None = None
        self.last_success: float | None = None
        self.last_error: str | None = None

    def refresh(self) -> None:
        try:
            r = requests.get(RT_URL, timeout=30)
            r.raise_for_status()
            msg = gtfs_realtime_pb2.FeedMessage()
            msg.ParseFromString(r.content)
            trips = {}
            for e in msg.entity:
                if e.HasField("trip_update"):
                    tu = e.trip_update
                    trips[(tu.trip.trip_id, tu.trip.start_date)] = tu
            with self._lock:
                self._trips = trips
                self.feed_timestamp = msg.header.timestamp or None
                self.last_success = time.time()
                self.last_error = None
        except Exception as exc:  # keep serving the last good snapshot
            log.warning("Realtime refresh failed: %s", exc)
            with self._lock:
                self.last_error = str(exc)

    def get(self, trip_id: str, start_date: str):
        with self._lock:
            # Some feeds omit start_date; fall back to a trip_id-only match.
            return self._trips.get((trip_id, start_date)) or self._trips.get((trip_id, ""))

    @property
    def stale(self) -> bool:
        return self.last_success is None or time.time() - self.last_success > STALE_AFTER_S


def _delay_of(stu) -> int | None:
    if stu.departure.HasField("delay"):
        return stu.departure.delay
    if stu.arrival.HasField("delay"):
        return stu.arrival.delay
    return None


def _apply_realtime(trip: TripStop, scheduled: int, tu, platforms) -> tuple[int, str, bool]:
    """Return (departure_epoch, platform_stop_id, excluded) after overlaying realtime."""
    if tu is None:
        return scheduled, trip.stop_id, False
    if tu.trip.schedule_relationship == CANCELED:
        return scheduled, trip.stop_id, True

    actual, stop_id = scheduled, trip.stop_id
    before_delay = None
    for stu in tu.stop_time_update:
        # Bus feeds may identify a stop by stop_sequence only.
        at_stop = stu.stop_id in platforms or (
            not stu.stop_id and stu.stop_sequence and stu.stop_sequence == trip.stop_sequence
        )
        if at_stop:
            if stu.schedule_relationship == SKIPPED:
                return scheduled, stop_id, True
            stop_id = stu.stop_id or stop_id
            if stu.departure.HasField("time") and stu.departure.time > 0:
                return stu.departure.time, stop_id, False
            delay = _delay_of(stu)
            return scheduled + (delay or 0), stop_id, False
        if stu.stop_sequence and stu.stop_sequence < trip.stop_sequence:
            d = _delay_of(stu)
            if d is not None:
                before_delay = d  # delay propagates to later stops
    return scheduled + (before_delay or 0), stop_id, False


def _destination(trip: TripStop) -> str:
    name = trip.headsign.removesuffix(" station")
    name = DESTINATION_ALIASES.get(name, name)
    if trip.via_city:
        return "City" if name == "Roma Street" else f"City & {name}"
    return name


def _city_message(now: float) -> str:
    try:
        messages = json.loads(CITY_MESSAGES_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        log.warning("Cannot read %s: %s", CITY_MESSAGES_PATH.name, exc)
        return ""
    return messages[int(now // MESSAGE_ROTATE_S) % len(messages)] if messages else ""


def _platform_sort_key(label: str) -> tuple[int, int, str]:
    digits = "".join(c for c in label if c.isdigit())
    return (0, int(digits), label) if digits else (1, 0, label)


def _fmt_time(epoch: int) -> str:
    dt = datetime.fromtimestamp(epoch, BRISBANE)
    return f"{dt.hour % 12 or 12}:{dt.minute:02d}"


def build_board(
    index: StaticIndex,
    rt: RealtimeCache,
    station_id: str,
    now: float | None = None,
    mode: str | None = None,
    platform: str | None = None,
) -> dict:
    """Departure board for a rail station or a bus stop (`mode` only matters if an id is both)."""
    if index.mode_of(station_id, mode) == BUS:
        return build_bus_board(index, rt, station_id, now)
    now = now if now is not None else time.time()
    now_dt = datetime.fromtimestamp(now, BRISBANE)
    view = index.station_view(station_id)
    platforms = view.station.platforms
    sections: dict[bool, list[dict]] = {True: [], False: []}

    # Today plus yesterday (trips running past midnight carry times > 24:00:00).
    for day in (now_dt.date() - timedelta(days=1), now_dt.date()):
        services = index.active_services(day)
        midnight = int(datetime(day.year, day.month, day.day, tzinfo=BRISBANE).timestamp())
        start_date = day.strftime("%Y%m%d")
        for trip in view.trips:
            if trip.is_last or trip.service_id not in services:
                continue
            scheduled = midnight + trip.dep_seconds
            if scheduled < now - 3600 or scheduled > now + LOOKAHEAD_S:
                continue
            actual, stop_id, excluded = _apply_realtime(
                trip, scheduled, rt.get(trip.trip_id, start_date), platforms
            )
            if excluded or actual < now or actual > now + LOOKAHEAD_S:
                continue
            sections[trip.inbound].append(
                {
                    "scheduled": _fmt_time(scheduled),
                    "destination": _destination(trip),
                    "express": trip.express,
                    "ngr": is_ngr(trip.trip_id, trip.trip_id in index.ngr_barred),
                    "three_car": is_three_car(trip.trip_id),
                    "platform": platforms.get(stop_id, "-"),
                    "trip_id": trip.trip_id,
                    "stop_sequence": trip.stop_sequence,
                    "departs_epoch": actual,
                    "minutes": math.floor((actual - now) / 60),
                    "route": trip.route_short,
                    "color": _line_color(trip),
                    "realtime": rt.get(trip.trip_id, start_date) is not None,
                }
            )

    def top(rows: list[dict], n: int = ROWS_PER_SECTION) -> list[dict]:
        return sorted(rows, key=lambda r: r["departs_epoch"])[:n]

    board = {
        "now_epoch": int(now),
        "station_id": view.station.id,
        "station": view.station.name,
        "realtime_timestamp": rt.feed_timestamp,
        "realtime_stale": rt.stale,
        "message": "",
        "mode": TRAIN,
        "kind_label": "Station",
        "platform_heading": "Platform",
        "stop_code": "",
        "platforms": sorted(set(platforms.values()) - {"-"}, key=_platform_sort_key),
        "platform": None,
    }
    if platform is not None and platform in board["platforms"]:
        # One platform: the next trains in the direction that platform serves (other platforms
        # of the same direction included, as on the real sign). The city station and platforms
        # used both ways have no single direction, so they list just that platform's trains.
        directions = {t.inbound for t in view.trips if platforms.get(t.stop_id) == platform}
        if station_id == index.city_id or len(directions) != 1:
            rows = [r for r in sections[True] + sections[False] if r["platform"] == platform]
        else:
            rows = sections[directions.pop()]
        board.update(
            platform=platform,
            title=f"Next {CITY_ROWS} Trains - Platform {platform}",
            route_title=f"Platform {platform}",
            single_section=True,
            inbound_heading="Service",
            outbound_heading="",
            inbound=top(rows, CITY_ROWS),
            outbound=[],
        )
        for row in board["inbound"]:
            row["stops"] = index.route_stops(row["trip_id"], row["stop_sequence"])
        if station_id == index.city_id:
            board["message"] = _city_message(now)
        return board
    if station_id == index.city_id:
        # Every train leaves the city, so inbound/outbound is meaningless: one longer list.
        board.update(
            title=f"Next (1-{CITY_ROWS}) Trains",
            single_section=True,
            inbound_heading="Service",
            outbound_heading="",
            message=_city_message(now),
            inbound=top(sections[True] + sections[False], CITY_ROWS),
            outbound=[],
        )
        return board
    inbound_title, outbound_title = SECTION_TITLES.get(view.station.name, DEFAULT_SECTION_TITLES)
    board.update(
        title="Next 3 Inbound and Outbound Trains",
        single_section=False,
        inbound_heading=f"Next Trains {inbound_title}",
        outbound_heading=f"Next Trains {outbound_title}",
        inbound=top(sections[True]),
        outbound=top(sections[False]),
    )
    return board


def build_bus_board(
    index: StaticIndex, rt: RealtimeCache, stop_id: str, now: float | None = None
) -> dict:
    """One chronological list of the next buses at a stop (or at every bay of a busway station)."""
    now = now if now is not None else time.time()
    now_dt = datetime.fromtimestamp(now, BRISBANE)
    view = index.bus_view(stop_id)
    platforms = view.station.platforms  # bay stop_id -> label (the stop code)
    rows: list[dict] = []

    for day in (now_dt.date() - timedelta(days=1), now_dt.date()):
        services = index.active_services(day)
        midnight = int(datetime(day.year, day.month, day.day, tzinfo=BRISBANE).timestamp())
        start_date = day.strftime("%Y%m%d")
        # Calls are sorted by time, so bisect to the window instead of scanning the whole day.
        lo = bisect_left(view.deps, int(now - 3600) - midnight)
        hi = bisect_right(view.deps, int(now + LOOKAHEAD_S) - midnight)
        for call in view.calls[lo:hi]:
            if call.is_last or call.service_id not in services:
                continue
            scheduled = midnight + call.dep_seconds
            tu = rt.get(call.trip_id, start_date)
            actual, bay, excluded = _apply_realtime(call, scheduled, tu, platforms)
            if excluded or actual < now or actual > now + LOOKAHEAD_S:
                continue
            rows.append(
                {
                    "scheduled": _fmt_time(scheduled),
                    "destination": call.headsign or call.route_long,
                    "express": False,
                    "ngr": False,
                    "three_car": False,
                    "platform": platforms.get(bay, "-"),
                    "departs_epoch": actual,
                    "minutes": math.floor((actual - now) / 60),
                    "route": call.route_short,
                    "color": BUS_COLOR,
                    "realtime": tu is not None,
                }
            )

    rows.sort(key=lambda r: r["departs_epoch"])
    return {
        "now_epoch": int(now),
        "station_id": view.station.id,
        "station": view.station.name,
        "realtime_timestamp": rt.feed_timestamp,
        "realtime_stale": rt.stale,
        "message": "",
        "mode": BUS,
        "kind_label": "Bus Stop",
        "platform_heading": "Stop",
        "stop_code": view.station.code,
        "platforms": [],  # no platform picker for buses
        "platform": None,
        "title": f"Next (1-{BUS_ROWS}) Buses",
        "single_section": True,
        "inbound_heading": "Service",
        "outbound_heading": "",
        "inbound": rows[:BUS_ROWS],
        "outbound": [],
    }
