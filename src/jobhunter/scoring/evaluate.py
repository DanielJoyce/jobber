"""Calibration report: scorer output vs hand labels (specs/006 "Calibration").

Labels written by console triage are the calibration set: ``interesting`` and ``applied`` are
positives, ``not_interesting`` negatives. Each labeled job group is joined to its latest matching
``fit_score`` row, and verdict / overall / bucket are recomputed with the CURRENT profile via
``compute_row`` (so a salary-floor edit shows up here, annotated by ``profile_change``).
Read-only: nothing here calls a model or writes to the database.
"""

from __future__ import annotations

import json
import math
import sqlite3
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from jobhunter.core.models import Bucket, Verdict
from jobhunter.pipeline.locations import load_job_group_locations
from jobhunter.scoring.buckets import ComputedFit, compute_row, thresholds_for
from jobhunter.scoring.profile import Profile

POSITIVE_LABELS = ("interesting", "applied")
MIN_LABELS = 150
TARGET_RECALL = 0.90
MAX_UNVERIFIED = 0.02
CONFUSION_N = 10
DIMENSIONS = (
    "skills",
    "recency_weighted_skills",
    "raw_skills",
    "seniority",
    "domain",
    "comp",
    "location",
    "overall",
)
_AB = (Bucket.A, Bucket.B)


UNRECORDED = "(unrecorded)"


@dataclass
class Item:
    group_id: int
    positive: bool
    title: str
    fit: ComputedFit
    dims: dict[str, int | None]
    unverified: bool
    claim: str | None
    cost_usd: float | None
    d_fallback: bool
    served_model: str | None = None
    # False for decisions-model rows (evidence_mode 'none'): no quotes, so they are left out
    # of the evidence_unverified rate rather than counted as verified.
    evidence_checked: bool = True


@dataclass
class VariantKey:
    prompt_version: str | None = None
    model: str | None = None
    scoring_version: str | None = None


@dataclass
class EvalReport:
    tier: str
    variant: dict[str, str | None]
    labels_total: int
    labels_unscored: int
    n: int
    n_pos: int
    n_neg: int
    metrics: dict[str, Any]
    targets: dict[str, Any]
    distribution: dict[str, Any]
    dimensions: dict[str, Any]
    confusion: dict[str, list[dict[str, Any]]]
    bucket_counts: dict[str, dict[str, int]]
    f_audit: dict[str, int]
    d_fallback: dict[str, int]
    profile_changes: list[dict[str, Any]]
    cost_per_1000: float | None = None
    served_models: dict[str, int] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {k: getattr(self, k) for k in self.__dataclass_fields__}

    def to_json(self) -> str:
        return json.dumps(self.as_dict(), indent=2, default=str)

    def to_text(self) -> str:
        return format_report(self)


@dataclass
class CompareReport:
    tier: str
    intersection: int
    variants: list[EvalReport]
    notes: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "tier": self.tier,
            "intersection": self.intersection,
            "notes": self.notes,
            "variants": [v.as_dict() for v in self.variants],
        }

    def to_json(self) -> str:
        return json.dumps(self.as_dict(), indent=2, default=str)

    def to_text(self) -> str:
        return format_compare(self)


# ─── loading ────────────────────────────────────────────────────────────────


def _rows(conn: sqlite3.Connection, sql: str, params: Sequence[Any] = ()) -> list[sqlite3.Row]:
    cur = conn.cursor()
    cur.row_factory = sqlite3.Row
    return cur.execute(sql, params).fetchall()


def _labels(conn: sqlite3.Connection) -> dict[int, tuple[str, str]]:
    return {
        r["job_group_id"]: (r["label"], r["labeled_at"])
        for r in _rows(conn, "SELECT job_group_id, label, labeled_at FROM label")
    }


def latest_prompt_version(conn: sqlite3.Connection, tier: str) -> str | None:
    rows = _rows(
        conn,
        "SELECT prompt_version FROM fit_score WHERE tier = ? ORDER BY created_at DESC, id DESC "
        "LIMIT 1",
        (tier,),
    )
    return rows[0]["prompt_version"] if rows else None


