"""Unit tests for the VDA quality rules, using small synthetic messages (no real data).

run:  python3 -m unittest discover -s tests -v
"""
import json
import os
import sys
import tempfile
import unittest
import zipfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from vda_dq import core  # noqa: E402


def t1(to="19000001", parts=(("3000000001", "10.000"),), supplier="0001234500", eta_time="06:00:00"):
    groups = [{"despatchedArticle": {"partNumber": p, "despatchedQuantity": {"value": q, "measurementUnitCode": ["PCE"]}}}
              for p, q in parts]
    return {"VDA4933T1": {"advancedShippingNotes": {"advancedShippingNote": {
        "interchangeHeader": {"workflowId": "W1"},
        "beginningOfMessage": {"documentIdentifier": to},
        "shipFrom": {"supplierCode": supplier, "duns": "500000001", "name": "Test Supplier GmbH"},
        "shipTo": {"plantCode": "21", "unloadingPoint": {"locationIdAssignedByBuyer": "UP1"}},
        "forwarder": {"supplierCode": "0050000100", "duns": "516000001", "name": "Test Carrier GmbH"},
        "requestedShipmentDateAndTime": {"date": "2026-06-16", "time": "08:00:00"},
        "estimatedArrivalDateAndTime": {"date": "2026-06-17", "time": eta_time},
        "listOfHandlingUnitGroup": {"handlingUnitGroupWithParts": groups},
    }}}}


def ts(to="19000001", timeslot="990026061500001", transport="TRIP42", plate="IN AB 123", process="MAT-ARRIVAL",
       shipment="500116"):
    return {"IFTSTA": {"statusMessages": {"statusMessage": {
        "beginningOfMessage": {"documentIdentifier": "d1", "processIndicator": process},
        "forwarder": {"supplierCode": "0050000100", "duns": "516000001", "name": "Test Carrier GmbH"},
        "transportNumber": {"referenceNumber": transport},
        "statusPosition": {"geocoordinates": "48.76,11.42"},
        "geoPositionDateAndTime": {"date": "2026-06-16", "time": "17:00:00", "timeZone": "+0000"},
        "transportInformationAndStatus": {"transportInformation": {"transportMeansIdentifier": {"identifier": plate}}},
        "listOfShipmentStatus": {"shipmentStatus": {
            "transportInstructionNumber": {"referenceNumber": to},
            "shipmentNumberAssignedByShipFrom": {"referenceNumber": shipment},
            "accessNumAtDropOff": {"referenceNumber": timeslot},
            "shipFrom": {"supplierCode": "0001234500", "duns": "500000001"},
            "shipTo": {"plantCode": "21"},
        }},
    }}}}


def asn(to="", shipment="500116", qty="23", despatch="20260616"):
    return {"_meta": {"sourceFile": "DUC.ABCKEY.VDA4987(1).AU"},
            "header": {"BGM": {"docId": "A1"}, "dtm_11_despatchDate": {"value": despatch, "format": "102"}},
            "references": {"rff_CRN_shipmentNumber": {"value": shipment},
                           **({"rff_AAO_transportOrderNumber": {"value": to}} if to else {})},
            "parties": {"SF": {"id": "0001234500", "duns": "685500114", "name1": "Test Supplier GmbH"},
                        "ST": {"id": "21"}, "FW": {"id": "0050000100", "duns": "516000001", "name1": "Test Carrier GmbH"}},
            "transport": {"TDT": {"modeCode": "30"}},
            "cps_groups": [{"lin_articles": [{"itemIdentifier": "3000000001",
                                              "qty_12_despatchedQuantity": {"value": qty, "unit": "PCE"}}]}]}


def run_on(messages):
    """messages: list of (filename, dict). Writes them into a temp zip and runs the pipeline."""
    d = tempfile.mkdtemp()
    z = os.path.join(d, "test.zip")
    with zipfile.ZipFile(z, "w") as zf:
        for name, m in messages:
            zf.writestr(name, json.dumps(m))
    return core.run(d)


def rules_of(tables, msg_type=None):
    return {f["rule_id"] for f in tables["findings"] if msg_type in (None, f["msg_type"])}


