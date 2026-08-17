"""W2-C2 — structured logging and where it is configured.

Before this, the application had two ``logger.warning`` calls and no logging
configuration at all: no timestamps, no logger names, no context, every
``logger.info`` discarded by Python's last-resort handler.

The interesting half is not the formatter. It is **where the configuration
is installed in the worker**, because that is where it silently does not
work. `arq/cli.py` imports the settings module (line 40) and calls
``logging.config.dictConfig(default_log_config(verbose))`` afterwards (line
45), so a `configure_logging()` at import time is applied and then partly
undone — ``dictConfig`` closes every existing handler on the way past.
`arq.worker.Worker.main` awaits ``on_startup`` after all of it.

Rather than assert that ordering by reading arq's source, the subprocess
below **replays arq's own sequence using arq's own ``default_log_config``**,
and runs the control (the same sequence without the hook) to show the
assertion can fail. It deliberately does not require Redis: the offline
suite must not grow a broker dependency. A real ``arq … --burst`` run
against a real Redis was used to verify the same property end to end —
5 structured lines, 2 unstructured (arq's pre-hook banner) — and that
measurement is recorded in `worker.main.startup`'s docstring, since it
cannot live in an offline test.

These live in `backend/tests/ingress/` with the rest of the worker's tests
(BUILD-PLAN W1-A7: "do NOT create backend/tests/worker/").
"""

from __future__ import annotations

import asyncio
import io
import json
import logging
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from logging_setup import JsonLogFormatter, configure_logging
from worker import main as worker_main

REPO_ROOT = Path(__file__).resolve().parents[3]
BACKEND_SRC = REPO_ROOT / "backend" / "src"


def _emit(**kwargs: Any) -> dict[str, Any]:
    """Send one record through a real handler and read the line back."""
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(JsonLogFormatter(service="test"))
    logger = logging.getLogger("w2c2.probe")
    logger.handlers = [handler]
    logger.propagate = False
    logger.setLevel(logging.DEBUG)
    try:
        logger.info(kwargs.pop("message", "hello %s"), *kwargs.pop("args", ()), **kwargs)
    finally:
        logger.handlers = []
    payload = json.loads(stream.getvalue().strip())
    assert isinstance(payload, dict)
    return payload


# --------------------------------------------------------------------------
# The formatter
# --------------------------------------------------------------------------


def test_a_log_line_is_one_json_object_with_the_fields_prose_had_to_hide() -> None:
    payload = _emit(message="agent run failed for ticket %s", args=("t-99",))

    assert payload["level"] == "INFO"
    assert payload["logger"] == "w2c2.probe"
    assert payload["service"] == "test"
    assert payload["message"] == "agent run failed for ticket t-99"
    # A timestamp at all — the last-resort handler emitted none.
    assert payload["ts"].startswith("20")


def test_extra_keys_become_top_level_fields_rather_than_interpolated_prose() -> None:
    """The point of the change: ``jq 'select(.ticket_id=="123")'`` works,
    instead of grepping a sentence for a substring."""
    payload = _emit(message="agent run completed", extra={"ticket_id": "123", "duration_s": 4.5})

    assert payload["ticket_id"] == "123"
    assert payload["duration_s"] == 4.5


def test_framework_internals_do_not_leak_into_the_structured_fields() -> None:
    """``extra`` is the caller's namespace. If the reserved-attribute list
    fell behind a Python release, `LogRecord` internals (``taskName``,
    ``relativeCreated``, ``msecs``) would start appearing as if the call
    site had put them there."""
    payload = _emit(message="plain", extra={"ticket_id": "1"})

    assert set(payload) == {"ts", "level", "logger", "service", "message", "ticket_id"}


def test_an_exception_arrives_as_a_field_not_as_extra_untagged_lines() -> None:
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(JsonLogFormatter(service="test"))
    logger = logging.getLogger("w2c2.exc")
    logger.handlers = [handler]
    logger.propagate = False
    try:
        raise RuntimeError("anthropic overloaded_error 529")
    except RuntimeError:
        logger.error("agent run failed", exc_info=True, extra={"ticket_id": "7"})
    finally:
        logger.handlers = []

    # One line, still parseable — a bare traceback would have produced
    # several lines that no JSON reader could consume.
    lines = stream.getvalue().strip().splitlines()
    assert len(lines) == 1
    payload = json.loads(lines[0])
    assert "overloaded_error 529" in payload["error"]
    assert payload["ticket_id"] == "7"


