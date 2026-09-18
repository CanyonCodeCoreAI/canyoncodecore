"""The CLI's environment pattern: one variable, read once, plus a pure resolver
per dev-only knob."""

import importlib
import os
import subprocess
import urllib.request

import dotenv
import pytest

from canyonos import env as env_module

DEV = env_module.DEVELOPMENT
TEST = env_module.TEST
PROD = env_module.PRODUCTION
OVERRIDABLE = (DEV, TEST)

API_PROD_IMAGE = "ghcr.io/canyoncodecoreai/canyonos-api:v9.9.9"


@pytest.fixture
def reloaded(monkeypatch):
    """Re-import env.py under chosen variables, then put the real one back.

    `.env` loading is stubbed out for the duration: what a contributor happens
    to have in their own cli/.env must not decide what these tests see.
    """
    saved = dict(os.environ)
    real_load_dotenv = dotenv.load_dotenv
    loads = []

    def record_load(path, override=None, **_kwargs):
        loads.append((path, override))
        return False

    monkeypatch.setattr(dotenv, "load_dotenv", record_load)

    def reload_with(**variables):
        for key in (
            env_module.ENV_VAR,
            env_module.CORE_IMAGE_VAR,
            env_module.SKILL_SOURCE_VAR,
            env_module.API_IMAGE_VAR,
            env_module.WEB_IMAGE_VAR,
        ):
            os.environ.pop(key, None)
        os.environ.update(variables)
        return importlib.reload(env_module)

    reload_with.loads = loads
    yield reload_with

    os.environ.clear()
    os.environ.update(saved)
    monkeypatch.setattr(dotenv, "load_dotenv", real_load_dotenv)
    importlib.reload(env_module)


# ------------------------------------------------------------------ #
#  The environment itself                                             #
# ------------------------------------------------------------------ #


@pytest.mark.parametrize("value", [None, "", "   "])
def test_an_unset_environment_is_production(value):
    assert env_module.resolve_environment(value) == PROD


@pytest.mark.parametrize("name", [DEV, TEST, PROD])
def test_every_accepted_environment_resolves_to_itself(name):
    assert env_module.resolve_environment(name) == name
    assert env_module.resolve_environment(f"  {name}  ") == name


@pytest.mark.parametrize("value", ["staging", "dev", "Development", "prod", "true"])
def test_an_unknown_environment_is_refused(value):
    with pytest.raises(RuntimeError, match="not a CanyonOS environment"):
        env_module.resolve_environment(value)


@pytest.mark.parametrize("name", [DEV, TEST, PROD])
def test_the_environment_flags_follow_the_variable(reloaded, name):
    env = reloaded(CANYONOS_ENV=name)

    assert env.environment == name
    assert (env.is_development, env.is_test, env.is_production) == (
        name == DEV,
        name == TEST,
        name == PROD,
    )


def test_an_unknown_environment_fails_at_import(reloaded):
    with pytest.raises(RuntimeError, match="not a CanyonOS environment"):
        reloaded(CANYONOS_ENV="staging")


def test_the_env_file_is_the_cli_one_not_the_working_directory(
    reloaded, monkeypatch, tmp_path
):
    monkeypatch.chdir(tmp_path)

    env = reloaded(CANYONOS_ENV=TEST)

    # cli/.env, i.e. the file next to cli/cli.py.
    assert env.ENV_PATH == env.CLI_DIR / ".env"
    assert (env.CLI_DIR / "cli.py").is_file()
    # A shell variable has to win over the file.
    assert reloaded.loads == [(env.ENV_PATH, False)]


def test_importing_env_touches_neither_docker_nor_the_network(reloaded, monkeypatch):
    def forbidden(*_args, **_kwargs):
        raise AssertionError("importing env.py must not shell out or open a connection")

    monkeypatch.setattr(subprocess, "run", forbidden)
    monkeypatch.setattr(urllib.request, "urlopen", forbidden)

    assert reloaded(CANYONOS_ENV=TEST).is_test


# ------------------------------------------------------------------ #
#  Global Controller image                                            #
# ------------------------------------------------------------------ #


@pytest.mark.parametrize("environment", [DEV, TEST, PROD])
@pytest.mark.parametrize("value", [None, "", "prod"])
def test_an_unset_core_image_is_the_production_image(environment, value):
    assert (
        env_module.resolve_core_image(environment, value) == env_module.PROD_CORE_IMAGE
    )


@pytest.mark.parametrize("environment", OVERRIDABLE)
def test_a_local_core_image_is_the_one_built_from_the_checkout(environment):
    assert (
        env_module.resolve_core_image(environment, "local")
        == env_module.LOCAL_CORE_IMAGE
        == "canyonos-core:dev"
    )