class HappyPath(unittest.TestCase):
    def test_complete_chain_has_no_errors(self):
        tb = run_on([("x/a_VDA4933.json", t1()), ("x/b_VDA4945.json", ts()), ("x/c_VDA4987.json", asn(to="19000001"))])
        errors = [f for f in tb["findings"] if f["severity"] == "error"]
        self.assertEqual(errors, [])
        self.assertEqual(len(tb["msg_4933"]), 1)
        self.assertEqual(len(tb["msg_4945"]), 1)
        self.assertEqual(len(tb["msg_4987"]), 1)


class ForThePage(unittest.TestCase):
    def test_each_delivery_lists_its_problems_and_keeps_the_raw_message(self):
        tb = run_on([("x/c_VDA4987.json", asn(to="", shipment="DN1"))])
        d = tb["deliveries"][0]
        self.assertEqual(d["problems"].split("|")[0], "ASN_TO_MISSING")  # most severe first
        raw = tb["msg_raw_4987"][0]
        self.assertEqual((raw["message_key"], raw["source"]), (d["message_key"], d["source"]))
        self.assertEqual(core.check_message(raw["raw_json"], {})["type"], "VDA 4987")  # can be re-checked as is


class Rules(unittest.TestCase):
    def test_asn_without_to_number(self):
        tb = run_on([("x/a_VDA4933.json", t1()), ("x/b_VDA4945.json", ts()), ("x/c_VDA4987.json", asn(to=""))])
        self.assertIn("ASN_TO_MISSING", rules_of(tb, "4987"))

    def test_asn_with_plant_code_as_to_number(self):
        tb = run_on([("x/c_VDA4987.json", asn(to="21"))])
        self.assertIn("ASN_TO_IMPLAUSIBLE", rules_of(tb, "4987"))

    def test_unknown_booking_number_is_wrong_only_if_we_know_the_suppliers_bookings(self):
        # we have this supplier's bookings, the number is not one of them -> wrong
        tb = run_on([("x/a_VDA4933.json", t1()), ("x/c_VDA4987.json", asn(to="77777777"))])
        self.assertIn("ASN_TO_IMPLAUSIBLE", rules_of(tb, "4987"))
        # no bookings of this supplier in the data -> can't verify, not an error, no side-effect finding
        tb = run_on([("x/c_VDA4987.json", asn(to="77777777"))])
        found = {(f["rule_id"], f["severity"]) for f in tb["findings"] if f["msg_type"] == "4987"}
        self.assertIn(("ASN_TO_UNVERIFIED", "info"), found)
        self.assertNotIn("ASN_TO_IMPLAUSIBLE", {r for r, _ in found})
        self.assertNotIn("ASN_NO_STATUS", {r for r, _ in found})

    def test_same_booking_number_on_notes_days_apart_is_wrong(self):
        tb = run_on([("x/c1_VDA4987.json", asn(to="500298", shipment="DN1", despatch="20260602")),
                     ("x/c2_VDA4987.json", asn(to="500298", shipment="DN2", despatch="20260617"))])
        self.assertIn("ASN_TO_IMPLAUSIBLE", rules_of(tb, "4987"))
        self.assertNotIn("ASN_TO_UNVERIFIED", rules_of(tb, "4987"))
        # the same note sent twice, or two notes on the same truck the next day: still only "can't verify"
        tb = run_on([("x/c1_VDA4987.json", asn(to="500245", shipment="DN1", despatch="20260619")),
                     ("x/c2_VDA4987.json", asn(to="500245", shipment="DN1", despatch="20260622")),
                     ("x/c3_VDA4987.json", asn(to="500300", shipment="DN2", despatch="20260619")),
                     ("x/c4_VDA4987.json", asn(to="500300", shipment="DN3", despatch="20260620"))])
        self.assertEqual({"ASN_TO_UNVERIFIED"}, rules_of(tb, "4987") & {"ASN_TO_UNVERIFIED", "ASN_TO_IMPLAUSIBLE"})

    def test_own_delivery_note_number_as_booking_is_wrong(self):
        tb = run_on([("x/c_VDA4987.json", asn(to="DN9", shipment="DN9"))])
        self.assertIn("ASN_TO_IMPLAUSIBLE", rules_of(tb, "4987"))

    def test_side_effects_and_cosmetics_are_info(self):
        for rule in ("ASN_NO_STATUS", "ASN_ENCODING", "T1_ENCODING"):
            self.assertEqual(core.RULES[rule][1], "info", rule)

    def test_placeholder_transport_number(self):
        for placeholder in ("UNKNOWN", "FOLGT", "0"):
            tb = run_on([("x/b_VDA4945.json", ts(transport=placeholder))])
            self.assertIn("TS_TRANSPORTNO_PLACEHOLDER", rules_of(tb, "4945"), placeholder)

    def test_timeslot_must_be_15_digits(self):
        tb = run_on([("x/b_VDA4945.json", ts(timeslot="1234567890"))])
        self.assertIn("TS_TIMESLOT_FORMAT", rules_of(tb, "4945"))

    def test_missing_timeslot_severity_depends_on_process(self):
        arrival = run_on([("x/b_VDA4945.json", ts(timeslot="", process="MAT-ARRIVAL"))])["findings"]
        xdock = run_on([("x/b_VDA4945.json", ts(timeslot="", process="MAT-XDOCK"))])["findings"]
        self.assertIn(("TS_TIMESLOT_MISSING", "error"), {(f["rule_id"], f["severity"]) for f in arrival})
        self.assertNotIn("TS_TIMESLOT_MISSING", {f["rule_id"] for f in xdock})

    def test_hash_is_not_a_plate(self):
        self.assertFalse(core.plausible_plate("1f70e096d5fd"))
        self.assertTrue(core.plausible_plate("IN AB 123"))
        self.assertTrue(core.plausible_plate("B-XY 4711"))

    def test_invalid_quantity(self):
        tb = run_on([("x/a_VDA4933.json", t1(parts=(("3000000001", "0"),)))])
        self.assertIn("T1_QTY_INVALID", rules_of(tb, "4933"))

    def test_edi_date_formats(self):
        self.assertEqual(core.edi_date("20260616", "102"), "2026-06-16")
        self.assertEqual(core.edi_date("202606161230", "203"), "2026-06-16")
        self.assertIsNone(core.edi_date("2026061", "102"))

    def test_findings_are_attributed_to_sender(self):
        tb = run_on([("x/b_VDA4945.json", ts(transport="UNKNOWN")), ("x/c_VDA4987.json", asn(to=""))])
        resp = {f["msg_type"]: f["responsible"] for f in tb["findings"]}
        self.assertEqual(resp["4945"], "carrier")
        self.assertEqual(resp["4987"], "supplier")


