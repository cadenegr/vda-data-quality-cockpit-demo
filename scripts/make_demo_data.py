"""Generate fictional VDA messages for the demo (the hackathon dataset is confidential and not in this repo).

One month (June 2026) of inbound logistics for one plant: fictional suppliers book trucks (VDA 4933 T1),
send delivery notes (VDA 4987), and fictional carriers send truck status messages (VDA 4945).
Every company gets its own "habits" (e.g. never fills the booking number, sends placeholders, malformed
timeslots), so scores, warnings, alarms and fix suggestions look like they did on the real data.

usage: python3 scripts/make_demo_data.py [out_dir]     (default: demo_data/, deterministic, ~1 MB zip)
"""
import json
import os
import random
import sys
import zipfile
from datetime import date, timedelta

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PLANT = "10"
UNLOADING_POINTS = ["UP1", "UP2", "UP4"]

SUPPLIER_NAMES = [
    "Brennwald Metallteile GmbH", "Kessler & Voss Kunststofftechnik", "Nordhain Elektronik GmbH",
    "Halvorsen Gusswerk AS", "Sonnberg Dichtungssysteme", "Altmühl Kabelwerk GmbH", "Moravec Precision s.r.o.",
    "Brightwater Plastics Ltd", "Lindqvist Fasteners AB", "Ostrava Pressings a.s.", "Fichtel Sitzsysteme GmbH",
    "Valcourt Composants SAS", "Rheinau Glas & Spiegel", "Tamsweg Lagertechnik GmbH", "Wielkopolska Wiring Sp. z o.o.",
    "Eichstedt Zierteile KG", "Ambergau Filter GmbH", "Seewinkel Gummiwerk GmbH",
]
CARRIER_NAMES = ["Falkenroute Logistik GmbH", "Bluelane Transport BV", "Westwind Spedition",
                 "Kranich Freight Sp. z o.o.", "Polarhaul Logistics"]
CITIES = [(48.14, 11.58), (49.45, 11.08), (50.11, 8.68), (48.78, 9.18), (49.01, 12.10), (50.08, 14.43),
          (48.21, 16.37), (52.23, 21.01), (51.34, 12.37), (47.81, 13.04)]
PLATE_PREFIX = ["M", "N", "IN", "R", "S", "A", "PA", "WL", "PZ", "KR"]


def make_companies(rnd):
    carriers = []
    for i, name in enumerate(CARRIER_NAMES):
        good = [0.95, 0.7, 0.45, 0.85, 0.3][i]  # how disciplined the carrier's IT is
        carriers.append({
            "code": f"{700100 + i * 100}", "duns": f"{516000100 + i}", "name": name,
            "p_transport_ph": (1 - good) * 0.6, "p_to_missing": (1 - good) * 0.5, "p_ship_ph": (1 - good) * 0.9,
            "p_slot_missing": (1 - good) * 0.5, "p_slot_bad": 0.25 if i == 2 else 0.02,
            "p_plate_bad": 0.6 if i == 4 else 0.03, "p_geo_bad": 0.15 if i in (2, 4) else 0.01,
            "p_send": 0.6 + 0.35 * good,
        })
    suppliers = []
    for i, name in enumerate(SUPPLIER_NAMES):
        good = rnd.uniform(0.05, 0.9)
        suppliers.append({
            "code": f"{100100 + i * 100}", "name": name, "key": f"DUC.S{100100 + i * 100}KEY",
            "duns": f"{600000100 + i}" if i not in (5, 13) else f"{600000100 + i}-00",  # two send a malformed DUNS
            "size": rnd.choice([1, 1, 2, 2, 3]),
            "parts": [f"{3000000000 + i * 1000 + k * 7}" for k in range(rnd.randint(3, 9))],
            "carriers": rnd.sample(carriers, rnd.choice([1, 1, 2])),
            "p_asn_to_missing": 1 - good, "p_asn_to_plant": 0.3 if i in (3, 11) else 0.0,
            "p_qty_bad": 0.08 if i in (7, 15) else 0.005, "eta_placeholder": i in (2, 9, 16),
            "single_as_object": i % 4 == 1, "p_conv_fail": 0.06 if i in (6, 12) else 0.0,
            "no_bookings": i == 17,  # sends delivery notes but no T1 -> booking numbers can't be verified
        })
    return suppliers, carriers


