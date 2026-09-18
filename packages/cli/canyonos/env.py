"""Where every dev-only knob in this CLI is resolved.

One variable decides the mode -- `CANYONOS_ENV`, read once, here -- and no
other module reads the environment itself. Each artifact the CLI pulls from
outside the repo (the Global Controller image, the porting skill, the dashboard
images) gets a small pure resolver below, so the rules are unit-testable and
live in one place.

Every artifact variable takes the same three forms:

    unset / `prod`   the production artifact, i.e. exactly what a released CLI uses
    `local`          the artifact from this checkout
    anything else    taken literally (an image tag, a directory, a git ref)

Overrides are a development affordance: in `production` anything but
unset/`prod` raises rather than quietly shipping a dev artifact to a user.
Developers opt in through `packages/cli/.env` (see packages/cli/DEVELOPMENT.md);
shell variables win over that file, and no `.env` at all means production.
"""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

DEVELOPMENT = "development"
TEST = "test"
PRODUCTION = "production"
ENVIRONMENTS = (DEVELOPMENT, TEST, PRODUCTION)

# Keyword forms every artifact variable accepts, next to a literal value.
PROD = "prod"
LOCAL = "local"

ENV_VAR = "CANYONOS_ENV"
CORE_IMAGE_VAR = "CANYONOS_CORE_IMAGE"
SKILL_SOURCE_VAR = "CANYONOS_SKILL_SOURCE"
# Already the names `dashboard.compose.yml` interpolates.
API_IMAGE_VAR = "CANYONOS_API_IMAGE"
WEB_IMAGE_VAR = "CANYONOS_WEB_IMAGE"

# This file is packages/cli/canyonos/env.py, so `packages/cli/` is two levels up
# and the workspace root is two above that -- the same walk as from
# packages/cli/cli.py. Only `.env` loading and the `local` artifacts use these,
# both development-only: an installed CLI has no checkout above it, and
# production refuses those overrides anyway.
CLI_DIR = Path(__file__).resolve().parent.parent
REPO_ROOT = CLI_DIR.parent.parent
ENV_PATH = CLI_DIR / ".env"

# Production artifacts: what a released CLI uses, unchanged by anything here.
PROD_CORE_IMAGE = "ghcr.io/canyoncodecoreai/canyonos-core:latest"
PROD_SKILL_REF = "main"

# `local` artifacts, i.e. what this checkout builds or ships.
LOCAL_CORE_IMAGE = "canyonos-core:dev"
LOCAL_SKILL_DIR = str(REPO_ROOT / ".claude" / "skills" / "porting-to-canyonos")
LOCAL_API_IMAGE = "canyonos-api:dev"
LOCAL_WEB_IMAGE = "canyonos-web:dev"

# Only the CLI's own file, never the current directory: a project the CLI is
# pointed at has a `.env` of its own (the dashboard stack writes one), and that
# belongs to the project, not to this CLI. `override=False` keeps a shell
# variable ahead of the file.
load_dotenv(ENV_PATH, override=False)


def resolve_environment(value):
    """The environment `value` selects. Unset or empty means production."""
    name = (value or "").strip()
    if not name:
        return PRODUCTION
    if name not in ENVIRONMENTS:
        raise RuntimeError(
            f"{ENV_VAR}={name} is not a CanyonOS environment. "
            f"Use one of {', '.join(ENVIRONMENTS)}, or leave it unset for {PRODUCTION}."
        )
    return name


environment = resolve_environment(os.environ.get(ENV_VAR))
is_development = environment == DEVELOPMENT
is_test = environment == TEST
is_production = environment == PRODUCTION


def _resolve_artifact(environment, value, variable, prod_value, local_value):
    """Shared keyword handling for one artifact variable. Pure, no I/O."""
    setting = (value or "").strip()
    if not setting or setting == PROD:
        return prod_value
    if environment == PRODUCTION:
        raise RuntimeError(
            f"{variable}={setting} is a development override and must not be set in "
            f"{PRODUCTION}. Unset it (or set it to `{PROD}`), or run with "
            f"{ENV_VAR}={DEVELOPMENT}."
        )
    if setting == LOCAL:
        return local_value
    return setting


def resolve_core_image(environment, value):
    """Global Controller image: `local` is the one built from this checkout."""
    return _resolve_artifact(
        environment, value, CORE_IMAGE_VAR, PROD_CORE_IMAGE, LOCAL_CORE_IMAGE
    )


def resolve_skill_source(environment, value):
    """Porting skill: a directory to copy, or a git ref to fetch."""
    return _resolve_artifact(
        environment, value, SKILL_SOURCE_VAR, PROD_SKILL_REF, LOCAL_SKILL_DIR
    )


def resolve_dashboard_image(environment, value, prod_image, local_image, variable):
    """One dashboard image. The production tag is passed in, since it is versioned
    alongside the stack it belongs to rather than here."""
    return _resolve_artifact(environment, value, variable, prod_image, local_image)


core_image = resolve_core_image(environment, os.environ.get(CORE_IMAGE_VAR))
skill_source = resolve_skill_source(environment, os.environ.get(SKILL_SOURCE_VAR))


def api_image(prod_image):
    """Dashboard API image for this environment."""
    return resolve_dashboard_image(
        environment,
        os.environ.get(API_IMAGE_VAR),
        prod_image,
        LOCAL_API_IMAGE,
        API_IMAGE_VAR,
    )


def web_image(prod_image):
    """Dashboard web image for this environment."""
    return resolve_dashboard_image(
        environment,
        os.environ.get(WEB_IMAGE_VAR),
        prod_image,
        LOCAL_WEB_IMAGE,
        WEB_IMAGE_VAR,
    )
