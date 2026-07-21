# Schema Retrieval Agent
#
# First stage of the NL-to-SQL pipeline. Embeds the natural-language question
# and retrieves the most relevant tables/columns from a schema index.
#
# Resource profile: GPU-bound embedding + vector search. Embeddings are highly
# batchable, so this agent is where the scheduler can coalesce work across
# concurrent requests before hitting the GPU.


class SchemaRetrievalAgent(object):
    def __init__(self):
        self.tools = [self.embed, self.get_relevant_schema]

        # In a real deployment this is where you would load the embedding
        # model and connect to the vector store, e.g.:
        #   from sentence_transformers import SentenceTransformer
        #   self.model = SentenceTransformer("BAAI/bge-small-en-v1.5")  # GPU
        #   self.index = qdrant_client.QdrantClient(...)
        #
        # Static catalog stands in for a vector-indexed schema store.
        self._catalog = {
            "customers": ["id", "name", "region", "signup_date"],
            "orders": ["id", "customer_id", "amount", "status", "created_at"],
            "products": ["id", "name", "category", "price"],
            "order_items": ["order_id", "product_id", "quantity"],
        }

    def embed(self, text: str) -> list:
        """Embed a piece of text into a vector (GPU, batchable)."""
        # Simulated deterministic embedding. Real impl calls the GPU model.
        vec = [float((ord(c) % 13) - 6) for c in text[:16].ljust(16)]
        return vec

    def get_relevant_schema(self, question: str) -> dict:
        """Retrieve the tables/columns most relevant to a question."""
        q = question.lower()
        # Cheap keyword overlap standing in for vector similarity search.
        selected = {}
        for table, cols in self._catalog.items():
            score = sum(1 for token in (table[:-1], table) if token in q)
            score += sum(1 for c in cols if c in q)
            if score > 0:
                selected[table] = cols
        # Always include the two core tables as a fallback so generation
        # has something to work with.
        if not selected:
            selected = {k: self._catalog[k] for k in ("customers", "orders")}
        return {"tables": selected}


if __name__ == "__main__":
    agent = SchemaRetrievalAgent()
    print(agent.get_relevant_schema("total order amount per customer region"))