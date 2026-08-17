"""W3-G2 — the deep deploy check, and the ways it must refuse to lie.

Two halves.

*Script half* (built on this package's existing fixtures — a copy of the real
``scripts/verify_deploy.sh`` in a disposable repo, a fake ``curl`` that never
opens a socket, and the hostile ``docker`` canary): the ``--deep`` flag's
preconditions are hard failures checked before a single request goes out, and
a run WITHOUT ``--deep`` says out loud that the core loop was not exercised.
That second one is the point of the whole package. ``docs/STATE.md §6.2``
records that this script's unqualified PASS is what carried "the deploy
works" for weeks across a stack with a dead core loop; a pass whose scope is
not stated is how that happens again.

*Logic half*: the decision rules in ``scripts/verify_core_loop.py`` that
decide whether a deployment passed. They are pure functions precisely so
they can be tested for the false-pass cases that are otherwise only
reachable by breaking a live deployment — most importantly that a
``runs`` row left behind by an EARLIER invocation can never be accepted as
evidence that this one worked.

SAFETY: no test here executes real docker, opens a socket, or reaches the
network. See conftest.py's module docstring.
"""

from __future__ import annotations

import ast
import inspect
import re
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest

import scripts.verify_core_loop as deep
from ingress.models import ZendeskWebhookPayload
from ingress.signature import SignatureVerificationError, verify_signature
from scripts.verify_core_loop import (
    PROTECTED_TABLES,
    AmbiguousRun,
    DeepCheckFailed,
    ForbiddenStatement,
    assert_not_destructive,
    assert_row_is_real,
    build_signed_request,
    dedup_cleanup_statements,
    find_new_run,
    residue_lines,
    unexpected_comment_ids,
)

from .conftest import (
    run_verify_deploy,
    write_docker_canary,
    write_fake_curl,
)

REPO_ROOT = Path(__file__).resolve().parents[3]
VERIFY_SCRIPT = REPO_ROOT / "scripts" / "verify_deploy.sh"
DEPLOY_DOC = REPO_ROOT / "docs" / "deploy.md"

SECRET = "a-signing-secret-that-is-not-valid-base64"


# ---------------------------------------------------------------------------
# Script half
# ---------------------------------------------------------------------------


def test_a_pass_without_deep_states_that_the_core_loop_was_not_exercised(
    fake_repo: Path, stub_bin: Path
) -> None:
    """The four liveness assertions passing must never read as "it works".

    This is the assertion the whole work package exists for: the same four
    green ticks were true of a droplet whose webhook accepted events and
    never started a run.
    """
    sentinel = fake_repo / "docker_canary_triggered.log"
    write_docker_canary(stub_bin, sentinel)
    write_fake_curl(stub_bin)

    result = run_verify_deploy(
        fake_repo,
        env_overrides={
            "DEPLOY_HOST": "example-remote-host.invalid",
            "PORTAL_TOKEN": "dummy-token",
        },
        stub_bin_dir=stub_bin,
        canary_sentinel=sentinel,
    )

    combined = result.stdout + result.stderr
    assert result.returncode == 0, combined
    assert "SCOPE: liveness only" in result.stdout, combined
    assert "core loop" in result.stdout, combined
    assert "was NOT exercised" in result.stdout, combined
    assert "--deep" in result.stdout, combined
    assert not sentinel.exists()


def test_deep_with_no_signing_secret_hard_fails_before_any_request(
    fake_repo: Path, stub_bin: Path
) -> None:
    """A missing precondition is a failure, never a silently skipped check.

    Also pins the ORDERING: the failure must land before the first assertion
    prints, so a caller can never watch four green ticks scroll past and
    then discover the check that matters could not run.
    """
    sentinel = fake_repo / "docker_canary_triggered.log"
    write_docker_canary(stub_bin, sentinel)
    write_fake_curl(stub_bin)

    result = run_verify_deploy(
        fake_repo,
        args=["--deep"],
        env_overrides={
            "DEPLOY_HOST": "example-remote-host.invalid",
            "PORTAL_TOKEN": "dummy-token",
            "ZENDESK_WEBHOOK_SIGNING_SECRET": "",
            "CXFORGE_VERIFY_TICKET_ID": "12345",
        },
        stub_bin_dir=stub_bin,
    )

    combined = result.stdout + result.stderr
    assert result.returncode != 0, combined
    assert "FAIL:" in result.stderr, combined
    assert "ZENDESK_WEBHOOK_SIGNING_SECRET" in result.stderr, combined
    assert "PASS" not in result.stdout, combined
    assert "1/4" not in result.stdout, combined