def _scored(conn: sqlite3.Connection, tier: str, key: VariantKey) -> dict[int, sqlite3.Row]:
    """Latest matching fit_score row per labeled job group."""
    where, params = ["fs.tier = ?"], [tier]
    for col, val in (
        ("prompt_version", key.prompt_version),
        ("model", key.model),
        ("scoring_version", key.scoring_version),
    ):
        if val is not None:
            where.append(f"fs.{col} = ?")
            params.append(val)
    sql = (
        "SELECT fs.*, j.id AS job_id, j.title, j.salary_min, j.salary_max, j.salary_period, "
        "j.salary_stated, j.location_scope "
        "FROM fit_score fs JOIN label l ON l.job_group_id = fs.job_group_id "
        "JOIN job_group g ON g.id = fs.job_group_id JOIN job j ON j.id = g.canonical_job_id "
        f"WHERE {' AND '.join(where)} ORDER BY fs.created_at, fs.id"
    )
    out: dict[int, sqlite3.Row] = {}
    for row in _rows(conn, sql, params):
        out[row["job_group_id"]] = row  # later rows overwrite earlier: latest wins
    return out


def _dim_score(dims: Mapping[str, Any], name: str) -> int | None:
    v = dims.get(name)
    if isinstance(v, Mapping):
        v = v.get("score")
    return int(v) if isinstance(v, int | float) else None


def _evidence_mode(row: sqlite3.Row) -> str:
    keys = row.keys()  # sqlite3.Row: ``in`` tests values, not column names
    return (row["evidence_mode"] if "evidence_mode" in keys else None) or "quotes"


def _is_d_fallback(
    fit: ComputedFit, dims: Mapping[str, int | None], th: Mapping[str, float]
) -> bool:
    """Bucket D reached without the explicit D rule: the unmatched-overall fallback (cb2d65d)."""
    if fit.bucket is not Bucket.D:
        return False
    domain = dims.get("domain")
    domain = 50 if domain is None else domain
    return not (fit.recency_weighted_skills >= th["d_skills"] and domain < th["d_domain"])


def _build_items(
    conn: sqlite3.Connection,
    profile: Profile,
    scored: Mapping[int, sqlite3.Row],
    labels: Mapping[int, tuple[str, str]],
    only: set[int] | None = None,
) -> list[Item]:
    th = thresholds_for(profile)
    items: list[Item] = []
    for gid in sorted(scored):
        if only is not None and gid not in only:
            continue
        row = scored[gid]
        fit = compute_row(row, row, load_job_group_locations(conn, row["job_id"]), profile)
        try:
            raw_dims = json.loads(row["dimensions"])
        except (TypeError, ValueError):
            raw_dims = {}
        if not isinstance(raw_dims, dict):
            raw_dims = {}
        dims: dict[str, int | None] = {
            "skills": _dim_score(raw_dims, "skills"),
            "recency_weighted_skills": fit.recency_weighted_skills,
            "raw_skills": fit.raw_skills,
            "seniority": _dim_score(raw_dims, "seniority"),
            "domain": _dim_score(raw_dims, "domain"),
            "comp": fit.comp,
            "location": fit.location,
            "overall": fit.overall,
        }
        claim = None
        try:
            ev = json.loads(row["evidence"] or "[]")
            if isinstance(ev, list) and ev and isinstance(ev[0], Mapping):
                claim = ev[0].get("claim")
        except ValueError:
            pass
        items.append(
            Item(
                group_id=gid,
                positive=labels[gid][0] in POSITIVE_LABELS,
                title=row["title"],
                fit=fit,
                dims=dims,
                unverified=bool(row["evidence_unverified"]),
                claim=claim,
                cost_usd=row["cost_usd"],
                d_fallback=_is_d_fallback(fit, dims, th),
                served_model=row["served_model"],
                evidence_checked=_evidence_mode(row) != "none",
            )
        )
    return items


