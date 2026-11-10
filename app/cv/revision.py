"""Content revision shared by saved CVs, Job Match, and AI Tailoring."""

import hashlib
import json
from typing import Any


def content_revision(content: dict[str, Any]) -> str:
    # Object key order isn't meaningful. Array order (in particular, bullet positions) is.
    canonical = json.dumps(content, sort_keys=True, ensure_ascii=False, separators=(',', ':'))
    return hashlib.sha256(canonical.encode('utf-8')).hexdigest()
