r"""Ventis workflow for the email assistant.

The REST route is this function's __name__:

  curl -X POST http://localhost:8080/main \
       -H 'Content-Type: application/json' \
       -d '{"author": "Alice <alice@company.com>",
            "to": "Lance <lance@company.com>",
            "subject": "Quick question about API documentation",
            "email_thread": "Could you clarify the permissions endpoint?"}'
  curl http://localhost:8080/status/<request_id>

One agent holds the whole graph, so this workflow is a pass-through. Splitting
triage and response into separate agents is a scaling decision, not something
the format requires -- and it would only pay off if one of them needed its own
replicas.
"""

import json

from deploy import deploy
from assistant import EmailAssistant


def main(**email_input):
    """Ventis splats the request body into kwargs, so the body *is* email_input."""
    assistant = EmailAssistant()

    # .value() blocks; with a single call there is nothing to overlap. Were a
    # second agent added, dispatch both before resolving either -- fusing the
    # calls into one comprehension makes the fan-out silently serial.
    future = assistant.run(email_input=email_input)

    # returns.type is dict, and .value() always hands back a string.
    return json.loads(future.value())


# This file is exec'd, not imported, so __name__ == "__main__" here and any
# `if __name__ == "__main__":` block would run in production. deploy() blocks
# on app.run(); nothing after it executes.
deploy(main, port=8080)
