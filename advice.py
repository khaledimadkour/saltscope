"""
AI soil advice for SaltScope, using the Google Gemini API.

The key is read from the GEMINI_API_KEY environment variable and is only ever
sent to Google, in a request header. It never reaches the browser.

Optional: GEMINI_MODEL (default below) if Google retires or renames the model.
"""

import json
import math
import os
import re
import sys
import time
from datetime import datetime, timezone

import requests

GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
DEFAULT_MODEL = "gemini-2.5-flash"
TIMEOUT_S = 40          # per attempt
TOTAL_BUDGET_S = 120    # across all attempts (the web page waits up to 150 s)
RETRY_PAUSE_S = 2
TRIES_PER_MODEL = 2      # for the main model; each fallback gets one try
MAX_ATTEMPTS = 6

# Gemini models are sometimes overloaded (HTTP 503). If the main model keeps
# failing, these are tried next. Override with GEMINI_FALLBACK_MODELS (comma
# separated, or empty for none).
DEFAULT_FALLBACKS = ("gemini-3.7-flash", "gemini-3.5-flash-lite", "gemini-2.5-flash")

_sleep = time.sleep

INDEX_NAMES = ("NDSI", "SI", "SI2", "SWIR_ND")

LEVEL_LABELS = {
    1: "Strong low-salinity spectral similarity",
    2: "Leaning toward low salinity",
    3: "Intermediate / uncertain",
    4: "Leaning toward high salinity",
    5: "Strong high-salinity spectral similarity",
}

MONTH_NAMES = [
    "January", "February", "March", "April", "May", "June",
    "July", "August", "September", "October", "November", "December",
]


class ApiError(Exception):
    def __init__(self, status, code, message, details=None):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.details = details


# ------------------------------------------------------------------
# Configuration
# ------------------------------------------------------------------

def api_key():
    return (os.environ.get("GEMINI_API_KEY") or "").strip()


def key_present():
    return bool(api_key())


def model_name():
    name = (os.environ.get("GEMINI_MODEL") or DEFAULT_MODEL).strip()
    if not re.fullmatch(r"[A-Za-z0-9._-]{1,80}", name):
        raise ApiError(
            500, "ai_config",
            "GEMINI_MODEL contains characters that are not allowed. "
            "Use a model name such as gemini-2.5-flash.",
        )
    return name


_MODEL_RE = re.compile(r"[A-Za-z0-9._-]{1,80}")


def model_chain():
    """The main model first, then any fallbacks, without duplicates."""
    chain = [model_name()]
    raw = os.environ.get("GEMINI_FALLBACK_MODELS")
    names = DEFAULT_FALLBACKS if raw is None else [n.strip() for n in raw.split(",")]
    for name in names:
        if name and _MODEL_RE.fullmatch(name) and name not in chain:
            chain.append(name)
    return chain


def _log(message):
    print("[advice] " + message, file=sys.stderr, flush=True)


# ------------------------------------------------------------------
# Season and hemisphere
# ------------------------------------------------------------------

_NORTH_SEASON = {
    12: "winter", 1: "winter", 2: "winter",
    3: "spring", 4: "spring", 5: "spring",
    6: "summer", 7: "summer", 8: "summer",
    9: "autumn", 10: "autumn", 11: "autumn",
}
_FLIP = {"winter": "summer", "summer": "winter", "spring": "autumn", "autumn": "spring"}


def season_info(lat, when=None):
    """Hemisphere and meteorological season for a latitude on a given date."""
    when = when or datetime.now(timezone.utc)
    hemisphere = "northern" if lat >= 0 else "southern"
    if abs(lat) <= 10:
        season = "tropical (no four distinct seasons; plan around wet and dry periods)"
    elif hemisphere == "northern":
        season = _NORTH_SEASON[when.month]
    else:
        season = _FLIP[_NORTH_SEASON[when.month]]
    return {
        "hemisphere": hemisphere,
        "season": season,
        "month": MONTH_NAMES[when.month - 1],
        "year": when.year,
    }


# ------------------------------------------------------------------
# Request validation (numbers only: no free text ever reaches the prompt)
# ------------------------------------------------------------------

