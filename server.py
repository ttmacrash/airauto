#!/usr/bin/env python3
# -*- coding: utf-8 -*-
import os
import re
import json
import math
import base64
import datetime
import subprocess
import urllib.request
import urllib.parse
from concurrent.futures import ThreadPoolExecutor
from http.server import HTTPServer, SimpleHTTPRequestHandler

PORT = 8765
DEFAULT_API_KEY = "d306zoyjsyarp7ifhu67rjxn52tv0t20"
CALENDAR_HASH = "5243dc1d86e9862e69b2dde8ca0dbe7ae8e19134baa8d9272064cb5e183d93bd"
BOOKIT_HASH = "9e16aafa7ee84e431648ff936cfc5a45e1a7cb68b4bec7e700d0f0371cd98909"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept-Language": "ko-KR,ko;q=0.9,en-US;q=0.8,en;q=0.7",
}


def extract_listing_id(url_or_id: str) -> str:
    s = url_or_id.strip()
    m = re.search(r"/rooms/(\d+)", s)
    if m:
        return m.group(1)
    m2 = re.search(r"(\d{7,22})", s)
    if m2:
        return m2.group(1)
    raise ValueError("유효한 에어비앤비 숙소 링크 또는 숙소 ID를 입력해주세요.")


def to_global_id(listing_id: str) -> str:
    raw = f"DemandStayListing:{listing_id}".encode("utf-8")
    return base64.b64encode(raw).decode("utf-8")


def parse_krw(price_str):
    if not price_str:
        return None
    nums = re.sub(r"[^\d]", "", str(price_str))
    return int(nums) if nums else None


def haversine_m(lat1, lon1, lat2, lon2):
    R = 6371000
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dl / 2) ** 2
    return int(2 * R * math.atan2(math.sqrt(a), math.sqrt(1 - a)))


_META_CACHE = {}

def fetch_listing_metadata(listing_id: str):
    if listing_id in _META_CACHE:
        return dict(_META_CACHE[listing_id])
    url = f"https://www.airbnb.co.kr/rooms/{listing_id}"
    req = urllib.request.Request(url, headers=HEADERS)
    html = ""
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            html = resp.read().decode("utf-8", errors="ignore")
    except Exception:
        pass

    api_key = DEFAULT_API_KEY
    m_key = re.search(r'"api_config":\{"key":"([^"]+)"', html)
    if m_key:
        api_key = m_key.group(1)

    scripts = re.findall(r'<script[^>]*type="application/json"[^>]*>(.*?)</script>', html, re.S)
    meta = {
        "listingId": listing_id,
        "title": f"에어비앤비 숙소 #{listing_id}",
        "personCapacity": 4,
        "overviewItems": [],
        "ratingAverage": None,
        "ratingCount": "0",
        "categoryRatings": {},
        "isGuestFavorite": False,
        "hostName": "호스트",
        "cohosts": [],
        "thumbnailUrl": None,
        "locationTitle": "서울",
        "lat": 37.5942,
        "lng": 126.9117,
        "apiKey": api_key,
    }

    for s in scripts:
        if "niobeClientData" not in s:
            continue
        try:
            data = json.loads(s)
            niobe = data.get("niobeClientData", [])
            if not niobe:
                continue
            node = niobe[0][1].get("data", {}).get("node", {})
            if not node:
                continue
            meta["personCapacity"] = node.get("personCapacity") or 4
            cohosts = node.get("cohosts") or []
            meta["cohosts"] = [c.get("displayFirstName") for c in cohosts if c.get("displayFirstName")]

            loc_coord = (node.get("location") or {}).get("coordinate") or {}
            if loc_coord.get("latitude") and loc_coord.get("longitude"):
                meta["lat"] = float(loc_coord["latitude"])
                meta["lng"] = float(loc_coord["longitude"])

            rstats = node.get("listingRatingStats") or {}
            overall = rstats.get("overallRatingStats") or {}
            meta["ratingAverage"] = overall.get("ratingAverage")
            meta["ratingCount"] = str(overall.get("ratingCount") or "0")
            cat_map = {}
            for c in rstats.get("categoryRatingStats") or []:
                ctype = c.get("categoryTypeA")
                val = (c.get("value") or {}).get("ratingAverage")
                if ctype and val is not None:
                    cat_map[ctype] = val
            meta["categoryRatings"] = cat_map

            gf = node.get("contextualizedGuestFavorites") or {}
            meta["isGuestFavorite"] = bool(gf.get("isGuestFavorite"))

            pdp = node.get("pdpPresentation") or {}
            t_obj = ((pdp.get("title") or {}).get("content") or {}).get("localizedString")
            if t_obj:
                meta["title"] = t_obj

            ov = pdp.get("overview") or {}
            if ov.get("title"):
                meta["locationTitle"] = ov.get("title")
            meta["overviewItems"] = ov.get("items") or []

            hinfo = ((pdp.get("hostInfo") or {}).get("passportData") or {})
            if hinfo.get("name"):
                meta["hostName"] = hinfo.get("name").replace("\u2068", "").replace("\u2069", "")

            seo = pdp.get("seoFeatures") or {}
            if seo.get("relImageSrc"):
                meta["thumbnailUrl"] = seo.get("relImageSrc")
            break
        except Exception:
            pass

    if meta["title"] != f"에어비앤비 숙소 #{listing_id}":
        _META_CACHE[listing_id] = dict(meta)
    return meta


