#!/usr/bin/env python3
"""
mlb_app.py - Local interactive MLB dashboard (multi-source, scrapes daily).

A tiny web server (Python standard library only - no Flask, no pip installs)
that aggregates several public, key-free data sources into one dashboard:

  * MLB Stats API (statsapi.mlb.com) - schedule, probable pitchers, team
    records, venue + coordinates, game-time weather, standings, team hitting &
    pitching tables, league leaders, per-pitcher season line, last-3 starts,
    vs-LHB/RHB splits, throwing hand.
  * Baseball Savant / Statcast (baseballsavant.mlb.com) - xERA, xwOBA, pitch
    arsenal (mix + whiff%), hard-hit%.
  * ESPN hidden API (site.api.espn.com) - betting odds (moneyline, O/U, run
    line) and national/local TV.
  * Open-Meteo (api.open-meteo.com) - forecast temp/wind/precip at first pitch
    (used before MLB publishes the official game-time weather).

Run:
    python mlb_app.py            # serves http://localhost:8765 and opens it
    python mlb_app.py 9000       # use a specific port

Everything degrades gracefully: if one source is unavailable, the rest of the
dashboard still renders.
"""

import csv as csvmod
import io
import json
import os
import re
import sys
import threading
import time
import webbrowser
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlencode, urlparse
from urllib.request import Request, urlopen

API = "https://statsapi.mlb.com/api/v1"
API11 = "https://statsapi.mlb.com/api/v1.1"
SAVANT = "https://baseballsavant.mlb.com/leaderboard"
ESPN = "https://site.api.espn.com/apis/site/v2/sports/baseball/mlb/scoreboard"
OPENMETEO = "https://api.open-meteo.com/v1/forecast"
UA = {"User-Agent": "mlb-dashboard/2.0 (personal use)"}
TIMEOUT = 25


def fetch_json(url):
    with urlopen(Request(url, headers=UA), timeout=TIMEOUT) as resp:
        return json.load(resp)


def fetch_text(url):
    with urlopen(Request(url, headers=UA), timeout=45) as resp:
        return resp.read().decode("utf-8-sig", errors="replace")


def csv_rows(text):
    return list(csvmod.DictReader(io.StringIO(text)))


# ---- US Eastern time ------------------------------------------------------

def _nth_sunday(year, month, n):
    first = date(year, month, 1)
    return 1 + (6 - first.weekday()) % 7 + (n - 1) * 7


def to_eastern(dt_utc):
    try:
        from zoneinfo import ZoneInfo
        return dt_utc.astimezone(ZoneInfo("America/New_York"))
    except Exception:
        pass
    from datetime import timedelta
    y = dt_utc.year
    start = datetime(y, 3, _nth_sunday(y, 3, 2), 7, tzinfo=timezone.utc)
    end = datetime(y, 11, _nth_sunday(y, 11, 1), 6, tzinfo=timezone.utc)
    off = -4 if start <= dt_utc < end else -5
    return dt_utc.astimezone(timezone(timedelta(hours=off), "EDT" if off == -4 else "EST"))


def format_first_pitch(iso):
    if not iso:
        return "TBD"
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone(timezone.utc)
    except ValueError:
        return "TBD"
    e = to_eastern(dt)
    return f"{e.hour % 12 or 12}:{e.minute:02d} {'AM' if e.hour < 12 else 'PM'} {e.tzname() or 'ET'}"


def today_eastern():
    return to_eastern(datetime.now(timezone.utc)).date().isoformat()


# ---- thread-safe TTL cache (does not cache None) --------------------------

_cache = {}
_lock = threading.Lock()


def cached(key, ttl, producer):
    now = time.time()
    with _lock:
        hit = _cache.get(key)
        if hit and now - hit[0] < ttl:
            return hit[1]
    val = producer()
    if val is not None:
        with _lock:
            _cache[key] = (now, val)
    return val


def norm(s):
    return "".join(ch for ch in (s or "").lower() if ch.isalnum())


# ---- MLB: standings -------------------------------------------------------

def standings(season):
    def produce():
        url = (f"{API}/standings?leagueId=103,104&season={season}"
               f"&standingsTypes=regularSeason&hydrate=division,team")
        try:
            data = fetch_json(url)
        except (HTTPError, URLError, ValueError):
            return None
        by_team, divisions = {}, []
        for rec in data.get("records", []):
            dv = rec.get("division", {}) or {}
            dname = dv.get("nameShort") or dv.get("abbreviation") or dv.get("name", "")
            teams = []
            for tr in rec.get("teamRecords", []):
                sr = {x.get("type"): x for x in tr.get("records", {}).get("splitRecords", [])}

                def wl(t):
                    r = sr.get(t)
                    return f'{r.get("wins")}-{r.get("losses")}' if r else ""
                info = {
                    "id": tr["team"]["id"], "name": tr["team"].get("name", ""),
                    "w": tr.get("wins"), "l": tr.get("losses"),
                    "pct": tr.get("winningPercentage"), "gb": tr.get("gamesBack"),
                    "rd": tr.get("runDifferential"),
                    "streak": (tr.get("streak") or {}).get("streakCode"),
                    "divRank": tr.get("divisionRank"), "wcRank": tr.get("wildCardRank"),
                    "l10": wl("lastTen"), "home": wl("home"), "away": wl("away"),
                    "div": dname,
                }
                by_team[info["id"]] = info
                teams.append(info)
            divisions.append({"division": dname, "teams": teams})
        order = ["AL East", "AL Central", "AL West", "NL East", "NL Central", "NL West"]
        divisions.sort(key=lambda d: order.index(d["division"]) if d["division"] in order else 99)
        return {"byTeam": by_team, "divisions": divisions}
    return cached(f"standings:{season}", 1800, produce)


# ---- MLB: team hitting & pitching tables (with ranks) ---------------------

def team_tables(season):
    def produce():
        out = {}
        try:
            hit = fetch_json(f"{API}/teams/stats?season={season}&sportId=1&group=hitting&stats=season")
            pit = fetch_json(f"{API}/teams/stats?season={season}&sportId=1&group=pitching&stats=season")
        except (HTTPError, URLError, ValueError):
            return None
        for sp in hit.get("stats", [{}])[0].get("splits", []):
            s = sp["stat"]
            out.setdefault(sp["team"]["id"], {})["hit"] = {
                "avg": s.get("avg"), "obp": s.get("obp"), "slg": s.get("slg"),
                "ops": s.get("ops"), "r": s.get("runs"), "hr": s.get("homeRuns"),
                "sb": s.get("stolenBases"), "g": s.get("gamesPlayed"),
            }
        for sp in pit.get("stats", [{}])[0].get("splits", []):
            s = sp["stat"]
            out.setdefault(sp["team"]["id"], {})["pit"] = {
                "era": s.get("era"), "whip": s.get("whip"),
                "so": s.get("strikeOuts"), "sv": s.get("saves"),
            }

        def rank(grp, key, high):
            vals = []
            for tid, d in out.items():
                try:
                    vals.append((tid, float(d.get(grp, {}).get(key))))
                except (TypeError, ValueError):
                    pass
            vals.sort(key=lambda x: x[1], reverse=high)
            for i, (tid, _) in enumerate(vals):
                out[tid].setdefault("rank", {})[f"{grp}_{key}"] = i + 1

        rank("hit", "ops", True)
        rank("hit", "r", True)
        rank("pit", "era", False)
        rank("pit", "whip", False)
        for d in out.values():
            h = d.get("hit", {})
            try:
                d["hit"]["rpg"] = round(float(h["r"]) / float(h["g"]), 2)
            except (TypeError, ValueError, ZeroDivisionError, KeyError):
                pass
        return out
    return cached(f"teamtab:{season}", 3600, produce)


def team_splits(season):
    """Per-team platoon offense (OPS vs LHP/RHP) and bullpen relief ERA."""
    def produce():
        out = {}

        def pull(group, sit, field, key):
            try:
                d = fetch_json(f"{API}/teams/stats?season={season}&sportId=1"
                               f"&group={group}&stats=statSplits&sitCodes={sit}")
                for sp in (d.get("stats") or [{}])[0].get("splits", []):
                    tid = (sp.get("team") or {}).get("id")
                    if tid is not None:
                        out.setdefault(tid, {})[key] = (sp.get("stat") or {}).get(field)
            except (HTTPError, URLError, ValueError, KeyError, IndexError):
                pass
        pull("hitting", "vl", "ops", "opsVsL")
        pull("hitting", "vr", "ops", "opsVsR")
        pull("pitching", "rp", "era", "relEra")
        return out or None
    return cached(f"splits:{season}", 3600, produce)


def team_home_park(season):
    """team_id -> home-park run factor (for park-neutralizing season aggregates)."""
    def produce():
        out = {}
        try:
            for t in fetch_json(f"{API}/teams?sportId=1&season={season}").get("teams", []):
                out[t["id"]] = park_factor((t.get("venue") or {}).get("name", ""))
        except (HTTPError, URLError, ValueError, KeyError):
            return None
        return out or None
    return cached(f"homepark:{season}", 86400, produce)


def team_recent(season, start, end):
    """team_id -> recent runs/game over a date window (recent form)."""
    def produce():
        out = {}
        try:
            d = fetch_json(f"{API}/teams/stats?season={season}&sportId=1&group=hitting"
                           f"&stats=byDateRange&startDate={start}&endDate={end}")
            for sp in (d.get("stats") or [{}])[0].get("splits", []):
                tid = (sp.get("team") or {}).get("id")
                s = sp.get("stat", {})
                r, g = _f(s.get("runs")), _f(s.get("gamesPlayed"))
                if tid and r is not None and g:
                    out[tid] = r / g
        except (HTTPError, URLError, ValueError, KeyError, IndexError):
            return None
        return out or None
    return cached(f"recent:{start}:{end}", 3600, produce)


# ---- MLB: league leaders --------------------------------------------------

def leaders(season):
    def produce():
        def pull(group, cats):
            try:
                d = fetch_json(f"{API}/stats/leaders?leaderCategories={cats}"
                               f"&statGroup={group}&season={season}&sportId=1"
                               f"&limit=5&playerPool=qualified")
            except (HTTPError, URLError, ValueError):
                return []
            res = []
            for c in d.get("leagueLeaders", []):
                tops = [{"name": x["person"]["fullName"],
                         "team": (x.get("team") or {}).get("name", ""),
                         "val": x.get("value")} for x in c.get("leaders", [])[:5]]
                if tops:
                    res.append({"cat": c.get("leaderCategory"), "leaders": tops})
            return res
        return {
            "hitting": pull("hitting", "battingAverage,homeRuns,runsBattedIn,onBasePlusSlugging,stolenBases"),
            "pitching": pull("pitching", "earnedRunAverage,strikeouts,wins,saves,whip"),
        }
    return cached(f"leaders:{season}", 3600, produce)


