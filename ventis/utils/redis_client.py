import redis


class RedisClient(object):
    """Redis utility for connecting to localhost with support for strings, hashes, and sets."""

    def __init__(self, host="localhost", port=6379, db=0):
        self.client = redis.Redis(host=host, port=port, db=db)

    # --- String operations ---

    def set(self, key, value):
        """Set a key-value pair in Redis."""
        self.client.set(key, value)

    def get(self, key):
        """Get a value by key from Redis. Returns None if key does not exist."""
        value = self.client.get(key)
        if value is not None:
            return self._decode(value)
        return None

    def delete(self, *keys):
        """Delete one or more keys from Redis."""
        self.client.delete(*keys)

    def setnx(self, key, value):
        """Set key to value only if it does not already exist. Returns True if set, False otherwise."""
        return self.client.setnx(key, value)

    # --- Hash operations ---

    def hset(self, name, field, value):
        """Set a single field in a hash."""
        self.client.hset(name, field, value)

    def hset_multiple(self, name, mapping):
        """Set multiple fields in a hash at once."""
        self.client.hset(name, mapping=mapping)

    def hget(self, name, field):
        """Get a single field from a hash. Returns None if field does not exist."""
        value = self.client.hget(name, field)
        if value is not None:
            return self._decode(value)
        return None

    def hgetall(self, name):
        """Get all fields and values from a hash."""
        data = self.client.hgetall(name)
        return {self._decode(k): self._decode(v) for k, v in data.items()}

    # --- Set operations ---

    def sadd(self, name, *values):
        """Add one or more members to a set."""
        self.client.sadd(name, *values)

    def srem(self, name, *values):
        """Remove one or more members from a set."""
        self.client.srem(name, *values)

    def smembers(self, name):
        """Get all members of a set."""
        return {self._decode(v) for v in self.client.smembers(name)}

    # --- Scan operations ---

    def scan_keys(self, pattern):
        """Scan for keys matching a glob pattern. Returns a list of matching key strings."""
        keys = []
        cursor = 0
        while True:
            cursor, batch = self.client.scan(cursor, match=pattern, count=100)
            keys.extend(self._decode(k) for k in batch)
            if cursor == 0:
                break
        return keys

    # --- Helper ---

    def _decode(self, value: bytes | str) -> str:
        if isinstance(value, bytes):
            return value.decode("utf-8")
        return value