def t1_msg(sup, car, to, d, parts, rnd):
    groups = [{"despatchedArticle": {"partNumber": p, "despatchedQuantity": {
        "value": f"{q:.3f}" if rnd.random() > sup["p_qty_bad"] else "0.000", "measurementUnitCode": ["PCE"]}}}
        for p, q in parts]
    eta = d + timedelta(days=rnd.choice([0, 1, 1]))
    return {"VDA4933T1": {"advancedShippingNotes": {"advancedShippingNote": {
        "interchangeHeader": {"workflowId": f"WF{to}"},
        "beginningOfMessage": {"documentIdentifier": to},
        "shipFrom": {"supplierCode": "0" + sup["code"], "duns": sup["duns"], "name": sup["name"]},
        "shipTo": {"plantCode": PLANT, "unloadingPoint": {"locationIdAssignedByBuyer": rnd.choice(UNLOADING_POINTS)}},
        "forwarder": {"supplierCode": "0" + car["code"], "duns": car["duns"], "name": car["name"]},
        "requestedShipmentDateAndTime": {"date": d.isoformat(), "time": f"{rnd.randint(5, 14):02d}:00:00"},
        "estimatedArrivalDateAndTime": {"date": eta.isoformat(),
                                        "time": "00:00:00" if sup["eta_placeholder"] else f"{rnd.randint(6, 20):02d}:30:00"},
        "listOfHandlingUnitGroup": {"handlingUnitGroupWithParts": groups[0] if sup["single_as_object"] and len(groups) == 1 else groups},
    }}}}


def asn_msg(sup, car, to, note, desp, parts, rnd, n):
    if rnd.random() < sup["p_asn_to_missing"]:
        to = ""
    elif rnd.random() < sup["p_asn_to_plant"]:
        to = PLANT
    refs = {"rff_CRN_shipmentNumber": {"value": note}}
    if to:
        refs["rff_AAO_transportOrderNumber"] = {"value": to}
    arrival = desp + timedelta(days=1)
    return {"_meta": {"sourceFile": f"{sup['key']}.VDA4987({n}).AU"},
            "header": {"BGM": {"docId": f"A{note}"}, "dtm_11_despatchDate": {"value": desp.strftime("%Y%m%d"), "format": "102"},
                       "dtm_132_estimatedArrival": {"value": arrival.strftime("%Y%m%d") + "1000", "format": "203"}},
            "references": refs,
            "parties": {"SF": {"id": sup["code"], "duns": sup["duns"], "name1": sup["name"]},
                        "ST": {"id": PLANT}, "FW": {"id": car["code"], "duns": car["duns"], "name1": car["name"]}},
            "transport": {"TDT": {"modeCode": "30"}},
            "cps_groups": [{"lin_articles": [{"itemIdentifier": p,
                                              "qty_12_despatchedQuantity": {"value": f"{q:g}" if rnd.random() > sup["p_qty_bad"] else "",
                                                                            "unit": "PCE"}}]} for p, q in parts]}