def test_an_unserialisable_value_does_not_take_the_log_line_down() -> None:
    """A log line is what you have left when everything else has gone
    wrong; it must not be the thing that raises."""

    class Opaque:
        def __repr__(self) -> str:
            return "<Opaque>"

    payload = _emit(message="odd", extra={"thing": Opaque()})
    assert payload["thing"] == "<Opaque>"


def test_configuring_twice_does_not_duplicate_every_line() -> None:
    """`main` configures at import and the worker from a startup hook; a
    process that did both must not emit everything twice."""
    root = logging.getLogger()
    saved_handlers, saved_level = root.handlers[:], root.level
    try:
        stream = io.StringIO()
        configure_logging(service="a", stream=stream)
        configure_logging(service="b", stream=stream)
        logging.getLogger("w2c2.dup").warning("once")
        lines = stream.getvalue().strip().splitlines()
    finally:
        root.handlers = saved_handlers
        root.setLevel(saved_level)

    assert len(lines) == 1
    assert json.loads(lines[0])["service"] == "b"


# --------------------------------------------------------------------------
# Where the worker configures it (the part that silently would not work)
# --------------------------------------------------------------------------

_ARQ_SEQUENCE = """
import asyncio, json, logging, logging.config, sys
sys.path.insert(0, {src!r})
from worker import main as worker_main            # arq/cli.py line 40
from arq.logs import default_log_config
logging.config.dictConfig(default_log_config(False))   # arq/cli.py line 45
{hook}
logging.getLogger("worker.main").info("probe", extra={{"ticket_id": "t-1"}})
logging.getLogger("arq.worker").info("an arq line")
"""


