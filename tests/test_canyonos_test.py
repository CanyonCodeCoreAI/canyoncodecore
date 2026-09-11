import json
import subprocess

import pytest

from canyonos import test as test_cmd
from canyonos import ui, verify

CONFIG = """\
agents:
  - name: EchoAgent
    entrypoint: echo_agent.py
    provider: EC2
    replicas: 2

  - name: Workflow
    type: workflow
    workflow_file: echo_workflow.py
    api_port: 8080
    provider: EC2
    replicas: 1
"""


@pytest.fixture(autouse=True)
def loud():
    """Every test starts with output enabled; `--json` runs flip it and restore it."""
    ui.set_quiet(False)
    yield
    ui.set_quiet(False)


@pytest.fixture
def project(monkeypatch, tmp_path):
    car = tmp_path / ".car"
    (car / "config").mkdir(parents=True)
    (car / "app").mkdir()
    (car / "config" / "global_controller.yaml").write_text(CONFIG)
    monkeypatch.chdir(tmp_path)
    return tmp_path


# ------------------------------------------------------------------ #
#  Runtime verification                                               #
# ------------------------------------------------------------------ #


@pytest.fixture
def runtime(monkeypatch):
    monkeypatch.setattr(verify.gc, "workflow_endpoints", lambda _port: [])

    def install(images, containers):
        monkeypatch.setattr(verify, "_built_images", lambda: set(images))
        monkeypatch.setattr(verify, "_running_containers", lambda: list(containers))

    return install


ALL_UP = [
    "canyonos-echoagent-0",
    "canyonos-echoagent-1",
    "canyonos-workflow-0",
]


def test_a_complete_deploy_passes(project, runtime):
    runtime({"canyonos-echoagent", "canyonos-workflow"}, ALL_UP)

    result = verify.verify_runtime(str(project / ".car" / "config" / "global_controller.yaml"), 8000)

    assert [(a["name"], a["running"], a["expected"]) for a in result["agents"]] == [
        ("EchoAgent", 2, 2),
        ("Workflow", 1, 1),
    ]


def test_a_short_replica_count_fails(project, runtime):
    runtime({"canyonos-echoagent", "canyonos-workflow"}, ALL_UP[1:])

    with pytest.raises(RuntimeError, match="1 of 2 replicas"):
        verify.verify_runtime(str(project / ".car" / "config" / "global_controller.yaml"), 8000)


def test_an_image_that_was_never_built_fails(project, runtime):
    runtime({"canyonos-workflow"}, ["canyonos-workflow-0"])

    with pytest.raises(RuntimeError, match="canyonos-echoagent was never built"):
        verify.verify_runtime(str(project / ".car" / "config" / "global_controller.yaml"), 8000)


def test_the_workflow_endpoint_falls_back_to_the_configured_port(project, runtime):
    runtime({"canyonos-echoagent", "canyonos-workflow"}, ALL_UP)

    result = verify.verify_runtime(str(project / ".car" / "config" / "global_controller.yaml"), 8000)

    assert result["agents"][0]["endpoint"] is None
    assert result["agents"][1]["endpoint"] == "127.0.0.1:8080"


# ------------------------------------------------------------------ #
#  The command itself                                                 #
# ------------------------------------------------------------------ #


@pytest.fixture
def deployable(monkeypatch, project):
    """A project where every step succeeds unless overridden."""
    calls = {"run_deploy": 0, "quit": 0}

    monkeypatch.setattr(test_cmd, "load_state", lambda: {"container_id": "abc", "port": 8000})
    monkeypatch.setattr(test_cmd, "deploy_status", lambda *_a: None)
    monkeypatch.setattr(test_cmd, "_wait_for_workflow", lambda *a: None)
    monkeypatch.setattr(test_cmd, "verify_runtime", lambda *a: {"agents": []})
    monkeypatch.setattr(test_cmd, "workflow_targets", lambda *a: [("Workflow", "127.0.0.1", 8080)])
    monkeypatch.setattr(test_cmd, "_send_query", lambda *a: "req-1")
    monkeypatch.setattr(test_cmd, "_await_result", lambda *a: {"status": "done", "result": {"r": 1}})
    monkeypatch.setattr(test_cmd, "_log_tail", lambda _cid: "boom")

    def run_deploy(*_a, **_k):
        calls["run_deploy"] += 1
        return {"container_id": "abc", "port": 8000}

    def quit_existing():
        calls["quit"] += 1

    monkeypatch.setattr(test_cmd, "run_deploy", run_deploy)
    monkeypatch.setattr(test_cmd, "quit_existing", quit_existing)
    return calls