def fetch_ultra_close_neighbors(my_listing_id: str, lat: float, lng: float, target_year: int, target_month: int, api_key: str):
    """Fetch ultra-close neighbors (same building / < 100m) without date restriction so even fully booked ones appear."""
    dlat = 0.0018  # ~180m
    dlng = 0.0022
    params = urllib.parse.urlencode(
        {
            "ne_lat": round(lat + dlat, 5),
            "ne_lng": round(lng + dlng, 5),
            "sw_lat": round(lat - dlat, 5),
            "sw_lng": round(lng - dlng, 5),
            "zoom": 18,
            "search_by_map": "true",
        }
    )
    url = f"https://www.airbnb.co.kr/s/Seoul--South-Korea/homes?{params}"
    req = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=15) as resp:
        html = resp.read().decode("utf-8", errors="ignore")

    candidates = []
    scripts = re.findall(r'<script[^>]*type="application/json"[^>]*>(.*?)</script>', html, re.S)
    for s in scripts:
        if "niobeClientData" not in s:
            continue
        try:
            d = json.loads(s)
            results = (
                d.get("niobeClientData", [])[0][1]
                .get("data", {})
                .get("presentation", {})
                .get("staysSearch", {})
                .get("results", {})
                .get("searchResults", [])
            )
            for r in results:
                dsl = r.get("demandStayListing") or {}
                gid = dsl.get("id") or ""
                if not gid:
                    continue
                try:
                    raw_id = base64.b64decode(gid).decode("utf-8").split(":")[-1]
                except Exception:
                    continue
                if raw_id == str(my_listing_id):
                    continue

                coord = (dsl.get("location") or {}).get("coordinate") or {}
                clat = coord.get("latitude")
                clng = coord.get("longitude")
                dist_m = haversine_m(lat, lng, clat, clng) if (clat and clng) else 50
                if dist_m > 250:
                    continue

                name = (
                    ((dsl.get("description") or {}).get("name") or {}).get("localizedStringWithTranslationPreference")
                    or r.get("subtitle")
                    or r.get("title")
                    or f"숙소 #{raw_id}"
                )
                specs = [
                    m.get("body")
                    for m in (r.get("structuredContent") or {}).get("primaryLine") or []
                    if m.get("body")
                ]
                pics = r.get("contextualPictures") or []
                thumb = pics[0].get("picture") if pics else None

                candidates.append(
                    {
                        "listingId": raw_id,
                        "name": name.replace("\n", " "),
                        "distanceM": dist_m,
                        "rating": r.get("avgRatingLocalized") or "신규",
                        "specs": " · ".join(specs),
                        "thumbnailUrl": thumb,
                        "url": f"https://www.airbnb.co.kr/rooms/{raw_id}",
                    }
                )
            break
        except Exception:
            pass

    candidates.sort(key=lambda x: x["distanceM"])
    top_neighbors = candidates[:15]

    # Enrich each ultra-close neighbor with its target month occupancy & sample 2-person / 4-person pricing
    def enrich_neighbor(nb):
        lid = nb["listingId"]
        try:
            cals = fetch_calendar(lid, target_year, target_month, count=2, api_key=api_key)
            days = cals[0].get("days", []) if cals else []
            next_days = cals[1].get("days", []) if len(cals) > 1 else []
            all_d = days + next_days

            total_d = len(days) or 30
            open_days = [d["calendarDate"] for d in days if d.get("available")]
            booked_d = total_d - len(open_days)
            occ = round((booked_d / total_d) * 100, 1)
            nb["bookedDays"] = booked_d
            nb["openDays"] = len(open_days)
            nb["totalDays"] = total_d
            nb["occupancyRate"] = occ

            # Next month occupancy
            if next_days:
                n_total = len(next_days)
                n_booked = sum(1 for d in next_days if not d.get("available"))
                nb["nextMonthOccupancyRate"] = round((n_booked / n_total) * 100, 1)
                nb["nextMonthBookedDays"] = n_booked
            else:
                nb["nextMonthOccupancyRate"] = 0
                nb["nextMonthBookedDays"] = 0

            # Min nights rule (weekday vs weekend)
            wd_mins = [
                d.get("minNights") or 1
                for d in all_d
                if datetime.date.fromisoformat(d["calendarDate"]).weekday() in (0, 1, 2, 3, 6)
            ]
            we_mins = [
                d.get("minNights") or 1
                for d in all_d
                if datetime.date.fromisoformat(d["calendarDate"]).weekday() in (4, 5)
            ]
            nb["weekdayMinNights"] = f"{min(wd_mins)}~{max(wd_mins)}박" if wd_mins and min(wd_mins) != max(wd_mins) else f"{min(wd_mins) if wd_mins else 1}박"
            nb["weekendMinNights"] = f"{min(we_mins)}~{max(we_mins)}박" if we_mins and min(we_mins) != max(we_mins) else f"{min(we_mins) if we_mins else 2}박"

            # Find an open weekday range and weekend range across target + next month
            sample_ci, sample_co, sample_nights = None, None, 2
            we_ci, we_co, we_nights = None, None, 2
            for idx in range(len(all_d) - 4):
                d_cur = all_d[idx]
                if not d_cur.get("available"):
                    continue
                req_min = max(2, d_cur.get("minNights") or 1)
                if all(all_d[idx + k].get("available") for k in range(req_min)):
                    dt_cur = datetime.date.fromisoformat(d_cur["calendarDate"])
                    co_str = (dt_cur + datetime.timedelta(days=req_min)).isoformat()
                    if dt_cur.weekday() in (0, 1, 2, 3) and not sample_ci:
                        sample_ci, sample_co, sample_nights = d_cur["calendarDate"], co_str, req_min
                    if dt_cur.weekday() in (4, 5) and not we_ci:
                        we_ci, we_co, we_nights = d_cur["calendarDate"], co_str, req_min
                    if sample_ci and we_ci:
                        break

            if sample_ci and sample_co:
                p2 = fetch_bookit_price(lid, sample_ci, sample_co, 2, api_key)
                p4 = fetch_bookit_price(lid, sample_ci, sample_co, 4, api_key)
                nb["price2p"] = p2.get("perNightPrice")
                nb["origPrice2p"] = p2.get("originalPerNightPrice")
                nb["price4p"] = p4.get("perNightPrice")
                nb["sampleDate"] = sample_ci[5:]
                nb["canInstantBook"] = p2.get("canInstantBook")
                nb["hasDiscountBadge"] = p2.get("hasDiscountBadge")
                nb["extraGuestFeePerPerson"] = (
                    round((nb["price4p"] - nb["price2p"]) / 2)
                    if (nb["price2p"] and nb["price4p"] and nb["price4p"] > nb["price2p"])
                    else 0
                )
            else:
                nb["price2p"] = None
                nb["origPrice2p"] = None
                nb["price4p"] = None
                nb["sampleDate"] = None
                nb["canInstantBook"] = False
                nb["hasDiscountBadge"] = False
                nb["extraGuestFeePerPerson"] = 0

            if we_ci and we_co:
                p_we = fetch_bookit_price(lid, we_ci, we_co, 2, api_key)
                nb["weekendPrice2p"] = p_we.get("perNightPrice")
            else:
                nb["weekendPrice2p"] = None
        except Exception:
            nb["bookedDays"] = 0
            nb["openDays"] = 0
            nb["totalDays"] = 30
            nb["occupancyRate"] = 0
        return nb

    with ThreadPoolExecutor(max_workers=10) as ex:
        enriched = list(ex.map(enrich_neighbor, top_neighbors))

    return enriched


