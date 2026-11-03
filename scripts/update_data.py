#!/usr/bin/env python3
"""Collect live inputs for the typhoon globe.

Writes three files into data/:
  wind.png    NOAA GFS 0.5 deg 10 m wind, u in red, v in green (scaled to +-WMAX m/s)
  wind.json   metadata for wind.png (model cycle, valid time, scale)
  storms.json active tropical cyclones from JMA (NW Pacific) and GDACS (all basins)

A source that fails leaves its previous file untouched, so the site keeps the last good data.
"""
import json
import math
import os
import sys
import tempfile
import time
import urllib.request
from datetime import datetime, timedelta, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "data")
UA = {"User-Agent": "typhoon-globe/1.0 (+https://wogma3067-stack.github.io)"}
WMAX = 40.0  # m/s mapped to the full 0..255 range


def log(*a):
    print(*a, file=sys.stderr, flush=True)


def get(url, timeout=60, tries=3):
    last = None
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers=UA)
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read()
        except Exception as e:  # network hiccups are common on NOMADS
            last = e
            time.sleep(3 * (i + 1))
    raise last


def get_json(url):
    return json.loads(get(url).decode("utf-8"))


def iso(dt):
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def write_if_changed(name, obj, stamp_key="updated"):
    """Write JSON only when the content (ignoring the stamp) changed, to avoid empty commits."""
    path = os.path.join(OUT, name)
    old = None
    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as f:
                old = json.load(f)
        except Exception:
            old = None
    strip = lambda d: {k: v for k, v in (d or {}).items() if k != stamp_key}
    if old is not None and strip(old) == strip(obj):
        log(f"{name}: unchanged")
        return False
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, separators=(",", ":"))
    log(f"{name}: written")
    return True


# --------------------------------------------------------------------------- wind
FRAME_STEP = 6     # hours between forecast wind frames
FRAME_MAX = 132    # last forecast hour fetched (covers +120 h from "now" even late in a cycle)


def gfs_url(cyc, fh):
    return (
        "https://nomads.ncep.noaa.gov/cgi-bin/filter_gfs_0p50.pl"
        f"?dir=%2Fgfs.{cyc:%Y%m%d}%2F{cyc:%H}%2Fatmos"
        f"&file=gfs.t{cyc:%H}z.pgrb2full.0p50.f{fh:03d}"
        "&var_UGRD=on&var_VGRD=on&lev_10_m_above_ground=on"
    )


