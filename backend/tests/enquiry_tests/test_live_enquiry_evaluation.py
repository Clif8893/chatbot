"""Opt-in live evaluation of Module A against the configured model.

Excluded from normal runs: it calls the real provider and incurs usage. Run with:

    $env:RUN_LIVE_ENQUIRY_EVAL="1"
    .\\backend\\.venv\\Scripts\\python.exe -m pytest -m live_provider backend\\tests\\enquiry_tests\\test_live_enquiry_evaluation.py -q -s

The model comes from backend/.env (ENQUIRY_MODEL, else OPENAI_MODEL). Set
EVAL_REPORT_PATH to keep the JSON report; otherwise it is written to pytest's
temporary directory. Only fictional participants are used.
"""

import json
import os
from pathlib import Path

import pytest

from backend.app import create_app
from backend.config import enquiry_model_settings
from backend.tests.conftest import authenticated_client, send
from backend.tests.enquiry_tests.eval.runner import load_cases, run_case, summarise


@pytest.mark.live_provider
@pytest.mark.skipif(
    os.getenv("RUN_LIVE_ENQUIRY_EVAL") != "1",
    reason="Set RUN_LIVE_ENQUIRY_EVAL=1 explicitly to call the real provider.",
)
def test_live_enquiry_evaluation(tmp_path):
    app = create_app(
        {
            "TESTING": True,
            "SECRET_KEY": "live-enquiry-evaluation",
            "SQLALCHEMY_DATABASE_URI": f"sqlite:///{(tmp_path / 'eval.db').as_posix()}",
        }
    )
    selected = os.getenv("EVAL_CASES")
    cases = [
        case
        for case in load_cases()
        if not selected or case["id"] in selected.split(",")
    ]
    results = [run_case(app, case, authenticated_client, send) for case in cases]
    summary = summarise(results)
    model, _ = enquiry_model_settings(app.config)
    summary["model"] = model
    report = {
        "summary": summary,
        "cases": [
            {"id": result.id, "category": result.category, "passed": result.passed,
             "failures": result.failures, "transcript": result.transcript}
            for result in results
        ],
    }
    path = Path(os.getenv("EVAL_REPORT_PATH") or tmp_path / "enquiry-eval-report.json")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")

    print(f"\nModel: {model}  Passed {summary['passed']}/{summary['total']}  Report: {path}")
    for category, stats in summary["categories"].items():
        flag = "ok" if stats["meets_threshold"] else "BELOW"
        print(f"  {category:18} {stats['passed']}/{stats['total']}  (threshold {stats['threshold']:.0%}) {flag}")
    for failure in summary["failures"]:
        print(f"  FAIL {failure['id']}: {'; '.join(failure['failures'])}")

    below = [name for name, stats in summary["categories"].items() if not stats["meets_threshold"]]
    assert not below, f"Categories below threshold: {below}"
