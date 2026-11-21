"""VDA data-quality core: parse VDA4933 T1 / VDA4945 / VDA4987 JSON messages,
flatten the fields that matter for DiTA matching, and run quality rules.

Pure standard library, so the same code runs locally and in a Databricks notebook.
"""
import hashlib
import json
import os
import re
import zipfile
from collections import Counter, defaultdict
from datetime import date, timedelta

# ---------------------------------------------------------------- helpers

def aslist(x):
    if x is None:
        return []
    return x if isinstance(x, list) else [x]


def g(obj, *path):
    """Safe nested get: g(d, 'a', 'b', 'c')."""
    for p in path:
        if not isinstance(obj, dict):
            return None
        obj = obj.get(p)
    return obj


def s(v):
    """Normalise a scalar to a stripped string ('' for missing)."""
    if v is None:
        return ""
    return str(v).strip()


def norm_code(v):
    """Supplier / carrier codes come with different zero padding per message type."""
    v = s(v)
    return v.lstrip("0") or v


PLACEHOLDERS = {"", "0", "null", "none", "unknown", "folgt", "n/a", "na", "-", "tbd", "xxx"}


def is_placeholder(v):
    return s(v).lower() in PLACEHOLDERS


DUNS_RE = re.compile(r"^\d{9}$")
TIMESLOT_RE = re.compile(r"^\d{15}$")
HEX_RE = re.compile(r"^[0-9a-f]{10,}$")


def plausible_plate(v):
    v = s(v)
    if is_placeholder(v):
        return False
    if HEX_RE.match(v.lower()) and not re.search(r"[g-z]", v.lower()):
        return False  # hashed / technical id instead of a plate
    return bool(re.search(r"[A-Za-z]", v)) and bool(re.search(r"\d", v)) and 4 <= len(v.replace(" ", "").replace("-", "")) <= 10


def valid_geo(v):
    try:
        lat, lon = (float(x) for x in s(v).split(","))
    except ValueError:
        return False
    return -90 <= lat <= 90 and -180 <= lon <= 180 and not (lat == 0 and lon == 0)


def parse_qty(v):
    try:
        return float(s(v))
    except ValueError:
        return None


def edi_date(value, fmt):
    """VDA4987 dates: format 102 = CCYYMMDD, 203 = CCYYMMDDHHMM. Returns 'YYYY-MM-DD' or None."""
    v = s(value)
    if fmt == "102" and re.fullmatch(r"\d{8}", v):
        return f"{v[:4]}-{v[4:6]}-{v[6:8]}"
    if fmt == "203" and re.fullmatch(r"\d{12}", v):
        return f"{v[:4]}-{v[4:6]}-{v[6:8]}"
    return None


# ---------------------------------------------------------------- reading

def iter_messages(source):
    """Yield (msg_type, source_name, text) from a folder of zips, a folder of
    extracted folders, or a single zip. msg_type in {'4933','4945','4987','error'}."""
    paths = []
    if os.path.isdir(source):
        for root, _, files in os.walk(source):
            for f in files:
                paths.append(os.path.join(root, f))
    else:
        paths.append(source)
    for p in sorted(paths):
        if p.lower().endswith(".zip"):
            try:
                zf = zipfile.ZipFile(p)
            except zipfile.BadZipFile:
                continue
            with zf:
                for name in zf.namelist():
                    t = _classify(name)
                    if t:
                        yield t, f"{os.path.basename(p)}:{name}", zf.read(name).decode("utf-8", errors="replace")
        else:
            t = _classify(p)
            if t:
                with open(p, encoding="utf-8", errors="replace") as fh:
                    yield t, p, fh.read()


def _classify(name):
    n = name.replace("\\", "/")
    if "/FEHLER/" in n or n.endswith("_ERROR.txt"):
        return "error"
    if not n.endswith(".json"):
        return None
    for t in ("4933", "4945", "4987"):
        if f"VDA{t}" in os.path.basename(n):
            return t
    return None


# ---------------------------------------------------------------- flatten

def flatten_4933(src, d):
    a = g(d, "VDA4933T1", "advancedShippingNotes", "advancedShippingNote") or {}
    groups = g(a, "listOfHandlingUnitGroup", "handlingUnitGroupWithParts")
    parts = []
    for grp in aslist(groups):
        art = grp.get("despatchedArticle") or {}
        parts.append({
            "part_number": s(art.get("partNumber")),
            "quantity": s(g(art, "despatchedQuantity", "value")),
            "unit": ",".join(aslist(g(art, "despatchedQuantity", "measurementUnitCode"))),
        })
    return {
        "source": src,
        "workflow_id": s(g(a, "interchangeHeader", "workflowId")),
        "to_number": s(g(a, "beginningOfMessage", "documentIdentifier")),
        "app_reference": s(g(a, "applicationReferenceNumber", "referenceNumber")),
        "supplier_code": norm_code(g(a, "shipFrom", "supplierCode")),
        "supplier_duns": s(g(a, "shipFrom", "duns")),
        "supplier_name": s(g(a, "shipFrom", "name")),
        "plant_code": s(g(a, "shipTo", "plantCode")),
        "unloading_point": s(g(a, "shipTo", "unloadingPoint", "locationIdAssignedByBuyer")),
        "carrier_code": norm_code(g(a, "forwarder", "supplierCode")),
        "carrier_duns": s(g(a, "forwarder", "duns")),
        "carrier_name": s(g(a, "forwarder", "name")),
        "despatch_advice_date": s(g(a, "despatchAdviceDateAndTime", "date")),
        "requested_shipment_date": s(g(a, "requestedShipmentDateAndTime", "date")),
        "requested_shipment_time": s(g(a, "requestedShipmentDateAndTime", "time")),
        "estimated_arrival_date": s(g(a, "estimatedArrivalDateAndTime", "date")),
        "estimated_arrival_time": s(g(a, "estimatedArrivalDateAndTime", "time")),
        "plate_at_pickup": s(g(a, "accessNumberAtPickUp", "referenceNumber")),
        "n_positions": len(parts),
        "part_numbers": ";".join(p["part_number"] for p in parts),
        "_parts": parts,
        "_parts_is_list": isinstance(groups, list),
        "_has_bad_encoding": "�" in json.dumps(a, ensure_ascii=False),
    }


def flatten_4945(src, d):
    rows = []
    for mi, sm in enumerate(aslist(g(d, "IFTSTA", "statusMessages", "statusMessage"))):
        tis = g(sm, "transportInformationAndStatus") or {}
        base = {
            "source": src,
            "msg_index": mi,
            "document_id": s(g(sm, "beginningOfMessage", "documentIdentifier")),
            "process": s(g(sm, "beginningOfMessage", "processIndicator")),
            "carrier_code": norm_code(g(sm, "forwarder", "supplierCode")),
            "carrier_duns": s(g(sm, "forwarder", "duns")),
            "carrier_name": s(g(sm, "forwarder", "name")),
            "sender_code": norm_code(g(sm, "messageSender", "supplierCode")),
            "transport_number": s(g(sm, "transportNumber", "referenceNumber")),
            "geo": s(g(sm, "statusPosition", "geocoordinates")),
            "geo_date": s(g(sm, "geoPositionDateAndTime", "date")),
            "geo_time": s(g(sm, "geoPositionDateAndTime", "time")),
            "geo_tz": s(g(sm, "geoPositionDateAndTime", "timeZone")),
            "plate": s(g(tis, "transportInformation", "transportMeansIdentifier", "identifier")),
            "plate_country": s(g(tis, "transportInformation", "transportMeansIdentifier", "countryCode")),
        }
        shipments = aslist(g(sm, "listOfShipmentStatus", "shipmentStatus")) or [{}]
        for si, ss in enumerate(shipments):
            r = dict(base)
            r.update({
                "shipment_index": si,
                "to_number": s(g(ss, "transportInstructionNumber", "referenceNumber")),
                "shipment_number": s(g(ss, "shipmentNumberAssignedByShipFrom", "referenceNumber")),
                "timeslot_id": s(g(ss, "accessNumAtDropOff", "referenceNumber")),
                "supplier_code": norm_code(g(ss, "shipFrom", "supplierCode")),
                "supplier_duns": s(g(ss, "shipFrom", "duns")),
                "plant_code": s(g(ss, "shipTo", "plantCode")),
                "status_code": s(ss.get("statusCode")) if ss else "",
                "eta_date": s(g(ss, "etaForThisShipment", "date")),
            })
            rows.append(r)
    return rows


