import hashlib
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


def report(errors=0, warnings=0, findings=(), ventis=False):
    return {
        "capabilities": {"ventis": ventis},
        "errors": errors,
        "warnings": warnings,
        "findings": list(findings),
    }


def finding(check, level="ERROR"):
    return {"check": check, "level": level, "path": "config/x.yaml", "line": 1, "summary": "s"}


# ------------------------------------------------------------------ #
#  Locating the porting validator                                     #
# ------------------------------------------------------------------ #


def test_validator_prefers_the_project_skill(monkeypatch, project, tmp_path):
    codex = tmp_path / "codex-skill"
    codex.mkdir()
    (codex / "validate.py").write_text("")
    local = project / ".claude" / "skills" / "porting-to-canyonos"
    local.mkdir(parents=True)
    (local / "validate.py").write_text("")

    monkeypatch.setattr(
        verify,
        "AGENTS",
        {
            "claude": {"skill_dirs": {"local": ".claude/skills/porting-to-canyonos"}},
            "codex": {"skill_dirs": {"global": str(codex)}},
        },
    )
    assert verify._find_validator(str(project)) == str(local / "validate.py")


def test_validator_falls_back_to_the_codex_skill(monkeypatch, project, tmp_path):
    codex = tmp_path / "codex-skill"
    codex.mkdir()
    (codex / "validate.py").write_text("")

    monkeypatch.setattr(
        verify,
        "AGENTS",
        {
            "claude": {"skill_dirs": {"local": ".claude/skills/porting-to-canyonos"}},
            "codex": {"skill_dirs": {"global": str(codex)}},
        },
    )
    assert verify._find_validator(str(project)) == str(codex / "validate.py")


def test_validator_is_fetched_when_nothing_is_installed(monkeypatch, project, tmp_path):
    cache = tmp_path / "cache"
    monkeypatch.setattr(verify, "AGENTS", {})
    monkeypatch.setattr(verify, "SKILL_CACHE_DIR", str(cache))

    def fake_install(dest):
        assert dest == str(cache)
        cache.mkdir()
        (cache / "validate.py").write_text("")
        return True

    monkeypatch.setattr(verify, "install_skill", fake_install)
    assert verify._find_validator(str(project)) == str(cache / "validate.py")


def test_a_validator_that_cannot_be_fetched_does_not_stop_the_run(monkeypatch, project, tmp_path):
    monkeypatch.setattr(verify, "AGENTS", {})
    monkeypatch.setattr(verify, "SKILL_CACHE_DIR", str(tmp_path / "empty-cache"))
    monkeypatch.setattr(verify, "install_skill", lambda _dest: False)

    summary = verify.verify_build_artifact(str(project))

    assert summary == {"errors": 0, "warnings": 0, "findings": [], "stale": []}


# ------------------------------------------------------------------ #
#  Reading the validator's report                                     #
# ------------------------------------------------------------------ #


def test_validator_errors_fail_the_phase(monkeypatch, project):
    monkeypatch.setattr(verify, "_find_validator", lambda _root: "/validate.py")
    monkeypatch.setattr(
        verify, "_run_validator", lambda *_: report(errors=1, findings=[finding("V002")])
    )

    with pytest.raises(verify.VerificationError):
        verify.verify_build_artifact(str(project))


def test_validator_warnings_pass(monkeypatch, project):
    monkeypatch.setattr(verify, "_find_validator", lambda _root: "/validate.py")
    monkeypatch.setattr(
        verify,
        "_run_validator",
        lambda *_: report(warnings=1, findings=[finding("V018", "WARN")]),
    )

    summary = verify.verify_build_artifact(str(project))

    assert (summary["errors"], summary["warnings"]) == (0, 1)


def test_rules_needing_ventis_are_dropped_when_it_is_not_importable(monkeypatch, project):
    monkeypatch.setattr(verify, "_find_validator", lambda _root: "/validate.py")
    monkeypatch.setattr(
        verify,
        "_run_validator",
        lambda *_: report(errors=1, findings=[finding("V030"), finding("V031", "INFO")]),
    )

    summary = verify.verify_build_artifact(str(project))

    assert summary["errors"] == 0
    assert summary["findings"] == []


