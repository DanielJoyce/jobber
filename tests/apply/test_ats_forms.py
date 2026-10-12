"""Form data per ATS (specs/022 "Testing", Form data): selectors find the fields on every
fixture, a renamed label is found by synonym, step and other controls are never Submit,
confirmation and error rules fire only where they should, and row 0 lists every hidden field."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
from selectolax.lexbor import LexborHTMLParser

from jobhunter.apply import ats_forms
from jobhunter.apply.ats_forms import Visibility, field_for, row0, variant_for

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "ats_forms"
FILES = sorted(p for p in FIXTURES.rglob("*") if p.is_file())

# Each variant: its fixture folder, a form URL, and a confirmation URL for URL rules.
VARIANTS = {
    "greenhouse_classic": (
        "greenhouse_classic",
        "https://boards.greenhouse.io/acme/jobs/4012345",
        "https://boards.greenhouse.io/acme/jobs/4012345/confirmation",
    ),
    "greenhouse_embed": (
        "greenhouse_classic",
        "https://boards.greenhouse.io/embed/job_app?for=acme&token=4012345",
        "https://boards.greenhouse.io/embed/job_app/confirmation?for=acme&token=4012345",
    ),
    "greenhouse_job_boards": (
        "greenhouse_job_boards",
        "https://job-boards.greenhouse.io/acme/jobs/4012345",
        "https://job-boards.greenhouse.io/acme/jobs/4012345/confirmation",
    ),
    "lever": (
        "lever",
        "https://jobs.lever.co/acme/0c0f2a5e-1b2c-4d3e-8f90-123456789abc/apply",
        "https://jobs.lever.co/acme/0c0f2a5e-1b2c-4d3e-8f90-123456789abc/thanks",
    ),
    "ashby": (
        "ashby",
        "https://jobs.ashbyhq.com/acme/0c0f2a5e-1b2c-4d3e-8f90-123456789abc/application",
        None,
    ),
    "ashby_embed": (
        "ashby",
        "https://jobs.ashbyhq.com/acme/0c0f2a5e-1b2c-4d3e-8f90-123456789abc/embed",
        None,
    ),
    "workable": (
        "workable",
        "https://apply.workable.com/acme/j/AB12CD34EF/apply/",
        None,
    ),
}
ALL = [v for f in ats_forms.FORMS.values() for v in f.variants]


def page(folder: str, name: str = "form.html") -> LexborHTMLParser:
    return LexborHTMLParser((FIXTURES / folder / name).read_text(encoding="utf-8"))


def css(tree: LexborHTMLParser, selector: str) -> list:
    """Matches of a selector, each element once (a selector list can repeat one)."""
    seen: dict[tuple, object] = {}
    for node in tree.css(selector):
        seen.setdefault((node.tag, node.html), node)
    return list(seen.values())


def test_every_variant_has_a_fixture_and_url():
    assert {v.name for v in ALL} == set(VARIANTS)


@pytest.mark.parametrize("name", sorted(VARIANTS))
def test_form_url_resolves_to_its_variant(name):
    _, url, _ = VARIANTS[name]
    v = variant_for(url)
    assert v is not None and v.name == name


@pytest.mark.parametrize("v", ALL, ids=lambda v: v.name)
def test_selectors_find_each_field_once_inside_the_form(v):
    tree = page(VARIANTS[v.name][0])
    forms = css(tree, v.form)
    assert len(forms) == 1, v.form
    form_html = forms[0].html
    for f in v.fields:
        found = css(tree, f.selector)
        assert len(found) == 1, f"{v.name}.{f.key}: {f.selector} matched {len(found)}"
        assert found[0].html in form_html, f"{f.key} is outside the form"
        if f.names:
            attrs = found[0].attributes
            assert {attrs.get("name"), attrs.get("id")} & set(f.names), f.key
        if f.helper == "attach":
            assert found[0].attributes.get("type") == "file", f.key
        assert f.answer is None or f.answer in ats_forms.ANSWER_KEYS, f.answer


@pytest.mark.parametrize("v", ALL, ids=lambda v: v.name)
def test_submit_is_one_control_and_no_other_control_is_it(v):
    tree = page(VARIANTS[v.name][0])
    submits = css(tree, v.submit)
    assert len(submits) == 1, v.submit
    submit = submits[0].html
    assert v.not_submit, "every variant names its other controls"
    for sel in v.not_submit:
        others = css(tree, sel)
        assert others, f"{sel} finds nothing on the fixture"
        assert all(o.html != submit for o in others), f"{sel} matches Submit"
    # The other controls' own words are not-submit words; Submit's are not.
    words = {w for w in ats_forms.NOT_SUBMIT_WORDS}
    label = (submits[0].attributes.get("value") or submits[0].text()).strip().casefold()
    assert label not in words


@pytest.mark.parametrize("v", ALL, ids=lambda v: v.name)
def test_errors_fire_on_the_error_page_only(v):
    folder = VARIANTS[v.name][0]
    err, form = page(folder, "error.html"), page(folder)
    assert any(css(err, s) for s in v.errors), "no error marker on error.html"
    assert not any(css(form, s) for s in v.errors), "an error marker on the clean form"


@pytest.mark.parametrize("v", ALL, ids=lambda v: v.name)
def test_confirmation_rules_fire_only_on_confirmation(v):
    folder, form_url, done_url = VARIANTS[v.name]
    assert v.confirmation and v.rule_version
    for rule in v.confirmation:
        if rule.kind == "url":
            assert done_url, "a URL rule needs a confirmation URL vector"
            assert re.search(rule.pattern, _path(done_url))
            assert not re.search(rule.pattern, _path(form_url))
        else:
            assert css(page(folder, "confirmation.html"), rule.pattern)
            assert not css(page(folder), rule.pattern)
            assert not css(page(folder, "confirmation.html"), v.form), "the form must go"


def _path(url: str) -> str:
    from urllib.parse import urlsplit

    return urlsplit(url).path


@pytest.mark.parametrize("v", [v for v in ALL if v.attach_ok or v.parse_done], ids=lambda v: v.name)
def test_parse_and_attach_signals_fire_after_the_upload_only(v):
    folder = VARIANTS[v.name][0]
    after, before = page(folder, "parsed.html"), page(folder)
    for sig in (v.attach_ok, v.parse_done):
        if sig is None:
            continue
        if sig.kind == "element_present":
            assert css(after, sig.pattern) and not css(before, sig.pattern)
        elif sig.kind == "field_populated":
            assert css(after, sig.pattern)[0].attributes.get("value")
            assert not css(before, sig.pattern)[0].attributes.get("value")


def test_ashby_parse_control_is_the_autofill_box_not_the_resume_field():
    v = variant_for(
        "https://jobs.ashbyhq.com/acme/0c0f2a5e-1b2c-4d3e-8f90-123456789abc/application"
    )
    roles = {f.role: f for f in v.fields if f.role}
    assert roles["parse_control"].selector != roles["resume"].selector
    assert "autofill" in roles["parse_control"].selector


# ─── field mapping ─────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "label, answer",
    [
        ("Given name", "contact.first_name"),
        ("Surname", "contact.last_name"),
        ("E-mail address (required)", "contact.email"),
        ("Mobile number", "contact.phone"),
        ("State / Province", "contact.region"),
        ("LinkedIn profile URL:", "links.linkedin"),
        ("Full Name *", "contact.full_name"),
        ("Location (City)", "contact.city"),
        ("Are you legally authorized to work in the United States?", "work_authorization"),
        ("Favourite colour", None),
        ("Name of your previous manager", None),  # contains "name": not a synonym
        ("City you would like to work in", None),
    ],
)
def test_generic_labels_map_by_synonym_only_on_a_whole_label(label, answer):
    assert field_for(label).answer == answer


def test_renamed_label_on_a_known_ats_still_maps_by_field_name():
    v = variant_for("https://boards.greenhouse.io/acme/jobs/4012345")
    m = field_for("Your given name", names=("job_application[first_name]",), variant=v)
    assert (m.answer, m.via, m.field_key) == ("contact.first_name", "field", "first_name")
    # A custom question with a generated name falls back to the label.
    m = field_for("LinkedIn Profile", names=("job_application[answers_attributes][0][text_value]",),
                  variant=v)  # fmt: skip
    assert (m.answer, m.via) == ("links.linkedin", "synonym")


@pytest.mark.parametrize(
    "label, names",
    [
        ("Street address", ("addr",)),
        ("Desired salary", ("pay",)),
        ("Email", ("salary_expectation",)),  # a never-store name beats the contact label
        ("Address", ("address",)),
    ],
)
def test_never_store_beats_every_mapping(label, names):
    v = variant_for("https://apply.workable.com/acme/j/AB12CD34EF/apply/")
    m = field_for(label, names=names, variant=v)
    assert m.via == "never_store" and m.answer is None


def test_generic_fixture_labels():
    tree = page("generic")
    got = {}
    for lab in tree.css("label"):
        target = tree.css_first(f"#{lab.attributes['for']}")
        got[target.attributes["name"]] = field_for(lab.text(), names=(target.attributes["name"],))
    assert got["fname"].answer == "contact.first_name"
    assert got["mail"].answer == "contact.email"
    assert got["addr"].via == "never_store"
    assert got["pay"].via == "never_store"
    assert got["colour"].via == "none"


# ─── row 0 ─────────────────────────────────────────────────────────────────

SHOWN = dict(
    check_visibility=True,
    width=200,
    height=30,
    in_scroll_area=True,
    aria_hidden=False,
    inert=False,
    disabled=False,
    read_only=False,
)


def test_row0_passes_a_visible_editable_field():
    assert row0(Visibility(**SHOWN)) is None


@pytest.mark.parametrize(
    "case, change",
    [
        ("display none honeypot email2", {"check_visibility": False}),
        ("opacity 0", {"check_visibility": False}),
        ("off-screen", {"in_scroll_area": False}),
        ("zero size", {"width": 0, "height": 0}),
        ("3 px tall", {"height": 3}),
        ("aria-hidden block", {"aria_hidden": True}),
        ("inert ancestor", {"inert": True}),
        ("disabled", {"disabled": True}),
        ("readOnly", {"read_only": True}),
        ("fact missing", {"inert": None}),
        ("size missing", {"width": None}),
    ],
)
def test_row0_lists_every_hidden_or_locked_field(case, change):
    assert row0(Visibility(**{**SHOWN, **change})) is not None, case


def test_row0_lists_a_field_with_no_facts_and_refuses_unknown_facts():
    assert row0(None) is not None
    assert row0(Visibility()) is not None
    with pytest.raises(ValueError):
        Visibility(**SHOWN, visible_trust_me=True)


# ─── the payload the worker caches ─────────────────────────────────────────

EVENT_WORDS = re.compile(
    r"\b(click|mousedown|mouseup|pointerdown|pointerup|keydown|keyup|submit\(|dispatch|"
    r"requestSubmit|dragenter|dragover|drop)\b",
    re.I,
)


def _strings(obj):
    if isinstance(obj, dict):
        for k, v in obj.items():
            yield str(k)
            yield from _strings(v)
    elif isinstance(obj, (list, tuple)):
        for v in obj:
            yield from _strings(v)
    elif isinstance(obj, str):
        yield obj


def test_forms_payload_is_json_and_names_no_event_types():
    payload = ats_forms.forms_payload()
    json.dumps(payload)
    assert payload["version"] == ats_forms.FORMS_VERSION
    assert set(payload["ats"]) == {"greenhouse", "lever", "ashby", "workable"}
    assert "synonyms" in payload["generic"] and "selector" not in json.dumps(payload["generic"])
    for s in _strings(payload):
        # "drop" is an attach method name, not an event, and appears only there.
        if s == "drop":
            continue
        assert not EVENT_WORDS.search(s), s


def test_variant_hosts_are_the_ats_rules_hosts():
    from jobhunter.pipeline.ats_rules import ATS_RULES

    hosts = {r.name: set(r.hosts) for r in ATS_RULES}
    for form in ats_forms.FORMS.values():
        for v in form.variants:
            assert set(v.hosts) <= hosts[form.ats], v.name


# ─── fixtures hold no personal data ────────────────────────────────────────

EMAIL = re.compile(r"[\w.+-]+@([\w-]+\.)+[a-z]{2,}", re.I)
PHONE = re.compile(r"\(?\b\d{3}\)?[\s.-]\d{3}[\s.-]\d{4}\b")


@pytest.mark.parametrize("path", FILES, ids=lambda p: str(p.relative_to(FIXTURES)))
def test_ats_form_fixture_has_no_personal_data(path):
    text = path.read_text(encoding="utf-8")
    for m in EMAIL.finditer(text):
        assert m.group(0).lower().endswith("@example.com"), m.group(0)
    assert not PHONE.search(text)