def flatten_4987(src, d):
    parties = d.get("parties") or {}
    hdr = d.get("header") or {}
    items = []
    for grp in aslist(d.get("cps_groups")):
        for art in aslist(grp.get("lin_articles")):
            if not isinstance(art, dict):
                continue
            items.append({
                "part_number": s(art.get("itemIdentifier")),
                "quantity": s(g(art, "qty_12_despatchedQuantity", "value")),
                "unit": s(g(art, "qty_12_despatchedQuantity", "unit")),
                "calloff": s(art.get("rff_AAU_callOffNumber")),
            })

    def dt(key):
        v = hdr.get(key) or {}
        return s(v.get("value")), s(v.get("format"))

    desp, desp_f = dt("dtm_11_despatchDate")
    arr, arr_f = dt("dtm_132_estimatedArrival")
    sf = parties.get("SF") or {}
    se = parties.get("SE") or {}
    fw = parties.get("FW") or {}
    return {
        "source": src,
        "source_file": s(g(d, "_meta", "sourceFile")),
        "key_prefix": _key_prefix(g(d, "_meta", "sourceFile")),
        "asn_number": s(g(hdr, "BGM", "docId")),
        "shipment_number": s(g(d, "references", "rff_CRN_shipmentNumber", "value")),
        "to_number": s(g(d, "references", "rff_AAO_transportOrderNumber", "value")),
        "supplier_code": norm_code(sf.get("id") or se.get("id")),
        "supplier_duns": s(sf.get("duns") or se.get("duns")),
        "supplier_name": s(sf.get("name1") or se.get("name1")),
        "plant_code": s(g(parties, "ST", "id")),
        "carrier_code": norm_code(fw.get("id")),
        "carrier_duns": s(fw.get("duns")),
        "carrier_name": s(fw.get("name1")),
        "despatch_raw": desp, "despatch_format": desp_f,
        "despatch_date": edi_date(desp, desp_f) or "",
        "arrival_raw": arr, "arrival_format": arr_f,
        "arrival_date": edi_date(arr, arr_f) or "",
        "transport_mode": s(g(d, "transport", "TDT", "modeCode")),
        "n_items": len(items),
        "part_numbers": ";".join(i["part_number"] for i in items),
        "_items": items,
        "_has_bad_encoding": "�" in json.dumps(parties, ensure_ascii=False),
    }


def _key_prefix(source_file):
    m = re.match(r"(DUC\.[A-Z0-9]+KEY)", s(source_file))
    return m.group(1) if m else ""


# ---------------------------------------------------------------- rules

# rule_id -> (message, severity, field path, what it breaks, how to fix)
RULES = {
    "T1_TO_MISSING": ("VDA4933 T1", "error", "/beginningOfMessage/documentIdentifier",
                      "Transport order has no TO number - nothing can be matched to it.",
                      "Send the transport order number in beginningOfMessage/documentIdentifier."),
    "T1_NO_PARTS": ("VDA4933 T1", "error", "/listOfHandlingUnitGroup/handlingUnitGroupWithParts[]/despatchedArticle/partNumber",
                    "No part numbers - the TO cannot be compared with the despatch advice (4987).",
                    "List every despatched article with its part number."),
    "T1_QTY_INVALID": ("VDA4933 T1", "error", "/.../despatchedArticle/despatchedQuantity/value",
                       "Quantity is missing, zero or not numeric.",
                       "Send the positive despatched quantity per article."),
    "T1_DUNS_FORMAT": ("VDA4933 T1", "warning", "/shipFrom/duns",
                       "Supplier DUNS is not a 9-digit number.",
                       "Use the 9-digit DUNS of the ship-from location."),
    "T1_DATE_ORDER": ("VDA4933 T1", "error", "/estimatedArrivalDateAndTime/date",
                      "Estimated arrival is before the requested pick-up date.",
                      "Check pick-up and arrival dates; arrival must be after pick-up."),
    "T1_ETA_PLACEHOLDER": ("VDA4933 T1", "info", "/estimatedArrivalDateAndTime/time",
                           "Arrival time is always 00:00:00 - looks like a default, not a planned time.",
                           "Send the planned arrival time (or the time window) instead of midnight."),
    "T1_SINGLE_AS_OBJECT": ("VDA4933 T1", "info", "/listOfHandlingUnitGroup/handlingUnitGroupWithParts",
                            "Repeating group sent as a single object instead of a list - parsers expecting a list break.",
                            "Always send handlingUnitGroupWithParts as an array, even with one element."),
    "T1_ENCODING": ("VDA4933 T1", "info", "(text fields)",
                    "Broken character encoding (a letter like ä, ü or ß arrives as �).",
                    "Encode the message in UTF-8 (or declare the EDIFACT character set correctly)."),
    "T1_NO_STATUS": ("VDA4933 T1", "warning", "-> VDA4945 transportInstructionNumber",
                     "No transport status (4945) references this TO - the truck cannot be tracked or matched.",
                     "Carrier must reference this TO number in VDA4945 transportInstructionNumber."),

    "TS_TRANSPORTNO_PLACEHOLDER": ("VDA4945", "error", "/statusMessage/transportNumber/referenceNumber",
                                   "Transport number is a placeholder (UNKNOWN / FOLGT / 0).",
                                   "Send the real transport / trip number as soon as it is known."),
    "TS_TO_MISSING": ("VDA4945", "error", "/shipmentStatus/transportInstructionNumber/referenceNumber",
                      "No transport order number - matching step 1 (4933 T1 <-> 4945) is impossible.",
                      "Reference the TO number from the supplier's VDA4933 T1."),
    "TS_TO_UNKNOWN": ("VDA4945", "info", "/shipmentStatus/transportInstructionNumber/referenceNumber",
                      "TO number does not exist in any received VDA4933 T1 (same plant).",
                      "Check for typos / use the documentIdentifier of the 4933 T1 exactly."),
    "TS_SHIPMENTNO_PLACEHOLDER": ("VDA4945", "warning", "/shipmentStatus/shipmentNumberAssignedByShipFrom/referenceNumber",
                                  "Delivery note / shipment number is '0' or empty - fallback matching (step 3) impossible.",
                                  "Send the supplier's delivery note number."),
    "TS_TIMESLOT_MISSING": ("VDA4945", "error", "/shipmentStatus/accessNumAtDropOff/referenceNumber",
                            "No timeslot-booking ID - no direct entry at the plant gate.",
                            "Send the 15-digit timeslot-booking ID."),
    "TS_TIMESLOT_FORMAT": ("VDA4945", "error", "/shipmentStatus/accessNumAtDropOff/referenceNumber",
                           "Timeslot-booking ID is not 15 digits.",
                           "Send exactly the 15-digit ID from the timeslot booking."),
    "TS_PLATE_INVALID": ("VDA4945", "warning", "/transportInformation/transportMeansIdentifier/identifier",
                         "License plate missing or not a plate (e.g. a hash / technical id).",
                         "Send the tractor/trailer license plate as on the vehicle."),
    "TS_GEO_INVALID": ("VDA4945", "warning", "/statusMessage/statusPosition/geocoordinates",
                       "GPS position missing or invalid.",
                       "Send 'lat,lon' in decimal degrees."),

    "ASN_TO_MISSING": ("VDA4987", "error", "/references/rff_AAO_transportOrderNumber/value",
                       "Despatch advice has no transport order number - matching step 2 impossible, no complete DiTA.",
                       "Fill RFF+AAO with the TO number of the VDA4933 T1."),
    "ASN_TO_IMPLAUSIBLE": ("VDA4987", "error", "/references/rff_AAO_transportOrderNumber/value",
                           "TO number is wrong: the plant code, the delivery note's own number, a number this "
                           "supplier never used in its 4933 T1, or one number on delivery notes sent days apart.",
                           "Fill RFF+AAO with the TO number, not the plant or another reference."),
    "ASN_SHIPMENTNO_MISSING": ("VDA4987", "error", "/references/rff_CRN_shipmentNumber/value",
                               "No shipment / delivery note number.",
                               "Fill RFF+CRN with the delivery note number."),
    "ASN_NO_ITEMS": ("VDA4987", "error", "/cps_groups[]/lin_articles[]",
                     "Despatch advice contains no articles.",
                     "Send LIN segments with part number and quantity."),
    "ASN_QTY_INVALID": ("VDA4987", "error", "/cps_groups[]/lin_articles[]/qty_12_despatchedQuantity/value",
                        "Article quantity missing, zero or not numeric.",
                        "Send QTY+12 with the positive despatched quantity."),
    "ASN_DATE_INVALID": ("VDA4987", "error", "/header/dtm_11_despatchDate",
                         "Despatch date does not match its declared format (102=CCYYMMDD, 203=CCYYMMDDHHMM).",
                         "Send the date in the format declared in DTM qualifier."),
    "ASN_DATE_ORDER": ("VDA4987", "warning", "/header/dtm_132_estimatedArrival",
                       "Estimated arrival is before despatch.",
                       "Arrival date must be on/after the despatch date."),
    "ASN_DUNS_FORMAT": ("VDA4987", "warning", "/parties/SF/duns",
                        "Ship-from DUNS is not a 9-digit number.",
                        "Use the 9-digit DUNS."),
    "ASN_ENCODING": ("VDA4987", "info", "/parties/* (names, addresses)",
                     "Broken character encoding in a party name or address (a letter like ä, ü or ß arrives as �).",
                     "Encode in UTF-8 / declare the correct UNB character set."),
    "ASN_NO_STATUS": ("VDA4987", "info", "-> VDA4945 shipmentNumberAssignedByShipFrom",
                      "No transport status (4945) contains this delivery note number - fallback matching impossible. "
                      "Consequence of the missing TO number; disappears when RFF+AAO is filled.",
                      "Fill RFF+AAO (primary fix). Optionally the carrier sends the delivery note number in VDA4945."),
    "ASN_TO_UNVERIFIED": ("VDA4987", "info", "/references/rff_AAO_transportOrderNumber/value",
                          "TO number is filled but cannot be verified: the data contains no 4933 T1 of this supplier.",
                          "Nothing to fix if the number is the real transport order number."),
    "ASN_CONVERSION_FAILED": ("VDA4987", "error", "(whole message)",
                              "Message could not be converted at all: 'Norm nicht erkannt' (standard not recognised).",
                              "Send a VDA4987 / DESADV message conforming to the OEM's guideline (check UNH association code)."),
}


SENDER = {"4933": "supplier", "4987": "supplier", "4945": "carrier"}

