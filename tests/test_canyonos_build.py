"""`canyonos build`: where the skill comes from, how the agent is launched, and
what verdict the port it leaves behind gets."""

import pytest

from canyonos import build as build_cmd

CODELOAD = "https://codeload.github.com/CanyonCodeCoreAI/canyonos"


@pytest.mark.parametrize(
    "ref",
    ["main", "cli-v0.1.724", "some/feature-branch"],
)
def test_the_tarball_url_is_a_bare_ref(ref):
    """codeload resolves a branch, a tag or a commit from `tar.gz/<ref>`; the
    refs/heads/ form 404s for a tag, which a skill source is allowed to be."""
    assert build_cmd._tarball_url(ref) == f"{CODELOAD}/tar.gz/{ref}"


@pytest.fixture
def skill_dir(tmp_path):
    source = tmp_path / "porting-to-canyonos"
    source.mkdir()
    (source / "SKILL.md").write_text("---\nname: porting-to-canyonos\n---\n")
    return source


def test_a_local_skill_directory_is_copied_into_place(skill_dir, tmp_path):
    dest = tmp_path / "skills" / "porting-to-canyonos"

    assert build_cmd.install_skill(str(dest), str(skill_dir)) is True
    assert (dest / "SKILL.md").is_file()
    # Copied, not moved: the checkout it came from is still there.
    assert (skill_dir / "SKILL.md").is_file()


def test_a_failed_copy_is_an_install_failure_not_a_traceback(
    monkeypatch, skill_dir, tmp_path
):
    dest = tmp_path / "skills" / "porting-to-canyonos"
    dest.mkdir(parents=True)
    (dest / "SKILL.md").write_text("previous install\n")

    def denied(*_args, **_kwargs):
        raise OSError(13, "Permission denied")

    monkeypatch.setattr(build_cmd.shutil, "copytree", denied)

    def never(*_args, **_kwargs):
        raise AssertionError("an explicit local source must not fall back to a fetch")

    monkeypatch.setattr(build_cmd, "FETCH_STRATEGIES", (("git", never),))

    assert build_cmd.install_skill(str(dest), str(skill_dir)) is False
    # Nothing was torn down on the way out.
    assert (dest / "SKILL.md").read_text() == "previous install\n"


def test_the_production_ref_is_never_read_as_a_directory(monkeypatch, tmp_path):
    """A project with a `main/` directory next to it still gets the ref fetched."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / build_cmd.SKILL_REF).mkdir()
    fetched = []

    monkeypatch.setattr(
        build_cmd,
        "FETCH_STRATEGIES",
        (("git", lambda _dest, ref: fetched.append(ref) or True),),
    )
    monkeypatch.setattr(
        build_cmd.shutil,
        "copytree",
        lambda *_a, **_k: pytest.fail("the ref was read as a path"),
    )

    assert build_cmd.install_skill(str(tmp_path / "dest"), build_cmd.SKILL_REF) is True
    assert fetched == [build_cmd.SKILL_REF]


@pytest.fixture
def buildable(monkeypatch):
    """Everything run_build drives before the agent succeeds."""
    monkeypatch.setattr(build_cmd, "install_skill", lambda *_a, **_k: True)
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