@pytest.mark.parametrize("environment", OVERRIDABLE)
def test_any_other_core_image_is_taken_literally(environment):
    assert env_module.resolve_core_image(environment, "canyonos-core:wip") == (
        "canyonos-core:wip"
    )


@pytest.mark.parametrize("value", ["local", "canyonos-core:wip"])
def test_a_core_image_override_is_refused_in_production(value):
    with pytest.raises(RuntimeError, match=env_module.CORE_IMAGE_VAR):
        env_module.resolve_core_image(PROD, value)


def test_the_resolved_core_image_comes_from_the_variable(reloaded):
    assert reloaded(CANYONOS_ENV=DEV).core_image == env_module.PROD_CORE_IMAGE
    assert (
        reloaded(CANYONOS_ENV=DEV, CANYONOS_CORE_IMAGE="local").core_image
        == env_module.LOCAL_CORE_IMAGE
    )


# ------------------------------------------------------------------ #
#  Porting skill                                                      #
# ------------------------------------------------------------------ #


@pytest.mark.parametrize("environment", [DEV, TEST, PROD])
@pytest.mark.parametrize("value", [None, "", "prod"])
def test_an_unset_skill_source_is_the_production_ref(environment, value):
    assert (
        env_module.resolve_skill_source(environment, value)
        == env_module.PROD_SKILL_REF
        == "main"
    )


@pytest.mark.parametrize("environment", OVERRIDABLE)
def test_a_local_skill_source_is_the_directory_in_the_checkout(environment):
    resolved = env_module.resolve_skill_source(environment, "local")

    assert resolved == env_module.LOCAL_SKILL_DIR
    assert resolved == str(
        env_module.REPO_ROOT / ".claude" / "skills" / "porting-to-canyonos"
    )
    assert os.path.isdir(resolved)


@pytest.mark.parametrize("value", ["some-branch", "/tmp/porting-to-canyonos"])
def test_any_other_skill_source_is_taken_literally(value):
    assert env_module.resolve_skill_source(DEV, value) == value


@pytest.mark.parametrize("value", ["local", "some-branch"])
def test_a_skill_source_override_is_refused_in_production(value):
    with pytest.raises(RuntimeError, match=env_module.SKILL_SOURCE_VAR):
        env_module.resolve_skill_source(PROD, value)


def test_the_resolved_skill_source_comes_from_the_variable(reloaded):
    assert reloaded(CANYONOS_ENV=DEV).skill_source == env_module.PROD_SKILL_REF
    assert (
        reloaded(CANYONOS_ENV=DEV, CANYONOS_SKILL_SOURCE="local").skill_source
        == env_module.LOCAL_SKILL_DIR
    )


# ------------------------------------------------------------------ #
#  Dashboard images                                                   #
# ------------------------------------------------------------------ #


def resolve_api_image(environment, value):
    return env_module.resolve_dashboard_image(
        environment,
        value,
        API_PROD_IMAGE,
        env_module.LOCAL_API_IMAGE,
        env_module.API_IMAGE_VAR,
    )


@pytest.mark.parametrize("environment", [DEV, TEST, PROD])
@pytest.mark.parametrize("value", [None, "", "prod"])
def test_an_unset_dashboard_image_is_the_stacks_published_image(environment, value):
    assert resolve_api_image(environment, value) == API_PROD_IMAGE


@pytest.mark.parametrize("environment", OVERRIDABLE)
def test_a_local_dashboard_image_is_the_locally_built_one(environment):
    assert resolve_api_image(environment, "local") == env_module.LOCAL_API_IMAGE


@pytest.mark.parametrize("environment", OVERRIDABLE)
def test_any_other_dashboard_image_is_taken_literally(environment):
    assert resolve_api_image(environment, "canyonos-api:pr-1") == "canyonos-api:pr-1"


@pytest.mark.parametrize("value", ["local", "canyonos-api:pr-1"])
def test_a_dashboard_image_override_is_refused_in_production(value):
    with pytest.raises(RuntimeError, match=env_module.API_IMAGE_VAR):
        resolve_api_image(PROD, value)


def test_the_dashboard_images_come_from_their_own_variables(reloaded):
    env = reloaded(CANYONOS_ENV=DEV, CANYONOS_WEB_IMAGE="local")

    assert env.api_image(API_PROD_IMAGE) == API_PROD_IMAGE
    assert env.web_image("ghcr.io/canyoncodecoreai/canyonos-web:v9.9.9") == (
        env.LOCAL_WEB_IMAGE
    )
