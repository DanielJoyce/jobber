"""Per-rule tests on real row shapes (specs/021 "Testing": per rule, follow-up, classifier and
bulk, time zone, weeks)."""

from __future__ import annotations

from datetime import timedelta

from achv_world import NOW, TZ, local

from jobhunter.achievements import catalog, engine, facts
from jobhunter.apply import packets
from jobhunter.console import inbox, tracking

# --- definitions mirror the console's constants ---------------------------------------------


def test_mirrored_constants_agree_with_the_console():
    assert facts.SENT_STATUSES == packets.SENT_STATUSES
    assert facts.EARLY_STATUSES == tracking.EARLY_STATUSES
    assert facts.AUTO_NOTES == tracking.AUTO_NOTES
    assert facts.SHORTLIST_NOTE == inbox.SHORTLIST_NOTE


# --- sending ----------------------------------------------------------------------------------


def test_email_sourced_sends_count(world):
    for day in ("2026-09-22", "2026-09-23", "2026-09-24", "2026-09-25", "2026-09-26"):
        world.app(f"{day} 10:00")
    assert world.progress("apply.first") == 5
    assert ("apply.momentum", 1) in world.reached()


def test_straight_to_acknowledged_is_sent(world):
    world.app("2026-09-22 10:00", status="acknowledged")
    assert ("apply.first", 1) in world.reached()


def test_merge_events_count_for_nothing(world):
    a = world.app()
    note = "Merged duplicate application (job 4 into 3)"
    world.event(a, "applied", "2026-08-01 10:00", note=note)
    world.event(a, "interview", "2026-09-10 10:00", note=note)
    s = world.series()
    assert s["apply.first"].progress == 0
    assert s["out.interview"].progress == 0
    assert s["out.long_game"].progress == 0


def test_six_sends_in_one_day_count_three_for_momentum(world):
    for h in range(6):
        world.app(f"2026-09-22 {9 + h:02d}:00")
    assert world.progress("apply.momentum") == 3
    assert world.progress("apply.first") == 6  # they all exist; they just do not move it


def test_sent_date_is_the_local_day(world):
    # 01:30 UTC on the 23rd is 18:30 on the 22nd in Los Angeles: same local day as the rest.
    for h in range(3):
        world.app(f"2026-09-22 {9 + h:02d}:00")
    a = world.app()
    world.conn.execute(
        "INSERT INTO application_event (application_id, at, status, source) "
        "VALUES (?, '2026-09-23T01:30:00+00:00', 'applied', 'email')",
        (a,),
    )
    assert world.progress("apply.momentum") == 3


# --- targeted and tailored --------------------------------------------------------------------


def test_only_automatic_early_events_are_not_targeted(world):
    a = world.app()
    world.event(a, "interested", "2026-09-20 10:00", note=facts.APPLY_CLICK_NOTE)
    world.event(a, "applied", "2026-09-22 10:00", source="email")
    assert world.progress("apply.targeted") == 0


def test_not_yet_then_yes_is_not_targeted(world):
    a = world.app()
    world.event(a, "preparing", "2026-09-20 10:00", note=facts.NOT_YET_NOTE)
    world.event(a, "applied", "2026-09-22 10:00", note="marked applied")
    assert world.progress("apply.targeted") == 0


def test_shortlist_and_send_on_the_same_day_is_not_targeted(world):
    a = world.app()
    world.event(a, "interested", "2026-09-22 09:00", note=facts.SHORTLIST_NOTE)
    world.event(a, "applied", "2026-09-22 15:00", source="email")
    assert world.progress("apply.targeted") == 0


def test_single_shortlist_on_an_earlier_day_is_targeted(world):
    a = world.app()
    world.event(a, "interested", "2026-09-21 09:00", note=facts.SHORTLIST_NOTE)
    world.event(a, "applied", "2026-09-22 15:00", source="email")
    assert world.progress("apply.targeted") == 1
    assert world.progress("search.shortlist") == 1


def test_manual_move_and_packet_start_are_deliberate(world):
    a = world.app()
    world.event(a, "preparing", "2026-09-20 09:00", note="moved from interested")
    world.event(a, "applied", "2026-09-22 15:00")
    b = world.app()
    world.event(b, "preparing", "2026-09-20 09:00", note=facts.PREPARE_NOTE)
    world.event(b, "applied", "2026-09-23 15:00")
    assert world.progress("apply.targeted") == 2


