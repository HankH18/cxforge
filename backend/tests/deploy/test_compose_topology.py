"""W1-F1 / W1-F2 — the queue, the worker and the tunnel, as compose declares them.

Companion to `test_env_forwarding.py`, which asks whether every variable
reaches every container. This module asks the structural questions: does the
worker exist, does it run the image and the command the frozen contract pins,
does exactly one process own DB seeding, and is the tunnel's token wired the
way a credential should be.

Everything here parses YAML and text. Nothing starts a container, and nothing
in this file is evidence that the tunnel works — see
`deploy/cloudflared/README.md`, which says so at more length.
"""

from __future__ import annotations

import re
import stat
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[3]
DEV_COMPOSE = REPO_ROOT / "docker-compose.yml"
DEPLOY_COMPOSE = REPO_ROOT / "deploy" / "docker-compose.yml"
COMPOSE_FILES = (DEV_COMPOSE, DEPLOY_COMPOSE)
COMPOSE_IDS = [p.relative_to(REPO_ROOT).as_posix() for p in COMPOSE_FILES]

ENV_EXAMPLE = REPO_ROOT / ".env.example"
CLOUDFLARED_DOC = REPO_ROOT / "deploy" / "cloudflared" / "README.md"
COMPOSE_WRAPPER = REPO_ROOT / "deploy" / "compose.sh"

# Pinned by the owner 2026-08-16 and given verbatim to Track A, which owns
# `backend/src/worker/`. If that module renames WorkerSettings, this goes red
# rather than the mismatch surfacing as a container that will not start on a
# droplet.
WORKER_COMMAND = ["arq", "worker.main.WorkerSettings"]

APP_DOCKERFILE = "deploy/Dockerfile.backend"


def _load(path: Path) -> dict[str, Any]:
    loaded = yaml.safe_load(path.read_text())
    assert isinstance(loaded, dict), path
    return loaded


def _services(path: Path) -> dict[str, Any]:
    return _load(path).get("services") or {}


def _env(service: dict[str, Any]) -> dict[str, str]:
    raw = service.get("environment") or {}
    if isinstance(raw, list):
        out = {}
        for item in raw:
            name, _, value = str(item).partition("=")
            out[name] = value
        return out
    return {str(k): "" if v is None else str(v) for k, v in raw.items()}


# --------------------------------------------------------------------------
# F1 — redis + worker in both compose files
# --------------------------------------------------------------------------


@pytest.mark.parametrize("compose_path", COMPOSE_FILES, ids=COMPOSE_IDS)
def test_both_stacks_define_a_redis_broker(compose_path: Path) -> None:
    services = _services(compose_path)
    assert "redis" in services, (
        f"{compose_path.relative_to(REPO_ROOT)} has no `redis` service; the "
        f"arq queue (ADR-002) has no broker to publish to"
    )
    image = str(services["redis"].get("image", ""))
    assert image.startswith("redis:"), image


@pytest.mark.parametrize("compose_path", COMPOSE_FILES, ids=COMPOSE_IDS)
def test_both_stacks_run_the_worker_from_the_application_image(
    compose_path: Path,
) -> None:
    """ADR-002: the worker is the same image as the backend, different command."""
    worker = _services(compose_path).get("worker")
    assert worker is not None, (
        f"{compose_path.relative_to(REPO_ROOT)} has no `worker` service, so "
        f"nothing consumes `cxforge:jobs`"
    )
    build = worker.get("build")
    assert isinstance(build, dict), worker
    assert build.get("dockerfile") == APP_DOCKERFILE, build


@pytest.mark.parametrize("compose_path", COMPOSE_FILES, ids=COMPOSE_IDS)
def test_the_worker_command_is_the_frozen_one(compose_path: Path) -> None:
    worker = _services(compose_path)["worker"]
    assert worker.get("command") == WORKER_COMMAND, (
        f"{compose_path.relative_to(REPO_ROOT)}'s worker command is "
        f"{worker.get('command')!r}, not the pinned {WORKER_COMMAND!r}. This "
        f"string is a cross-track contract: `backend/src/worker/main.py` is "
        f"where `WorkerSettings` lives, and a rename on either side is a "
        f"container that exits at start on a droplet, not a test failure."
    )