def decode_uv(data):
    import numpy as np
    import eccodes
    fields = {}
    with tempfile.NamedTemporaryFile(suffix=".grb2") as tmp:
        tmp.write(data)
        tmp.flush()
        with open(tmp.name, "rb") as f:
            while True:
                g = eccodes.codes_grib_new_from_file(f)
                if g is None:
                    break
                try:
                    name = eccodes.codes_get(g, "shortName")
                    ni = eccodes.codes_get(g, "Ni")
                    nj = eccodes.codes_get(g, "Nj")
                    fields[name] = eccodes.codes_get_values(g).reshape(nj, ni)
                finally:
                    eccodes.codes_release(g)
    if "10u" not in fields or "10v" not in fields:
        return None
    u, v = fields["10u"], fields["10v"]
    ni = u.shape[1]
    # GRIB rows run 90N -> 90S and columns 0E -> 359.5E; shift so column 0 is 180W,
    # matching the equirectangular Earth textures (top row = north).
    return np.roll(u, -ni // 2, axis=1), np.roll(v, -ni // 2, axis=1)


def save_uv(u, v, path):
    import numpy as np
    from PIL import Image
    enc = lambda a: np.clip(np.rint((a + WMAX) / (2 * WMAX) * 255.0), 0, 255).astype("uint8")
    rgb = np.dstack([enc(u), enc(v), np.zeros(u.shape, dtype="uint8")])
    Image.fromarray(rgb, "RGB").save(path, optimize=True)


def fetch_wind(now):
    """Fetch the latest GFS run as a series of frames (every FRAME_STEP h) so the page can
    animate the wind forward in time. Frames are only re-downloaded when a new run appears."""
    import numpy as np

    old = None
    try:
        with open(os.path.join(OUT, "wind.json"), encoding="utf-8") as f:
            old = json.load(f)
    except Exception:
        pass

    cyc0 = now.replace(minute=0, second=0, microsecond=0)
    cyc0 = cyc0.replace(hour=cyc0.hour - cyc0.hour % 6)
    for k in range(5):
        cyc = cyc0 - timedelta(hours=6 * k)
        # the run is usable once its last needed frame is published
        try:
            probe = get(gfs_url(cyc, FRAME_MAX), timeout=120, tries=2)
        except Exception as e:
            log("GFS", cyc, "probe failed:", e)
            continue
        if probe[:4] != b"GRIB":
            log("GFS", cyc, "not complete yet")
            continue
        if old and old.get("cycle") == iso(cyc) and old.get("frames") and all(
            os.path.exists(os.path.join(OUT, fr["file"])) for fr in old["frames"]
        ):
            log("GFS", cyc, "already have this run")
            return old
        os.makedirs(os.path.join(OUT, "wind"), exist_ok=True)
        frames, peak = [], 0.0
        for fh in range(0, FRAME_MAX + 1, FRAME_STEP):
            data = probe if fh == FRAME_MAX else None
            if data is None:
                try:
                    data = get(gfs_url(cyc, fh), timeout=120, tries=3)
                except Exception as e:
                    log("GFS", cyc, fh, "failed:", e)
                    continue
                time.sleep(1.0)  # be gentle with NOMADS
            if data[:4] != b"GRIB":
                continue
            uv = decode_uv(data)
            if uv is None:
                continue
            name = f"wind/f{fh:03d}.png"
            save_uv(uv[0], uv[1], os.path.join(OUT, name))
            peak = max(peak, float(np.hypot(*uv).max()))
            frames.append({"file": name, "fh": fh, "valid": iso(cyc + timedelta(hours=fh))})
        if len(frames) < 3:
            log("GFS", cyc, "too few frames")
            continue
        # remove frames from older runs
        keep = {fr["file"] for fr in frames}
        for fn in os.listdir(os.path.join(OUT, "wind")):
            if f"wind/{fn}" not in keep:
                os.remove(os.path.join(OUT, "wind", fn))
        # wind.png stays as the frame closest to now, for older clients
        best = min(frames, key=lambda fr: abs((datetime.fromisoformat(fr["valid"].replace("Z", "+00:00")) - now).total_seconds()))
        with open(os.path.join(OUT, best["file"]), "rb") as a, open(os.path.join(OUT, "wind.png"), "wb") as b:
            b.write(a.read())
        meta = {
            "source": "NOAA GFS 0.5°, 10 m wind",
            "cycle": iso(cyc),
            "forecastHour": best["fh"],
            "valid": best["valid"],
            "width": 720,
            "height": 361,
            "wmax": WMAX,
            "maxSpeed": round(peak, 1),
            "frames": frames,
            "updated": iso(now),
        }
        write_if_changed("wind.json", meta)
        log("GFS ok", cyc, len(frames), "frames")
        return meta
    raise RuntimeError("no GFS cycle available")


# --------------------------------------------------------------------------- storms
DIR_KO = str.maketrans({"北": "북", "南": "남", "東": "동", "西": "서"})
JMA_CAT = {"TY": "태풍", "STS": "강한 열대폭풍", "TS": "열대폭풍", "TD": "열대저압부", "LOW": "온대저기압"}
JMA_INT = {"強い": "강", "非常に強い": "매우 강", "猛烈な": "초강력"}
JMA_SCALE = {"大型": "대형", "超大型": "초대형"}


def basin_of(lat, lon):
    lon = ((lon + 180) % 360) - 180
    if lat >= 0:
        if lon >= 100:
            return "wp"
        if lon < -140:
            return "cp"
        if 40 <= lon < 100:
            return "ni"
        if -140 <= lon < -80:
            # Gulf of Mexico / Caribbean belong to the Atlantic basin
            return "na" if (lon > -100 and lat > 17) or (lon > -84 and lat > 9) else "ep"
        return "na"
    if 20 <= lon < 90:
        return "si"
    if 90 <= lon < 160:
        return "au"
    return "sp"


BASIN_KO = {"wp": "북서태평양", "cp": "중부태평양", "ep": "북동태평양", "na": "북대서양", "ni": "북인도양",
            "si": "남서인도양", "au": "호주 근해", "sp": "남태평양"}


def num(x):
    try:
        return float(x)
    except Exception:
        return None


def fetch_jma():
    out = []
    targets = get_json("https://www.jma.go.jp/bosai/typhoon/data/targetTc.json")
    for t in targets:
        tc = t.get("tropicalCyclone")
        if not tc:
            continue
        spec = get_json(f"https://www.jma.go.jp/bosai/typhoon/data/{tc}/specifications.json")
        try:
            fc = get_json(f"https://www.jma.go.jp/bosai/typhoon/data/{tc}/forecast.json")
        except Exception:
            fc = []
        title = spec[0]
        parts = [p for p in spec[1:] if isinstance(p, dict) and "position" in p]
        if not parts:
            continue
        parts.sort(key=lambda p: p.get("advancedHours", 0))
        ana = next((p for p in parts if p.get("advancedHours", 0) == 0), parts[0])

        def wind(p):
            w = (p.get("maximumWind") or {}).get("sustained") or {}
            return num(w.get("kt"))

        def gust(p):
            w = (p.get("maximumWind") or {}).get("gust") or {}
            return num(w.get("kt"))

        def maxkm(lst):
            vals = [num((a.get("range") or {}).get("km")) for a in (lst or [])]
            vals = [v for v in vals if v]
            return max(vals) if vals else None

        points = []
        for p in parts:
            lat, lon = p["position"]["deg"]
            points.append({
                "t": p["validtime"]["UTC"],
                "lat": round(lat, 2), "lon": round(((lon + 180) % 360) - 180, 2),
                "kt": wind(p),
                "hpa": num(p.get("pressure")),
                "r": num((p.get("probabilityCircleRadius") or {}).get("km")),
                "cat": (p.get("category") or {}).get("en"),
            })
        past = []
        if len(fc) > 1 and isinstance(fc[1], dict):
            track = fc[1].get("track") or {}
            for key in ("preTyphoon", "typhoon", "afterTyphoon", "extratropical"):
                for ll in track.get(key) or []:
                    if not past or past[-1] != ll:
                        past.append(ll)
        past = [[round(a, 2), round(((b + 180) % 360) - 180, 2)] for a, b in past]
        cat = (ana.get("category") or {}).get("en") or (title.get("category") or {}).get("en")
        number = title.get("typhoonNumber")
        speed = ana.get("speed") or {}
        sp_note = (speed.get("note") or {}).get("jp") if isinstance(speed.get("note"), dict) else None
        name_en = (title.get("name") or {}).get("en")
        out.append({
            "id": "jma-" + tc,
            "agency": "JMA",
            "agencyKo": "일본기상청",
            "windAvg": "10분 평균",
            "name": (name_en or (f"TD {tc[-2:]}" if cat == "TD" else tc)).upper(),
            "number": f"제{int(number[2:])}호" if number and number[2:].isdigit() else None,
            "basin": "wp",
            "basinKo": BASIN_KO["wp"],
            "category": cat,
            "categoryKo": JMA_CAT.get(cat, cat),
            "intensityKo": JMA_INT.get(ana.get("intensity"), None),
            "scaleKo": JMA_SCALE.get(ana.get("scale"), None),
            "time": ana["validtime"]["UTC"],
            "issued": (title.get("issue") or {}).get("UTC"),
            "lat": round(ana["position"]["deg"][0], 2),
            "lon": round(((ana["position"]["deg"][1] + 180) % 360) - 180, 2),
            "kt": wind(ana),
            "gustKt": gust(ana),
            "hpa": num(ana.get("pressure")),
            "courseKo": (ana.get("course") or "").translate(DIR_KO) or None,
            "speedKmh": num(speed.get("km/h")),
            "speedNote": {"ゆっくり": "느림", "ほとんど停滞": "거의 정체", "停滞": "정체"}.get(sp_note, sp_note),
            "galeKm": maxkm(ana.get("galeWarning")),
            "stormKm": maxkm(ana.get("stormWarning")),
            "past": past,
            "fc": points,
            "link": "https://www.jma.go.jp/bosai/map.html#contents=typhoon",
        })
    return out


def gdacs_time(label, ref):
    """'01/11 06:00 UTC' -> datetime, choosing the year closest to ref."""
    d, rest = label.split(" ", 1)
    day, mon = [int(x) for x in d.split("/")]
    hh, mm = [int(x) for x in rest.split()[0].split(":")]
    best = None
    for y in (ref.year - 1, ref.year, ref.year + 1):
        try:
            c = datetime(y, mon, day, hh, mm, tzinfo=timezone.utc)
        except ValueError:
            continue
        if best is None or abs((c - ref).total_seconds()) < abs((best - ref).total_seconds()):
            best = c
    return best


GD_CAT = {"TD": "TD", "TS": "TS", "HU": "HU", "TY": "HU", "STY": "HU", "ST": "HU"}


def fetch_gdacs(now):
    out = []
    d = get_json("https://www.gdacs.org/gdacsapi/api/events/geteventlist/SEARCH?eventlist=TC&alertlevel=green;orange;red")
    for f in d.get("features", []):
        p = f["properties"]
        if str(p.get("iscurrent")).lower() != "true":
            continue
        todate = datetime.fromisoformat(p["todate"]).replace(tzinfo=timezone.utc)
        if now - todate > timedelta(hours=30):
            continue
        try:
            geo = get_json(p["url"]["geometry"])
        except Exception as e:
            log("GDACS geometry failed", p.get("eventname"), e)
            continue
        pts, lines = [], []
        for g in geo.get("features", []):
            q = g["properties"]
            cls = q.get("Class", "")
            if cls.startswith("Point_Polygon_Point_") and g["geometry"]["type"] == "Polygon":
                ring = g["geometry"]["coordinates"][0]
                lon = sum(c[0] for c in ring) / len(ring)
                lat = sum(c[1] for c in ring) / len(ring)
                try:
                    t = gdacs_time(q.get("polygonlabel", ""), todate)
                except Exception:
                    continue
                pts.append({"dt": t, "lat": round(lat, 2), "lon": round(((lon + 180) % 360) - 180, 2)})
            elif cls.startswith("Line_Line_") and g["geometry"]["type"] == "LineString":
                c = g["geometry"]["coordinates"]
                if c:
                    lines.append((c[0], q.get("polygonlabel")))
        if not pts:
            lon, lat = f["geometry"]["coordinates"]
            pts = [{"dt": todate, "lat": lat, "lon": lon}]
        pts.sort(key=lambda x: x["dt"])
        for pt in pts:
            for c0, lab in lines:
                if abs(c0[0] - pt["lon"]) < 0.08 and abs(c0[1] - pt["lat"]) < 0.08:
                    pt["cat"] = GD_CAT.get(lab, lab)
                    break
        cur_i = 0
        for i, pt in enumerate(pts):
            if pt["dt"] <= todate + timedelta(minutes=1):
                cur_i = i
        cur = pts[cur_i]
        kt = num((p.get("severitydata") or {}).get("severity"))
        kt = round(kt / 1.852) if kt else None
        fcpts = []
        for i, pt in enumerate(pts[cur_i:]):
            fcpts.append({"t": iso(pt["dt"]), "lat": pt["lat"], "lon": pt["lon"],
                          "kt": kt if i == 0 else None, "hpa": None, "r": None, "cat": pt.get("cat")})
        name = p.get("eventname", "").rsplit("-", 1)[0].upper()
        b = basin_of(cur["lat"], cur["lon"])
        out.append({
            "id": f"gdacs-{p['eventid']}",
            "agency": p.get("source") or "GDACS",
            "agencyKo": {"JTWC": "미국 합동태풍경보센터(JTWC)", "NOAA": "미국 국립허리케인센터(NHC)"}.get(p.get("source"), p.get("source") or "GDACS"),
            "windAvg": "1분 평균",
            "name": name,
            "number": None,
            "basin": b,
            "basinKo": BASIN_KO[b],
            "category": cur.get("cat"),
            "categoryKo": None,
            "intensityKo": None,
            "scaleKo": None,
            "time": iso(cur["dt"]),
            "issued": iso(datetime.fromisoformat(p["datemodified"]).replace(tzinfo=timezone.utc)) if p.get("datemodified") else None,
            "lat": cur["lat"], "lon": cur["lon"],
            "kt": kt, "gustKt": None, "hpa": None,
            "courseKo": None, "speedKmh": None, "speedNote": None,
            "galeKm": None, "stormKm": None,
            "past": [[x["lat"], x["lon"]] for x in pts[:cur_i + 1]],
            "fc": fcpts,
            "alert": p.get("alertlevel"),
            "link": p["url"].get("report"),
        })
    return out


def gc_deg(a, b):
    la1, lo1, la2, lo2 = map(math.radians, (a[0], a[1], b[0], b[1]))
    c = math.sin(la1) * math.sin(la2) + math.cos(la1) * math.cos(la2) * math.cos(lo1 - lo2)
    return math.degrees(math.acos(max(-1.0, min(1.0, c))))


def fetch_storms(now):
    storms, ok = [], []
    try:
        jma = fetch_jma()
        storms += jma
        ok.append("JMA")
    except Exception as e:
        jma = []
        log("JMA failed:", e)
    try:
        gd = fetch_gdacs(now)
        ok.append("GDACS")
    except Exception as e:
        gd = []
        log("GDACS failed:", e)
    for s in gd:
        # JMA is the primary source for the NW Pacific; drop GDACS duplicates of the same storm
        if any(gc_deg((s["lat"], s["lon"]), (j["lat"], j["lon"])) < 5 for j in jma):
            continue
        storms.append(s)
    if not ok:
        raise RuntimeError("all storm sources failed")
    storms.sort(key=lambda s: -(s.get("kt") or 0))
    return {"updated": iso(now), "sources": ok, "storms": storms}


def main():
    os.makedirs(OUT, exist_ok=True)
    now = datetime.now(timezone.utc)
    failures = 0
    try:
        write_if_changed("storms.json", fetch_storms(now))
    except Exception as e:
        failures += 1
        log("storms failed:", e)
    try:
        fetch_wind(now)
    except Exception as e:
        failures += 1
        log("wind failed:", e)
    if failures == 2:
        sys.exit(1)


if __name__ == "__main__":
    main()