def parse_advice_request(data):
    if not isinstance(data, dict):
        raise ApiError(400, "bad_request", "The request must be a JSON object.")

    def number(source, name, low, high, label=None):
        value = source.get(name)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ApiError(400, "bad_request", f"'{label or name}' must be a number.")
        value = float(value)
        if not math.isfinite(value) or not low <= value <= high:
            raise ApiError(
                400, "bad_request", f"'{label or name}' must be between {low} and {high}."
            )
        return value

    indices = data.get("indices")
    if not isinstance(indices, dict):
        raise ApiError(400, "bad_request", "'indices' must be an object.")

    return {
        "lat": number(data, "lat", -85, 85),
        "lon": number(data, "lon", -180, 180),
        "days": int(round(number(data, "days", 1, 365))),
        "buffer_m": int(round(number(data, "buffer_m", 1, 5000))),
        "score": number(data, "score", 0, 100),
        "bare_soil_fraction": number(data, "bare_soil_fraction", 0, 1),
        "mean_ndvi": number(data, "mean_ndvi", -1, 1),
        "indices": {
            name: number(indices, name, -5, 5, f"indices.{name}") for name in INDEX_NAMES
        },
    }


# ------------------------------------------------------------------
# Prompt
# ------------------------------------------------------------------

SYSTEM_PROMPT = """You are an agronomy assistant inside a small soil screening tool.
You receive satellite-derived numbers for one location. Rules:
- The "salinity similarity score" is NOT measured soil salt (ECe, dS/m). It only says how closely the satellite spectrum matches a few known low-salinity and high-salinity reference fields. Never present it as a measured value.
- Soil texture or soil type cannot be read from these numbers. Do not invent them. You may use the spectral clues provided only as weak hints, and say so.
- Use the coordinates to judge the likely climate and region. Recommend crops that can really be grown there in the stated season, and that suit the salinity level.
- Be practical and brief. Plain language, short sentences, no filler.
- If data reliability is low, say the advice is less certain.
- Always recommend confirming with a soil test (EC and sodium/SAR) before large investments.
Return JSON that matches the requested schema."""


def build_prompt(req, level, when=None):
    seasonal = season_info(req["lat"], when)
    idx = req["indices"]
    bare = req["bare_soil_fraction"]
    if bare >= 0.70:
        reliability = "good (mostly bare soil)"
    elif bare >= 0.40:
        reliability = "moderate"
    else:
        reliability = "low (little bare soil, so the spectrum is partly vegetation)"

    lines = [
        "Location and date",
        f"- Coordinates: {req['lat']:.5f}, {req['lon']:.5f}",
        f"- Hemisphere: {seasonal['hemisphere']}",
        f"- Date: {seasonal['month']} {seasonal['year']} (season there: {seasonal['season']})",
        "",
        "Satellite screening (Sentinel-2, median over clear pixels)",
        f"- Area analysed: about {req['buffer_m'] * 2} m wide, imagery from the last {req['days']} days",
        f"- High-salinity similarity score: {req['score']:.1f}% (0 = matches low-salinity reference sites, 100 = matches high-salinity reference sites)",
        f"- Screening class: {LEVEL_LABELS[level]}",
        f"- Data reliability: {reliability}; bare soil {bare * 100:.0f}%, mean NDVI {req['mean_ndvi']:.2f}",
        "- Spectral indices: " + ", ".join(f"{n} {idx[n]:.4f}" for n in INDEX_NAMES),
        "",
        "Task",
        "1. assessment: 1 to 2 sentences on what this probably means for the soil, with honest uncertainty.",
        "2. steps: 4 to 6 short, practical steps to make this soil healthier and manage salinity, tailored to this class. One or two sentences each.",
        "3. crops: the 3 best crops to grow now at this location, given the hemisphere, the current season, the likely climate, and the salinity level. For each, give its salt tolerance (Low, Moderate or High) and one short reason.",
        "4. caution: one sentence on the main limit of this advice.",
        "",
        'Reply as JSON with these keys: "assessment" (string), "steps" (list of {"title", "detail"}), '
        '"crops" (list of {"name", "salt_tolerance", "reason"}) and "caution" (string).',
    ]
    return SYSTEM_PROMPT, "\n".join(lines)


RESPONSE_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "assessment": {"type": "STRING"},
        "steps": {
            "type": "ARRAY",
            "items": {
                "type": "OBJECT",
                "properties": {"title": {"type": "STRING"}, "detail": {"type": "STRING"}},
                "required": ["title", "detail"],
            },
        },
        "crops": {
            "type": "ARRAY",
            "items": {
                "type": "OBJECT",
                "properties": {
                    "name": {"type": "STRING"},
                    "salt_tolerance": {"type": "STRING"},
                    "reason": {"type": "STRING"},
                },
                "required": ["name", "salt_tolerance", "reason"],
            },
        },
        "caution": {"type": "STRING"},
    },
    "required": ["assessment", "steps", "crops", "caution"],
}


