"""Issue / judgment data handling: extraction from LLM output, validation,
convergence check, and rendering for the Proposer."""
from __future__ import annotations

import json
import re
from typing import Any, Iterable


VALID_SEVERITIES = ("high", "medium", "low")
VALID_DECISIONS = ("must_fix", "should_fix", "reject", "defer")
VALID_COGNITIVE_MOVES = (
    "analogy",
    "inversion",
    "minimalization",
    "rederivation",
    "requirement_pushback",
    "scale_extrapolation",
)


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


def parse_alternatives(text: str) -> list[dict]:
    """Normalize a Reframer's raw response into a list of alternative dicts.

    Each alt preserves id / cognitive_move / one_line / key_invariant /
    tradeoff_vs_baseline / sketch. Invalid cognitive_moves fall back to
    "rederivation" (the most generic one) with a record of the bad value
    in `invalid_cognitive_move_raw` so we can tell the LLM tagged it
    badly vs. really did a rederivation.
    """
    data = extract_json(text)
    if isinstance(data, dict) and "alternatives" in data:
        data = data["alternatives"]
    if not isinstance(data, list):
        raise ValueError("reframer output is not a JSON array of alternatives")
    out: list[dict] = []
    for i, raw in enumerate(data):
        if not isinstance(raw, dict):
            continue
        move_raw = str(raw.get("cognitive_move") or "")
        move = move_raw if move_raw in VALID_COGNITIVE_MOVES else "rederivation"
        entry: dict = {
            "id": str(raw.get("id") or f"alt-{i+1}"),
            "cognitive_move": move,
            "one_line": str(raw.get("one_line") or "unspecified"),
            "key_invariant": str(raw.get("key_invariant") or ""),
            "tradeoff_vs_baseline": str(raw.get("tradeoff_vs_baseline") or ""),
            "sketch": str(raw.get("sketch") or ""),
        }
        if move != move_raw:
            entry["invalid_cognitive_move_raw"] = move_raw
        out.append(entry)
    return out


def format_alternatives_for_proposer(alternatives: list[dict]) -> str:
    """Render alternatives as a markdown block for the Proposer's prompt."""
    if not alternatives:
        return "_(Reframer produced no alternatives this round.)_"
    lines: list[str] = [f"Reframer produced {len(alternatives)} alternative(s)."]
    for a in alternatives:
        lines.append("")
        lines.append(
            f"### `{a['id']}` — {a.get('cognitive_move', '?')}"
        )
        lines.append(f"**One-line**: {a.get('one_line', '')}")
        if a.get("key_invariant"):
            lines.append(f"**Key invariant vs baseline**: {a['key_invariant']}")
        if a.get("tradeoff_vs_baseline"):
            lines.append(
                f"**Trade-off vs baseline**: {a['tradeoff_vs_baseline']}"
            )
        if a.get("sketch"):
            lines.append("")
            lines.append("**Sketch:**")
            lines.append(a["sketch"])
    return "\n".join(lines)


def format_alternatives_summary(alternatives: list[dict]) -> str:
    """Render alternatives as a standalone markdown summary for rounds/iter_NNN/."""
    if not alternatives:
        return "_No alternatives produced this round._\n"
    lines: list[str] = [f"# Reframer alternatives ({len(alternatives)})"]
    for a in alternatives:
        lines.append("")
        lines.append(f"## `{a['id']}` — {a.get('cognitive_move', '?')}")
        lines.append("")
        lines.append(f"**{a.get('one_line', '')}**")
        if a.get("key_invariant"):
            lines.append("")
            lines.append(f"- **Key invariant vs baseline**: {a['key_invariant']}")
        if a.get("tradeoff_vs_baseline"):
            lines.append(
                f"- **Trade-off vs baseline**: {a['tradeoff_vs_baseline']}"
            )
        if a.get("invalid_cognitive_move_raw"):
            lines.append(
                f"- _(raw cognitive_move tag was "
                f"`{a['invalid_cognitive_move_raw']}` — normalized to "
                f"`{a['cognitive_move']}`)_"
            )
        if a.get("sketch"):
            lines.append("")
            lines.append("### Sketch")
            lines.append("")
            lines.append(a["sketch"])
    return "\n".join(lines) + "\n"


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
    """Single-round quality-suppression check: no must_fix, ≤2 should_fix, no actionable high.

    NOTE: this is one of the two signals the orchestrator requires for
    declaring overall convergence. The other is `is_stable` (the
    previous round was itself quality_ok). Callers that want the
    combined signal should `and` both together.
    """
    s = judgment.get("summary") or {}
    return (
        s.get("must_fix_count", 1) == 0
        and s.get("should_fix_count", 99) <= 2
        and s.get("high_severity_count", 1) == 0
    )


