class SmokeAgent(object):
    def __init__(self):
        self.tools = [self.ping]

    def ping(self, name: str) -> str:
        return f"smoke:{name}"
