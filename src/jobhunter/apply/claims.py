"""Lexicons for the no-fabrication checker (specs/017 "No-fabrication rule").

Data, grown like ``ats_rules.py``. ``apply/factcheck.py`` reads it; nothing here decides.

- ``CLAIM_CLASSES``: an output word from a class that no cited line uses is a stronger claim
  than the source supports ("contributed to the migration" -> "led the migration").
- ``CREDENTIALS``: credential words that must appear in a cited line.
- ``TECH``: technology names checked against the whole resume (a skill or technology that is
  not anywhere in the resume fails); ``SYNONYMS`` maps aliases to one canonical name.
"""

from __future__ import annotations

import re


def _forms(*verbs: str) -> tuple[str, ...]:
    """Every form of a verb we match: ``"lead/led"`` -> lead, leads, led, leading. A spec is
    ``base`` or ``base/irregular past[/irregular participle]``; regular forms are derived."""
    out: list[str] = []
    for spec in verbs:
        base, *irregular = spec.split("/")
        stem = base[:-1] if base.endswith("e") and not base.endswith("ee") else base
        third = base + ("es" if base.endswith(("s", "sh", "ch", "x")) else "s")
        past = irregular or [stem + "ed"]
        out += [base, third, stem + "ing", *past]
    return tuple(x for x in dict.fromkeys(out) if x not in _AMBIGUOUS_FORMS)


# Forms that are mostly other words in resumes: "heading", "direct reports", "hard drive".
_AMBIGUOUS_FORMS = {"head", "heads", "heading", "direct", "drive", "champion", "champions"}


# Each class: whole words or phrases (every verb form listed, present tense included, since a
# current role and a summary are usually written in it).
CLAIM_CLASSES: dict[str, tuple[str, ...]] = {
    "leadership": (
        *_forms(
            "lead/led",
            "manage",
            "head/headed",
            "direct",
            "supervise",
            "spearhead",
            "drive/drove/driven",
            "oversee/oversaw/overseen",
            "champion",
        ),
        "owns",
        "owned",
        "owning",
        "owner",
        "ownership",
        "leader",
        "leadership",
        "head of",
    ),
    "scope": (
        *_forms("architect"),
        "designed the",
        "founded",
        "founding",
        "founder",
        "co-founded",
        "cofounded",
        "built the team",
        "company-wide",
        "organization-wide",
        "org-wide",
        "end-to-end",
        "single-handedly",
        "from scratch",
    ),
    "superlative": (
        "expert",
        "expertise",
        "best",
        "first",
        "sole",
        "top",
        "world-class",
        "renowned",
        "unparalleled",
        "exceptional",
    ),
}
# Seniority words are checked only on resume lines (a letter may name the role it applies for).
SENIORITY: tuple[str, ...] = (
    "senior",
    "sr",
    "staff",
    "principal",
    "distinguished",
    "chief",
    "vp",
    "vice president",
    "director",
    "manager",
    "executive",
    "cto",
    "cio",
    "ciso",
)
# Team-size claims are patterns, not words.
TEAM_SIZE = re.compile(
    r"\b(team of \d+|\d+[- ](person|people|member|engineer|engineers|developers|reports|direct "
    r"reports|staff)|(\d+|several|many) (engineers|developers|reports|direct reports|people))\b"
)

CREDENTIALS: tuple[str, ...] = (
    "certified",
    "certification",
    "certificate",
    "licensed",
    "license",
    "licence",
    "clearance",
    "secret clearance",
    "top secret",
    "ts/sci",
    "degree",
    "bachelor",
    "bachelors",
    "bachelor's",
    "master",
    "masters",
    "master's",
    "phd",
    "ph.d",
    "doctorate",
    "mba",
    "cpa",
    "pmp",
    "cissp",
    "ccna",
    "rhce",
    "accredited",
    # Degree and certification notations resumes actually use.
    "b.s.",
    "b.s",
    "bs",
    "b.a.",
    "b.a",
    "m.s.",
    "m.s",
    "ms",
    "m.a.",
    "m.a",
    "m.eng",
    "b.eng",
    "m.sc",
    "b.sc",
    "associate's",
    "cka",
    "ckad",
    "cks",
    "security+",
    "network+",
    "comptia",
    "cism",
    "cisa",
    "oscp",
    "ccnp",
    "ccie",
    "itil",
    "public trust",
    "aws certified",
    "csm",
    "scrum master",
)