# ---- Savant: expected stats (xERA) & pitch arsenal ------------------------

def savant_xstats(season):
    def produce():
        url = (f"{SAVANT}/expected_statistics?type=pitcher&year={season}"
               f"&position=&team=&filterType=q&min=q&csv=true")
        try:
            rows = csv_rows(fetch_text(url))
        except (HTTPError, URLError):
            return None
        out = {}
        for r in rows:
            try:
                pid = int(r.get("player_id"))
            except (TypeError, ValueError):
                continue
            out[pid] = {"xera": r.get("xera"), "xwoba": r.get("est_woba"),
                        "xba": r.get("est_ba"), "era": r.get("era")}
        return out or None
    return cached(f"savx:{season}", 21600, produce)


def savant_arsenal(season):
    def produce():
        url = (f"{SAVANT}/pitch-arsenal-stats?type=pitcher&pitchType=&year={season}"
               f"&team=&min=10&csv=true")
        try:
            rows = csv_rows(fetch_text(url))
        except (HTTPError, URLError):
            return None
        byp = {}
        for r in rows:
            try:
                pid = int(r.get("player_id"))
                usage = float(r.get("pitch_usage") or 0)
            except (TypeError, ValueError):
                continue
            byp.setdefault(pid, []).append({
                "name": r.get("pitch_name"), "usage": usage,
                "whiff": r.get("whiff_percent"), "hh": r.get("hard_hit_percent"),
                "n": r.get("pitches"),
            })
        out = {}
        for pid, arr in byp.items():
            arr.sort(key=lambda x: x["usage"], reverse=True)
            tot = whiff = hh = 0.0
            for p in arr:
                try:
                    n = float(p["n"] or 0)
                    tot += n
                    if p["whiff"]:
                        whiff += float(p["whiff"]) * n
                    if p["hh"]:
                        hh += float(p["hh"]) * n
                except (TypeError, ValueError):
                    pass
            out[pid] = {
                "pitches": [{"name": p["name"], "usage": round(p["usage"]),
                             "whiff": p["whiff"]} for p in arr[:4]],
                "whiff": round(whiff / tot, 1) if tot else None,
                "hardhit": round(hh / tot, 1) if tot else None,
            }
        return out or None
    return cached(f"sava:{season}", 21600, produce)


# ---- ESPN: odds + TV ------------------------------------------------------

def _ml_to_prob(ml):
    try:
        ml = float(ml)
    except (TypeError, ValueError):
        return None
    return (-ml) / (-ml + 100) if ml < 0 else 100 / (ml + 100)


def _parse_details(details):
    # "BAL -148" -> ("BAL", -148.0)
    if not details:
        return None, None
    parts = details.rsplit(" ", 1)
    if len(parts) != 2:
        return None, None
    try:
        return parts[0], float(parts[1].replace("+", ""))
    except ValueError:
        return None, None


def _ml(side):
    if not side:
        return None
    v = side.get("moneyLine")
    if v in (None, ""):
        v = (side.get("current") or {}).get("moneyLine")
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def espn_odds(date_str):
    def produce():
        try:
            data = fetch_json(f"{ESPN}?dates={date_str.replace('-', '')}")
        except (HTTPError, URLError, ValueError):
            return None
        out = {}
        for ev in data.get("events", []):
            comp = (ev.get("competitions") or [{}])[0]
            cs = comp.get("competitors", [])
            home = next((c for c in cs if c.get("homeAway") == "home"), None)
            away = next((c for c in cs if c.get("homeAway") == "away"), None)
            if not home or not away:
                continue
            o = (comp.get("odds") or [None])[0]
            tv = []
            for b in comp.get("broadcasts", []):
                tv += b.get("names", [])
            entry = {
                "details": o.get("details") if o else None,
                "ou": o.get("overUnder") if o else None,
                "spread": o.get("spread") if o else None,
                "book": (o.get("provider") or {}).get("name") if o else None,
                "tv": ", ".join(dict.fromkeys(tv)) if tv else None,
                "impAway": None, "impHome": None,
            }
            if o:
                impA, impH = _ml_to_prob(_ml(o.get("awayTeamOdds"))), _ml_to_prob(_ml(o.get("homeTeamOdds")))
                if impA and impH:
                    s = impA + impH
                    entry["impAway"], entry["impHome"] = impA / s, impH / s
                elif o.get("details"):
                    fav, line = _parse_details(o["details"])
                    pf = _ml_to_prob(line)
                    if pf is not None and fav:
                        favn = norm(fav)
                        ha, aa = norm(home["team"].get("abbreviation")), norm(away["team"].get("abbreviation"))
                        if favn and (favn == ha or favn in ha or ha in favn):
                            entry["impHome"], entry["impAway"] = pf, 1 - pf
                        elif favn and (favn == aa or favn in aa or aa in favn):
                            entry["impAway"], entry["impHome"] = pf, 1 - pf
            k = norm(away["team"]["displayName"]) + "@" + norm(home["team"]["displayName"])
            out[k] = entry
        return out
    return cached(f"espn:{date_str}", 300, produce)


# ---- MLB: pitcher hand (bulk) + per-pitcher detail ------------------------

def pitcher_hands(pids, season):
    if not pids:
        return {}
    ids = ",".join(str(p) for p in pids)

    def produce():
        out = {}
        try:
            url = (f"{API}/people?personIds={ids}"
                   f"&hydrate=stats(group=[pitching],type=[season],season={season})")
            for p in fetch_json(url).get("people", []):
                out[p["id"]] = (p.get("pitchHand") or {}).get("code")
        except (HTTPError, URLError, ValueError, KeyError):
            pass
        return out
    return cached(f"hands:{season}:{ids}", 3600, produce)


def pitcher_detail(pid, season):
    if not pid:
        return None

    def produce():
        url = (f"{API}/people/{pid}/stats?stats=season,gameLog,statSplits"
               f"&sitCodes=vl,vr&group=pitching&season={season}")
        try:
            data = fetch_json(url)
        except (HTTPError, URLError, ValueError):
            return None
        res = {"era": None, "wl": "", "whip": None, "k": None, "ip": None,
               "gs": None, "last3": [], "vsL": None, "vsR": None}
        for block in data.get("stats", []):
            t = (block.get("type") or {}).get("displayName")
            splits = block.get("splits", [])
            if t == "season" and splits:
                s = splits[0]["stat"]
                res.update(era=s.get("era"), wl=f'{s.get("wins", 0)}-{s.get("losses", 0)}',
                           whip=s.get("whip"), k=s.get("strikeOuts"),
                           ip=s.get("inningsPitched"), gs=s.get("gamesStarted"),
                           bb=s.get("baseOnBalls"), hr=s.get("homeRuns"), hbp=s.get("hitByPitch"))
            elif t == "gameLog":
                starts = [g for g in splits
                          if str((g.get("stat") or {}).get("gamesStarted", "0")) == "1"] or splits
                for g in starts[-3:]:
                    s = g["stat"]
                    res["last3"].append({
                        "date": g.get("date"), "opp": (g.get("opponent") or {}).get("name", ""),
                        "ip": s.get("inningsPitched"), "er": s.get("earnedRuns"),
                        "so": s.get("strikeOuts"), "h": s.get("hits"),
                    })
            elif t == "statSplits":
                for sp in splits:
                    s = sp.get("stat", {})
                    val = {"avg": s.get("avg"), "ops": s.get("ops")}
                    code = (sp.get("split") or {}).get("code")
                    if code == "vl":
                        res["vsL"] = val
                    elif code == "vr":
                        res["vsR"] = val
        return res
    return cached(f"pdet:{pid}:{season}", 900, produce)


# ---- Open-Meteo forecast at first pitch -----------------------------------

def forecast(lat, lon, iso):
    if lat is None or lon is None or not iso:
        return None
    key = f"fc:{round(float(lat), 2)}:{round(float(lon), 2)}:{iso[:13]}"

    def produce():
        url = (f"{OPENMETEO}?latitude={lat}&longitude={lon}"
               f"&hourly=temperature_2m,precipitation_probability,wind_speed_10m,"
               f"wind_direction_10m,weather_code&temperature_unit=fahrenheit"
               f"&wind_speed_unit=mph&timezone=UTC&forecast_days=2")
        try:
            data = fetch_json(url)
        except (HTTPError, URLError, ValueError):
            return None
        h = data.get("hourly", {})
        times = h.get("time", [])
        if not times:
            return None
        try:
            tgt = datetime.fromisoformat(iso.replace("Z", "")[:16])
        except ValueError:
            return None

        def diff(t):
            try:
                return abs((datetime.fromisoformat(t[:16]) - tgt).total_seconds())
            except ValueError:
                return 1e18
        idx = min(range(len(times)), key=lambda i: diff(times[i]))

        def at(name):
            arr = h.get(name, [])
            return arr[idx] if idx < len(arr) else None
        return {"temp": at("temperature_2m"), "precip": at("precipitation_probability"),
                "wind": at("wind_speed_10m"), "winddir": at("wind_direction_10m"),
                "code": at("weather_code")}
    return cached(key, 1800, produce)


def weather(game_pk):
    def produce():
        try:
            data = fetch_json(f"{API11}/game/{game_pk}/feed/live"
                              f"?fields=gameData,weather,condition,temp,wind")
            w = (data.get("gameData", {}) or {}).get("weather", {}) or {}
            if w.get("condition"):
                return {"condition": w.get("condition"), "temp": w.get("temp"), "wind": w.get("wind")}
        except (HTTPError, URLError, ValueError, KeyError):
            pass
        return None
    return cached(f"wx:{game_pk}", 300, produce)


# ---- BALLDONTLIE: player injuries (ALL-STAR tier) -------------------------
BDL = "https://api.balldontlie.io/mlb/v1"
BDL_CONFIG = os.path.join(os.path.dirname(os.path.abspath(__file__)), "bdl_config.json")


def bdl_key():
    if os.environ.get("BALLDONTLIE_API_KEY"):
        return os.environ["BALLDONTLIE_API_KEY"]
    if os.path.exists(BDL_CONFIG):
        try:
            with open(BDL_CONFIG, encoding="utf-8") as f:
                return json.load(f).get("api_key")
        except (OSError, ValueError):
            pass
    return None


def bdl_get(path, params):
    key = bdl_key()
    if not key:
        return None
    url = f"{BDL}{path}"
    if params:
        url += "?" + urlencode(params, doseq=True)
    req = Request(url, headers={"Authorization": key, "Accept": "application/json",
                                "User-Agent": UA["User-Agent"]})
    with urlopen(req, timeout=TIMEOUT) as r:
        return json.load(r)


