"""The achievement catalog: ids, names, categories, tiers and copy (specs/021 "The catalog").

Static data only. The rules that decide each one live in ``rules.py``; this module says what
the board shows. Descriptions and hints never mention scoring, generating, or sending more
applications (a test checks every string against a deny list).

``setup.ready`` (All set) takes 019's ``essentials_ok`` from the caller; see ``engine.evaluate``.
"""

from __future__ import annotations

from dataclasses import dataclass

# Bumped when a rule changes meaning; stored on each unlock (specs/021 "Revocation").
CATALOG_VERSION = 1

# (key, label), in board order.
CATEGORIES: tuple[tuple[str, str], ...] = (
    ("setup", "Setup"),
    ("search", "Search"),
    ("apply", "Apply"),
    ("follow", "Follow-up"),
    ("out", "Outcomes"),
    ("rhythm", "Rhythm"),
)
CATEGORY_KEYS = tuple(k for k, _ in CATEGORIES)


@dataclass(frozen=True)
class Achievement:
    id: str
    name: str
    category: str
    description: str  # one-line plain-words rule for the card
    hint: str  # a healthy next action, shown while locked (never for hidden ones)
    thresholds: tuple[int, ...] = (1,)  # one per tier; a single achievement has one
    hidden: bool = False
    weekly: bool = False  # judged only on finished weeks

    @property
    def tiers(self) -> int:
        return len(self.thresholds)