def test_deep_with_no_ticket_id_hard_fails_and_says_why_one_cannot_be_invented(
    fake_repo: Path, stub_bin: Path
) -> None:
    write_fake_curl(stub_bin)

    result = run_verify_deploy(
        fake_repo,
        args=["--deep"],
        env_overrides={
            "DEPLOY_HOST": "example-remote-host.invalid",
            "PORTAL_TOKEN": "dummy-token",
            "ZENDESK_WEBHOOK_SIGNING_SECRET": SECRET,
            "CXFORGE_VERIFY_TICKET_ID": "",
        },
        stub_bin_dir=stub_bin,
    )

    combined = result.stdout + result.stderr
    assert result.returncode != 0, combined
    assert "CXFORGE_VERIFY_TICKET_ID" in result.stderr, combined
    # The reason matters as much as the requirement: a reader who does not
    # know why has every incentive to "fix" this by generating an id.
    assert "fetch_ticket" in result.stderr, combined
    assert "PASS" not in result.stdout, combined


def test_deploy_doc_documents_the_deep_flag_the_script_actually_parses() -> None:
    """Mirrors the ``--local`` check in test_docs_and_env_example_consistency.

    The flag is read out of the script rather than hardcoded, so renaming it
    without updating the doc fails here instead of leaving the doc describing
    an invocation that no longer works.
    """
    assert "--deep)" in VERIFY_SCRIPT.read_text(), "verify_deploy.sh no longer parses --deep"
    doc = DEPLOY_DOC.read_text()
    assert re.search(r"--deep\b", doc), "docs/deploy.md does not mention the --deep flag"
    assert "CXFORGE_VERIFY_TICKET_ID" in doc, (
        "docs/deploy.md does not tell the operator about the one variable --deep "
        "cannot run without"
    )


# ---------------------------------------------------------------------------
# Logic half — signing
# ---------------------------------------------------------------------------


def test_the_bytes_that_get_posted_are_the_bytes_that_get_signed() -> None:
    """Verified with the SERVER's own verifier, against the exact buffer.

    `ingress/signature.py`'s docstring records what the alternative costs:
    signing a re-serialized form of the body passes every unit test that
    mints its own body and fails every real request.
    """
    raw_body, headers = build_signed_request(
        secret=SECRET, ticket_id="12345", comment_id="cxforge-verify-abc"
    )
    verify_signature(
        secret=SECRET,
        timestamp=headers["X-Zendesk-Webhook-Signature-Timestamp"],
        signature=headers["X-Zendesk-Webhook-Signature"],
        raw_body=raw_body,
    )
    payload = ZendeskWebhookPayload.model_validate_json(raw_body)
    assert payload.ticket_id == "12345"
    assert payload.comment_id == "cxforge-verify-abc"


def test_a_body_altered_after_signing_no_longer_verifies() -> None:
    raw_body, headers = build_signed_request(
        secret=SECRET, ticket_id="12345", comment_id="cxforge-verify-abc"
    )
    with pytest.raises(SignatureVerificationError):
        verify_signature(
            secret=SECRET,
            timestamp=headers["X-Zendesk-Webhook-Signature-Timestamp"],
            signature=headers["X-Zendesk-Webhook-Signature"],
            raw_body=raw_body.replace(b"12345", b"99999"),
        )


def test_every_invocation_signs_a_different_comment_id() -> None:
    a, _ = build_signed_request(secret=SECRET, ticket_id="1", comment_id="x")
    from scripts.verify_core_loop import synthetic_comment_id

    assert synthetic_comment_id() != synthetic_comment_id()
    assert b"x" in a


