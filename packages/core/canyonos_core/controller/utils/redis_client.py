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

    def expire(self, key, seconds):
        """Set a TTL (in seconds) on a key. No-op if the key does not exist."""
        return self.client.expire(key, seconds)

    def incrby(self, key, amount=1):
        """Atomically add amount to a key's integer value. Returns the new value."""
        return self.client.incrby(key, amount)

    # --- List operations ---

    def lpush(self, key, *values):
        """Push one or more values onto the head of a list."""
        self.client.lpush(key, *values)

    def rpop(self, key):
        """Pop one value off the tail of a list. Returns None if the list is empty."""
        value = self.client.rpop(key)
        if isinstance(value, (bytes, str)):
            return self._decode(value)
        return None

    def brpop(self, key, timeout=0):
        """Pop off the tail of a list, blocking up to timeout seconds. None on timeout."""
        result = self.client.brpop(key, timeout=timeout)
        if result is not None:
            return self._decode(result[1])
        return None

    # --- Hash operations ---

    def hset(self, name, field, value):
        """Set a single field in a hash."""
        self.client.hset(name, field, value)

    def hset_multiple(self, name, mapping):
        """Set multiple fields in a hash at once."""
        self.client.hset(name, mapping=mapping)

    def hincrby(self, name, field, amount=1):
        """Atomically increment a hash field by the given amount."""
        return self.client.hincrby(name, field, amount)

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