def test_bulk_shortlist_then_sends_is_zero_targeted(world):
    batch = "2026-09-14 09:00"
    apps = [world.app() for _ in range(20)]
    for a in apps:
        world.event(a, "interested", batch, note=facts.SHORTLIST_NOTE)
    for i, a in enumerate(apps):
        world.event(a, "applied", f"2026-09-{15 + i // 3:02d} {9 + i % 3:02d}:00", source="email")
    s = world.series()
    assert s["apply.targeted"].progress == 0
    assert s["search.shortlist"].progress == 0


def test_fit_score_alone_is_not_targeted(world):
    a = world.app("2026-09-22 10:00")
    world.conn.execute(
        "INSERT INTO fit_score (job_group_id, tier, model, prompt_version, scoring_version, "
        "verdict, overall, dimensions, evidence, created_at) "
        "VALUES (?, 'deep', 'm', 'p', 's', 'strong', 95, '{}', '[]', '2026-09-20T00:00:00Z')",
        (world.group_of(a),),
    )
    assert world.progress("apply.targeted") == 0


def test_packet_ref_makes_a_tailored_application(world):
    a = world.app()
    pid, v = world.packet(a, "generated")
    world.conn.execute(
        "UPDATE application SET resume_version = ? WHERE id = ?", (packets.packet_ref(pid, v), a)
    )
    world.event(a, "applied", "2026-09-22 10:00")
    s = world.series()
    assert s["apply.tailored"].progress == 1
    assert s["apply.targeted"].progress == 1
    assert s["apply.own_words"].progress == 0


def test_typed_packet_ref_with_no_such_packet_is_not_tailored(world):
    world.app("2026-09-22 10:00", resume_version="packet:9/resume/v1")
    assert world.progress("apply.tailored") == 0


def test_packet_of_another_application_is_not_tailored(world):
    other = world.app()
    pid, v = world.packet(other)
    world.app("2026-09-22 10:00", resume_version=packets.packet_ref(pid, v))
    assert world.progress("apply.tailored") == 0


def test_typed_resume_version_with_the_packet_attachment_is_tailored(world):
    a = world.app(resume_version="resume-2026-fall.pdf")
    pid, v = world.packet(a, "edited", cover=True)
    world.attach(a, "resume", f"packets/{pid}/resume-v{v}.pdf")
    world.attach(a, "cover_letter", f"packets/{pid}/cover_letter-v1.pdf")
    world.event(a, "applied", "2026-09-22 10:00")
    s = world.series()
    assert s["apply.tailored"].progress == 1
    assert s["apply.own_words"].progress == 1
    assert s["apply.cover"].progress == 1


def test_cover_needs_the_letter_recorded(world):
    a = world.app()
    pid, v = world.packet(a, cover=True)
    world.conn.execute(
        "UPDATE application SET resume_version = ? WHERE id = ?", (packets.packet_ref(pid, v), a)
    )
    world.event(a, "applied", "2026-09-22 10:00")
    assert world.progress("apply.cover") == 0
    world.conn.execute(
        "UPDATE application SET cover_letter_path = ? WHERE id = ?",
        (f"packets/{pid}/cover_letter-v1.pdf", a),
    )
    assert world.progress("apply.cover") == 1


def test_base_resume_sent_is_not_tailored(world):
    a = world.app()
    pid, v = world.packet(a, "base")
    world.conn.execute(
        "UPDATE application SET resume_version = ? WHERE id = ?", (packets.packet_ref(pid, v), a)
    )
    world.event(a, "applied", "2026-09-22 10:00")
    assert world.progress("apply.tailored") == 0


# --- search --------------------------------------------------------------------------------


def test_bulk_triage_of_300_moves_decisive_by_20(world):
    for _ in range(300):
        world.label("2026-09-22 10:00", "not_interesting")
    s = world.series()
    assert s["search.decide"].progress == 20
    assert s["search.no"].progress == 5


def test_found_it_yourself_needs_an_added_capture(world):
    world.capture("2026-09-22 10:00", "previewed")
    assert world.progress("search.found") == 0
    world.capture("2026-09-22 11:00", "linked")
    assert world.progress("search.found") == 1


# --- follow-up -----------------------------------------------------------------------------


def test_follow_up_pairs_are_distinct_and_capped(world):
    a = world.app("2026-09-01 10:00")
    world.followup(a, "planned", "2026-09-22 10:00", "2026-09-29")
    world.followup(a, "planned", "2026-09-22 11:00", "2026-09-30")  # same app, same day
    world.followup(a, "done", "2026-09-29 10:00", "2026-09-29")
    world.followup(a, "planned", "2026-09-29 11:00", "2026-09-29")  # the same pair again
    world.followup(a, "done", "2026-09-29 12:00", "2026-09-29")
    s = world.series()
    assert s["follow.plan"].progress == 1
    assert s["follow.done"].progress == 1