# Matching = can the message be linked into the digital transport file (DiTA)?
# Completeness = are the mandatory contents there?   Format = are values well-formed?
CATEGORY = {}
for _r in ("T1_NO_STATUS", "TS_TO_MISSING", "TS_TO_UNKNOWN", "TS_SHIPMENTNO_PLACEHOLDER", "TS_TRANSPORTNO_PLACEHOLDER",
           "ASN_TO_MISSING", "ASN_TO_IMPLAUSIBLE", "ASN_TO_UNVERIFIED", "ASN_NO_STATUS", "ASN_SHIPMENTNO_MISSING"):
    CATEGORY[_r] = "matching"
for _r in ("T1_TO_MISSING", "T1_NO_PARTS", "T1_QTY_INVALID", "TS_TIMESLOT_MISSING", "TS_PLATE_INVALID", "TS_GEO_INVALID",
           "ASN_NO_ITEMS", "ASN_QTY_INVALID", "ASN_CONVERSION_FAILED"):
    CATEGORY[_r] = "completeness"
for _r in RULES:
    CATEGORY.setdefault(_r, "format")
CATEGORIES = ("matching", "completeness", "format")

# Plain-language version of each rule for non-experts: (title, who has to fix it, why it matters)
IT, CARRIER, TMS, SUPPLIER = "your IT / EDI team", "your carrier", "your transport system (TMS)", "the supplier"
COSMETIC = "Cosmetic: one letter in a name or address shows as �. It doesn't stop the truck."
NO_LINK = "The plant can't tell which truck carries these parts, so the truck is checked by hand at the gate."
PLAIN = {
    "T1_TO_MISSING": ("Your transport booking has no booking number", IT, NO_LINK),
    "T1_NO_PARTS": ("Your transport booking doesn't list the parts", IT, "The booking can't be compared with the delivery note."),
    "T1_QTY_INVALID": ("A part amount in your booking is missing or zero", IT, "The plant can't plan what arrives."),
    "T1_DUNS_FORMAT": ("Your company ID (DUNS) is not a valid 9-digit number", IT, "Your messages may not be assigned to you."),
    "T1_DATE_ORDER": ("Arrival date is before the pick-up date", IT, "The plan for the truck is impossible."),
    "T1_ETA_PLACEHOLDER": ("Arrival time is always 00:00 (a default, not a plan)", IT, "The plant can't plan the unloading slot."),
    "T1_SINGLE_AS_OBJECT": ("Parts list sent in a non-standard technical form", IT, "Some systems can't read it."),
    "T1_ENCODING": ("A special character (ä, ö, ü, ß) arrives broken", IT, COSMETIC),
    "T1_NO_STATUS": ("No truck tracking mentions this booking", CARRIER, NO_LINK),
    "TS_TRANSPORTNO_PLACEHOLDER": ("Trip number is a placeholder (UNKNOWN / FOLGT / 0)", TMS, "The trip can't be identified."),
    "TS_TO_MISSING": ("Tracking doesn't say which booking the truck carries", TMS, NO_LINK),
    "TS_TO_UNKNOWN": ("Tracking names a booking the plant never received", SUPPLIER, "The truck can't be matched to an order."),
    "TS_SHIPMENTNO_PLACEHOLDER": ("Tracking doesn't say which delivery notes are on the truck", TMS,
                                  "The backup way of matching the truck fails."),
    "TS_TIMESLOT_MISSING": ("No gate time-slot number sent", TMS, "Without a slot the truck can't drive straight in."),
    "TS_TIMESLOT_FORMAT": ("Gate time-slot number is not 15 digits", TMS, "The gate system can't find the slot."),
    "TS_PLATE_INVALID": ("Licence plate missing or not a real plate", TMS, "The gate can't recognise the truck."),
    "TS_GEO_INVALID": ("GPS position missing or invalid", TMS, "The plant can't see where the truck is."),
    "ASN_TO_MISSING": ("Delivery note doesn't say which transport booking it belongs to", IT, NO_LINK),
    "ASN_TO_IMPLAUSIBLE": ("Delivery note has a wrong booking number (e.g. the plant code)", IT, NO_LINK),
    "ASN_TO_UNVERIFIED": ("Booking number can't be checked (we have none of your bookings in this data)",
                          "nobody (a gap in our data)", "None if the number is right."),
    "ASN_SHIPMENTNO_MISSING": ("Delivery note has no delivery note number", IT, NO_LINK),
    "ASN_NO_ITEMS": ("Delivery note lists no parts", IT, "The plant doesn't know what is on the truck."),
    "ASN_QTY_INVALID": ("A part amount on the delivery note is missing or zero", IT, "The plant doesn't know how much arrives."),
    "ASN_DATE_INVALID": ("Despatch date is in the wrong format", IT, "The delivery can't be placed in time."),
    "ASN_DATE_ORDER": ("Arrival date is before the despatch date", IT, "The plan for the truck is impossible."),
    "ASN_DUNS_FORMAT": ("Company ID (DUNS) is not a valid 9-digit number", IT, "Your messages may not be assigned to you."),
    "ASN_ENCODING": ("A special character (ä, ö, ü, ß) arrives broken", IT, COSMETIC),
    "ASN_NO_STATUS": ("No truck tracking mentions this delivery note", CARRIER,
                      "Only matters while the booking number is missing: filling it in solves this too."),
    "ASN_CONVERSION_FAILED": ("Message could not be read at all", IT, "The plant receives nothing usable."),
}


def finding(rule, msg_type, key, supplier, carrier, value="", source="", severity=None, date=""):
    return {
        "rule_id": rule, "msg_type": msg_type, "message_key": key, "msg_date": date,
        "category": CATEGORY[rule],
        "supplier_code": supplier, "carrier_code": carrier,
        "responsible": SENDER[msg_type],
        "severity": severity or RULES[rule][1], "value": s(value)[:80], "source": source,
    }


# ---------------------------------------------------------------- per-message checks
# Each check looks at ONE flattened message plus a context of what the plant already knows (other messages).
# run() applies them to the whole dataset; check_message() applies them to a single new message.

def booking_reused_across_days(asn, min_days=2):
    """(supplier, booking number) used on different delivery notes despatched >= min_days apart.
    One truck booking can't carry them all, so such a number is not a real booking number."""
    seen = defaultdict(lambda: defaultdict(set))  # (supplier, number) -> delivery note -> dates
    for a in asn:
        d = _date(a["despatch_date"])
        if d and not is_placeholder(a["to_number"]) and not is_placeholder(a["shipment_number"]):
            seen[(a["supplier_code"], a["to_number"])][a["shipment_number"]].add(d)
    out = set()
    for key, notes in seen.items():
        dates = sorted(min(ds) for ds in notes.values())  # one date per note (resent notes count once)
        if len(notes) >= 2 and (dates[-1] - dates[0]).days >= min_days:
            out.add(key)
    return out


def context(t1, ts, asn=()):
    """What the plant already knows: transport order numbers, numbers referenced in tracking, plants,
    and (from all delivery notes) booking numbers reused across days."""
    return {
        "reused_to": booking_reused_across_days(asn),
        "to_t1": {r["to_number"] for r in t1 if r["to_number"]},
        "plants_t1": {r["plant_code"] for r in t1},
        "suppliers_t1": {norm_code(r["supplier_code"]) for r in t1},
        "to_ts": {r["to_number"] for r in ts if not is_placeholder(r["to_number"])},
        "ship_ts": {r["shipment_number"] for r in ts if not is_placeholder(r["shipment_number"])},
    }


def check_4933(r, ctx):
    out = []
    k, sup, car = r["to_number"] or r["workflow_id"], r["supplier_code"], r["carrier_code"]
    F = lambda rule, v="": out.append(finding(rule, "4933", k, sup, car, v, r["source"], date=r["requested_shipment_date"]))
    if is_placeholder(r["to_number"]):
        F("T1_TO_MISSING", r["to_number"])
    if not r["_parts"] or all(not p["part_number"] for p in r["_parts"]):
        F("T1_NO_PARTS")
    for p in r["_parts"]:
        q = parse_qty(p["quantity"])
        if q is None or q <= 0:
            F("T1_QTY_INVALID", f'{p["part_number"]}={p["quantity"]}')
            break
    if not DUNS_RE.match(r["supplier_duns"]):
        F("T1_DUNS_FORMAT", r["supplier_duns"])
    if r["estimated_arrival_date"] and r["requested_shipment_date"] and r["estimated_arrival_date"] < r["requested_shipment_date"]:
        F("T1_DATE_ORDER", f'{r["requested_shipment_date"]} -> {r["estimated_arrival_date"]}')
    if r["estimated_arrival_time"] == "00:00:00":
        F("T1_ETA_PLACEHOLDER", r["estimated_arrival_time"])
    if r["_parts"] and not r["_parts_is_list"]:
        F("T1_SINGLE_AS_OBJECT")
    if r["_has_bad_encoding"]:
        F("T1_ENCODING")
    if r["to_number"] and r["to_number"] not in ctx["to_ts"]:
        F("T1_NO_STATUS", r["to_number"])
    return out


