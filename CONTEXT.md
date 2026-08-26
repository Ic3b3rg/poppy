# Poppy

Poppy is a personal, reviewer-only pull-request gate for one human owner and only the GitHub repositories that owner explicitly enables. It can approve or request changes, but it never creates or merges pull requests.

## Language

**Operator**:
The sole human who installs, configures, and can disable Poppy.
_Avoid_: Customer, tenant, admin

**Managed Repository**:
A GitHub repository on which the Operator has explicitly installed Poppy.
_Avoid_: Monitored repository, arbitrary repository

**Review Cycle**:
One evaluation of a specific pull-request revision that produces a review result for the Operator.
_Avoid_: Build, job, autonomous fix

**Finding**:
A concrete concern found during a Review Cycle, carrying severity, evidence, and a suggested response.
_Avoid_: Opinion, generic advice, style preference

**Review Gate**:
The GitHub Check Run produced from a Review Cycle: pass when no blocking Finding exists, otherwise block for human attention.
_Avoid_: Merge, autonomous merge decision

**Formal Review**:
The GitHub reviewer verdict Poppy submits for one exact pull-request revision: approve, request changes, or comment. It never performs the merge.
_Avoid_: Merge authorization, auto-merge

**Change Size**:
A deterministic `XXS` through `XXL` classification of pull-request magnitude derived from changed lines.
_Avoid_: Difficulty, effort estimate, risk