def test_migration_backfill_plans_earn_nothing(world):
    a = world.app("2026-09-01 10:00")
    world.followup(a, "planned", "2026-09-22 10:00", "2026-09-29", backfill=1)
    assert world.progress("follow.plan") == 0
    assert all(r[0] != "followup_log" for refs in world_active(world).values() for r in refs)


def world_active(world):
    return facts.load(world.conn, NOW, TZ).active


def test_done_on_the_due_day_with_a_date_only_due_is_on_time(world):
    # Done at 20:00 local on the 29th is 03:00 UTC on the 30th: still on time locally.
    for i in range(5):
        a = world.app("2026-09-01 10:00")
        world.followup(a, "planned", f"2026-09-2{i} 10:00", "2026-09-29")
        world.followup(a, "done", f"2026-09-{25 + i} 20:00", "2026-09-29")
    assert world.progress("follow.on_time") == 5
    late = world.app("2026-09-01 10:00")
    world.followup(late, "planned", "2026-09-20 10:00", "2026-09-29")
    world.followup(late, "done", "2026-09-30 09:00", "2026-09-29")
    assert world.progress("follow.on_time") == 5


def test_tracking_follow_up_writes_feed_the_rules(world):
    a = world.app("2026-09-01 10:00")
    when = local("2026-09-22 10:00")
    tracking.set_next_action(world.conn, a, "Email the recruiter", "2026-09-29", now=when)
    tracking.set_next_action(world.conn, a, "Email the recruiter again", "2026-09-29", now=when)
    tracking.done(world.conn, a, local("2026-09-29 09:00"))
    s = world.series()
    assert s["follow.plan"].progress == 1
    assert s["follow.done"].progress == 1


def test_tidy_and_lessons(world):
    apps = [world.app(f"2026-08-0{i + 1} 10:00") for i in range(5)]
    for i, a in enumerate(apps):
        tracking.no_response(world.conn, a, local(f"2026-09-2{i} 10:00"))
    s = world.series()
    assert s["follow.tidy"].progress == 5
    assert s["follow.lessons"].progress == 0  # "marked from follow-ups" is not your note
    for i, a in enumerate(apps[:3]):
        world.event(a, "withdrawn", f"2026-09-2{i} 12:00", note="Pay band was lower than posted")
    world.event(apps[3], "rejected", "2026-09-23 12:00", note="moved from no_response")
    assert world.progress("follow.lessons") == 3


def test_networker_counts_contacts_dated_or_not(world):
    a = world.app("2026-09-01 10:00")
    world.contact(a, None)
    world.contact(a, "2026-09-22 10:00")
    assert world.progress("follow.contact") == 2
    # a contact from before the migration is not an active day
    days = world_active(world)
    assert sum(1 for refs in days.values() for t, _ in refs if t == "contact") == 1


def test_classifier_rejections_alone_do_not_close_the_loop(world):
    world.rejection("2026-09-22 10:00")  # mail match wrote it, confirmed by itself
    assert world.progress("follow.loop") == 0
    world.rejection("2026-09-23 10:00", state="pending")
    world.conn.execute(
        "UPDATE rejection SET state = 'confirmed', confirmed_at = ? WHERE state = 'pending'",
        ("2026-09-24T17:00:00+00:00",),
    )
    assert world.progress("follow.loop") == 1


def test_manual_rejection_and_manual_rejected_event_close_the_loop(world):
    world.rejection("2026-09-22 10:00", source="manual")
    assert world.progress("follow.loop") == 1
    a = world.app("2026-09-01 10:00")
    world.event(a, "rejected", "2026-09-25 10:00")
    assert world.progress("follow.loop") == 2


# --- outcomes ------------------------------------------------------------------------------


def test_outcomes(world):
    a = world.app("2026-08-01 10:00")
    world.event(a, "screening", "2026-08-10 10:00", source="email")
    b = world.app("2026-08-02 10:00")
    world.event(b, "interview", "2026-09-05 10:00")
    s = world.series()
    assert s["out.heard_back"].progress == 2
    assert s["out.interview"].progress == 1
    assert s["out.long_game"].progress == 1  # b: 34 days after sending
    assert s["out.offer"].progress == 0


def test_bounce_back_within_two_weeks_of_a_no(world):
    world.rejection("2026-09-01 10:00")
    world.app("2026-09-20 10:00")  # 19 days later: too late
    assert world.progress("out.bounce_back") == 0
    world.app("2026-09-14 10:00")
    assert world.progress("out.bounce_back") == 1


# --- weeks ---------------------------------------------------------------------------------


