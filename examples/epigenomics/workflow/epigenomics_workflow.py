# Epigenomics Workflow
#
# WfCommons-style synthetic Epigenomics DAG for local, LLM-free testing:
#   0. SplitAgent  - split input_size bytes into num_chunks chunks (single call)
#   1. FilterAgent - per-chunk contaminant filter                  (fan-out)
#   2. MapAgent    - per-chunk alignment, the heaviest stage       (fan-out)
#   3. SortAgent   - per-chunk sort                                (fan-out)
#   4. DedupAgent  - merge + dedup every sorted chunk              (fan-in barrier)
#   5. IndexAgent  - build the final index from the merged digest  (single call)
#
# Every stage does deterministic SHA-256 work sized off chunk byte counts --
# no LLM calls, no external services -- so results are reproducible and the
# fan-out width scales with num_chunks.
#
# After running `ventis build` and `ventis deploy`:
#   curl -X POST http://localhost:8080/main -H 'Content-Type: application/json' \
#        -d '{"input_size": 65536, "num_chunks": 4}'
#   curl http://localhost:8080/status/<request_id>

import sys
import os

# These path inserts are needed when running inside a Docker container
# where all files are copied flat into /app/.
sys.path.insert(0, os.path.dirname(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "stubs"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "grpc_stubs"))

import json

from deploy import deploy
from agents.split_agent import SplitAgent
from agents.filter_agent import FilterAgent
from agents.map_agent import MapAgent
from agents.sort_agent import SortAgent
from agents.dedup_agent import DedupAgent
from agents.index_agent import IndexAgent


def main(input_size: int = 65536, num_chunks: int = 4):
    split_agent = SplitAgent()
    filter_agent = FilterAgent()
    map_agent = MapAgent()
    sort_agent = SortAgent()
    dedup_agent = DedupAgent()
    index_agent = IndexAgent()

    # Stage 0: single call, produces the chunk list the fan-out below runs over.
    split = json.loads(
        split_agent.split(input_size=input_size, num_chunks=num_chunks).value()
    )
    chunks = split["chunks"]

    # Stage 1: fan out one filter call per chunk -- every call returns a Future
    # immediately, so all chunks are dispatched before we block on any of them.
    filter_futures = {
        c["chunk_id"]: filter_agent.filter_contams(chunk_id=c["chunk_id"], size=c["size"])
        for c in chunks
    }
    filtered = {cid: json.loads(f.value()) for cid, f in filter_futures.items()}

    # Stage 2: fan out alignment -- the heaviest stage -- one call per chunk.
    map_futures = {
        cid: map_agent.align(chunk_id=cid, size=r["size"], digest=r["digest"])
        for cid, r in filtered.items()
    }
    mapped = {cid: json.loads(f.value()) for cid, f in map_futures.items()}

    # Stage 3: fan out sort, one call per chunk.
    sort_futures = {
        cid: sort_agent.sort(chunk_id=cid, size=r["size"], digest=r["digest"])
        for cid, r in mapped.items()
    }
    sorted_chunks = {cid: json.loads(f.value()) for cid, f in sort_futures.items()}

    # Stage 4: fan-in barrier -- dedup needs every sorted chunk before it can run.
    merged = json.loads(
        dedup_agent.merge_dedup(chunks=list(sorted_chunks.values())).value()
    )

    # Stage 5: build the final index from the merged digest.
    index = json.loads(
        index_agent.build_index(
            merged_digest=merged["merged_digest"], total_size=merged["total_size"]
        ).value()
    )

    return {
        "input_size": input_size,
        "num_chunks": num_chunks,
        "merged_digest": merged["merged_digest"],
        "n_chunks": merged["n_chunks"],
        "index_digest": index["index_digest"],
    }


deploy(main, port=8080)