class Detective(unittest.TestCase):
    """fix suggestions: find the transport order for an ASN without TO number"""

    def suggestions(self, *msgs):
        return {s_["delivery_note"]: s_ for s_ in run_on(list(msgs))["fix_suggestions"]}

    def test_unique_full_part_match_is_high(self):
        sug = self.suggestions(("x/a_VDA4933.json", t1(to="19000001")), ("x/c_VDA4987.json", asn(to="", shipment="DN1")))
        self.assertEqual(sug["DN1"]["suggested_value"], "19000001")
        self.assertEqual(sug["DN1"]["confidence"], "high")

    def test_two_full_candidates_need_approval(self):
        sug = self.suggestions(("x/a_VDA4933.json", t1(to="19000001")), ("x/b_VDA4933.json", t1(to="19000002")),
                               ("x/c_VDA4987.json", asn(to="", shipment="DN1")))
        self.assertEqual(sug["DN1"]["confidence"], "medium")
        self.assertIn("2 orders contain all parts", sug["DN1"]["reason"])

    def test_same_amounts_decide_between_full_candidates(self):
        # both orders contain the part, only 19000002 has the delivery note's amount (23) -> sure
        sug = self.suggestions(("x/a_VDA4933.json", t1(to="19000001", parts=(("3000000001", "10"),))),
                               ("x/b_VDA4933.json", t1(to="19000002", parts=(("3000000001", "23.000"),))),
                               ("x/c_VDA4987.json", asn(to="", shipment="DN1", qty="23")))
        self.assertEqual(sug["DN1"]["suggested_value"], "19000002")
        self.assertEqual(sug["DN1"]["confidence"], "high")
        self.assertIn("same amounts", sug["DN1"]["reason"])

    def test_status_confirmation_is_mentioned(self):
        sug = self.suggestions(("x/a_VDA4933.json", t1(to="19000001")), ("x/b_VDA4945.json", ts(to="19000001", shipment="DN1")),
                               ("x/c_VDA4987.json", asn(to="", shipment="DN1")))
        self.assertTrue(sug["DN1"]["status_confirmed"])
        self.assertIn("IN AB 123", sug["DN1"]["reason"])

    def test_no_suggestion_without_evidence(self):
        other_parts = t1(to="19000001", parts=(("9999999999", "5"),))
        sug = self.suggestions(("x/a_VDA4933.json", other_parts), ("x/c_VDA4987.json", asn(to="", shipment="DN1")))
        self.assertNotIn("DN1", sug)

    def test_delivery_note_copied_into_to_field_is_ignored(self):
        sug = self.suggestions(("x/b_VDA4945.json", ts(to="DN1", shipment="DN1")), ("x/c_VDA4987.json", asn(to="", shipment="DN1")))
        self.assertNotIn("DN1", sug)

    def test_valid_to_number_gets_no_suggestion(self):
        sug = self.suggestions(("x/a_VDA4933.json", t1(to="19000001")), ("x/c_VDA4987.json", asn(to="19000001", shipment="DN1")))
        self.assertEqual(sug, {})

    def test_date_window(self):
        # order picked up on 16 Jun, delivery note despatched 15 Jun -> only the "next_day" window finds it
        found = run_on([("x/a_VDA4933.json", t1(to="19000001")),
                        ("x/c_VDA4987.json", asn(to="", shipment="DN1", despatch="20260615"))])["fix_suggestions"]
        self.assertEqual({s_["date_window"] for s_ in found}, {"next_day"})
        # pick-up BEFORE despatch never happens in the data -> no window accepts it
        none = run_on([("x/a_VDA4933.json", t1(to="19000001")),
                       ("x/c_VDA4987.json", asn(to="", shipment="DN1", despatch="20260617"))])["fix_suggestions"]
        self.assertEqual(none, [])

    def test_carrier_placeholder_gets_delivery_note_via_detective(self):
        # status sends '0' as delivery note; the note behind its transport order lacks the order number,
        # the detective matched it -> the status gets that delivery note
        sug = run_on([("x/a_VDA4933.json", t1(to="19000001")), ("x/b_VDA4945.json", ts(to="19000001", shipment="0")),
                      ("x/c_VDA4987.json", asn(to="", shipment="DN1"))])["fix_suggestions_carrier"]
        self.assertEqual(len(sug), 1)
        self.assertEqual((sug[0]["message_key"], sug[0]["suggested_value"], sug[0]["confidence"]), ("d1#0", "DN1", "high"))
        self.assertEqual(sug[0]["rule_id"], "TS_SHIPMENTNO_PLACEHOLDER")
        self.assertIn("only delivery note", sug[0]["reason"])

    def test_carrier_direct_link_and_several_notes_need_a_choice(self):
        one = run_on([("x/a_VDA4933.json", t1(to="19000001")), ("x/b_VDA4945.json", ts(to="19000001", shipment="0")),
                      ("x/c_VDA4987.json", asn(to="19000001", shipment="DN1"))])["fix_suggestions_carrier"]
        self.assertEqual((one[0]["suggested_value"], one[0]["confidence"]), ("DN1", "high"))
        two = run_on([("x/a_VDA4933.json", t1(to="19000001")), ("x/b_VDA4945.json", ts(to="19000001", shipment="0")),
                      ("x/c_VDA4987.json", asn(to="19000001", shipment="DN1")),
                      ("x/d_VDA4987.json", asn(to="19000001", shipment="DN2"))])["fix_suggestions_carrier"]
        self.assertEqual((two[0]["confidence"], two[0]["n_candidates"]), ("medium", 2))
        self.assertTrue(two[0]["alternatives"].startswith("DN2 "))

    def test_carrier_no_suggestion_without_order_or_evidence(self):
        no_to = run_on([("x/b_VDA4945.json", ts(to="", shipment="0")),
                        ("x/c_VDA4987.json", asn(to="19000001", shipment="DN1"))])["fix_suggestions_carrier"]
        self.assertEqual(no_to, [])
        no_note = run_on([("x/b_VDA4945.json", ts(to="19000001", shipment="0"))])["fix_suggestions_carrier"]
        self.assertEqual(no_note, [])
        real = run_on([("x/b_VDA4945.json", ts(to="19000001", shipment="DN1")),
                       ("x/c_VDA4987.json", asn(to="19000001", shipment="DN1"))])["fix_suggestions_carrier"]
        self.assertEqual(real, [])

    def test_backtest_counts_correct_blind_guess(self):
        tb = run_on([("x/a_VDA4933.json", t1(to="19000001")), ("x/b_VDA4945.json", ts(to="19000001", shipment="DN1")),
                     ("x/c_VDA4987.json", asn(to="", shipment="DN1"))])
        q_ = {(r["date_window"], r["confidence"]): r for r in tb["detective_quality"]}
        self.assertEqual((q_[("next_day", "high")]["verifiable"], q_[("next_day", "high")]["correct"]), (1, 1))

    def test_deliveries_gate_view(self):
        tb = run_on([("x/a_VDA4933.json", t1(to="19000001")), ("x/b_VDA4945.json", ts(to="19000001", shipment="DN1")),
                     ("x/c_VDA4987.json", asn(to="", shipment="DN1")), ("x/d_VDA4987.json", asn(to="19000001", shipment="DN2"))])
        d = {r["delivery_note"]: r for r in tb["deliveries"]}
        self.assertEqual((d["DN1"]["link_status"], d["DN1"]["booking"], d["DN1"]["truck_plate"]),
                         ("suggestion_high", "19000001", "IN AB 123"))
        self.assertEqual(d["DN2"]["link_status"], "linked")
        self.assertTrue(d["DN1"]["truck_arrived"])

    def test_items_tables(self):
        tb = run_on([("x/a_VDA4933.json", t1(to="19000001")), ("x/c_VDA4987.json", asn(to="", shipment="DN1"))])
        self.assertEqual(tb["items_4933"][0]["part_number"], "3000000001")
        self.assertEqual(tb["items_4987"][0]["quantity"], "23")

    def test_display_name(self):
        self.assertEqual(core.display_name("Fiktiver Lieferant 3805 GmbH", "carrier", "1"), "Carrier 3805")
        self.assertEqual(core.display_name("", "supplier", "1234500"), "Supplier 1234500")
        self.assertEqual(core.display_name("Alpha Components 1141 GmbH", "supplier", "1"), "Alpha Components 1141 GmbH")