def check_4945(r, ctx, seen_msg):
    """One shipment-status row. seen_msg: set shared across rows so message-level checks run once per message."""
    out = []
    k = f'{r["document_id"]}#{r["shipment_index"]}'
    sup, car = r["supplier_code"], r["carrier_code"]
    F = lambda rule, v="": out.append(finding(rule, "4945", k, sup, car, v, r["source"], date=r["geo_date"]))
    msg_key = (r["source"], r["msg_index"])
    if msg_key not in seen_msg:  # message-level checks once per status message
        seen_msg.add(msg_key)
        if is_placeholder(r["transport_number"]):
            F("TS_TRANSPORTNO_PLACEHOLDER", r["transport_number"])
        if not plausible_plate(r["plate"]):
            F("TS_PLATE_INVALID", r["plate"])
        if not valid_geo(r["geo"]):
            F("TS_GEO_INVALID", r["geo"])
    if is_placeholder(r["to_number"]):
        F("TS_TO_MISSING", r["to_number"])
    elif r["plant_code"] in ctx["plants_t1"] and r["to_number"] not in ctx["to_t1"]:
        F("TS_TO_UNKNOWN", r["to_number"])
    if is_placeholder(r["shipment_number"]):
        # delivery note is only the fallback key (matching step 3): just info when the TO number is there
        sev = "info" if not is_placeholder(r["to_number"]) else None
        out.append(finding("TS_SHIPMENTNO_PLACEHOLDER", "4945", k, sup, car, r["shipment_number"],
                           r["source"], sev, r["geo_date"]))
    if not r["timeslot_id"]:
        # the timeslot is needed for gate entry: hard error on arrival, softer en route, n/a for cross-dock legs
        sev = {"MAT-ARRIVAL": "error", "MAT-XDOCK": None}.get(r["process"], "warning")
        if sev:
            out.append(finding("TS_TIMESLOT_MISSING", "4945", k, sup, car, r["process"], r["source"], sev, r["geo_date"]))
    elif not TIMESLOT_RE.match(r["timeslot_id"]):
        F("TS_TIMESLOT_FORMAT", r["timeslot_id"])
    return out


def check_4987(r, ctx):
    """One despatch advice. Also sets r["_to_bad"] (TO number unusable for matching) for the detective."""
    out = []
    k = r["shipment_number"] or r["asn_number"] or r["source"]
    sup, car = r["supplier_code"], r["carrier_code"]
    F = lambda rule, v="": out.append(finding(rule, "4987", k, sup, car, v, r["source"], date=r["despatch_date"]))
    r["_to_bad"] = asn_to_bad(r, ctx["to_t1"], ctx["to_ts"])
    to = r["to_number"]
    # an unknown number is only "wrong" if we could have known it: we have this supplier's bookings
    unverified = (r["_to_bad"] and not is_placeholder(to) and to not in (r["plant_code"], r["shipment_number"])
                  and norm_code(sup) not in ctx["suppliers_t1"] and (sup, to) not in ctx.get("reused_to", ()))
    if is_placeholder(to):
        F("ASN_TO_MISSING")
    elif unverified:
        F("ASN_TO_UNVERIFIED", to)
    elif r["_to_bad"]:
        F("ASN_TO_IMPLAUSIBLE", to)
    if is_placeholder(r["shipment_number"]):
        F("ASN_SHIPMENTNO_MISSING", r["shipment_number"])
    elif r["_to_bad"] and not unverified and r["shipment_number"] not in ctx["ship_ts"]:
        # fallback (matching step 3) is only needed when the TO number is unusable
        F("ASN_NO_STATUS", r["shipment_number"])
    if not r["_items"]:
        F("ASN_NO_ITEMS")
    for it in r["_items"]:
        q = parse_qty(it["quantity"])
        if q is None or q <= 0:
            F("ASN_QTY_INVALID", f'{it["part_number"]}={it["quantity"]}')
            break
    if not r["despatch_date"]:
        F("ASN_DATE_INVALID", f'{r["despatch_raw"]} (fmt {r["despatch_format"]})')
    elif r["arrival_date"] and r["arrival_date"] < r["despatch_date"]:
        F("ASN_DATE_ORDER", f'{r["despatch_date"]} -> {r["arrival_date"]}')
    if not DUNS_RE.match(r["supplier_duns"]):
        F("ASN_DUNS_FORMAT", r["supplier_duns"])
    if r["_has_bad_encoding"]:
        F("ASN_ENCODING")
    return out


def run(source, progress=None):
    """Parse everything under `source` and return dict of tables (lists of dicts)."""
    t1, ts, asn, errors = [], [], [], []
    bad_json = 0
    for i, (t, src, text) in enumerate(iter_messages(source)):
        if progress and i % 2000 == 0:
            progress(i)
        if t == "error":
            errors.append({"source": src, "text": text.strip().splitlines()[0] if text.strip() else ""})
            continue
        try:
            d = json.loads(text)
        except json.JSONDecodeError:
            bad_json += 1
            continue
        if t == "4933":
            t1.append(flatten_4933(src, d))
        elif t == "4945":
            ts.extend(flatten_4945(src, d))
        else:
            asn.append(flatten_4987(src, d))
            asn[-1]["_raw"] = json.dumps(d, ensure_ascii=False, separators=(",", ":"))

    ctx = context(t1, ts, asn)
    findings = []
    for r in t1:
        findings += check_4933(r, ctx)
    seen_msg = set()
    for r in ts:
        findings += check_4945(r, ctx, seen_msg)
    for r in asn:
        findings += check_4987(r, ctx)

    # --- conversion failures: map DUC.<X>KEY prefix to the supplier that uses it
    prefix_sup = defaultdict(Counter)
    for r in asn:
        if r["key_prefix"]:
            prefix_sup[r["key_prefix"]][r["supplier_code"]] += 1
    for e in errors:
        pre = _key_prefix(os.path.basename(e["source"].split(":")[-1]))
        sup = prefix_sup[pre].most_common(1)[0][0] if prefix_sup.get(pre) else ""
        findings.append(finding("ASN_CONVERSION_FAILED", "4987", os.path.basename(e["source"]), sup, "", e["text"], e["source"]))

    rules = [{"rule_id": k, "msg_type": v[0], "severity": v[1], "category": CATEGORY[k],
              "field_path": v[2], "problem": v[3], "fix": v[4],
              "plain_title": PLAIN[k][0], "who_fixes": PLAIN[k][1], "impact": PLAIN[k][2]}
             for k, v in RULES.items()]
    strip = lambda rows: [{k: v for k, v in r.items() if not k.startswith("_")} for r in rows]
    suggestions = [s_ for name in DATE_WINDOWS for s_ in suggest_fixes(t1, ts, asn, name)]
    scores = {"supplier": score({"4933": t1, "4987": asn}, findings, "supplier_code", "supplier_name", "supplier"),
              "carrier": score({"4945": ts}, findings, "carrier_code", "carrier_name", "carrier")}
    warns = warnings(findings, {"supplier": {"4933": t1, "4987": asn}, "carrier": {"4945": ts}}, scores, suggestions, ts)
    return {
        "warnings": warns,
        "warning_overview": warning_overview(warns),
        "alarms": alarms(findings, t1, ts, asn, scores, suggestions),
        "fix_suggestions": suggestions,
        "fix_suggestions_carrier": suggest_carrier_fixes(ts, asn, suggestions),
        "deliveries":deliveries(asn, ts, suggestions, findings),
        # the delivery note exactly as received: shown in the cockpit and pre-filled into "try a fix"
        "msg_raw_4987": [{"supplier_code": a["supplier_code"], "message_key": a["shipment_number"] or a["asn_number"] or a["source"],
                          "source": a["source"], "raw_json": a.get("_raw", "")} for a in asn],
        "detective_quality": backtest(t1, ts, asn),
        # part lists with quantities, used to show the evidence behind a suggestion
        "items_4933": [{"to_number": r["to_number"], "part_number": p["part_number"], "quantity": p["quantity"]}
                       for r in t1 for p in r["_parts"]],
        "items_4987": [{"source": r["source"], "part_number": i["part_number"], "quantity": i["quantity"]}
                       for r in asn for i in r["_items"]],
        "msg_4933": strip(t1),
        "msg_4945": ts,
        "msg_4987": strip(asn),
        "findings": findings,
        "rules": rules,
        "scores_supplier": scores["supplier"],
        "scores_carrier": scores["carrier"],
        "_stats": {"bad_json": bad_json, "conversion_errors": len(errors)},
    }


def asn_to_bad(r, to_t1, to_ts):
    """Is the ASN's transport order number missing or unusable for matching step 2?"""
    to = r["to_number"]
    return is_placeholder(to) or to == r["plant_code"] or (to not in to_t1 and to not in to_ts)


def _date(v):
    try:
        return date.fromisoformat(s(v)[:10])
    except ValueError:
        return None


ASN_TO_FIELD = "/references/rff_AAO_transportOrderNumber/value"


# Measured on the hackathon data (ASN<->TO pairs confirmed independently by a 4945 status): almost all
# pick-ups on the despatch day, a few one day later, none outside. Window = allowed (pick-up date - despatch date) in days.
DATE_WINDOWS = {"same_day": (0, 0), "next_day": (0, 1)}


def _amounts(lines):
    """part number -> total amount over all lines (a part can appear in several packages)."""
    out = defaultdict(float)
    for x in lines:
        if x.get("part_number"):
            out[x["part_number"]] += parse_qty(x.get("quantity")) or 0.0
    return dict(out)


