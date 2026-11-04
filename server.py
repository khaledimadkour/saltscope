#!/usr/bin/env python3
"""
SaltScope local server (prototype).

Serves the website in ./web and exposes a small JSON API that calls the
functions in main.py (the Sentinel-2 salinity screening code). Your Copernicus
credentials stay on this computer and are never sent to the browser.

Run (same folder as main.py):

    export CDSE_CLIENT_ID="..."
    export CDSE_CLIENT_SECRET="..."
    python server.py

Then open http://127.0.0.1:8000
"""

import argparse
import json
import math
import os
import sys
import threading
import time
import traceback
from datetime import datetime, timezone
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import advice
import main as salinity  # your screening code, used unchanged
from advice import ApiError

ROOT = Path(__file__).resolve().parent
WEB_DIR = ROOT / "web"

MIN_BUFFER_M, MAX_BUFFER_M = 50, 2000
MIN_DAYS, MAX_DAYS = 7, 365
MAX_BODY_BYTES = 8192
TOKEN_TTL_S = 8 * 60  # Copernicus tokens last ~10 minutes

LOOPBACK_HOSTS = {"localhost", "127.0.0.1", "::1"}

# Mirrors the thresholds in main.classify_salinity (25 / 40 / 60 / 75).
LEVEL_THRESHOLDS = (25, 40, 60, 75)


# ------------------------------------------------------------------
# Errors
# ------------------------------------------------------------------

def exit_message(exc):
    """main.py reports failures with sys.exit(text); turn that into one line."""
    text = "" if exc.code is None else str(exc.code)
    return " ".join(text.split())


# ------------------------------------------------------------------
# Copernicus access (token cache + retry)
# ------------------------------------------------------------------

_token = {"value": None, "expires": 0.0}
_token_lock = threading.Lock()
_slots = threading.BoundedSemaphore(2)  # at most two scans at once
_advice_slot = threading.BoundedSemaphore(1)  # one AI request at a time (free-tier limits)


def credentials_present():
    return bool(
        os.environ.get("CDSE_CLIENT_ID") and os.environ.get("CDSE_CLIENT_SECRET")
    )


def get_cached_token(force=False):
    with _token_lock:
        if not force and _token["value"] and time.time() < _token["expires"]:
            return _token["value"]
        try:
            value = salinity.get_token()
        except SystemExit as exc:
            details = exit_message(exc)
            if "Client Error" in details:
                message = (
                    "Copernicus rejected the sign-in. Check CDSE_CLIENT_ID and "
                    "CDSE_CLIENT_SECRET, then restart the server."
                )
            else:
                message = (
                    "Could not reach Copernicus to sign in. "
                    "Check your internet connection and try again."
                )
            raise ApiError(502, "auth_failed", message, details)
        _token["value"] = value
        _token["expires"] = time.time() + TOKEN_TTL_S
        return value


def fetch_imagery(lat, lon, buffer_m, days):
    for attempt in (0, 1):
        token = get_cached_token(force=attempt == 1)
        try:
            return salinity.fetch_bands(lat, lon, token, buffer_m, days)
        except SystemExit as exc:
            details = exit_message(exc)
            if attempt == 0 and "401" in details:
                continue  # token expired early: sign in again once
            raise ApiError(
                502,
                "upstream",
                "Copernicus could not return imagery right now. Try again in a moment.",
                details,
            )


# ------------------------------------------------------------------
# Analysis
# ------------------------------------------------------------------

def level_for(score):
    for position, limit in enumerate(LEVEL_THRESHOLDS, start=1):
        if score < limit:
            return position
    return len(LEVEL_THRESHOLDS) + 1


def reliability_for(bare_fraction):
    if bare_fraction >= 0.70:
        return {"level": "good", "text": "Good bare-soil coverage."}
    if bare_fraction >= 0.40:
        return {"level": "moderate", "text": "Moderate bare-soil coverage."}
    return {
        "level": "low",
        "text": "Low bare-soil coverage. The result may be unreliable.",
    }


def parse_request(data):
    if not isinstance(data, dict):
        raise ApiError(400, "bad_request", "The request must be a JSON object.")

    def number(name, low, high):
        value = data.get(name)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ApiError(400, "bad_request", f"'{name}' must be a number.")
        value = float(value)
        if not math.isfinite(value) or not low <= value <= high:
            raise ApiError(
                400, "bad_request", f"'{name}' must be between {low} and {high}."
            )
        return value

    lat = number("lat", -85, 85)
    lon = number("lon", -180, 180)
    buffer_m = int(round(number("buffer_m", 0, 1_000_000)))
    days = int(round(number("days", 0, 100_000)))
    buffer_m = max(MIN_BUFFER_M, min(MAX_BUFFER_M, buffer_m))
    days = max(MIN_DAYS, min(MAX_DAYS, days))
    return lat, lon, buffer_m, days


