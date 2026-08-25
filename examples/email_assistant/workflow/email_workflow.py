# Email assistant exposed as a REST endpoint.
#
#   curl -X POST http://localhost:8080/main \
#        -H 'Content-Type: application/json' \
#        -d '{"author": "Alice <alice@company.com>",
#             "to": "Lance <lance@company.com>",
#             "subject": "Quick question about API documentation",
#             "email_thread": "Could you clarify the permissions endpoint?"}'
#   curl http://localhost:8080/status/<request_id>
#
# One agent holds the whole graph, so this workflow is a pass-through. Splitting
# triage and response into separate agents is a scaling decision, not something
# the format requires -- and it would only pay off if one of them needed its own
# replicas.

import json

from deploy import deploy
from assistant import email_assistant


def main(**email_input):
    """Ventis splats the request body into kwargs, so the body *is* email_input."""
    future = email_assistant().invoke(input={"email_input": email_input})
    return json.loads(future.value())


deploy(main, port=8080)
