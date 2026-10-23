"""Static GTFS handling: download the SEQ feed and index rail and bus departures."""
from __future__ import annotations

import csv
import difflib
import heapq
import io
import math
import os
import pickle
import sys
import time
import zipfile
from array import array
from collections import defaultdict
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import NamedTuple

import requests

STATIC_URL = "https://gtfsrt.api.translink.com.au/GTFS/SEQ_GTFS.zip"
DATA_DIR = Path(__file__).resolve().parent.parent / "data"
ZIP_PATH = DATA_DIR / "SEQ_GTFS.zip"
INDEX_PATH = DATA_DIR / "index.pkl"
INDEX_VERSION = 6
MAX_ZIP_AGE_S = 24 * 3600

TRAIN = "train"
BUS = "bus"


def route_mode(route_type: str) -> str | None:
    """Map a GTFS route_type to the mode this app supports (None = ignored, e.g. ferry/tram).

    Handles the basic types (2 = rail, 3 = bus) and the extended ranges (100-199 rail,
    700-799 bus).
    """
    try:
        t = int(route_type)
    except (TypeError, ValueError):
        return None
    if t == 2 or 100 <= t <= 199:
        return TRAIN
    if t == 3 or 700 <= t <= 799:
        return BUS
    return None


# Trains calling at this station after the sign's station are heading to the city.
CITY_STATION = "Roma Street"

# NGR sets are not permitted on the Ferny Grove line (platform gap at Newmarket/Keperra, short
# platforms) or west of Ipswich (platform length at Rosewood). Any trip calling at one of these
# stations cannot be an NGR, whatever its run code says.
NGR_BARRED_STATIONS = {
    "Ferny Grove", "Keperra", "Newmarket",
    "Wulkuraka", "Karrabin", "Walloon", "Thomas Street", "Rosewood",
}


@dataclass
class Station:
    """A place with departures: a rail station, or a bus stop / busway station."""
    id: str
    name: str  # without the " station" suffix
    lat: float
    lon: float
    platforms: dict[str, str]  # platform/bay stop_id -> label (platform, or stop code for buses)
    mode: str = TRAIN
    code: str = ""  # stop_code: the number printed on a bus stop pole


@dataclass
class RailTrip:
    trip_id: str
    service_id: str
    route_short: str
    route_long: str
    color: str
    text_color: str
    headsign: str
    stops: list[tuple[int, str, int]]  # (stop_sequence, stop_id, dep_seconds), in order


@dataclass
class TripStop:
    """One trip's call at the station a StationView was built for."""
    trip_id: str
    service_id: str
    route_short: str
    route_long: str
    color: str
    text_color: str
    headsign: str
    inbound: bool  # heading towards the city
    via_city: bool  # calls at the city station after this one
    stop_id: str  # platform stop used by this trip
    stop_sequence: int
    dep_seconds: int  # seconds after midnight of the service day (may exceed 86400)
    is_last: bool  # the station is the final stop (no departure)
    express: bool


@dataclass
class StationView:
    station: Station
    trips: list[TripStop]


class BusTrip(NamedTuple):
    trip_id: str
    service_id: str
    route_short: str
    route_long: str
    headsign: str
    direction_id: str
    last_seq: int  # stop_sequence of the trip's final stop


class BusCall(NamedTuple):
    """One bus trip's call at a stop. Field names match TripStop where the realtime overlay
    (departures._apply_realtime) reads them."""
    trip_id: str
    service_id: str
    route_short: str
    route_long: str
    headsign: str
    direction_id: str
    stop_id: str
    stop_sequence: int
    dep_seconds: int  # seconds after midnight of the service day (may exceed 86400)
    is_last: bool  # the stop is the trip's final stop (no departure)


@dataclass
class BusStopView:
    station: Station
    calls: list[BusCall]  # sorted by dep_seconds
    deps: list[int]  # dep_seconds of each call, for bisecting a time window


def _rows(zf: zipfile.ZipFile, name: str):
    with zf.open(name) as f:
        yield from csv.DictReader(io.TextIOWrapper(f, "utf-8-sig"))


def _hms(value: str) -> int:
    h, m, s = value.split(":")
    return int(h) * 3600 + int(m) * 60 + int(s)