class InstantCheck(unittest.TestCase):
    """check_message: one new message, checked against what the plant already knows"""

    def known(self):
        tb_t1 = [core.flatten_4933("k", t1(to="19000001"))]
        tb_ts = core.flatten_4945("k", ts(to="19000001", shipment="DN1"))
        return {"t1": tb_t1, "ts": tb_ts}

    def test_detects_types(self):
        self.assertEqual(core.detect_type(t1()), "4933")
        self.assertEqual(core.detect_type(ts()), "4945")
        self.assertEqual(core.detect_type(asn()), "4987")
        self.assertIsNone(core.detect_type({"hello": 1}))

    def test_delivery_note_without_booking_gets_instant_suggestion(self):
        res = core.check_message(json.dumps(asn(to="", shipment="DN1")), self.known())
        self.assertEqual(res["verdict"], "error")
        self.assertEqual(res["sender"]["code"], "1234500")
        self.assertIn("ASN_TO_MISSING", {f["rule_id"] for f in res["findings"]})
        self.assertEqual(res["suggestion"]["suggested_value"], "19000001")
        self.assertTrue(all(f["plain_title"] and f["who_fixes"] for f in res["findings"]))

    def test_good_delivery_note_passes(self):
        res = core.check_message(json.dumps(asn(to="19000001", shipment="DN1")), self.known())
        self.assertEqual(res["verdict"], "ok")
        self.assertIsNone(res["suggestion"])

    def test_corrected_message_from_supplier_without_bookings_is_not_an_error(self):
        # the user fixed their note; we just don't have their booking in our sample
        res = core.check_message(json.dumps(asn(to="19555555", shipment="DN7")), {"t1": [], "ts": []})
        self.assertNotEqual(res["verdict"], "error")
        self.assertIn("ASN_TO_UNVERIFIED", {f["rule_id"] for f in res["findings"]})

    def test_bad_input(self):
        self.assertIn("error", core.check_message("not json", {}))
        self.assertIn("error", core.check_message('{"x": 1}', {}))


