"""Ventis entrypoint for the Google ADK academic-research agent.

Nothing here restates the project. The coordinator prompt, both sub-agents,
the built-in `google_search` tool and the `gemini-2.5-pro` binding all live in
`academic_research/` and are reached with an import.

ADK owns no separate runtime the way LangGraph does -- `Runner` is a library
call -- so there is no orchestration to re-express as Python either. This file
only turns one ADK turn into one synchronous method, which is the whole of what
Ventis requires.
"""

import asyncio
import os

from google.adk.runners import InMemoryRunner
from google.genai import types

# The source tree, untouched. `academic_research/__init__.py` resolves the GCP
# project through google.auth.default() at import time.
from academic_research.agent import root_agent

APP_NAME = "academic_research"


class AcademicCoordinator(object):
    """The coordinator, exposed as the class name `agent.name` declares."""

    def __init__(self):
        # No constructor arguments -- LocalController does `AcademicCoordinator()`.
        # Anything configurable has to come from the environment.
        self._runner = InMemoryRunner(agent=root_agent, app_name=APP_NAME)
        self._user_id = os.environ.get("VENTIS_USER_ID", "ventis")

    def research(self, seminal_paper: str) -> dict:
        """Analyze a seminal paper, find recent citing work, propose directions.

        Synchronous by signature -- the executor calls this with no `await`, and
        returning a coroutine would put `<coroutine object ...>` into Redis.
        The body is free to run a loop.
        """
        return asyncio.run(self._research(seminal_paper))

    async def _research(self, seminal_paper):
        session = await self._runner.session_service.create_session(
            app_name=APP_NAME, user_id=self._user_id
        )
        message = types.Content(role="user", parts=[types.Part(text=seminal_paper)])

        # Sub-agent turns surface as events too; only the coordinator's final
        # response is the answer.
        reply = []
        async for event in self._runner.run_async(
            user_id=self._user_id,
            session_id=session.id,
            new_message=message,
        ):
            if not event.is_final_response() or event.author != root_agent.name:
                continue
            if event.content and event.content.parts:
                reply.extend(p.text for p in event.content.parts if p.text)

        # Both agents declare an `output_key`, so their results are in state.
        state = (
            await self._runner.session_service.get_session(
                app_name=APP_NAME, user_id=self._user_id, session_id=session.id
            )
        ).state

        return {
            "report": "".join(reply),
            "seminal_paper": state.get("seminal_paper", ""),
            "recent_citing_papers": state.get("recent_citing_papers", ""),
        }
