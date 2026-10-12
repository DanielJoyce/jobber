"""Gmail setup tests: fake service object and mocked keyring only. No network."""

from __future__ import annotations

import xml.etree.ElementTree as ET

import pytest
from typer.testing import CliRunner

from jobhunter.cli import app
from jobhunter.config import Settings
from jobhunter.mail import auth, setup

ADDR = "someone+jobs@example.test"


class _Call:
    def __init__(self, fn):
        self.fn = fn

    def execute(self):
        return self.fn()


class _Labels:
    def __init__(self, g):
        self.g = g

    def list(self, userId):
        return _Call(lambda: {"labels": list(self.g.labels)})

    def create(self, userId, body):
        def run():
            lab = {"id": f"Label_{len(self.g.labels) + 1}", **body}
            self.g.labels.append(lab)
            self.g.writes.append(("label", body))
            return lab

        return _Call(run)

    def get(self, userId, id):
        return _Call(lambda: {"id": id, "messagesTotal": 7})


class _Filters:
    def __init__(self, g):
        self.g = g

    def list(self, userId):
        return _Call(lambda: {"filter": list(self.g.filters)})

    def create(self, userId, body):
        def run():
            self.g.filters.append(body)
            self.g.writes.append(("filter", body))
            return body

        return _Call(run)


class _Settings:
    def __init__(self, g):
        self._f = _Filters(g)

    def filters(self):
        return self._f


class _Users:
    def __init__(self, g):
        self._l = _Labels(g)
        self._s = _Settings(g)

    def labels(self):
        return self._l

    def settings(self):
        return self._s


class Gmail:
    """Fake Gmail service: records writes; returns existing labels/filters on later calls."""

    def __init__(self):
        self.labels = []
        self.filters = []
        self.writes = []
        self._u = _Users(self)

    def users(self):
        return self._u


def make_settings(**mail) -> Settings:
    return Settings.model_validate({"mail": {"alerts_address": ADDR, **mail}})


def test_setup_creates_then_is_idempotent():
    g = Gmail()
    s = make_settings(fallback_sender_domains=["workintexas.com"])
    first = setup.setup_mailbox(g, s)
    assert len(first.created) == 3 and not first.existing
    writes = len(g.writes)
    second = setup.setup_mailbox(g, s)
    assert not second.created and len(second.existing) == 3
    assert len(g.writes) == writes


def test_filter_action_shape():
    g = Gmail()
    setup.setup_mailbox(g, make_settings())
    body = g.filters[0]
    label_id = g.labels[0]["id"]
    assert g.labels[0]["name"] == "jobhunter/alerts"
    assert body["criteria"] == {"to": ADDR}
    assert body["action"] == {"addLabelIds": [label_id], "removeLabelIds": ["INBOX", "SPAM"]}


def test_fallback_domain_filters():
    g = Gmail()
    setup.setup_mailbox(g, make_settings(fallback_sender_domains=["a.test", "@b.test"]))
    assert [f["criteria"] for f in g.filters] == [
        {"to": ADDR},
        {"from": "@a.test"},
        {"from": "@b.test"},
    ]


def test_dry_run_writes_nothing():
    g = Gmail()
    report = setup.setup_mailbox(g, make_settings(), dry_run=True)
    assert report.created and g.writes == []


def test_placeholder_address_refused():
    with pytest.raises(setup.MailSetupError, match=r"config\.toml"):
        setup.setup_mailbox(Gmail(), Settings())


def test_status_reports_counts():
    g = Gmail()
    s = make_settings()
    setup.setup_mailbox(g, s)
    info = setup.mailbox_status(g, s)
    assert info["label_present"] and info["messages"] == 7
    assert all(info["filters"].values())


def test_xml_well_formed():
    s = make_settings(fallback_sender_domains=["a.test"])
    root = ET.fromstring(setup.filters_xml(s))
    props = [
        (p.get("name"), p.get("value"))
        for p in root.iter("{http://schemas.google.com/apps/2006}property")
    ]
    assert ("to", ADDR) in props and ("from", "@a.test") in props
    assert ("label", "jobhunter/alerts") in props
    assert ("shouldNeverSpam", "true") in props and ("shouldArchive", "true") in props
    assert len(root.findall("{http://www.w3.org/2005/Atom}entry")) == 2