class Wording(unittest.TestCase):
    def test_every_rule_has_plain_language_and_owner(self):
        self.assertEqual(set(core.RULES), set(core.PLAIN))
        for rule, (title, who, impact) in core.PLAIN.items():
            self.assertTrue(title and who and impact, rule)


class Scores(unittest.TestCase):
    def test_warnings_count_half(self):
        self.assertEqual(core.category_score(10, set(), set()), 100.0)
        self.assertEqual(core.category_score(10, {1}, set()), 90.0)
        self.assertEqual(core.category_score(10, set(), {1, 2}), 90.0)
        self.assertEqual(core.category_score(10, {1}, {1, 2}), 85.0)  # message 1 counted once, as error

    def test_category_scores(self):
        tb = run_on([("x/a_VDA4933.json", t1()), ("x/b_VDA4945.json", ts()), ("x/c_VDA4987.json", asn(to=""))])
        sup = {r["supplier_code"]: r for r in tb["scores_supplier"]}["1234500"]
        self.assertEqual(sup["n_messages"], 2)
        self.assertEqual(sup["score_completeness"], 100.0)
        self.assertLess(sup["score_matching"], 100.0)  # the ASN cannot be matched
        self.assertTrue(0 <= sup["quality_score"] <= 100)


def ts_doc(doc_id, **kw):
    m = ts(**kw)
    m["IFTSTA"]["statusMessages"]["statusMessage"]["beginningOfMessage"]["documentIdentifier"] = doc_id
    return m


