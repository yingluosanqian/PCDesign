"""Issue / judgment data handling: extraction from LLM output, validation,
convergence check, and rendering for the Proposer."""
from __future__ import annotations

import json
import re
from typing import Any, Iterable


VALID_SEVERITIES = ("high", "medium", "low")
VALID_DECISIONS = ("must_fix", "should_fix", "reject", "defer")


def extract_json(text: str) -> Any:
    """Best-effort: find and parse the first JSON value in an LLM response."""
    s = text.strip()
    try:
        return json.loads(s)
    except json.JSONDecodeError:
        pass
    m = re.search(r"```(?:json)?\s*(.+?)\s*```", s, re.DOTALL)
    if m:
        try:
            return json.loads(m.group(1))
        except json.JSONDecodeError:
            pass
    for opener in ("[", "{"):
        span = _find_balanced_span(s, opener)
        if span is not None:
            try:
                return json.loads(span)
            except json.JSONDecodeError:
                continue
    raise ValueError(f"no JSON value found in response (first 300 chars): {s[:300]!r}")


def _find_balanced_span(s: str, opener: str) -> str | None:
    closer = "]" if opener == "[" else "}"
    lo = s.find(opener)
    if lo < 0:
        return None
    depth = 0
    in_string = False
    escape = False
    for i in range(lo, len(s)):
        c = s[i]
        if in_string:
            if escape:
                escape = False
            elif c == "\\":
                escape = True
            elif c == '"':
                in_string = False
            continue
        if c == '"':
            in_string = True
            continue
        if c == opener:
            depth += 1
        elif c == closer:
            depth -= 1
            if depth == 0:
                return s[lo : i + 1]
    return None


def parse_critic_issues(role: str, text: str) -> list[dict]:
    """Normalize a Critic's raw response into a list of issue dicts."""
    data = extract_json(text)
    if isinstance(data, dict) and "issues" in data and isinstance(data["issues"], list):
        data = data["issues"]
    if not isinstance(data, list):
        raise ValueError(f"critic {role!r} did not return a JSON array")
    issues: list[dict] = []
    for i, raw in enumerate(data):
        if not isinstance(raw, dict):
            continue
        severity = raw.get("severity", "medium")
        if severity not in VALID_SEVERITIES:
            severity = "medium"
        issues.append(
            {
                "id": str(raw.get("id") or f"{role[:3]}-{i+1}"),
                "critic_role": role,
                "section": str(raw.get("section") or "unknown"),
                "location": str(raw.get("location") or ""),
                "root_problem": str(raw.get("root_problem") or "unspecified"),
                "severity": severity,
                "evidence": str(raw.get("evidence") or ""),
                "suggested_direction": str(raw.get("suggested_direction") or ""),
            }
        )
    return issues


def parse_judgment(text: str) -> dict:
    """Normalize the Judge's raw response into a judgment dict.

    Always recomputes the summary from the package for reliability.
    """
    data = extract_json(text)
    if not isinstance(data, dict):
        raise ValueError("judge output is not a JSON object")
    pkg_raw = data.get("issue_package") or data.get("issues") or []
    if not isinstance(pkg_raw, list):
        raise ValueError("judge output 'issue_package' is not a list")
    package: list[dict] = []
    for i, raw in enumerate(pkg_raw):
        if not isinstance(raw, dict):
            continue
        decision = raw.get("decision", "should_fix")
        if decision not in VALID_DECISIONS:
            decision = "should_fix"
        severity = raw.get("severity", "medium")
        if severity not in VALID_SEVERITIES:
            severity = "medium"
        merged_ids_raw = raw.get("merged_issue_ids") or []
        merged_ids = [str(x) for x in merged_ids_raw if isinstance(x, (str, int))]
        package.append(
            {
                "cluster_id": str(raw.get("cluster_id") or f"c{i+1}"),
                "merged_issue_ids": merged_ids,
                "decision": decision,
                "severity": severity,
                "section": str(raw.get("section") or "unknown"),
                "root_problem": str(raw.get("root_problem") or "unspecified"),
                "rationale": str(raw.get("rationale") or ""),
                "suggested_direction": str(raw.get("suggested_direction") or ""),
                "evidence_summary": str(raw.get("evidence_summary") or ""),
            }
        )
    return {"issue_package": package, "summary": _summarize(package)}


def _summarize(package: Iterable[dict]) -> dict:
    must_fix = should_fix = reject = defer = 0
    high_actionable = 0
    for it in package:
        d = it["decision"]
        if d == "must_fix":
            must_fix += 1
        elif d == "should_fix":
            should_fix += 1
        elif d == "reject":
            reject += 1
        elif d == "defer":
            defer += 1
        if it["severity"] == "high" and d in ("must_fix", "should_fix"):
            high_actionable += 1
    return {
        "must_fix_count": must_fix,
        "should_fix_count": should_fix,
        "reject_count": reject,
        "defer_count": defer,
        "high_severity_count": high_actionable,
    }


def is_converged(judgment: dict) -> bool:
    """v1 convergence rule: no must_fix, ≤2 should_fix, no actionable high."""
    s = judgment.get("summary") or {}
    return (
        s.get("must_fix_count", 1) == 0
        and s.get("should_fix_count", 99) <= 2
        and s.get("high_severity_count", 1) == 0
    )


def format_issue_package_for_proposer(judgment: dict) -> str:
    """Render the Judge's issue package as markdown for inclusion in a prompt."""
    pkg = judgment.get("issue_package") or []
    if not pkg:
        return "_(Judge produced no actionable issues this round.)_"
    lines: list[str] = [f"Judge produced {len(pkg)} clustered items."]
    for decision in VALID_DECISIONS:
        items = [it for it in pkg if it["decision"] == decision]
        if not items:
            continue
        lines.append(f"\n## {decision} ({len(items)})")
        for it in items:
            lines.append(
                f"- **[{it['severity']}]** {it['root_problem']} — section: {it['section']}"
            )
            if it.get("suggested_direction"):
                lines.append(f"    - suggestion: {it['suggested_direction']}")
            if it.get("rationale"):
                lines.append(f"    - judge rationale: {it['rationale']}")
            if it.get("evidence_summary"):
                lines.append(f"    - evidence: {it['evidence_summary']}")
            if it.get("merged_issue_ids"):
                lines.append(
                    f"    - source issues: {', '.join(it['merged_issue_ids'])}"
                )
    return "\n".join(lines)
