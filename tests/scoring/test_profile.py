"""Profile loading, versioning and model input (specs/006, specs/014). Synthetic data only."""

from __future__ import annotations

import re
import shutil
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from ruamel.yaml import YAML

from jobhunter.scoring.profile import ProfileError, load_profile, scoring_inputs

REPO_ROOT = Path(__file__).resolve().parents[2]
EXAMPLE = REPO_ROOT / "examples" / "preferences.example.yaml"

RESUME = "Synthetic Person\n\n## Experience\n- Ran a small fleet of synthetic test hosts.\n"


def _write_profile(tmp_path: Path, yaml_text: str | None = None, resume: str = RESUME) -> Path:
    profile_dir = tmp_path / "profile"
    profile_dir.mkdir(exist_ok=True)
    text = EXAMPLE.read_text(encoding="utf-8") if yaml_text is None else yaml_text
    (profile_dir / "preferences.yaml").write_text(text, encoding="utf-8")
    (tmp_path / "resume.md").write_text(resume, encoding="utf-8")
    return profile_dir


def _edit(profile_dir: Path, mutate: Callable[[Any], None]) -> None:
    """Round-trip the YAML with ruamel, apply a mutation, and write it back."""
    yaml = YAML(typ="rt")
    path = profile_dir / "preferences.yaml"
    data = yaml.load(path.read_text(encoding="utf-8"))
    mutate(data)
    with path.open("w", encoding="utf-8") as fh:
        yaml.dump(data, fh)


def _load(tmp_path: Path, profile_dir: Path):
    return load_profile(profile_dir, resume_path=tmp_path / "resume.md")


def test_example_loads(tmp_path):
    profile_dir = _write_profile(tmp_path)
    profile = _load(tmp_path, profile_dir)

    assert profile.hard.states_allowed == ["CO", "NM", "UT", "WA"]
    assert profile.soft.state_ranking == ["CO", "UT", "WA", "NM"]
    assert profile.current_focus is not None
    assert profile.current_focus.since == "2023-01"
    assert profile.load_warnings == ()
    assert re.fullmatch(r"[0-9a-f]{64}", profile.filter_version)
    assert re.fullmatch(r"[0-9a-f]{64}", profile.scoring_version)
    assert profile.filter_version != profile.scoring_version


def test_comment_only_edit_keeps_both_hashes(tmp_path):
    profile_dir = _write_profile(tmp_path)
    before = _load(tmp_path, profile_dir)

    original = (profile_dir / "preferences.yaml").read_text(encoding="utf-8")
    commented = "# A new leading comment\n" + original.replace(
        "hard:\n", "hard:  # inline comment\n"
    ).replace("\n\n", "\n# spacer comment\n\n")
    (profile_dir / "preferences.yaml").write_text(commented, encoding="utf-8")
    after = _load(tmp_path, profile_dir)

    assert after.filter_version == before.filter_version
    assert after.scoring_version == before.scoring_version


def test_key_order_does_not_change_hashes(tmp_path):
    doc = (
        "hard: {states_allowed: [CO, NM], remote_ok: true}\n"
        "narrative: {want: Small teams}\n"
        "current_focus: {since: 2023-01, doing: Systems work}\n"
    )
    reordered = (
        "current_focus: {since: 2023-01, doing: Systems work}\n"
        "narrative: {want: Small teams}\n"
        "hard: {states_allowed: [CO, NM], remote_ok: true}\n"
    )
    profile_dir = _write_profile(tmp_path, doc)
    before = _load(tmp_path, profile_dir)

    (profile_dir / "preferences.yaml").write_text(reordered, encoding="utf-8")
    after = _load(tmp_path, profile_dir)

    assert after.filter_version == before.filter_version
    assert after.scoring_version == before.scoring_version


def test_salary_floor_edit_changes_filter_version_only(tmp_path):
    profile_dir = _write_profile(tmp_path)
    before = _load(tmp_path, profile_dir)

    _edit(profile_dir, lambda d: d["hard"]["salary_floor"].update(amount=98765))
    after = _load(tmp_path, profile_dir)

    assert after.hard.salary_floor is not None
    assert after.hard.salary_floor.amount == 98765
    assert after.filter_version != before.filter_version
    assert after.scoring_version == before.scoring_version


def test_done_with_edit_changes_scoring_version_only(tmp_path):
    profile_dir = _write_profile(tmp_path)
    before = _load(tmp_path, profile_dir)

    _edit(profile_dir, lambda d: d["current_focus"]["done_with"].append("synthetic thing"))
    after = _load(tmp_path, profile_dir)

    assert after.scoring_version != before.scoring_version
    assert after.filter_version == before.filter_version


def test_resume_text_edit_changes_scoring_version_only(tmp_path):
    profile_dir = _write_profile(tmp_path)
    before = _load(tmp_path, profile_dir)

    (tmp_path / "resume.md").write_text(RESUME + "- Added a synthetic role.\n", encoding="utf-8")
    after = _load(tmp_path, profile_dir)

    assert after.scoring_version != before.scoring_version
    assert after.filter_version == before.filter_version


def test_weights_not_summing_to_one_rejected(tmp_path):
    profile_dir = _write_profile(tmp_path)
    _edit(profile_dir, lambda d: d["soft"]["weights"].update(skills=0.5))

    with pytest.raises(ProfileError) as info:
        _load(tmp_path, profile_dir)

    message = str(info.value)
    assert "preferences.yaml" in message
    assert "soft.weights" in message
    assert "sum to 1.0" in message


def test_weights_within_tolerance_accepted(tmp_path):
    profile_dir = _write_profile(tmp_path)
    _edit(profile_dir, lambda d: d["soft"]["weights"].update(skills=0.305))

    profile = _load(tmp_path, profile_dir)

    assert profile.soft.weights.skills == pytest.approx(0.305)