def ts_msg(car, sup, to, note, d, process, rnd, doc):
    ph = lambda p: rnd.random() < p
    lat, lon = rnd.choice(CITIES)
    plate = f"{rnd.choice(PLATE_PREFIX)} {rnd.choice('ABCDEFGHKLMNPRSTVXZ')}{rnd.choice('ABCDEFGHKLMNPRSTVXZ')} {rnd.randint(100, 9999)}"
    slot = f"{PLANT}00{d.strftime('%y%m%d')}{rnd.randint(0, 99999):05d}{rnd.randint(0, 9)}"[:15].ljust(15, "0")
    if ph(car["p_slot_bad"]):
        slot = slot[:10]
    elif ph(car["p_slot_missing"]):
        slot = ""
    status = {"transportInstructionNumber": {"referenceNumber": rnd.choice(["UNKNOWN", "0", "FOLGT"]) if ph(car["p_to_missing"]) else to},
              "shipmentNumberAssignedByShipFrom": {"referenceNumber": rnd.choice(["FOLGT", "0", "UNKNOWN"]) if ph(car["p_ship_ph"]) else note},
              "shipFrom": {"supplierCode": sup["code"], "duns": sup["duns"]}, "shipTo": {"plantCode": PLANT}}
    if slot:
        status["accessNumAtDropOff"] = {"referenceNumber": slot}
    return {"IFTSTA": {"statusMessages": {"statusMessage": {
        "beginningOfMessage": {"documentIdentifier": doc, "processIndicator": process},
        "forwarder": {"supplierCode": car["code"], "duns": car["duns"], "name": car["name"]},
        "transportNumber": {"referenceNumber": rnd.choice(["UNKNOWN", "FOLGT", "0"]) if ph(car["p_transport_ph"]) else f"TR{doc[-6:]}"},
        "statusPosition": {"geocoordinates": "0,0" if ph(car["p_geo_bad"]) else f"{lat + rnd.uniform(-.3, .3):.4f},{lon + rnd.uniform(-.3, .3):.4f}"},
        "geoPositionDateAndTime": {"date": d.isoformat(), "time": f"{rnd.randint(6, 21):02d}:{rnd.randint(0, 59):02d}:00", "timeZone": "+0200"},
        "transportInformationAndStatus": {"transportInformation": {"transportMeansIdentifier": {
            "identifier": rnd.choice(["UNKNOWN", "a3f9c2e81b77d0"]) if ph(car["p_plate_bad"]) else plate}}},
        "listOfShipmentStatus": {"shipmentStatus": status},
    }}}}


def generate(out_dir, seed=42):
    rnd = random.Random(seed)
    suppliers, carriers = make_companies(rnd)
    files = []
    to_n, note_n, doc_n, asn_n = 19000100, 600100, 0, 0
    day = date(2026, 6, 1)
    while day <= date(2026, 6, 30):
        if day.weekday() < 5:
            for sup in suppliers:
                for _ in range(rnd.randint(0, sup["size"] + 1)):
                    to_n += 1
                    note_n += 1
                    car = rnd.choice(sup["carriers"])
                    to, note = str(to_n), str(note_n)
                    k = min(len(sup["parts"]), rnd.choice([1, 1, 2, 2, 3]))
                    parts = [(p, rnd.choice([10, 20, 40, 60, 120, 240])) for p in rnd.sample(sup["parts"], k)]
                    if not sup["no_bookings"]:
                        files.append((f"VDA4933/{to}_VDA4933.json", t1_msg(sup, car, to, day, parts, rnd)))
                    desp = day - timedelta(days=1) if rnd.random() < 0.05 else day
                    asn_n += 1
                    if rnd.random() < sup["p_conv_fail"]:
                        files.append((f"VDA4987/FEHLER/{sup['key']}.VDA4987({asn_n}).AU_ERROR.txt",
                                      "Norm nicht erkannt (standard not recognised)\n"))
                    else:
                        files.append((f"VDA4987/{note}_VDA4987.json", asn_msg(sup, car, to, note, desp, parts, rnd, asn_n)))
                    if rnd.random() < car["p_send"]:
                        for offset, process in ((0, "MAT-PICKUP"), (1, "MAT-ARRIVAL")):
                            doc_n += 1
                            doc = f"S{doc_n:07d}"
                            files.append((f"VDA4945/{doc}_VDA4945.json",
                                          ts_msg(car, sup, to, note, day + timedelta(days=offset), process, rnd, doc)))
        day += timedelta(days=1)
    os.makedirs(out_dir, exist_ok=True)
    path = os.path.join(out_dir, "demo_june_2026.zip")
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, m in files:
            zf.writestr(name, m if isinstance(m, str) else json.dumps(m, ensure_ascii=False))
    print(f"wrote {len(files):,} fictional messages ({len(suppliers)} suppliers, {len(carriers)} carriers) to {path}")
    return path


if __name__ == "__main__":
    generate(sys.argv[1] if len(sys.argv) > 1 else os.path.join(ROOT, "demo_data"))