# ─── metrics ────────────────────────────────────────────────────────────────


def _ratio(num: int, den: int) -> float | None:
    return round(num / den, 4) if den else None


def pearson(xs: Sequence[float], ys: Sequence[float]) -> float | None:
    """Pearson r; with a 0/1 ``ys`` this is the point-biserial correlation. None if undefined."""
    n = len(xs)
    if n < 2:
        return None
    mx, my = sum(xs) / n, sum(ys) / n
    sxx = sum((x - mx) ** 2 for x in xs)
    syy = sum((y - my) ** 2 for y in ys)
    if sxx == 0 or syy == 0:
        return None
    sxy = sum((x - mx) * (y - my) for x, y in zip(xs, ys, strict=True))
    return round(sxy / math.sqrt(sxx * syy), 4)


def _median(vals: list[float]) -> float | None:
    if not vals:
        return None
    s = sorted(vals)
    mid = len(s) // 2
    return float(s[mid]) if len(s) % 2 else (s[mid - 1] + s[mid]) / 2


def _distribution(items: Sequence[Item]) -> dict[str, Any]:
    scores = [it.fit.overall for it in items]
    hist = [0] * 10
    for s in scores:
        hist[min(s // 10, 9)] += 1
    labels = [f"{i * 10}-{i * 10 + 9 if i < 9 else 100}" for i in range(10)]
    return {
        "histogram": dict(zip(labels, hist, strict=True)),
        "mean": round(sum(scores) / len(scores), 2) if scores else None,
        "median": _median([float(s) for s in scores]),
        "share_80_plus": _ratio(sum(s >= 80 for s in scores), len(scores)),
    }


def _dimension_stats(items: Sequence[Item]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for name in DIMENSIONS:
        pairs = [(it.dims[name], 1.0 if it.positive else 0.0) for it in items]
        known = [(float(v), y) for v, y in pairs if v is not None]
        vals = [v for v, _ in known]
        n = len(vals)
        mean = sum(vals) / n if n else None
        var = sum((v - mean) ** 2 for v in vals) / n if n and mean is not None else None
        out[name] = {
            "n": n,
            "mean": round(mean, 2) if mean is not None else None,
            "variance": round(var, 2) if var is not None else None,
            "point_biserial": pearson(vals, [y for _, y in known]),
            "never_varies": n > 1 and len(set(vals)) == 1,
        }
    return out


def _confusion(items: Sequence[Item]) -> dict[str, list[dict[str, Any]]]:
    def row(it: Item) -> dict[str, Any]:
        return {
            "group_id": it.group_id,
            "title": it.title,
            "bucket": it.fit.bucket.value,
            "overall": it.fit.overall,
            "claim": it.claim,
        }

    pos = sorted((i for i in items if i.positive), key=lambda i: (i.fit.overall, i.group_id))
    neg = sorted((i for i in items if not i.positive), key=lambda i: (-i.fit.overall, i.group_id))
    return {
        "missed_positives": [row(i) for i in pos[:CONFUSION_N]],
        "false_alarms": [row(i) for i in neg[:CONFUSION_N]],
    }


def _profile_changes(conn: sqlite3.Connection, stamps: Sequence[str]) -> list[dict[str, Any]]:
    if not stamps:
        return []
    rows = _rows(
        conn,
        "SELECT at, field_path, old_value, new_value, source FROM profile_change "
        "WHERE at >= ? AND at <= ? ORDER BY at, id",
        (min(stamps), max(stamps)),
    )
    return [dict(r) for r in rows]


def _served_mix(items: Sequence[Item]) -> dict[str, int]:
    """Count of scored items per model that actually served them (routers vary by request)."""
    mix: dict[str, int] = {}
    for i in items:
        name = i.served_model or UNRECORDED
        mix[name] = mix.get(name, 0) + 1
    return dict(sorted(mix.items(), key=lambda kv: (-kv[1], kv[0])))


def _summarize(
    conn: sqlite3.Connection,
    items: Sequence[Item],
    labels: Mapping[int, tuple[str, str]],
    *,
    tier: str,
    key: VariantKey,
    labels_total: int,
) -> EvalReport:
    pos = [i for i in items if i.positive]
    neg = [i for i in items if not i.positive]
    n = len(items)

    def is_ab(i: Item) -> bool:
        return i.fit.bucket in _AB

    verdict_ok = sum(i.fit.verdict in (Verdict.strong, Verdict.possible) for i in pos)
    a_all = [i for i in items if i.fit.bucket is Bucket.A]
    ab_all = [i for i in items if is_ab(i)]
    strong = [i for i in items if i.fit.verdict is Verdict.strong]
    checked = [i for i in items if i.evidence_checked]
    metrics = {
        "recall_verdict_strong_possible": _ratio(verdict_ok, len(pos)),
        "recall_bucket_ab": _ratio(sum(is_ab(i) for i in pos), len(pos)),
        "precision_bucket_a": _ratio(sum(i.positive for i in a_all), len(a_all)),
        "precision_bucket_ab": _ratio(sum(i.positive for i in ab_all), len(ab_all)),
        "precision_verdict_strong": _ratio(sum(i.positive for i in strong), len(strong)),
        "discard_rate_not_interesting": _ratio(sum(not is_ab(i) for i in neg), len(neg)),
        "evidence_unverified_rate": _ratio(sum(i.unverified for i in checked), len(checked)),
    }
    buckets = {
        b.value: {
            "positive": sum(i.fit.bucket is b for i in pos),
            "negative": sum(i.fit.bucket is b for i in neg),
        }
        for b in Bucket
    }
    d_fb = [i for i in items if i.d_fallback]
    costs = [i.cost_usd for i in items if i.cost_usd is not None]
    recall = metrics["recall_bucket_ab"]
    unv = metrics["evidence_unverified_rate"]
    targets = {
        "min_labels": {"target": MIN_LABELS, "value": n, "pass": n >= MIN_LABELS},
        "recall_bucket_ab": {
            "target": TARGET_RECALL,
            "value": recall,
            "pass": recall is not None and recall >= TARGET_RECALL,
        },
        "evidence_unverified_rate": {
            "target": MAX_UNVERIFIED,
            "value": unv,
            # No quote-bearing rows (a decisions scorer): nothing to fail, reported as n/a.
            "pass": (unv is None and n > 0 and not checked)
            or (unv is not None and unv <= MAX_UNVERIFIED),
            "excluded": n - len(checked),
        },
    }
    notes = []
    if n < MIN_LABELS:
        notes.append(f"insufficient labels ({n}/{MIN_LABELS})")
    if n > len(checked):
        notes.append(
            f"{n - len(checked)} decisions-scorer rows carry no evidence quotes and are left "
            "out of the evidence_unverified rate"
        )
    return EvalReport(
        tier=tier,
        variant={
            "prompt_version": key.prompt_version,
            "model": key.model,
            "scoring_version": key.scoring_version,
        },
        labels_total=labels_total,
        labels_unscored=labels_total - n,
        n=n,
        n_pos=len(pos),
        n_neg=len(neg),
        metrics=metrics,
        targets=targets,
        distribution=_distribution(items),
        dimensions=_dimension_stats(items),
        confusion=_confusion(items),
        bucket_counts=buckets,
        f_audit={
            "positives_in_F": buckets["F"]["positive"],
            "negatives_in_F": buckets["F"]["negative"],
        },
        d_fallback={
            "positive": sum(i.positive for i in d_fb),
            "negative": sum(not i.positive for i in d_fb),
        },
        profile_changes=_profile_changes(conn, [labels[i.group_id][1] for i in items]),
        cost_per_1000=round(sum(costs) / len(costs) * 1000, 4) if costs else None,
        served_models=_served_mix(items),
        notes=notes,
    )


# ─── public API ─────────────────────────────────────────────────────────────


def evaluate(
    conn: sqlite3.Connection,
    profile: Profile,
    *,
    prompt_version: str | None = None,
    scoring_version: str | None = None,
    model: str | None = None,
    tier: str = "screen",
) -> EvalReport:
    """Score-vs-label report for one variant. ``prompt_version=None`` means the newest one
    present in ``fit_score`` for the tier; model and scoring_version default to any (latest row
    per group wins)."""
    if prompt_version is None:
        prompt_version = latest_prompt_version(conn, tier)
    key = VariantKey(prompt_version, model, scoring_version)
    labels = _labels(conn)
    items = _build_items(conn, profile, _scored(conn, tier, key), labels)
    return _summarize(conn, items, labels, tier=tier, key=key, labels_total=len(labels))


def compare(
    conn: sqlite3.Connection,
    profile: Profile,
    variants: Sequence[VariantKey],
    *,
    tier: str = "screen",
) -> CompareReport:
    """Side-by-side metrics on the labeled groups scored by every variant."""
    if len(variants) < 2:
        raise ValueError("compare needs at least two variants")
    labels = _labels(conn)
    scored = [_scored(conn, tier, v) for v in variants]
    common = set.intersection(*(set(s) for s in scored))
    reports = [
        _summarize(
            conn,
            _build_items(conn, profile, s, labels, only=common),
            labels,
            tier=tier,
            key=v,
            labels_total=len(common),
        )
        for v, s in zip(variants, scored, strict=True)
    ]
    notes = []
    if len(common) < MIN_LABELS:
        notes.append(f"insufficient labels ({len(common)}/{MIN_LABELS})")
    return CompareReport(tier=tier, intersection=len(common), variants=reports, notes=notes)


def parse_variant(spec: str) -> VariantKey:
    """``prompt_version@model`` or ``prompt_version`` (latest model)."""
    pv, _, model = spec.partition("@")
    if not pv:
        raise ValueError(f"bad variant {spec!r}: expected prompt_version[@model]")
    return VariantKey(prompt_version=pv, model=model or None)


# ─── text rendering ─────────────────────────────────────────────────────────


def _pct(v: float | None) -> str:
    return "n/a" if v is None else f"{v:.3f}"


def _table(headers: Sequence[str], rows: Sequence[Sequence[Any]]) -> list[str]:
    cells = [[str(c) for c in r] for r in rows]
    widths = [max([len(h), *(len(r[i]) for r in cells)]) for i, h in enumerate(headers)]
    fmt = "  ".join(f"{{:<{w}}}" for w in widths)
    return [fmt.format(*headers).rstrip(), *(fmt.format(*r).rstrip() for r in cells)]


def _pf(t: Mapping[str, Any]) -> str:
    return "PASS" if t["pass"] else "FAIL"


def format_report(r: EvalReport) -> str:
    v = r.variant
    out = [
        f"jobhunter eval  tier={r.tier}  prompt_version={v['prompt_version'] or '-'}  "
        f"model={v['model'] or 'any'}  scoring_version={v['scoring_version'] or 'any'}",
        f"labels: {r.labels_total} total, {r.n} scored ({r.n_pos} positive, {r.n_neg} negative), "
        f"{r.labels_unscored} unscored",
        "",
    ]
    if r.n < MIN_LABELS:
        out += [f"insufficient labels ({r.n}/{MIN_LABELS})", ""]
    t = r.targets
    out += ["Targets"]
    out += _table(
        ["target", "value", "threshold", ""],
        [
            ["labels", t["min_labels"]["value"], f">= {MIN_LABELS}", _pf(t["min_labels"])],
            [
                "recall A+B on positives",
                _pct(t["recall_bucket_ab"]["value"]),
                f">= {TARGET_RECALL}",
                _pf(t["recall_bucket_ab"]),
            ],
            [
                "evidence_unverified rate",
                _pct(t["evidence_unverified_rate"]["value"]),
                f"<= {MAX_UNVERIFIED}",
                _pf(t["evidence_unverified_rate"]),
            ],
        ],
    )
    mix = ", ".join(f"{m} x{n}" for m, n in r.served_models.items())
    out += ["", f"Served models: {mix or '-'}", "", "Metrics"]
    out += _table(["metric", "value"], [[k, _pct(val)] for k, val in r.metrics.items()])
    d = r.distribution
    out += [
        "",
        f"Score distribution  mean={d['mean']}  median={d['median']}  "
        f">=80: {_pct(d['share_80_plus'])}",
    ]
    out += _table(["overall", "count"], list(d["histogram"].items()))
    out += ["", "Dimensions"]
    out += _table(
        ["dimension", "n", "mean", "variance", "r(label)", "flag"],
        [
            [
                k,
                s["n"],
                "n/a" if s["mean"] is None else s["mean"],
                "n/a" if s["variance"] is None else s["variance"],
                _pct(s["point_biserial"]),
                "NEVER VARIES" if s["never_varies"] else "",
            ]
            for k, s in r.dimensions.items()
        ],
    )
    out += ["", "Buckets (label counts)"]
    out += _table(
        ["bucket", "positive", "negative"],
        [[b, c["positive"], c["negative"]] for b, c in r.bucket_counts.items()],
    )
    out += [
        "",
        f"Bucket F audit: {r.f_audit['positives_in_F']} positives in F "
        "(stale matches you wanted; recency weighting too aggressive if high)",
        f"D-fallback (no rule matched): {r.d_fallback['positive']} positive, "
        f"{r.d_fallback['negative']} negative",
    ]
    for title, rows in (
        ("Missed positives (lowest scored)", r.confusion["missed_positives"]),
        ("False alarms (highest scored negatives)", r.confusion["false_alarms"]),
    ):
        out += ["", title]
        out += _table(
            ["group", "bucket", "overall", "title", "first evidence claim"],
            [
                [c["group_id"], c["bucket"], c["overall"], c["title"], c["claim"] or ""]
                for c in rows
            ],
        )
    out += ["", "Profile changes in label window"]
    if r.profile_changes:
        out += _table(
            ["at", "field", "old", "new", "source"],
            [
                [c["at"], c["field_path"], c["old_value"], c["new_value"], c["source"]]
                for c in r.profile_changes
            ],
        )
    else:
        out.append("(none)")
    return "\n".join(out)


def format_compare(c: CompareReport) -> str:
    out = [
        f"jobhunter eval --compare  tier={c.tier}",
        f"groups scored by all {len(c.variants)} variants and labeled: {c.intersection}",
        *c.notes,
        "",
    ]
    cols = [f"{r.variant['prompt_version']}@{r.variant['model'] or 'any'}" for r in c.variants]
    keys = [
        ("recall_bucket_ab", "recall A+B"),
        ("recall_verdict_strong_possible", "recall strong+possible"),
        ("precision_bucket_a", "precision A"),
        ("precision_bucket_ab", "precision A+B"),
        ("discard_rate_not_interesting", "discard not_interesting"),
        ("evidence_unverified_rate", "evidence_unverified"),
    ]
    rows: list[list[Any]] = [[lab, *(_pct(r.metrics[k]) for r in c.variants)] for k, lab in keys]
    rows.append(["mean overall", *(r.distribution["mean"] for r in c.variants)])
    rows.append(["recall >= 0.90", *(_pf(r.targets["recall_bucket_ab"]) for r in c.variants)])
    rows.append(
        [
            "cost per 1,000 scored (USD)",
            *("n/a" if r.cost_per_1000 is None else r.cost_per_1000 for r in c.variants),
        ]
    )
    out += _table(["metric", *cols], rows)
    changes = c.variants[0].profile_changes
    out += ["", "Served models (count per variant)"]
    for col, r in zip(cols, c.variants, strict=True):
        mix = ", ".join(f"{m} x{n}" for m, n in r.served_models.items()) or "-"
        out.append(f"  {col}: {mix}")
    out += ["", f"Profile changes in label window: {len(changes)}"]
    for ch in changes:
        out.append(f"  {ch['at']}  {ch['field_path']}  ({ch['source']})")
    return "\n".join(out)