class Warnings(unittest.TestCase):
    def test_levels(self):
        self.assertEqual(core.warning_level("ASN_TO_MISSING", "error"), "critical")      # truck stopped at the gate
        self.assertEqual(core.warning_level("TS_TRANSPORTNO_PLACEHOLDER", "error"), "high")
        self.assertEqual(core.warning_level("TS_TIMESLOT_MISSING", "warning"), "medium")  # en route, not at the gate
        self.assertIsNone(core.warning_level("ASN_NO_STATUS", "info"))                  # info: nothing to fix
        self.assertTrue(core.GATE_BLOCKING <= set(core.RULES) and core.MISSING_DATA <= set(core.RULES))

    def test_missing_booking_is_critical_systematic_and_ready_to_fix(self):
        tb = run_on([("x/a_VDA4933.json", t1(to="19000001")), ("x/c_VDA4987.json", asn(to="", shipment="DN1"))])
        w = [w for w in tb["warnings"] if w["rule_id"] == "ASN_TO_MISSING"][0]
        self.assertEqual((w["level"], w["kind"], w["pattern"], w["partner_rank"]), ("critical", "missing", "systematic", 1))
        self.assertEqual((w["n_units"], w["share_pct"], w["ready_fixes"]), (1, 100.0, 1))  # the detective found it
        self.assertGreater(w["score_gain"], 0)
        self.assertIn("one click", w["headline"])
        self.assertNotIn("ASN_NO_STATUS", {w["rule_id"] for w in tb["warnings"]})  # info stays out
        top = tb["warning_overview"][0]
        self.assertEqual((top["priority"], top["rule_id"], top["n_partners"]), (1, "ASN_TO_MISSING", 1))

    def test_score_gain_equals_rescoring_without_the_problem(self):
        tb = run_on([("x/a_VDA4933.json", t1()), ("x/c_VDA4987.json", asn(to="")), ("x/d_VDA4987.json", asn(to="", shipment="DN2"))])
        sup = tb["scores_supplier"][0]
        w = [w for w in tb["warnings"] if w["rule_id"] == "ASN_TO_MISSING"][0]
        rest = [f for f in tb["findings"] if f["rule_id"] != "ASN_TO_MISSING"]
        after = core.score({"4933": tb["msg_4933"], "4987": tb["msg_4987"]}, rest, "supplier_code", "supplier_name", "supplier")
        self.assertAlmostEqual(w["score_gain"], after[0]["quality_score"] - sup["quality_score"], places=1)

    def test_tracking_pings_count_as_one_shipment(self):
        # two en-route status messages of the same shipment without a gate slot -> one shipment, not two
        tb = run_on([("x/a_VDA4945.json", ts_doc("d1", timeslot="", process="MAT-TRANSPORT")),
                     ("x/b_VDA4945.json", ts_doc("d2", timeslot="", process="MAT-TRANSPORT"))])
        w = [w for w in tb["warnings"] if w["rule_id"] == "TS_TIMESLOT_MISSING"][0]
        self.assertEqual((w["level"], w["n_messages"], w["n_units"], w["role"]), ("medium", 2, 1, "carrier"))

    def test_critical_ranks_before_bigger_warnings(self):
        tb = run_on([("x/a_VDA4945.json", ts_doc("d1", timeslot="", process="MAT-TRANSPORT", shipment="A")),
                     ("x/b_VDA4945.json", ts_doc("d2", timeslot="", process="MAT-TRANSPORT", shipment="B")),
                     ("x/c_VDA4945.json", ts_doc("d3", timeslot="", process="MAT-ARRIVAL", shipment="C"))])
        levels = [(w["level"], w["n_units"]) for w in tb["warnings"] if w["rule_id"] == "TS_TIMESLOT_MISSING"]
        self.assertEqual(levels, [("critical", 1), ("medium", 2)])
        self.assertEqual([w["global_rank"] for w in tb["warnings"]], list(range(1, len(tb["warnings"]) + 1)))