def fetch_nearby_competitors(my_listing_id: str, lat: float, lng: float, check_in: str, check_out: str, adults: int = 2):
    dlat = 0.0095  # ~1km
    dlng = 0.0115  # ~1km
    params = urllib.parse.urlencode(
        {
            "checkin": check_in,
            "checkout": check_out,
            "adults": adults,
            "ne_lat": round(lat + dlat, 5),
            "ne_lng": round(lng + dlng, 5),
            "sw_lat": round(lat - dlat, 5),
            "sw_lng": round(lng - dlng, 5),
            "zoom": 15,
            "search_by_map": "true",
        }
    )
    url = f"https://www.airbnb.co.kr/s/Seoul--South-Korea/homes?{params}"
    req = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=15) as resp:
        html = resp.read().decode("utf-8", errors="ignore")

    d1 = datetime.date.fromisoformat(check_in)
    d2 = datetime.date.fromisoformat(check_out)
    nights = max(1, (d2 - d1).days)

    competitors = []
    scripts = re.findall(r'<script[^>]*type="application/json"[^>]*>(.*?)</script>', html, re.S)
    for s in scripts:
        if "niobeClientData" not in s:
            continue
        try:
            d = json.loads(s)
            results = (
                d.get("niobeClientData", [])[0][1]
                .get("data", {})
                .get("presentation", {})
                .get("staysSearch", {})
                .get("results", {})
                .get("searchResults", [])
            )
            for r in results:
                dsl = r.get("demandStayListing") or {}
                gid = dsl.get("id") or ""
                if not gid:
                    continue
                try:
                    raw_id = base64.b64decode(gid).decode("utf-8").split(":")[-1]
                except Exception:
                    continue
                if raw_id == str(my_listing_id):
                    continue

                coord = (dsl.get("location") or {}).get("coordinate") or {}
                clat = coord.get("latitude")
                clng = coord.get("longitude")
                dist_m = haversine_m(lat, lng, clat, clng) if (clat and clng) else 500

                name = (
                    ((dsl.get("description") or {}).get("name") or {}).get("localizedStringWithTranslationPreference")
                    or r.get("subtitle")
                    or r.get("title")
                    or f"숙소 #{raw_id}"
                )
                pline = (r.get("structuredDisplayPrice") or {}).get("primaryLine") or {}
                price_str = pline.get("discountedPrice") or pline.get("price")
                orig_str = pline.get("originalPrice")
                total_krw = parse_krw(price_str)
                orig_krw = parse_krw(orig_str)
                if not total_krw:
                    continue

                per_night = round(total_krw / nights)
                orig_per_night = round(orig_krw / nights) if orig_krw else None

                specs = [
                    m.get("body")
                    for m in (r.get("structuredContent") or {}).get("primaryLine") or []
                    if m.get("body")
                ]
                pics = r.get("contextualPictures") or []
                thumb = pics[0].get("picture") if pics else None
                badges = [b.get("text") for b in (r.get("badges") or []) if b.get("text")]

                competitors.append(
                    {
                        "listingId": raw_id,
                        "name": name.replace("\n", " "),
                        "distanceM": dist_m,
                        "rating": r.get("avgRatingLocalized") or "신규",
                        "specs": " · ".join(specs),
                        "bedroomText": specs[0] if specs else "집 전체",
                        "perNightPrice": per_night,
                        "originalPerNightPrice": orig_per_night,
                        "hasDiscount": orig_per_night is not None and orig_per_night > per_night,
                        "thumbnailUrl": thumb,
                        "badges": badges,
                        "url": f"https://www.airbnb.co.kr/rooms/{raw_id}?check_in={check_in}&check_out={check_out}&adults={adults}",
                    }
                )
            break
        except Exception:
            pass

    competitors.sort(key=lambda x: x["perNightPrice"])
    return competitors[:10]


