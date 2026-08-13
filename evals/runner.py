"""Deterministic offline evaluations for the synthetic commerce agent.

The evaluator checks behavior and execution trajectories, not only reply text.  It
intentionally uses the rule planner and synthetic fixtures so CI never needs a model
API key or network access.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import tempfile
from collections import Counter
from pathlib import Path
from typing import Any


def _load_cases(path: Path) -> list[dict[str, Any]]:
    cases: list[dict[str, Any]] = []
    seen: set[str] = set()
    with path.open(encoding="utf-8") as stream:
        for line_number, raw_line in enumerate(stream, start=1):
            line = raw_line.strip()
            if not line:
                continue
            try:
                case = json.loads(line)
            except json.JSONDecodeError as error:
                raise ValueError(f"Invalid JSON at {path}:{line_number}: {error}") from error
            case_id = case.get("id")
            if not isinstance(case_id, str) or not case_id:
                raise ValueError(f"Missing case id at {path}:{line_number}")
            if case_id in seen:
                raise ValueError(f"Duplicate case id: {case_id}")
            if not isinstance(case.get("request"), dict) or not isinstance(
                case.get("expect"), dict
            ):
                raise ValueError(f"Case {case_id} must contain request and expect objects")
            seen.add(case_id)
            cases.append(case)
    if not cases:
        raise ValueError(f"No evaluation cases found in {path}")
    return cases


def _check_equal(failures: list[str], *, label: str, expected: Any, actual: Any) -> None:
    if actual != expected:
        failures.append(f"{label}: expected {expected!r}, got {actual!r}")


def _evaluate_response(
    response: dict[str, Any],
    expected: dict[str, Any],
    trace: dict[str, Any],
    history: list[dict[str, str]],
) -> list[str]:
    failures: list[str] = []
    for field in ("intent", "status", "deduplicated"):
        if field in expected:
            _check_equal(
                failures,
                label=field,
                expected=expected[field],
                actual=response.get(field),
            )

    actual_tools = [call.get("name") for call in response.get("tool_calls", [])]
    for required in expected.get("required_tools", []):
        if required not in actual_tools:
            failures.append(f"required tool not called: {required}")
    for forbidden in expected.get("forbidden_tools", []):
        if forbidden in actual_tools:
            failures.append(f"forbidden tool called: {forbidden}")

    if "policy_outcome" in expected:
        policy = response.get("policy") or {}
        _check_equal(
            failures,
            label="policy.outcome",
            expected=expected["policy_outcome"],
            actual=policy.get("outcome"),
        )
    if "policy_reason" in expected:
        policy = response.get("policy") or {}
        _check_equal(
            failures,
            label="policy.reason_code",
            expected=expected["policy_reason"],
            actual=policy.get("reason_code"),
        )
    if "pending_action_state" in expected:
        pending_action = response.get("pending_action") or {}
        _check_equal(
            failures,
            label="pending_action.state",
            expected=expected["pending_action_state"],
            actual=pending_action.get("state"),
        )

    actual_facts = set(response.get("facts_used", []))
    for fact in expected.get("required_facts", []):
        if fact not in actual_facts:
            failures.append(f"required grounding fact missing: {fact}")

    reply = response.get("reply", "")
    for fragment in expected.get("reply_contains", []):
        if fragment not in reply:
            failures.append(f"reply does not contain {fragment!r}")

    trace_names = {step.get("name") for step in trace.get("steps", [])}
    for step_name in expected.get("trace_steps", []):
        if step_name not in trace_names:
            failures.append(f"required trace step missing: {step_name}")

    if trace:
        _check_equal(
            failures,
            label="trace.trace_id",
            expected=response.get("trace_id"),
            actual=trace.get("trace_id"),
        )
        _check_equal(
            failures,
            label="trace.status",
            expected=response.get("status"),
            actual=trace.get("status"),
        )
    else:
        failures.append("persisted trace is missing")

    serialized = json.dumps(
        {"response": response, "trace": trace, "history": history},
        ensure_ascii=False,
        sort_keys=True,
    )
    for fragment in expected.get("not_persisted", []):
        if fragment in serialized:
            failures.append(f"sensitive fragment was persisted or returned: {fragment!r}")
    return failures


async def _run_cases(
    *, project_root: Path, database_path: Path, cases: list[dict[str, Any]]
) -> dict[str, Any]:
    src_path = project_root / "src"
    if str(src_path) not in sys.path:
        sys.path.insert(0, str(src_path))

    from dxl_agent.config import Settings
    from dxl_agent.domain import IncomingMessage
    from dxl_agent.runtime import AgentRuntime

    settings = Settings(
        database_path=database_path,
        fixture_dir=project_root / "demo" / "fixtures",
        policy_path=project_root / "policies" / "default.toml",
        max_history_turns=8,
        demo_mode=True,
        planner_mode="rules",
    )
    runtime = AgentRuntime.from_settings(settings)
    results: list[dict[str, Any]] = []
    tag_totals: Counter[str] = Counter()
    tag_passed: Counter[str] = Counter()

    for case in cases:
        request = dict(case["request"])
        request["attachments"] = tuple(request.get("attachments", ()))
        message = IncomingMessage(**request)
        failures: list[str]
        try:
            response = await runtime.handle_message(message)
            trace = runtime.get_trace(response["trace_id"])
            history = runtime.store.recent_turns(message.session_key, 100)
            failures = _evaluate_response(response, case["expect"], trace, history)
            actual = {
                "intent": response.get("intent"),
                "status": response.get("status"),
                "tools": [call.get("name") for call in response.get("tool_calls", [])],
                "policy_outcome": (response.get("policy") or {}).get("outcome"),
                "policy_reason": (response.get("policy") or {}).get("reason_code"),
                "deduplicated": response.get("deduplicated"),
                "trace_id": response.get("trace_id"),
            }
        except Exception as error:  # Evaluations report a case failure instead of aborting.
            failures = [f"unhandled {type(error).__name__}: {error}"]
            actual = {"exception": type(error).__name__, "message": str(error)}

        passed = not failures
        tags = case.get("tags", [])
        for tag in tags:
            tag_totals[tag] += 1
            if passed:
                tag_passed[tag] += 1
        results.append(
            {
                "id": case["id"],
                "passed": passed,
                "tags": tags,
                "failures": failures,
                "actual": actual,
            }
        )

    passed_count = sum(result["passed"] for result in results)
    tag_metrics = {
        tag: {
            "passed": tag_passed[tag],
            "total": total,
            "pass_rate": round(tag_passed[tag] / total, 4),
        }
        for tag, total in sorted(tag_totals.items())
    }
    return {
        "suite": "synthetic-commerce-agent",
        "planner_mode": "rules",
        "total": len(results),
        "passed": passed_count,
        "failed": len(results) - passed_count,
        "pass_rate": round(passed_count / len(results), 4),
        "safety_failures": sum(
            1 for result in results if "safety" in result["tags"] and not result["passed"]
        ),
        "tags": tag_metrics,
        "cases": results,
    }


def run_suite(project_root: str | Path, database_path: str | Path | None = None) -> dict[str, Any]:
    """Run all JSONL cases and return a machine-readable evaluation report."""

    root = Path(project_root).resolve()
    cases = _load_cases(root / "evals" / "cases.jsonl")
    if database_path is not None:
        return asyncio.run(
            _run_cases(project_root=root, database_path=Path(database_path), cases=cases)
        )
    with tempfile.TemporaryDirectory(prefix="dxl-agent-eval-") as temporary_dir:
        return asyncio.run(
            _run_cases(
                project_root=root,
                database_path=Path(temporary_dir) / "eval.db",
                cases=cases,
            )
        )


def _print_human_report(report: dict[str, Any]) -> None:
    print(
        f"Eval: {report['passed']}/{report['total']} passed "
        f"({report['pass_rate']:.1%}); safety failures={report['safety_failures']}"
    )
    for case in report["cases"]:
        marker = "PASS" if case["passed"] else "FAIL"
        print(f"[{marker}] {case['id']}")
        for failure in case["failures"]:
            print(f"  - {failure}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--project-root",
        type=Path,
        default=Path(__file__).resolve().parents[1],
        help="Repository root (defaults to the parent of evals/)",
    )
    parser.add_argument("--database-path", type=Path, default=None)
    parser.add_argument("--json", action="store_true", help="Print the full JSON report")
    arguments = parser.parse_args(argv)
    report = run_suite(arguments.project_root, arguments.database_path)
    if arguments.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        _print_human_report(report)
    return 0 if report["failed"] == 0 and report["safety_failures"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
