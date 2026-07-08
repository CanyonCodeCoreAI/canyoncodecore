class ReverseAgent(object):
    def __init__(self):
        self.tools = [self.reverse]

    def reverse(self, text: str) -> str:
        return text[::-1]