# ---------------------------------------------------------------------------
# Logic half — which row counts as evidence
# ---------------------------------------------------------------------------


def _run_item(run_id: int, ticket_id: str, seconds: float = 7.0) -> dict[str, object]:
    received = datetime(2026, 8, 17, 5, 0, 0, tzinfo=UTC)
    return {
        "run_id": run_id,
        "ticket_id": ticket_id,
        "route": "case_status",
        "outcome": "auto_sent",
        "received_at": received.isoformat(),
        "replied_at": (received + timedelta(seconds=seconds)).isoformat(),
    }


def test_a_stale_row_for_the_same_ticket_is_not_accepted_as_evidence() -> None:
    """The false pass this check exists to make impossible.

    A dead worker plus one `runs` row left over from an earlier invocation
    must NOT read as a working core loop. Confirmed against a live stack as
    well as here: with the worker killed and a real matching row in the
    table, the check still failed.
    """
    stale = _run_item(6, "987654")
    assert find_new_run([stale], ticket_id="987654", baseline_run_ids={6}) is None


def test_a_new_row_for_a_different_ticket_is_not_accepted_either() -> None:
    other = _run_item(7, "some-other-ticket")
    assert find_new_run([other], ticket_id="987654", baseline_run_ids={6}) is None


def test_the_new_row_for_this_ticket_is_found_among_the_stale_ones() -> None:
    feed = [_run_item(6, "987654"), _run_item(7, "other"), _run_item(8, "987654")]
    found = find_new_run(feed, ticket_id="987654", baseline_run_ids={6, 7})
    assert found is not None and found["run_id"] == 8


def test_find_new_run_refuses_rather_than_guesses_when_two_candidates_exist() -> None:
    """The half of the defect that had no test at all.

    Until 2026-08-17 this returned `max(run_id)` here, describing it as
    "newest first, so a (pathological) double-run reports the later row". Two
    new rows on this ticket is not a pathological double-run: this invocation
    posts exactly ONE webhook, and `CXFORGE_VERIFY_TICKET_ID` names a ticket
    reused on purpose that takes real customer comments. So the second row is
    somebody else's, `runs` carries no provenance to tell them apart, and the
    old code handed the guess straight to a `finally` that deleted it.

    Refusing is the required behaviour, and it must be loud: exit 1 with both
    ids named, not a pass on a row it cannot prove it caused.
    """
    feed = [_run_item(6, "3"), _run_item(9, "3"), _run_item(10, "3")]
    with pytest.raises(AmbiguousRun) as excinfo:
        find_new_run(feed, ticket_id="3", baseline_run_ids={6})

    # Both ids reported, so a human can look at them by hand.
    assert excinfo.value.candidate_run_ids == (9, 10)
    message = str(excinfo.value)
    assert "9" in message and "10" in message
    assert "Refusing to pick one" in message
    assert "Nothing has been deleted" in message
    # And it is a check failure (exit 1), not a crash or a silent pass.
    assert isinstance(excinfo.value, DeepCheckFailed)


def test_the_ambiguity_refusal_survives_a_third_party_run_arriving_mid_poll() -> None:
    """The concrete sequence that made this urgent: the check posts, a real
    customer comments on the same ticket seconds later, and both runs land
    inside the polling window. The first poll (only the customer's row
    present, say) must not silently become the answer once a second appears —
    every poll re-evaluates, and the moment two coexist the check stops."""
    baseline = {1, 2, 3}
    first_poll = [_run_item(1, "3"), _run_item(11, "3")]
    early = find_new_run(first_poll, ticket_id="3", baseline_run_ids=baseline)
    assert early is not None and early["run_id"] == 11
    second_poll = [*first_poll, _run_item(12, "3")]
    with pytest.raises(AmbiguousRun):
        find_new_run(second_poll, ticket_id="3", baseline_run_ids=baseline)


