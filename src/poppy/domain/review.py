from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum
from html import escape

RERUN_LABEL = "poppy:rerun"


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

_SEVERITY_MARKER = {
    Severity.LOW: "🔵",
    Severity.MEDIUM: "🟡",
    Severity.HIGH: "🟠",
    Severity.CRITICAL: "🔴",
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
    status_marker = {
        GateConclusion.SUCCESS: "✅",
        GateConclusion.FAILURE: "🔴",
        GateConclusion.ACTION_REQUIRED: "⚠️",
    }[decision.conclusion]
    ordered = order_findings(result.findings)
    lines = [
        "## 🤖 Poppy review",
        "",
        (
            f"> {status_marker} **{status}** · Risk **{result.risk.value.title()}**"
            f" · Size {change_size.name} ({changed_lines} lines)"
            f" · Commit `{head_sha[:7]}`"
        ),
        "",
        f"<p>{_render_plain_text(result.summary)}</p>",
        "",
        "### Findings",
    ]

    if not ordered:
        lines.extend(["", "✅ No concrete findings."])
        return _with_rerun_footer(lines)

    lines.extend(
        [
            "",
            "| Severity | Finding | Location |",
            "| --- | --- | --- |",
        ]
    )

    for finding in ordered:
        marker = _SEVERITY_MARKER[finding.severity]
        location = _render_location(finding)
        rendered_location = (
            f"<code>{_render_single_line(location)}</code>" if location is not None else "—"
        )
        lines.append(
            "| "
            f"{marker} **{finding.severity.value.upper()}** | "
            f"{_escape_table_cell(finding.title)} | {rendered_location} |"
        )

    for finding in ordered:
        marker = _SEVERITY_MARKER[finding.severity]
        location = _render_location(finding)
        lines.extend(
            [
                "",
                "<details>",
                (
                    f"<summary>{marker} <strong>"
                    f"{finding.severity.value.upper()}</strong> · "
                    f"{_render_single_line(finding.title)}</summary>"
                ),
                "",
                *(
                    (
                        "<p><strong>Location:</strong> "
                        f"<code>{_render_single_line(location)}</code></p>",
                        "",
                    )
                    if location
                    else ()
                ),
                f"<p>{_render_plain_text(finding.explanation)}</p>",
                "",
                (f"<p><strong>Evidence:</strong> {_render_plain_text(finding.evidence)}</p>"),
                "",
                (
                    "<p><strong>Suggested fix:</strong> "
                    f"{_render_plain_text(finding.suggested_response)}</p>"
                ),
                "",
                "</details>",
            ]
        )

    return _with_rerun_footer(lines)


def order_findings(findings: Sequence[Finding]) -> tuple[Finding, ...]:
    return tuple(sorted(findings, key=lambda finding: finding.severity.rank, reverse=True))


def severity_marker(severity: Severity) -> str:
    return _SEVERITY_MARKER[severity]


def render_finding_comment(finding: Finding) -> str:
    title = _render_single_line(finding.title)
    return "\n".join(
        (
            f"### {severity_marker(finding.severity)} {finding.severity.value.upper()} · {title}",
            "",
            f"<p>{_render_plain_text(finding.explanation)}</p>",
            "",
            (f"<p><strong>Evidence:</strong> {_render_plain_text(finding.evidence)}</p>"),
            "",
            (
                "<p><strong>Suggested fix:</strong> "
                f"{_render_plain_text(finding.suggested_response)}</p>"
            ),
        )
    )


def _with_rerun_footer(lines: list[str]) -> str:
    lines.extend(
        [
            "",
            "---",
            "Apply the `poppy:rerun` label to review the latest commit again.",
        ]
    )
    return "\n".join(lines)


def _escape_table_cell(value: str) -> str:
    return _render_single_line(value).replace("|", "&#124;")


def _render_single_line(value: str) -> str:
    return escape(" ".join(value.split()))


def _render_plain_text(value: str) -> str:
    normalized = value.replace("\r\n", "\n").replace("\r", "\n")
    return escape(normalized).replace("\n", "<br>\n")


def _render_location(finding: Finding) -> str | None:
    if finding.file_path is None:
        return None
    if finding.line is None:
        return finding.file_path
    return f"{finding.file_path}:{finding.line}"
