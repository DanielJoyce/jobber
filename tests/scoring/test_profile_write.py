"""save_profile_changes: ruamel round-trip, atomic write, validation, conflicts. Synthetic data."""

from __future__ import annotations

import os

import pytest

from jobhunter.scoring.profile import (
    ProfileConflict,
    ProfileValidationError,
    load_profile,
    preferences_mtime_ns,
    profile_data,
    save_profile_changes,
    validate_profile_data,
)

PREFS = """\
# Synthetic profile for tests.
resume_path: resume.md

# ─── HARD ───
hard:
  states_allowed: all
  states_excluded: [TX]   # never
  remote_ok: true
  salary_floor: {amount: 90000, period: year}
  title_exclusions:
    - intern       # unpaid
    - volunteer

soft:
  # weights must sum to 1
  weights: {skills: 0.30, seniority: 0.20, domain: 0.20, comp: 0.15, location: 0.15}
  state_ranking: [CO, WA]

narrative:
  want: |
    Synthetic systems work.
    Small teams.
"""


@pytest.fixture
def pdir(tmp_path):
    d = tmp_path / "profile"
    d.mkdir()
    (d / "preferences.yaml").write_text(PREFS, encoding="utf-8")
    (d / "resume.md").write_text("Synthetic Person\n- ran synthetic hosts\n", encoding="utf-8")
    return d


def text(pdir):
    return (pdir / "preferences.yaml").read_text(encoding="utf-8")


def test_round_trip_preserves_comments_and_order(pdir):
    mtime = preferences_mtime_ns(pdir)
    p = save_profile_changes(
        pdir,
        {"hard.salary_floor.amount": 80000, "soft.state_ranking": ["WA", "CO", "OR"]},
        expected_mtime_ns=mtime,
    )
    assert p.hard.salary_floor.amount == 80000
    assert p.soft.state_ranking == ["WA", "CO", "OR"]
    out = text(pdir)
    for comment in ("# Synthetic profile", "# ─── HARD ───", "# never", "# unpaid", "# weights"):
        assert comment in out
    assert "salary_floor: {amount: 80000, period: year}" in out
    assert "state_ranking: [WA, CO, OR]" in out
    keys = [ln.split(":")[0] for ln in out.splitlines() if ln and ln[0].isalpha()]
    assert keys == ["resume_path", "hard", "soft", "narrative"]
    # Only the two changed lines differ.
    changed = [(a, b) for a, b in zip(PREFS.splitlines(), out.splitlines(), strict=True) if a != b]
    assert len(changed) == 2


def test_block_sequence_indent_kept(pdir):
    (pdir / "preferences.yaml").write_text(
        "resume_path: resume.md\ntarget_titles:\n  - Engineer   # main\n  - Admin\n",
        encoding="utf-8",
    )
    save_profile_changes(
        pdir, {"hard.remote_ok": True}, expected_mtime_ns=preferences_mtime_ns(pdir)
    )
    out = text(pdir)
    assert "  - Engineer   # main\n  - Admin\n" in out
    assert "hard:\n  remote_ok: true" in out


def test_new_keys_none_deletes_and_multiline(pdir):
    p = save_profile_changes(
        pdir,
        {
            "hard.salary_floor": None,
            "current_focus.done_with": ["Java enterprise"],
            "narrative.avoid": "On-call rotations.\nVendor consoles.",
        },
        expected_mtime_ns=preferences_mtime_ns(pdir),
    )
    assert p.hard.salary_floor is None
    assert p.current_focus.done_with == ["Java enterprise"]
    assert p.narrative.avoid == "On-call rotations.\nVendor consoles."
    out = text(pdir)
    assert "salary_floor" not in out
    assert "avoid: |" in out


def test_validation_failure_leaves_original(pdir):
    with pytest.raises(ProfileValidationError) as info:
        save_profile_changes(
            pdir,
            {"soft.weights.skills": 0.9, "hard.salary_floor.period": "fortnight"},
            expected_mtime_ns=preferences_mtime_ns(pdir),
        )
    assert "soft.weights" in info.value.errors
    assert "hard.salary_floor.period" in info.value.errors
    assert str(pdir / "preferences.yaml") in str(info.value)
    assert text(pdir) == PREFS
    assert sorted(os.listdir(pdir)) == ["preferences.yaml", "resume.md"]


def test_conflict_refuses(pdir):
    mtime = preferences_mtime_ns(pdir)
    (pdir / "preferences.yaml").write_text(PREFS + "target_titles: [X]\n", encoding="utf-8")
    os.utime(pdir / "preferences.yaml", ns=(mtime + 10**9, mtime + 10**9))
    with pytest.raises(ProfileConflict):
        save_profile_changes(pdir, {"hard.remote_ok": False}, expected_mtime_ns=mtime)
    assert "remote_ok: true" in text(pdir)


def test_validate_profile_data_no_disk(pdir):
    base = load_profile(pdir)
    data = profile_data(base)
    data["hard"]["salary_floor"] = {"amount": 70000, "period": "year"}
    after = validate_profile_data(data, base=base)
    assert after.hard.salary_floor.amount == 70000
    assert after.resume_text == base.resume_text
    assert after.scoring_version == base.scoring_version
    assert after.filter_version != base.filter_version
    data["soft"]["weights"]["skills"] = 5
    with pytest.raises(ProfileValidationError) as info:
        validate_profile_data(data, base=base)
    assert list(info.value.errors) == ["soft.weights"]
    assert text(pdir) == PREFS
