#!/usr/bin/env python3
# -*- coding: utf-8 -*-
import os
import json
import datetime
from concurrent.futures import ThreadPoolExecutor
import server

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "data")
os.makedirs(DATA_DIR, exist_ok=True)

MY_ID = "1714436404764677191"
KST = datetime.timezone(datetime.timedelta(hours=9))


def now_iso():
    return datetime.datetime.now(KST).isoformat(timespec="seconds")


def dump(path, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"ok": True, "generatedAt": now_iso(), "data": data}, f, ensure_ascii=False)


print("1) Generating main analysis data for Nov 2026 & Dec 2026...")
nov_data = server.analyze_listing(MY_ID, target_month=11, target_year=2026)
dump(os.path.join(DATA_DIR, "analyze_2026_11.json"), nov_data)

dec_data = server.analyze_listing(MY_ID, target_month=12, target_year=2026)
dump(os.path.join(DATA_DIR, "analyze_2026_12.json"), dec_data)

comp_ids = set()
for nb in nov_data.get("ultraCloseNeighbors", []):
    comp_ids.add(nb["listingId"])
for c in nov_data.get("competitors", []):
    comp_ids.add(c["listingId"])

print(f"2) Generating 1:1 detail modal snapshots for {len(comp_ids)} competitors...")


def save_comp(cid):
    try:
        detail = server.compare_competitor_detail(MY_ID, cid, target_month=11, target_year=2026)
        dump(os.path.join(DATA_DIR, f"comp_{cid}_2026_11.json"), detail)
        print(f"  Saved comp_{cid}_2026_11.json")
    except Exception as e:
        print(f"  Failed {cid}: {e}")


with ThreadPoolExecutor(max_workers=6) as ex:
    list(ex.map(save_comp, comp_ids))

print("Done generating static snapshots!")
