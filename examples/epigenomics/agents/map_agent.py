# Map Agent
#
# Sequence-alignment stand-in (Epigenomics' map stage) -- by far the most
# CPU-expensive stage in the real workflow, so its per-byte cost multiplier
# here is set well above the other stages to match that shape.
#
# Resource profile: heavy CPU, high fan-out (one call per chunk).

import hashlib

COST_MULTIPLIER = 8


class MapAgent(object):
    def __init__(self):
        self.tools = [self.align]

    def align(self, chunk_id: str, size: int, digest: str) -> dict:
        """Align one filtered chunk, returning its post-alignment digest."""
        aligned = self._cpu_work(digest, size * COST_MULTIPLIER)
        return {"chunk_id": chunk_id, "size": size, "digest": aligned}

    def _cpu_work(self, seed: str, iterations: int) -> str:
        """Deterministic CPU-bound stand-in for the stage's real processing cost."""
        digest = seed.encode()
        for _ in range(max(1, iterations)):
            digest = hashlib.sha256(digest).digest()
        return digest.hex()


if __name__ == "__main__":
    agent = MapAgent()
    print(agent.align("chunk-0", 16384, "deadbeef"))