def test_a_passing_run_tears_everything_down(deployable):
    assert test_cmd.run_test("hi") == 0
    assert deployable["quit"] == 1


def test_llm_is_stubbed_by_default(monkeypatch, deployable):
    """`canyonos test` hands the stub flag to `canyonos deploy` so no real LLM is hit."""
    seen = {}
    monkeypatch.setattr(
        test_cmd, "run_deploy",
        lambda *_a, **kwargs: seen.update(extra_env=kwargs.get("extra_env")) or {"container_id": "abc", "port": 8000},
    )
    assert test_cmd.run_test("hi") == 0
    assert seen["extra_env"] == {"CANYONOS_LLM_STUB_TEXT": "test"}


def test_real_llm_flag_disables_the_stub(monkeypatch, deployable):
    seen = {}
    monkeypatch.setattr(
        test_cmd, "run_deploy",
        lambda *_a, **kwargs: seen.update(extra_env=kwargs.get("extra_env")) or {"container_id": "abc", "port": 8000},
    )
    assert test_cmd.run_test("hi", llm_stub=None) == 0
    assert seen["extra_env"] is None


def test_the_provider_is_restored_after_the_run(project, deployable):
    config = project / ".car" / "config" / "global_controller.yaml"

    test_cmd.run_test("hi")

    assert config.read_text() == CONFIG


def test_a_failed_deploy_keeps_the_container_and_reads_its_log(monkeypatch, deployable, capsys):
    def boom(*_a):
        raise RuntimeError("the deploy did not come up")

    monkeypatch.setattr(test_cmd, "_wait_for_workflow", boom)

    assert test_cmd.run_test("hi", as_json=True) == 1
    payload = json.loads(capsys.readouterr().out)

    assert deployable["quit"] == 0
    assert payload["log_tail"] == "boom"


def test_a_failure_before_the_deploy_leaves_existing_state_alone(monkeypatch, deployable, capsys):
    """A failure that never gets as far as starting this run's own deploy must
    not tear down whatever deploy was already there -- see
    test_refuses_to_run_when_a_deploy_is_already_up.
    """
    def boom(*_a, **_k):
        raise RuntimeError("Could not sync the project into the container.")

    monkeypatch.setattr(test_cmd, "run_deploy", boom)

    assert test_cmd.run_test("hi", as_json=True) == 1
    payload = json.loads(capsys.readouterr().out)

    assert deployable["quit"] == 0
    assert payload["log_tail"] is None


def test_queries_the_existing_deploy_instead_of_standing_up_its_own(monkeypatch, deployable, capsys):
    """When a deploy is already up, `canyonos test` queries it as it stands
    rather than refusing -- and must not deploy or tear anything down."""
    monkeypatch.setattr(test_cmd, "deploy_status", lambda *_a: {"running": True})

    assert test_cmd.run_test("hi", as_json=True) == 0
    payload = json.loads(capsys.readouterr().out)

    assert deployable["run_deploy"] == 0
    assert deployable["quit"] == 0
    assert payload["against_existing_deploy"] is True
    assert payload["result"] == {"r": 1}
    # The query is the only phase in this path.
    assert [(p["name"], p["ok"]) for p in payload["phases"]] == [("query", True)]


def test_json_mode_prints_one_object_and_nothing_else(deployable, capsys):
    assert test_cmd.run_test("a prompt", as_json=True) == 0
    payload = json.loads(capsys.readouterr().out)

    assert payload["ok"] is True
    assert payload["query"] == "a prompt"
    assert payload["result"] == {"r": 1}
    assert payload["error"] is None
    assert [(p["name"], p["ok"]) for p in payload["phases"]] == [
        ("deploy", True),
        ("verify_runtime", True),
        ("query", True),
    ]


