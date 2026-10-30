"""CC Switch model metadata matching, adapted for the Provider Hub.

Adapted from CC Switch v4.0.21 src/lib/modelMetadata.ts and src/lib/modelsDev.ts,
copyright (c) 2025 Jason Young, MIT. See third_party/cc-switch/LICENSE.
"""
from __future__ import annotations

import math
import re
from collections.abc import Mapping
from dataclasses import dataclass
from urllib.parse import urlsplit


ALLOWED_REASONING_LEVELS = frozenset(
    {"none", "low", "medium", "high", "xhigh", "max", "ultra"}
)
SOURCE_PRESET = "preset"
SOURCE_MODELS_DEV_PROVIDER = "models.dev:same-provider"
SOURCE_MODELS_DEV_VENDOR = "models.dev:vendor-reference"

_FIELDS = ("context_window", "reasoning_levels", "supports_image")
_VERSION_SEGMENT = re.compile(r"^v\d+(?:(?:alpha|beta)\d*)?$")
_REQUEST_SEGMENTS = frozenset({"responses", "messages"})
_SCHEME = re.compile(r"^[a-z][a-z0-9+.-]*://", re.IGNORECASE)


@dataclass(frozen=True)
class _Provider:
    provider_id: str
    models_by_key: dict[str, Mapping[str, object]]
    models_by_lower: dict[str, Mapping[str, object]]
    models_by_normalized: dict[str, Mapping[str, object]]


@dataclass(frozen=True)
class MetadataIndex:
    providers_by_endpoint: dict[str, tuple[_Provider, ...]]
    providers_by_id: dict[str, _Provider]
    vendor_models_by_normalized: dict[str, Mapping[str, object]]


def normalize_model_id(model_id: str) -> str:
    """Normalize a model ID the same way CC Switch does."""
    after_slash = model_id[model_id.rfind("/") + 1 :]
    before_colon = after_slash.split(":")[0]
    normalized = before_colon.strip().replace("@", "-").lower()
    if normalized.endswith("[1m]"):
        normalized = normalized[: -len("[1m]")].strip()
    return normalized


def _endpoint_key(url: object) -> str | None:
    if not isinstance(url, str):
        return None
    trimmed = url.strip()
    if not trimmed:
        return None
    try:
        parsed = urlsplit(trimmed if _SCHEME.match(trimmed) else f"https://{trimmed}")
        hostname = parsed.hostname
        port = parsed.port
    except (TypeError, ValueError):
        return None
    if not hostname:
        return None
    segments = [segment.lower() for segment in parsed.path.split("/") if segment]
    while segments:
        last = segments[-1]
        if last == "completions" and len(segments) >= 2 and segments[-2] == "chat":
            del segments[-2:]
        elif last in _REQUEST_SEGMENTS or _VERSION_SEGMENT.fullmatch(last):
            segments.pop()
        else:
            break
    host = hostname.lower()
    if port is not None and not ((parsed.scheme == "https" and port == 443) or (parsed.scheme == "http" and port == 80)):
        host += f":{port}"
    key = "/".join([host, *segments])
    return key


def _string_list(value: object) -> list[str] | None:
    if not isinstance(value, list):
        return None
    result = []
    seen = set()
    for item in value:
        if not isinstance(item, str):
            continue
        normalized = item.strip().lower()
        if normalized and normalized not in seen:
            seen.add(normalized)
            result.append(normalized)
    return result or None