def asn_arr(arrival, **kw):
    m = asn(**kw)
    m["header"]["dtm_132_estimatedArrival"] = {"value": arrival, "format": "102"}
    return m


class Alarms(unittest.TestCase):
    def test_deadline_and_colour_by_day(self):
        # delivery note sent 16 Jun without booking number, truck at the gate 19 Jun
        tb = run_on([("x/c_VDA4987.json", asn_arr("20260619", to="", shipment="DN1", despatch="20260616"))])
        a = [a for a in tb["alarms"] if a["rule_id"] == "ASN_TO_MISSING"][0]
        self.assertEqual((a["known_from"], a["needed_by"], a["level"], a["role"]),
                         ("2026-06-16", "2026-06-19", "critical", "supplier"))
        self.assertEqual(core.open_alarms(tb["alarms"], "2026-06-15"), [])          # not sent yet: system can't know
        self.assertEqual(core.open_alarms(tb["alarms"], "2026-06-20"), [])          # truck already arrived
        day16 = [x for x in core.open_alarms(tb["alarms"], "2026-06-16") if x["rule_id"] == "ASN_TO_MISSING"][0]
        day18 = [x for x in core.open_alarms(tb["alarms"], "2026-06-18") if x["rule_id"] == "ASN_TO_MISSING"][0]
        self.assertEqual((day16["days_left"], day16["colour"]), (3, "orange"))
        self.assertEqual((day18["days_left"], day18["colour"]), (1, "red"))

    def test_no_arrival_date_estimates_next_day_and_errors_only(self):
        tb = run_on([("x/c_VDA4987.json", asn(to="", despatch="20260616"))])
        a = [a for a in tb["alarms"] if a["rule_id"] == "ASN_TO_MISSING"][0]
        self.assertEqual((a["needed_by"], a["deadline_basis"]), ("2026-06-17", "estimated: despatch day + 1"))
        self.assertTrue({a["level"] for a in tb["alarms"]} <= {"critical", "high"})  # errors only, no warnings
        self.assertNotIn("ASN_NO_STATUS", {a["rule_id"] for a in tb["alarms"]})     # info never alarms

    def test_tracking_pings_are_one_alarm_and_not_gate_blocking_is_yellow(self):
        tb = run_on([("x/a_VDA4945.json", ts_doc("d1", transport="0")), ("x/b_VDA4945.json", ts_doc("d2", transport="0"))])
        a = [a for a in tb["alarms"] if a["rule_id"] == "TS_TRANSPORTNO_PLACEHOLDER"]
        self.assertEqual((len(a), a[0]["n_messages"], a[0]["role"]), (1, 2, "carrier"))
        self.assertEqual(core.open_alarms(a, "2026-06-16")[0]["colour"], "yellow")

    def test_fixed_message_raises_no_alarm(self):
        tb = run_on([("x/a_VDA4933.json", t1(to="19000001")),
                     ("x/c_VDA4987.json", asn_arr("20260617", to="19000001", despatch="20260616"))])
        self.assertEqual([a for a in tb["alarms"] if a["msg_type"] == "4987"], [])


if __name__ == "__main__":
    unittest.main()
