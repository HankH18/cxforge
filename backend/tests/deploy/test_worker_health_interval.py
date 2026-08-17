"""The worker healthcheck's detection window — bound to a number, not a hope.

WHY. Both compose files probe the worker by checking that arq's health key
exists in Redis. `arq.worker.Worker.record_health` writes that key with
``psetex((health_check_interval + 1) * 1000)`` and arq's default interval is
**3600s**, so before ``HEALTH_CHECK_INTERVAL_SECONDS`` existed a worker whose
main loop stopped kept a *valid* key for up to an hour. The probe could only
catch a process that had exited — which the container catches anyway, since
``arq`` is its command. A green healthcheck for an hour after the worker stops
working was written down in a YAML comment and nowhere else
(``docs/BUILD-PLAN.md §10.7f`` now records it).

These assertions are deliberately about the RELATIONSHIP between three numbers
that live in three different files — the arq setting, the probe interval and
the retry count — because that product, not any one of them, is the time a
hung worker reads as healthy. Changing any of the three re-opens the window,
and that is what should fail here.

Everything runs offline: ``Worker(**get_kwargs(...))`` builds without opening a
connection (arq creates its pool in ``main()``), so this needs no broker.
"""

from __future__ import annotations

import asyncio
import inspect
import re
from pathlib import Path
from typing import Any, cast

import yaml
from arq.constants import health_check_key_suffix
from arq.worker import Worker, create_worker, get_kwargs

from worker.main import HEALTH_CHECK_INTERVAL_SECONDS, WorkerSettings
from worker.settings import QUEUE_NAME

REPO_ROOT = Path(__file__).resolve().parents[3]
COMPOSE_FILES = (
    REPO_ROOT / "docker-compose.yml",
    REPO_ROOT / "deploy" / "docker-compose.yml",
)

# The window a hung worker may read as healthy before the probe says otherwise.
# Two minutes is ~10 agent runs (~11.5s each): long enough that no legitimate
# pause trips it, short enough that a human watching a demo notices the
# container go unhealthy rather than the hour arq's default allowed.
MAX_DETECTION_WINDOW_SECONDS = 180

_DURATION = re.compile(r"^(?:(\d+)m)?(?:(\d+)s)?$")


def _seconds(duration: str) -> int:
    """Parse compose's ``30s`` / ``1m30s`` duration form."""
    match = _DURATION.match(duration.strip())
    assert match and duration.strip(), f"unparsed compose duration: {duration!r}"
    minutes, seconds = match.groups()
    return int(minutes or 0) * 60 + int(seconds or 0)


def _built_worker() -> Worker:
    """The ``Worker`` arq itself would build from these settings, no broker.

    Via ``arq.worker.create_worker`` inside a running loop, which is exactly how
    ``backend/tests/ingress/test_queue_contract.py`` does it and for the same two
    reasons recorded there: ``Worker.__init__`` calls
    ``asyncio.get_event_loop()`` (deprecated outside a loop), and
    ``handle_signals=False`` stops the worker installing SIGTERM handlers that
    pytest reports later as an unraisable exception. ``create_worker`` routes
    through ``get_kwargs``, so this is arq's real extraction path and not a
    re-implementation of it. No connection is opened — arq creates its pool in
    ``main()``.
    """

    async def build() -> Worker:
        return create_worker(WorkerSettings, handle_signals=False)

    return asyncio.run(build())


def _settings_kwargs() -> dict[str, Any]:
    """What arq itself would pass to ``Worker(...)`` for these settings.

    ``arq.worker.get_kwargs`` is annotated ``-> dict[str, NameError]`` upstream
    (arq 0.28.0) — a typo in arq's own signature, not a claim about the values,
    which are the ordinary worker kwargs. Cast rather than ``# type: ignore``
    so the mismatch is named where a reader can check it against arq.
    """
    return cast(dict[str, Any], get_kwargs(WorkerSettings))


def _worker_healthcheck(path: Path) -> dict[str, Any]:
    compose = yaml.safe_load(path.read_text())
    worker = compose["services"]["worker"]
    healthcheck = worker.get("healthcheck")
    assert healthcheck, f"{path} has no worker healthcheck at all"
    return dict(healthcheck)