def _run_arq_sequence(*, with_hook: bool) -> list[str]:
    hook = (
        "asyncio.run(worker_main.WorkerSettings.on_startup({}))"
        if with_hook
        else "pass  # the hook that would configure logging"
    )
    env = {
        k: v
        for k, v in os.environ.items()
        # The child must not look like a pytest process: `worker/main.py`
        # gates its `.env` load on exactly that.
        if k not in {"PYTEST_VERSION", "PYTEST_CURRENT_TEST"}
    }
    result = subprocess.run(
        [sys.executable, "-c", _ARQ_SEQUENCE.format(src=str(BACKEND_SRC), hook=hook)],
        cwd=str(REPO_ROOT),
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, result.stderr
    return [ln for ln in (result.stdout + result.stderr).splitlines() if ln.strip()]


def test_the_worker_logs_structured_json_after_arq_has_configured_logging() -> None:
    lines = _run_arq_sequence(with_hook=True)
    payloads = [json.loads(ln) for ln in lines]

    probe = next(p for p in payloads if p["message"] == "probe")
    assert probe["service"] == "worker"
    assert probe["ticket_id"] == "t-1"
    # arq's own logger lost its plain handler and joined the same stream,
    # exactly once.
    arq_lines = [p for p in payloads if p["logger"] == "arq.worker"]
    assert len(arq_lines) == 1, payloads


def test_the_control_shows_arq_leaves_logging_unstructured_on_its_own() -> None:
    """Without the hook the same sequence produces prose, so the assertion
    above is about `startup` and not about something arq does anyway."""
    lines = _run_arq_sequence(with_hook=False)

    parsed = []
    for line in lines:
        try:
            parsed.append(json.loads(line))
        except json.JSONDecodeError:
            parsed.append(None)
    assert all(p is None for p in parsed), lines
    # And the ticket id is nowhere to be found — the whole reason `extra`
    # exists. Python's last-resort handler drops INFO entirely, so the probe
    # line is not merely unstructured, it is absent.
    assert not any("t-1" in line for line in lines), lines


_API_SEQUENCE = """
import logging, sys
sys.path.insert(0, {src!r})
import main
logging.getLogger("probe").warning("api probe", extra={{"ticket_id": "t-2"}})
"""


@pytest.mark.parametrize("look_like_pytest", [False, True], ids=["real-process", "under-pytest"])
def test_the_api_process_configures_logging_at_import_but_never_inside_pytest(
    look_like_pytest: bool,
) -> None:
    """`main.py` configures at import, behind the same `PYTEST_VERSION` gate
    that guards its `.env` load — `backend/tests/portal/**` imports this
    module for `app`, and reconfiguring the root logger inside a pytest
    process fights `caplog` and sprays JSON through the suite's output.

    Both directions are asserted, because a gate that never opens and a gate
    that never closes are equally wrong and only one of them is visible from
    inside the suite."""
    env = {
        k: v
        for k, v in os.environ.items()
        if k not in {"PYTEST_VERSION", "PYTEST_CURRENT_TEST"}
    }
    if look_like_pytest:
        env["PYTEST_VERSION"] = "8.3.0"
    result = subprocess.run(
        [sys.executable, "-c", _API_SEQUENCE.format(src=str(BACKEND_SRC))],
        cwd=str(REPO_ROOT),
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, result.stderr
    lines = [ln for ln in (result.stdout + result.stderr).splitlines() if ln.strip()]
    assert len(lines) == 1, lines

    if look_like_pytest:
        # Python's last-resort handler: bare message, no JSON, no context.
        assert lines[0] == "api probe"
    else:
        payload = json.loads(lines[0])
        assert payload["service"] == "api"
        assert payload["ticket_id"] == "t-2"


# --------------------------------------------------------------------------
# What the worker actually says about a run
# --------------------------------------------------------------------------


@pytest.fixture
def zendesk_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """`run_ticket` builds a real `ZendeskAdapter()` before it calls
    `run_agent`, and that constructor raises without these — a missing
    credential would masquerade as the failure a test meant to induce.
    Mirrors `test_dispatch.py`'s fixture of the same name (it is local to
    that module)."""
    monkeypatch.setenv("ZENDESK_SUBDOMAIN", "cxforge-logging-test")
    monkeypatch.setenv("ZENDESK_OAUTH_TOKEN", "not-a-real-oauth-token")


def test_a_successful_run_says_so_with_its_ticket_id_and_duration(
    zendesk_env: None, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """`worker.main.run_ticket`'s own docstring: a swallowed exception is
    booked by arq as ``success = True``, so every arq-derived metric shows a
    healthy worker while every run fails. Until W2-C2 a *successful* run
    said nothing at all, so there was no signal on either side to compare."""
    monkeypatch.setattr(worker_main, "run_agent", lambda *a, **k: None)
    payload = {
        "ticket_id": "t-42",
        "comment_id": "c-1",
        "received_at": "2026-08-16T00:00:00+00:00",
    }

    with caplog.at_level(logging.INFO, logger="worker.main"):
        asyncio.run(worker_main.run_ticket({}, payload))

    records = [r for r in caplog.records if r.name == "worker.main"]
    messages = [r.getMessage() for r in records]
    assert "agent run started" in messages
    assert "agent run completed" in messages
    assert all(getattr(r, "ticket_id", None) == "t-42" for r in records)
    completed = next(r for r in records if r.getMessage() == "agent run completed")
    assert isinstance(getattr(completed, "duration_s", None), float)


def test_a_failed_run_still_carries_the_ticket_id_as_a_field(
    zendesk_env: None, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """The existing ERROR text is unchanged (other tests read it); what is
    new is that the ticket id is now a queryable field and not only a
    substring of a sentence."""

    def _boom(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("anthropic overloaded_error 529")

    monkeypatch.setattr(worker_main, "run_agent", _boom)
    monkeypatch.setattr(worker_main, "release_dedup_row", lambda job: None)
    payload = {
        "ticket_id": "t-43",
        "comment_id": "c-9",
        "received_at": "2026-08-16T00:00:00+00:00",
    }

    with caplog.at_level(logging.INFO, logger="worker.main"):
        asyncio.run(worker_main.run_ticket({}, payload))

    errors = [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert len(errors) == 1
    assert getattr(errors[0], "ticket_id", None) == "t-43"
    assert getattr(errors[0], "comment_id", None) == "c-9"
    assert "overloaded_error 529" in errors[0].getMessage()
    # "agent run completed" must NOT be claimed for a run that raised.
    assert "agent run completed" not in [r.getMessage() for r in caplog.records]
