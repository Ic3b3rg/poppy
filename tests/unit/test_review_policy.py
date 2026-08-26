from collections.abc import Sequence

import pytest

from poppy.domain.review import (
    ChangeSize,
    Finding,
    GateConclusion,
    ReviewEvent,
    ReviewResult,
    Severity,
    classify_change_size,
    decide_gate,
    render_review,
)


@pytest.mark.parametrize(
    ("changed_lines", "expected"),
    [
        (0, ChangeSize.XXS),
        (10, ChangeSize.XXS),
        (11, ChangeSize.XS),
        (50, ChangeSize.XS),
        (51, ChangeSize.S),
        (200, ChangeSize.S),
        (201, ChangeSize.M),
        (500, ChangeSize.M),
        (501, ChangeSize.L),
        (750, ChangeSize.L),
        (751, ChangeSize.XL),
        (999, ChangeSize.XL),
        (1000, ChangeSize.XXL),
        (10_000, ChangeSize.XXL),
    ],
)
def test_classifies_change_size_at_every_boundary(
    changed_lines: int,
    expected: ChangeSize,
) -> None:
    assert classify_change_size(changed_lines) is expected


def test_rejects_negative_change_size() -> None:
    with pytest.raises(ValueError, match="non-negative"):
        classify_change_size(-1)


@pytest.mark.parametrize(
    ("findings", "is_draft", "is_complete", "conclusion", "review_event"),
    [
        ((), False, True, GateConclusion.SUCCESS, ReviewEvent.APPROVE),
        (
            (
                Finding(
                    severity=Severity.MEDIUM,
                    title="Concern",
                    explanation="Explanation",
                    evidence="Evidence",
                    suggested_response="Response",
                ),
            ),
            False,
            True,
            GateConclusion.SUCCESS,
            ReviewEvent.APPROVE,
        ),
        (
            (
                Finding(
                    severity=Severity.HIGH,
                    title="Bug",
                    explanation="Explanation",
                    evidence="Evidence",
                    suggested_response="Response",
                ),
            ),
            False,
            True,
            GateConclusion.FAILURE,
            ReviewEvent.REQUEST_CHANGES,
        ),
        (
            (
                Finding(
                    severity=Severity.CRITICAL,
                    title="Leak",
                    explanation="Explanation",
                    evidence="Evidence",
                    suggested_response="Response",
                ),
            ),
            True,
            True,
            GateConclusion.FAILURE,
            ReviewEvent.COMMENT,
        ),
        ((), True, True, GateConclusion.SUCCESS, ReviewEvent.COMMENT),
        ((), False, False, GateConclusion.ACTION_REQUIRED, ReviewEvent.COMMENT),
    ],
)
def test_decides_gate_and_formal_review(
    findings: Sequence[Finding],
    is_draft: bool,
    is_complete: bool,
    conclusion: GateConclusion,
    review_event: ReviewEvent,
) -> None:
    decision = decide_gate(
        findings,
        blocking_severity=Severity.HIGH,
        is_draft=is_draft,
        is_complete=is_complete,
    )

    assert decision.conclusion is conclusion
    assert decision.review_event is review_event


def test_returns_only_blocking_findings() -> None:
    medium = Finding(
        severity=Severity.MEDIUM,
        title="Medium",
        explanation="Explanation",
        evidence="Evidence",
        suggested_response="Response",
    )
    high = Finding(
        severity=Severity.HIGH,
        title="High",
        explanation="Explanation",
        evidence="Evidence",
        suggested_response="Response",
    )

    decision = decide_gate(
        (medium, high),
        blocking_severity=Severity.HIGH,
        is_draft=False,
        is_complete=True,
    )

    assert decision.blocking == (high,)


def test_maps_gate_conclusion_to_owned_result_label() -> None:
    assert GateConclusion.SUCCESS.result_label == "poppy:passed"
    assert GateConclusion.FAILURE.result_label == "poppy:blocked"
    assert GateConclusion.ACTION_REQUIRED.result_label == "poppy:action-required"


def test_renders_one_concise_formal_review() -> None:
    finding = Finding(
        severity=Severity.HIGH,
        title="Unvalidated redirect",
        explanation="The destination comes from request data.",
        evidence="redirect(target) accepts any host.",
        suggested_response="Allow only trusted relative paths.",
        file_path="src/redirect.py",
        line=42,
    )
    result = ReviewResult(
        summary="The change needs one security correction.",
        risk=Severity.HIGH,
        findings=(finding,),
    )
    decision = decide_gate(
        result.findings,
        blocking_severity=Severity.HIGH,
        is_draft=False,
        is_complete=True,
    )

    body = render_review(
        result=result,
        decision=decision,
        change_size=ChangeSize.XS,
        changed_lines=37,
        head_sha="0123456789abcdef",
    )

    assert "Poppy review" in body
    assert "`0123456`" in body
    assert "XS (37 lines)" in body
    assert "High" in body
    assert "The change needs one security correction." in body
    assert "Unvalidated redirect" in body
    assert "src/redirect.py:42" in body
    assert "Allow only trusted relative paths." in body


def test_renders_no_findings_without_empty_sections() -> None:
    result = ReviewResult(summary="Looks focused and safe.", risk=Severity.LOW, findings=())
    decision = decide_gate(
        result.findings,
        blocking_severity=Severity.HIGH,
        is_draft=False,
        is_complete=True,
    )

    body = render_review(
        result=result,
        decision=decision,
        change_size=ChangeSize.XXS,
        changed_lines=4,
        head_sha="abcdef0123456789",
    )

    assert "No concrete findings." in body
    assert "None" not in body