def test_two_new_rows_on_a_DIFFERENT_ticket_are_not_ambiguity() -> None:
    """Only rows for the ticket under test are candidates, so a busy
    deployment does not make this check unusable."""
    feed = [_run_item(9, "other"), _run_item(10, "other"), _run_item(11, "3")]
    found = find_new_run(feed, ticket_id="3", baseline_run_ids={6})
    assert found is not None and found["run_id"] == 11


# ---------------------------------------------------------------------------
# Logic half — whether the row that DID appear is real
# ---------------------------------------------------------------------------


def test_a_genuine_row_passes_and_reports_its_interval() -> None:
    assert assert_row_is_real(_run_item(8, "987654", seconds=7.5), ticket_id="987654") == 7.5


def test_the_22us_interval_that_adr_004_corrected_is_rejected() -> None:
    """`docs/STATE.md §4.1`: before Wave 1 `received_at` was minted inside
    `act`, the LAST graph node, so the interval timed only the tail-end
    helpdesk calls and measured 22µs across a 300ms ingest delay. A row
    existed the whole time. Existence is not evidence."""
    row = _run_item(8, "987654", seconds=0.000022)
    with pytest.raises(DeepCheckFailed, match="µs"):
        assert_row_is_real(row, ticket_id="987654")


def test_a_gated_run_is_rejected_rather_than_quietly_counted() -> None:
    """Gate ON writes `outcome = NULL` and `replied_at = NULL` by design
    (`agent.nodes.act`). Nothing was sent, so nothing is proven."""
    row = _run_item(8, "987654")
    row["outcome"] = None
    row["replied_at"] = None
    with pytest.raises(DeepCheckFailed, match="gate"):
        assert_row_is_real(row, ticket_id="987654")


def test_a_row_with_no_route_is_rejected() -> None:
    row = _run_item(8, "987654")
    row["route"] = None
    with pytest.raises(DeepCheckFailed, match="classify"):
        assert_row_is_real(row, ticket_id="987654")


def test_a_row_for_the_wrong_ticket_is_rejected() -> None:
    with pytest.raises(DeepCheckFailed, match="expected"):
        assert_row_is_real(_run_item(8, "someone-elses-ticket"), ticket_id="987654")


# ---------------------------------------------------------------------------
# Logic half — the audit tables are off limits
#
# This section replaces the "cleanup blast radius" tests that stood here
# until 2026-08-17. Those asserted, verbatim, that the script emitted
#
#     DELETE FROM runs WHERE id = 8;
#     DELETE FROM drafts WHERE run_id = 8;
#
# and were written in the same commit as the code they described (ddb75a5,
# W3-G2), so they pinned the behaviour rather than a requirement. That
# behaviour destroyed production audit rows 4, 5 and 6 — three successful
# `auto_sent` runs that each posted a real public reply, still visible in
# Zendesk, with no local record left. The run id came from `find_new_run`,
# which picked `max(run_id)` among the rows for the ticket it had not seen
# before, on a ticket that is reused by design and takes real customer
# comments. So the two assertions above encoded a defect as the contract:
# they required the script to delete a row it could not prove it had created.
# They are deleted here and replaced by their inverse.
# ---------------------------------------------------------------------------

# The destructive-statement pattern this package enforces. Deliberately its
# own copy rather than an import of the script's `_DESTRUCTIVE_SQL`: a test
# that reuses the guard it is checking cannot notice the guard being widened.
_DESTRUCTIVE_SQL = re.compile(
    r"\b(?:DELETE\s+FROM|TRUNCATE(?:\s+TABLE)?|UPDATE)\s+(?:ONLY\s+)?\"?(?:runs|drafts)\b",
    re.IGNORECASE,
)


