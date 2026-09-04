# Dedup Agent
#
# Fan-in barrier (mirrors Epigenomics' mark-duplicates/merge stage): needs
# every sorted chunk before it can run. Combines all chunk digests into one
# merged digest, with cost scaling off the total merged data volume.
#
# Resource profile: moderate CPU, single call per request (the barrier).

import hashlib


class DedupAgent(object):
    def __init__(self):
        self.tools = [self.merge_dedup]

    def merge_dedup(self, chunks: list) -> dict:
        """Merge and deduplicate every sorted chunk into one combined digest."""
        total_size = sum(c["size"] for c in chunks)
        seed = "".join(c["digest"] for c in sorted(chunks, key=lambda c: c["chunk_id"]))
        merged_digest = self._cpu_work(seed, total_size)
        return {
            "merged_digest": merged_digest,
            "total_size": total_size,
            "n_chunks": len(chunks),
        }

    def _cpu_work(self, seed: str, iterations: int) -> str:
        """Deterministic CPU-bound stand-in for the stage's real processing cost."""
        digest = seed.encode()
        for _ in range(max(1, iterations)):
            digest = hashlib.sha256(digest).digest()
        return digest.hex()


if __name__ == "__main__":
    agent = DedupAgent()
    print(
        agent.merge_dedup(
            [
                {"chunk_id": "chunk-0", "size": 16384, "digest": "aa"},
                {"chunk_id": "chunk-1", "size": 16384, "digest": "bb"},
            ]
        )
    )