def test_scopes_exact():
    assert auth.SCOPES == [
        "https://www.googleapis.com/auth/gmail.readonly",
        "https://www.googleapis.com/auth/gmail.labels",
        "https://www.googleapis.com/auth/gmail.settings.basic",
    ]


def test_missing_client_secrets_error(tmp_path, monkeypatch):
    monkeypatch.delenv(auth.SECRETS_ENV_VAR, raising=False)
    s = Settings.model_validate({"mail": {"client_secrets_path": str(tmp_path / "nope.json")}})
    with pytest.raises(auth.MailAuthError) as exc:
        auth.run_oauth_flow(s)
    text = str(exc.value)
    assert str(tmp_path / "nope.json") in text
    assert "Desktop app" in text and "Gmail API" in text and auth.SECRETS_ENV_VAR in text


def test_env_overrides_secrets_path(tmp_path, monkeypatch):
    monkeypatch.setenv(auth.SECRETS_ENV_VAR, str(tmp_path / "x.json"))
    assert auth.client_secrets_path(Settings()) == tmp_path / "x.json"


def test_keyring_store_and_load(monkeypatch):
    store = {}
    monkeypatch.setattr(auth.keyring, "set_password", lambda s, k, v: store.update({(s, k): v}))
    monkeypatch.setattr(auth.keyring, "get_password", lambda s, k: store.get((s, k)))
    auth.store_token("refresh-token")
    assert store == {("jobhunter", "gmail"): "refresh-token"}
    assert auth.load_token() == "refresh-token"


def test_authenticate_stores_flow_token(monkeypatch, tmp_path):
    f = tmp_path / "c.json"
    f.write_text("{}")
    monkeypatch.setenv(auth.SECRETS_ENV_VAR, str(f))
    saved = []

    class Creds:
        refresh_token = "rt"

    class Flow:
        @classmethod
        def from_client_secrets_file(cls, path, scopes):
            assert scopes == auth.SCOPES
            return cls()

        def run_local_server(self, host, port, open_browser, **kw):
            assert host == "127.0.0.1" and port == 0
            return Creds()

    import google_auth_oauthlib.flow as flow_mod

    monkeypatch.setattr(flow_mod, "InstalledAppFlow", Flow)
    monkeypatch.setattr(auth, "store_token", lambda t: saved.append(t) or "keyring")
    auth.authenticate(Settings())
    assert saved == ["rt"]


runner = CliRunner()


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    path = tmp_path / "config.toml"
    path.write_text(f'[mail]\nalerts_address = "{ADDR}"\nfallback_sender_domains = ["a.test"]\n')
    monkeypatch.setenv("JOBHUNTER_CONFIG", str(path))
    monkeypatch.setattr(auth, "load_token", lambda: None)
    return path


def test_cli_xml_needs_no_oauth(cfg):
    result = runner.invoke(app, ["mail", "setup", "--xml"])
    assert result.exit_code == 0, result.output
    ET.fromstring(result.output)
    assert ADDR in result.output and "jobhunter/alerts" in result.output


def test_cli_dry_run_without_token(cfg):
    result = runner.invoke(app, ["mail", "setup", "--dry-run"])
    assert result.exit_code == 0, result.output
    assert f"to:{ADDR}" in result.output and "from:@a.test" in result.output


def test_cli_dry_run_with_service(cfg, monkeypatch):
    g = Gmail()
    monkeypatch.setattr(auth, "load_token", lambda: "t")
    monkeypatch.setattr(auth, "build_service", lambda settings: g)
    result = runner.invoke(app, ["mail", "setup", "--dry-run"])
    assert result.exit_code == 0 and "would create: label" in result.output
    assert g.writes == []


def test_cli_setup_placeholder_refused(tmp_path, monkeypatch):
    monkeypatch.setenv("JOBHUNTER_CONFIG", str(tmp_path / "none.toml"))
    result = runner.invoke(app, ["mail", "setup", "--dry-run"])
    assert result.exit_code == 1


def test_cli_auth_missing_secrets(cfg, tmp_path, monkeypatch):
    monkeypatch.setenv(auth.SECRETS_ENV_VAR, str(tmp_path / "missing.json"))
    result = runner.invoke(app, ["mail", "auth"])
    assert result.exit_code == 1
    assert "Desktop app" in result.output


