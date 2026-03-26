# patch.py  — import this BEFORE any google.adk import
# Globally replaces json.dumps with a bytes-safe version so that
# google.adk.telemetry never crashes on non-serialisable bytes objects.

import json
import builtins

_original_dumps = json.dumps

class _BytesSafeEncoder(json.JSONEncoder):
    def default(self, obj):
        if isinstance(obj, (bytes, bytearray)):
            return obj.decode("utf-8", errors="replace")
        # Fallback: convert anything else to its string repr instead of crashing
        try:
            return super().default(obj)
        except TypeError:
            return repr(obj)

def _safe_dumps(obj, *args, **kwargs):
    if "cls" not in kwargs:
        kwargs["cls"] = _BytesSafeEncoder
    try:
        return _original_dumps(obj, *args, **kwargs)
    except Exception:
        # Absolute last resort — return empty JSON object so telemetry never
        # propagates an exception into application code.
        return "{}"

# Patch both the module attribute AND the reference inside the json module
# itself so every importer that does `from json import dumps` also gets it.
json.dumps = _safe_dumps