def ensure_zip(force: bool = False) -> Path:
    """Download the static feed if missing or older than a day."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    if not force and ZIP_PATH.exists() and time.time() - ZIP_PATH.stat().st_mtime < MAX_ZIP_AGE_S:
        return ZIP_PATH
    tmp = ZIP_PATH.with_suffix(".tmp")
    try:
        with requests.get(STATIC_URL, stream=True, timeout=120) as r:
            r.raise_for_status()
            with open(tmp, "wb") as f:
                for chunk in r.iter_content(1 << 20):
                    f.write(chunk)
        zipfile.ZipFile(tmp).testzip()
        os.replace(tmp, ZIP_PATH)
    except Exception:
        tmp.unlink(missing_ok=True)
        if not ZIP_PATH.exists():
            raise
    return ZIP_PATH


def _normalise(name: str) -> str:
    return name.strip().lower().removesuffix(" station").strip()


class StaticIndex:
    def __init__(
        self,
        trips: list[RailTrip],
        stations: dict[str, Station],
        stop_station: dict[str, str],
        calendar: dict,
        calendar_dates: dict,
        bus_stops: dict[str, Station] | None = None,
        bus_trips: list[BusTrip] | None = None,
        bus_calls: dict[str, tuple[array, array, array]] | None = None,
    ):
        self.trips = trips
        self.stations = stations
        self.stop_station = stop_station
        self.calendar = calendar
        self.calendar_dates = calendar_dates
        self.city_id = next((s.id for s in stations.values() if s.name == CITY_STATION), None)
        # Bus stops (and busway stations). Rail stations stay in `stations`; the bus side is
        # deliberately lighter: per stop, parallel arrays of (trip index, stop_sequence,
        # dep_seconds) rather than full trip patterns, as buses need no direction/express logic.
        self.bus_stops = bus_stops or {}
        self.bus_trips = bus_trips or []
        self.bus_calls = bus_calls or {}
        self._bus_views: dict[str, BusStopView] = {}
        # Lookup tables. Trains come first in every list so that an ambiguous name resolves to
        # the station (e.g. "Northgate" is the station, not a bus stop called "Northgate station").
        ordered = list(stations.values()) + sorted(self.bus_stops.values(), key=lambda p: p.id)
        self._by_name: dict[str, list[tuple[str, str]]] = defaultdict(list)
        self._by_code: dict[str, list[str]] = defaultdict(list)
        self._search: list[tuple[str, str, Station]] = []
        for place in ordered:
            norm = _normalise(place.name)
            self._by_name[norm].append((place.mode, place.id))
            if place.code:
                self._by_code[place.code].append(place.id)
            self._search.append((norm, place.code.lower(), place))
        barred_ids = {s.id for s in stations.values() if s.name in NGR_BARRED_STATIONS}
        self.ngr_barred: set[str] = {
            t.trip_id for t in trips if any(stop_station[s[1]] in barred_ids for s in t.stops)
        }
        self._views: dict[str, StationView] = {}
        # station id -> [(trip, index of its first call at that station)]
        self._calls: dict[str, list[tuple[RailTrip, int]]] = defaultdict(list)
        for trip in trips:
            seen: set[str] = set()
            for i, (_, stop_id, _) in enumerate(trip.stops):
                sid = stop_station[stop_id]
                if sid not in seen:
                    seen.add(sid)
                    self._calls[sid].append((trip, i))

    def mode_of(self, place_id: str, prefer: str | None = None) -> str | None:
        """Whether an id is a train station or a bus stop (`prefer` breaks the rare tie)."""
        for mode in ((prefer,) if prefer else ()) + (TRAIN, BUS):
            if place_id in (self.stations if mode == TRAIN else self.bus_stops):
                return mode
        return None

    def place(self, place_id: str, mode: str | None = None) -> Station | None:
        mode = self.mode_of(place_id, mode)
        if mode is None:
            return None
        return (self.stations if mode == TRAIN else self.bus_stops)[place_id]

    def find_place(self, query: str, mode: str | None = None) -> str | None:
        """Resolve an id, bus stop code or (case-insensitive) name to a place id.

        `mode` limits the match to trains or buses. A name shared by several places resolves
        to the station if there is one, otherwise to the lowest stop id.
        """
        q = query.strip()
        if not q:
            return None
        if self.mode_of(q, mode) is not None and (mode is None or self.mode_of(q, mode) == mode):
            return q
        if mode in (None, BUS):
            for pid in self._by_code.get(q, []):
                return pid
        for m, pid in self._by_name.get(_normalise(q), []):
            if mode in (None, m):
                return pid
        return None

    def find_station(self, query: str) -> str | None:
        """Resolve a rail station id or name to a station id (kept for the original API)."""
        return self.find_place(query, TRAIN)

    def search_places(self, query: str, mode: str | None = None, limit: int = 20) -> list[Station]:
        """Rank places for the picker: exact code/name, then prefix, then word/substring matches.

        An empty query lists train stations alphabetically (the original picker behaviour).
        """
        q = _normalise(query)
        if not q:
            if mode == BUS:
                return []
            return sorted(self.stations.values(), key=lambda p: p.name)[:limit]
        tokens = q.split()
        scored = []
        for norm, code, place in self._search:
            if mode and place.mode != mode:
                continue
            if q == code:
                rank = 0
            elif norm == q:
                rank = 1
            elif norm.startswith(q):
                rank = 2
            elif all(t in norm for t in tokens):
                rank = 3
            elif code and q in code:
                rank = 4
            else:
                continue
            scored.append((rank, place.mode != TRAIN, norm, place.id, place))
        return [row[-1] for row in heapq.nsmallest(limit, scored, key=lambda row: row[:4])]

    def suggest_places(self, query: str) -> list[str]:
        names = {p.name for p in self.stations.values()} | {p.name for p in self.bus_stops.values()}
        return difflib.get_close_matches(query.strip().title(), names, n=5, cutoff=0.5)

    def suggest_stations(self, query: str) -> list[str]:
        names = {s.name for s in self.stations.values()}
        return difflib.get_close_matches(query.strip().title(), names, n=5, cutoff=0.5)

    def bus_view(self, place_id: str) -> BusStopView:
        view = self._bus_views.get(place_id)
        if view is None:
            view = self._bus_views[place_id] = self._build_bus_view(place_id)
        return view

    def _build_bus_view(self, place_id: str) -> BusStopView:
        station = self.bus_stops[place_id]
        calls: list[BusCall] = []
        for stop_id in station.platforms:  # a busway station has one stop per bay
            arrays = self.bus_calls.get(stop_id)
            if arrays is None:
                continue
            trip_idx, seqs, deps = arrays
            for i in range(len(trip_idx)):
                t = self.bus_trips[trip_idx[i]]
                calls.append(
                    BusCall(
                        trip_id=t.trip_id,
                        service_id=t.service_id,
                        route_short=t.route_short,
                        route_long=t.route_long,
                        headsign=t.headsign,
                        direction_id=t.direction_id,
                        stop_id=stop_id,
                        stop_sequence=seqs[i],
                        dep_seconds=deps[i],
                        is_last=seqs[i] >= t.last_seq,
                    )
                )
        calls.sort(key=lambda c: c.dep_seconds)
        return BusStopView(station, calls, [c.dep_seconds for c in calls])

    def station_view(self, station_id: str) -> StationView:
        view = self._views.get(station_id)
        if view is None:
            view = self._views[station_id] = self._build_view(station_id)
        return view

    def _distance_to_city(self, station_id: str) -> float:
        city, s = self.stations[self.city_id], self.stations[station_id]
        return math.hypot(s.lat - city.lat, (s.lon - city.lon) * math.cos(math.radians(s.lat)))

    def _build_view(self, station_id: str) -> StationView:
        station = self.stations[station_id]
        raw = []
        for trip, idx in self._calls.get(station_id, []):
            sequence = [self.stop_station[s[1]] for s in trip.stops]
            via_city = self.city_id is not None and self.city_id in sequence[idx + 1:]
            if via_city:
                inbound = True
            elif self.city_id is not None and self.city_id in sequence[:idx]:
                inbound = False
            else:  # never touches the city: compare the end of the line with this station
                inbound = (
                    self.city_id is not None
                    and self._distance_to_city(sequence[-1]) < self._distance_to_city(station_id)
                )
            raw.append((trip, idx, inbound, via_city, tuple(sequence[idx:])))

        # A trip is express if it skips a station that another trip in the same direction
        # calls at between two of this trip's consecutive stops (e.g. Northgate -> Petrie
        # on the Caboolture line, where Redcliffe trains stop at Virginia..Bray Park).
        gaps: dict[bool, set[tuple[str, str]]] = {True: set(), False: set()}
        for inbound in gaps:
            for pattern in {p for _, _, i, _, p in raw if i == inbound}:
                for i in range(len(pattern)):
                    for j in range(i + 2, len(pattern)):
                        gaps[inbound].add((pattern[i], pattern[j]))

        trips = []
        for trip, idx, inbound, via_city, pattern in raw:
            express = any((a, b) in gaps[inbound] for a, b in zip(pattern, pattern[1:]))
            seq, stop_id, dep = trip.stops[idx]
            trips.append(
                TripStop(
                    trip_id=trip.trip_id,
                    service_id=trip.service_id,
                    route_short=trip.route_short,
                    route_long=trip.route_long,
                    color=trip.color,
                    text_color=trip.text_color,
                    headsign=trip.headsign,
                    inbound=inbound,
                    via_city=via_city,
                    stop_id=stop_id,
                    stop_sequence=seq,
                    dep_seconds=dep,
                    is_last=idx == len(trip.stops) - 1,
                    express=express,
                )
            )
        return StationView(station, trips)

    def route_stops(self, trip_id: str, stop_sequence: int) -> list[dict]:
        """Stations a trip calls at after `stop_sequence`; `express` marks a leg that skips stations."""
        cache = self.__dict__
        if "_trips_by_id" not in cache:
            cache["_trips_by_id"] = {t.trip_id: t for t in self.trips}
            # (a, b) is a skip leg if any trip calls at a then b with something in between.
            skips: set[tuple[str, str]] = set()
            for pattern in {tuple(self.stop_station[s[1]] for s in t.stops) for t in self.trips}:
                for i in range(len(pattern)):
                    for j in range(i + 2, len(pattern)):
                        skips.add((pattern[i], pattern[j]))
            cache["_skip_legs"] = skips
        trip = cache["_trips_by_id"].get(trip_id)
        if trip is None:
            return []
        sequence = [self.stop_station[s[1]] for s in trip.stops]
        start = next((i for i, s in enumerate(trip.stops) if s[0] == stop_sequence), None)
        if start is None:
            return []
        stops: list[dict] = []
        prev_skipped = False
        for i in range(start + 1, len(sequence)):
            skipped = (sequence[i - 1], sequence[i]) in cache["_skip_legs"]
            # Only the first stop of a run of skipping legs is annotated, as on the real sign.
            stops.append({"name": self.stations[sequence[i]].name, "express": skipped and not prev_skipped})
            prev_skipped = skipped
        return stops

    def active_services(self, day: date) -> set[str]:
        ymd = day.strftime("%Y%m%d")
        weekday = day.strftime("%A").lower()
        active = {
            sid
            for sid, c in self.calendar.items()
            if c["start"] <= ymd <= c["end"] and c[weekday]
        }
        for sid, exc in self.calendar_dates.get(ymd, []):
            if exc == "1":
                active.add(sid)
            elif exc == "2":
                active.discard(sid)
        return active


def _build(zip_path: Path) -> StaticIndex:
    zf = zipfile.ZipFile(zip_path)

    routes: dict[str, dict] = {}  # rail routes
    bus_routes: dict[str, dict] = {}
    for r in _rows(zf, "routes.txt"):
        mode = route_mode(r["route_type"])
        if mode == TRAIN:
            routes[r["route_id"]] = r
        elif mode == BUS:
            bus_routes[r["route_id"]] = r

    trip_meta: dict[str, dict] = {}  # rail trips
    bus_trip_index: dict[str, int] = {}  # bus trip_id -> position in bus_trips
    bus_trips: list[BusTrip] = []
    for r in _rows(zf, "trips.txt"):
        if r["route_id"] in routes:
            trip_meta[r["trip_id"]] = r
        elif r["route_id"] in bus_routes:
            route = bus_routes[r["route_id"]]
            bus_trip_index[r["trip_id"]] = len(bus_trips)
            bus_trips.append(
                BusTrip(
                    trip_id=r["trip_id"],
                    service_id=sys.intern(r["service_id"]),
                    route_short=sys.intern(route["route_short_name"]),
                    route_long=route["route_long_name"],
                    headsign=sys.intern(r["trip_headsign"]),
                    direction_id=r.get("direction_id", ""),
                    last_seq=0,
                )
            )

    stops_by_trip: dict[str, list[tuple[int, str, int]]] = defaultdict(list)
    bus_last_seq = array("I", [0]) * len(bus_trips)
    bus_calls: dict[str, tuple[array, array, array]] = {}
    for r in _rows(zf, "stop_times.txt"):
        tid = r["trip_id"]
        bus_idx = bus_trip_index.get(tid)
        if bus_idx is not None:
            seq = int(r["stop_sequence"])
            if seq > bus_last_seq[bus_idx]:
                bus_last_seq[bus_idx] = seq
            dep = r["departure_time"] or r["arrival_time"]
            if not dep or r.get("pickup_type") == "1":  # untimed, or set-down only
                continue
            arrays = bus_calls.get(r["stop_id"])
            if arrays is None:
                arrays = bus_calls[sys.intern(r["stop_id"])] = (array("I"), array("I"), array("I"))
            arrays[0].append(bus_idx)
            arrays[1].append(seq)
            arrays[2].append(_hms(dep))
        elif tid in trip_meta:
            dep = r["departure_time"] or r["arrival_time"]
            stops_by_trip[tid].append((int(r["stop_sequence"]), r["stop_id"], _hms(dep)))
    bus_trips = [t._replace(last_seq=bus_last_seq[i]) for i, t in enumerate(bus_trips)]

    trips: list[RailTrip] = []
    for tid, rows in stops_by_trip.items():
        rows.sort()
        meta = trip_meta[tid]
        route = routes[meta["route_id"]]
        trips.append(
            RailTrip(
                trip_id=tid,
                service_id=meta["service_id"],
                route_short=route["route_short_name"],
                route_long=route["route_long_name"],
                color=route["route_color"] or "FFFFFF",
                text_color=route["route_text_color"] or "FFFFFF",
                headsign=meta["trip_headsign"],
                stops=rows,
            )
        )

    # Stations are the parents of the platform stops that rail trips call at.
    all_stops = {r["stop_id"]: r for r in _rows(zf, "stops.txt")}
    used_stops = {s[1] for t in trips for s in t.stops}
    stations: dict[str, Station] = {}
    stop_station: dict[str, str] = {}
    for stop_id in used_stops:
        stop = all_stops[stop_id]
        sid = stop["parent_station"] or stop_id
        parent = all_stops.get(sid, stop)
        station = stations.get(sid)
        if station is None:
            station = stations[sid] = Station(
                id=sid,
                name=parent["stop_name"].removesuffix(" station"),
                lat=float(parent["stop_lat"]),
                lon=float(parent["stop_lon"]),
                platforms={},
            )
        station.platforms[stop_id] = stop["platform_code"] or "-"
        stop_station[stop_id] = sid

    # Bus places: a standalone stop is its own place; the bays of a busway station are grouped
    # under their parent station, like rail platforms.
    def _coord(value: str) -> float:
        return float(value) if value else 0.0

    bus_stops: dict[str, Station] = {}
    for stop_id in bus_calls:
        stop = all_stops.get(stop_id)
        if stop is None:
            continue
        pid = stop["parent_station"] or stop_id
        parent = all_stops.get(pid, stop)
        place = bus_stops.get(pid)
        if place is None:
            place = bus_stops[pid] = Station(
                id=pid,
                name=parent["stop_name"].removesuffix(" station"),
                lat=_coord(parent["stop_lat"]),
                lon=_coord(parent["stop_lon"]),
                platforms={},
                mode=BUS,
                code=(parent.get("stop_code") or "").strip(),
            )
        place.platforms[stop_id] = (stop.get("platform_code") or stop.get("stop_code") or "-").strip()

    calendar = {
        r["service_id"]: {
            "start": r["start_date"],
            "end": r["end_date"],
            **{d: r[d] == "1" for d in (
                "monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")},
        }
        for r in _rows(zf, "calendar.txt")
    }
    calendar_dates: dict[str, list[tuple[str, str]]] = defaultdict(list)
    for r in _rows(zf, "calendar_dates.txt"):
        calendar_dates[r["date"]].append((r["service_id"], r["exception_type"]))

    return StaticIndex(
        trips, stations, stop_station, calendar, dict(calendar_dates), bus_stops, bus_trips, bus_calls
    )


def load_index(force_download: bool = False) -> StaticIndex:
    zip_path = ensure_zip(force_download)
    stamp = (INDEX_VERSION, zip_path.stat().st_mtime_ns, zip_path.stat().st_size)
    if INDEX_PATH.exists():
        try:
            with open(INDEX_PATH, "rb") as f:
                cached_stamp, index = pickle.load(f)
            if cached_stamp == stamp:
                return index
        except Exception:
            pass
    index = _build(zip_path)
    with open(INDEX_PATH, "wb") as f:
        pickle.dump((stamp, index), f)
    return index
