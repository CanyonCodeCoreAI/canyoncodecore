import os

try:
    from ventis.utils.redis_client import RedisClient
    import ventis.ventis_context as ventis_context
except ImportError:
    from redis_client import RedisClient
    import ventis_context

_redis = RedisClient(
    host=os.environ.get("VENTIS_REDIS_HOST", "localhost"),
    port=int(os.environ.get("VENTIS_REDIS_PORT", 6379)),
)


def call_bedrock(model_id: str, messages: list, inference_config: dict, region: str = "us-east-1") -> dict:
    """Call Bedrock's converse() API and log token/error telemetry onto the
    currently executing future's hash (future:<future_id>)."""
    import boto3

    client = boto3.client("bedrock-runtime", region_name=region)
    future_id = ventis_context.get_current_future_id()
    error_count = 0
    response = None
    try:
        response = client.converse(
            modelId=model_id, messages=messages, inferenceConfig=inference_config
        )
        return response
    except Exception as e:
        # Counts failed attempts for this call. With no retry logic today this is
        # always 0 or 1, matching `failed`; once retries are added, a call that
        # eventually succeeds can still report error_count > 0 while failed stays 0.
        error_count += 1
        metrics_key = ventis_context.get_current_metrics_key()
        if metrics_key:
            _redis.hincrby(metrics_key, "error_count", 1)
        if future_id:
            _redis.hset_multiple(f"future:{future_id}", {
                "failed": 1,
                "error_message": str(e),
            })
        raise
    finally:
        if future_id:
            usage = (response or {}).get("usage", {})
            _redis.hset_multiple(f"future:{future_id}", {
                "model": model_id,
                "input_token_count": str(usage.get("inputTokens", "")),
                "output_token_count": str(usage.get("outputTokens", "")),
                "token_count": str(usage.get("totalTokens", "")),
                "errors": str(error_count),
                "input_cache_tokens": str(usage.get("cacheReadInputTokens", "")),
                "input_cache_write_tokens": str(usage.get("cacheWriteInputTokens", "")),
            })
