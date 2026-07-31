# Example Agent
# Replace this with your agent implementation.
# The class name must match the 'name' field in the YAML definition.


class ExampleAgent(object):
    def __init__(self):
        self.tools = [self.hello]

    def hello(self, name: str) -> str:
        """Greet someone by name."""
        return f"Hello, {name}! I'm the ExampleAgent."