def injuries():
    def produce():
        if not bdl_key():
            return None
        by_team, alias, cursor, pages = {}, {}, None, 0
        while pages < 40:
            params = [("per_page", 100)]
            if cursor is not None:
                params.append(("cursor", cursor))
            try:
                data = bdl_get("/player_injuries", params)
            except (HTTPError, URLError, ValueError):
                break
            if not data:
                break
            for r in data.get("data", []):
                pl = r.get("player") or {}
                tm = pl.get("team") or {}
                dn = tm.get("display_name") or tm.get("name") or ""
                if not dn:
                    continue
                by_team.setdefault(dn, []).append({
                    "name": pl.get("full_name") or "",
                    "pos": pl.get("position"), "status": r.get("status"),
                    "type": r.get("type"), "detail": r.get("detail"),
                    "return": (r.get("return_date") or "")[:10],
                    "comment": r.get("short_comment"),
                })
                alias[norm(dn)] = dn
                if tm.get("name"):
                    alias[norm(tm.get("name"))] = dn
            cursor = data.get("meta", {}).get("next_cursor")
            pages += 1
            if not cursor:
                break
        if not by_team:
            return None
        return {"byTeam": by_team, "alias": alias,
                "count": sum(len(v) for v in by_team.values())}
    return cached("bdl_injuries", 1800, produce)


def team_injuries(inj_data, team_name):
    if not inj_data or not team_name:
        return []
    dn = inj_data["alias"].get(norm(team_name)) or inj_data["alias"].get(norm(team_name.split()[-1]))
    return inj_data["byTeam"].get(dn, []) if dn else []


# ---- prediction model (first-principles run-expectancy; no training data) -

PARK_FACTORS = {  # approximate run park factors (1.00 = neutral)
    "coors field": 1.15, "fenway park": 1.05, "great american ball park": 1.06,
    "globe life field": 1.02, "chase field": 1.01, "yankee stadium": 1.02,
    "oriole park at camden yards": 1.01, "citizens bank park": 1.03,
    "truist park": 1.01, "wrigley field": 1.01, "dodger stadium": 0.97,
    "petco park": 0.96, "oracle park": 0.93, "t-mobile park": 0.94,
    "comerica park": 0.97, "loandepot park": 0.97, "tropicana field": 0.97,
    "kauffman stadium": 1.00, "target field": 1.01, "rate field": 1.01,
    "guaranteed rate field": 1.01, "pnc park": 0.98, "busch stadium": 0.99,
    "citi field": 0.95, "nationals park": 1.01, "daikin park": 1.01,
    "minute maid park": 1.01, "angel stadium": 0.98, "american family field": 1.01,
    "progressive field": 0.98, "sutter health park": 1.02,
}


def park_factor(venue):
    return PARK_FACTORS.get((venue or "").strip().lower(), 1.00)