@pytest.mark.parametrize("compose_path", COMPOSE_FILES, ids=COMPOSE_IDS)
def test_the_worker_reaches_redis_over_the_compose_network(
    compose_path: Path,
) -> None:
    """Not `localhost`: inside a container that is the container itself.

    `backend/src/worker/settings.py` falls back to `redis://localhost:6379`
    when REDIS_URL is unset, which inside a container is a broker that does
    not exist — so this value being right is what stops the worker from
    silently polling nothing.
    """
    worker = _services(compose_path)["worker"]
    url = _env(worker).get("REDIS_URL", "")
    assert re.match(r"^redis://redis:6379(/\d+)?$", url), (
        f"{compose_path.relative_to(REPO_ROOT)}'s worker REDIS_URL is {url!r}; "
        f"expected the compose service name, e.g. redis://redis:6379/0"
    )


@pytest.mark.parametrize("compose_path", COMPOSE_FILES, ids=COMPOSE_IDS)
def test_the_worker_waits_for_its_dependencies(compose_path: Path) -> None:
    depends = _services(compose_path)["worker"].get("depends_on") or {}
    assert set(depends) >= {"db", "redis"}, depends
    for name in ("db", "redis"):
        assert depends[name].get("condition") == "service_healthy", depends[name]


@pytest.mark.parametrize("compose_path", COMPOSE_FILES, ids=COMPOSE_IDS)
def test_the_worker_overrides_the_images_http_healthcheck(compose_path: Path) -> None:
    """W3-G3: the worker must not inherit the image's HTTP probe.

    A container inherits the IMAGE's ``HEALTHCHECK`` unless the service
    overrides it, and ``deploy/Dockerfile.backend``'s is an HTTP GET to
    ``127.0.0.1:8000/health``. The worker runs ``arq`` and serves no HTTP, so
    it can never pass that probe. Measured on the droplet 2026-08-17 before
    this override existed: ``othram-deploy-worker`` was ``Up (unhealthy)`` with
    a FailingStreak of 25 and ``ConnectionRefusedError: [Errno 111]`` on every
    attempt, while it was in fact consuming ``cxforge:jobs``; and
    ``deploy/compose.sh up -d --build --wait`` exited **1** with "container
    othram-deploy-worker is unhealthy" on an otherwise successful deploy.

    So this asserts three separate things, and each one is a defect this
    package actually hit: that the service declares its OWN healthcheck at all
    (otherwise the image's is inherited); that the declared one does not probe
    the backend's HTTP port; and that it derives arq's health-check key from
    the source of truth instead of hard-coding the queue name a third time.
    """
    worker = _services(compose_path)["worker"]
    healthcheck = worker.get("healthcheck")
    assert isinstance(healthcheck, dict), (
        f"{compose_path.relative_to(REPO_ROOT)}'s worker declares no "
        "`healthcheck:`, so it INHERITS deploy/Dockerfile.backend's HTTP probe "
        "against 127.0.0.1:8000/health — which an arq process serves nothing "
        "on. The container then reports `unhealthy` forever and "
        "`docker compose up --wait` exits non-zero on a working stack."
    )
    test = healthcheck.get("test")
    assert test, f"{compose_path.relative_to(REPO_ROOT)}'s worker healthcheck has no `test`"
    rendered = " ".join(test) if isinstance(test, list) else str(test)
    assert "8000" not in rendered, (
        f"{compose_path.relative_to(REPO_ROOT)}'s worker healthcheck probes port "
        f"8000 ({rendered!r}). That is the backend's uvicorn port; this container "
        "runs `arq` and serves no HTTP, so the probe can only ever fail."
    )
    assert "health_check_key_suffix" in rendered and "QUEUE_NAME" in rendered, (
        f"{compose_path.relative_to(REPO_ROOT)}'s worker healthcheck is "
        f"{rendered!r}; expected it to assemble arq's health-check key from "
        "`worker.settings.QUEUE_NAME` + `arq.constants.health_check_key_suffix`, "
        "so the probe proves the broker link and cannot drift from either name."
    )
    # Imported here, not at module scope: this module is otherwise pure
    # YAML/text parsing, and the point is to compare the compose file against
    # the value the application actually uses.
    from worker.settings import QUEUE_NAME

    assert QUEUE_NAME not in rendered, (
        f"{compose_path.relative_to(REPO_ROOT)}'s worker healthcheck hard-codes "
        f"the queue name {QUEUE_NAME!r}. That string is a frozen contract living "
        "in backend/src/worker/settings.py precisely so it exists once; a copy "
        "here is a third place for it to go stale. Import it in the probe."
    )
    # `arq --check` is the obvious probe and is deliberately NOT used: it
    # imports worker.main (langgraph/langchain/anthropic) and measured 16.5s on
    # the 2-vCPU droplet, versus 1.8s for the key probe.
    assert "--check" not in rendered, (
        f"{compose_path.relative_to(REPO_ROOT)}'s worker healthcheck uses "
        f"`arq --check` ({rendered!r}). Measured on the droplet 2026-08-17 it "
        "takes 16.5s because it imports the whole agent stack, which exceeds any "
        "reasonable healthcheck timeout and burns a core every interval."
    )


