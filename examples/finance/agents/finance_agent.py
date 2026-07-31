from vllm_agent import VllmAgent


# Example of a simple finance agent
class FinanceAgent(object):
    def __init__(self):
        self.tools = [self.get_stock_price, self.get_company_name]
        self.vllm = VllmAgent()

    def get_stock_price(self, ticker: str) -> float:
        """Get the stock price for a given ticker."""
        return 100.0

    def get_company_name(self, ticker: str) -> str:
        """Get the company name for a given ticker."""
        print("get_company_name")
        val = self.run(ticker)
        print(val)
        return val

    def run(self, query: str) -> str:
        # company = self.get_company_name(query)
        # price = self.get_stock_price(company)

        prompt = f"The company is {query} and the stock price is . Please write a short, professional response."

        # Call the VLLM agent remotely and wait for the result
        # .value() blocks until the future completes via Redis
        response = self.vllm.generate(prompt).value()
        # print(response.value())
        return response


if __name__ == "__main__":
    agent = FinanceAgent()
    print(agent.run("What is the stock price of Apple?"))