def two_days(world, monday: str, days: tuple[int, ...] = (0, 2)) -> None:
    """Prefs saves (active actions) on the given weekdays of the week starting ``monday``."""
    start = local(f"{monday} 10:00")
    for d in days:
        world.prefs_save((start + timedelta(days=d)).strftime("%Y-%m-%d %H:%M"))


def test_week_good_on_tuesday_then_six_untargeted_sends_is_no_quality_week(world):
    for day in ("2026-09-22", "2026-09-23"):
        a = world.app()
        world.event(a, "interested", f"2026-09-15 0{day[-1]}:00", note=facts.SHORTLIST_NOTE)
        world.event(a, "applied", f"{day} 10:00")
    assert world.progress("apply.quality_week") == 1
    for h in range(6):
        world.app(f"2026-09-26 {9 + h:02d}:00")
    assert world.progress("apply.quality_week") == 0


def test_current_week_gives_no_rhythm_or_rest_credit(world):
    two_days(world, "2026-09-21")
    two_days(world, "2026-09-28")
    two_days(world, "2026-10-05")  # in progress
    s = world.series()
    assert s["rhythm.weeks"].progress == 2
    assert s["rhythm.rest"].progress == 2
    nxt = NOW + timedelta(days=1)  # Monday: the week has ended
    s = world.series(nxt)
    assert s["rhythm.weeks"].progress == 3
    assert s["rhythm.rest"].progress == 3


def test_weeks_split_at_monday_midnight_local(world):
    # Sunday 23:30 local is Monday 06:30 UTC: still the earlier week.
    two_days(world, "2026-09-21", (0,))
    world.prefs_save("2026-09-27 23:30")
    assert world.progress("rhythm.weeks") == 1


def test_one_day_week_is_missed(world):
    two_days(world, "2026-09-14")
    two_days(world, "2026-09-21", (0,))
    two_days(world, "2026-09-28")
    assert world.progress("rhythm.weeks") == 1


def test_freezes_earned_at_four_and_capped_at_two(world):
    weeks = ["2026-06-01", "2026-06-08", "2026-06-15", "2026-06-22"]  # 4 active: 1 freeze
    for w in weeks:
        two_days(world, w)
    # Jun 29 missed (freeze used), then Jul 6 active: the run goes on to 5.
    two_days(world, "2026-07-06")
    assert world.progress("rhythm.weeks") == 5
    # Then two misses with no freeze left: the run ends.
    two_days(world, "2026-07-27")
    f = facts.load(world.conn, NOW, TZ)
    from jobhunter.achievements import rules

    status = rules.Rules(f).rhythm_status()
    assert status.best == 5
    assert status.run == 0  # after Jul 27, many missed weeks: paused


def test_freeze_cap(world):
    start = local("2026-01-05 10:00")
    for i in range(12):  # 12 active weeks: 3 freezes earned, 2 held
        two_days(world, (start + timedelta(weeks=i)).strftime("%Y-%m-%d"))
    f = facts.load(world.conn, local("2026-03-30 12:00"), TZ)
    from jobhunter.achievements import rules

    assert rules.Rules(f).rhythm_status().freezes == 2


def test_comeback_needs_an_earlier_active_week(world):
    two_days(world, "2026-09-28")  # the first active week ever: a start, not a comeback
    assert world.progress("rhythm.comeback") == 0
    two_days(world, "2026-09-07")  # now there was an earlier one, and two missed weeks between
    assert world.progress("rhythm.comeback") == 1


def test_backdated_manual_event_does_not_fill_a_past_week(world):
    two_days(world, "2026-09-21", (0,))
    a = world.app()
    world.event(a, "applied", "2026-09-23 10:00", recorded="2026-10-06 10:00")
    assert world.progress("rhythm.weeks") == 0
    # but momentum and the other send-date rules keep the real date
    assert facts.load(world.conn, NOW, TZ).day(world.series()["apply.first"].items[0].at) == (
        local("2026-09-23 10:00").date()
    )


def test_every_catalog_entry_has_a_rule(world):
    s = world.series()
    assert set(s) == {a.id for a in catalog.CATALOG}
    assert len(catalog.CATALOG) == 32
    assert sum(a.tiers for a in catalog.CATALOG) == 54


def test_all_set_follows_the_essentials_input(world):
    assert engine.compute(world.conn, NOW, TZ, essentials=True).series["setup.ready"].progress == 1
    assert engine.compute(world.conn, NOW, TZ, essentials=False).series["setup.ready"].progress == 0
    unknown = engine.compute(world.conn, NOW, TZ)
    assert unknown.series["setup.ready"].progress == 0
    assert unknown.unknown == {"setup.ready"}