def _executable_string_literals(source: str) -> list[str]:
    """Every string constant in `source` that is not a docstring.

    Docstrings are excluded because they are the one kind of string that
    provably cannot reach a cursor — and this module's docstrings have to be
    able to describe, in prose, the statements that caused the data loss.
    Comments never appear in the AST at all, so they need no exclusion.

    f-strings are covered: `ast.walk` reaches the `Constant` parts inside a
    `JoinedStr`, so `f"DELETE FROM runs WHERE id = {run_id};"` still surfaces
    its literal `"DELETE FROM runs WHERE id = "` prefix.
    """
    tree = ast.parse(source)
    docstrings: set[int] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef):
            continue
        first = node.body[0] if node.body else None
        if (
            isinstance(first, ast.Expr)
            and isinstance(first.value, ast.Constant)
            and isinstance(first.value.value, str)
        ):
            docstrings.add(id(first.value))
    return [
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and id(node) not in docstrings
    ]


def test_no_sql_the_deep_check_could_execute_touches_the_audit_tables() -> None:
    """THE invariant: this script issues no delete/update/truncate against
    `runs` or `drafts`, on any path.

    Asserted by reading the script's own source rather than by running it,
    because "the fixture never reached that line" is exactly how the old
    behaviour survived: every one of the nine tests in this file passed while
    the `finally` deleted audit rows on every real invocation. A static scan
    of every string literal the module could hand to a cursor cannot be
    satisfied by a code path going unexercised.

    Scoped to string literals, so it is blind to SQL assembled at runtime
    from fragments — which is why `assert_not_destructive` also stands
    between any statement and the database (see the two tests below).
    """
    source = Path(inspect.getfile(deep)).read_text(encoding="utf-8")
    offenders = [
        literal
        for literal in _executable_string_literals(source)
        if _DESTRUCTIVE_SQL.search(literal)
    ]
    assert not offenders, (
        "scripts/verify_core_loop.py contains SQL that could destroy audit rows: "
        f"{offenders!r}. A `runs` row is the only local record that the agent answered a "
        "real customer; three of them (4, 5, 6) are already gone because this script "
        "deleted rows it could not prove it had created. This gate reads the audit tables "
        "and never writes them."
    )


@pytest.mark.parametrize("ticket_id", ["3", "987654", "12345"])
@pytest.mark.parametrize("comment_id", ["cxforge-verify-abc", f"cxforge-verify-{'f' * 32}"])
def test_the_only_statement_the_check_runs_is_the_dedup_row_it_minted(
    ticket_id: str, comment_id: str
) -> None:
    """The complete set of SQL the script executes, enumerated.

    One statement, against `tickets_seen`, keyed on the `(ticket_id,
    comment_id)` this process generated itself. That is the standard the
    removed `runs`/`drafts` deletes could not meet — the row is provably this
    invocation's own with no inference about ids, ordering or recency, and
    `tickets_seen` is a dedup guard rather than an audit record.
    """
    statements = dedup_cleanup_statements(ticket_id, comment_id)
    assert len(statements) == 1, statements
    assert statements[0].startswith("DELETE FROM tickets_seen")
    assert f"WHERE ticket_id = '{ticket_id}' AND comment_id = '{comment_id}'" in statements[0]
    for statement in statements:
        assert_not_destructive(statement)
        for table in PROTECTED_TABLES:
            assert not re.search(rf"\b{table}\b", statement, re.IGNORECASE), (
                f"a statement this script executes names the audit table {table!r}: "
                f"{statement!r}"
            )


def test_the_executor_refuses_a_destructive_statement_even_if_handed_one() -> None:
    """The runtime half of the invariant, for SQL the static scan cannot see.

    `_execute` calls this on every statement before opening a connection, so
    a future edit that composes a delete out of fragments — or one that
    reintroduces the literal — hits a `ForbiddenStatement` instead of a
    database. Parametrised over the exact statements that destroyed runs 4-6.
    """
    for statement in (
        "DELETE FROM runs WHERE id = 4;",
        "DELETE FROM drafts WHERE run_id = 4;",
        "delete from runs where ticket_id = '3';",
        "TRUNCATE TABLE runs;",
        "UPDATE runs SET outcome = NULL WHERE id = 5;",
        'DELETE FROM "drafts" WHERE run_id = 6;',
    ):
        with pytest.raises(ForbiddenStatement, match="runs / drafts"):
            assert_not_destructive(statement)

    # ...and still lets the one legitimate statement through.
    assert_not_destructive(dedup_cleanup_statements("3", "cxforge-verify-abc")[0])