def fetch_calendar(listing_id: str, year: int, month: int, count: int = 2, api_key: str = DEFAULT_API_KEY):
    gid = to_global_id(listing_id)
    vars_dict = {
        "listingId": gid,
        "input": {
            "month": month,
            "year": year,
            "count": count,
            "returnPropertyLevelCalendarIfApplicable": False,
        },
    }
    ext_dict = {"persistedQuery": {"version": 1, "sha256Hash": CALENDAR_HASH}}
    params = urllib.parse.urlencode(
        {
            "operationName": "StaysPdpAtomicAvailabilityCalendarV2Query",
            "locale": "ko",
            "currency": "KRW",
            "variables": json.dumps(vars_dict, separators=(",", ":")),
            "extensions": json.dumps(ext_dict, separators=(",", ":")),
        }
    )
    url = f"https://www.airbnb.co.kr/api/v3/StaysPdpAtomicAvailabilityCalendarV2Query/{CALENDAR_HASH}?{params}"
    req = urllib.request.Request(url, headers={**HEADERS, "X-Airbnb-API-Key": api_key})
    with urllib.request.urlopen(req, timeout=15) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    return (
        data.get("data", {})
        .get("node", {})
        .get("pdpAvailabilityCalendar", {})
        .get("calendarMonths", [])
    )


def fetch_bookit_price(listing_id: str, check_in: str, check_out: str, adults: int = 1, api_key: str = DEFAULT_API_KEY):
    gid = to_global_id(listing_id)
    vars_dict = {
        "id": gid,
        "dateRange": {"startDate": check_in, "endDate": check_out},
        "guestCounts": {
            "numberOfAdults": adults,
            "numberOfChildren": 0,
            "numberOfInfants": 0,
            "numberOfPets": 0,
        },
        "includePdpMigrationBookItCalendarSheetFragment": True,
        "includePdpMigrationBookItFloatingFooterFragment": True,
        "includePdpMigrationBookItNavFragment": True,
        "includePdpMigrationBookItSidebarFragment": True,
        "includeGalleryBookItCtaFragment": True,
        "includeOverviewMerchandisingTipsFragment": True,
        "includeStaysPdpPriceHeatmapFragment": True,
        "priceHeatmapDateRange": {"startDate": check_in, "endDate": check_out},
        "p3ImpressionId": "p3_1793206372_P3bhrsj3Ky4Vljom",
        "selectedCancellationPolicyId": None,
        "causeId": None,
        "selectedGuestOptionId": None,
    }
    ext_dict = {"persistedQuery": {"version": 1, "sha256Hash": BOOKIT_HASH}}
    params = urllib.parse.urlencode(
        {
            "operationName": "StaysPdpBookItQuery",
            "locale": "ko",
            "currency": "KRW",
            "variables": json.dumps(vars_dict, separators=(",", ":")),
            "extensions": json.dumps(ext_dict, separators=(",", ":")),
        }
    )
    url = f"https://www.airbnb.co.kr/api/v3/StaysPdpBookItQuery/{BOOKIT_HASH}?{params}"
    req = urllib.request.Request(url, headers={**HEADERS, "X-Airbnb-API-Key": api_key})
    with urllib.request.urlopen(req, timeout=15) as resp:
        data = json.loads(resp.read().decode("utf-8"))

    bookit = (
        data.get("data", {})
        .get("node", {})
        .get("pdpPresentation", {})
        .get("bookIt")
        or {}
    )
    avail = bookit.get("availability") or {}
    sp = bookit.get("structuredDisplayPrice") or {}
    pline = sp.get("primaryLine") or {}
    total_str = pline.get("discountedPrice") or pline.get("price")
    orig_str = pline.get("originalPrice")

    expl = sp.get("explanationData") or {}
    line_desc = None
    for grp in expl.get("priceDetails") or []:
        for it in grp.get("items") or []:
            if it.get("description") and "박" in it.get("description", ""):
                line_desc = it.get("description")
                if not total_str:
                    total_str = it.get("priceString")
                if not orig_str:
                    orig_str = it.get("originalPriceString")
                break

    total_krw = parse_krw(total_str)
    orig_krw = parse_krw(orig_str)
    d1 = datetime.date.fromisoformat(check_in)
    d2 = datetime.date.fromisoformat(check_out)
    nights = max(1, (d2 - d1).days)
    per_night = round(total_krw / nights) if total_krw else None
    orig_per_night = round(orig_krw / nights) if orig_krw else None

    return {
        "checkIn": check_in,
        "checkOut": check_out,
        "nights": nights,
        "adults": adults,
        "isAvailable": bool(avail.get("isAvailable")),
        "canInstantBook": bool(avail.get("canInstantBook")),
        "totalPrice": total_krw,
        "totalPriceStr": total_str,
        "originalTotalPrice": orig_krw,
        "originalTotalPriceStr": orig_str,
        "perNightPrice": per_night,
        "originalPerNightPrice": orig_per_night,
        "hasDiscountBadge": orig_krw is not None and total_krw is not None and orig_krw > total_krw,
        "lineDescription": line_desc,
    }