def suggest_fixes(t1, ts, asn, window="next_day"):
    """The 'detective': for every despatch advice (4987) whose transport order number is missing or
    implausible, find the most likely VDA4933 T1 transport order and propose it as a fix.

    Evidence, following the VDA matching sequence:
      * same supplier, requested pick-up inside DATE_WINDOWS[window] relative to the despatch date
      * share of the ASN's part numbers that appear in the transport order
      * a transport status (4945) that reports the same supplier + delivery note together with a TO number
      * tie-breaker: share of parts with exactly the same amount in TO and delivery note
        (measured: clearly better yellow best-match accuracy; carrier and time of day added nothing on top)
    Confidence:
      high   -> all parts in exactly one TO (not contradicted by a status), or several TOs contain all parts but
                exactly one also has the same amounts (back-test: all correct), or status-confirmed with >=50 % parts
      medium -> >=50 % of parts, several full candidates, or status confirmation only
      (anything weaker gets no suggestion)
    """
    by_sup = defaultdict(list)
    for o in t1:
        if o["to_number"]:
            by_sup[o["supplier_code"]].append((o, _amounts(o["_parts"]), _date(o["requested_shipment_date"])))
    lo, hi = DATE_WINDOWS[window]
    known_to = {o["to_number"] for o in t1}
    via_ts = defaultdict(lambda: defaultdict(set))  # (supplier, delivery note) -> TO -> plates
    for r in ts:
        if not is_placeholder(r["shipment_number"]) and not is_placeholder(r["to_number"]):
            via_ts[(r["supplier_code"], r["shipment_number"])][r["to_number"]].add(r["plate"])

    out = []
    for a in asn:
        if not a.get("_to_bad"):
            continue
        amounts = _amounts(a["_items"])
        parts = set(amounts)
        d = _date(a["despatch_date"])
        confirmed = {} if is_placeholder(a["shipment_number"]) else via_ts.get((a["supplier_code"], a["shipment_number"]), {})

        cands = {}
        for o, oparts, od in by_sup.get(a["supplier_code"], []):
            if d and od and lo <= (od - d).days <= hi:
                c = cands.setdefault(o["to_number"], {"to": o["to_number"], "matched": 0, "same_qty": 0, "day_diff": None})
                c["matched"] = max(c["matched"], len(parts & set(oparts)))
                c["same_qty"] = max(c["same_qty"], sum(1 for p in parts if p in oparts and abs(amounts[p] - oparts[p]) < 1e-6))
                c["day_diff"] = (od - d).days
        for to, plates in confirmed.items():
            if to in (a["shipment_number"], a["plant_code"]):
                continue  # carrier copied the delivery note / plant into the TO field - not evidence
            c = cands.setdefault(to, {"to": to, "matched": 0, "same_qty": 0, "day_diff": None, "unknown_parts": to not in known_to})
            c["confirmed"] = sorted(p for p in plates if p)
        if not cands:
            continue

        def overlap(c):
            return c["matched"] / len(parts) if parts else 0.0

        def strength(c):
            date_bonus = 0.2 * (1 - abs(c["day_diff"]) / (hi + 1)) if c["day_diff"] is not None else 0
            return min(1.0, 0.6 * overlap(c) + date_bonus + (0.3 if "confirmed" in c else 0))

        ranked = sorted(cands.values(), key=lambda c: (round(strength(c), 6), c["same_qty"]), reverse=True)
        best, ov = ranked[0], overlap(ranked[0])
        full = [c for c in ranked if parts and c["matched"] == len(parts)]
        full_same_qty = [c for c in full if c["same_qty"] == len(parts)]
        contradicted = bool(confirmed) and best["to"] not in confirmed
        decided_by_amounts = len(full) > 1 and len(full_same_qty) == 1 and full_same_qty[0] is best
        if ((ov >= 0.99 and len(full) == 1 and not contradicted)
                or (decided_by_amounts and not contradicted)
                or ("confirmed" in best and ov >= 0.5)):
            level = "high"
        elif ov >= 0.5 or ("confirmed" in best and not best.get("unknown_parts")):
            level = "medium"  # a status-only hint to a TO we have never seen is not verifiable -> no suggestion
        else:
            continue

        why = []
        if best.get("unknown_parts"):
            why.append(f"transport order {best['to']} is not in the received 4933 data, so parts could not be compared")
        elif parts:
            why.append(f"{best['matched']}/{len(parts)} parts of the delivery note are in transport order {best['to']}")
        if best["day_diff"] is not None:
            why.append("same day" if best["day_diff"] == 0 else f"pick-up {abs(best['day_diff'])} day(s) "
                       + ("after" if best["day_diff"] > 0 else "before") + " despatch")
        if "confirmed" in best:
            plate = f" (truck {best['confirmed'][0]})" if best["confirmed"] else ""
            why.append(f"a transport status{plate} reports delivery note {a['shipment_number']} with this order")
        if decided_by_amounts:
            why.append(f"{len(full)} orders contain all parts, but only {best['to']} has exactly the same amounts")
        elif len(full) > 1:
            why.append(f"{len(full)} orders contain all parts - " + ("same amounts and closest date chosen"
                       if best["same_qty"] == len(parts) else "closest date chosen"))
        if contradicted:
            why.append(f"note: truck status points to {', '.join(sorted(confirmed))}")
        alts = [f"{c['to']} ({c['matched']}/{len(parts)} parts)" for c in ranked[1:4] if parts and overlap(c) >= 0.5]

        out.append({
            "suggestion_id": hashlib.sha1(f'{a["source"]}|{window}'.encode()).hexdigest()[:12],
            "date_window": window,
            "supplier_code": a["supplier_code"],
            "supplier_name": a["supplier_name"],
            "message_key": a["shipment_number"] or a["asn_number"] or a["source"],  # = findings.message_key
            "asn_number": a["asn_number"],
            "delivery_note": a["shipment_number"],
            "despatch_date": a["despatch_date"],
            "rule_id": "ASN_TO_MISSING" if is_placeholder(a["to_number"]) else "ASN_TO_IMPLAUSIBLE",
            "field_path": ASN_TO_FIELD,
            "current_value": a["to_number"],
            "suggested_value": best["to"],
            "confidence": level,
            "confidence_score": round(strength(best), 2),
            "parts_matched": best["matched"],
            "parts_total": len(parts),
            "status_confirmed": "confirmed" in best,
            "n_candidates": len(ranked),
            "alternatives": "; ".join(alts),
            "reason": "; ".join(why),
            "source": a["source"],
        })
    return out


TS_SHIPMENTNO_FIELD = "/shipmentStatus/shipmentNumberAssignedByShipFrom/referenceNumber"


def suggest_carrier_fixes(ts, asn, suggestions, window="next_day"):
    """The detective, carrier side: for every truck status (4945) that sends a placeholder instead of the
    delivery note number but does name a transport order, find the delivery note that travels with that order.

    Evidence for a delivery note, strongest first:
      * the delivery note (4987) itself names this transport order               -> "direct"
      * another truck status of the same transport order sends the real number    -> "status"
      * the supplier-side detective (suggest_fixes) matched the delivery note to this order -> "detective"
        (in the June data this is the only bridge: the delivery notes behind these orders lack the order number)
    Confidence:
      high   -> exactly one delivery note fits, backed by direct / status evidence or a 'high' detective match
      medium -> several delivery notes fit, or only a 'medium' detective match (a human must choose)
    """
    cands_by_to = defaultdict(dict)  # (supplier, TO) -> delivery note -> evidence

    def add(sup, to, dn, kind, score, despatch="", note=""):
        if is_placeholder(dn) or dn == to:
            return
        e = cands_by_to[(sup, to)].setdefault(dn, {"kinds": set(), "score": 0.0, "despatch": despatch, "notes": []})
        e["kinds"].add(kind)
        e["score"] = max(e["score"], score)
        e["despatch"] = e["despatch"] or despatch
        if note:
            e["notes"].append(note)

    for a in asn:
        if not a.get("_to_bad") and not is_placeholder(a["to_number"]):
            add(a["supplier_code"], a["to_number"], a["shipment_number"], "direct", 1.0, a["despatch_date"])
    for r in ts:
        if not is_placeholder(r["shipment_number"]) and not is_placeholder(r["to_number"]):
            add(r["supplier_code"], r["to_number"], r["shipment_number"], "status", 0.9)
    for s_ in suggestions:
        if s_["date_window"] == window and s_["delivery_note"]:
            add(s_["supplier_code"], s_["suggested_value"], s_["delivery_note"], "detective_" + s_["confidence"],
                0.8 * float(s_["confidence_score"] or 0), s_["despatch_date"],
                f"matched by the detective ({s_['parts_matched']}/{s_['parts_total']} parts, {s_['confidence']} confidence)")

    out = []
    for r in ts:
        if not is_placeholder(r["shipment_number"]) or is_placeholder(r["to_number"]):
            continue
        cands = cands_by_to.get((r["supplier_code"], r["to_number"]))
        if not cands:
            continue
        geo = _date(r["geo_date"])

        def rank(item):
            dn, e = item
            d = _date(e["despatch"])
            gap = abs((geo - d).days) if geo and d else 99
            return (-e["score"], gap, dn)

        ranked = sorted(cands.items(), key=rank)
        best_dn, best = ranked[0]
        strong = best["kinds"] & {"direct", "status", "detective_high"}
        level = "high" if len(ranked) == 1 and strong else "medium"

        why = []
        if "direct" in best["kinds"]:
            why.append(f"delivery note {best_dn} names transport order {r['to_number']}")
        if "status" in best["kinds"]:
            why.append(f"another truck status of transport order {r['to_number']} sends delivery note {best_dn}")
        if best["notes"]:
            why.append(f"delivery note {best_dn} {best['notes'][0]} to transport order {r['to_number']}")
        if len(ranked) == 1:
            why.append("it is the only delivery note found for this transport order")
        else:
            why.append(f"{len(ranked)} delivery notes travel with this transport order - best match chosen")
        alts = [f"{dn} ({'; '.join(sorted(e['kinds']))})" for dn, e in ranked[1:6]]

        out.append({
            "suggestion_id": hashlib.sha1(f'{r["source"]}|{r["msg_index"]}|{r["shipment_index"]}'.encode()).hexdigest()[:12],
            "date_window": window,
            "carrier_code": r["carrier_code"],
            "carrier_name": r["carrier_name"],
            "supplier_code": r["supplier_code"],
            "message_key": f'{r["document_id"]}#{r["shipment_index"]}',  # = findings.message_key
            "document_id": r["document_id"],
            "to_number": r["to_number"],
            "plate": r["plate"],
            "geo_date": r["geo_date"],
            "rule_id": "TS_SHIPMENTNO_PLACEHOLDER",
            "field_path": TS_SHIPMENTNO_FIELD,
            "current_value": r["shipment_number"],
            "suggested_value": best_dn,
            "confidence": level,
            "confidence_score": round(min(1.0, best["score"]), 2),
            "n_candidates": len(ranked),
            "alternatives": "; ".join(alts),
            "reason": "; ".join(why),
            "source": r["source"],
        })
    return out