def test_rules_needing_ventis_are_kept_when_it_is_importable(monkeypatch, project):
    monkeypatch.setattr(verify, "_find_validator", lambda _root: "/validate.py")
    monkeypatch.setattr(
        verify,
        "_run_validator",
        lambda *_: report(errors=1, findings=[finding("V030")], ventis=True),
    )

    with pytest.raises(verify.VerificationError):
        verify.verify_build_artifact(str(project))


def test_a_missing_car_directory_is_an_error(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    with pytest.raises(verify.VerificationError, match="Run `canyonos build` first"):
        verify.verify_build_artifact(str(tmp_path))


# ------------------------------------------------------------------ #
#  Source drift                                                       #
# ------------------------------------------------------------------ #


def write_porting_state(project, entries):
    (project / ".car" / "config" / ".porting-state.json").write_text(
        json.dumps({"version": 1, "source_files": entries})
    )


def sha256(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_unchanged_sources_are_not_reported_as_stale(project):
    source = project / "echo_agent.py"
    source.write_text("x = 1\n")
    write_porting_state(project, {"echo_agent.py": sha256(source)})

    assert verify._stale_sources(str(project), str(project / ".car")) == []


def test_changed_and_deleted_sources_are_reported(project):
    source = project / "echo_agent.py"
    source.write_text("x = 2\n")
    write_porting_state(
        project, {"echo_agent.py": "0" * 64, "gone.py": "0" * 64}
    )

    assert verify._stale_sources(str(project), str(project / ".car")) == [
        "echo_agent.py",
        "gone.py",
    ]


def test_the_skills_own_files_are_not_reported_as_drift(project):
    write_porting_state(project, {".claude/skills/porting-to-canyonos/SKILL.md": "0" * 64})

    assert verify._stale_sources(str(project), str(project / ".car")) == []


def test_a_hand_written_artifact_has_no_state_to_compare(project):
    assert verify._stale_sources(str(project), str(project / ".car")) == []


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
    "ventis-local-echoagent-0",
    "ventis-local-echoagent-1",
    "ventis-local-workflow-0",
]


def test_a_complete_deploy_passes(project, runtime):
    runtime({"ventis-echoagent", "ventis-workflow"}, ALL_UP)

    result = verify.verify_runtime(str(project / ".car" / "config" / "global_controller.yaml"), 8000)

    assert [(a["name"], a["running"], a["expected"]) for a in result["agents"]] == [
        ("EchoAgent", 2, 2),
        ("Workflow", 1, 1),
    ]


def test_a_short_replica_count_fails(project, runtime):
    runtime({"ventis-echoagent", "ventis-workflow"}, ALL_UP[1:])

    with pytest.raises(verify.VerificationError, match="1 of 2 replicas"):
        verify.verify_runtime(str(project / ".car" / "config" / "global_controller.yaml"), 8000)


def test_an_image_that_was_never_built_fails(project, runtime):
    runtime({"ventis-workflow"}, ["ventis-local-workflow-0"])

    with pytest.raises(verify.VerificationError, match="ventis-echoagent was never built"):
        verify.verify_runtime(str(project / ".car" / "config" / "global_controller.yaml"), 8000)


def test_the_workflow_endpoint_falls_back_to_the_configured_port(project, runtime):
    runtime({"ventis-echoagent", "ventis-workflow"}, ALL_UP)

    result = verify.verify_runtime(str(project / ".car" / "config" / "global_controller.yaml"), 8000)

    assert result["agents"][0]["endpoint"] is None
    assert result["agents"][1]["endpoint"] == "127.0.0.1:8080"


# ------------------------------------------------------------------ #
#  The command itself                                                 #
# ------------------------------------------------------------------ #


@pytest.fixture
def deployable(monkeypatch, project):
    """A project where every step past the build check succeeds unless overridden."""
    calls = {"post_deploy": 0, "quit": 0}

    monkeypatch.setattr(test_cmd, "verify_build_artifact", lambda *a: {"warnings": 0, "stale": []})
    monkeypatch.setattr(test_cmd, "run_init", lambda banner=True: None)
    monkeypatch.setattr(test_cmd, "run_sync", lambda: True)
    monkeypatch.setattr(test_cmd, "load_state", lambda: {"container_id": "abc", "port": 8000})
    monkeypatch.setattr(test_cmd, "_port_in_use", lambda _port: False)
    monkeypatch.setattr(test_cmd, "_wait_for_workflow", lambda *a: None)
    monkeypatch.setattr(test_cmd, "verify_runtime", lambda *a: {"agents": []})
    monkeypatch.setattr(test_cmd, "workflow_targets", lambda *a: [("Workflow", "127.0.0.1", 8080)])
    monkeypatch.setattr(test_cmd, "_send_query", lambda *a: "req-1")
    monkeypatch.setattr(test_cmd, "_await_result", lambda *a: {"status": "done", "result": {"r": 1}})
    monkeypatch.setattr(test_cmd, "_log_tail", lambda _cid: "boom")

    def post_deploy(*_a, **_k):
        calls["post_deploy"] += 1

    def quit_existing():
        calls["quit"] += 1

    monkeypatch.setattr(test_cmd, "post_deploy", post_deploy)
    monkeypatch.setattr(test_cmd, "quit_existing", quit_existing)
    return calls


def test_a_passing_run_tears_everything_down(deployable):
    assert test_cmd.run_test("hi") == 0
    assert deployable["quit"] == 1


def test_the_provider_is_restored_after_the_run(project, deployable):
    config = project / ".car" / "config" / "global_controller.yaml"

    test_cmd.run_test("hi")

    assert config.read_text() == CONFIG


def test_an_occupied_api_port_fails_before_the_deploy(monkeypatch, deployable, capsys):
    monkeypatch.setattr(test_cmd, "_port_in_use", lambda _port: True)

    assert test_cmd.run_test("hi", as_json=True) == 1
    payload = json.loads(capsys.readouterr().out)

    assert deployable["post_deploy"] == 0
    assert "8080 is already in use" in payload["error"]


def test_a_failed_deploy_keeps_the_container_and_reads_its_log(monkeypatch, deployable, capsys):
    def boom(*_a):
        raise test_cmd._TestFailed("the deploy did not come up")

    monkeypatch.setattr(test_cmd, "_wait_for_workflow", boom)

    assert test_cmd.run_test("hi", as_json=True) == 1
    payload = json.loads(capsys.readouterr().out)

    assert deployable["quit"] == 0
    assert payload["log_tail"] == "boom"


def test_a_failure_before_the_deploy_leaves_nothing_running(monkeypatch, deployable, capsys):
    monkeypatch.setattr(test_cmd, "run_sync", lambda: False)

    assert test_cmd.run_test("hi", as_json=True) == 1
    payload = json.loads(capsys.readouterr().out)

    assert deployable["quit"] == 1
    assert payload["log_tail"] is None


def test_json_mode_prints_one_object_and_nothing_else(deployable, capsys):
    assert test_cmd.run_test("a prompt", as_json=True) == 0
    payload = json.loads(capsys.readouterr().out)

    assert payload["ok"] is True
    assert payload["query"] == "a prompt"
    assert payload["result"] == {"r": 1}
    assert payload["error"] is None
    assert [(p["name"], p["ok"]) for p in payload["phases"]] == [
        ("verify_build", True),
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


def test_a_flat_layout_project_skips_the_build_check(monkeypatch, tmp_path, deployable, capsys):
    legacy = tmp_path / "legacy" / "config"
    legacy.mkdir(parents=True)
    (legacy / "global_controller.yaml").write_text(CONFIG)
    monkeypatch.chdir(tmp_path / "legacy")

    def unexpected(*_a):
        raise AssertionError("the artifact validator should not run without a .car/")

    monkeypatch.setattr(test_cmd, "verify_build_artifact", unexpected)

    assert test_cmd.run_test("hi", as_json=True) == 0
    payload = json.loads(capsys.readouterr().out)

    assert payload["phases"][0] == {
        "name": "verify_build",
        "ok": True,
        "detail": "skipped: no .car/ artifact",
    }


# ------------------------------------------------------------------ #
#  Docker plumbing                                                    #
# ------------------------------------------------------------------ #


def test_running_containers_are_filtered_to_the_local_provider(monkeypatch):
    seen = []

    def fake_run(argv, **_):
        seen.append(argv)
        return subprocess.CompletedProcess(argv, 0, "ventis-local-echoagent-0\n", "")

    monkeypatch.setattr(verify.subprocess, "run", fake_run)

    assert verify._running_containers() == ["ventis-local-echoagent-0"]
    assert "name=ventis-local-" in seen[0]