def compare_competitor_detail(my_id: str, comp_id: str, target_month: int = 11, target_year: int = 2026):
    my_id = extract_listing_id(my_id)
    comp_id = extract_listing_id(comp_id)

    with ThreadPoolExecutor(max_workers=6) as ex:
        f_my_meta = ex.submit(fetch_listing_metadata, my_id)
        f_comp_meta = ex.submit(fetch_listing_metadata, comp_id)
        f_my_cal = ex.submit(fetch_calendar, my_id, target_year, target_month, 2)
        f_comp_cal = ex.submit(fetch_calendar, comp_id, target_year, target_month, 2)

        my_meta = f_my_meta.result()
        comp_meta = f_comp_meta.result()
        my_cals = f_my_cal.result()
        comp_cals = f_comp_cal.result()

    my_days = my_cals[0].get("days", []) if my_cals else []
    my_next = my_cals[1].get("days", []) if len(my_cals) > 1 else []
    comp_days = comp_cals[0].get("days", []) if comp_cals else []
    comp_next = comp_cals[1].get("days", []) if len(comp_cals) > 1 else []

    my_all = {d["calendarDate"]: d for d in (my_days + my_next)}
    comp_all = {d["calendarDate"]: d for d in (comp_days + comp_next)}

    # Build contiguous open windows for comp_id and my_id to query exact prices
    def extract_windows(day_list, all_map):
        wins = []
        idx = 0
        while idx < len(day_list):
            if day_list[idx].get("available"):
                s_idx = idx
                j = idx
                while j < len(day_list) and day_list[j].get("available"):
                    j += 1
                s_date = day_list[s_idx]["calendarDate"]
                last_dt = datetime.date.fromisoformat(day_list[j - 1]["calendarDate"])
                # check if next month days continue
                while (last_dt + datetime.timedelta(days=1)).isoformat() in all_map and all_map[(last_dt + datetime.timedelta(days=1)).isoformat()].get("available"):
                    last_dt = last_dt + datetime.timedelta(days=1)
                e_date = (last_dt + datetime.timedelta(days=1)).isoformat()
                n_cnt = (datetime.date.fromisoformat(e_date) - datetime.date.fromisoformat(s_date)).days
                min_n = max(d.get("minNights") or 1 for d in day_list[s_idx:j])
                wins.append({
                    "startDate": s_date,
                    "endDate": e_date,
                    "nights": n_cnt,
                    "minNights": min_n,
                    "dates": [d["calendarDate"] for d in day_list[s_idx:j]],
                })
                idx = j
            else:
                idx += 1
        return wins

    my_wins = extract_windows(my_days, my_all)
    comp_wins = extract_windows(comp_days, comp_all)
    # If comp has 0 open windows in target month, also include next month open windows so user can still see what price they charge!
    if len(comp_wins) < 3 and comp_next:
        comp_wins.extend(extract_windows(comp_next, comp_all)[:4])

    # Query prices for all my_wins and comp_wins in parallel (2 guests and 4 guests)
    my_day_price = {}
    comp_day_price = {}

    with ThreadPoolExecutor(max_workers=12) as ex:
        futs = []
        for w in my_wins:
            if w["nights"] >= w["minNights"]:
                futs.append(("my_2p", w, ex.submit(fetch_bookit_price, my_id, w["startDate"], w["endDate"], 2)))
                futs.append(("my_4p", w, ex.submit(fetch_bookit_price, my_id, w["startDate"], w["endDate"], 4)))
        for w in comp_wins:
            if w["nights"] >= w["minNights"]:
                futs.append(("comp_2p", w, ex.submit(fetch_bookit_price, comp_id, w["startDate"], w["endDate"], 2)))
                futs.append(("comp_4p", w, ex.submit(fetch_bookit_price, comp_id, w["startDate"], w["endDate"], 4)))

        for kind, w, fut in futs:
            try:
                res = fut.result()
            except Exception:
                res = None
            if not res or not res.get("perNightPrice"):
                continue
            target_map = my_day_price if kind.startswith("my") else comp_day_price
            field = "price2p" if kind.endswith("2p") else "price4p"
            for d_str in w["dates"]:
                if d_str not in target_map:
                    target_map[d_str] = {}
                target_map[d_str][field] = res["perNightPrice"]
                if kind.endswith("2p"):
                    target_map[d_str]["origPrice2p"] = res.get("originalPerNightPrice")
                    target_map[d_str]["hasDiscountBadge"] = res.get("hasDiscountBadge")
                    target_map[d_str]["canInstantBook"] = res.get("canInstantBook")
            w[field] = res["perNightPrice"]
            if kind.endswith("2p"):
                w["origPrice2p"] = res.get("originalPerNightPrice")
                w["hasDiscountBadge"] = res.get("hasDiscountBadge")
                w["canInstantBook"] = res.get("canInstantBook")

    my_avg_2p = round(sum(v["price2p"] for v in my_day_price.values() if v.get("price2p")) / max(1, len([v for v in my_day_price.values() if v.get("price2p")]))) if my_day_price else 135000
    comp_avg_2p = round(sum(w["price2p"] for w in comp_wins if w.get("price2p")) / max(1, len([w for w in comp_wins if w.get("price2p")]))) if any(w.get("price2p") for w in comp_wins) else None
    comp_avg_4p = round(sum(w["price4p"] for w in comp_wins if w.get("price4p")) / max(1, len([w for w in comp_wins if w.get("price4p")]))) if any(w.get("price4p") for w in comp_wins) else None

    # Build day-by-day comparison table for the target month
    daily_comparison = []
    wk_names = ["월", "화", "수", "목", "금", "토", "일"]
    for d in comp_days:
        cdate = d["calendarDate"]
        dt = datetime.date.fromisoformat(cdate)
        my_d = my_all.get(cdate, {})
        my_p = my_day_price.get(cdate, {})
        cp_p = comp_day_price.get(cdate, {})

        my_avail = bool(my_d.get("available"))
        comp_avail = bool(d.get("available"))

        my_p2 = my_p.get("price2p") or (my_avg_2p if my_avail else None)
        cp_p2 = cp_p.get("price2p") or (comp_avg_2p if comp_avail else None)

        daily_comparison.append({
            "date": cdate,
            "dayNum": dt.day,
            "weekday": wk_names[dt.weekday()],
            "isWeekend": dt.weekday() in (4, 5),
            "myAvailable": my_avail,
            "myMinNights": my_d.get("minNights") or 1,
            "myPrice2p": my_p2,
            "myPrice4p": my_p.get("price4p") or my_p2,
            "compAvailable": comp_avail,
            "compMinNights": d.get("minNights") or 1,
            "compPrice2p": cp_p2,
            "compOrigPrice2p": cp_p.get("origPrice2p"),
            "compPrice4p": cp_p.get("price4p"),
            "priceDiff2p": (cp_p2 - my_p2) if (cp_p2 and my_p2) else None,
        })

    my_booked = sum(1 for d in my_days if not d.get("available"))
    comp_booked = sum(1 for d in comp_days if not d.get("available"))

    return {
        "targetYear": target_year,
        "targetMonth": target_month,
        "myMeta": my_meta,
        "compMeta": comp_meta,
        "summary": {
            "myBookedDays": my_booked,
            "myOpenDays": len(my_days) - my_booked,
            "myOccupancyRate": round(my_booked / max(1, len(my_days)) * 100, 1),
            "myAvgPrice2p": my_avg_2p,
            "compBookedDays": comp_booked,
            "compOpenDays": len(comp_days) - comp_booked,
            "compOccupancyRate": round(comp_booked / max(1, len(comp_days)) * 100, 1),
            "compAvgPrice2p": comp_avg_2p,
            "compAvgPrice4p": comp_avg_4p,
            "extraGuestFee": round((comp_avg_4p - comp_avg_2p) / 2) if (comp_avg_2p and comp_avg_4p and comp_avg_4p > comp_avg_2p) else 0,
        },
        "compOpenWindows": comp_wins,
        "dailyComparison": daily_comparison,
    }


