# Filter Agent
#
# First per-chunk stage in the fan-out (mirrors Epigenomics' filter_contams
# stage): scrubs one chunk and hands back a content digest the later stages
# build on. The "work" is a deterministic SHA-256 chain sized off the
# chunk's declared byte size, standing in for the real stage's per-byte cost.
#
# Resource profile: light CPU, high fan-out (one call per chunk).

import hashlib


class FilterAgent(object):
    def __init__(self):
        self.tools = [self.filter_contams]

    def filter_contams(self, chunk_id: str, size: int) -> dict:
        """Filter contaminants out of one chunk, returning its content digest."""
        digest = self._cpu_work(chunk_id, size)
        return {"chunk_id": chunk_id, "size": size, "digest": digest}

    def _cpu_work(self, seed: str, iterations: int) -> str:
        """Deterministic CPU-bound stand-in for the stage's real processing cost."""
        digest = seed.encode()
        for _ in range(max(1, iterations)):
            digest = hashlib.sha256(digest).digest()
        return digest.hex()


if __name__ == "__main__":
    agent = FilterAgent()
    print(agent.filter_contams("chunk-0", 16384))