def test_the_seed_on_start_defaults_are_the_deliberate_asymmetric_pair() -> None:
    """The two stacks default differently, and that has to be a decision.

    Production defaults to `true` — meaning "seed only into an empty database"
    since `deploy/backend/bootstrap.py` changed — because a fresh droplet must
    come up demo-able with no extra step.

    Dev defaults to `false`, because this stack's `db` is `othram-db`: the
    container the whole test suite runs against, whose `public` schema holds
    whatever the developer seeded by hand. A bare `docker compose up -d`
    should not rewrite that. The suite itself is insulated by per-process
    `OTHRAM_TEST_SCHEMA` schemas; the developer's own data is not.

    Nothing else records this, so without an assertion the difference reads as
    an oversight and the next person "fixes" it.
    """
    dev = _env(_services(DEV_COMPOSE)["worker"]).get("SEED_ON_START")
    assert dev == "${SEED_ON_START:-false}", (
        f"the dev worker's SEED_ON_START default is {dev!r}; it must default "
        f"to false so `docker compose up -d` never reseeds othram-db"
    )
    prod = _env(_services(DEPLOY_COMPOSE)["backend"]).get("SEED_ON_START")
    assert prod == "${SEED_ON_START:-true}", (
        f"the production backend's SEED_ON_START default is {prod!r}; a fresh "
        f"droplet must seed itself on first boot"
    )


def test_exactly_one_process_owns_schema_and_seeding_in_the_production_stack() -> None:
    """The worker must not re-run the deploy bootstrap underneath the backend.

    `deploy/backend/entrypoint.sh` runs `bootstrap.py`, which with
    SEED_ON_START=true TRUNCATEs and reloads `cases`/`kb_chunks`. Two
    containers from the same image both running it means the worker wipes and
    reloads the KB while the backend is already serving requests. The worker
    resets the entrypoint instead, and only starts once the backend is
    healthy — i.e. once the bootstrap it *did* run has finished.
    """
    worker = _services(DEPLOY_COMPOSE)["worker"]
    assert worker.get("entrypoint") == [], (
        "deploy/docker-compose.yml's worker must reset the image ENTRYPOINT "
        "(`entrypoint: []`) so it does not re-run the DB bootstrap; found "
        f"{worker.get('entrypoint')!r}"
    )
    # This removes the SECOND seeder and nothing more. `backend` restarts on
    # its own (`restart: unless-stopped`) and `depends_on` does not govern
    # restarts, so the first seeder still runs while this worker is consuming.
    # backend/tests/deploy/test_bootstrap_seeding.py is what makes that safe.
    assert worker.get("restart") == "unless-stopped"
    depends = worker.get("depends_on") or {}
    assert depends.get("backend", {}).get("condition") == "service_healthy", (
        "the worker must start only after the backend's bootstrap has "
        "completed and it is answering /health"
    )
    backend = _services(DEPLOY_COMPOSE)["backend"]
    assert "entrypoint" not in backend, (
        "the backend must keep the image ENTRYPOINT — it is the one process "
        "that creates the schema"
    )


