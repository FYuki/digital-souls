from copy import deepcopy
from hashlib import sha256
import json
import unittest
from uuid import UUID

import importlib.util
from pathlib import Path
import sys

_ROOT = Path(__file__).resolve().parents[3]
_SPEC = importlib.util.spec_from_file_location("voice_quality_vad_report", _ROOT / "scripts/voice_quality/report_vad.py")
assert _SPEC is not None and _SPEC.loader is not None
report_vad = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = report_vad
_SPEC.loader.exec_module(report_vad)
measure, summarize, main = report_vad.measure, report_vad.summarize, report_vad.main


def fixture():
    return {"audio_sha256": "a" * 64, "cohort": "take_turn", "sample_rate_hz": 48000,
            "expected_utterances": 1, "speech_intervals": [{"start_sample": 4800, "end_sample": 48000}]}


def trial():
    return {"cohort": "take_turn", "fixture_sha256": "a" * 64,
            "session_id": str(UUID(int=1)), "session_end_confirmed": True,
            "fixture_clock_bounds": {
                "sourceStart": {"sourceSample": 0, "lowerMs": 900, "upperMs": 902},
                "speechStart": {"sourceSample": 4800, "lowerMs": 1000, "upperMs": 1002},
                "speechEnd": {"sourceSample": 48000, "lowerMs": 1900, "upperMs": 1902}},
            "evidence": {"vad": {"events": [
                {"type": "candidate", "speechStartedAtMs": 980, "detectedAtMs": 1076},
                {"type": "confirmed", "speechStartedAtMs": 980, "detectedAtMs": 1300},
                {"type": "ended", "speechStartedAtMs": 980, "detectedAtMs": 2600}]}}}


def cohort(count=100):
    fixtures = {"trials": [fixture() for _ in range(count)]}
    fb = json.dumps(fixtures).encode()
    trials = [trial() for _ in range(count)]
    for index, row in enumerate(trials, 1):
        row["session_id"] = str(UUID(int=index))
    manifest = {"measurement_scope": "labeled_livekit_interruption_diagnostic", "cohort": "take_turn",
                "expected_measured": count, "labeled_manifest_sha256": sha256(fb).hexdigest(), "trials": trials}
    return manifest, fb


