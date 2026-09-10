"""
curl_cffi bootstrap for MFP MCP Server
Monkey-patches cloudscraper and myfitnesspal to use curl_cffi
which bypasses Cloudflare's TLS fingerprint detection.

v3 (2026-05-10): Replace deprecated HTML scrape with internal JSON API.
- READ:  GET  /api/user-measurements/measurements?types=Weight   (cookie auth)
- WRITE: PUT  /api/services/incubator/measurements/upsert         (cookie + CSRF)
Local JSON ~/.mfp_mcp/weight_log.json kept as fallback only.
"""
import json
import logging
import datetime
from collections import OrderedDict
from pathlib import Path

logger = logging.getLogger("mfp_mcp.cffi_patch")

WEIGHT_LOG_PATH = Path.home() / ".mfp_mcp" / "weight_log.json"
COOKIES_PATH = Path.home() / ".mfp_mcp" / "cookies.json"

MFP_BASE = "https://www.myfitnesspal.com"
MFP_GET_MEASUREMENTS_URL = MFP_BASE + "/api/user-measurements/measurements"
MFP_UPSERT_URL = MFP_BASE + "/api/services/incubator/measurements/upsert"
MFP_CSRF_URL = MFP_BASE + "/api/auth/csrf"


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


def _build_session():
    """Build a fresh curl_cffi session with current cookies. Returns None on failure."""
    try:
        from curl_cffi import requests as cffi_requests
    except ImportError:
        return None
    if not COOKIES_PATH.exists():
        return None
    try:
        cookies = json.loads(COOKIES_PATH.read_text()).get("cookies", {})
    except Exception:
        return None
    s = cffi_requests.Session(impersonate="chrome")
    for n, v in cookies.items():
        s.cookies.set(n, v, domain="www.myfitnesspal.com")
    return s


def _local_log_to_ordered(measurement, lower_bound, upper_bound):
    raw = _load_local_weight_log()
    if not raw:
        raise RuntimeError(
            "MFP measurements API failed and no local weight log found. "
            f"Log weight via mfp_set_measurement or manually edit {WEIGHT_LOG_PATH}"
        )
    upper = upper_bound or datetime.date.today()
    lower = lower_bound or (upper - datetime.timedelta(days=30))
    out = OrderedDict()
    for date_iso in sorted(raw.keys()):
        d = datetime.datetime.strptime(date_iso, "%Y-%m-%d").date()
        if lower <= d <= upper:
            out[d] = raw[date_iso]
    return out


def _fetch_measurements_v3(measurement, lower_bound, upper_bound):
    """Fetch measurements from MFP's internal JSON API (v3 endpoint)."""
    s = _build_session()
    if s is None:
        raise RuntimeError("Could not build cffi session (curl_cffi or cookies missing)")
    r = s.get(f"{MFP_GET_MEASUREMENTS_URL}?types={measurement}", timeout=15)
    if r.status_code != 200:
        raise RuntimeError(f"MFP GET measurements returned {r.status_code}: {r.text[:200]}")
    items = r.json().get("items", [])
    upper = upper_bound or datetime.date.today()
    lower = lower_bound or (upper - datetime.timedelta(days=30))
    out = OrderedDict()
    for it in sorted(items, key=lambda x: x.get("date", "")):
        try:
            d = datetime.datetime.strptime(it["date"], "%Y-%m-%d").date()
            v = float(it["value"])
        except (KeyError, ValueError):
            continue
        if lower <= d <= upper:
            out[d] = v
            # Mirror to local for backup/offline use
            if measurement == "Weight":
                try:
                    _save_local_weight(it["date"], v)
                except Exception:
                    pass
    return out


def _upsert_measurement_v3(measurement, value, target_date):
    """Push a measurement to MFP via the upsert endpoint."""
    s = _build_session()
    if s is None:
        raise RuntimeError("Could not build cffi session")
    csrf_resp = s.get(MFP_CSRF_URL, timeout=15)
    csrf = csrf_resp.json().get("csrfToken", "")
    body = {
        "item": {
            "entry_date": target_date.isoformat(),
            "unit": "kilograms",
            "value": str(float(value)),
            "type": measurement.lower(),
        }
    }
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "X-CSRF-Token": csrf,
        "Origin": MFP_BASE,
        "Referer": MFP_BASE + "/measurements/check-in",
        "X-Requested-With": "XMLHttpRequest",
    }
    r = s.put(MFP_UPSERT_URL, headers=headers, json=body, timeout=15)
    if r.status_code != 200:
        raise RuntimeError(f"MFP upsert returned {r.status_code}: {r.text[:200]}")
    return r.json()


def apply_curl_cffi_patch():
    """Apply curl_cffi monkey-patch to bypass Cloudflare."""
    try:
        from curl_cffi import requests as cffi_requests
    except ImportError:
        logger.warning("curl_cffi not installed, skipping patch")
        return False

    cookies = {}
    if COOKIES_PATH.exists():
        try:
            cookie_data = json.loads(COOKIES_PATH.read_text())
            cookies = cookie_data.get("cookies", {})
            logger.info(f"Loaded {len(cookies)} cookies from {COOKIES_PATH}")
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

    # v3: get_measurements via internal JSON API, local fallback only on failure
    def _patched_get_measurements(self, measurement="Weight", lower_bound=None, upper_bound=None):
        try:
            return _fetch_measurements_v3(measurement, lower_bound, upper_bound)
        except Exception as e:
            logger.warning(f"MFP v3 get failed ({e}); falling back to local weight log")
            if measurement != "Weight":
                logger.warning(f"Local fallback only supports 'Weight', not '{measurement}'")
                raise
            return _local_log_to_ordered(measurement, lower_bound, upper_bound)

    myfitnesspal.client.Client.get_measurements = _patched_get_measurements

    # v3: set_measurements via upsert endpoint, mirror to local
    def _patched_set_measurements(self, measurement="Weight", value=None, date=None):
        if value is None:
            raise ValueError("Cannot update blank value.")
        target_date = date or datetime.date.today()
        # Mirror to local first (always succeeds, never poisons MFP)
        if measurement == "Weight":
            try:
                _save_local_weight(target_date.isoformat(), float(value))
            except Exception as e:
                logger.warning(f"Local mirror write failed: {e}")
        # Push to MFP via upsert
        try:
            return _upsert_measurement_v3(measurement, value, target_date)
        except Exception as e:
            logger.warning(f"MFP v3 upsert failed ({e}); weight saved to local log only")
            return None

    myfitnesspal.client.Client.set_measurements = _patched_set_measurements

    logger.info("curl_cffi patch applied — Cloudflare bypass + v3 measurements API + local fallback")
    return True


# Apply on import
_patch_applied = apply_curl_cffi_patch()