# --------------------------------------------------------------------------
# F2 — the cloudflared named tunnel (ADR-005). UNVERIFIED against a live
# tunnel; these assertions are about wiring, not about reachability.
# --------------------------------------------------------------------------


def test_the_production_stack_defines_the_tunnel() -> None:
    cloudflared = _services(DEPLOY_COMPOSE).get("cloudflared")
    assert cloudflared is not None, "deploy/docker-compose.yml has no cloudflared service"
    image = str(cloudflared.get("image", ""))
    assert image.startswith("cloudflare/cloudflared:"), image
    assert image != "cloudflare/cloudflared:latest", (
        "pin a version: a floating tag silently changes the transport "
        "component between a rehearsal and a take"
    )
    assert cloudflared.get("restart") == "unless-stopped", (
        "ADR-005 commits to the tunnel surviving a reboot"
    )


def test_the_tunnel_token_has_no_default_and_is_never_committed() -> None:
    """The `:-` empty default is how a missing credential becomes invisible.

    `deploy/docker-compose.yml:98` uses `${LANGFUSE_PUBLIC_KEY:-}` and that is
    exactly the shape that let the stack run for weeks with no key at all
    (`docs/STATE.md §6.2`). The tunnel token does not get one: with no
    default, compose warns and cloudflared exits non-zero instead of quietly
    serving nothing.
    """
    env = _env(_services(DEPLOY_COMPOSE)["cloudflared"])
    assert env.get("TUNNEL_TOKEN") == "${CLOUDFLARE_TUNNEL_TOKEN}", (
        f"expected TUNNEL_TOKEN: ${{CLOUDFLARE_TUNNEL_TOKEN}} with no default; "
        f"found {env.get('TUNNEL_TOKEN')!r}"
    )
    raw = DEPLOY_COMPOSE.read_text()
    assert "${CLOUDFLARE_TUNNEL_TOKEN:-" not in raw, (
        "a `:-` default on the tunnel token renders an empty string and "
        "starts a cloudflared that serves nothing"
    )
    # A tunnel token is a long base64url JWT-shaped blob. If one is ever
    # pasted into a tracked file, fail here rather than after it reaches a
    # remote. The length floor is what keeps the docs' own `eyJ...` placeholder
    # from matching.
    token_shaped = re.compile(r"eyJ[A-Za-z0-9_-]{40,}")
    for tracked in (DEPLOY_COMPOSE, ENV_EXAMPLE, CLOUDFLARED_DOC):
        assert not token_shaped.search(tracked.read_text()), (
            f"{tracked.relative_to(REPO_ROOT)} looks like it contains a real "
            f"tunnel token"
        )


def test_the_tunnel_terminates_at_the_backend_service_not_a_droplet_port() -> None:
    """ADR-005's whole point: no inbound port is opened on the droplet.

    The ingress rule lives in the Cloudflare dashboard (token-managed tunnel),
    so the committed record of it is `deploy/cloudflared/README.md`. This
    binds that record to the compose file, so renaming the backend service or
    changing its container port without updating the runbook fails here.
    """
    doc = CLOUDFLARED_DOC.read_text()
    services = _services(DEPLOY_COMPOSE)
    assert "backend" in services
    assert "backend:8000" in doc, (
        "deploy/cloudflared/README.md must record the exact ingress target "
        "the owner has to configure (OA-3 step 3): backend:8000"
    )
    healthcheck = str(services["backend"].get("healthcheck", {}))
    assert "8000" in healthcheck, (
        "the backend no longer answers on 8000; the tunnel's documented "
        "ingress target is now wrong"
    )


