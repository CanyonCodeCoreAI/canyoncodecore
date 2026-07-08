# Simple VLLM Agent Example


class VllmAgent(object):
    def __init__(self):
        self.tools = [self.generate]

    def generate(self, prompt: str) -> str:
        """Generates a response using an LLM model based on the given prompt."""
        print(f"VllmAgent: Received prompt: '{prompt}'")

        # Simulated LLM generation
        synthetic_response = f"This is an LLM generated response to: '{prompt}'"
        return synthetic_response


if __name__ == "__main__":
    agent = VllmAgent()
    print(agent.generate("What is the stock price?"))