def run_analysis(lat, lon, buffer_m, days):
    if not _slots.acquire(timeout=1):
        raise ApiError(
            429,
            "busy",
            "Another scan is still running. Wait for it to finish, then try again.",
        )
    try:
        arr = fetch_imagery(lat, lon, buffer_m, days)
        indices, note = salinity.compute_indices(arr)
        if indices is None:
            raise ApiError(
                422,
                "no_clear_pixels",
                "No cloud-free imagery was found for this area in the chosen "
                "period. Try a longer imagery window.",
                note,
            )
        result = salinity.classify_salinity(indices)
    finally:
        _slots.release()

    numbers = [indices[name] for name in salinity.INDEX_NAMES]
    numbers += [indices["bare_soil_fraction"], indices["mean_NDVI"], result["score"]]
    if not all(math.isfinite(n) for n in numbers):
        raise ApiError(
            422, "bad_data", "The imagery for this area could not be analyzed."
        )

    bare = indices["bare_soil_fraction"]
    return {
        "ok": True,
        "fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "location": {"lat": lat, "lon": lon, "buffer_m": buffer_m, "days": days},
        "indices": {name: indices[name] for name in salinity.INDEX_NAMES},
        "bare_soil_fraction": bare,
        "mean_ndvi": indices["mean_NDVI"],
        "note": note,
        "reliability": reliability_for(bare),
        "result": {
            "score": result["score"],
            "classification": result["classification"],
            "level": level_for(result["score"]),
            "distance_low": result["distance_low"],
            "distance_high": result["distance_high"],
        },
        "reference": {
            "low": result["low_reference"],
            "high": result["high_reference"],
            "low_count": len(salinity.REFERENCES["low"]),
            "high_count": len(salinity.REFERENCES["high"]),
        },
    }


def run_advice(data):
    req = advice.parse_advice_request(data)
    if not advice.key_present():
        raise ApiError(
            503,
            "no_ai_key",
            "AI advice is turned off. Set GEMINI_API_KEY and restart the server.",
        )
    if not _advice_slot.acquire(timeout=1):
        raise ApiError(
            429, "busy", "Another AI request is still running. Wait for it, then try again."
        )
    try:
        return advice.get_advice(req, level_for(req["score"]))
    finally:
        _advice_slot.release()


# ------------------------------------------------------------------
# HTTP
# ------------------------------------------------------------------

def hostname_of(host_header):
    host = (host_header or "").strip().lower()
    if host.startswith("["):
        return host[1:host.find("]")] if "]" in host else host
    return host.split(":")[0]


class Handler(SimpleHTTPRequestHandler):
    server_version = "SaltScope"
    restrict_hosts = True  # set in main() when bound to a loopback address

    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(WEB_DIR), **kwargs)

    def end_headers(self):
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        super().end_headers()

    # -- helpers --------------------------------------------------

    def _send_json(self, status, payload):
        body = json.dumps(payload, allow_nan=False).encode("utf-8")
        try:
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass  # the browser went away (for example the user cleared the area)

    def _send_error_json(self, error):
        payload = {"code": error.code, "message": error.message}
        if error.details:
            payload["details"] = error.details
        self._send_json(error.status, {"ok": False, "error": payload})

    def _host_allowed(self):
        if not self.restrict_hosts:
            return True
        return hostname_of(self.headers.get("Host")) in LOOPBACK_HOSTS

    # -- routes ---------------------------------------------------

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path == "/api/health":
            if not self._host_allowed():
                return self._send_error_json(
                    ApiError(403, "forbidden", "Unexpected host name.")
                )
            return self._send_json(
                200,
                {
                    "ok": True,
                    "credentials": credentials_present(),
                    "ai": advice.key_present(),
                },
            )
        if path.startswith("/api/"):
            return self._send_error_json(ApiError(404, "not_found", "Unknown API route."))
        return super().do_GET()

    def do_POST(self):
        path = self.path.split("?", 1)[0]
        try:
            if path not in ("/api/salinity", "/api/advice"):
                raise ApiError(404, "not_found", "Unknown API route.")
            if not self._host_allowed():
                raise ApiError(403, "forbidden", "Unexpected host name.")
            # Requiring JSON blocks cross-site form posts to this local server.
            if self.headers.get_content_type() != "application/json":
                raise ApiError(415, "bad_request", "Send the request as application/json.")
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                length = 0
            if length <= 0:
                raise ApiError(400, "bad_request", "The request body is empty.")
            if length > MAX_BODY_BYTES:
                raise ApiError(413, "bad_request", "The request body is too large.")
            try:
                data = json.loads(self.rfile.read(length))
            except ValueError:
                raise ApiError(400, "bad_request", "The request body is not valid JSON.")

            if path == "/api/advice":
                self._send_json(200, run_advice(data))
                return

            lat, lon, buffer_m, days = parse_request(data)
            if not credentials_present():
                raise ApiError(
                    503,
                    "no_credentials",
                    "The server has no Copernicus credentials. Set CDSE_CLIENT_ID and "
                    "CDSE_CLIENT_SECRET, then restart the server.",
                )
            self._send_json(200, run_analysis(lat, lon, buffer_m, days))
        except ApiError as error:
            self._send_error_json(error)
        except Exception:
            traceback.print_exc()
            self._send_error_json(
                ApiError(
                    500,
                    "internal",
                    "Something went wrong while analyzing this area. "
                    "Check the server terminal for details.",
                )
            )


# ------------------------------------------------------------------
# Entry point
# ------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="SaltScope local server")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()

    if not WEB_DIR.is_dir():
        sys.exit(f"Cannot find the website folder: {WEB_DIR}")

    Handler.restrict_hosts = args.host in LOOPBACK_HOSTS
    if not Handler.restrict_hosts:
        print(
            "Warning: the server is reachable from your network and has no login. "
            "Anyone who can open it can use your Copernicus quota."
        )
    if not credentials_present():
        print(
            "Warning: CDSE_CLIENT_ID / CDSE_CLIENT_SECRET are not set. "
            "The page will open, but scans are disabled until you set them and restart."
        )

    try:
        httpd = ThreadingHTTPServer((args.host, args.port), Handler)
    except OSError as exc:
        sys.exit(f"Cannot start on {args.host}:{args.port}: {exc}. Try --port 8001.")

    shown_host = "127.0.0.1" if args.host in ("0.0.0.0", "::") else args.host
    print(f"SaltScope is running at http://{shown_host}:{args.port}  (Ctrl+C to stop)")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
    finally:
        httpd.server_close()


if __name__ == "__main__":
    main()