class ReportTests(unittest.TestCase):
    def test_targeted_selection_is_correlated_and_never_formal(self):
        m, fb = cohort()
        fs = json.loads(fb)
        for index, f in enumerate(fs["trials"]):
            f["audio_sha256"] = sha256(str(index).encode()).hexdigest()
            m["trials"][index]["fixture_sha256"] = f["audio_sha256"]
        fb = json.dumps(fs).encode()
        m["labeled_manifest_sha256"] = sha256(fb).hexdigest()
        m["trials"] = [m["trials"][index-1] for index in (51, 56, 72)]
        m["expected_measured"] = 3
        m["fixture_indices"] = [51, 56, 72]
        report = summarize(json.dumps(m).encode(), fb)
        self.assertEqual(report["measured_trials"], 3)
        self.assertFalse(report["evaluation"]["passed"])
        for invalid in ([51, 51, 72], [0, 56, 72], [True, 56, 72], [51, 56], [51, 56, 101]):
            m["fixture_indices"] = invalid
            with self.assertRaises(ValueError): summarize(json.dumps(m).encode(), fb)
        m, fb = cohort()
        m["fixture_indices"] = list(range(1, 101))
        with self.assertRaises(ValueError): summarize(json.dumps(m).encode(), fb)

    def test_cli_validates_schema_preserves_output_and_omits_private_error(self):
        from contextlib import redirect_stdout
        from io import StringIO
        from pathlib import Path
        from tempfile import TemporaryDirectory
        with TemporaryDirectory() as directory:
            base = Path(directory)
            m, fb = cohort(1)
            (base/"manifest.json").write_text(json.dumps(m))
            (base/"fixtures.json").write_bytes(fb)
            schema = _ROOT / "docs/schemas/voice-quality-vad-report-v1.schema.json"
            argv = ["--manifest", str(base/"manifest.json"), "--fixtures", str(base/"fixtures.json"),
                    "--schema", str(schema), "--output", str(base/"report.json")]
            with redirect_stdout(StringIO()): self.assertEqual(main(argv), 1)
            original = (base/"report.json").read_bytes()
            out = StringIO()
            with redirect_stdout(out): self.assertEqual(main(argv), 2)
            self.assertEqual((base/"report.json").read_bytes(), original)
            (base/"manifest.json").write_text('{"private-sentinel":')
            with redirect_stdout(out): self.assertEqual(main(argv), 2)
            self.assertNotIn("private-sentinel", out.getvalue())
            self.assertNotIn(directory, out.getvalue())
            (base/"manifest.json").write_text(json.dumps(m))
            (base/"bad-schema.json").write_text('{"not": {}}')
            argv[5] = str(base/"bad-schema.json")
            argv[7] = str(base/"rejected-report.json")
            with redirect_stdout(out): self.assertEqual(main(argv), 2)
            self.assertFalse((base/"rejected-report.json").exists())

    def test_normal_bounds(self):
        row = measure(trial(), fixture())
        self.assertEqual(row["onset_offset_lower_ms"], -22)
        self.assertEqual(row["onset_offset_upper_ms"], -20)
        self.assertFalse(row["leading_error"])
        self.assertFalse(row["early_end_error"])

    def test_split_does_not_hide_first_early_end(self):
        t = trial()
        t["evidence"]["vad"]["events"][-1]["detectedAtMs"] = 1600
        t["evidence"]["vad"]["events"] += [
            {"type": "confirmed", "speechStartedAtMs": 1610, "detectedAtMs": 1700},
            {"type": "ended", "speechStartedAtMs": 1610, "detectedAtMs": 2600}]
        row = measure(t, fixture())
        self.assertTrue(row["early_end_error"])
        self.assertTrue(row["split"])
        self.assertEqual(row["end_offset_upper_ms"], -300)

    def test_bounds_crossing_threshold_count_conservatively(self):
        t = trial()
        for e in t["evidence"]["vad"]["events"]:
            e["speechStartedAtMs"] = 1101
        t["evidence"]["vad"]["events"][0]["detectedAtMs"] = 1197
        row = measure(t, fixture())
        self.assertTrue(row["leading_error"])
        self.assertTrue(row["boundary_uncertain"])

    def test_absent_end_is_missing(self):
        t = trial(); t["evidence"]["vad"]["events"].pop()
        self.assertEqual(measure(t, fixture()), {"missing": "speech_end_unavailable"})

    def test_bad_bounds_and_overflow(self):
        t = trial(); t["fixture_clock_bounds"]["speechStart"]["upperMs"] = 1040
        self.assertEqual(measure(t, fixture()), {"missing": "fixture_boundary_unavailable"})
        t = trial(); t["evidence"]["vad"]["events"] *= 43
        self.assertEqual(measure(t, fixture()), {"missing": "detector_event_overflow"})

    def test_explicit_event_overflow_never_becomes_complete(self):
        t = trial(); t["evidence"]["vad"]["eventOverflow"] = True
        self.assertEqual(measure(t, fixture()), {"missing": "detector_event_overflow"})

    def test_prior_input_is_not_a_fixture_onset(self):
        t = trial()
        for e in t["evidence"]["vad"]["events"]:
            e["speechStartedAtMs"] = 700
        self.assertEqual(measure(t, fixture()), {"missing": "detector_utterance_correlation_unavailable"})

    def test_pause_label_must_match_actual_interval(self):
        m, _ = cohort()
        m["cohort"] = "pause"
        fs = {"trials": []}
        for t in m["trials"]:
            t["cohort"] = "pause"
            f = fixture(); f["cohort"] = "pause"; f["pause_samples"] = 9600
            f["speech_intervals"] = [{"start_sample": 4800, "end_sample": 24000},
                                      {"start_sample": 57600, "end_sample": 96000}]
            fs["trials"].append(f)
        # ラベルは200msだが実際の区間は700ms。600ms以内の母集団に混ぜない。
        fb = json.dumps(fs).encode(); m["labeled_manifest_sha256"] = sha256(fb).hexdigest()
        with self.assertRaises(ValueError): summarize(json.dumps(m).encode(), fb)

    def test_missing_and_failed_cases_stay_in_denominator(self):
        m, fb = cohort()
        m["trials"][0]["evidence"]["vad"]["events"] = []
        m["trials"][1]["outcome"] = "failure"
        r = summarize(json.dumps(m).encode(), fb)
        self.assertEqual(r["expected_trials"], 100)
        self.assertEqual(r["measured_trials"], 99)
        self.assertEqual(r["missing"], {"detector_events_unavailable": 1})
        self.assertFalse(r["evaluation"]["passed"])

    def test_private_fields_never_exported(self):
        m, fb = cohort()
        for t in m["trials"]:
            t["transcript"] = "private-sentinel"
            t["evidence"]["vad"]["events"][0]["private"] = "private-sentinel"
        r = summarize(json.dumps(m).encode(), fb)
        serialized = json.dumps(r)
        self.assertTrue(r["evaluation"]["passed"])
        self.assertNotIn("private-sentinel", serialized)
        self.assertNotIn(m["trials"][0]["session_id"], serialized)
        self.assertFalse(r["limits"]["captured_pcm_boundary_verified"])
        self.assertFalse(r["limits"]["all_vad_acceptance_verified"])

    def test_duplicate_session_and_mixed_fixture_rejected(self):
        m, fb = cohort()
        m["trials"][1]["session_id"] = m["trials"][0]["session_id"]
        with self.assertRaises(ValueError): summarize(json.dumps(m).encode(), fb)
        m, fb = cohort(); m["trials"][0]["fixture_sha256"] = "b" * 64
        with self.assertRaises(ValueError): summarize(json.dumps(m).encode(), fb)

    def test_one_percent_gate(self):
        m, fb = cohort()
        for count in (1, 2):
            data = deepcopy(m)
            for t in data["trials"][:count]:
                for e in t["evidence"]["vad"]["events"]:
                    e["speechStartedAtMs"] = 1150
                t["evidence"]["vad"]["events"][0]["detectedAtMs"] = 1246
            r = summarize(json.dumps(data).encode(), fb)
            self.assertEqual(r["evaluation"]["passed"], count == 1)

    def test_pilot_cannot_pass_hundred_gate(self):
        m, fb = cohort(1)
        self.assertFalse(summarize(json.dumps(m).encode(), fb)["evaluation"]["passed"])


if __name__ == "__main__":
    unittest.main()
