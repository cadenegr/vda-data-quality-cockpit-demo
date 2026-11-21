"""Run: python3 scripts/experiment_tiebreak.py "$PWD"   (needs the data zips in data/)

Experiment: do amounts (a), carrier (b) and time of day (c) make the detective smarter?

Ground truth: delivery notes for which a truck status (4945) names the booking (and that booking is in 4933).
The detective is blind to the truck evidence. We compare which booking each variant ranks first.
"""
import collections
import itertools
import json
import sys

sys.path.insert(0, sys.argv[1])
from vda_dq import core  # noqa: E402
from vda_dq.core import is_placeholder  # noqa: E402

t1, ts, asn = [], [], []
for t, src, text in core.iter_messages(sys.argv[1] + "/data"):
    if t == "error":
        continue
    d = json.loads(text)
    if t == "4933":
        t1.append(core.flatten_4933(src, d))
    elif t == "4945":
        ts.extend(core.flatten_4945(src, d))
    else:
        asn.append(core.flatten_4987(src, d))
ctx = core.context(t1, ts)
for a in asn:
    core.check_4987(a, ctx)

known = {r["to_number"] for r in t1}
truth = collections.defaultdict(set)
for r in ts:
    if not is_placeholder(r["shipment_number"]) and r["to_number"] in known:
        truth[(r["supplier_code"], r["shipment_number"])].add(r["to_number"])


def ckey(code):  # carrier codes: '12345600' (4933) vs '123456' (4987) are the same carrier
    c = (code or "").lstrip("0")
    return c[:-2] if len(c) >= 8 else c


def qty(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def minutes(hhmm):
    try:
        return int(hhmm[:2]) * 60 + int(hhmm[2:4])
    except (TypeError, ValueError):
        return None


by_sup = collections.defaultdict(list)
for o in t1:
    parts = collections.defaultdict(float)
    for p in o["_parts"]:
        if p["part_number"]:
            parts[p["part_number"]] += qty(p["quantity"]) or 0
    by_sup[o["supplier_code"]].append((o, dict(parts), core._date(o["requested_shipment_date"]),
                                       minutes(o["requested_shipment_time"].replace(":", ""))))

cases = []
for a in asn:
    tr = truth.get((a["supplier_code"], a["shipment_number"]))
    if not a["_to_bad"] or not tr:
        continue
    items = collections.defaultdict(float)
    for i in a["_items"]:
        if i["part_number"]:
            items[i["part_number"]] += qty(i["quantity"]) or 0
    d = core._date(a["despatch_date"])
    t_desp = minutes(a["despatch_raw"][8:12]) if a["despatch_format"] == "203" else None
    cands = []
    for o, oparts, od, ot in by_sup.get(a["supplier_code"], []):
        if not (d and od) or not (0 <= (od - d).days <= 1):
            continue
        common = [p for p in items if p in oparts]
        cands.append({
            "to": o["to_number"],
            "overlap": len(common) / len(items) if items else 0.0,
            "full": bool(items) and len(common) == len(items),
            "day": (od - d).days,
            "qty": sum(1 for p in common if abs(items[p] - oparts[p]) < 1e-6) / len(items) if items else 0.0,
            "carrier": (ckey(a["carrier_code"]) == ckey(o["carrier_code"])) if a["carrier_code"] and o["carrier_code"] else None,
            "time": abs(ot - t_desp) if (ot is not None and t_desp is not None and (od - d).days == 0) else None,
        })
    if cands:
        cases.append((a, cands, tr))

base_strength = lambda c: 0.6 * c["overlap"] + 0.2 * (1 - abs(c["day"]) / 2)
CLUES = {
    "a_amounts": lambda c: c["qty"],
    "b_carrier": lambda c: 1 if c["carrier"] else 0,
    "c_time": lambda c: -(c["time"] if c["time"] is not None else 10_000),
}


def pick(cands, clues):
    return max(cands, key=lambda c: (round(base_strength(c), 6),) + tuple(CLUES[k](c) for k in clues))


def level(cands):
    full = [c for c in cands if c["full"]]
    best = max(cands, key=base_strength)
    if best["overlap"] >= 0.99 and len(full) == 1:
        return "high"
    if best["overlap"] >= 0.5:
        return "medium"
    return None


print(f"verifiable cases: {len(cases)}  (carrier known in {sum(any(c['carrier'] is not None for c in cs) for _, cs, _ in cases)}, "
      f"time known in {sum(any(c['time'] is not None for c in cs) for _, cs, _ in cases)})")
for lvl in ("high", "medium"):
    sub = [(a, cs, tr) for a, cs, tr in cases if level(cs) == lvl]
    print(f"\n=== {lvl}: {len(sub)} cases")
    for r in range(0, 4):
        for combo in itertools.combinations(CLUES, r):
            ok = sum(pick(cs, combo)["to"] in tr for _, cs, tr in sub)
            name = " + ".join(combo) or "baseline (today)"
            print(f"  {name:34} {ok:4}/{len(sub)} = {100 * ok / max(len(sub), 1):5.1f}%")

# could a clue turn yellow into green safely? yellow cases where exactly ONE full candidate also matches the clue
print("\n=== upgrade yellow -> green when exactly one fully matching booking also matches the clue")
yellow = [(a, cs, tr) for a, cs, tr in cases if level(cs) == "medium"]
for name, test in (("same carrier", lambda c: c["carrier"] is True),
                   ("same amounts (all)", lambda c: c["qty"] >= 0.99),
                   ("same carrier + same amounts", lambda c: c["carrier"] is True and c["qty"] >= 0.99)):
    up = [(cs, tr) for _, cs, tr in yellow if sum(1 for c in cs if c["full"] and test(c)) == 1]
    ok = sum(next(c for c in cs if c["full"] and test(c))["to"] in tr for cs, tr in up)
    print(f"  {name:30} upgrades {len(up):3} yellow cases, correct {ok}/{len(up)}" + (f" = {100 * ok / len(up):.0f}%" if up else ""))