def test_the_setting_is_in_the_class_body_where_arq_can_actually_see_it() -> None:
    """``arq.worker.get_kwargs`` reads ``settings_cls.__dict__``, so a value
    inherited from ``WorkerSettingsBase`` — or set anywhere but this class
    body — is never passed to ``Worker(...)`` and the 3600s default silently
    stands. Asserted through arq's own extraction rather than by attribute
    lookup, which would happily read an inherited value and pass while the
    running worker used the default."""
    assert "health_check_interval" in vars(WorkerSettings)
    assert _settings_kwargs()["health_check_interval"] == HEALTH_CHECK_INTERVAL_SECONDS
    # And arq accepts it under that name: a rename in arq would raise here
    # rather than leave a silently-ignored setting.
    assert _built_worker().health_check_interval == HEALTH_CHECK_INTERVAL_SECONDS


def test_the_override_is_load_bearing_against_arqs_own_default() -> None:
    """If arq ever ships a sane default, this override stops being the thing
    that closes the window and the reasoning in ``worker/main.py`` needs
    re-deciding rather than silently inheriting."""
    default = inspect.signature(Worker.__init__).parameters["health_check_interval"].default
    assert default >= 3600, (
        f"arq's default health_check_interval is now {default}s, not 3600s — "
        "re-read worker.main.HEALTH_CHECK_INTERVAL_SECONDS's comment and decide "
        "again whether the override is still the right value"
    )
    assert HEALTH_CHECK_INTERVAL_SECONDS < default


def test_a_stale_health_key_cannot_outlive_one_probe_window() -> None:
    """The key's TTL is ``interval + 1`` (``Worker.record_health``). If that
    exceeds the docker probe's own interval, the probe can observe a key that
    a stopped worker wrote arbitrarily long ago — which is precisely the
    3600s defect, in smaller form."""
    for path in COMPOSE_FILES:
        healthcheck = _worker_healthcheck(path)
        probe_interval = _seconds(str(healthcheck["interval"]))
        key_ttl = HEALTH_CHECK_INTERVAL_SECONDS + 1
        assert key_ttl <= probe_interval + 1, (
            f"{path}: the health key lives {key_ttl}s but the probe only looks every "
            f"{probe_interval}s, so a worker that stopped can still be reported healthy"
        )


def test_the_total_detection_window_is_bounded() -> None:
    """Key TTL + (probe interval x retries) is the real answer to "how long can
    a hung worker look healthy?", and it is spread across three files. This is
    the assertion that goes red if any one of them is loosened."""
    for path in COMPOSE_FILES:
        healthcheck = _worker_healthcheck(path)
        probe_interval = _seconds(str(healthcheck["interval"]))
        retries = int(healthcheck["retries"])
        window = (HEALTH_CHECK_INTERVAL_SECONDS + 1) + probe_interval * retries
        assert window <= MAX_DETECTION_WINDOW_SECONDS, (
            f"{path}: a hung worker would read as healthy for up to {window}s "
            f"(key TTL {HEALTH_CHECK_INTERVAL_SECONDS + 1}s + {retries} x "
            f"{probe_interval}s of retries), over the {MAX_DETECTION_WINDOW_SECONDS}s "
            "bound. With arq's 3600s default this number was 3691s."
        )


def test_the_probe_checks_the_key_arq_actually_writes() -> None:
    """The compose probe assembles the key from ``worker.settings.QUEUE_NAME``
    plus ``arq.constants.health_check_key_suffix`` rather than a literal. This
    pins that the assembly matches what a Worker built from these very settings
    would write — the two could otherwise agree on a string and disagree with
    arq."""
    assert _built_worker().health_check_key == QUEUE_NAME + health_check_key_suffix
    for path in COMPOSE_FILES:
        probe = " ".join(str(part) for part in _worker_healthcheck(path)["test"])
        assert "QUEUE_NAME" in probe and "health_check_key_suffix" in probe, (
            f"{path}: the worker probe stopped deriving the health key from "
            "QUEUE_NAME + arq's own suffix, so it can now drift from the key "
            "arq writes"
        )