def test_a_workflow_error_is_reported_as_a_failure(monkeypatch, deployable, capsys):
    monkeypatch.setattr(
        test_cmd, "_await_result", lambda *a: {"status": "error", "error": "agent blew up"}
    )

    assert test_cmd.run_test("hi", as_json=True) == 1

    assert json.loads(capsys.readouterr().out)["error"] == "agent blew up"


def test_a_flat_layout_project_deploys_fine_with_no_car_directory(monkeypatch, tmp_path, deployable, capsys):
    legacy = tmp_path / "legacy" / "config"
    legacy.mkdir(parents=True)
    (legacy / "global_controller.yaml").write_text(CONFIG)
    monkeypatch.chdir(tmp_path / "legacy")

    assert test_cmd.run_test("hi", as_json=True) == 0
    payload = json.loads(capsys.readouterr().out)

    assert [(p["name"], p["ok"]) for p in payload["phases"]] == [
        ("deploy", True),
        ("verify_runtime", True),
        ("query", True),
    ]


# ------------------------------------------------------------------ #
#  Query body derived from the workflow signature                     #
# ------------------------------------------------------------------ #


def _write_workflow(project, source):
    (project / ".car" / "app" / "echo_workflow.py").write_text(source)
    return str(project / ".car" / "config" / "global_controller.yaml")


def test_query_body_uses_the_real_entrypoint_param_name(project):
    config = _write_workflow(
        project,
        "def run(ticker):\n    return {}\n\ndeploy(run)\n",
    )

    route, body = test_cmd._query_route_and_body(config, "AAPL")

    assert route == "run"
    assert body == {"ticker": "AAPL"}


def test_query_body_maps_prompt_to_first_param_and_keeps_defaults(project):
    config = _write_workflow(
        project,
        "def main(query, n_candidates=5):\n    return {}\n\ndeploy(main)\n",
    )

    route, body = test_cmd._query_route_and_body(config, "hi")

    assert route == "main"
    assert body == {"query": "hi", "n_candidates": 5}


def test_query_body_falls_back_to_query_when_introspection_fails(project):
    # No workflow file on disk -> workflow_entrypoint returns None.
    config = str(project / ".car" / "config" / "global_controller.yaml")

    route, body = test_cmd._query_route_and_body(config, "hi")

    assert route == test_cmd.WORKFLOW_ROUTE
    assert body == {"query": "hi"}


# ------------------------------------------------------------------ #
#  Docker plumbing                                                    #
# ------------------------------------------------------------------ #


def test_running_containers_are_filtered_to_the_runtime_prefix(monkeypatch):
    seen = []

    def fake_run(argv, **_):
        seen.append(argv)
        return subprocess.CompletedProcess(argv, 0, "canyonos-echoagent-0\n", "")

    monkeypatch.setattr(verify.subprocess, "run", fake_run)

    assert verify._running_containers() == ["canyonos-echoagent-0"]
    # Broad prefix filter -- the per-agent regex, not this filter, excludes
    # the GC/redis/dashboard containers it also pulls in.
    assert "name=canyonos-" in seen[0]


def test_replica_count_matches_agents_and_excludes_infra(monkeypatch):
    """Agent replicas (`canyonos-<name>-<i>`) are counted; the GC/redis/dashboard
    containers pulled in by the broad `canyonos-` filter are excluded.
    """
    containers = [
        "canyonos-priceagent-0",       # agent replica -- counts
        "canyonos-global-controller",  # must NOT count
        "canyonos-redis-localhost",    # must NOT count
        "canyonos-dashboard-db-1",     # must NOT count
    ]
    assert sum(1 for c in containers if verify._replica_pattern("PriceAgent").match(c)) == 1
    assert sum(1 for c in containers if verify._replica_pattern("Controller").match(c)) == 0