def analyze_listing(url_or_id: str, target_month: int = 11, target_year: int = 2026):
    listing_id = extract_listing_id(url_or_id)
    meta = fetch_listing_metadata(listing_id)
    api_key = meta["apiKey"]

    cal_months = fetch_calendar(listing_id, target_year, target_month, count=2, api_key=api_key)
    if not cal_months:
        raise ValueError("달력 데이터를 불러올 수 없습니다.")

    target_cal = None
    next_cal = None
    for cm in cal_months:
        if cm.get("year") == target_year and cm.get("month") == target_month:
            target_cal = cm
        elif target_cal is not None and next_cal is None:
            next_cal = cm

    if not target_cal:
        target_cal = cal_months[0]

    days = target_cal.get("days") or []
    all_days = list(days)
    if next_cal and next_cal.get("days"):
        all_days.extend(next_cal.get("days")[:7])

    total_days = len(days)
    open_days = [d for d in days if d.get("available")]
    booked_days = total_days - len(open_days)
    occupancy_rate = round((booked_days / total_days) * 100, 1) if total_days else 0

    windows = []
    i = 0
    while i < len(days):
        d = days[i]
        if d.get("available"):
            start_idx = i
            j = i
            while j < len(all_days) and all_days[j].get("available"):
                j += 1
            start_date = all_days[start_idx]["calendarDate"]
            if j < len(all_days):
                end_date = all_days[j]["calendarDate"]
            else:
                last_dt = datetime.date.fromisoformat(all_days[j - 1]["calendarDate"])
                end_date = (last_dt + datetime.timedelta(days=1)).isoformat()

            win_days = all_days[start_idx:j]
            target_month_nights = sum(1 for wd in win_days if wd["calendarDate"].startswith(f"{target_year}-{target_month:02d}"))
            min_nights_max = max(wd.get("minNights") or 1 for wd in win_days)
            has_weekend = any(
                datetime.date.fromisoformat(wd["calendarDate"]).weekday() in (4, 5)
                for wd in win_days
            )
            windows.append(
                {
                    "startDate": start_date,
                    "endDate": end_date,
                    "nights": len(win_days),
                    "targetMonthNights": target_month_nights,
                    "minNights": min_nights_max,
                    "hasWeekend": has_weekend,
                    "dates": [wd["calendarDate"] for wd in win_days],
                }
            )
            i = j
        else:
            i += 1

    comp_ci = windows[0]["startDate"] if windows else f"{target_year}-{target_month:02d}-10"
    comp_co = windows[0]["endDate"] if windows else f"{target_year}-{target_month:02d}-12"

    max_cap = min(int(meta.get("personCapacity") or 4), 6)
    competitors = []
    ultra_close = []
    with ThreadPoolExecutor(max_workers=12) as ex:
        futures = {}
        for idx, w in enumerate(windows):
            futures[("win_1p", idx)] = ex.submit(
                fetch_bookit_price, listing_id, w["startDate"], w["endDate"], 1, api_key
            )
        if windows and max_cap > 1:
            w0 = windows[0]
            futures[("win_maxp", 0)] = ex.submit(
                fetch_bookit_price, listing_id, w0["startDate"], w0["endDate"], max_cap, api_key
            )
        futures[("competitors", 0)] = ex.submit(
            fetch_nearby_competitors, listing_id, meta["lat"], meta["lng"], comp_ci, comp_co, 2
        )
        futures[("ultra_close", 0)] = ex.submit(
            fetch_ultra_close_neighbors, listing_id, meta["lat"], meta["lng"], target_year, target_month, api_key
        )

        for k, fut in futures.items():
            try:
                res = fut.result()
            except Exception:
                res = None
            if k[0] == "win_1p":
                windows[k[1]]["pricing"] = res
            elif k[0] == "win_maxp":
                meta["maxGuestSamplePricing"] = res
            elif k[0] == "competitors":
                competitors = res or []
            elif k[0] == "ultra_close":
                ultra_close = res or []

    same_guest_price = False
    can_instant_book = False
    if windows and windows[0].get("pricing"):
        p1 = windows[0]["pricing"].get("totalPrice")
        can_instant_book = bool(windows[0]["pricing"].get("canInstantBook"))
        pmax = (meta.get("maxGuestSamplePricing") or {}).get("totalPrice")
        if p1 and pmax and p1 == pmax:
            same_guest_price = True

    valid_prices = [
        w["pricing"]["perNightPrice"]
        for w in windows
        if w.get("pricing") and w["pricing"].get("perNightPrice")
    ]
    avg_open_price = round(sum(valid_prices) / len(valid_prices)) if valid_prices else 145000
    est_lost_revenue = sum(
        (w["pricing"]["perNightPrice"] if (w.get("pricing") and w["pricing"].get("perNightPrice")) else avg_open_price)
        * w["targetMonthNights"]
        for w in windows
    )

    comp_prices = [c["perNightPrice"] for c in competitors if c.get("perNightPrice")]
    comp_avg_price = round(sum(comp_prices) / len(comp_prices)) if comp_prices else round(avg_open_price * 0.8)
    cheaper_comp_count = sum(1 for c in competitors if c["perNightPrice"] < avg_open_price)

    diagnostics = []

    if same_guest_price and meta["personCapacity"] >= 3:
        diagnostics.append(
            {
                "id": "guest_price_flat",
                "severity": "CRITICAL",
                "badge": "원인 1 · 평일 가격 저항",
                "title": f"1명 요금과 {meta['personCapacity']}명 요금이 똑같아요",
                "description": (
                    f"평일 빈 날짜를 찾는 손님은 대부분 1~2명(커플·출장)인데, 현재 5인 가족 기준 요금({avg_open_price:,}원)을 그대로 받고 있어서 바로 옆집이나 주변 숙소(평균 {comp_avg_price:,}원)에 손님을 뺏기고 있습니다."
                ),
                "autoAction": "평일 2인 기준 요금 자동 인하 + 3인부터 인원 추가비 분리",
            }
        )

    no_badge_wins = [
        w for w in windows if w.get("pricing") and not w["pricing"].get("hasDiscountBadge")
    ]
    if no_badge_wins:
        date_labels = ", ".join(f"{w['startDate'][5:]}~{w['endDate'][5:]}" for w in no_badge_wins[:3])
        diagnostics.append(
            {
                "id": "missing_promo_badge",
                "severity": "HIGH",
                "badge": "원인 2 · 노출 순위 하락",
                "title": f"당장 코앞인 빈 날({date_labels})에 '할인 취소선'이 없어요",
                "description": (
                    "체크인이 코앞인 날짜에 할인 배지(취소선)가 없으면 에어비앤비 알고리즘이 검색 결과 뒤쪽으로 밀어버립니다."
                ),
                "autoAction": "체크인 14일·7일 전 빈 날짜에 '맞춤 프로모션(취소선 배지)' 자동 발동",
            }
        )

    locked_wins = [w for w in windows if w["minNights"] >= 2]
    if locked_wins:
        date_labels = ", ".join(f"{w['startDate'][5:]}~{w['endDate'][5:]}" for w in locked_wins[:3])
        diagnostics.append(
            {
                "id": "min_nights_lock",
                "severity": "HIGH",
                "badge": "원인 3 · 검색 차단됨",
                "title": f"빈 날({date_labels})이 '최소 {locked_wins[0]['minNights']}박'으로 잠겨 있어요",
                "description": (
                    f"1박만 자고 가려는 손님이 검색할 때 내 숙소가 아예 화면에 안 뜹니다. 징검다리 빈 날은 최소 숙박일을 1박으로 풀어줘야 합니다."
                ),
                "autoAction": "예약 사이에 낀 빈 날은 최소 숙박일(2박→1박) 자동 해제",
            }
        )

    if not can_instant_book:
        diagnostics.append(
            {
                "id": "instant_book_off",
                "severity": "MEDIUM",
                "badge": "원인 4 · 결제 이탈",
                "title": "'즉시 예약'이 꺼져 있어서 급한 손님이 떠나요",
                "description": (
                    "당장 이번 달 숙소를 찾는 손님은 호스트 승인을 기다리지 않고 바로 결제되는 숙소를 예약합니다."
                ),
                "autoAction": "당월 잔여 공실에는 '즉시 예약' 자동 켜기",
            }
        )

    ui_days = []
    for d in days:
        dt = datetime.date.fromisoformat(d["calendarDate"])
        ui_days.append(
            {
                "date": d["calendarDate"],
                "dayNum": dt.day,
                "weekday": dt.weekday(),
                "available": bool(d.get("available")),
                "minNights": d.get("minNights") or 1,
            }
        )

    return {
        "meta": meta,
        "targetYear": target_year,
        "targetMonth": target_month,
        "summary": {
            "totalDays": total_days,
            "bookedDays": booked_days,
            "openDays": len(open_days),
            "occupancyRate": occupancy_rate,
            "avgOpenPerNightPrice": avg_open_price,
            "competitorAvgPrice": comp_avg_price,
            "cheaperCompetitorCount": cheaper_comp_count,
            "totalCompetitorCount": len(competitors),
            "competitorCheckIn": comp_ci,
            "competitorCheckOut": comp_co,
            "estimatedLostRevenue": est_lost_revenue,
            "canInstantBook": can_instant_book,
            "sameGuestPrice": same_guest_price,
        },
        "windows": windows,
        "ultraCloseNeighbors": ultra_close,
        "competitors": competitors,
        "diagnostics": diagnostics,
        "calendarDays": ui_days,
    }


