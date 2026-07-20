from __future__ import annotations

import base64
from typing import Any
from urllib.parse import urlparse


PART_KEYS = ("text", "raw", "url", "data")


def normalize_a2a_parts(parts: Any, *, allow_legacy: bool = False) -> list[dict[str, Any]]:
    """Validate A2A 1.0 Part oneofs and return a transport-neutral representation."""
    if not isinstance(parts, list) or not parts:
        raise ValueError("message.parts must be a non-empty array")
    normalized: list[dict[str, Any]] = []
    for index, original in enumerate(parts):
        if not isinstance(original, dict):
            raise ValueError(f"message.parts[{index}] must be an object")
        part = dict(original)
        mapping = "a2a-1.0"
        if "kind" in part:
            if not allow_legacy:
                raise ValueError("legacy Part kind mapping requires explicit compatibility mode")
            kind = part.pop("kind")
            if kind not in PART_KEYS:
                raise ValueError(f"message.parts[{index}] has an unsupported legacy kind")
            mapping = "legacy-kind"
        present = [key for key in PART_KEYS if key in part]
        if len(present) != 1:
            raise ValueError(f"message.parts[{index}] must contain exactly one of text, raw, url, or data")
        kind = present[0]
        value = part[kind]
        result: dict[str, Any] = {"type": kind, "mapping": mapping}
        if kind == "text":
            if not isinstance(value, str):
                raise ValueError(f"message.parts[{index}].text must be a string")
            result["text"] = value
        elif kind == "raw":
            if not isinstance(value, str):
                raise ValueError(f"message.parts[{index}].raw must be base64 text")
            try:
                result["bytes"] = base64.b64decode(value, validate=True)
            except Exception as exc:
                raise ValueError(f"message.parts[{index}].raw is not valid base64") from exc
        elif kind == "url":
            if not isinstance(value, str) or urlparse(value).scheme not in {"http", "https", "hub-cas"}:
                raise ValueError(f"message.parts[{index}].url must use http, https, or hub-cas")
            result["url"] = value
        else:
            if not isinstance(value, dict):
                raise ValueError(f"message.parts[{index}].data must be an object")
            result["data"] = value
        for metadata in ("filename", "mediaType"):
            if metadata in part:
                if not isinstance(part[metadata], str) or not part[metadata]:
                    raise ValueError(f"message.parts[{index}].{metadata} must be a non-empty string")
                result[metadata] = part[metadata]
        normalized.append(result)
    return normalized


def public_part(part: dict[str, Any]) -> dict[str, Any]:
    """Convert the internal representation back to the official oneof shape."""
    kind = part["type"]
    if kind == "raw":
        value: Any = base64.b64encode(part["bytes"]).decode("ascii")
    else:
        value = part[kind]
    result = {kind: value}
    for metadata in ("filename", "mediaType"):
        if metadata in part:
            result[metadata] = part[metadata]
    return result
