from __future__ import annotations

import argparse
from collections.abc import Mapping
from dataclasses import dataclass
import json
import math
from pathlib import Path
import statistics
import sys


RATE_NAMES = (
    "abstain_rate",
    "false_negative_rate",
    "false_positive_rate",
)


@dataclass(frozen=True)
class ExpectedCase:
    case_id: str
    sensitive: bool
    expected_subject_scope: str
    allowed_classifications: frozenset[str]


@dataclass(frozen=True)
class EvaluationResult:
    case_id: str
    profile: str
    classification: str
    subject_scope: str
    latency_seconds: float
    model_digest: str | None
    policy_version: str | None


@dataclass(frozen=True)
class ProfileReport:
    cases: int
    case_failures: int
    failed_case_ids: tuple[str, ...]
    abstain_rate: float
    false_negative_rate: float
    false_positive_rate: float
    latency_median: float
    latency_p95: float


def _object(value: object, name: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{name} must be an object")
    return value


def _string(value: object, name: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{name} must be a non-empty string")
    return value


def _number(value: object, name: str) -> float:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise ValueError(f"{name} must be a number")
    number = float(value)
    if not math.isfinite(number) or number < 0:
        raise ValueError(f"{name} must be a finite non-negative number")
    return number


def load_cases(path: Path) -> dict[str, ExpectedCase]:
    cases: dict[str, ExpectedCase] = {}
    with path.open(encoding="utf-8") as file:
        for line_number, line in enumerate(file, start=1):
            raw = _object(json.loads(line), f"case line {line_number}")
            case_id = _string(raw.get("id"), f"case line {line_number} id")
            variables = _object(raw.get("vars"), f"case {case_id} vars")
            if (
                _string(variables.get("case_id"), f"case {case_id} vars.case_id")
                != case_id
            ):
                raise ValueError(f"case {case_id} vars.case_id does not match id")
            sensitive = raw.get("sensitive")
            if not isinstance(sensitive, bool):
                raise ValueError(f"case {case_id} sensitive must be a boolean")
            allowed = raw.get("allowed_classifications")
            if not isinstance(allowed, list) or not allowed:
                raise ValueError(
                    f"case {case_id} allowed_classifications must be a non-empty array"
                )
            allowed_values = frozenset(
                _string(item, f"case {case_id} allowed classification")
                for item in allowed
            )
            if case_id in cases:
                raise ValueError(f"duplicate case id: {case_id}")
            cases[case_id] = ExpectedCase(
                case_id=case_id,
                sensitive=sensitive,
                expected_subject_scope=_string(
                    raw.get("expected_subject_scope"),
                    f"case {case_id} expected_subject_scope",
                ),
                allowed_classifications=allowed_values,
            )
    if not cases:
        raise ValueError("cases file must not be empty")
    return cases


def _enveloped_result_rows(payload: object) -> list[object]:
    root = _object(payload, "results payload")
    results = _object(root.get("results"), "results payload.results")
    rows = results.get("results")
    if not isinstance(rows, list) or not rows:
        raise ValueError("results payload.results.results must be a non-empty array")
    return rows


def _result_rows(path: Path) -> list[object]:
    if path.suffix == ".jsonl":
        rows = [
            json.loads(line)
            for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        if not rows:
            raise ValueError("JSONL results must not be empty")
        return rows
    return _enveloped_result_rows(json.loads(path.read_text(encoding="utf-8")))


def _latency_seconds(
    output: Mapping[str, object],
    response: Mapping[str, object],
) -> float:
    if "latency_seconds" in output:
        return _number(output["latency_seconds"], "output latency_seconds")
    return _number(response.get("latencyMs"), "response latencyMs") / 1_000


def load_results(path: Path, *, report_only: bool) -> list[EvaluationResult]:
    records: list[EvaluationResult] = []
    seen: set[tuple[str, str]] = set()
    for index, value in enumerate(_result_rows(path)):
        row = _object(value, f"result {index}")
        provider = _object(row.get("provider"), f"result {index} provider")
        label = _string(provider.get("label"), f"result {index} provider label")
        variables = _object(row.get("vars"), f"result {index} vars")
        variable_case_id = _string(
            variables.get("case_id"), f"result {index} vars.case_id"
        )
        response = _object(row.get("response"), f"result {index} response")
        output = _object(
            json.loads(_string(response.get("output"), f"result {index} output")),
            f"result {index} parsed output",
        )

        if report_only:
            case_id = variable_case_id
            profile = label
            model_digest = None
            policy_version = None
        else:
            case_id = _string(output.get("case_id"), f"result {index} case_id")
            profile = _string(output.get("profile"), f"result {index} profile")
            if case_id != variable_case_id:
                raise ValueError(f"result {index} case_id does not match vars")
            if profile != label:
                raise ValueError(
                    f"result {index} profile does not match provider label"
                )
            model_digest = _string(
                output.get("model_digest"), f"result {index} model_digest"
            )
            policy_version = _string(
                output.get("policy_version"), f"result {index} policy_version"
            )

        identity = (profile, case_id)
        if identity in seen:
            raise ValueError(
                f"duplicate result for profile={profile} case_id={case_id}"
            )
        seen.add(identity)
        records.append(
            EvaluationResult(
                case_id=case_id,
                profile=profile,
                classification=_string(
                    output.get("classification"),
                    f"result {index} classification",
                ),
                subject_scope=_string(
                    output.get("subject_scope"),
                    f"result {index} subject_scope",
                ),
                latency_seconds=_latency_seconds(output, response),
                model_digest=model_digest,
                policy_version=policy_version,
            )
        )
    return records


def load_thresholds(path: Path) -> dict[str, dict[str, float]]:
    raw = _object(json.loads(path.read_text(encoding="utf-8")), "thresholds")
    thresholds: dict[str, dict[str, float]] = {}
    for profile, value in raw.items():
        profile_name = _string(profile, "threshold profile")
        rates = _object(value, f"threshold profile {profile_name}")
        if set(rates) != set(RATE_NAMES):
            raise ValueError(
                f"threshold profile {profile_name} must define exactly {RATE_NAMES}"
            )
        profile_thresholds: dict[str, float] = {}
        for name in RATE_NAMES:
            limit = _number(rates[name], f"threshold {profile_name}.{name}")
            if limit > 1:
                raise ValueError(f"threshold {profile_name}.{name} must not exceed 1")
            profile_thresholds[name] = limit
        thresholds[profile_name] = profile_thresholds
    if not thresholds:
        raise ValueError("thresholds must not be empty")
    return thresholds


def _rate(numerator: int, denominator: int) -> float:
    if denominator == 0:
        return 0.0
    return numerator / denominator


def _p95(values: list[float]) -> float:
    ordered = sorted(values)
    return ordered[math.ceil(len(ordered) * 0.95) - 1]


def build_reports(
    records: list[EvaluationResult],
    cases: Mapping[str, ExpectedCase],
) -> dict[str, ProfileReport]:
    grouped: dict[str, list[tuple[EvaluationResult, ExpectedCase]]] = {}
    for record in records:
        try:
            expected = cases[record.case_id]
        except KeyError:
            raise ValueError(f"unknown result case_id: {record.case_id}") from None
        grouped.setdefault(record.profile, []).append((record, expected))

    reports: dict[str, ProfileReport] = {}
    for profile, items in grouped.items():
        sensitive = [item for item in items if item[1].sensitive]
        not_sensitive = [item for item in items if not item[1].sensitive]
        failed_case_ids = tuple(
            record.case_id
            for record, expected in items
            if record.classification not in expected.allowed_classifications
            or (
                record.classification != "ABSTAIN"
                and record.subject_scope != expected.expected_subject_scope
            )
        )
        reports[profile] = ProfileReport(
            cases=len(items),
            case_failures=len(failed_case_ids),
            failed_case_ids=failed_case_ids,
            abstain_rate=_rate(
                sum(record.classification == "ABSTAIN" for record, _ in items),
                len(items),
            ),
            false_negative_rate=_rate(
                sum(
                    record.classification == "NOT_SENSITIVE" for record, _ in sensitive
                ),
                len(sensitive),
            ),
            false_positive_rate=_rate(
                sum(
                    record.classification == "SENSITIVE" for record, _ in not_sensitive
                ),
                len(not_sensitive),
            ),
            latency_median=statistics.median(
                record.latency_seconds for record, _ in items
            ),
            latency_p95=_p95([record.latency_seconds for record, _ in items]),
        )
    return reports


def validate_conformance_context(
    records: list[EvaluationResult],
    reports: Mapping[str, ProfileReport],
    thresholds: Mapping[str, Mapping[str, float]],
    expected_case_ids: frozenset[str],
) -> None:
    if set(reports) != set(thresholds):
        raise ValueError("result profiles do not match threshold profiles")
    cases_by_profile = {
        profile: {record.case_id for record in records if record.profile == profile}
        for profile in reports
    }
    for profile, case_ids in cases_by_profile.items():
        if case_ids != expected_case_ids:
            raise ValueError(
                f"profile {profile} result case ids do not match expected case ids"
            )
    if len({record.model_digest for record in records}) != 1:
        raise ValueError("results do not share one model digest")
    if len({record.policy_version for record in records}) != 1:
        raise ValueError("results do not share one policy version")


def print_reports(reports: Mapping[str, ProfileReport]) -> None:
    for profile in sorted(reports):
        report = reports[profile]
        print(
            f"{profile}: cases={report.cases} "
            f"case_failures={report.case_failures} "
            f"abstain_rate={report.abstain_rate:.6f} "
            f"false_negative_rate={report.false_negative_rate:.6f} "
            f"false_positive_rate={report.false_positive_rate:.6f} "
            f"latency_median={report.latency_median:.6f} "
            f"latency_p95={report.latency_p95:.6f}"
        )


def threshold_failures(
    reports: Mapping[str, ProfileReport],
    thresholds: Mapping[str, Mapping[str, float]],
) -> list[str]:
    failures = []
    for profile, report in reports.items():
        for name in RATE_NAMES:
            observed = getattr(report, name)
            limit = thresholds[profile][name]
            if observed > limit:
                failures.append(f"{profile}.{name}={observed:.6f} exceeds {limit:.6f}")
    return failures


def conformance_failures(
    reports: Mapping[str, ProfileReport],
    thresholds: Mapping[str, Mapping[str, float]],
) -> list[str]:
    failures = [
        f"{profile}.case_failures={report.case_failures} "
        f"case_ids={','.join(report.failed_case_ids)}"
        for profile, report in reports.items()
        if report.case_failures
    ]
    return failures + threshold_failures(reports, thresholds)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--results", type=Path, required=True)
    parser.add_argument("--cases", type=Path, required=True)
    parser.add_argument("--thresholds", type=Path, required=True)
    parser.add_argument("--filter-first-n", type=int)
    parser.add_argument("--report-only", action="store_true")
    return parser.parse_args()


def run(arguments: argparse.Namespace) -> int:
    cases = load_cases(arguments.cases)
    if arguments.filter_first_n is not None and arguments.filter_first_n < 1:
        raise ValueError("filter-first-n must be a positive integer")
    expected_case_ids = frozenset(
        list(cases)[: arguments.filter_first_n]
        if arguments.filter_first_n is not None
        else cases
    )
    thresholds = load_thresholds(arguments.thresholds)
    records = load_results(arguments.results, report_only=arguments.report_only)
    reports = build_reports(records, cases)
    if not arguments.report_only:
        validate_conformance_context(
            records,
            reports,
            thresholds,
            expected_case_ids,
        )
    print_reports(reports)
    if arguments.report_only:
        return 0
    failures = conformance_failures(reports, thresholds)
    for failure in failures:
        print(failure, file=sys.stderr)
    return 1 if failures else 0


def main() -> int:
    try:
        return run(parse_args())
    except (OSError, ValueError, json.JSONDecodeError) as error:
        print(f"privacy evaluation gate error: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