# Canonical technology names (lowercase), for synonyms and lowercase names. Names outside the
# table are still caught in prose when they look like names (factcheck "name-like" tokens), and
# every `skills` entry is checked against the resume regardless of this table.
TECH: tuple[str, ...] = (
    "ansible",
    "apache",
    "aws",
    "azure",
    "bash",
    "c#",
    "c++",
    "cassandra",
    "chef",
    "circleci",
    "cloudformation",
    "datadog",
    "django",
    "docker",
    "elasticsearch",
    "fastapi",
    "flask",
    "gcp",
    "git",
    "github actions",
    "gitlab",
    "go",
    "grafana",
    "graphql",
    "hadoop",
    "helm",
    "java",
    "javascript",
    "jenkins",
    "jira",
    "kafka",
    "kotlin",
    "kubernetes",
    "linux",
    "mongodb",
    "mysql",
    "nginx",
    "node.js",
    "openshift",
    "oracle",
    "perl",
    "php",
    "postgresql",
    "powershell",
    "prometheus",
    "puppet",
    "python",
    "rabbitmq",
    "react",
    "redis",
    "ruby",
    "rust",
    "salesforce",
    "sap",
    "scala",
    "snowflake",
    "spark",
    "splunk",
    "sql",
    "sql server",
    "swift",
    "tableau",
    "terraform",
    "typescript",
    "vmware",
    "windows server",
    "airflow",
    "dbt",
    "angular",
    ".net",
    "spring",
    "confluence",
    "excel",
    "pytorch",
    "tensorflow",
    "servicenow",
    "databricks",
    "vue",
    "kafka streams",
    "bigquery",
    "redshift",
    "looker",
    "power bi",
)
SYNONYMS: dict[str, str] = {
    "k8s": "kubernetes",
    "kube": "kubernetes",
    "golang": "go",
    "postgres": "postgresql",
    "psql": "postgresql",
    "js": "javascript",
    "ts": "typescript",
    "node": "node.js",
    "nodejs": "node.js",
    "amazon web services": "aws",
    "google cloud": "gcp",
    "google cloud platform": "gcp",
    "microsoft azure": "azure",
    "elastic": "elasticsearch",
    "mssql": "sql server",
    "ms sql": "sql server",
    "gha": "github actions",
    "tf": "terraform",
    "rhel": "linux",
    "red hat": "linux",
    "ubuntu": "linux",
    "debian": "linux",
    "centos": "linux",
}

NUMBER_WORDS: dict[str, str] = {
    "a dozen": "12",
    "one": "1",
    "two": "2",
    "three": "3",
    "four": "4",
    "five": "5",
    "six": "6",
    "seven": "7",
    "eight": "8",
    "nine": "9",
    "ten": "10",
    "eleven": "11",
    "twelve": "12",
    "fifteen": "15",
    "twenty": "20",
    "thirty": "30",
    "forty": "40",
    "fifty": "50",
    "sixty": "60",
    "seventy": "70",
    "eighty": "80",
    "ninety": "90",
    "hundred": "100",
}

# Vague quantities: allowed only when a cited line uses the same word.
VAGUE_QUANTITIES: tuple[str, ...] = (
    "dozens",
    "hundreds",
    "thousands",
    "millions",
    "billions",
    "decade",
    "decades",
    "half",
    "double",
    "doubled",
    "tripled",
    "tenfold",
    "several",
    "countless",
)

# Tokens of an employer name that never identify it ("the", "inc", "state of").
EMPLOYER_STOP = {
    "the",
    "of",
    "and",
    "inc",
    "llc",
    "ltd",
    "co",
    "corp",
    "corporation",
    "company",
    "group",
    "state",
    "department",
    "dept",
    "county",
    "city",
    "university",
    "services",
    "service",
    "technologies",
    "technology",
    "systems",
    "solutions",
    "us",
    "usa",
    "america",
    "american",
    "national",
}
