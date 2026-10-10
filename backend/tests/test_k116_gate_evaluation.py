from pathlib import Path

from verify_k116_gate import verify


def test_gate_evaluation_fails_closed_when_evidence_is_missing() -> None:
    report = verify(Path("absent-k116-run-evidence"))

    assert report["k116_gate_result"] == "NO PASS"
    assert any(
        check["name"] == "required_artifacts" and not check["passed"] for check in report["checks"]
    )