def test_cli_status_without_token(cfg):
    result = runner.invoke(app, ["mail", "status"])
    assert result.exit_code == 1 and "mail auth" in result.output


def test_fallback_sender_filters_are_exact_from_addresses():
    g = Gmail()
    s = make_settings(
        fallback_sender_domains=["a.test"],
        fallback_senders=["JobAlerts-NoReply@linkedin.com", "jobalerts-noreply@linkedin.com"],
    )
    setup.setup_mailbox(g, s)
    assert [f["criteria"] for f in g.filters] == [
        {"to": ADDR},
        {"from": "@a.test"},
        {"from": "jobalerts-noreply@linkedin.com"},
    ]
    xml = setup.filters_xml(s)
    assert "jobalerts-noreply@linkedin.com" in xml
    assert 'value="@linkedin.com"' not in xml


@pytest.mark.parametrize(
    "bad", ["linkedin.com", "@linkedin.com", "*@linkedin.com", "LinkedIn <a@linkedin.com>", "a@b"]
)
def test_fallback_sender_validation_rejects_non_addresses(bad):
    with pytest.raises(setup.MailSetupError, match="fallback_senders"):
        setup.wanted_filters(make_settings(fallback_senders=[bad]))
    with pytest.raises(setup.MailSetupError):
        setup.setup_mailbox(Gmail(), make_settings(fallback_senders=[bad]))


def test_personal_mail_domain_warning():
    s = make_settings(fallback_sender_domains=["@linkedin.com", "www.craigslist.org", "ok.test"])
    warns = setup.domain_warnings(s)
    assert len(warns) == 2 and "linkedin.com" in warns[0] and "fallback_senders" in warns[0]
    assert "craigslist.org" in warns[1]
    assert setup.domain_warnings(make_settings(fallback_sender_domains=["mail.linkedin.com"]))
    report = setup.setup_mailbox(Gmail(), make_settings(fallback_sender_domains=["linkedin.com"]))
    assert any(line.startswith("warning:") for line in report.lines())
    assert not setup.domain_warnings(make_settings(fallback_senders=["a@linkedin.com"]))


# --- mail_setup_at for the achievements board (specs/021 setup.mail, ticket A1) ------------


@pytest.fixture
def cfg_db(tmp_path, monkeypatch):
    db_path = tmp_path / "jh.db"
    path = tmp_path / "config.toml"
    path.write_text(
        f'[paths]\ndb_path = "{db_path}"\n\n[mail]\nalerts_address = "{ADDR}"\n'
        'fallback_sender_domains = ["a.test"]\n'
    )
    monkeypatch.setenv("JOBHUNTER_CONFIG", str(path))
    g = Gmail()
    monkeypatch.setattr(auth, "load_token", lambda: "t")
    monkeypatch.setattr(auth, "build_service", lambda settings: g)
    return db_path


def _mail_setup_at(db_path):
    from jobhunter.core import db

    c = db.connect(db_path)
    try:
        row = c.execute(
            "SELECT value FROM achievement_setting WHERE key = 'mail_setup_at'"
        ).fetchone()
        return None if row is None else row[0]
    finally:
        c.close()


def test_cli_setup_records_mail_setup_at_once(cfg_db):
    result = runner.invoke(app, ["mail", "setup"])
    assert result.exit_code == 0, result.output
    first = _mail_setup_at(cfg_db)
    assert first is not None and first.endswith("+00:00")
    assert runner.invoke(app, ["mail", "setup"]).exit_code == 0  # re-run: nothing to create
    assert _mail_setup_at(cfg_db) == first  # the first success is kept


def test_cli_dry_run_records_nothing(cfg_db):
    from jobhunter.core import db

    c = db.connect(cfg_db)
    db.migrate(c)
    c.close()
    result = runner.invoke(app, ["mail", "setup", "--dry-run"])
    assert result.exit_code == 0 and "would create: label" in result.output
    assert _mail_setup_at(cfg_db) is None


def test_cli_failed_setup_records_nothing(cfg_db, monkeypatch):
    def refuse(settings):
        raise auth.MailAuthError("token revoked")

    monkeypatch.setattr(auth, "build_service", refuse)
    result = runner.invoke(app, ["mail", "setup"])
    assert result.exit_code == 1
    assert not cfg_db.exists() or _mail_setup_at(cfg_db) is None
