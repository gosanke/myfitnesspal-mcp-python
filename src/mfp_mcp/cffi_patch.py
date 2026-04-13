"""
curl_cffi bootstrap for MFP MCP Server
Monkey-patches cloudscraper and myfitnesspal to use curl_cffi
which bypasses Cloudflare's TLS fingerprint detection.
"""
import json
import logging
from pathlib import Path
from http.cookiejar import CookieJar

logger = logging.getLogger("mfp_mcp.cffi_patch")

def apply_curl_cffi_patch():
    """Apply curl_cffi monkey-patch to bypass Cloudflare."""
    try:
        from curl_cffi import requests as cffi_requests
    except ImportError:
        logger.warning("curl_cffi not installed, skipping patch")
        return False
    
    # Load cookies from stored file
    cookies_file = Path.home() / ".mfp_mcp" / "cookies.json"
    cookies = {}
    if cookies_file.exists():
        try:
            cookie_data = json.loads(cookies_file.read_text())
            cookies = cookie_data.get("cookies", {})
            logger.info(f"Loaded {len(cookies)} cookies from {cookies_file}")
        except Exception as e:
            logger.warning(f"Failed to load cookies: {e}")
    
    # Create curl_cffi session with Chrome TLS impersonation
    cffi_session = cffi_requests.Session(impersonate="chrome")
    for name, value in cookies.items():
        cffi_session.cookies.set(name, value, domain="www.myfitnesspal.com")
    
    # Monkey-patch cloudscraper to return our curl_cffi session
    import cloudscraper
    cloudscraper._original_create_scraper = cloudscraper.create_scraper
    def patched_create_scraper(*args, **kwargs):
        return cffi_session
    cloudscraper.create_scraper = patched_create_scraper
    
    # Monkey-patch myfitnesspal.Client._get_user_metadata to handle 500 errors
    import myfitnesspal.client
    _original_get_user_metadata = myfitnesspal.client.Client._get_user_metadata
    def _patched_get_user_metadata(self):
        try:
            return _original_get_user_metadata(self)
        except (KeyError, Exception) as e:
            logger.warning(f"User metadata fetch failed ({e}), using fallback")
            return {"username": "patrickbjurquist", "unit_preferences": {"energy": {"unit": "calories"}}}
    myfitnesspal.client.Client._get_user_metadata = _patched_get_user_metadata
    
    logger.info("curl_cffi patch applied successfully - Cloudflare bypass active")
    return True

# Apply on import
_patch_applied = apply_curl_cffi_patch()
