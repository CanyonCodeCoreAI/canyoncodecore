"""Ventis workflow for the academic-research agent.

One agent, one call. The REST route is this function's __name__, so POSTing
{"seminal_paper": "..."} to /research on the workflow's api_port reaches it.
"""

import json

from deploy import deploy
from coordinator import AcademicCoordinator


def research(seminal_paper):
    """Route: POST /research  {"seminal_paper": "<title, DOI or URL>"}"""
    coordinator = AcademicCoordinator()

    # .value() blocks; with a single call there is nothing to overlap. Were a
    # second agent added, dispatch both before resolving either -- fusing the
    # calls into one comprehension makes the fan-out silently serial.
    future = coordinator.research(seminal_paper=seminal_paper)

    # returns.type is dict, and .value() always hands back a string.
    return json.loads(future.value())


# This file is exec'd, not imported, so __name__ == "__main__" here and any
# `if __name__ == "__main__":` block would run in production. deploy() blocks
# on app.run(); nothing after it executes.
deploy(research, port=8080)