def deliveries(asn, ts, suggestions, findings=(), window="next_day"):
    """'Your deliveries at the gate': one row per delivery note (VDA 4987) as the plant sees it.

    link_status: linked            - carries a usable transport order number
                 suggestion_high   - number missing, the detective is sure which booking it is
                 suggestion_medium - number missing, a human must choose
                 not_linkable      - number missing and no suggestion possible
    Truck data comes from the transport status (4945): via the TO number if usable, else via the delivery note.
    problems: this note's rule ids from `findings`, most severe first, "|"-separated (join with `rules` for titles).
    (Fixes approved in the cockpit live in fix_log and are combined with this table in the app.)
    """
    by_to, by_dn = defaultdict(list), defaultdict(list)
    for r in ts:
        if not is_placeholder(r["to_number"]):
            by_to[r["to_number"]].append(r)
        if not is_placeholder(r["shipment_number"]):
            by_dn[(r["supplier_code"], r["shipment_number"])].append(r)
    sug = {(s_["supplier_code"], s_["message_key"]): s_ for s_ in suggestions if s_["date_window"] == window}
    order = {"error": 0, "warning": 1, "info": 2}
    probs = defaultdict(dict)  # (supplier, note) -> rule -> severity
    for f in findings:
        if f["msg_type"] == "4987":
            probs[(f["supplier_code"], f["message_key"])][f["rule_id"]] = f["severity"]
    out = []
    for a in asn:
        key = a["shipment_number"] or a["asn_number"] or a["source"]
        s_ = sug.get((a["supplier_code"], key))
        if not a.get("_to_bad"):
            status, booking = "linked", a["to_number"]
        elif s_:
            status, booking = f"suggestion_{s_['confidence']}", s_["suggested_value"]
        else:
            status, booking = "not_linkable", ""
        trucks = by_to.get(booking, []) if booking else []
        if not trucks and not is_placeholder(a["shipment_number"]):
            trucks = by_dn.get((a["supplier_code"], a["shipment_number"]), [])
        plates = sorted({t["plate"] for t in trucks if plausible_plate(t["plate"])})
        slots = sorted({t["timeslot_id"] for t in trucks if TIMESLOT_RE.match(t["timeslot_id"] or "")})
        out.append({
            "supplier_code": a["supplier_code"], "message_key": key, "delivery_note": a["shipment_number"],
            "despatch_date": a["despatch_date"], "carrier_name": display_name(a["carrier_name"], "carrier", a["carrier_code"]),
            "n_items": a["n_items"], "link_status": status, "booking": booking,
            "truck_plate": plates[0] if plates else "", "timeslot_id": slots[0] if slots else "",
            "truck_arrived": any(t["process"] == "MAT-ARRIVAL" for t in trucks),
            "n_status_messages": len(trucks), "source": a["source"],
            "problems": "|".join(sorted(probs.get((a["supplier_code"], key), {}),
                                        key=lambda r_: (order.get(probs[(a["supplier_code"], key)][r_], 3), r_))),
        })
    return out


def backtest(t1, ts, asn):
    """How often is the detective right? Ground truth: delivery notes for which a truck status (4945)
    names the transport order. The detective is run WITHOUT the 4945 evidence and compared to it."""
    to_t1 = {r["to_number"] for r in t1}
    truth = defaultdict(set)
    for r in ts:
        if not is_placeholder(r["shipment_number"]) and r["to_number"] in to_t1:
            truth[(r["supplier_code"], r["shipment_number"])].add(r["to_number"])
    out = []
    for window in DATE_WINDOWS:
        res = Counter()
        for s_ in suggest_fixes(t1, [], asn, window):
            t = truth.get((s_["supplier_code"], s_["delivery_note"]))
            if t:
                res[(s_["confidence"], "n")] += 1
                res[(s_["confidence"], "ok")] += s_["suggested_value"] in t
        for conf in ("high", "medium"):
            n, ok = res[(conf, "n")], res[(conf, "ok")]
            out.append({"date_window": window, "confidence": conf, "verifiable": n, "correct": ok,
                        "accuracy_pct": round(100 * ok / n, 1) if n else None})
    return out


def display_name(name, role, code):
    """Anonymised names like 'Fiktiver Lieferant 3805 GmbH' ('fictional supplier') confuse users,
    especially for carriers. Turn them into 'Carrier 3805' / 'Supplier 3805'."""
    name = s(name)
    m = re.match(r"Fiktiver Lieferant (\d+)", name)
    if m:
        return f"{role.capitalize()} {m.group(1)}"
    return name or f"{role.capitalize()} {code}"


WARNING_WEIGHT = 0.5  # a message with only warnings in a category counts half as bad as one with an error


def category_score(total, err_msgs, warn_msgs):
    """0-100: share of messages without problems in a category; warning-only messages count half."""
    if not total:
        return 0.0
    penalty = len(err_msgs) + WARNING_WEIGHT * len(set(warn_msgs) - set(err_msgs))
    return round(100 * max(0.0, total - penalty) / total, 1)


def score(sent, findings, key, name_key, role):
    """Per partner, over the messages that partner SENT: share without errors and top issues."""
    msgs = defaultdict(lambda: Counter())
    names = {}
    for typ, rows in sent.items():
        for r in rows:
            p = r.get(key, "")
            msgs[p][typ] += 1
            if r.get(name_key):
                names.setdefault(p, r[name_key])
    err_keys = defaultdict(set)
    cat_err = defaultdict(lambda: defaultdict(set))   # partner -> category -> messages with an error
    cat_warn = defaultdict(lambda: defaultdict(set))  # partner -> category -> messages with a warning
    rule_counts = defaultdict(Counter)
    sev_counts = defaultdict(Counter)
    for f in findings:
        if f["responsible"] != role:
            continue
        p = f[key]
        rule_counts[p][f["rule_id"]] += 1
        sev_counts[p][f["severity"]] += 1
        mk = (f["msg_type"], f["message_key"])
        if f["severity"] == "error":
            err_keys[p].add(mk)
            cat_err[p][f["category"]].add(mk)
        elif f["severity"] == "warning":
            cat_warn[p][f["category"]].add(mk)
    out = []
    for p, c in msgs.items():
        if not p:
            continue
        total = sum(c.values())
        bad = len(err_keys[p])
        cats = {f"score_{cat}": category_score(total, cat_err[p][cat], cat_warn[p][cat]) for cat in CATEGORIES}
        out.append({
            key: p, "name": names.get(p, ""), "display_name": display_name(names.get(p, ""), role, p),
            "n_4933": c["4933"], "n_4945": c["4945"], "n_4987": c["4987"], "n_messages": total,
            "n_with_errors": bad,
            "quality_score": round(sum(cats.values()) / len(cats), 1),
            **cats,
            "error_free_pct": round(100 * max(0, total - bad) / total, 1),
            "n_errors": sev_counts[p]["error"], "n_warnings": sev_counts[p]["warning"], "n_info": sev_counts[p]["info"],
            "top_issues": ", ".join(f"{r}({n})" for r, n in rule_counts[p].most_common(3)),
        })
    out.sort(key=lambda r: (r["quality_score"], -r["n_messages"]))
    return out


# ---------------------------------------------------------------- warning system: what must be fixed first

# Errors that make the truck stop at the gate: the plant can't link it to a booking / delivery note, or it has no usable slot.
GATE_BLOCKING = {"ASN_TO_MISSING", "ASN_TO_IMPLAUSIBLE", "ASN_SHIPMENTNO_MISSING", "ASN_NO_ITEMS", "ASN_CONVERSION_FAILED",
                 "T1_TO_MISSING", "TS_TO_MISSING", "TS_TIMESLOT_MISSING", "TS_TIMESLOT_FORMAT"}
# Rules about data that is not there at all (vs. there but wrong)
MISSING_DATA = {"T1_TO_MISSING", "T1_NO_PARTS", "T1_QTY_INVALID", "T1_NO_STATUS", "TS_TRANSPORTNO_PLACEHOLDER",
                "TS_TO_MISSING", "TS_SHIPMENTNO_PLACEHOLDER", "TS_TIMESLOT_MISSING", "TS_PLATE_INVALID", "TS_GEO_INVALID",
                "ASN_TO_MISSING", "ASN_SHIPMENTNO_MISSING", "ASN_NO_ITEMS", "ASN_QTY_INVALID", "ASN_NO_STATUS",
                "ASN_CONVERSION_FAILED"}