def is_stable(prev_judgment: dict | None) -> bool:
    """Stability: the most-recent non-degraded round was itself quality_ok.

    Combined with `is_converged(curr)` the convergence rule becomes "two
    consecutive quality_ok rounds" — the second observation supplies the
    cross-round evidence §1.3 of the design requires.

    The previous form (`prev.must_fix >= curr.must_fix`) was trivially
    true whenever `is_converged(curr)` already forced `curr.must_fix == 0`,
    so stability carried no real signal and a single 5→0 transition
    declared convergence. Requiring the prev round to be quality_ok is
    the §3.4 C4 fix from the iterated spec.

    Callers are responsible for passing the last NON-DEGRADED judgment as
    `prev_judgment` — a Proposer no-op or critic failure must not count
    as evidence the design is stable.
    """
    if not isinstance(prev_judgment, dict):
        return False
    return is_converged(prev_judgment)


def no_progress(must_fix_history: list[int]) -> bool:
    """Detect "stuck" runs: must_fix non-decreasing across the last 2 transitions.

    Needs at least 3 data points. Returns True iff the last three
    must_fix counts satisfy h[-1] >= h[-2] >= h[-3] and h[-1] > 0.
    When True, the orchestrator halts and recommends human check.
    """
    if len(must_fix_history) < 3:
        return False
    a, b, c = must_fix_history[-3], must_fix_history[-2], must_fix_history[-1]
    return c >= b and b >= a and c > 0


def format_round_summary(
    iteration: int,
    critics_output: dict,
    judgment: dict,
    manually_edited: bool = False,
    degraded: bool = False,
    degraded_reasons: list[str] | None = None,
) -> str:
    """Render one iteration — all three critics + the Judge's package — as
    a single human-readable markdown document. Dropped onto disk at
    `.pcd/rounds/iter_NNN/summary.md` next to the per-role JSON files."""
    lines: list[str] = [f"# Iteration {iteration}"]
    if degraded:
        reasons = ", ".join(degraded_reasons or []) or "unspecified"
        lines.append("")
        lines.append(
            f"> ⚠ **Degraded round**: {reasons}. This round's quality signal "
            "is not counted as stability evidence for later rounds, and is "
            "skipped from the no-progress sliding window."
        )
    if manually_edited:
        lines.append("")
        lines.append(
            "> Judge's package was manually edited via `--manual-judge` "
            "before the Proposer consumed it."
        )

    for role, issues in critics_output.items():
        lines.append("")
        lines.append(f"## Critic: {role} ({len(issues)} issue(s))")
        if not issues:
            lines.append("")
            lines.append("_No issues raised._")
            continue
        for it in issues:
            sev = it.get("severity", "?")
            rp = it.get("root_problem") or "unspecified"
            lines.append("")
            lines.append(f"- **[{sev}]** {rp}")
            loc = it.get("location") or ""
            if loc:
                lines.append(f"    - location: {loc}")
            ev = it.get("evidence") or ""
            if ev:
                lines.append(f"    - evidence: {ev}")
            sd = it.get("suggested_direction") or ""
            if sd:
                lines.append(f"    - suggestion: {sd}")
            iid = it.get("id") or ""
            if iid:
                lines.append(f"    - id: `{iid}`")

    pkg = judgment.get("issue_package") or []
    lines.append("")
    lines.append(f"## Judge's package ({len(pkg)} cluster(s))")
    for decision in VALID_DECISIONS:
        items = [c for c in pkg if c.get("decision") == decision]
        if not items:
            continue
        lines.append("")
        lines.append(f"### {decision} ({len(items)})")
        for c in items:
            sev = c.get("severity", "?")
            rp = c.get("root_problem") or "unspecified"
            section = c.get("section") or "?"
            lines.append("")
            lines.append(f"- **[{sev}]** {rp} — section: {section}")
            if c.get("suggested_direction"):
                lines.append(f"    - suggestion: {c['suggested_direction']}")
            if c.get("rationale"):
                lines.append(f"    - judge rationale: {c['rationale']}")
            if c.get("evidence_summary"):
                lines.append(f"    - evidence: {c['evidence_summary']}")
            if c.get("merged_issue_ids"):
                lines.append(
                    "    - source issues: "
                    + ", ".join(c["merged_issue_ids"])
                )

    s = judgment.get("summary") or {}
    lines.append("")
    lines.append("## Summary")
    for k in (
        "must_fix_count",
        "should_fix_count",
        "reject_count",
        "defer_count",
        "high_severity_count",
    ):
        lines.append(f"- `{k}`: {s.get(k, 0)}")

    return "\n".join(lines) + "\n"


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