def test_resume_directory_with_one_md_file_works(tmp_path):
    profile_dir = _write_profile(tmp_path)
    resume_dir = tmp_path / "resume"
    resume_dir.mkdir()
    (resume_dir / "resume.md").write_text(RESUME, encoding="utf-8")
    (resume_dir / "notes.txt").write_text("ignored", encoding="utf-8")

    profile = load_profile(profile_dir, resume_path=resume_dir)

    assert profile.resume_file == resume_dir / "resume.md"
    assert profile.resume_text == RESUME


def test_resume_directory_with_no_md_file_is_an_error(tmp_path):
    profile_dir = _write_profile(tmp_path)
    resume_dir = tmp_path / "resume"
    resume_dir.mkdir()
    (resume_dir / "notes.txt").write_text("not markdown", encoding="utf-8")

    with pytest.raises(ProfileError, match=r"no \.md file"):
        load_profile(profile_dir, resume_path=resume_dir)


def test_resume_directory_with_two_md_files_is_an_error(tmp_path):
    profile_dir = _write_profile(tmp_path)
    resume_dir = tmp_path / "resume"
    resume_dir.mkdir()
    (resume_dir / "a.md").write_text(RESUME, encoding="utf-8")
    (resume_dir / "b.md").write_text(RESUME, encoding="utf-8")

    with pytest.raises(ProfileError, match=r"exactly one \.md file, found 2"):
        load_profile(profile_dir, resume_path=resume_dir)


def test_resume_path_from_yaml_resolves_against_profile_dir(tmp_path):
    profile_dir = _write_profile(tmp_path)
    shutil.move(str(tmp_path / "resume.md"), str(profile_dir / "resume.md"))
    _edit(profile_dir, lambda d: d.update(resume_path="resume.md"))

    profile = load_profile(profile_dir)

    assert profile.resume_file == profile_dir / "resume.md"


def test_scoring_inputs_excludes_salary_and_states(tmp_path):
    profile_dir = _write_profile(tmp_path)
    _edit(
        profile_dir,
        lambda d: (
            d["hard"]["salary_floor"].update(amount=98765),
            d["hard"]["home"].update(state="NM"),
            d["hard"]["states_allowed"].append("AZ"),
            d["soft"]["state_ranking"].append("OR"),
        ),
    )
    profile = _load(tmp_path, profile_dir)

    text = scoring_inputs(profile)

    assert "98765" not in text
    assert "salary" not in text.casefold()
    for code in ("NM", "CO", "UT", "WA", "AZ", "OR"):
        assert not re.search(rf"\b{code}\b", text), code
    assert "Synthetic Person" in text
    assert "Done with" in text
    assert "### Avoid" in text


def test_scoring_inputs_is_deterministic(tmp_path):
    profile_dir = _write_profile(tmp_path)
    profile = _load(tmp_path, profile_dir)

    assert scoring_inputs(profile) == scoring_inputs(profile)


def test_states_allowed_all_accepted(tmp_path):
    profile_dir = _write_profile(tmp_path)
    _edit(profile_dir, lambda d: d["hard"].update(states_allowed="all"))

    profile = _load(tmp_path, profile_dir)

    assert profile.hard.states_allowed == "all"


def test_state_names_normalize_to_codes(tmp_path):
    profile_dir = _write_profile(tmp_path)
    _edit(profile_dir, lambda d: d["hard"].update(states_allowed=["Colorado", "new mexico"]))

    profile = _load(tmp_path, profile_dir)

    assert profile.hard.states_allowed == ["CO", "NM"]


def test_unknown_top_level_key_is_warned_not_rejected(tmp_path):
    profile_dir = _write_profile(tmp_path, "version_note: synthetic\n" + EXAMPLE.read_text())

    profile = _load(tmp_path, profile_dir)

    assert any("version_note" in warning for warning in profile.load_warnings)


def test_unknown_nested_key_is_rejected(tmp_path):
    profile_dir = _write_profile(tmp_path)
    _edit(profile_dir, lambda d: d["hard"].update(salray_floor={"amount": 1}))

    with pytest.raises(ProfileError) as info:
        _load(tmp_path, profile_dir)

    assert "hard.salray_floor" in str(info.value)


def test_unset_values_use_defaults(tmp_path):
    doc = "hard: {states_allowed: null, salary_floor: {amount: null}}\nqueries: []\n"
    profile_dir = _write_profile(tmp_path, doc)

    profile = _load(tmp_path, profile_dir)

    assert profile.hard.states_allowed == "all"
    assert profile.hard.salary_floor is None
    assert profile.queries == []


def test_yaml_resume_path_relative_to_repo_root(tmp_path, monkeypatch):
    # Regression: preferences.yaml says resume_path: resume/<file>.md (relative to the repo,
    # where resume/ sits next to profile/). It used to resolve under profile/ and fail.
    from jobhunter.scoring.profile import load_profile

    repo = tmp_path / "repo"
    (repo / "profile").mkdir(parents=True)
    (repo / "resume").mkdir()
    (repo / "resume" / "Pat Example Resume.md").write_text("# Pat Example\n")
    src = (
        Path(__file__).resolve().parents[2] / "examples" / "preferences.example.yaml"
    ).read_text()
    (repo / "profile" / "preferences.yaml").write_text(
        src.replace("resume_path: null", 'resume_path: "resume/Pat Example Resume.md"')
    )
    monkeypatch.chdir(tmp_path)  # cwd is NOT the repo, so only the parent fallback works
    profile = load_profile(repo / "profile")
    assert profile.resume_text.startswith("# Pat Example")