class AirAutoHandler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=os.path.dirname(os.path.abspath(__file__)), **kwargs)

    def end_headers(self):
        self.send_header("Cache-Control", "no-store, no-cache, must-revalidate, max-age=0")
        self.send_header("Pragma", "no-cache")
        self.send_header("Expires", "0")
        super().end_headers()

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path == "/api/analyze":
            qs = urllib.parse.parse_qs(parsed.query)
            url_param = (qs.get("url") or ["1714436404764677191"])[0]
            month_param = int((qs.get("month") or ["11"])[0])
            year_param = int((qs.get("year") or ["2026"])[0])
            try:
                result = analyze_listing(url_param, target_month=month_param, target_year=year_param)
                body = json.dumps({"ok": True, "data": result}, ensure_ascii=False).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            except Exception as e:
                snap_file = os.path.join(
                    os.path.dirname(os.path.abspath(__file__)),
                    "data",
                    f"analyze_{year_param}_{month_param}.json",
                )
                if os.path.exists(snap_file):
                    with open(snap_file, "rb") as sf:
                        body = sf.read()
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json; charset=utf-8")
                    self.send_header("Access-Control-Allow-Origin", "*")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                else:
                    body = json.dumps({"ok": False, "error": str(e)}, ensure_ascii=False).encode("utf-8")
                    self.send_response(400)
                    self.send_header("Content-Type", "application/json; charset=utf-8")
                    self.send_header("Access-Control-Allow-Origin", "*")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
            return

        if parsed.path == "/api/competitor-detail":
            qs = urllib.parse.parse_qs(parsed.query)
            my_id = (qs.get("my_id") or ["1714436404764677191"])[0]
            comp_id = (qs.get("comp_id") or [""])[0]
            month_param = int((qs.get("month") or ["11"])[0])
            year_param = int((qs.get("year") or ["2026"])[0])
            try:
                result = compare_competitor_detail(my_id, comp_id, target_month=month_param, target_year=year_param)
                body = json.dumps({"ok": True, "data": result}, ensure_ascii=False).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            except Exception as e:
                snap_file = os.path.join(
                    os.path.dirname(os.path.abspath(__file__)),
                    "data",
                    f"comp_{comp_id}_{year_param}_{month_param}.json",
                )
                if os.path.exists(snap_file):
                    with open(snap_file, "rb") as sf:
                        body = sf.read()
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json; charset=utf-8")
                    self.send_header("Access-Control-Allow-Origin", "*")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                else:
                    body = json.dumps({"ok": False, "error": str(e)}, ensure_ascii=False).encode("utf-8")
                    self.send_response(400)
                    self.send_header("Content-Type", "application/json; charset=utf-8")
                    self.send_header("Access-Control-Allow-Origin", "*")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
            return

        if parsed.path == "/api/bot-status":
            status_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "bot_status.json")
            if os.path.exists(status_path):
                with open(status_path, "r", encoding="utf-8") as f:
                    st = json.load(f)
            else:
                st = {"state": "IDLE", "message": "대기 중"}
            body = json.dumps({"ok": True, "status": st}, ensure_ascii=False).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        return super().do_GET()

    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path == "/api/run-bot":
            length = int(self.headers.get("Content-Length", 0))
            raw_body = self.rfile.read(length).decode("utf-8") if length else "{}"
            try:
                payload = json.loads(raw_body)
                base_dir = os.path.dirname(os.path.abspath(__file__))
                bot_script = os.path.join(base_dir, "bot_runner.js")
                subprocess.Popen(
                    ["/opt/homebrew/bin/node", bot_script, json.dumps(payload, ensure_ascii=False)],
                    cwd=base_dir,
                )
                resp_body = json.dumps(
                    {"ok": True, "message": "자동 세팅 봇이 실행되었습니다."}, ensure_ascii=False
                ).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.send_header("Content-Length", str(len(resp_body)))
                self.end_headers()
                self.wfile.write(resp_body)
            except Exception as e:
                resp_body = json.dumps({"ok": False, "error": str(e)}, ensure_ascii=False).encode("utf-8")
                self.send_response(400)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.send_header("Content-Length", str(len(resp_body)))
                self.end_headers()
                self.wfile.write(resp_body)
            return


if __name__ == "__main__":
    print(f"Starting AirAuto server on http://localhost:{PORT}")
    server = HTTPServer(("127.0.0.1", PORT), AirAutoHandler)
    server.serve_forever()