def test_execute_checks_every_statement_before_running_any_of_them() -> None:
    """A list that is destructive in its LAST element must not get its first
    element committed. `_execute` therefore validates the whole list before it
    connects — asserted here by handing it an unreachable DSN: if validation
    happened per-statement, the first statement would be attempted and the
    failure would be a connection error rather than `ForbiddenStatement`."""
    with pytest.raises(ForbiddenStatement):
        deep._execute(
            "postgresql://user@127.0.0.1:1/nope",
            [
                "DELETE FROM tickets_seen WHERE ticket_id = '3' AND comment_id = 'x';",
                "DELETE FROM runs WHERE id = 4;",
            ],
        )


# ---------------------------------------------------------------------------
# Logic half — what is left behind is announced
# ---------------------------------------------------------------------------


def test_the_residue_report_names_the_run_it_left_behind() -> None:
    """Requirement 5 of the fix: whatever the script leaves, it says so on
    stdout, by run id. Silence on this path is what let the deletes run for
    weeks without anyone noticing rows going missing."""
    lines = residue_lines(
        "3", {"posted": True, "run_id": 42, "ambiguous_run_ids": (), "ticket_run_ids": (7, 42)}
    )
    joined = "\n".join(lines)
    assert "LEFT BEHIND" in joined
    assert "runs row 42" in joined
    assert "SELECT * FROM runs WHERE id IN (42);" in joined
    # The metrics warning is the honest half of choosing visible pollution
    # over silent deletion — a reader has to know the numbers now include it.
    assert "human_avoidance_rate" in joined and "sample_count" in joined
    # And no copy-pasteable statement for the audit tables: printing one would
    # hand the next reader the precise text that lost runs 4-6.
    assert not _DESTRUCTIVE_SQL.search(joined), joined


def test_the_residue_report_names_candidates_it_could_not_attribute() -> None:
    """The worst case gets the loudest output: two candidate rows, one of
    which may be a customer's, and neither touched."""
    lines = residue_lines(
        "3",
        {
            "posted": True,
            "run_id": None,
            "ambiguous_run_ids": (9, 10),
            "ticket_run_ids": (9, 10),
        },
    )
    joined = "\n".join(lines)
    assert "[9, 10]" in joined
    assert "could NOT attribute" in joined
    assert "None of them was touched." in joined
    assert not _DESTRUCTIVE_SQL.search(joined), joined


def test_nothing_is_reported_as_left_behind_when_nothing_was_sent() -> None:
    lines = residue_lines("3", {"posted": False, "run_id": None})
    assert lines == ["cleanup     : nothing to undo — no webhook was sent."]


def test_a_foreign_comment_on_the_reused_ticket_is_detected() -> None:
    """`tickets_seen` is the only table in this schema that records what
    triggered a run, so it is the only provenance available without a schema
    change. A comment id that appeared during the window and is not the one
    this process minted means attribution by elimination is over."""
    ours = "cxforge-verify-abc"
    assert unexpected_comment_ids({ours}, baseline=set(), ours=ours) == set()
    assert unexpected_comment_ids({ours, "998"}, baseline=set(), ours=ours) == {"998"}
    # A comment that was already there before the POST is not news.
    assert unexpected_comment_ids({ours, "998"}, baseline={"998"}, ours=ours) == set()


# ---------------------------------------------------------------------------
# The whole path, driven for real
#
# Everything above is a pure function. The rows this script destroyed were
# destroyed by `main`'s `finally`, which no test in this file ever entered —
# nine tests were green while every real invocation deleted audit rows. So
# these two drive `main` itself: real argument parsing, real `_check`, real
# `finally`. No socket is opened (httpx.MockTransport answers in-process) and
# no database is touched (CXFORGE_VERIFY_DB_URL is left unset, which is the
# documented droplet configuration anyway).
# ---------------------------------------------------------------------------


