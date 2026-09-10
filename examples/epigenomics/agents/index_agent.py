# Index Agent
#
# Final stage (mirrors Epigenomics' index-build stage): produces the
# workflow's terminal artifact from the merged, deduplicated digest.
#
# Resource profile: light CPU, single call per request.

import hashlib


class IndexAgent(object):
    def __init__(self):
        self.tools = [self.build_index]

    def build_index(self, merged_digest: str, total_size: int) -> dict:
        """Build the final index from the merged digest."""
        index_digest = self._cpu_work(merged_digest, max(1, total_size // 4))
        return {"index_digest": index_digest, "total_size": total_size}

    def _cpu_work(self, seed: str, iterations: int) -> str:
        """Deterministic CPU-bound stand-in for the stage's real processing cost."""
        digest = seed.encode()
        for _ in range(max(1, iterations)):
            digest = hashlib.sha256(digest).digest()
        return digest.hex()


if __name__ == "__main__":
    agent = IndexAgent()
    print(agent.build_index("deadbeef", 65536))