LEVELS = ("critical", "high", "medium")
LEVEL_PLAIN = {"critical": "the truck is stopped and checked by hand at the gate",
               "high": "The plant receives wrong or incomplete data",
               "medium": "data is weak or unusual but the delivery still works"}
UNIT = {"4987": ("delivery note", "delivery notes"), "4933": ("transport booking", "transport bookings"),
        "4945": ("shipment on a truck", "shipments on trucks")}
SYSTEMATIC_PCT, FREQUENT_PCT = 90.0, 30.0  # share of the partner's messages of that type
STILL_HAPPENING_DAYS = 3  # seen within this many days of the partner's latest message
UNKNOWN_SENDER = "unknown"


def warning_level(rule, severity):
    """critical / high / medium, or None for info findings (not something that must be fixed)."""
    if severity == "error":
        return "critical" if rule in GATE_BLOCKING else "high"
    return "medium" if severity == "warning" else None


def _sent_unit(typ, r):
    """What a person at the gate counts: a delivery note, a booking, a shipment on a truck - not a GPS ping.
    For 4987 / 4933 this is the message key the checks use; tracking rows are grouped per (booking, delivery note)."""
    if typ == "4987":
        return r["shipment_number"] or r["asn_number"] or r["source"]
    if typ == "4933":
        return r["to_number"] or r["workflow_id"]
    to, dn = r["to_number"], r["shipment_number"]
    return f"{to}|{dn}" if not (is_placeholder(to) and is_placeholder(dn)) else r["document_id"]


def warnings(findings, sent, scores, suggestions=(), ts=(), window="next_day"):
    """The warning system: per supplier / carrier, the problems that must be fixed, most urgent first.

    One row per (partner, rule) over errors and warnings (info findings are left out):
      level        critical (truck stopped at the gate) > high (other error) > medium (warning)
      n_units      how many delivery notes / bookings / shipments are affected (tracking pings are grouped per
                   shipment, so a carrier is not ranked higher just because its truck reports every 20 minutes)
      share_pct    share of the partner's delivery notes / bookings / shipments with the problem;
      pattern      systematic (>= 90 %: almost certainly a setting in their system - one fix solves all),
                   frequent (>= 30 %), occasional
      score_gain   points the partner's quality score rises if this problem is gone (same formula as `score`)
      ready_fixes  affected delivery notes the detective can fill in now (high-confidence suggestions)
      still_happening  last seen within 3 days of the partner's latest message (else maybe already fixed)
    Ranking: level, then affected units, then score gain. `partner_rank` is within the partner, `global_rank` across all.

    sent: {"supplier": {"4933": rows, "4987": rows}, "carrier": {"4945": rows}} - the messages each partner SENT.
    scores: {"supplier": scores_supplier rows, "carrier": scores_carrier rows}.
    """
    key_of = {"supplier": "supplier_code", "carrier": "carrier_code"}
    ts_unit = {f'{r["document_id"]}#{r["shipment_index"]}': _sent_unit("4945", r) for r in ts}
    n_sent, units_sent, latest = defaultdict(Counter), defaultdict(set), {}
    for role, by_type in sent.items():
        for typ, rows in by_type.items():
            for r in rows:
                p = r.get(key_of[role], "")
                n_sent[(role, p)][typ] += 1
                units_sent[(role, p, typ)].add(_sent_unit(typ, r))
    ready = Counter()  # (supplier, delivery note key) with a sure suggestion
    for s_ in suggestions:
        if s_["date_window"] == window and s_["confidence"] == "high":
            ready[(s_["supplier_code"], s_["message_key"])] += 1
    score_row = {(role, r[key_of[role]]): r for role, rows in scores.items() for r in rows}

    # per partner: message -> category -> worst severity per rule, to recompute scores without one rule
    per_partner = defaultdict(lambda: defaultdict(list))  # (role, p) -> msg -> [(rule, category, severity)]
    groups = defaultdict(lambda: {"msgs": set(), "units": set(), "values": Counter(), "dates": [], "ready": 0})
    for f in findings:
        role = f["responsible"]
        p = f[key_of[role]] or UNKNOWN_SENDER  # unreadable messages may have no known sender: keep them visible
        mk = (f["msg_type"], f["message_key"])
        per_partner[(role, p)][mk].append((f["rule_id"], f["category"], f["severity"]))
        if f["msg_date"]:
            latest[(role, p)] = max(latest.get((role, p), ""), f["msg_date"])
        level = warning_level(f["rule_id"], f["severity"])
        if not level:
            continue
        gr = groups[(role, p, f["rule_id"], level)]
        if mk not in gr["msgs"]:
            gr["ready"] += f["msg_type"] == "4987" and ready[(p, f["message_key"])] > 0
        gr["msgs"].add(mk)
        gr["units"].add(ts_unit.get(f["message_key"], f["message_key"]) if f["msg_type"] == "4945" else f["message_key"])
        gr["msg_type"] = f["msg_type"]
        if f["value"]:
            gr["values"][f["value"]] += 1
        if f["msg_date"]:
            gr["dates"].append(f["msg_date"])

    def gain(role, p, rule, level):
        """Quality-score points won if every finding of `rule` (at this level's severity) disappears."""
        total = sum(n_sent[(role, p)].values())
        if not total:
            return 0.0
        before, after = defaultdict(lambda: [set(), set()]), defaultdict(lambda: [set(), set()])
        for mk, items in per_partner[(role, p)].items():
            for r_, cat, sev in items:
                i = {"error": 0, "warning": 1}.get(sev)
                if i is None:
                    continue
                before[cat][i].add(mk)
                if not (r_ == rule and warning_level(r_, sev) == level):
                    after[cat][i].add(mk)
        diff = sum(category_score(total, *after[c]) - category_score(total, *before[c]) for c in CATEGORIES)
        return round(diff / len(CATEGORIES), 1)

    out = []
    for (role, p, rule, level), gr in groups.items():
        typ = gr["msg_type"]
        n_units = len(gr["units"])
        # conversion failures are not in the sent rows (nothing could be read) -> the share falls back to 100 %
        n_total = len(units_sent[(role, p, typ)] | gr["units"])
        share = round(100 * n_units / n_total, 1)
        pattern = "systematic" if share >= SYSTEMATIC_PCT else "frequent" if share >= FREQUENT_PCT else "occasional"
        dates = sorted(gr["dates"])
        last, newest = (dates[-1] if dates else ""), latest.get((role, p), "")
        still = bool(last and newest and (_date(newest) - _date(last)).days <= STILL_HAPPENING_DAYS) if last else True
        title, who, impact = PLAIN[rule]
        sr = score_row.get((role, p), {})
        one, many = UNIT[typ]
        unit = many
        sg = gain(role, p, rule, level)
        pct = f"{share:.0f} %" if share >= 1 else "under 1 %"
        head = f"{n_units} {one if n_units == 1 else many}: {title[0].lower() + title[1:]}"
        head += f" ({pct} of your {many}) - {LEVEL_PLAIN[level]}."
        if pattern == "systematic":
            head += " It happens every time, so it is probably one setting in your system: one fix solves all."
        if gr["ready"]:
            head += f" {gr['ready']} can be fixed now with one click."
        if sg > 0:
            head += f" Fixing it raises your score by {sg:g} points."
        out.append({
            "role": role, "partner_code": p,
            "partner_name": sr.get("display_name") or ("Unknown sender" if p == UNKNOWN_SENDER else display_name("", role, p)),
            "rule_id": rule, "level": level, "kind": "missing" if rule in MISSING_DATA else "wrong",
            "msg_type": typ, "title": title, "who_fixes": who, "impact": impact,
            "field_path": RULES[rule][2], "fix": RULES[rule][4],
            "n_messages": len(gr["msgs"]), "n_units": n_units, "unit": unit, "share_pct": share, "pattern": pattern,
            "score_gain": sg, "quality_score": sr.get("quality_score", ""), "ready_fixes": gr["ready"],
            "first_seen": dates[0] if dates else "", "last_seen": last, "still_happening": still,
            "example_values": ", ".join(v for v, _ in gr["values"].most_common(3)),
            "headline": head,
        })
    order = {lv: i for i, lv in enumerate(LEVELS)}
    out.sort(key=lambda w: (order[w["level"]], -w["n_units"], -w["score_gain"], w["role"], w["partner_code"], w["rule_id"]))
    seen = Counter()
    for i, w in enumerate(out, 1):
        w["global_rank"] = i
        seen[(w["role"], w["partner_code"])] += 1
        w["partner_rank"] = seen[(w["role"], w["partner_code"])]
    return out


