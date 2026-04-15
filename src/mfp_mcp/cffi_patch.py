"""
curl_cffi bootstrap for MFP MCP Server
Monkey-patches cloudscraper and myfitnesspal to use curl_cffi
which bypasses Cloudflare's TLS fingerprint detection.

v2: Adds local weight-log fallback for get_measurements / set_measurements
because MFP deprecated the /measurements/edit endpoint for cookie-auth
sessions (2026-04-15). HTML page now redirects to /account/logout.
Workaround: JSON file at ~/.mfp_mcp/weight_log.json
"""
import json
import logging
import datetime
from collections import OrderedDict
from pathlib import Path
from http.cookiejar import CookieJar

logger = logging.getLogger("mfp_mcp.cffi_patch")

WEIGHT_LOG_PATH = Path.home() / ".mfp_mcp" / "weight_log.json"


def _load_local_weight_log():
    """Load local weight-log fallback. Returns {date_iso: float_kg}."""
    if not WEIGHT_LOG_PATH.exists():
        return {}
    try:
        raw = json.loads(WEIGHT_LOG_PATH.read_text())
        return {k: float(v) for k, v in raw.items()}
    except Exception as e:
        logger.warning(f"Failed to load weight log: {e}")
        return {}


def _save_local_weight(date_iso: str, value_kg: float):
    """Append/update a weight entry in the local weight log."""
    WEIGHT_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    data = _load_local_weight_log()
    data[date_iso] = float(value_kg)
    WEIGHT_LOG_PATH.write_text(json.dumps(data, indent=2, sort_keys=True))
    logger.info(f"Saved weight {value_kg} kg for {date_iso} to local log")


def apply_curl_cffi_patch():
    """Apply curl_cffi monkey-patch to bypass Cloudflare."""
    try:
        from curl_cffi import requests as cffi_requests
    except ImportError:
        logger.warning("curl_cffi not installed, skipping patch")
        return False

    cookies_file = Path.home() / ".mfp_mcp" / "cookies.json"
    cookies = {}
    if cookies_file.exists():
        try:
            cookie_data = json.loads(cookies_file.read_text())
            cookies = cookie_data.get("cookies", {})
            logger.info(f"Loaded {len(cookies)} cookies from {cookies_file}")
        except Exception as e:
            logger.warning(f"Failed to load cookies: {e}")

    cffi_session = cffi_requests.Session(impersonate="chrome")
    for name, value in cookies.items():
        cffi_session.cookies.set(name, value, domain="www.myfitnesspal.com")

    import cloudscraper
    cloudscraper._original_create_scraper = cloudscraper.create_scraper

    def patched_create_scraper(*args, **kwargs):
        return cffi_session

    cloudscraper.create_scraper = patched_create_scraper

    import myfitnesspal.client
    _original_get_user_metadata = myfitnesspal.client.Client._get_user_metadata

    def _patched_get_user_metadata(self):
        try:
            return _original_get_user_metadata(self)
        except (KeyError, Exception) as e:
            logger.warning(f"User metadata fetch failed ({e}), using fallback")
            return {"username": "patrickbjurquist", "unit_preferences": {"energy": {"unit": "calories"}}}

    myfitnesspal.client.Client._get_user_metadata = _patched_get_user_metadata

    # Patch get_measurements to fall back to local weight log
    # MFP deprecated /measurements/edit for cookie-auth sessions (2026-04-15).
    _original_get_measurements = myfitnesspal.client.Client.get_measurements

    def _patched_get_measurements(self, measurement="Weight", lower_bound=None, upper_bound=None):
        try:
            return _original_get_measurements(self, measurement, lower_bound, upper_bound)
        except Exception as e:
            logger.warning(f"MFP get_measurements failed ({e}); falling back to local weight log")
            if measurement != "Weight":
                logger.warning(f"Local fallback only supports 'Weight', not '{measurement}'")
                raise
            raw = _load_local_weight_log()
            if not raw:
                raise RuntimeError(
                    "MFP measurements API deprecated and no local weight log found. "
                    f"Log weight via mfp_set_measurement or manually edit {WEIGHT_LOG_PATH}"
                )
            # Convert to {date: value}, filtered by bounds
            upper = upper_bound or datetime.date.today()
            lower = lower_bound or (upper - datetime.timedelta(days=30))
            out = OrderedDict()
            for date_iso in sorted(raw.keys()):
                d = datetime.datetime.strptime(date_iso, "%Y-%m-%d").date()
                if lower <= d <= upper:
                    out[d] = raw[date_iso]
            return out

    myfitnesspal.client.Client.get_measurements = _patched_get_measurements

    # Patch set_measurements to also write to local log (so future reads work)
    _original_set_measurements = myfitnesspal.client.Client.set_measurements

    def _patched_set_measurements(self, measurement="Weight", value=None, date=None):
        if value is None:
            raise ValueError("Cannot update blank value.")
        target_date = date or datetime.date.today()
        # Always write to local log first (source of truth while MFP API broken)
        if measurement == "Weight":
            _save_local_weight(target_date.isoformat(), float(value))
        # Still try MFP (may work in future); swallow errors
        try:
            return _original_set_measurements(self, measurement, value, date)
        except Exception as e:
            logger.warning(f"MFP set_measurements failed ({e}); weight saved to local log only")
            return None

    myfitnesspal.client.Client.set_measurements = _patched_set_measurements

    logger.info("curl_cffi patch applied successfully - Cloudflare bypass + weight-log fallback active")
    return True


# Apply on import
_patch_applied = apply_curl_cffi_patch()