def _f(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def _ip_to_num(ip):
    """MLB innings notation '88.2' (88 ip, 2 outs) -> 88.667 decimal innings."""
    v = _f(ip)
    if v is None:
        return None
    whole = int(v)
    return whole + round((v - whole) * 10) / 3.0


def fip(p, c=3.15):
    """Fielding Independent Pitching on the ERA scale (defense/luck-stripped)."""
    ip = _ip_to_num(p.get("ip"))
    k, bb, hr = _f(p.get("k")), _f(p.get("bb")), _f(p.get("hr"))
    hbp = _f(p.get("hbp")) or 0
    if not ip or ip < 1 or k is None or bb is None or hr is None:
        return None
    return (13 * hr + 3 * (bb + hbp) - 2 * k) / ip + c


def weather_factor(gd):
    """Run-environment multiplier from weather (MLB wind/temp preferred)."""
    w, f = gd.get("weather"), gd.get("forecast")
    mult, notes, temp, windspeed, wtxt = 1.0, [], None, None, ""
    if w:
        temp = _f(w.get("temp"))
        wtxt = (w.get("wind") or "").lower()
        if "dome" in wtxt or "roof closed" in wtxt:
            return 1.0, "roof closed"
        m = re.match(r"\s*(\d+)\s*mph", w.get("wind") or "")
        if m:
            windspeed = float(m.group(1))
    elif f:
        temp = _f(f.get("temp"))
    if temp is not None:
        t = max(35.0, min(100.0, temp))
        mult *= 1 + (t - 70) * 0.0015
        notes.append("warm +" if t >= 85 else "cold -" if t <= 50 else "")
    if windspeed is not None and wtxt:
        if "out" in wtxt:
            mult *= 1 + min(windspeed, 20) * 0.004
            notes.append("wind out +")
        elif "in" in wtxt:
            mult *= 1 - min(windspeed, 20) * 0.004
            notes.append("wind in -")
    return max(0.85, min(1.18, mult)), ", ".join(n for n in notes if n)


def _pitcher_r9(p, lg_era):
    """Estimate a starter's true runs/9, blending ERA & xERA (regressed)."""
    if not p:
        return lg_era * 1.06, False
    era, xera, f = _f(p.get("era")), _f(p.get("xera")), fip(p)
    if era is not None and xera is not None and f is not None:
        return 0.30 * era + 0.40 * xera + 0.30 * f, True   # ERA + xERA + FIP
    if era is not None and xera is not None:
        return 0.45 * era + 0.55 * xera, True
    if era is not None and f is not None:
        return 0.5 * era + 0.5 * f, True                    # FIP stabilizes when no xERA
    if era is not None:
        try:
            gs = int(p.get("gs") or 0)
        except (TypeError, ValueError):
            gs = 0
        w = min(gs, 12) / 12.0
        return w * era + (1 - w) * lg_era, gs >= 3
    return lg_era, False


def _injury_mult(inj):
    """Dampen offense for significant (60-day IL) position players who are out."""
    if not inj:
        return 1.0
    n = sum(1 for p in inj
            if "60" in (p.get("status") or "") and "pitch" not in (p.get("pos") or "").lower())
    return max(0.94, 1 - n * 0.015)


def _platoon_mult(off, opp_hand):
    """Adjust a team's offense for the opposing starter's handedness (OPS split)."""
    base = _f(off.get("ops"))
    hand = (opp_hand or "").upper()
    split = _f(off.get("opsVsL")) if hand == "L" else _f(off.get("opsVsR")) if hand == "R" else None
    if base and split and base > 0:
        return max(0.90, min(1.10, split / base))
    return 1.0


def _expected_runs(off, deff, lg_rpg, lg_era, park, wx, home):
    rspg = _f(off.get("rspg"))
    recent = _f(off.get("recentRpg"))
    neut_o = 0.5 * (_f(off.get("homePark")) or 1.0) + 0.5   # strip home-park bias
    if rspg and lg_rpg:
        base = 0.7 * rspg + 0.3 * recent if recent is not None else rspg   # recent-form blend
        off_m = max(0.70, min(1.40, (base / neut_o) / lg_rpg))
    else:
        off_m = 1.0
    off_m *= _platoon_mult(off, (deff.get("pitcher") or {}).get("hand"))   # platoon
    off_m *= _injury_mult(off.get("inj"))                                  # injuries
    sr9, _ok = _pitcher_r9(deff.get("pitcher"), lg_era)
    s_mult = (sr9 / lg_era) if lg_era else 1.0
    neut_d = 0.5 * (_f(deff.get("homePark")) or 1.0) + 0.5
    pen_era = _f(deff.get("relEra")) or _f(deff.get("teamEra")) or lg_era  # real bullpen ERA
    p_mult = ((pen_era / neut_d) / lg_era) if lg_era else 1.0
    def_m = max(0.65, min(1.45, 0.62 * s_mult + 0.38 * p_mult))
    lam = lg_rpg * off_m * def_m * park * wx * (1.04 if home else 0.965)
    return max(1.5, min(9.0, lam)), _ok


def _nb_pmf(lam, r=4.0, kmax=24):
    """Negative-binomial run distribution: mean=lam, shape r (~MLB spread)."""
    p = r / (r + lam)
    pmf = [0.0] * (kmax + 1)
    pmf[0] = p ** r
    for k in range(1, kmax + 1):
        pmf[k] = pmf[k - 1] * (k + r - 1) / k * (1 - p)
    s = sum(pmf) or 1.0
    return [x / s for x in pmf]


def game_probs(lam_a, lam_h, ou):
    pa, ph = _nb_pmf(lam_a), _nb_pmf(lam_h)
    n = len(pa)
    p_home_reg = p_tie = 0.0
    total_pmf = [0.0] * (2 * n - 1)
    for a in range(n):
        if pa[a] <= 0:
            continue
        for h in range(n):
            joint = pa[a] * ph[h]
            total_pmf[a + h] += joint
            if h > a:
                p_home_reg += joint
            elif h == a:
                p_tie += joint
    denom = (lam_h + lam_a) or 1.0
    p_home = p_home_reg + p_tie * (lam_h / denom)
    res = {"pAway": 1 - p_home, "pHome": p_home, "total": lam_a + lam_h, "pOver": None}
    if ou is not None:
        res["pOver"] = sum(prob for t, prob in enumerate(total_pmf) if t > ou)
    return res


def market_edge(model, odds, away_team, home_team):
    if not odds:
        return None
    out = {}
    imp_a, imp_h = odds.get("impAway"), odds.get("impHome")
    if imp_a is not None and imp_h is not None:
        out["impAway"], out["impHome"] = round(imp_a, 3), round(imp_h, 3)
        e_h, e_a = model["pHome"] - imp_h, model["pAway"] - imp_a
        if e_h >= e_a:
            out.update(side="home", pick=home_team, edge=round(e_h, 3))
        else:
            out.update(side="away", pick=away_team, edge=round(e_a, 3))
    ou = _f(odds.get("ou"))
    if ou is not None and model.get("pOver") is not None:
        out["ou"] = ou
        out["pOver"] = round(model["pOver"], 3)
        out["totalLean"] = ("Over" if model["pOver"] > 0.52
                            else "Under" if model["pOver"] < 0.48 else "—")
    return out or None


def _confidence(away, home):
    c = 100
    for s in (away, home):
        p = s.get("pitcher")
        if not p:
            c -= 35
            continue
        if p.get("xera") is None:
            c -= 6
        try:
            gs = int(p.get("gs") or 0)
        except (TypeError, ValueError):
            gs = 0
        if gs < 5:
            c -= 8
    if not away.get("rspg") or not home.get("rspg"):
        c -= 10
    return max(20, c)


def run_model(gd, away, home, lg_rpg, lg_era):
    park = park_factor(gd.get("venue", ""))
    wx, wxnote = weather_factor(gd)
    lam_a, _a = _expected_runs(away, home, lg_rpg, lg_era, park, wx, False)
    lam_h, _h = _expected_runs(home, away, lg_rpg, lg_era, park, wx, True)
    ou = _f((gd.get("odds") or {}).get("ou"))
    gm = game_probs(lam_a, lam_h, ou)
    edge = market_edge(gm, gd.get("odds"), away["team"], home["team"])
    drivers = []
    if abs(park - 1) >= 0.03:
        drivers.append(["Park", ("+" if park > 1 else "") + str(round((park - 1) * 100)) + "%"])
    if wxnote:
        drivers.append(["Weather", wxnote])
    ar9, _ar = _pitcher_r9(away.get("pitcher"), lg_era)
    hr9, _hr = _pitcher_r9(home.get("pitcher"), lg_era)
    if abs(ar9 - hr9) >= 0.6:
        drivers.append(["SP edge", home["team"] if hr9 < ar9 else away["team"]])
    for s, opp in ((away, home), (home, away)):
        hand = ((opp.get("pitcher") or {}).get("hand") or "")
        pm = _platoon_mult(s, hand)
        if abs(pm - 1) >= 0.045:
            drivers.append(["Platoon", f'{s["team"].split()[-1]} {"+" if pm > 1 else ""}{round((pm-1)*100)}% v{hand}HP'])
    ah, hh = _f(away.get("relEra")), _f(home.get("relEra"))
    if ah is not None and hh is not None and abs(ah - hh) >= 0.80:
        drivers.append(["Bullpen", (home["team"] if hh < ah else away["team"]).split()[-1] + " pen"])
    for s in (away, home):
        n = sum(1 for p in (s.get("inj") or [])
                if "60" in (p.get("status") or "") and "pitch" not in (p.get("pos") or "").lower())
        if n >= 2:
            drivers.append(["Injuries", f'{s["team"].split()[-1]} -{n}'])
    for s in (away, home):
        rp, sp = _f(s.get("recentRpg")), _f(s.get("rspg"))
        if rp is not None and sp and abs(rp - sp) >= 0.9:
            drivers.append(["Form", f'{s["team"].split()[-1]} {"hot" if rp > sp else "cold"}'])
    conf = _confidence(away, home)
    return {
        "pAway": round(gm["pAway"], 3), "pHome": round(gm["pHome"], 3),
        "expAway": round(lam_a, 1), "expHome": round(lam_h, 1),
        "total": round(gm["total"], 1), "pOver": gm.get("pOver"),
        "conf": conf, "confLabel": "High" if conf >= 80 else "Med" if conf >= 55 else "Low",
        "edge": edge, "drivers": drivers,
    }


# ---- assemble the slate ---------------------------------------------------

def build_slate(target_date):
    season = target_date[:4]
    try:
        _td = datetime.strptime(target_date, "%Y-%m-%d")
        recent_start = (_td - timedelta(days=30)).strftime("%Y-%m-%d")
        recent_end = (_td - timedelta(days=1)).strftime("%Y-%m-%d")
    except ValueError:
        recent_start = recent_end = target_date
    url = (f"{API}/schedule?sportId=1&date={target_date}"
           f"&hydrate=probablePitcher,team,venue(location)")
    raw = (fetch_json(url).get("dates") or [{}])
    raw = raw[0].get("games", []) if raw else []

    pids = set()
    for g in raw:
        for s in ("away", "home"):
            pp = g["teams"][s].get("probablePitcher") or {}
            if pp.get("id"):
                pids.add(pp["id"])

    with ThreadPoolExecutor(max_workers=16) as ex:
        fs = {
            "std": ex.submit(standings, season),
            "team": ex.submit(team_tables, season),
            "lead": ex.submit(leaders, season),
            "xs": ex.submit(savant_xstats, season),
            "ar": ex.submit(savant_arsenal, season),
            "odds": ex.submit(espn_odds, target_date),
            "hands": ex.submit(pitcher_hands, sorted(pids), season),
            "inj": ex.submit(injuries),
            "splits": ex.submit(team_splits, season),
            "homepark": ex.submit(team_home_park, season),
            "recent": ex.submit(team_recent, season, recent_start, recent_end),
        }
        det_f = {pid: ex.submit(pitcher_detail, pid, season) for pid in pids}
        wx_f = {g["gamePk"]: ex.submit(weather, g["gamePk"]) for g in raw}
        fc_f = {}
        for g in raw:
            loc = ((g.get("venue") or {}).get("location") or {}).get("defaultCoordinates", {})
            fc_f[g["gamePk"]] = ex.submit(forecast, loc.get("latitude"),
                                          loc.get("longitude"), g.get("gameDate", ""))
        std = fs["std"].result()
        team = fs["team"].result() or {}
        lead = fs["lead"].result() or {"hitting": [], "pitching": []}
        xs = fs["xs"].result() or {}
        ar = fs["ar"].result() or {}
        odds = fs["odds"].result() or {}
        hands = fs["hands"].result() or {}
        inj_data = fs["inj"].result()
        splits = fs["splits"].result() or {}
        homeparks = fs["homepark"].result() or {}
        recent = fs["recent"].result() or {}
        det = {pid: f.result() for pid, f in det_f.items()}
        wx = {pk: f.result() for pk, f in wx_f.items()}
        fc = {pk: f.result() for pk, f in fc_f.items()}

    byteam = std["byTeam"] if std else {}

    def side(team_obj):
        t = team_obj.get("team", {})
        tid = t.get("id")
        rec = team_obj.get("leagueRecord", {}) or {}
        sd = byteam.get(tid, {})
        tt = team.get(tid, {})
        pp = team_obj.get("probablePitcher") or {}
        pitcher = None
        if pp.get("fullName"):
            pid = pp.get("id")
            d = det.get(pid) or {}
            x = xs.get(pid) or {}
            a = ar.get(pid) or {}
            pitcher = {
                "name": pp["fullName"], "hand": hands.get(pid),
                "era": d.get("era"), "wl": d.get("wl"), "whip": d.get("whip"),
                "k": d.get("k"), "ip": d.get("ip"), "gs": d.get("gs"),
                "bb": d.get("bb"), "hr": d.get("hr"), "hbp": d.get("hbp"),
                "xera": x.get("xera"), "xwoba": x.get("xwoba"),
                "whiff": a.get("whiff"), "hardhit": a.get("hardhit"),
                "arsenal": a.get("pitches"), "last3": d.get("last3"),
                "vsL": d.get("vsL"), "vsR": d.get("vsR"),
            }
            _fv = fip(pitcher)
            pitcher["fip"] = round(_fv, 2) if _fv is not None else None
        return {
            "team": t.get("name", ""), "id": tid,
            "record": (f'{rec.get("wins")}-{rec.get("losses")}'
                       if rec.get("wins") is not None else ""),
            "l10": sd.get("l10"), "streak": sd.get("streak"), "rd": sd.get("rd"),
            "divRank": sd.get("divRank"), "div": sd.get("div"),
            "ops": tt.get("hit", {}).get("ops"),
            "opsRank": tt.get("rank", {}).get("hit_ops"),
            "teamEra": tt.get("pit", {}).get("era"),
            "eraRank": tt.get("rank", {}).get("pit_era"),
            "rspg": tt.get("hit", {}).get("rpg"),
            "opsVsL": splits.get(tid, {}).get("opsVsL"),
            "opsVsR": splits.get(tid, {}).get("opsVsR"),
            "relEra": splits.get(tid, {}).get("relEra"),
            "homePark": homeparks.get(tid, 1.0),
            "recentRpg": recent.get(tid),
            "inj": team_injuries(inj_data, t.get("name", "")),
            "pitcher": pitcher,
        }

    # League run-environment baselines (computed live from the team tables).
    eras = [e for e in (_f(d.get("pit", {}).get("era")) for d in team.values()) if e]
    rpgs = [r for r in (_f(d.get("hit", {}).get("rpg")) for d in team.values()) if r]
    lg_era = sum(eras) / len(eras) if eras else 4.20
    lg_rpg = sum(rpgs) / len(rpgs) if rpgs else 4.40

    games = []
    for g in raw:
        a, h = g["teams"]["away"], g["teams"]["home"]
        key = norm(a["team"].get("name")) + "@" + norm(h["team"].get("name"))
        away_s, home_s = side(a), side(h)
        gd = {
            "gamePk": g["gamePk"],
            "status": (g.get("status") or {}).get("detailedState", ""),
            "firstPitch": format_first_pitch(g.get("gameDate")),
            "sort": g.get("gameDate", ""),
            "venue": (g.get("venue") or {}).get("name", ""),
            "weather": wx.get(g["gamePk"]), "forecast": fc.get(g["gamePk"]),
            "odds": odds.get(key),
            "away": away_s, "home": home_s,
        }
        gd["model"] = run_model(gd, away_s, home_s, lg_rpg, lg_era)
        games.append(gd)
    games.sort(key=lambda x: x["sort"])
    return {"date": target_date, "count": len(games), "games": games,
            "standings": std["divisions"] if std else [], "leaders": lead,
            "injuries": inj_data["byTeam"] if inj_data else {},
            "injuriesCount": inj_data["count"] if inj_data else 0}


# ---- HTTP server ----------------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, body, ctype):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def do_GET(self):
        p = urlparse(self.path)
        if p.path == "/":
            self._send(200, PAGE.encode("utf-8"), "text/html; charset=utf-8")
        elif p.path == "/api/games":
            d = (parse_qs(p.query).get("date") or [today_eastern()])[0]
            try:
                datetime.strptime(d, "%Y-%m-%d")
            except ValueError:
                d = today_eastern()
            try:
                self._send(200, json.dumps(build_slate(d)).encode("utf-8"), "application/json")
            except (HTTPError, URLError) as e:
                self._send(502, json.dumps({"error": f"MLB API unreachable: {e}"}).encode(), "application/json")
            except Exception as e:  # noqa: BLE001
                self._send(500, json.dumps({"error": str(e)}).encode(), "application/json")
        elif p.path == "/favicon.ico":
            self._send(204, b"", "image/x-icon")
        else:
            self._send(404, b"Not found", "text/plain; charset=utf-8")


PAGE = r"""<!DOCTYPE html>
<html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>MLB Dashboard</title>
<style>
  :root{--bg:#12161f;--panel:#1a1f2b;--panel2:#1f2632;--line:#2c3340;--text:#dde3ee;
    --muted:#98a2b5;--accent:#5b9bf5;--accent2:#3b7de0;--chip:#252c3a;--shadow:0 4px 18px rgba(0,0,0,.28);
    --glow:#1a2740;--header:rgba(18,22,31,.86);--th:#1b2331}
  :root[data-theme=light]{--bg:#eef1f6;--panel:#ffffff;--panel2:#f6f8fb;--line:#e0e5ee;--text:#1c2430;
    --muted:#5f6b7e;--accent:#2f6fe0;--accent2:#2560c8;--chip:#eef1f7;--shadow:0 3px 14px rgba(30,45,70,.10);
    --glow:#dde6f5;--header:rgba(245,247,251,.9);--th:#eaeef5}
  *{box-sizing:border-box}
  body{margin:0;background:radial-gradient(1200px 600px at 80% -10%,var(--glow) 0%,var(--bg) 55%);
    color:var(--text);font:15px/1.5 system-ui,-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;min-height:100vh}
  header{position:sticky;top:0;z-index:5;backdrop-filter:blur(8px);background:var(--header);
    border-bottom:1px solid var(--line);padding:10px 16px}
  .bar{display:flex;align-items:center;gap:10px;flex-wrap:wrap}
  .brand{font-size:19px;font-weight:800;letter-spacing:.3px}
  .brand .ball{filter:drop-shadow(0 0 6px rgba(59,130,246,.5))}
  .tabs{display:flex;gap:4px;background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:3px}
  .tabs button{border:0;background:transparent;color:var(--muted);padding:6px 13px;border-radius:8px;font-weight:700;cursor:pointer}
  .tabs button.active{background:var(--accent);color:#fff}
  .ctrl{display:flex;align-items:center;gap:6px;background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:4px}
  button,.btn{background:var(--panel2);color:var(--text);border:1px solid var(--line);border-radius:8px;
    padding:7px 11px;cursor:pointer;font-weight:600;font-size:13px;transition:.15s;white-space:nowrap}
  button:hover{border-color:var(--accent);color:#fff}
  button.primary{background:linear-gradient(180deg,var(--accent),var(--accent2));border-color:transparent}
  input[type=date]{background:var(--panel2);color:var(--text);border:1px solid var(--line);border-radius:8px;
    padding:6px 8px;font:inherit;color-scheme:light dark}
  .seg{display:flex;border:1px solid var(--line);border-radius:9px;overflow:hidden}
  .seg button{border:0;border-radius:0;background:transparent}
  .seg button.active{background:var(--accent);color:#fff}
  label.toggle{display:flex;align-items:center;gap:7px;color:var(--muted);font-size:13px;cursor:pointer}
  .meta{display:flex;align-items:center;gap:12px;margin-top:8px;color:var(--muted);font-size:12.5px;flex-wrap:wrap}
  .pill{background:var(--chip);border:1px solid var(--line);border-radius:999px;padding:3px 10px}
  .legend{display:flex;gap:9px;align-items:center;margin-left:auto;flex-wrap:wrap}
  .legend i{width:11px;height:11px;border-radius:3px;display:inline-block;margin-right:4px;vertical-align:-1px}
  main{padding:16px;max-width:1560px;margin:0 auto}
  .grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(430px,1fr));gap:18px}
  .card{background:linear-gradient(180deg,var(--panel),var(--panel2));border:1px solid var(--line);
    border-radius:16px;padding:16px 17px;box-shadow:var(--shadow);transition:transform .12s,border-color .12s}
  .card:hover{transform:translateY(-3px);border-color:#3946604d}
  .ctop{display:flex;justify-content:space-between;align-items:center;gap:8px;margin-bottom:9px}
  .ctop .right{display:flex;align-items:center;gap:8px;color:var(--muted);font-size:12px;flex-wrap:wrap;justify-content:flex-end}
  .time{font-weight:700;color:var(--text)}
  .badge{font-size:10.5px;font-weight:800;letter-spacing:.4px;text-transform:uppercase;padding:3px 9px;border-radius:999px;border:1px solid transparent}
  .badge.live{background:rgba(46,204,113,.16);color:#2ebd6e;border-color:rgba(46,204,113,.34);animation:pulse 1.6s infinite}
  .badge.sched{background:rgba(91,155,245,.15);color:#4f8ae6;border-color:rgba(91,155,245,.3)}
  .badge.final{background:rgba(140,150,170,.16);color:#8792a5;border-color:rgba(140,150,170,.3)}
  .badge.warn{background:rgba(240,150,50,.16);color:#dd8f2c;border-color:rgba(240,150,50,.34)}
  @keyframes pulse{0%,100%{opacity:1}50%{opacity:.55}}
  .matchup{display:grid;grid-template-columns:1fr auto 1fr;align-items:start;gap:8px;margin-bottom:10px}
  .team.home{text-align:right}
  .tname{font-weight:700;font-size:15.5px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
  .tsub{color:var(--muted);font-size:11.5px;margin-top:2px;line-height:1.5}
  .at{color:var(--muted);font-weight:800;font-size:12px;padding-top:3px}
  .rank{color:#9fb2d6}
  .pitchers{display:grid;grid-template-columns:1fr 1fr;gap:11px}
  .pcol{background:#0e16280f;border:1px solid var(--line);border-radius:11px;padding:11px}
  .pcol.home{text-align:right}
  .plabel{font-size:10px;text-transform:uppercase;letter-spacing:.5px;color:var(--muted)}
  .pname{font-weight:700;margin:2px 0 6px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
  .pname .hand{color:var(--muted);font-weight:600;font-size:11.5px}
  .chips{display:flex;flex-wrap:wrap;gap:4px}
  .pcol.home .chips{justify-content:flex-end}
  .chip{font-size:11px;font-weight:700;background:var(--chip);border:1px solid var(--line);border-radius:7px;padding:2px 6px;color:var(--muted)}
  .chip.era{color:#0c1018}
  .chip b{color:var(--text);font-weight:800}
  .pdet{margin-top:6px;font-size:11px;color:var(--muted);line-height:1.55}
  .pdet .k{color:#9fb2d6}
  .arsenal{margin-top:5px;display:flex;flex-wrap:wrap;gap:4px}
  .pcol.home .arsenal{justify-content:flex-end}
  .pitch{font-size:10.5px;background:#172036;border:1px solid var(--line);border-radius:6px;padding:1px 6px;color:#c7d2e6}
  .cfoot{display:flex;justify-content:space-between;gap:10px;margin-top:10px;padding-top:9px;border-top:1px solid var(--line);color:var(--muted);font-size:12px;flex-wrap:wrap}
  .cfoot .odds b{color:#ffd479}
  .expander{margin-top:8px}
  .expander summary{cursor:pointer;color:#9fb2d6;font-size:11.5px;list-style:none;user-select:none}
  .expander summary::-webkit-details-marker{display:none}
  .expander summary:before{content:'\25B8 ';}
  .expander[open] summary:before{content:'\25BE ';}
  .l3{margin-top:6px;font-size:11px;color:var(--muted)}
  .l3 table{width:100%;border-collapse:collapse}
  .l3 td,.l3 th{padding:2px 6px;text-align:right;border-bottom:1px solid #20283a}
  .l3 th{color:#7f8aa0;font-weight:600}.l3 td:first-child,.l3 th:first-child{text-align:left}
  table.tbl{width:100%;border-collapse:collapse;font-size:13px;background:var(--panel);border-radius:12px;overflow:hidden}
  table.tbl thead th{position:sticky;top:62px;background:var(--th);text-align:left;padding:9px 10px;border-bottom:1px solid var(--line);font-size:12px;color:var(--muted);white-space:nowrap}
  table.tbl th.sortable{cursor:pointer}.table.tbl th.sortable:hover{color:#fff}
  table.tbl tbody td{padding:8px 10px;border-bottom:1px solid #20283a;white-space:nowrap}
  table.tbl tbody tr:hover{background:#1a2236}
  td.era{font-weight:800;border-radius:5px;color:#0c1018;text-align:center}
  .sub{color:var(--muted);font-size:11.5px}
  .stand{display:grid;grid-template-columns:repeat(auto-fill,minmax(440px,1fr));gap:15px}
  .stand h3{margin:0 0 8px;font-size:14px;color:#cdd9f0}
  .stand table{width:100%;border-collapse:collapse;font-size:13px}
  .stand td,.stand th{padding:6px 8px;border-bottom:1px solid #20283a;text-align:right;white-space:nowrap}
  .stand th{color:var(--muted);font-weight:600;font-size:11.5px}
  .stand td:first-child,.stand th:first-child{text-align:left}
  .stand tr.lead td{background:#13251a}
  .pos{color:#54e08a}.neg{color:#ff8a8a}
  .lead-wrap{display:grid;grid-template-columns:1fr 1fr;gap:15px}
  .lead-card{background:var(--panel);border:1px solid var(--line);border-radius:13px;padding:14px}
  .lead-card h3{margin:0 0 10px;font-size:15px}
  .lcat{margin-bottom:11px}
  .lcat .name{font-size:11px;text-transform:uppercase;letter-spacing:.5px;color:var(--muted);margin-bottom:3px}
  .lrow{display:flex;justify-content:space-between;padding:2px 0;border-bottom:1px solid #1c2436}
  .lrow .v{font-weight:800;color:#ffd479}
  .lrow .t{color:var(--muted);font-size:11px}
  .center{text-align:center;color:var(--muted);padding:60px 10px}
  .spinner{width:34px;height:34px;border:3px solid var(--line);border-top-color:var(--accent);border-radius:50%;animation:spin .8s linear infinite;margin:0 auto 12px}
  @keyframes spin{to{transform:rotate(360deg)}}
  .err{color:#ff8a8a;background:#2a1620;border:1px solid #5a2230;border-radius:12px;padding:16px;text-align:center}
  .hidden{display:none}
  .model{margin-top:9px;padding-top:9px;border-top:1px solid var(--line)}
  .winbar{display:flex;height:19px;border-radius:6px;overflow:hidden;border:1px solid var(--line);font-size:11px;font-weight:800}
  .winbar .wa{background:linear-gradient(90deg,#2c4575,#3b82f6);color:#fff;display:flex;align-items:center;padding:0 6px;min-width:32px}
  .winbar .wh{background:linear-gradient(90deg,#1f6f43,#27c46b);color:#06210f;display:flex;align-items:center;justify-content:flex-end;padding:0 6px;min-width:32px}
  .mrow{display:flex;justify-content:space-between;align-items:center;margin-top:5px;font-size:12px;color:var(--muted);gap:8px}
  .mrow b{color:var(--text)}
  .mrow.edge .val{color:#ffd479;font-weight:800}
  .conf{font-size:10px;font-weight:800;padding:2px 7px;border-radius:999px;border:1px solid var(--line);white-space:nowrap}
  .conf.high{background:rgba(46,204,113,.15);color:#2ebd6e}.conf.med{background:rgba(230,180,60,.16);color:#c99a2e}.conf.low{background:rgba(239,84,84,.15);color:#df5656}
  .disc{margin-top:7px;color:#7f8aa0;font-size:11.5px;line-height:1.5}
  tr.val td{background:#221b0e}
  .topedges{background:linear-gradient(180deg,#1d1a0e,#171b29);border:1px solid #4a3f1e;border-radius:14px;padding:12px 14px;margin-bottom:16px}
  .te-head{font-size:14px;font-weight:800;color:#ffd479;margin-bottom:10px}
  .te-head .te-sub{color:var(--muted);font-weight:500;font-size:11.5px}
  .te-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(245px,1fr));gap:10px}
  .te-card{background:#12182699;border:1px solid var(--line);border-radius:11px;padding:10px 11px}
  .te-match{font-size:11.5px;color:var(--muted);margin-bottom:5px;display:flex;justify-content:space-between;gap:8px}
  .te-pick{font-weight:800;font-size:15px}
  .te-pick .te-edge{color:#ffd479;margin-left:4px}
  .te-meta{font-size:11px;color:var(--muted);margin-top:5px;display:flex;align-items:center;gap:6px;flex-wrap:wrap}
  .te-chip{background:#222c41;border:1px solid var(--line);border-radius:6px;padding:1px 6px;color:#c7d2e6}
  .injflag{color:#ff9a6b;font-weight:700}
  .injrow{padding:6px 0;border-bottom:1px solid #1c2436}
  .injrow:last-child{border-bottom:0}
  .injname{font-weight:700}
  .injpos{color:var(--muted);font-size:11px}
  .injmeta{display:flex;gap:8px;align-items:center;flex-wrap:wrap;font-size:11.5px;color:var(--muted);margin-top:2px}
  .injbadge{font-size:10px;font-weight:800;padding:1px 7px;border-radius:999px;border:1px solid var(--line)}
  .injbadge.out{background:rgba(239,84,84,.15);color:#df5656}.injbadge.il{background:rgba(240,150,50,.16);color:#dd8f2c}.injbadge.day{background:rgba(91,155,245,.15);color:#4f8ae6}
  .drv{margin-top:6px;color:var(--muted);font-size:11px;line-height:1.55}
  .injret{color:#54e08a}
  @media(max-width:560px){.lead-wrap{grid-template-columns:1fr}}
</style></head>
<body>
<header>
  <div class="bar">
    <div class="brand"><span class="ball">&#9918;</span> MLB Dashboard</div>
    <div class="tabs" id="tabs">
      <button data-tab="games" class="active">Games</button>
      <button data-tab="edges">Edges</button>
      <button data-tab="standings">Standings</button>
      <button data-tab="leaders">Leaders</button>
      <button data-tab="injuries">Injuries</button>
    </div>
    <div class="ctrl" id="dateCtrl">
      <button id="prev" title="Previous day">&#8592;</button>
      <input type="date" id="date">
      <button id="next" title="Next day">&#8594;</button>
      <button id="today">Today</button>
    </div>
    <button id="refresh" class="primary">&#8635; Refresh</button>
    <div class="seg" id="view"><button data-view="cards" class="active">Cards</button><button data-view="table">Table</button></div>
    <label class="toggle"><input type="checkbox" id="auto"> Auto 60s</label>
    <button id="theme" title="Light / dark theme">&#127769;</button>
  </div>
  <div class="meta">
    <span class="pill" id="dateLabel">&mdash;</span>
    <span class="pill" id="count">&mdash;</span>
    <span id="updated"></span>
    <span class="legend">ERA/xERA:
      <span><i style="background:#27c46b"></i>&lt;2.75</span><span><i style="background:#86d549"></i>&lt;3.75</span>
      <span><i style="background:#f4c542"></i>&lt;4.5</span><span><i style="background:#f59e42"></i>&lt;5.5</span>
      <span><i style="background:#ef5454"></i>5.5+</span></span>
  </div>
  <div class="disc">&#9888;&#65039; Model = first-principles run-expectancy estimate (no historical backtesting). For research/entertainment only &mdash; not betting advice.</div>
</header>
<main id="content"><div class="center"><div class="spinner"></div>Loading the slate&hellip;</div></main>
<script>
const state={data:null,date:null,tab:'games',view:'cards',sortKey:'time',sortDir:1,auto:false,timer:null};
const $=s=>document.querySelector(s);
try{state.view=localStorage.getItem('mlb_view')||'cards';state.auto=localStorage.getItem('mlb_auto')==='1';state.tab=localStorage.getItem('mlb_tab')||'games';}catch(e){}

function eraColor(v){v=parseFloat(v);if(isNaN(v))return '#7f8aa0';if(v<2.75)return '#27c46b';if(v<3.75)return '#86d549';if(v<4.5)return '#f4c542';if(v<5.5)return '#f59e42';return '#ef5454';}
function num(x){const v=parseFloat(x);return isNaN(v)?Infinity:v;}
function esc(s){return (s==null?'':String(s)).replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));}
function ord(n){n=parseInt(n);if(isNaN(n))return '';const s=['th','st','nd','rd'],v=n%100;return n+(s[(v-20)%10]||s[v]||s[0]);}
function statusClass(s){s=(s||'').toLowerCase();if(s.includes('progress')||s.includes('live')||s.includes('warmup'))return 'live';if(s.includes('final')||s.includes('game over')||s.includes('completed'))return 'final';if(s.includes('postpon')||s.includes('suspend')||s.includes('cancel')||s.includes('delay'))return 'warn';return 'sched';}
const WMO={0:['☀️','Clear'],1:['🌤️','Mainly clear'],2:['⛅','Partly cloudy'],3:['☁️','Overcast'],45:['🌫️','Fog'],48:['🌫️','Fog'],51:['🌦️','Drizzle'],53:['🌦️','Drizzle'],55:['🌦️','Drizzle'],61:['🌧️','Rain'],63:['🌧️','Rain'],65:['🌧️','Heavy rain'],71:['❄️','Snow'],73:['❄️','Snow'],75:['❄️','Snow'],80:['🌦️','Showers'],81:['🌧️','Showers'],82:['⛈️','Heavy showers'],95:['⛈️','Thunderstorm'],96:['⛈️','Thunderstorm'],99:['⛈️','Thunderstorm']};
function windArrow(d){if(d==null)return '';const dirs=['↓','↙','←','↖','↑','↗','→','↘'];return dirs[Math.round(((d%360)/45))%8];}
function wxText(g){
  const w=g.weather;
  if(w){const t=w.temp?w.temp+'&deg;':'';const wind=w.wind?' &middot; '+esc(w.wind):'';
    let ic='🌡️';const c=(w.condition||'').toLowerCase();
    if(c.includes('rain')||c.includes('shower'))ic='🌧️';else if(c.includes('cloud')||c.includes('overcast'))ic='☁️';
    else if(c.includes('dome')||c.includes('roof'))ic='🏟️';else if(c.includes('clear')||c.includes('sunny'))ic='☀️';else if(c.includes('partly'))ic='⛅';
    return ic+' '+esc(w.condition)+(t?' '+t:'')+wind;}
  const f=g.forecast;
  if(f){const m=WMO[f.code]||['🌡️',''];const t=f.temp!=null?Math.round(f.temp)+'&deg;':'';
    const wind=f.wind!=null?' &middot; '+windArrow(f.winddir)+Math.round(f.wind)+'mph':'';
    const rain=(f.precip!=null&&f.precip>=5)?' &middot; '+f.precip+'%☔':'';
    return m[0]+' '+t+wind+rain+' <span style="opacity:.6">(fcst)</span>';}
  return '<span style="opacity:.55">&mdash;</span>';
}
function chip(label,cls,color){const st=color?` style="background:${color};border-color:${color}"`:'';return `<span class="chip ${cls||''}"${st}>${label}</span>`;}
function teamSub(s){
  const bits=[];
  if(s.record)bits.push('<b style="color:#cdd9f0">'+esc(s.record)+'</b>');
  if(s.l10)bits.push('L10 '+esc(s.l10));
  if(s.streak)bits.push(esc(s.streak));
  if(s.inj&&s.inj.length)bits.push('<span class="injflag" title="'+esc(s.inj.slice(0,8).map(x=>x.name).join(', '))+'">&#127973; '+s.inj.length+'</span>');
  let line2=[];
  if(s.ops)line2.push('OPS '+esc(s.ops)+(s.opsRank?' <span class="rank">('+ord(s.opsRank)+')</span>':''));
  if(s.teamEra)line2.push('tmERA '+esc(s.teamEra)+(s.eraRank?' <span class="rank">('+ord(s.eraRank)+')</span>':''));
  if(s.rd!=null)line2.push('<span class="'+(s.rd>=0?'pos':'neg')+'">'+(s.rd>=0?'+':'')+s.rd+'</span>');
  return '<div class="tsub">'+bits.join(' &middot; ')+(line2.length?'<br>'+line2.join(' &middot; '):'')+'</div>';
}
function pitcherBlock(p,home){
  if(!p)return '<div class="plabel">'+(home?'Home':'Away')+' starter</div><div class="pname" style="opacity:.7">TBD</div>';
  const c=eraColor(p.era);
  const chips=[chip('<b>'+esc(p.era||'-.--')+'</b> ERA','era',c)];
  if(p.xera)chips.push(chip('<b>'+esc(p.xera)+'</b> xERA',null,null));
  if(p.fip!=null)chips.push(chip('<b>'+esc(p.fip)+'</b> FIP'));
  if(p.wl)chips.push(chip(esc(p.wl)));
  if(p.whip)chips.push(chip('<b>'+esc(p.whip)+'</b> WHIP'));
  if(p.k!=null)chips.push(chip('<b>'+esc(p.k)+'</b> K'));
  let det=[];
  if(p.whiff!=null)det.push('<span class="k">Whiff</span> '+esc(p.whiff)+'%');
  if(p.hardhit!=null)det.push('<span class="k">HH</span> '+esc(p.hardhit)+'%');
  if(p.vsL&&p.vsL.avg)det.push('<span class="k">vL</span> '+esc(p.vsL.avg));
  if(p.vsR&&p.vsR.avg)det.push('<span class="k">vR</span> '+esc(p.vsR.avg));
  let ars='';
  if(p.arsenal&&p.arsenal.length){ars='<div class="arsenal">'+p.arsenal.map(x=>'<span class="pitch">'+esc(x.name||'')+' '+esc(x.usage)+'%</span>').join('')+'</div>';}
  let l3='';
  if(p.last3&&p.last3.length){
    l3='<details class="expander"><summary>Last '+p.last3.length+' starts</summary><div class="l3"><table><tr><th>Date</th><th>Opp</th><th>IP</th><th>ER</th><th>K</th><th>H</th></tr>'+
      p.last3.slice().reverse().map(s=>'<tr><td>'+esc((s.date||'').slice(5))+'</td><td style="text-align:left">'+esc(s.opp||'')+'</td><td>'+esc(s.ip)+'</td><td>'+esc(s.er)+'</td><td>'+esc(s.so)+'</td><td>'+esc(s.h)+'</td></tr>').join('')+'</table></div></details>';
  }
  const hand=p.hand?' <span class="hand">('+esc(p.hand)+'HP)</span>':'';
  return '<div class="plabel">'+(home?'Home':'Away')+' starter</div><div class="pname">'+esc(p.name)+hand+'</div>'+
    '<div class="chips">'+chips.join('')+'</div>'+(det.length?'<div class="pdet">'+det.join(' &middot; ')+'</div>':'')+ars+l3;
}
function oddsText(o){
  if(!o)return '';
  let parts=[];
  if(o.details)parts.push('<b>'+esc(o.details)+'</b>');
  if(o.ou!=null)parts.push('O/U '+esc(o.ou));
  return parts.length?'💰 '+parts.join(' &middot; '):'';
}
function pct(x){return x==null?'':Math.round(x*100)+'%';}
function modelBlock(g){
  const m=g.model;if(!m)return '';
  const aw=Math.round(m.pAway*100),hw=Math.round(m.pHome*100);
  const e=m.edge;let eline='';
  if(e&&e.edge!=null){const ev=Math.round(e.edge*100),strong=Math.abs(e.edge)>=0.05;
    eline=`<span class="${strong?'val':''}">${e.side==='home'?esc(g.home.team):esc(g.away.team)} ${ev>=0?'+':''}${ev}% ML</span>`;}
  let tline='';
  if(e&&e.ou!=null){tline=(eline?' &middot; ':'')+(e.totalLean==='—'?('tot '+m.total):('lean '+e.totalLean+' '+e.ou+' &middot; '+pct(e.pOver)+' O'));}
  const drv=(m.drivers||[]).map(d=>esc(d[0])+' '+esc(d[1])).join('  &middot;  ');
  return `<div class="model">
    <div class="winbar"><div class="wa" style="width:${aw}%">${aw}%</div><div class="wh" style="width:${hw}%">${hw}%</div></div>
    <div class="mrow"><span>Proj <b>${m.expAway} &ndash; ${m.expHome}</b> &middot; tot ${m.total}</span><span class="conf ${m.confLabel.toLowerCase()}">${m.confLabel} conf</span></div>
    ${(eline||tline)?`<div class="mrow edge">${eline}${tline}</div>`:''}
    ${drv?`<div class="drv">${drv}</div>`:''}
  </div>`;
}
function topEdgesBanner(){
  const all=(state.data.games||[]).filter(g=>g.model&&g.model.pHome!=null);
  const withEdge=all.filter(g=>g.model.edge&&g.model.edge.edge!=null&&Math.abs(g.model.edge.edge)>=0.03)
    .sort((a,b)=>Math.abs(b.model.edge.edge)-Math.abs(a.model.edge.edge));
  if(withEdge.length){
    const cards=withEdge.slice(0,3).map(g=>{
      const e=g.model.edge,m=g.model,ev=Math.round(e.edge*100);
      const pick=e.side==='home'?g.home.team:g.away.team;
      const mp=Math.round((e.side==='home'?m.pHome:m.pAway)*100);
      const ip=e.side==='home'?e.impHome:e.impAway;
      const ipt=ip!=null?Math.round(ip*100)+'%':'—';
      const tot=(e.ou!=null&&e.totalLean&&e.totalLean!=='—')?`<span class="te-chip">${e.totalLean} ${e.ou}</span>`:'';
      return `<div class="te-card">
        <div class="te-match"><span>${esc(g.away.team)} @ ${esc(g.home.team)}</span><span>${esc(g.firstPitch)}</span></div>
        <div class="te-pick">${esc(pick)} <span class="te-edge">+${ev}%</span></div>
        <div class="te-meta">model ${mp}% &middot; mkt ${ipt} <span class="conf ${m.confLabel.toLowerCase()}">${m.confLabel}</span> ${tot}</div>
      </div>`;
    }).join('');
    return `<div class="topedges"><div class="te-head">&#127919; Top Edges of the Day <span class="te-sub">biggest model-vs-market gaps &middot; not betting advice</span></div><div class="te-grid">${cards}</div></div>`;
  }
  // No market posted yet -> show the model's strongest leans so the day still loads
  const leans=all.slice().sort((a,b)=>Math.abs(b.model.pHome-0.5)-Math.abs(a.model.pHome-0.5)).slice(0,3);
  if(!leans.length)return '';
  const cards=leans.map(g=>{
    const m=g.model,homeFav=m.pHome>=0.5,pick=homeFav?g.home.team:g.away.team;
    const pc=Math.round((homeFav?m.pHome:m.pAway)*100);
    return `<div class="te-card">
      <div class="te-match"><span>${esc(g.away.team)} @ ${esc(g.home.team)}</span><span>${esc(g.firstPitch)}</span></div>
      <div class="te-pick">${esc(pick)} <span class="te-edge">${pc}%</span></div>
      <div class="te-meta">proj ${m.expAway}&ndash;${m.expHome} &middot; tot ${m.total} <span class="conf ${m.confLabel.toLowerCase()}">${m.confLabel}</span></div>
    </div>`;
  }).join('');
  return `<div class="topedges"><div class="te-head">&#127919; Top Model Leans <span class="te-sub">market not posted yet &middot; model projections, not betting advice</span></div><div class="te-grid">${cards}</div></div>`;
}
function renderCards(){
  const games=state.data.games;
  if(!games.length){$('#content').innerHTML=emptyMsg();return;}
  $('#content').innerHTML=topEdgesBanner()+'<div class="grid">'+games.map(g=>{
    const tv=g.odds&&g.odds.tv?'<span title="Broadcast">📺 '+esc(g.odds.tv)+'</span>':'';
    return `<div class="card">
      <div class="ctop"><span class="badge ${statusClass(g.status)}">${esc(g.status||'')}</span>
        <span class="right"><span class="time">${esc(g.firstPitch)}</span>${tv}</span></div>
      <div class="matchup">
        <div class="team away"><div class="tname">${esc(g.away.team)}</div>${teamSub(g.away)}</div>
        <div class="at">@</div>
        <div class="team home"><div class="tname">${esc(g.home.team)}</div>${teamSub(g.home)}</div>
      </div>
      <div class="pitchers"><div class="pcol away">${pitcherBlock(g.away.pitcher,false)}</div>
        <div class="pcol home">${pitcherBlock(g.home.pitcher,true)}</div></div>
      ${modelBlock(g)}
      <div class="cfoot"><span class="odds">${oddsText(g.odds)}</span><span class="wx">${wxText(g)}</span></div>
    </div>`;}).join('')+'</div>';
}
const COLS={time:g=>g.sort,aera:g=>num(g.away.pitcher&&g.away.pitcher.era),hera:g=>num(g.home.pitcher&&g.home.pitcher.era),status:g=>g.status};
function pCell(p){if(!p)return '<td><span class="sub">TBD</span></td><td class="sub">-</td>';
  const c=eraColor(p.era);const x=p.xera?' <span class="sub">x'+esc(p.xera)+'</span>':'';
  return '<td>'+esc(p.name)+(p.hand?' <span class="sub">('+esc(p.hand)+')</span>':'')+'<div class="sub">'+esc(p.wl)+' &middot; '+esc(p.whip)+' WHIP &middot; '+esc(p.k)+'K'+(p.whiff!=null?' &middot; '+esc(p.whiff)+'% whiff':'')+'</div></td>'+
    '<td class="era" style="background:'+c+'">'+esc(p.era||'-')+x+'</td>';}
function renderTable(){
  const games=state.data.games;
  if(!games.length){$('#content').innerHTML=emptyMsg();return;}
  const head='<tr><th class="sortable" data-k="time">Time</th><th class="sortable" data-k="status">Status</th>'+
    '<th>Away</th><th>Starter</th><th class="sortable" data-k="aera">ERA/x</th>'+
    '<th>Home</th><th>Starter</th><th class="sortable" data-k="hera">ERA/x</th><th>Odds</th><th>Weather</th></tr>';
  const rows=games.map(g=>`<tr><td>${esc(g.firstPitch)}</td><td><span class="badge ${statusClass(g.status)}">${esc(g.status||'')}</span></td>
    <td>${esc(g.away.team)}<div class="sub">${esc(g.away.record)}</div></td>${pCell(g.away.pitcher)}
    <td>${esc(g.home.team)}<div class="sub">${esc(g.home.record)}</div></td>${pCell(g.home.pitcher)}
    <td class="sub">${oddsText(g.odds).replace('💰 ','')||'-'}</td><td>${wxText(g)}</td></tr>`).join('');
  $('#content').innerHTML=topEdgesBanner()+'<div style="overflow-x:auto"><table class="tbl"><thead>'+head+'</thead><tbody>'+rows+'</tbody></table></div>';
  document.querySelectorAll('th.sortable').forEach(th=>th.onclick=()=>{const k=th.dataset.k;state.sortDir=state.sortKey===k?-state.sortDir:1;state.sortKey=k;sortGames();renderTable();});
}
function sortGames(){const f=COLS[state.sortKey]||COLS.time;state.data.games.sort((a,b)=>{let x=f(a),y=f(b);return (typeof x==='string'?x.localeCompare(y):x-y)*state.sortDir;});}
function renderStandings(){
  const divs=state.data.standings||[];
  if(!divs.length){$('#content').innerHTML='<div class="center">Standings unavailable.</div>';return;}
  $('#content').innerHTML='<div class="stand">'+divs.map(d=>{
    const rows=d.teams.map((t,i)=>`<tr class="${i===0?'lead':''}"><td>${esc(t.name)}</td><td>${esc(t.w)}</td><td>${esc(t.l)}</td>
      <td>${esc(t.pct)}</td><td>${t.gb==='-'?'&mdash;':esc(t.gb)}</td><td>${esc(t.l10)}</td><td>${esc(t.streak||'')}</td>
      <td class="${t.rd>=0?'pos':'neg'}">${t.rd>=0?'+':''}${esc(t.rd)}</td></tr>`).join('');
    return `<div class="lead-card"><h3>${esc(d.division)}</h3><table>
      <tr><th>Team</th><th>W</th><th>L</th><th>PCT</th><th>GB</th><th>L10</th><th>Strk</th><th>RD</th></tr>${rows}</table></div>`;
  }).join('')+'</div>';
}
function renderLeaders(){
  const L=state.data.leaders||{hitting:[],pitching:[]};
  const NAMES={battingAverage:'Batting Avg',homeRuns:'Home Runs',runsBattedIn:'RBI',onBasePlusSlugging:'OPS',stolenBases:'Stolen Bases',earnedRunAverage:'ERA',strikeouts:'Strikeouts',wins:'Wins',saves:'Saves',whip:'WHIP'};
  function block(title,cats){return `<div class="lead-card"><h3>${title}</h3>`+(cats.length?cats.map(c=>`<div class="lcat"><div class="name">${esc(NAMES[c.cat]||c.cat)}</div>`+
    c.leaders.map(x=>`<div class="lrow"><span>${esc(x.name)} <span class="t">${esc(x.team)}</span></span><span class="v">${esc(x.val)}</span></div>`).join('')+'</div>').join(''):'<div class="sub">Unavailable</div>')+'</div>';}
  $('#content').innerHTML='<div class="lead-wrap">'+block('⚾ Hitting',L.hitting)+block('🔥 Pitching',L.pitching)+'</div>';
}
function renderEdges(){
  const all=(state.data.games||[]).filter(g=>g.model&&g.model.pHome!=null);
  const games=all.filter(g=>g.model.edge&&g.model.edge.edge!=null)
    .sort((a,b)=>Math.abs(b.model.edge.edge)-Math.abs(a.model.edge.edge));
  if(games.length){
    const rows=games.map(g=>{const m=g.model,e=m.edge,ev=Math.round(e.edge*100),strong=Math.abs(e.edge)>=0.05;
      return `<tr class="${strong?'val':''}"><td>${esc(g.firstPitch)}</td>
        <td>${esc(g.away.team)} @ ${esc(g.home.team)}</td>
        <td>${Math.round(m.pAway*100)}% / ${Math.round(m.pHome*100)}%</td>
        <td>${e.impAway!=null?Math.round(e.impAway*100)+'% / '+Math.round(e.impHome*100)+'%':'&mdash;'}</td>
        <td><b>${e.side==='home'?esc(g.home.team):esc(g.away.team)}</b> ${ev>=0?'+':''}${ev}%</td>
        <td>${m.total}${e.ou!=null?' <span class="sub">(O/U '+e.ou+')</span>':''}</td>
        <td>${e.ou!=null?(e.totalLean==='—'?'&mdash;':e.totalLean+' '+pct(m.pOver)):'&mdash;'}</td>
        <td><span class="conf ${m.confLabel.toLowerCase()}">${m.confLabel}</span></td></tr>`;}).join('');
    $('#content').innerHTML=`<div style="overflow-x:auto"><table class="tbl"><thead><tr>
      <th>Time</th><th>Matchup</th><th>Model A/H</th><th>Market A/H</th><th>ML Edge</th><th>Proj Tot</th><th>Total Lean</th><th>Conf</th></tr></thead><tbody>${rows}</tbody></table></div>
      <div class="disc" style="margin-top:12px">Edge = model win% &minus; market implied% (de-vigged when both moneylines are posted). A positive edge means the model rates that side higher than the betting market. First-principles estimate, not betting advice.</div>`;
    return;
  }
  // No market posted yet -> show the model's own projections so the day still loads
  const proj=all.slice().sort((a,b)=>Math.abs(b.model.pHome-0.5)-Math.abs(a.model.pHome-0.5));
  if(!proj.length){$('#content').innerHTML='<div class="center">No games scheduled for this date.</div>';return;}
  const rows=proj.map(g=>{const m=g.model,homeFav=m.pHome>=0.5,pick=homeFav?g.home.team:g.away.team,pc=Math.round((homeFav?m.pHome:m.pAway)*100);
    return `<tr><td>${esc(g.firstPitch)}</td>
      <td>${esc(g.away.team)} @ ${esc(g.home.team)}</td>
      <td>${Math.round(m.pAway*100)}% / ${Math.round(m.pHome*100)}%</td>
      <td><b>${esc(pick)}</b> ${pc}%</td>
      <td>${m.expAway} &ndash; ${m.expHome}</td>
      <td>${m.total}</td>
      <td><span class="conf ${m.confLabel.toLowerCase()}">${m.confLabel}</span></td></tr>`;}).join('');
  $('#content').innerHTML=`<div class="disc" style="margin:0 0 12px">Betting market isn't posted for this date yet (sportsbook lines usually appear game-day). Showing your <b>model's projections</b> &mdash; edges vs the market appear here automatically once lines are up.</div>
    <div style="overflow-x:auto"><table class="tbl"><thead><tr><th>Time</th><th>Matchup</th><th>Model A/H</th><th>Model pick</th><th>Proj score</th><th>Proj total</th><th>Conf</th></tr></thead><tbody>${rows}</tbody></table></div>`;
}
function renderInjuries(){
  const byTeam=state.data.injuries||{};
  const teams=Object.keys(byTeam).sort();
  if(!teams.length){$('#content').innerHTML='<div class="center">No injury data (needs the BALLDONTLIE key &amp; ALL-STAR tier).</div>';return;}
  const sc=s=>{s=(s||'').toLowerCase();if(s.includes('60')||s==='out')return 'out';if(s.includes('il'))return 'il';return 'day';};
  const cards=teams.map(t=>{
    const rows=byTeam[t].map(p=>`<div class="injrow">
      <div><span class="injname">${esc(p.name)}</span> <span class="injpos">${esc(p.pos||'')}</span></div>
      <div class="injmeta"><span class="injbadge ${sc(p.status)}">${esc(p.status||'')}</span>
        <span>${esc([p.type,p.detail].filter(Boolean).join(' / '))}</span>
        ${p.return?'<span class="injret">&#8618; '+esc(p.return)+'</span>':''}</div>
    </div>`).join('');
    return `<div class="lead-card"><h3>${esc(t)} <span class="sub">${byTeam[t].length}</span></h3>${rows}</div>`;
  }).join('');
  $('#content').innerHTML=`<div class="meta" style="margin:0 0 12px"><span class="pill">${state.data.injuriesCount||0} injuries &middot; ${teams.length} teams</span><span class="sub">via BALLDONTLIE &middot; current status</span></div><div class="stand">${cards}</div>`;
}
function render(){
  document.querySelectorAll('#tabs button').forEach(b=>b.classList.toggle('active',b.dataset.tab===state.tab));
  document.querySelectorAll('#view button').forEach(b=>b.classList.toggle('active',b.dataset.view===state.view));
  $('#view').classList.toggle('hidden',state.tab!=='games');
  $('#dateCtrl').classList.toggle('hidden',state.tab==='standings'||state.tab==='leaders'||state.tab==='injuries');
  if(!state.data){return;}
  if(state.tab==='standings')return renderStandings();
  if(state.tab==='leaders')return renderLeaders();
  if(state.tab==='injuries')return renderInjuries();
  if(state.tab==='edges')return renderEdges();
  if(state.view==='table'){sortGames();renderTable();}else renderCards();
}
function emptyMsg(){return '<div class="center"><div style="font-size:34px">😶</div>No MLB games scheduled for this date.</div>';}
async function load(d){
  $('#content').innerHTML='<div class="center"><div class="spinner"></div>Fetching from MLB, Statcast, ESPN &amp; Open-Meteo&hellip;</div>';
  try{
    const r=await fetch('/api/games'+(d?('?date='+d):''));const data=await r.json();
    if(data.error){$('#content').innerHTML='<div class="err">Could not load: '+esc(data.error)+'</div>';return;}
    state.data=data;state.date=data.date;$('#date').value=data.date;
    const dt=new Date(data.date+'T12:00:00');
    $('#dateLabel').textContent=dt.toLocaleDateString(undefined,{weekday:'long',month:'long',day:'numeric',year:'numeric'});
    $('#count').textContent=data.count+(data.count===1?' game':' games');
    $('#updated').textContent='Updated '+new Date().toLocaleTimeString();
    render();
  }catch(e){$('#content').innerHTML='<div class="err">Network error: '+esc(e.message)+'</div>';}
}
function shiftDay(n){const d=new Date(($('#date').value||state.date)+'T12:00:00');d.setDate(d.getDate()+n);load(d.toISOString().slice(0,10));}
$('#prev').onclick=()=>shiftDay(-1);$('#next').onclick=()=>shiftDay(1);$('#today').onclick=()=>load(null);
$('#refresh').onclick=()=>load($('#date').value);$('#date').onchange=()=>load($('#date').value);
document.querySelectorAll('#tabs button').forEach(b=>b.onclick=()=>{state.tab=b.dataset.tab;try{localStorage.setItem('mlb_tab',state.tab)}catch(e){}render();});
document.querySelectorAll('#view button').forEach(b=>b.onclick=()=>{state.view=b.dataset.view;try{localStorage.setItem('mlb_view',state.view)}catch(e){}render();});
const ab=$('#auto');ab.checked=state.auto;
function applyAuto(){if(state.timer){clearInterval(state.timer);state.timer=null;}if(state.auto)state.timer=setInterval(()=>load($('#date').value),60000);}
ab.onchange=()=>{state.auto=ab.checked;try{localStorage.setItem('mlb_auto',state.auto?'1':'0')}catch(e){}applyAuto();};
const themeBtn=$('#theme');
function applyTheme(t){document.documentElement.setAttribute('data-theme',t);themeBtn.textContent=t==='light'?'☀️':'🌙';}
let theme='dark';try{theme=localStorage.getItem('mlb_theme')||'dark';}catch(e){}
applyTheme(theme);
themeBtn.onclick=()=>{theme=theme==='light'?'dark':'light';try{localStorage.setItem('mlb_theme',theme)}catch(e){}applyTheme(theme);};
applyAuto();load(null);
</script>
</body></html>
"""


def main():
    # Cloud hosts (Render, Railway, Fly, ...) inject $PORT and expect 0.0.0.0.
    env_port = os.environ.get("PORT")
    cloud = bool(env_port)
    port = int(env_port) if env_port else 8765
    if not cloud and len(sys.argv) > 1:
        try:
            port = int(sys.argv[1])
        except ValueError:
            sys.exit("Port must be a number, e.g. python mlb_app.py 9000")
    host = os.environ.get("HOST") or ("0.0.0.0" if cloud else "127.0.0.1")
    try:
        server = ThreadingHTTPServer((host, port), Handler)
    except OSError as e:
        sys.exit(f"Could not start on {host}:{port} ({e}). Try: python mlb_app.py {port + 1}")
    print(f"MLB dashboard running on {host}:{port}")
    if not cloud:
        print("Leave this window open. Press Ctrl+C to stop.")
        try:
            threading.Timer(0.7, lambda: webbrowser.open(f"http://localhost:{port}/")).start()
        except Exception:
            pass
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down.")
        server.shutdown()


if __name__ == "__main__":
    main()