def _fake_deployment(
    feeds: list[list[dict[str, object]]], *, gate_enabled: bool = False
) -> httpx.MockTransport:
    """A deployment that answers the three endpoints the check uses.

    ``feeds`` is consumed one entry per ``GET /api/feed``; the last entry is
    repeated once exhausted, so a test states only the polls it cares about.
    """
    remaining = list(feeds)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/settings/gate":
            return httpx.Response(200, json={"enabled": gate_enabled})
        if request.url.path == "/api/feed":
            feed = remaining.pop(0) if len(remaining) > 1 else remaining[0]
            return httpx.Response(200, json={"runs": feed})
        if request.url.path == "/webhooks/zendesk":
            return httpx.Response(202, json={"duplicate": False})
        raise AssertionError(f"unexpected request: {request.method} {request.url}")

    return httpx.MockTransport(handler)


@pytest.fixture
def deployment(monkeypatch: pytest.MonkeyPatch) -> Callable[[httpx.MockTransport], None]:
    """Wire the script's own `httpx.Client` to a mock transport, remove the
    poll delay, and give it the three environment variables it requires.

    `CXFORGE_VERIFY_DB_URL` is deliberately NOT set: unset is the droplet's
    real configuration (its Postgres publishes no host port), so this is the
    path a human actually runs, and it keeps the test incapable of touching
    any database.
    """
    monkeypatch.setattr(deep, "POLL_INTERVAL_SECONDS", 0.0)
    monkeypatch.setenv("ZENDESK_WEBHOOK_SIGNING_SECRET", SECRET)
    monkeypatch.setenv("PORTAL_TOKEN", "dummy-token")
    monkeypatch.setenv("CXFORGE_VERIFY_TICKET_ID", "3")
    monkeypatch.delenv("CXFORGE_VERIFY_DB_URL", raising=False)

    real_client = httpx.Client

    def install(transport: httpx.MockTransport) -> None:
        def factory(**kwargs: object) -> httpx.Client:
            kwargs.pop("transport", None)
            return real_client(transport=transport, **kwargs)  # type: ignore[arg-type]

        monkeypatch.setattr(deep.httpx, "Client", factory)

    return install


def test_a_passing_run_leaves_its_row_in_place_and_says_which_one(
    deployment: Callable[[httpx.MockTransport], None], capsys: pytest.CaptureFixture[str]
) -> None:
    """The success path: the check passes, the synthetic run STAYS, and its id
    is on stdout. This is the behaviour that replaced the deletes."""
    baseline = [_run_item(7, "3")]
    after = [_run_item(7, "3"), _run_item(42, "3")]
    deployment(_fake_deployment([baseline, after]))

    exit_code = deep.main(["--base-url", "http://deployment.invalid:8080"])
    out = capsys.readouterr().out

    assert exit_code == 0, out
    assert "PASS" in out
    assert "LEFT BEHIND" in out
    assert "runs row 42" in out
    assert "ticket 3 now carries 2 runs rows in the feed: [7, 42]" in out
    # The one thing this must never print on the way out.
    assert not _DESTRUCTIVE_SQL.search(out), out


def test_a_second_run_on_the_reused_ticket_fails_the_check_and_deletes_nothing(
    deployment: Callable[[httpx.MockTransport], None], capsys: pytest.CaptureFixture[str]
) -> None:
    """A real customer comment landing on ticket 3 mid-poll. The old code
    picked `max(run_id)` and deleted it. This must exit 1, name both ids, and
    remove nothing."""
    baseline = [_run_item(7, "3")]
    two_new = [_run_item(7, "3"), _run_item(42, "3"), _run_item(43, "3")]
    deployment(_fake_deployment([baseline, two_new]))

    exit_code = deep.main(["--base-url", "http://deployment.invalid:8080"])
    captured = capsys.readouterr()

    assert exit_code == 1
    assert "2 new runs rows appeared for ticket 3" in captured.err
    assert "Refusing to pick one" in captured.err
    assert "could NOT attribute" in captured.out
    assert "[42, 43]" in captured.out
    assert not _DESTRUCTIVE_SQL.search(captured.out + captured.err)
