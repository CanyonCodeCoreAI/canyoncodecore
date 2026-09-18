import pytest

from canyonos import build as build_cmd


@pytest.fixture
def buildable(monkeypatch):
    """Everything run_build drives before the agent succeeds."""
    monkeypatch.setattr(build_cmd, "install_skill", lambda _dest: True)
    monkeypatch.setattr(build_cmd, "report_port", lambda _skill_dir: True)


@pytest.fixture
def agent_on_path(monkeypatch):
    """The agent CLI resolves; record the argv it would have been run with."""
    monkeypatch.setattr(build_cmd.shutil, "which", lambda _cli: f"/usr/bin/{_cli}")
    calls = []

    class Completed:
        returncode = 0

    def run(argv, **_kwargs):
        calls.append(argv)
        return Completed()

    monkeypatch.setattr(build_cmd.subprocess, "run", run)
    return calls


def _set_tty(monkeypatch, attached):
    monkeypatch.setattr(build_cmd.sys.stdin, "isatty", lambda: attached)
    monkeypatch.setattr(build_cmd.sys.stdout, "isatty", lambda: attached)


def test_an_attended_launch_passes_no_unattended_flags(monkeypatch, agent_on_path):
    _set_tty(monkeypatch, True)

    assert build_cmd.launch_agent("claude", "port it", unattended=False) == 0
    assert agent_on_path[0] == ["claude", "port it"]


def test_an_unattended_launch_keeps_its_flags_on_a_tty(monkeypatch, agent_on_path):
    _set_tty(monkeypatch, True)

    build_cmd.launch_agent("claude", "port it", unattended=True)

    argv = agent_on_path[0]
    assert argv[1:-1] == build_cmd.AGENTS["claude"]["unattended"]
    assert argv[-1].endswith(build_cmd.UNATTENDED_NOTE)


def test_a_launch_without_a_tty_is_unattended(monkeypatch, agent_on_path):
    _set_tty(monkeypatch, False)

    build_cmd.launch_agent("claude", "port it", unattended=False)

    argv = agent_on_path[0]
    assert argv[1:-1] == build_cmd.AGENTS["claude"]["unattended"]
    assert argv[-1].endswith(build_cmd.UNATTENDED_NOTE)


def test_a_missing_agent_cli_reports_no_status(monkeypatch):
    monkeypatch.setattr(build_cmd.shutil, "which", lambda _cli: None)

    assert build_cmd.launch_agent("claude", "port it", unattended=True) is None


def test_yes_runs_the_agent_unattended(monkeypatch, buildable, agent_on_path):
    _set_tty(monkeypatch, True)

    assert build_cmd.run_build(yes=True) is True
    assert agent_on_path[0][1:-1] == build_cmd.AGENTS["claude"]["unattended"]


def test_a_failed_agent_never_consults_an_earlier_port(monkeypatch, buildable):
    monkeypatch.setattr(build_cmd, "launch_agent", lambda *_a, **_k: 1)
    monkeypatch.setattr(
        build_cmd,
        "report_port",
        lambda _skill_dir: pytest.fail("a dead session is no evidence about .car"),
    )

    assert build_cmd.run_build(yes=True) is False


def test_a_missing_agent_cli_fails_the_build(monkeypatch, buildable):
    monkeypatch.setattr(build_cmd, "launch_agent", lambda *_a, **_k: None)

    assert build_cmd.run_build(yes=True) is False


def test_a_port_with_no_car_fails(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    skill = tmp_path / "skill"
    skill.mkdir()
    (skill / build_cmd.VALIDATOR).write_text("")

    assert build_cmd.report_port(str(skill)) is False


def test_a_port_with_no_validator_fails(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    (tmp_path / build_cmd.CAR_DIR).mkdir()

    assert build_cmd.report_port(str(tmp_path / "skill")) is False


@pytest.mark.parametrize(("status", "passed"), [(0, True), (1, False)])
def test_a_port_takes_its_verdict_from_the_validator(
    monkeypatch, tmp_path, status, passed
):
    monkeypatch.chdir(tmp_path)
    (tmp_path / build_cmd.CAR_DIR).mkdir()
    skill = tmp_path / "skill"
    skill.mkdir()
    (skill / build_cmd.VALIDATOR).write_text(f"raise SystemExit({status})")

    assert build_cmd.report_port(str(skill)) is passed
