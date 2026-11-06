"""Jev (TypeSafe System One) via OpenRouter — typed judgments for code, not text generation.

Endpoint: POST https://openrouter.ai/api/alpha/decisions  (model "~typesafe/jev-latest")
Body/answers follow the TypeSafe System One contract: {"state", "questions"} →
{"answers": {id: {"type": "noul"|"choice"|"score", ...}}, "usage": {..., "cost"}}.

Design rules (TypeSafe guidance):
  * code owns the workflow; Jev only answers narrow questions (Noul / Choice / Score);
  * independent questions over the same state go in ONE request (fan-out);
  * act on probabilities/confidence thresholds, never on "the model said so";
  * every call degrades gracefully: on any failure we return None and callers fall back.
Price: ~$0.04 per 1M input tokens, output free → cheap enough to call on every message.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
import time
import urllib.error
import urllib.request
from typing import Any, Dict, Optional

logger = logging.getLogger("jev")

URL = os.environ.get("JEV_URL", "https://openrouter.ai/api/alpha/decisions")
MODEL = os.environ.get("JEV_MODEL", "~typesafe/jev-latest")
TIMEOUT = float(os.environ.get("JEV_TIMEOUT", "8"))
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CACHE_FILE = os.path.join(BASE_DIR, "jev_cache.json")
_CACHE_MAX = 4000
_lock = threading.Lock()
_cache: Optional[Dict[str, Any]] = None
STATS = {"calls": 0, "cache_hits": 0, "cost": 0.0, "errors": 0, "ms": 0.0}


def _key() -> str:
    return os.environ.get("OPENROUTER_API_KEY", "")


def available() -> bool:
    return bool(_key()) and os.environ.get("JEV_DISABLED", "").lower() not in ("1", "true", "yes")


def _load_cache() -> Dict[str, Any]:
    global _cache
    if _cache is None:
        try:
            _cache = json.load(open(CACHE_FILE, encoding="utf-8"))
        except Exception:
            _cache = {}
    return _cache


def _save_cache() -> None:
    if _cache is None:
        return
    try:
        if len(_cache) > _CACHE_MAX:
            for k in list(_cache)[: len(_cache) - _CACHE_MAX]:
                _cache.pop(k, None)
        tmp = CACHE_FILE + ".tmp"
        json.dump(_cache, open(tmp, "w", encoding="utf-8"), ensure_ascii=False)
        os.replace(tmp, CACHE_FILE)
    except Exception as exc:  # cache is an optimization only
        logger.debug("jev cache save failed: %s", exc)


def decide(state: Any, questions: Dict[str, Dict[str, Any]], *, cache: bool = True,
           timeout: Optional[float] = None) -> Optional[Dict[str, Any]]:
    """One System One request. Returns the ``answers`` map, or None on any failure."""
    if not available() or not questions:
        return None
    body = {"model": MODEL, "state": state, "questions": questions}
    ck = hashlib.sha1(json.dumps(body, sort_keys=True, ensure_ascii=False).encode()).hexdigest() if cache else ""
    if cache:
        with _lock:
            hit = _load_cache().get(ck)
        if hit is not None:
            STATS["cache_hits"] += 1
            return hit
    req = urllib.request.Request(URL, data=json.dumps(body).encode("utf-8"), method="POST",
                                 headers={"Authorization": f"Bearer {_key()}", "Content-Type": "application/json"})
    t0 = time.monotonic()
    for attempt in range(2):
        try:
            with urllib.request.urlopen(req, timeout=timeout or TIMEOUT) as r:
                data = json.loads(r.read().decode("utf-8"))
            break
        except urllib.error.HTTPError as exc:
            if exc.code in (429, 529, 502, 503) and attempt == 0:
                time.sleep(0.8)
                continue
            STATS["errors"] += 1
            logger.warning("jev HTTP %s: %s", exc.code, exc.read()[:200])
            return None
        except Exception as exc:
            STATS["errors"] += 1
            logger.warning("jev error: %s", exc)
            return None
    else:
        return None
    answers = data.get("answers") or {}
    STATS["calls"] += 1
    STATS["cost"] += float((data.get("usage") or {}).get("cost") or 0)
    STATS["ms"] += (time.monotonic() - t0) * 1000
    if cache and answers:
        with _lock:
            _load_cache()[ck] = answers
            _save_cache()
    return answers


# ---------------------------------------------------------------- tiny helpers
def noul(answers: Optional[Dict[str, Any]], qid: str, default: float = 0.0) -> float:
    try:
        return float(answers[qid]["noul"])
    except Exception:
        return default


def choice(answers: Optional[Dict[str, Any]], qid: str) -> tuple[Optional[str], float]:
    try:
        a = answers[qid]
        return a["choice"], float(a.get("confidence", 0))
    except Exception:
        return None, 0.0


def score(answers: Optional[Dict[str, Any]], qid: str, default: float = -1.0) -> tuple[float, float]:
    try:
        a = answers[qid]
        return float(a["score"]), float(a.get("confidence", 0))
    except Exception:
        return default, 0.0