# ------------------------------------------------------------------
# Gemini call
# ------------------------------------------------------------------

def _redact(text, key):
    text = str(text)
    return text.replace(key, "***") if key else text


def _error_for_status(response, key, model):
    status = response.status_code
    body = _redact(response.text[:400], key)
    if status == 429:
        err = ApiError(
            429, "ai_quota",
            "The free Gemini limit was reached. Wait a minute and try again.", body,
        )
    elif status == 404:
        err = ApiError(
            502, "ai_model",
            f"Gemini does not know the model '{model}'. Set GEMINI_MODEL to a current "
            "model listed in Google AI Studio, then restart the server.", body,
        )
    elif status in (401, 403) or (status == 400 and "API key" in response.text):
        err = ApiError(
            502, "ai_key",
            "Gemini rejected the API key. Check GEMINI_API_KEY, then restart the server.", body,
        )
    elif status == 400:
        err = ApiError(502, "ai_request", "Gemini could not process this request.", body)
    else:
        err = ApiError(
            502, "ai_upstream",
            f"Gemini is overloaded or having problems right now (HTTP {status}). "
            "This is on Google's side and usually clears up within minutes.", body,
        )
    err.upstream = status
    return err


def _extract_json(text):
    text = text.strip()
    fence = re.fullmatch(r"```(?:json)?\s*(.*?)\s*```", text, flags=re.DOTALL)
    if fence:
        text = fence.group(1)
    return json.loads(text)


def _clean(value, limit):
    if not isinstance(value, str):
        value = "" if value is None else str(value)
    value = " ".join(value.split())
    return value if len(value) <= limit else value[: limit - 1].rstrip() + "…"


def validate_advice(raw):
    """Check the model's JSON and cut it down to a safe, small shape."""
    if not isinstance(raw, dict):
        raise ValueError("not an object")

    steps = []
    for item in raw.get("steps") or []:
        if isinstance(item, dict):
            title, detail = _clean(item.get("title"), 90), _clean(item.get("detail"), 320)
            if title or detail:
                steps.append({"title": title, "detail": detail})
    crops = []
    for item in raw.get("crops") or []:
        if isinstance(item, dict) and _clean(item.get("name"), 60):
            crops.append({
                "name": _clean(item.get("name"), 60),
                "salt_tolerance": _clean(item.get("salt_tolerance"), 24),
                "reason": _clean(item.get("reason"), 240),
            })

    assessment = _clean(raw.get("assessment"), 600)
    if not assessment or not steps or not crops:
        raise ValueError("missing sections")
    return {
        "assessment": assessment,
        "steps": steps[:6],
        "crops": crops[:3],
        "caution": _clean(raw.get("caution"), 320),
    }


def generation_config(model, thinking=True, schema=True):
    """Settings that suit the model family.

    Gemini 3 models think by default and Google advises keeping their default
    temperature, so we only lower the thinking level. Gemini 2.5 Flash models
    can have thinking switched off, which makes this simple task much faster.
    """
    config = {"maxOutputTokens": 8192, "responseMimeType": "application/json"}
    if schema:
        config["responseSchema"] = RESPONSE_SCHEMA
    if model.startswith("gemini-3"):
        if thinking:
            config["thinkingConfig"] = {"thinkingLevel": "low"}
    else:
        config["temperature"] = 0.4
        if thinking and model.startswith("gemini-2.5-flash"):
            config["thinkingConfig"] = {"thinkingBudget": 0}
    return config


