"""Explicit factories or trusted module:factory extensions; no automatic fallback."""
import hashlib
import importlib
import json

SOURCE_FACTORIES = {"f2": "douyin2text.providers.f2:F2Source"}
ASR_FACTORIES = {"whisper_cpp": "douyin2text.providers.whisper_cpp:WhisperCpp"}


def create_provider(spec, factories):
    name = spec.get("name")
    factory = factories.get(name)
    if factory is None:
        # Extension module paths are trusted administrator configuration, never API input.
        factory = spec.get("factory")
    if factory is None or not name:
        raise ValueError(f"Unknown provider: {name}")
    if isinstance(factory, str):
        module, attribute = factory.split(":", 1)
        factory = getattr(importlib.import_module(module), attribute)
    return factory(spec.get("options", {}))


def source_provider(config):
    return create_provider(config.get("providers", {}).get("source", {"name": "f2"}), SOURCE_FACTORIES)


def asr_provider(config):
    return create_provider(config.get("providers", {}).get("asr", {"name": "whisper_cpp"}), ASR_FACTORIES)


def effective_config(config):
    """Provider changes invalidate artifacts and job reuse; legacy defaults retain keys."""
    result = dict(config)
    if config.get("providers"):
        signature = json.dumps(config["providers"], sort_keys=True, separators=(",", ":"))
        for key in ("asr_fingerprint", "output_fingerprint"):
            previous = config.get(key, config["asr_fingerprint"])
            result[key] = hashlib.sha256((previous + signature).encode()).hexdigest()
    else:
        result.setdefault("output_fingerprint", config["asr_fingerprint"])
    return result