def _positive_integer(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        if value <= 0:
            return None
        try:
            parsed = float(value)
        except OverflowError:
            return None
        return value if math.isfinite(parsed) else None
    if isinstance(value, float):
        parsed = value
    elif isinstance(value, str):
        if not value.strip():
            return None
        try:
            parsed = float(value)
        except ValueError:
            return None
    else:
        return None
    if not math.isfinite(parsed) or parsed <= 0:
        return None
    return math.trunc(parsed)


def _reasoning_levels(value: object, model_id: str) -> list[str] | None:
    levels = _string_list(value)
    if levels is None:
        return None
    filtered = [level for level in levels if level in ALLOWED_REASONING_LEVELS]
    return filtered or None


def _effort_levels(model: Mapping[str, object], model_id: str) -> list[str] | None:
    options = model.get("reasoning_options")
    if not isinstance(options, list):
        return None
    values = []
    for option in options:
        if not isinstance(option, Mapping) or option.get("type") != "effort":
            continue
        option_values = option.get("values")
        if isinstance(option_values, list):
            values.extend(option_values)
    return _reasoning_levels(values, model_id)


def _supports_image(model: Mapping[str, object]) -> bool | None:
    modalities = model.get("modalities")
    if not isinstance(modalities, Mapping):
        return None
    inputs = _string_list(modalities.get("input"))
    return "image" in inputs if inputs is not None else None


def _metadata_from_models_dev(
    model: Mapping[str, object], model_id: str
) -> dict[str, object | None]:
    limit = model.get("limit")
    context_window = (
        _positive_integer(limit.get("context"))
        if isinstance(limit, Mapping)
        else None
    )
    return {
        "context_window": context_window,
        "reasoning_levels": _effort_levels(model, model_id),
        "supports_image": _supports_image(model),
    }


def _metadata_from_preset(
    model: Mapping[str, object], model_id: str
) -> dict[str, object | None]:
    context = model.get("context_window", model.get("contextWindow"))
    reasoning = model.get(
        "reasoning_levels", model.get("reasoningEfforts")
    )
    image = model.get("supports_image")
    if not isinstance(image, bool):
        image = None
    return {
        "context_window": _positive_integer(context),
        "reasoning_levels": _reasoning_levels(reasoning, model_id),
        "supports_image": image,
    }


def _lookup_model(
    models_by_key: Mapping[str, Mapping[str, object]],
    models_by_lower: Mapping[str, Mapping[str, object]],
    models_by_normalized: Mapping[str, Mapping[str, object]],
    model_id: str,
) -> Mapping[str, object] | None:
    exact = models_by_key.get(model_id)
    if exact is not None:
        return exact
    lower = models_by_lower.get(model_id.lower())
    if lower is not None:
        return lower
    return models_by_normalized.get(normalize_model_id(model_id))


def _unique_vendor_model(
    normalized_id: str,
    entries: list[tuple[str, Mapping[str, object]]],
    provider_models: Mapping[str, Mapping[str, Mapping[str, object]]],
    vendors: frozenset[str],
) -> Mapping[str, object] | None:
    pointed: dict[str, Mapping[str, object]] = {}
    owned: dict[str, Mapping[str, object]] = {}
    for provider_id, model in entries:
        canonical = model.get("canonical_model_id")
        if isinstance(canonical, str) and "/" in canonical:
            vendor_id, _, model_key = canonical.partition("/")
            vendor_models = provider_models.get(vendor_id)
            vendor_model = (
                vendor_models.get(model_key) if vendor_models is not None else None
            )
            if vendor_model is not None:
                pointed[canonical] = vendor_model
                continue
            if vendor_id != provider_id:
                continue
        if provider_id in vendors:
            model_id = model.get("id")
            owned[
                f"{provider_id}/{model_id if isinstance(model_id, str) else normalized_id}"
            ] = model
    found = pointed or owned
    if len(found) != 1:
        return None
    return next(iter(found.values()))


def build_metadata_index(models_dev: object) -> MetadataIndex:
    """Build a reusable lookup index with one pass over models.dev data."""
    if not isinstance(models_dev, Mapping):
        return MetadataIndex({}, {}, {})

    providers_by_id = {}
    providers_by_endpoint: dict[str, list[_Provider]] = {}
    by_normalized_id: dict[str, list[tuple[str, Mapping[str, object]]]] = {}
    vendors = set()

    for provider_id, provider in models_dev.items():
        if not isinstance(provider_id, str) or not isinstance(provider, Mapping):
            continue
        models_by_key = {}
        models_by_lower = {}
        models_by_normalized = {}
        models = provider.get("models")
        if isinstance(models, Mapping):
            for model_key, model in models.items():
                if not isinstance(model_key, str) or not isinstance(model, Mapping):
                    continue
                models_by_key[model_key] = model
                models_by_lower.setdefault(model_key.lower(), model)
                normalized = normalize_model_id(model_key)
                if normalized:
                    models_by_normalized.setdefault(normalized, model)
                    by_normalized_id.setdefault(normalized, []).append(
                        (provider_id, model)
                    )
                canonical = model.get("canonical_model_id")
                if isinstance(canonical, str) and "/" in canonical:
                    vendors.add(canonical.partition("/")[0])
        indexed = _Provider(
            provider_id,
            models_by_key,
            models_by_lower,
            models_by_normalized,
        )
        providers_by_id[provider_id] = indexed
        endpoint = _endpoint_key(provider.get("api"))
        if endpoint is not None:
            providers_by_endpoint.setdefault(endpoint, []).append(indexed)

    provider_models = {
        provider_id: provider.models_by_key
        for provider_id, provider in providers_by_id.items()
    }
    vendor_models = {}
    vendor_set = frozenset(vendors)
    for normalized, entries in by_normalized_id.items():
        model = _unique_vendor_model(
            normalized, entries, provider_models, vendor_set
        )
        if model is not None:
            vendor_models[normalized] = model

    return MetadataIndex(
        {
            endpoint: tuple(endpoint_providers)
            for endpoint, endpoint_providers in providers_by_endpoint.items()
        },
        providers_by_id,
        vendor_models,
    )


def _find_preset_model(
    model_id: str, base_url: object, presets: object
) -> Mapping[str, object] | None:
    if presets is None or isinstance(presets, (str, bytes, Mapping)):
        return None
    target = _endpoint_key(base_url)
    if target is None:
        return None
    matches = []
    try:
        iterator = iter(presets)
    except TypeError:
        return None
    for preset in iterator:
        if not isinstance(preset, Mapping):
            continue
        preset_url = preset.get("base_url", preset.get("baseUrl"))
        if _endpoint_key(preset_url) != target:
            continue
        models = preset.get("models")
        if not isinstance(models, Mapping):
            continue
        by_key = {
            key: value
            for key, value in models.items()
            if isinstance(key, str) and isinstance(value, Mapping)
        }
        by_lower = {}
        by_normalized = {}
        for key, value in by_key.items():
            by_lower.setdefault(key.lower(), value)
            by_normalized.setdefault(normalize_model_id(key), value)
        model = _lookup_model(by_key, by_lower, by_normalized, model_id)
        if model is not None:
            matches.append(model)
    return matches[0] if len(matches) == 1 else None


def _find_provider_model(
    model_id: str, base_url: object, index: MetadataIndex
) -> Mapping[str, object] | None:
    target = _endpoint_key(base_url)
    if target is None:
        return None
    matches = []
    for provider in index.providers_by_endpoint.get(target, ()):
        model = _lookup_model(
            provider.models_by_key,
            provider.models_by_lower,
            provider.models_by_normalized,
            model_id,
        )
        if model is not None:
            matches.append(model)
    return matches[0] if len(matches) == 1 else None


def _unknown_metadata() -> dict[str, object]:
    result: dict[str, object] = {field: None for field in _FIELDS}
    result["sources"] = {field: None for field in _FIELDS}
    return result


def resolve_metadata(
    model_id: str,
    base_url: str,
    models_dev: object = None,
    presets: object = None,
    *,
    index: MetadataIndex | None = None,
) -> dict[str, object]:
    """Resolve safe model metadata from presets and a models.dev snapshot.

    Field precedence is preset, same-endpoint models.dev provider, then a unique
    models.dev vendor reference. Unknown or ambiguous values remain None.
    """
    result = _unknown_metadata()
    if not isinstance(model_id, str) or not model_id.strip():
        return result
    requested_id = model_id.strip()
    if index is None:
        index = build_metadata_index(models_dev)
    elif not isinstance(index, MetadataIndex):
        return result

    candidates = []
    preset_model = _find_preset_model(requested_id, base_url, presets)
    if preset_model is not None:
        candidates.append(
            (SOURCE_PRESET, _metadata_from_preset(preset_model, requested_id))
        )

    provider_model = _find_provider_model(requested_id, base_url, index)
    if provider_model is not None:
        candidates.append(
            (
                SOURCE_MODELS_DEV_PROVIDER,
                _metadata_from_models_dev(provider_model, requested_id),
            )
        )

    vendor_model = index.vendor_models_by_normalized.get(
        normalize_model_id(requested_id)
    )
    if vendor_model is not None:
        vendor = _metadata_from_models_dev(vendor_model, requested_id)
        candidates.append((SOURCE_MODELS_DEV_VENDOR, vendor))

    sources = result["sources"]
    for source, candidate in candidates:
        for field in _FIELDS:
            if result[field] is None and candidate[field] is not None:
                value = candidate[field]
                result[field] = list(value) if field == "reasoning_levels" else value
                sources[field] = source
    return result
