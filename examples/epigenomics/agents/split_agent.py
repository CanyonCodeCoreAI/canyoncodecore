# Split Agent
#
# Entry stage of the pipeline (mirrors WfCommons' Epigenomics fastq-split
# stage): splits one logical input into num_chunks equal-sized chunks for the
# downstream fan-out. There's no real sequence file here -- each chunk's
# "size" just stands in for its data volume, which is what every downstream
# stage prices its synthetic CPU work off of.
#
# Resource profile: cheap CPU, single call per request.


class SplitAgent(object):
    def __init__(self):
        self.tools = [self.split]

    def split(self, input_size: int, num_chunks: int) -> dict:
        """Split input_size bytes of data into num_chunks equal chunks."""
        chunk_size = max(1, input_size // num_chunks)
        chunks = [
            {"chunk_id": f"chunk-{i}", "size": chunk_size} for i in range(num_chunks)
        ]
        return {"chunks": chunks}


if __name__ == "__main__":
    agent = SplitAgent()
    print(agent.split(65536, 4))
