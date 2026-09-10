# Sort Agent
#
# Third per-chunk stage in the fan-out (mirrors Epigenomics' sort stage):
# orders one aligned chunk, returning an updated digest for the fan-in below.
#
# Resource profile: moderate CPU, high fan-out (one call per chunk).

import hashlib

COST_MULTIPLIER = 2


class SortAgent(object):
    def __init__(self):
        self.tools = [self.sort]

    def sort(self, chunk_id: str, size: int, digest: str) -> dict:
        """Sort one aligned chunk, returning its post-sort digest."""
        sorted_digest = self._cpu_work(digest, size * COST_MULTIPLIER)
        return {"chunk_id": chunk_id, "size": size, "digest": sorted_digest}

    def _cpu_work(self, seed: str, iterations: int) -> str:
        """Deterministic CPU-bound stand-in for the stage's real processing cost."""
        digest = seed.encode()
        for _ in range(max(1, iterations)):
            digest = hashlib.sha256(digest).digest()
        return digest.hex()


if __name__ == "__main__":
    agent = SortAgent()
    print(agent.sort("chunk-0", 16384, "deadbeef"))