def warning_overview(warns):
    """The plant's alarm board: one row per problem across all partners, most urgent first."""
    agg = defaultdict(lambda: {"partners": Counter(), "n_units": 0, "n_messages": 0, "systematic": 0, "ready": 0,
                               "still": 0})
    for w in warns:
        a = agg[(w["level"], w["rule_id"], w["role"])]
        a["partners"][(w["partner_name"], w["partner_code"])] += w["n_units"]  # names can repeat (anonymised)
        a["n_units"] += w["n_units"]
        a["n_messages"] += w["n_messages"]
        a["systematic"] += w["pattern"] == "systematic"
        a["ready"] += w["ready_fixes"]
        a["still"] += w["still_happening"]
        a.update(unit=w["unit"], title=w["title"], who=w["who_fixes"], kind=w["kind"])
    order = {lv: i for i, lv in enumerate(LEVELS)}
    out = []
    for (level, rule, role), a in sorted(agg.items(), key=lambda kv: (order[kv[0][0]], -kv[1]["n_units"])):
        out.append({
            "level": level, "rule_id": rule, "role": role, "kind": a["kind"], "title": a["title"], "who_fixes": a["who"],
            "n_partners": len(a["partners"]), "n_units": a["n_units"], "unit": a["unit"], "n_messages": a["n_messages"],
            "n_partners_systematic": a["systematic"], "n_partners_still_happening": a["still"], "ready_fixes": a["ready"],
            "top_partners": ", ".join(f"{n} [{c}] ({u})" for (n, c), u in a["partners"].most_common(3)),
        })
    for i, r in enumerate(out, 1):
        r["priority"] = i
    return out


# ---------------------------------------------------------------- real-time alarms (deadline per open problem)
# An alarm = an ERROR on a message whose truck has not reached the plant yet. It is "open" on day D when the message
# was known by D (known_from <= D) and the truck is still to come (needed_by >= D); days_left = needed_by - D.
# Red = the truck would be stopped at the gate (critical) and arrives within RED_DAYS days.
# Live mode: D = today. Simulation: any past D - the table holds no per-day data, the filter does the work,
# so new messages raise alarms as soon as the pipeline has read them.
RED_DAYS = 1
DEADLINE_ESTIMATE_DAYS = 1  # delivery note without arrival date: 1 day after despatch (most common lead time)


def _needed_by(typ, r):
    """(date the info is needed at the latest, where that date comes from)."""
    if typ == "4987":
        if _date(r["arrival_date"]):
            return r["arrival_date"], "truck arrival in the delivery note"
        d = _date(r["despatch_date"])
        return (str(d + timedelta(days=DEADLINE_ESTIMATE_DAYS)), "estimated: despatch day + 1") if d else ("", "")
    if typ == "4933":
        for col, basis in (("requested_shipment_date", "truck pick-up in the booking"),
                           ("estimated_arrival_date", "truck arrival in the booking")):
            if _date(r[col]):
                return r[col], basis
        return "", ""
    if _date(r["eta_date"]):
        return r["eta_date"], "truck ETA in the status message"
    return (r["geo_date"], "truck reported at/near the gate") if _date(r["geo_date"]) else ("", "")


def alarms(findings, t1, ts, asn, scores, suggestions=(), window="next_day"):
    """One row per open ERROR per unit (delivery note / booking / shipment on a truck) with its deadline.
    known_from = first message date showing the problem, needed_by = latest truck date of those messages."""
    msg = {("4987", r["source"]): r for r in asn}
    msg.update({("4933", r["source"]): r for r in t1})
    msg.update({("4945", (r["source"], f'{r["document_id"]}#{r["shipment_index"]}')): r for r in ts})
    key_of = {"supplier": "supplier_code", "carrier": "carrier_code"}
    names = {(role, r[key_of[role]]): r.get("display_name", "") for role, rows in scores.items() for r in rows}
    ready = {(s_["supplier_code"], s_["message_key"]) for s_ in suggestions
             if s_["date_window"] == window and s_["confidence"] == "high"}
    groups = {}
    for f in findings:
        if f["severity"] != "error":
            continue
        typ = f["msg_type"]
        r = msg.get((typ, (f["source"], f["message_key"]) if typ == "4945" else f["source"]))
        if r is None:  # unreadable message (conversion failed): no truck date known
            continue
        need, basis = _needed_by(typ, r)
        if not need or not f["msg_date"]:
            continue
        need = max(need, f["msg_date"])  # a truck date before the message itself is a typo: needed at once
        role = f["responsible"]
        p = f[key_of[role]] or UNKNOWN_SENDER
        unit = _sent_unit(typ, r)
        k = (role, p, f["rule_id"], typ, unit)
        g_ = groups.get(k)
        if g_ is None:
            groups[k] = {"known_from": f["msg_date"], "needed_by": need, "basis": basis, "n_messages": 1}
        else:
            g_["known_from"] = min(g_["known_from"], f["msg_date"])
            if need > g_["needed_by"]:
                g_["needed_by"], g_["basis"] = need, basis
            g_["n_messages"] += 1
    out = []
    for (role, p, rule, typ, unit), g_ in groups.items():
        title, who, _ = PLAIN[rule]
        out.append({
            "role": role, "partner_code": p,
            "partner_name": names.get((role, p)) or display_name("", role, p),
            "rule_id": rule, "level": warning_level(rule, "error"), "msg_type": typ, "unit_key": unit,
            "unit": UNIT[typ][0], "title": title, "who_fixes": who, "fix": RULES[rule][4],
            "known_from": g_["known_from"], "needed_by": g_["needed_by"], "deadline_basis": g_["basis"],
            "n_messages": g_["n_messages"],
            "ready_fix": typ == "4987" and (p, unit) in ready,
        })
    out.sort(key=lambda a: (a["needed_by"], LEVELS.index(a["level"]), a["role"], a["partner_code"], a["rule_id"], a["unit_key"]))
    return out


def open_alarms(rows, day):
    """The alarms open on `day` (ISO date) with days_left and colour - what the cockpit shows for that day."""
    out = []
    for a in rows:
        if a["known_from"] <= day <= a["needed_by"]:
            left = (_date(a["needed_by"]) - _date(day)).days
            red = a["level"] == "critical" and left <= RED_DAYS
            out.append(dict(a, days_left=left,
                            colour="red" if red else "orange" if a["level"] == "critical" else "yellow"))
    out.sort(key=lambda a: (a["days_left"], ("red", "orange", "yellow").index(a["colour"])))
    return out


# ---------------------------------------------------------------- instant check of ONE new message

def detect_type(d):
    """Which VDA message is this JSON? Returns '4933', '4945', '4987' or None."""
    if isinstance(d, dict):
        if "VDA4933T1" in d:
            return "4933"
        if "IFTSTA" in d:
            return "4945"
        if "cps_groups" in d or "DESADV" in json.dumps(d.get("_meta", {})) or ("references" in d and "parties" in d):
            return "4987"
    return None


def check_message(text, known, source="instant check"):
    """Check one message the moment it arrives.

    text  : the message as JSON text
    known : what the plant already has, as dict with lists of flattened rows {"t1": [...], "ts": [...]}
            (t1 rows need "_parts" for the booking suggestion; the app rebuilds them from the tables)
    Returns {"type", "sender", "summary", "findings", "suggestion"} - nothing is stored.
    """
    try:
        d = json.loads(text)
    except json.JSONDecodeError as e:
        return {"type": None, "error": f"not valid JSON: {e}"}
    t = detect_type(d)
    if t is None:
        return {"type": None, "error": "not a VDA 4933 / 4945 / 4987 JSON message"}
    ctx = context(known.get("t1", []), known.get("ts", []))
    suggestion = None
    if t == "4933":
        r = flatten_4933(source, d)
        findings = check_4933(r, ctx)
        sender = {"role": "supplier", "code": r["supplier_code"], "name": r["supplier_name"], "duns": r["supplier_duns"]}
        summary = {"transport_order": r["to_number"], "pick_up": f'{r["requested_shipment_date"]} {r["requested_shipment_time"]}',
                   "carrier": r["carrier_name"], "parts": r["n_positions"]}
    elif t == "4945":
        rows = flatten_4945(source, d)
        seen = set()
        findings = [f for r in rows for f in check_4945(r, ctx, seen)]
        r = rows[0] if rows else {}
        sender = {"role": "carrier", "code": r.get("carrier_code", ""), "name": r.get("carrier_name", ""), "duns": r.get("carrier_duns", "")}
        summary = {"status_messages": len({(x["msg_index"]) for x in rows}), "shipments": len(rows),
                   "plate": r.get("plate", ""), "transport_number": r.get("transport_number", ""),
                   "transport_orders": sorted({x["to_number"] for x in rows if x["to_number"]})[:5]}
    else:
        r = flatten_4987(source, d)
        findings = check_4987(r, ctx)
        sender = {"role": "supplier", "code": r["supplier_code"], "name": r["supplier_name"], "duns": r["supplier_duns"]}
        summary = {"delivery_note": r["shipment_number"], "despatched": r["despatch_date"],
                   "transport_order": r["to_number"], "parts": r["n_items"]}
        if r["_to_bad"]:
            found = suggest_fixes(known.get("t1", []), known.get("ts", []), [r], "next_day")
            if found:
                s_ = found[0]
                suggestion = {k: s_[k] for k in ("suggested_value", "confidence", "reason", "alternatives", "field_path")}
    for f in findings:
        f["plain_title"], f["who_fixes"], f["impact"] = PLAIN[f["rule_id"]]
        f["fix"] = RULES[f["rule_id"]][4]
        f["field_path"] = RULES[f["rule_id"]][2]
    order = {"error": 0, "warning": 1, "info": 2}
    findings.sort(key=lambda f: order.get(f["severity"], 3))
    return {"type": f"VDA {t}", "sender": sender, "summary": summary, "findings": findings, "suggestion": suggestion,
            "verdict": "error" if any(f["severity"] == "error" for f in findings)
                       else "warning" if any(f["severity"] == "warning" for f in findings) else "ok"}