def test_env_example_declares_the_tunnel_variables_empty() -> None:
    lines = ENV_EXAMPLE.read_text().splitlines()
    for name in ("CLOUDFLARE_TUNNEL_TOKEN", "PUBLIC_BASE_URL"):
        matching = [ln for ln in lines if ln.startswith(f"{name}=")]
        assert matching == [f"{name}="], (
            f".env.example must declare {name} as a bare empty assignment; "
            f"found {matching}"
        )


def test_the_tunnel_doc_still_says_it_is_unverified() -> None:
    """`.claude/rules/build-protocol.md` rule 8, made mechanical.

    Nothing about this tunnel has been run. When OA-3 lands and someone
    actually brings it up and reads the effect back, deleting this caveat is
    the deliberate act of recording that — not a tidy-up somebody does on the
    way past.
    """
    doc = CLOUDFLARED_DOC.read_text()
    assert "UNVERIFIED AGAINST A LIVE TUNNEL" in doc, (
        "deploy/cloudflared/README.md dropped its unverified marker. Delete it "
        "only alongside evidence that the tunnel carries traffic — a request "
        "from outside the droplet that reached the app."
    )


# --------------------------------------------------------------------------
# The `set -a; source .env` trap (docs/deploy.md:151)
# --------------------------------------------------------------------------


def test_the_compose_wrapper_is_executable_and_sources_the_env_file() -> None:
    assert COMPOSE_WRAPPER.exists()
    assert COMPOSE_WRAPPER.stat().st_mode & stat.S_IXUSR, "deploy/compose.sh is not executable"


def test_the_compose_wrapper_actually_exports_the_env_file_to_docker(
    tmp_path: Path,
) -> None:
    """Drive the real script with a stub `docker` and read back what it saw.

    The whole value of the wrapper is that the variables are exported by the
    time `docker compose` interpolates, so asserting on the script's text
    would prove nothing. This runs it, with a fake `docker` on PATH that
    writes its own environment to a file, and checks the variable arrived.
    Never touches real docker: the stub is a three-line shell script.
    """
    env_file = tmp_path / "synthetic.env"
    env_file.write_text("CXFORGE_WRAPPER_PROBE=reached-docker\n")

    seen = tmp_path / "docker-env.txt"
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    stub = bin_dir / "docker"
    stub.write_text(
        "#!/usr/bin/env bash\n"
        f'printf "%s\\n" "$CXFORGE_WRAPPER_PROBE" > "{seen}"\n'
        'printf "%s\\n" "$*" >> ' + f'"{seen}"\n'
        "exit 0\n"
    )
    stub.chmod(stub.stat().st_mode | stat.S_IEXEC)

    result = subprocess.run(
        ["bash", str(COMPOSE_WRAPPER), "config", "--quiet"],
        env={
            "PATH": f"{bin_dir}:/usr/bin:/bin",
            "CXFORGE_ENV_FILE": str(env_file),
            "HOME": str(tmp_path),
        },
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert result.returncode == 0, result.stderr
    lines = seen.read_text().splitlines()
    assert lines[0] == "reached-docker", (
        "deploy/compose.sh did not export the env file's variables before "
        f"invoking docker compose; the child saw {lines[0]!r}"
    )
    assert "config --quiet" in lines[1], lines[1]


def test_the_compose_wrapper_refuses_to_run_without_an_env_file(
    tmp_path: Path,
) -> None:
    """Loud and early. Continuing is the silent-default failure it exists to stop."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    canary = bin_dir / "docker"
    canary.write_text(
        "#!/usr/bin/env bash\n"
        'echo "docker must not be invoked with no env file" >&2\n'
        "exit 111\n"
    )
    canary.chmod(canary.stat().st_mode | stat.S_IEXEC)

    result = subprocess.run(
        ["bash", str(COMPOSE_WRAPPER), "config"],
        env={
            "PATH": f"{bin_dir}:/usr/bin:/bin",
            "CXFORGE_ENV_FILE": str(tmp_path / "does-not-exist.env"),
            "HOME": str(tmp_path),
        },
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert result.returncode == 1, result
    assert "no env file" in result.stderr
    assert "docker must not be invoked" not in result.stderr