CATALOG: tuple[Achievement, ...] = (
    # --- Setup
    Achievement(
        "setup.prefs",
        "Tuned in",
        "setup",
        "You saved your preferences on the Prefs page.",
        "Open Prefs and save what you are looking for.",
    ),
    Achievement(
        "setup.alerts",
        "Net cast",
        "setup",
        "Job alert sign-ups you marked as subscribed.",
        "Sign up for a job alert on the Alerts page.",
        (1, 5),
    ),
    Achievement(
        "setup.mail",
        "Inbox sorted",
        "setup",
        "You connected your mailbox with jobhunter mail setup.",
        "Run jobhunter mail setup to connect your mailbox.",
    ),
    Achievement(
        "setup.ready",
        "All set",
        "setup",
        "The essentials for jobhunter are in place: nothing required is missing.",
        "Finish the checklist on the Setup page.",
    ),
    # --- Search
    Achievement(
        "search.decide",
        "Decisive",
        "search",
        "Jobs you made a call on in the inbox. Saying no is a decision too.",
        "Go through a few jobs in the inbox and decide yes or no.",
        (10, 50, 150, 300),
    ),
    Achievement(
        "search.no",
        "Clear no",
        "search",
        "Jobs you marked not interesting.",
        "Mark a job that is not for you as not interesting.",
        (10,),
    ),
    Achievement(
        "search.shortlist",
        "Shortlist",
        "search",
        "Jobs you picked out one at a time to look at closely.",
        "Shortlist a job you would like to look at closely.",
        (5,),
    ),
    Achievement(
        "search.found",
        "Found it yourself",
        "search",
        "You added a job you found with the browser extension.",
        "Found a posting on your own? Add it with the browser extension.",
    ),
    # --- Apply
    Achievement(
        "apply.first",
        "First one out",
        "apply",
        "Your first application went out.",
        "Pick a job you like and apply.",
    ),
    Achievement(
        "apply.momentum",
        "Momentum",
        "apply",
        "Applications you sent, counting up to three a day.",
        "Keep a steady pace: a few good applications beat a pile of rushed ones.",
        (5, 10, 25, 50),
    ),
    Achievement(
        "apply.targeted",
        "Aimed, not sprayed",
        "apply",
        "Applications you shortlisted or prepared a packet for on an earlier day.",
        "Shortlist a job today and apply to it on another day.",
        (1, 5, 15),
    ),
    Achievement(
        "apply.tailored",
        "Tailor-made",
        "apply",
        "Applications sent with a packet tailored to the job.",
        "Prepare a packet, review it, mark it ready, then apply with it.",
        (1, 5, 10),
    ),
    Achievement(
        "apply.own_words",
        "In your own words",
        "apply",
        "You sent a tailored resume you edited yourself.",
        "Edit your packet's resume so it sounds like you, then apply with it.",
    ),
    Achievement(
        "apply.cover",
        "Dear hiring manager",
        "apply",
        "You sent a tailored packet with a cover letter.",
        "Add a cover letter to a packet before you apply with it.",
    ),
    Achievement(
        "apply.quality_week",
        "Quality week",
        "apply",
        "A week with two to five applications, every one of them aimed.",
        "Plan a week around a few jobs you prepared for in advance.",
        weekly=True,
    ),
    # --- Follow-up
    Achievement(
        "follow.plan",
        "Next step set",
        "follow",
        "Next steps you put a date on.",
        "Open an application and set a date for its next step.",
        (1, 5, 15),
    ),
    Achievement(
        "follow.done",
        "Follow-through",
        "follow",
        "Follow-ups you marked done or closed as no response.",
        "Mark a follow-up done when you have sent it.",
        (1, 5, 15, 30),
    ),
    Achievement(
        "follow.on_time",
        "On time",
        "follow",
        "Follow-ups done on or before the day you planned.",
        "Set a date for a follow-up and do it by then.",
        (5,),
    ),
    Achievement(
        "follow.tidy",
        "Tidy pipeline",
        "follow",
        "Stale applications you closed as no response. Closing them keeps the pipeline honest.",
        "Close an application that has gone quiet as no response.",
        (5,),
    ),
    Achievement(
        "follow.contact",
        "Networker",
        "follow",
        "Contacts you recorded for your applications.",
        "Add a recruiter or hiring manager to an application.",
        (1, 5, 15),
    ),
    Achievement(
        "follow.loop",
        "Closing the loop",
        "follow",
        "You recorded how an application ended. Knowing where you stand frees you to move on.",
        "When an employer answers, confirm it on the Employer rejections page or log it.",
    ),
    Achievement(
        "follow.lessons",
        "Lessons kept",
        "follow",
        "Three times you wrote down what you learned from an ending.",
        "When an application ends, add a note about what you learned.",
        (3,),
    ),
    # --- Outcomes (employer-driven milestones, no cap)
    Achievement(
        "out.heard_back",
        "Heard back",
        "out",
        "An employer answered with a next step.",
        "Log a screening call or interview when one comes in.",
    ),
    Achievement(
        "out.conversations",
        "Conversations",
        "out",
        "Applications where an employer moved you forward.",
        "Log each screening call or interview on its application.",
        (3, 10),
    ),
    Achievement(
        "out.interview",
        "First interview",
        "out",
        "Your first interview.",
        "Log an interview on its application when it is booked.",
    ),
    Achievement(
        "out.interviews",
        "Interview circuit",
        "out",
        "Interviews with three different employers.",
        "Log each interview on its application.",
        (3,),
    ),
    Achievement(
        "out.offer",
        "Offer",
        "out",
        "An employer made you an offer.",
        "Log an offer on its application when it arrives.",
    ),
    Achievement(
        "out.long_game",
        "The long game",
        "out",
        "An employer answered a month or more after you applied. Patience paid off.",
        "",
        hidden=True,
    ),
    Achievement(
        "out.bounce_back",
        "Bounce back",
        "out",
        "A no, and you kept going within two weeks. That is what resilience looks like.",
        "",
        hidden=True,
    ),
    # --- Rhythm (finished weeks only)
    Achievement(
        "rhythm.weeks",
        "Steady rhythm",
        "rhythm",
        "Weeks in a row with job-search work on two or more days.",
        "Do a little on two different days this week.",
        (2, 4, 8, 12),
        weekly=True,
    ),
    Achievement(
        "rhythm.rest",
        "Rest is part of it",
        "rhythm",
        "Steady weeks that still had two or more days off.",
        "Work on two or three days this week and take the others off.",
        (3,),
        weekly=True,
    ),
    Achievement(
        "rhythm.comeback",
        "Welcome back",
        "rhythm",
        "You picked the search back up after a break of two weeks or more.",
        "Breaks happen. Two days of work in a week after one counts.",
        weekly=True,
    ),
)

BY_ID: dict[str, Achievement] = {a.id: a for a in CATALOG}


def category_of(achievement_id: str) -> str:
    """The category key of an id; unknown ids (a later catalog's) fall back to the prefix."""
    a = BY_ID.get(achievement_id)
    return a.category if a is not None else achievement_id.split(".", 1)[0]
