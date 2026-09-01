from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum


class Severity(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"

    @property
    def rank(self) -> int:
        return _SEVERITY_RANK[self]


_SEVERITY_RANK = {
    Severity.LOW: 0,
    Severity.MEDIUM: 1,
    Severity.HIGH: 2,
    Severity.CRITICAL: 3,
}


class ChangeSize(StrEnum):
    XXS = "xxs"
    XS = "xs"
    S = "s"
    M = "m"
    L = "l"
    XL = "xl"
    XXL = "xxl"

    @property
    def label(self) -> str:
        return f"poppy:size/{self.value}"


class GateConclusion(StrEnum):
    SUCCESS = "success"
    FAILURE = "failure"
    ACTION_REQUIRED = "action_required"

    @property
    def result_label(self) -> str:
        return {
            GateConclusion.SUCCESS: "poppy:passed",
            GateConclusion.FAILURE: "poppy:blocked",
            GateConclusion.ACTION_REQUIRED: "poppy:action-required",
        }[self]


class ReviewEvent(StrEnum):
    APPROVE = "APPROVE"
    REQUEST_CHANGES = "REQUEST_CHANGES"
    COMMENT = "COMMENT"


@dataclass(frozen=True, slots=True)
class Finding:
    severity: Severity
    title: str
    explanation: str
    evidence: str
    suggested_response: str
    file_path: str | None = None
    line: int | None = None


@dataclass(frozen=True, slots=True)
class ReviewResult:
    summary: str
    risk: Severity
    findings: tuple[Finding, ...]


@dataclass(frozen=True, slots=True)
class GateDecision:
    conclusion: GateConclusion
    review_event: ReviewEvent
    blocking: tuple[Finding, ...]


def classify_change_size(changed_lines: int) -> ChangeSize:
    if changed_lines < 0:
        raise ValueError("changed lines must be non-negative")
    if changed_lines <= 10:
        return ChangeSize.XXS
    if changed_lines <= 50:
        return ChangeSize.XS
    if changed_lines <= 200:
        return ChangeSize.S
    if changed_lines <= 500:
        return ChangeSize.M
    if changed_lines <= 750:
        return ChangeSize.L
    if changed_lines <= 999:
        return ChangeSize.XL
    return ChangeSize.XXL


def decide_gate(
    findings: Sequence[Finding],
    *,
    blocking_severity: Severity,
    is_draft: bool,
    is_complete: bool,
) -> GateDecision:
    blocking = tuple(
        finding for finding in findings if finding.severity.rank >= blocking_severity.rank
    )

    if not is_complete:
        return GateDecision(
            conclusion=GateConclusion.ACTION_REQUIRED,
            review_event=ReviewEvent.COMMENT,
            blocking=blocking,
        )

    conclusion = GateConclusion.FAILURE if blocking else GateConclusion.SUCCESS
    if is_draft:
        review_event = ReviewEvent.COMMENT
    elif blocking:
        review_event = ReviewEvent.REQUEST_CHANGES
    else:
        review_event = ReviewEvent.APPROVE

    return GateDecision(
        conclusion=conclusion,
        review_event=review_event,
        blocking=blocking,
    )


def render_review(
    *,
    result: ReviewResult,
    decision: GateDecision,
    change_size: ChangeSize,
    changed_lines: int,
    head_sha: str,
) -> str:
    status = {
        GateConclusion.SUCCESS: "Passed",
        GateConclusion.FAILURE: "Blocked",
        GateConclusion.ACTION_REQUIRED: "Action required",
    }[decision.conclusion]
    lines = [
        "## 🤖 Poppy review",
        "",
        f"**Commit:** `{head_sha[:7]}`  ",
        f"**Change size:** {change_size.name} ({changed_lines} lines)  ",
        f"**Risk:** {result.risk.value.title()}  ",
        f"**Gate:** {status}",
        "",
        result.summary,
        "",
        "### Findings",
    ]

    if not result.findings:
        lines.append("No concrete findings.")
        return "\n".join(lines)

    for finding in result.findings:
        location = _render_location(finding)
        lines.extend(
            [
                "",
                f"#### [{finding.severity.value.upper()}] {finding.title}",
                *((f"`{location}`",) if location else ()),
                "",
                finding.explanation,
                "",
                f"**Evidence:** {finding.evidence}",
                "",
                f"**Suggested response:** {finding.suggested_response}",
            ]
        )

    return "\n".join(lines)


def _render_location(finding: Finding) -> str | None:
    if finding.file_path is None:
        return None
    if finding.line is None:
        return finding.file_path
    return f"{finding.file_path}:{finding.line}"