def call_gemini(system_prompt, user_prompt, key, model, thinking=True, schema=True, timeout=TIMEOUT_S):
    """One request to one model. Raises ApiError on any failure."""
    body = {
        "systemInstruction": {"parts": [{"text": system_prompt}]},
        "contents": [{"role": "user", "parts": [{"text": user_prompt}]}],
        "generationConfig": generation_config(model, thinking, schema),
    }
    try:
        response = requests.post(
            GEMINI_URL.format(model=model),
            json=body,
            headers={"x-goog-api-key": key},
            timeout=timeout,
        )
    except requests.exceptions.Timeout:
        raise ApiError(504, "ai_timeout", f"Gemini ({model}) took too long to answer.")
    except requests.exceptions.RequestException as exc:
        raise ApiError(
            502, "ai_unreachable",
            "Could not reach the Gemini API. Check your internet connection.",
            type(exc).__name__,
        )

    if response.status_code != 200:
        raise _error_for_status(response, key, model)

    try:
        data = response.json()
    except ValueError:
        raise ApiError(502, "ai_bad_response", "Gemini sent an unreadable answer.")

    candidates = data.get("candidates") or []
    if not candidates:
        reason = (data.get("promptFeedback") or {}).get("blockReason")
        raise ApiError(
            502, "ai_blocked",
            "Gemini returned no answer for this request.",
            f"blockReason={reason}" if reason else None,
        )
    candidate = candidates[0]
    if candidate.get("finishReason") == "MAX_TOKENS":
        raise ApiError(502, "ai_bad_response", "Gemini's answer was cut off.")

    parts = (candidate.get("content") or {}).get("parts") or []
    text = "".join(p.get("text", "") for p in parts if isinstance(p, dict) and not p.get("thought"))
    try:
        return validate_advice(_extract_json(text))
    except (ValueError, TypeError):
        raise ApiError(
            502, "ai_bad_response",
            "Gemini's answer was not in the expected format.",
        )


# ------------------------------------------------------------------
# Retries and fallback models
# ------------------------------------------------------------------

_RETRY_SAME_MODEL = {"ai_upstream", "ai_timeout", "ai_unreachable", "ai_bad_response", "ai_blocked"}
_TRY_NEXT_MODEL = {"ai_quota", "ai_model"}


def _label(err):
    upstream = getattr(err, "upstream", None)
    if upstream:
        return f"HTTP {upstream}"
    return {
        "ai_timeout": "timed out",
        "ai_unreachable": "no connection",
        "ai_bad_response": "bad answer",
        "ai_blocked": "no answer",
    }.get(err.code, err.code)


def generate(system_prompt, user_prompt, key, chain, clock=time.monotonic):
    """Ask the models in order until one answers. Returns (advice, model_used)."""
    started = clock()
    tried = []
    last = None
    attempts = 0
    out_of_time = False

    for model in chain:
        thinking = schema = True
        tries = 0
        limit = TRIES_PER_MODEL if model == chain[0] else 1
        while tries < limit and attempts < MAX_ATTEMPTS:
            remaining = TOTAL_BUDGET_S - (clock() - started)
            if remaining < 5:
                out_of_time = True
                break
            attempts += 1
            began = clock()
            try:
                result = call_gemini(
                    system_prompt, user_prompt, key, model,
                    thinking=thinking, schema=schema, timeout=min(TIMEOUT_S, remaining),
                )
                _log(f"{model}: ok in {clock() - began:.1f}s")
                return result, model
            except ApiError as err:
                last = err
                tried.append(f"{model} {_label(err)}")
                _log(f"{model}: {err.code} ({_label(err)}) {err.details or ''}".rstrip())

                if err.code == "ai_key":
                    raise
                if err.code == "ai_request":
                    detail = (err.details or "").lower()
                    if thinking and "thinking" in detail:
                        thinking = False
                        continue
                    if schema:
                        schema = False
                        continue
                    raise
                if err.code in _TRY_NEXT_MODEL:
                    break
                if err.code in _RETRY_SAME_MODEL:
                    tries += 1
                    if tries < limit:
                        _sleep(RETRY_PAUSE_S)
                    continue
                raise
        if out_of_time or attempts >= MAX_ATTEMPTS:
            break

    if last is None:
        raise ApiError(504, "ai_timeout", "Gemini did not answer in time. Try again.")
    summary = ", ".join(tried)
    raise ApiError(
        last.status, last.code, f"{last.message} (Tried: {summary}.)", last.details
    )


def get_advice(req, level, when=None):
    key = api_key()
    if not key:
        raise ApiError(
            503, "no_ai_key",
            "AI advice is turned off. Set GEMINI_API_KEY and restart the server.",
        )
    chain = model_chain()
    system_prompt, user_prompt = build_prompt(req, level, when)
    advice, used = generate(system_prompt, user_prompt, key, chain)
    seasonal = season_info(req["lat"], when)
    return {
        "ok": True,
        "advice": advice,
        "context": {
            "hemisphere": seasonal["hemisphere"],
            "season": seasonal["season"],
            "month": seasonal["month"],
            "model": used,
            "fallback": used != chain[0],
        },
    }
