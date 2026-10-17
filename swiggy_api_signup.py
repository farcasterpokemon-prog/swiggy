import argparse
import base64
import gzip
import json
import os
import queue
import re
import ssl
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Dict

if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
if hasattr(sys.stderr, "reconfigure"):
    try:
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import swiggy_signup as ss

LOG_HOOK = None
INSUFFICIENT_FUNDS_LOCK = threading.Lock()
INSUFFICIENT_PAUSE_UNTIL = 0.0
CHECKER_BUSY_LOCK = threading.Lock()
CHECKER_BUSY_UNTIL = 0.0


def notify(msg):
    if LOG_HOOK:
        try:
            LOG_HOOK(str(msg))
        except Exception:
            pass


def slog(msg):
    ss.log(msg)
    if LOG_HOOK and LOG_HOOK != ss.LOG_HOOK:
        notify(msg)


PAID_FAILURES_LOCK = threading.Lock()
CONSECUTIVE_PAID_FAILURES = 0
CIRCUIT_BREAKER_TRIGGERED = False


def get_circuit_breaker_status():
    with PAID_FAILURES_LOCK:
        return CIRCUIT_BREAKER_TRIGGERED, CONSECUTIVE_PAID_FAILURES


def reset_circuit_breaker():
    global CONSECUTIVE_PAID_FAILURES, CIRCUIT_BREAKER_TRIGGERED
    with PAID_FAILURES_LOCK:
        CONSECUTIVE_PAID_FAILURES = 0
        CIRCUIT_BREAKER_TRIGGERED = False


def record_paid_success(phone=""):
    global CONSECUTIVE_PAID_FAILURES
    with PAID_FAILURES_LOCK:
        if CONSECUTIVE_PAID_FAILURES > 0:
            slog(f"✅ [CIRCUIT BREAKER RESET] Account created successfully for {phone}! Failure counter reset from {CONSECUTIVE_PAID_FAILURES} to 0.")
        CONSECUTIVE_PAID_FAILURES = 0


def record_paid_failure(phone, reason, cfg=None):
    global CONSECUTIVE_PAID_FAILURES, CIRCUIT_BREAKER_TRIGGERED
    threshold = 4
    if cfg and isinstance(cfg, dict):
        threshold = int((cfg.get("signup") or {}).get("max_consecutive_paid_failures", 4))
    with PAID_FAILURES_LOCK:
        CONSECUTIVE_PAID_FAILURES += 1
        curr = CONSECUTIVE_PAID_FAILURES
        slog(f"⚠️ [CIRCUIT BREAKER] Paid OTP failure #{curr}/{threshold} on {phone}: {reason}")
        if curr >= threshold:
            CIRCUIT_BREAKER_TRIGGERED = True
            slog(f"🚨 [CIRCUIT BREAKER TRIGGERED] {curr} consecutive accounts failed after paid OTP ($0.{curr * 10:02d} deducted). Stopping run immediately to protect balance!")
            ss.trigger_cancel()
            return True
        return False


BASE = "https://profile.swiggy.com"
CTX = ssl.create_default_context()
CTX.check_hostname = False
CTX.verify_mode = ssl.CERT_NONE
UNVERIFIED_CTX = CTX


def get_proxy_url(cfg=None):
    """Retrieves configured proxy URL if enabled, including active rotation session."""
    return ss.get_proxy_url(cfg)


def get_opener(proxy_url=None):
    """Builds a urllib opener with optional proxy and unverified SSL context."""
    return ss.get_opener(proxy_url)


def get_current_ip(proxy_url=None):
    """Checks the public IP address routed through the active proxy."""
    return ss.get_current_ip(proxy_url)

CLEAN_CLONING = {
    "appFilesDirPathInvalid": 0,
    "developerModeEnabled": 0,
    "deviceModelVmos": 0,
    "emulatorStatus": 0,
    "packageName": "in.swiggy.android",
    "workProfileEnabled": 0,
}


def _read(resp):
    raw = resp.read()
    if resp.headers.get("Content-Encoding") == "gzip":
        try:
            raw = gzip.decompress(raw)
        except Exception:
            pass
    return raw


def call(method, path, body=None, headers=None, timeout=30, proxy_url=None):
    url = BASE + path

    # Attempt 1: via persistent requests.Session with active proxy
    for attempt in range(2):
        sess = ss.get_checker_session()
        if sess is not None:
            try:
                data_bytes = json.dumps(body).encode("utf-8") if body is not None else None
                h = dict(headers or {})
                if body is not None and "Content-Type" not in h and "content-type" not in h:
                    h["Content-Type"] = "application/json; charset=utf-8"
                resp = sess.request(method.upper(), url, data=data_bytes, headers=h, timeout=timeout)
                # If rate-limited or blocked, rotate proxy IP once and retry
                if resp.status_code in (403, 429) and attempt == 0:
                    ss.rotate_proxy_ip()
                    time.sleep(1.0)
                    continue
                return resp.status_code, resp.content
            except Exception:
                if attempt == 0:
                    ss.rotate_proxy_ip()
                    time.sleep(0.5)
                    continue

    # Attempt 2: via urllib with explicit proxy opener (never unproxied)
    p_url = proxy_url if proxy_url is not None else get_proxy_url()
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, headers=headers or {}, method=method)
    if p_url:
        try:
            opener = get_opener(p_url)
            with opener.open(req, timeout=timeout) as resp:
                return resp.status, _read(resp)
        except urllib.error.HTTPError as he:
            return he.code, _read(he)
        except Exception as e:
            return 0, str(e).encode()

    return 0, b'{"error": "no_proxy_configured"}'


def base_headers(swuid=None, tid="", sid=""):
    if swuid is None:
        swuid = uuid.uuid4().hex[:16]
    return {
        "pl-version": "140",
        "user-agent": "Swiggy-Android",
        "content-type": "application/json; charset=utf-8",
        "tid": str(tid or ""),
        "sid": str(sid or ""),
        "version-code": "1807",
        "app-version": "4.114.2",
        "latitude": "0.0",
        "longitude": "0.0",
        "os-version": "7.1.2",
        "accessibility_enabled": "false",
        "swuid": swuid,
        "deviceid": swuid,
        "x-network-quality": "GOOD",
        "faw-flags": "1354",
        "accept-encoding": "gzip",
        "accept": "application/json; charset=utf-8",
    }


def send_otp(phone, swuid=None):
    path = "/api/v3/app/sms_otp?" + urllib.parse.urlencode({"mobile": str(phone)})
    code, raw = call("GET", path, headers=base_headers(swuid))
    try:
        data = json.loads(raw.decode("utf-8", "replace"))
    except Exception:
        data = {"_raw": raw[:500].decode("utf-8", "replace")}
    return code, data


def verify_otp(otp, tid, sid, swuid=None):
    body = {"cloningSignalsData": CLEAN_CLONING, "otp": str(otp).strip()}
    path = "/api/v3/app/login/verify?" + urllib.parse.urlencode({"otp_source": "Sms-automatic"})
    h = base_headers(swuid, tid=tid, sid=sid)
    h["manufacturer"] = "XIAOMI"
    h["model-name"] = "REDMI 4A"
    code, raw = call("POST", path, body=body, headers=h)
    try:
        data = json.loads(raw.decode("utf-8", "replace"))
    except Exception:
        data = {"_raw": raw[:500].decode("utf-8", "replace")}
    return code, data


def signup(phone, name, tid, sid, swuid=None):
    body = {
        "cloningSignalsData": CLEAN_CLONING,
        "signUp": {"email": "", "mobile": str(phone), "name": name or "Swiggy User"},
    }
    path = "/api/v3/app/signup"
    h = base_headers(swuid, tid=tid, sid=sid)
    h["manufacturer"] = "XIAOMI"
    h["model-name"] = "REDMI 4A"
    code, raw = call("POST", path, body=body, headers=h)
    try:
        data = json.loads(raw.decode("utf-8", "replace"))
    except Exception:
        data = {"_raw": raw[:500].decode("utf-8", "replace")}
    return code, data


def decode_jwt_payload(jwt_str: str) -> Dict[str, Any]:
    """Safely decodes and extracts JSON payload from a JWT token without external libraries."""
    if not jwt_str or "." not in str(jwt_str):
        return {}
    parts = str(jwt_str).strip().split(".")
    if len(parts) < 2:
        return {}
    payload_b64 = parts[1]
    rem = len(payload_b64) % 4
    if rem > 0:
        payload_b64 += "=" * (4 - rem)
    try:
        raw = base64.urlsafe_b64decode(payload_b64)
        return json.loads(raw.decode("utf-8", "ignore"))
    except Exception:
        return {}


def extract_customer_id_from_any(data: Any, tid: str = "", token: str = "") -> str:
    """
    Exhaustive customerId extraction using multiple redundant strategies:
    1. Direct key search in dictionary.
    2. Common nested sub-objects (data, user, userDetails, profile, customer, account, session).
    3. Recursive traversal across all keys.
    4. JWT payload decoding of `tid` token (user_id / customerId).
    5. JWT payload decoding of `token` (user_id / customerId).
    """
    if not data and not tid and not token:
        return ""

    # Strategy 1: Direct key search
    if isinstance(data, dict):
        for k in ["customerId", "customer_id", "userId", "user_id", "cid"]:
            val = data.get(k)
            if val and str(val).strip() not in ["", "None", "null", "?", "0"]:
                return str(val).strip()

        # Strategy 2: Common nested objects
        for parent_k in ["data", "user", "userDetails", "profile", "customer", "account", "session", "response"]:
            nested = data.get(parent_k)
            if isinstance(nested, dict):
                cid = extract_customer_id_from_any(nested)
                if cid:
                    return cid

        # Strategy 3: Deep recursive search in dict
        for k, v in data.items():
            if isinstance(v, (dict, list)):
                cid = extract_customer_id_from_any(v)
                if cid:
                    return cid

    elif isinstance(data, list):
        for item in data:
            cid = extract_customer_id_from_any(item)
            if cid:
                return cid

    # Strategy 4: Decode JWT from tid
    target_tid = str(tid or (data.get("tid") if isinstance(data, dict) else "") or "")
    if target_tid:
        jwt_data = decode_jwt_payload(target_tid)
        for k in ["user_id", "customerId", "customer_id", "userId", "cid"]:
            val = jwt_data.get(k)
            if val and str(val).strip() not in ["", "None", "null", "?", "0"]:
                return str(val).strip()
        sub = jwt_data.get("sub")
        if sub and str(sub).strip() not in ["", "None", "null", "?"] and not ("-" in str(sub) and len(str(sub)) > 25):
            return str(sub).strip()

    # Strategy 5: Decode JWT from token
    target_token = str(token or (data.get("token") if isinstance(data, dict) else "") or "")
    if target_token and "." in target_token:
        jwt_data = decode_jwt_payload(target_token)
        for k in ["user_id", "customerId", "customer_id", "userId", "sub", "cid"]:
            val = jwt_data.get(k)
            if val and str(val).strip() not in ["", "None", "null", "?", "0"]:
                return str(val).strip()

    return ""


def fetch_profile_customer_id(tid: str, sid: str, swuid: str = "") -> str:
    """
    Live profile query fallback to Swiggy API to fetch the verified customerId
    if it was missing from the signup response body.
    """
    try:
        h = base_headers(swuid, tid=tid, sid=sid)
        endpoints = [
            "/api/v4/user/profile?silentSession=true&optionalKeys=IS_SUPER,SUPER_DETAILS,SWIGGY_PAY",
            "/api/v3/app/profile",
            "/api/v2/app/user/profile",
            "/api/v1/user/profile",
        ]
        for ep in endpoints:
            code, raw = call("GET", ep, headers=h, timeout=10)
            if code == 200:
                try:
                    prof_data = json.loads(raw.decode("utf-8", "ignore"))
                    cid = extract_customer_id_from_any(prof_data, tid=tid)
                    if cid:
                        slog(f"Profile API fallback retrieved customerId: {cid}")
                        return cid
                except Exception:
                    pass
    except Exception as e:
        slog(f"fetch_profile_customer_id error: {e}")
    return ""


def sign_in_with_tid(tid: str, sid: str, swuid: str):
    """
    Exchanges the short-lived tid JWT with Swiggy's Disc Auth endpoint
    for the persistent long-term access_token + refresh_token.
    """
    if not tid:
        return 0, {}
    path = "/v1/accounts/signInWithTID"
    h = {
        "host": "disc.swiggy.com",
        "pl-version": "140",
        "user-agent": "Swiggy-Android",
        "content-type": "application/json; charset=utf-8",
        "tid": str(tid or ""),
        "sid": str(sid or ""),
        "version-code": "1807",
        "app-version": "4.114.2",
        "latitude": "0.0",
        "longitude": "0.0",
        "os-version": "7.1.2",
        "accessibility_enabled": "false",
        "swuid": str(swuid or ""),
        "deviceid": str(swuid or ""),
        "x-network-quality": "GOOD",
        "accept-encoding": "gzip",
        "accept": "application/json; charset=utf-8",
    }
    body = {"tid": str(tid or ""), "token": "", "user_id": ""}
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request("https://disc.swiggy.com" + path, data=data, headers=h, method="POST")
    opener = get_opener()
    try:
        with opener.open(req, timeout=20) as resp:
            raw = _read(resp)
            return resp.status, json.loads(raw.decode("utf-8", "replace"))
    except urllib.error.HTTPError as e:
        raw = _read(e)
        try:
            return e.code, json.loads(raw.decode("utf-8", "replace"))
        except Exception:
            return e.code, {}
    except Exception as e:
        return 0, {"error": str(e)}


def extract_account_dict(data, phone, tid="", sid="", swuid="", name=""):
    """Safely extracts all tokens and user credentials from Swiggy API response with persistent session tokens."""
    sess = data.get("data") or {}
    if not isinstance(sess, dict):
        sess = {}

    res_tid = str(data.get("tid") or sess.get("tid") or tid or "")
    res_sid = str(data.get("sid") or sess.get("sid") or sid or "")
    res_device = str(data.get("deviceId") or sess.get("deviceId") or swuid or "")

    # Multi-layered customerId extraction
    cust_id = extract_customer_id_from_any(data, tid=res_tid)
    if not cust_id and res_tid:
        cust_id = fetch_profile_customer_id(res_tid, res_sid, res_device)

    actual_name = (
        sess.get("userName")
        or sess.get("name")
        or (data.get("data") or {}).get("name")
        or (data.get("data") or {}).get("userName")
        or name
        or (ss.random_name() if hasattr(ss, "random_name") else "Swiggy User")
    )

    token = (
        sess.get("token")
        or sess.get("accessToken")
        or sess.get("access_token")
        or data.get("token")
        or ""
    )

    mob = str(sess.get("phoneNumber") or sess.get("mobile") or phone or "").strip()

    acct_dict = {
        "token": token,
        "tid": res_tid,
        "sid": res_sid,
        "deviceId": res_device,
        "customerId": cust_id,
        "mobile": mob,
        "phoneNumber": mob,
        "name": actual_name,
        "userName": actual_name,
        "is_new_user": True,
        "created_at": int(time.time()),
    }

    return acct_dict


def verify_session_live(acct):
    """Directly verifies that the generated account session is 100% active on Swiggy Profile API."""
    if not acct or not acct.get("tid"):
        return False, acct
    if not acct.get("token"):
        acct["token"] = acct.get("tid", "")
    headers = {
        "User-Agent": "Swiggy-Android",
        "Content-Type": "application/json; charset=utf-8",
        "Accept": "application/json; charset=utf-8",
        "tid": str(acct.get("tid", "")),
        "sid": str(acct.get("sid", "")),
        "token": str(acct.get("token", "")),
        "deviceid": str(acct.get("deviceId", "")),
        "swuid": str(acct.get("deviceId", "")),
        "version-code": "1756",
        "app-version": "4.109.1",
        "latitude": "25.3176",
        "longitude": "82.9739",
        "current-latitude": "25.3176",
        "current-longitude": "82.9739",
        "os-version": "15",
        "accessibility_enabled": "false",
        "x-network-quality": "GOOD",
        "cache-control": "no-store",
        "Accept-Encoding": "gzip",
        "faw-flags": "1354",
        "pl-version": "134",
        "manufacturer": "GOOGLE",
        "model-name": "PIXEL 9A",
    }
    params = {"silentSession": "true", "optionalKeys": "IS_SUPER,SUPER_DETAILS,SWIGGY_PAY"}
    try:
        url = "https://profile.swiggy.com/api/v4/user/profile?" + urllib.parse.urlencode(params)
        req = urllib.request.Request(url, headers=headers, method="GET")
        opener = get_opener()
        with opener.open(req, timeout=12) as resp:
            raw = _read(resp)
            data = json.loads(raw.decode("utf-8", "replace"))
            if resp.status == 200 and data.get("statusCode") == 0:
                if data.get("tid"):
                    acct["tid"] = data["tid"]
                sess = data.get("data") or {}
                if sess.get("name"):
                    acct["name"] = sess["name"]
                    acct["userName"] = sess["name"]
                if sess.get("token"):
                    acct["token"] = sess["token"]
                if sess.get("customer_id") or sess.get("customerId"):
                    acct["customerId"] = str(sess.get("customer_id") or sess.get("customerId"))
                if sess.get("mobile") or sess.get("phoneNumber"):
                    mob = str(sess.get("mobile") or sess.get("phoneNumber"))
                    acct["mobile"] = mob
                    acct["phoneNumber"] = mob
                return True, acct
            return False, acct
    except Exception as e:
        slog("verify_session_live notice: %s" % e)
        return False, acct


def create_api_account(cfg, phone=None, order_id=None, name=None):
    if ss.is_cancelled():
        slog("[!] Cancelled before account creation started.")
        return None

    if cfg.get("signup", {}).get("auto_random", True) and not name:
        name = ss.random_name()
    provider = ss.make_provider(cfg.get("otp_provider"))
    op = cfg.get("otp_provider") or {}

    if phone is None:
        phone, order_id, swuid, tid, sid = None, None, None, "", ""
        attempt = 0
        while not ss.is_cancelled():
            attempt += 1
            try:
                p, o = provider.get_number()
                rent_time = time.time()
                ss.register_active_order(o, p, getattr(provider, "cfg", {}).get("type", "nexnum"), rent_time)
            except Exception as e:
                err_str = str(e)
                is_no_balance = any(k in err_str.upper() for k in ["NO_BALANCE", "INSUFFICIENT", "NOT ENOUGH", "NO_MONEY", "LOW_BALANCE", "ZERO_BALANCE"])
                if is_no_balance:
                    active_count = len(ss.load_active_orders())
                    if active_count > 0:
                        ss.log("Balance temporarily reserved by in-flight orders; retrying in 2s...")
                        if ss.cancel_sleep(2.0):
                            return None
                        continue
                    global INSUFFICIENT_PAUSE_UNTIL
                    with INSUFFICIENT_FUNDS_LOCK:
                        now = time.time()
                        # Only broadcast the message once per 15 seconds
                        if now >= INSUFFICIENT_PAUSE_UNTIL:
                            INSUFFICIENT_PAUSE_UNTIL = now + 15.0
                            slog("⏳ [Insufficient Funds] Provider balance is empty or insufficient. Pausing for 15s before retrying... (Please recharge or use /setkey)")
                        pause_sec = max(1.0, INSUFFICIENT_PAUSE_UNTIL - time.time())

                    ss.log("Worker pausing %ds due to insufficient provider balance..." % int(pause_sec))
                    if ss.cancel_sleep(pause_sec):
                        return None

                    # After 1 min pause, re-check provider balance
                    try:
                        new_bal = ss.get_provider_balance(op)
                        val = float(re.sub(r"[^\d.]", "", str(new_bal)) or 0)
                        if val >= 0.08:
                            with INSUFFICIENT_FUNDS_LOCK:
                                if INSUFFICIENT_PAUSE_UNTIL > 0:
                                    INSUFFICIENT_PAUSE_UNTIL = 0.0
                                    slog("✨ [Balance Recharged] Provider balance is now %s! Resuming account creation..." % new_bal)
                    except Exception:
                        pass
                    continue
                elif "NO_NUMBERS" in err_str:
                    ss.log("⚠️ Notice [attempt #%d]: No numbers in stock at max price. Retrying in 3s (use /setprice to increase)..." % attempt)
                else:
                    ss.log("⚠️ Rent attempt #%d notice: %s" % (attempt, err_str[:120]))
                if ss.cancel_sleep(3):
                    return None
                continue

            p = str(p).strip()
            slog("📱 Bought number %s (Order #%s) [Attempt #%d]" % (p, o, attempt))

            if ss.is_cancelled():
                ss.cancel_async(provider, o, rent_time=rent_time)
                return None

            # Pre-Check: Only fresh/unregistered numbers proceed to OTP request
            precheck_on = (op.get("precheck_enabled", True) if isinstance(op, dict) else True)
            if precheck_on:
                try:
                    slog("🔍 Checking if %s is a fresh unregistered number on Swiggy..." % p)
                    registered, resp = ss.check_swiggy_registered(p, cfg)
                    status_str = str(resp.get("status", "unknown")).lower().strip()
                except Exception as e:
                    ss.log("pre-checker notice for %s: %s" % (p, e))
                    registered = False
                    status_str = "checker_fallback"

                if registered or status_str == "registered":
                    slog("🚫 [Already Registered] %s was previously used on Swiggy. Cancelling order %s for 100%% refund & renting fresh number..." % (p, o))
                    ss.cancel_async(provider, o, rent_time=rent_time)
                    continue
                elif status_str == "not_registered":
                    slog("✨ [100%% Fresh Number Confirmed] %s is brand new!" % p)
                else:
                    # Status is unknown or checker busy: pause 1 minute and rotate IP using proxy
                    global CHECKER_BUSY_LOCK, CHECKER_BUSY_UNTIL
                    now = time.time()
                    should_rotate = False
                    with CHECKER_BUSY_LOCK:
                        if now >= CHECKER_BUSY_UNTIL:
                            CHECKER_BUSY_UNTIL = now + 60.0
                            should_rotate = True

                    if should_rotate:
                        slog("⏳ [Checker Busy / Status Unknown] Rate-limit detected on current IP. Pausing for 60s and rotating proxy to fresh Indian IP...")
                        new_ip, city, country = ss.rotate_proxy_ip(cfg)
                    else:
                        ss.log("[%s] Checker rate-limited; waiting for active 60s proxy cooldown window..." % p)

                    pause_sec = max(1.0, CHECKER_BUSY_UNTIL - time.time())
                    if ss.cancel_sleep(pause_sec):
                        ss.cancel_async(provider, o, rent_time=rent_time)
                        return None

                    if should_rotate:
                        slog("🔄 [Proxy IP Rotated] Resumed after 60s pause! New Routed IP: %s (%s, %s). Re-checking number %s..." % (new_ip, city, country, p))
                    else:
                        slog("🔄 [Checker Resumed] Re-checking number %s with fresh proxy connection..." % p)

                    # Re-check the number with the new IP
                    try:
                        registered, resp = ss.check_swiggy_registered(p, cfg)
                        status_str = str(resp.get("status", "unknown")).lower().strip()
                    except Exception:
                        status_str = "unknown"

                    if registered or status_str == "registered":
                        slog("🚫 [Already Registered] %s was previously used on Swiggy. Cancelling order %s for 100%% refund & renting fresh number..." % (p, o))
                        ss.cancel_async(provider, o, rent_time=rent_time)
                        continue
                    elif status_str == "not_registered":
                        slog("✨ [100%% Fresh Number Confirmed (after IP rotation)] %s is brand new!" % p)
                    else:
                        slog("🚫 [Pre-Check Unresolved] %s status is still '%s' after proxy rotation. Cancelling order %s for 100%% refund & buying next..." % (p, status_str, o))
                        ss.cancel_async(provider, o, rent_time=rent_time)
                        continue

            if ss.is_cancelled():
                ss.cancel_async(provider, o, rent_time=rent_time)
                return None

            ss.log("[%s] Requesting Swiggy OTP..." % p)
            sw = uuid.uuid4().hex[:16]
            code, data = send_otp(p, sw)
            if code != 200 or data.get("statusCode") != 0:
                ss.log("[%s] sms_otp failed (HTTP %d): %s -> Cancelling in background & buying next..." % (p, code, str(data)[:120]))
                ss.cancel_async(provider, o, rent_time=rent_time)
                continue

            tid0 = data.get("tid", "")
            sid0 = data.get("sid", "")
            max_wait_sec = int(op.get("max_wait_sec", 120))
            ss.log("[%s] OTP requested successfully. Waiting up to %ds for SMS..." % (p, max_wait_sec))

            otp, _s, _r = ss.get_otp(None, provider, op, p, o, cfg["signup"])
            if not otp:
                if ss.is_cancelled():
                    ss.log("[%s] Cancelled during OTP wait -> cancelling order %s" % (p, o))
                else:
                    ss.log("[%s] ⏰ OTP timeout -> Cancelling order %s in background & buying next number..." % (p, o))
                ss.cancel_async(provider, o, rent_time=rent_time)
                if ss.is_cancelled():
                    return None
                continue

            if ss.is_cancelled():
                ss.cancel_async(provider, o, rent_time=rent_time)
                return None

            ss.log("[%s] 🔥 OTP RECEIVED: %s" % (p, otp))
            code, data = verify_otp(otp, tid0, sid0, sw)
            status_code = data.get("statusCode")
            status_msg = data.get("statusMessage") or data.get("message") or data.get("error") or ""
            ss.log("[%s] verify response -> HTTP %d status=%s msg='%s'" % (p, code, status_code, status_msg))

            is_verify_ok = (code in [200, 201]) and (status_code in [0, "0"] or (status_code is None and bool(data.get("tid"))))
            if is_verify_ok:
                sess_data = data.get("data") or {}
                is_registered = bool(sess_data.get("registered", False))
                tid1 = data.get("tid") or (data.get("data") or {}).get("tid") or tid0
                sid1 = data.get("sid") or (data.get("data") or {}).get("sid") or sid0

                phone, order_id, swuid, tid, sid = p, o, sw, tid1, sid1
                verify_data = data
                if is_registered:
                    slog("ℹ️ [%s] Swiggy verified account (Registered User). Finalizing & saving account to prevent wasting paid OTP..." % p)
                else:
                    slog("✨ [FRESH NUMBER CONFIRMED] %s is a BRAND NEW user (registered=False)! Finalizing signup..." % p)
                break
            else:
                ss.log("⚠️ [%s] Swiggy rejected OTP (HTTP %d status=%s: '%s') -> Cancelling order %s in background & buying next..." % (p, code, status_code, status_msg or str(data)[:100], o))
                ss.cancel_async(provider, o, rent_time=rent_time)
                record_paid_failure(p, f"Swiggy rejected OTP (HTTP {code} status={status_code})", cfg)
                continue

        if phone is None or ss.is_cancelled():
            return None

        ss.log("proceeding to finalize account for %s (order %s)" % (phone, order_id))
    else:
        phone = str(phone).strip()
        ss.log("sending OTP for override number %s" % phone)
        swuid = uuid.uuid4().hex[:16]
        code, data = send_otp(phone, swuid)
        if code != 200 or data.get("statusCode") != 0:
            ss.log("sms_otp error: HTTP %d %s" % (code, data))
            if order_id and provider:
                ss.cancel_async(provider, order_id)
            return None
        tid = data.get("tid", "")
        sid = data.get("sid", "")

        otp, _src, _raw = ss.get_otp(None, provider, op, phone, order_id, cfg["signup"])
        if not otp:
            ss.log("no OTP received for %s" % phone)
            if order_id and provider:
                ss.cancel_async(provider, order_id)
            return None
        ss.log("OTP obtained: %s" % otp)

        code, data = verify_otp(otp, tid, sid, swuid)
        if code != 200 or data.get("statusCode") != 0:
            ss.log("verify failed: HTTP %d %s" % (code, data))
            if order_id and provider:
                ss.cancel_async(provider, order_id)
            return None

        sess_data = data.get("data") or {}
        is_registered = bool(sess_data.get("registered", False))
        if is_registered:
            ss.log("⚠️ Override number %s is already registered." % phone)
        tid = data.get("tid") or (data.get("data") or {}).get("tid") or tid
        sid = data.get("sid") or (data.get("data") or {}).get("sid") or sid
        verify_data = data

    final_data = verify_data
    if not is_registered:
        ss.log("[%s] Submitting name '%s' for brand new account signup..." % (phone, name))
        signup_ok = False
        for s_attempt in range(1, 3):
            try:
                code, reg_data = signup(phone, name, tid, sid, swuid)
                status_code = reg_data.get("statusCode")
                msg = reg_data.get("statusMessage") or reg_data.get("_raw", "")[:120]
                ss.log("[%s] signup attempt %d -> HTTP %d status=%s msg='%s'" % (phone, s_attempt, code, status_code, msg))
                if code in (200, 201) and status_code in (0, "0"):
                    final_data = reg_data
                    tid = str(reg_data.get("tid") or (reg_data.get("data") or {}).get("tid") or tid)
                    sid = str(reg_data.get("sid") or (reg_data.get("data") or {}).get("sid") or sid)
                    signup_ok = True
                    break
                else:
                    ss.log("[%s] signup attempt %d rejected (%s). Rotating proxy & retrying..." % (phone, s_attempt, msg))
                    ss.rotate_proxy_ip(cfg)
                    time.sleep(1.0)
            except Exception as e:
                ss.log("[%s] signup attempt %d error: %s" % (phone, s_attempt, e))
                ss.rotate_proxy_ip(cfg)
                time.sleep(1.0)

        if not signup_ok:
            slog("❌ [%s] Swiggy brand-new user signup failed. Refusing to create invalid account without customerId." % phone)
            if order_id and provider:
                ss.cancel_async(provider, order_id, rent_time=rent_time)
            record_paid_failure(phone, "Swiggy brand-new user signup failed", cfg)
            return None

    # Step 2: Extract account credentials
    account = extract_account_dict(final_data, phone, tid, sid, swuid, name=name)
    account["is_new_user"] = not is_registered
    account["account_type"] = "OLD" if is_registered else "FRESH"

    # Multi-layered customerId extraction & validation
    cust_id = str(account.get("customerId", "")).strip()
    if not cust_id or cust_id in ["0", "?", "None", "null"]:
        cust_id = extract_customer_id_from_any(final_data, tid=tid)
    if not cust_id or cust_id in ["0", "?", "None", "null"]:
        cust_id = fetch_profile_customer_id(tid, sid, swuid)
    account["customerId"] = cust_id

    # Token extraction & validation (never use fake dummy tokens)
    real_token = (
        (final_data.get("data") or {}).get("token")
        or (verify_data.get("data") or {}).get("token")
        or (final_data.get("data") or {}).get("accessToken")
        or account.get("token")
    )
    if not real_token or str(real_token).startswith("jwt_swiggy_"):
        if tid and len(tid) > 30 and "." in tid:
            real_token = tid
        else:
            real_token = ""
    account["token"] = real_token

    # Verify session is 100% active on Swiggy Profile API
    verified_ok = False
    try:
        is_live, enriched_account = verify_session_live(account)
        if is_live and enriched_account:
            account = enriched_account
            verified_ok = True
        else:
            ss.rotate_proxy_ip(cfg)
            is_live, enriched_account = verify_session_live(account)
            if is_live and enriched_account:
                account = enriched_account
                verified_ok = True
    except Exception as e:
        slog("verify_session_live error: %s" % e)

    # FINAL QUALITY GATE: Reject any account lacking valid customerId, token, or live session
    final_cid = str(account.get("customerId", "")).strip()
    if not final_cid or final_cid in ["0", "?", "None", "null"] or not account.get("token") or not verified_ok:
        slog("❌ [%s] Account rejected by quality gate: customerId='%s', token='%s', is_live=%s. Refusing to deliver invalid login." % (
            phone, final_cid, "VALID" if account.get("token") else "EMPTY", verified_ok
        ))
        if order_id and provider:
            ss.cancel_async(provider, order_id, rent_time=rent_time)
        record_paid_failure(phone, f"Quality gate rejected (customerId='{final_cid}', verified={verified_ok})", cfg)
        return None

    # Mark provider activation complete (Status 6) ONLY AFTER verification passes
    if order_id and provider and hasattr(provider, "set_status"):
        try:
            provider.set_status(order_id, 6)
            ss.log("activation marked complete on provider for %s" % phone)
        except Exception as e:
            ss.log("provider completion notice: %s" % e)
        ss.unregister_active_order(order_id)

    saved = ss.save_account(account)
    if not saved:
        slog("❌ [%s] Failed to save account to database." % phone)
        record_paid_failure(phone, "Database save failed", cfg)
        return None

    slog("🎉 SUCCESS: Account Created & Verified [%s]: %s (customerId: %s)" % (
        account.get("account_type", "FRESH"), account["mobile"], account["customerId"]
    ))
    record_paid_success(phone)
    return account


def create_pipeline_batch(count: int, cfg: dict, on_account_created=None, is_cancelled=None):
    """
    High-Speed Asynchronous Parallel Pipeline Engine:
    Stage 1: Parallel Number Hunting & Pre-Checking Swarm
    Stage 2: Instant Parallel Swiggy OTP Dispatcher
    Stage 3: Parallel High-Frequency OTP Poller (1.2s interval)
    Stage 4: Instant Account Verification, Session Enrichment & Delivery
    """
    if count <= 0:
        return []

    provider = ss.make_provider(cfg.get("otp_provider"))
    if not provider:
        slog("❌ OTP Provider not configured.")
        return []

    op = cfg.get("otp_provider") or {}
    provider_type = getattr(provider, "cfg", {}).get("type", "nexnum")
    max_wait_sec = float(op.get("max_wait_sec", 120))
    poll_interval = float(op.get("poll_interval_sec", 1.2))
    resend_after_sec = float(op.get("resend_after_sec", 999))

    cfg_workers = cfg.get("workers") or op.get("workers") or 10
    total_workers = min(max(int(cfg_workers), 1), 100)
    hunter_concurrency = min(total_workers, max(count, 1), 10)

    stop_event = threading.Event()
    fresh_queue = queue.Queue(maxsize=100)
    active_orders = {}
    active_lock = threading.Lock()
    hunting_state = {"in_progress": 0}
    created_accounts = []
    created_lock = threading.Lock()

    def is_stopped():
        if stop_event.is_set():
            return True
        if ss.is_cancelled():
            stop_event.set()
            return True
        if is_cancelled and is_cancelled():
            stop_event.set()
            return True
        with created_lock:
            fresh_count = sum(1 for a in created_accounts if a.get("is_new_user", True))
            if fresh_count >= count or len(created_accounts) >= count + 10:
                stop_event.set()
                return True
        return False

    def finalize_order(item, otp):
        p = item["phone"]
        o = item["order_id"]
        sw = item["swuid"]
        tid0 = item["tid"]
        sid0 = item["sid"]
        rent_time = item["rent_time"]

        slog("[%s] 🔥 OTP RECEIVED: %s" % (p, otp))
        code, data = verify_otp(otp, tid0, sid0, sw)
        status_code = data.get("statusCode")
        status_msg = data.get("statusMessage") or data.get("message") or data.get("error") or ""
        slog("[%s] verify response -> HTTP %d status=%s msg='%s'" % (p, code, status_code, status_msg))

        is_verify_ok = (code in [200, 201]) and (status_code in [0, "0"] or (status_code is None and bool(data.get("tid"))))
        if is_verify_ok:
            sess_data = data.get("data") or {}
            is_registered = bool(sess_data.get("registered", False))
            tid1 = data.get("tid") or (data.get("data") or {}).get("tid") or tid0
            sid1 = data.get("sid") or (data.get("data") or {}).get("sid") or sid0

            if is_registered:
                slog("ℹ️ [%s] Swiggy verified account (Registered User). Finalizing & saving account to prevent wasting paid OTP..." % p)
            else:
                slog("✨ [FRESH NUMBER CONFIRMED] %s is a BRAND NEW user (registered=False)! Finalizing signup..." % p)

            # Signup if fresh
            final_data = data
            name = ss.random_name() if cfg.get("signup", {}).get("auto_random", True) else (cfg.get("signup", {}).get("name") or "Swiggy User")
            if not is_registered:
                slog("[%s] Submitting name '%s' for fresh account signup..." % (p, name))
                signup_ok = False
                for s_attempt in range(1, 3):
                    try:
                        code_s, reg_data = signup(p, name, tid1, sid1, sw)
                        status_code_s = reg_data.get("statusCode")
                        if code_s in (200, 201) and status_code_s in (0, "0"):
                            final_data = reg_data
                            tid1 = str(reg_data.get("tid") or (reg_data.get("data") or {}).get("tid") or tid1)
                            sid1 = str(reg_data.get("sid") or (reg_data.get("data") or {}).get("sid") or sid1)
                            signup_ok = True
                            break
                        else:
                            ss.rotate_proxy_ip(cfg)
                            time.sleep(1.0)
                    except Exception as e:
                        ss.rotate_proxy_ip(cfg)
                        time.sleep(1.0)

                if not signup_ok:
                    slog("❌ [%s] Fresh user signup failed. Refusing to create invalid account." % p)
                    ss.cancel_async(provider, o, rent_time=rent_time)
                    record_paid_failure(p, "Fresh user signup failed", cfg)
                    return

            acct = extract_account_dict(final_data, p, tid1, sid1, sw, name=name)
            acct["is_new_user"] = not is_registered
            acct["account_type"] = "OLD" if is_registered else "FRESH"

            cust_id = str(acct.get("customerId", "")).strip()
            if not cust_id or cust_id in ["0", "?", "None", "null"]:
                cust_id = extract_customer_id_from_any(final_data, tid=tid1)
            if not cust_id or cust_id in ["0", "?", "None", "null"]:
                cust_id = fetch_profile_customer_id(tid1, sid1, sw)
            acct["customerId"] = cust_id

            real_token = (
                (final_data.get("data") or {}).get("token")
                or (data.get("data") or {}).get("token")
                or (final_data.get("data") or {}).get("accessToken")
                or acct.get("token")
            )
            if not real_token or str(real_token).startswith("jwt_swiggy_"):
                if tid1 and len(tid1) > 30 and "." in tid1:
                    real_token = tid1
                else:
                    real_token = ""
            acct["token"] = real_token

            verified_ok = False
            try:
                ok_live, live_acct = verify_session_live(acct)
                if ok_live and live_acct:
                    acct = live_acct
                    verified_ok = True
                else:
                    ss.rotate_proxy_ip(cfg)
                    ok_live, live_acct = verify_session_live(acct)
                    if ok_live and live_acct:
                        acct = live_acct
                        verified_ok = True
            except Exception as e:
                slog("live verify notice: %s" % e)

            final_cid = str(acct.get("customerId", "")).strip()
            if not final_cid or final_cid in ["0", "?", "None", "null"] or not acct.get("token") or not verified_ok:
                slog("❌ [%s] Account rejected by quality gate: customerId='%s', token='%s', is_live=%s. Refusing to deliver invalid login." % (
                    p, final_cid, "VALID" if acct.get("token") else "EMPTY", verified_ok
                ))
                ss.cancel_async(provider, o, rent_time=rent_time)
                record_paid_failure(p, f"Quality gate rejected (customerId='{final_cid}', verified={verified_ok})", cfg)
                return

            # Mark provider completed ONLY AFTER verification passes
            if hasattr(provider, "set_status"):
                try:
                    provider.set_status(o, 6)
                    slog("activation marked complete on provider for %s" % p)
                except Exception as e:
                    slog("provider completion notice: %s" % e)
            ss.unregister_active_order(o)

            saved = ss.save_account(acct)
            if not saved:
                record_paid_failure(p, "Database save failed", cfg)
                return

            slog("🎉 SUCCESS: Account Created & Verified [%s]: %s (customerId: %s)" % (acct.get("account_type", "FRESH"), acct["mobile"], acct["customerId"]))
            record_paid_success(acct.get("mobile", p))

            with created_lock:
                created_accounts.append(acct)
                fresh_count = sum(1 for a in created_accounts if a.get("is_new_user", True))
                if fresh_count >= count or len(created_accounts) >= count + 10:
                    stop_event.set()
                if on_account_created:
                    try:
                        on_account_created(acct, len(created_accounts), count)
                    except Exception as e:
                        slog("callback error: %s" % e)
        else:
            slog("⚠️ [%s] Swiggy rejected OTP (HTTP %d status=%s: '%s') -> Cancelling order in background..." % (p, code, status_code, status_msg or str(data)[:100]))
            ss.cancel_async(provider, o, rent_time=rent_time)
            record_paid_failure(p, f"Swiggy rejected OTP (HTTP {code} status={status_code})", cfg)

    # 1. Hunter Worker (Stage 1)
    def hunter_worker():
        while not is_stopped():
            with active_lock:
                with created_lock:
                    fresh_count = sum(1 for a in created_accounts if a.get("is_new_user", True))
                needed = max(1, count - fresh_count)
                target_max = min(max(needed * 2, 2), hunter_concurrency)
                in_flight = len(active_orders) + fresh_queue.qsize() + hunting_state["in_progress"]
                if in_flight >= target_max:
                    time.sleep(0.3)
                    continue
                hunting_state["in_progress"] += 1

            try:
                p, o = provider.get_number()
                rent_time = time.time()
                ss.register_active_order(o, p, provider_type, rent_time)
            except Exception as e:
                err_str = str(e)
                if "NO_BALANCE" in err_str.upper() or "INSUFFICIENT" in err_str.upper():
                    if ss.cancel_sleep(2.0):
                        break
                elif ss.cancel_sleep(1.5):
                    break
                continue
            finally:
                with active_lock:
                    hunting_state["in_progress"] = max(0, hunting_state["in_progress"] - 1)

            p = str(p).strip()
            slog("rented %s (order %s) [hunting]" % (p, o))

            if is_stopped():
                ss.cancel_async(provider, o, rent_time=rent_time)
                break

            # Pre-check
            precheck_on = (op.get("precheck_enabled", True) if isinstance(op, dict) else True)
            if precheck_on:
                try:
                    registered, resp = ss.check_swiggy_registered(p, cfg)
                    status_str = str(resp.get("status", "unknown")).lower().strip()
                except Exception as e:
                    registered = False
                    status_str = "error"

                if registered or status_str == "registered":
                    slog("🚫 [PRE-CHECK REJECT] %s is REGISTERED on Swiggy -> Cancelling order %s in background for refund" % (p, o))
                    ss.cancel_async(provider, o, rent_time=rent_time)
                    continue
                elif status_str == "not_registered":
                    slog("✨ [PRE-CHECK PASS] %s is UNREGISTERED (100%% Fresh) -> Queued for Instant OTP!" % p)
                else:
                    resolved = False
                    for retry_attempt in range(1, 3):
                        slog("⏳ [Checker Busy] %s status is '%s'; retrying (%d/2)..." % (p, status_str, retry_attempt))
                        time.sleep(1.2)
                        ss.reset_checker_session()
                        try:
                            registered, resp = ss.check_swiggy_registered(p, cfg)
                            status_str = str(resp.get("status", "unknown")).lower().strip()
                        except Exception:
                            pass
                        if registered or status_str == "registered":
                            slog("🚫 [PRE-CHECK REJECT] %s is REGISTERED on Swiggy -> Cancelling order %s for refund" % (p, o))
                            ss.cancel_async(provider, o, rent_time=rent_time)
                            resolved = True
                            break
                        elif status_str == "not_registered":
                            slog("✨ [PRE-CHECK PASS (on retry)] %s is UNREGISTERED -> Queued for Instant OTP!" % p)
                            resolved = True
                            break

                    if resolved and (registered or status_str == "registered"):
                        continue
                    if not resolved or status_str != "not_registered":
                        slog("🚫 [PRE-CHECK REJECT] %s status is '%s'. Cancelling order %s & pausing 3s..." % (p, status_str, o))
                        ss.cancel_async(provider, o, rent_time=rent_time)
                        time.sleep(3.0)
                        continue
            else:
                slog("✨ [PRE-CHECK SKIPPED] %s queued for Instant OTP!" % p)
            fresh_queue.put((p, o, rent_time))

    # 2. Dispatcher Worker (Stage 2)
    def dispatcher_worker():
        while not is_stopped():
            try:
                item = fresh_queue.get(timeout=0.5)
            except queue.Empty:
                continue

            p, o, rent_time = item
            if is_stopped():
                ss.cancel_async(provider, o, rent_time=rent_time)
                continue

            sw = uuid.uuid4().hex[:16]
            code, data = send_otp(p, sw)
            if code == 200 and data.get("statusCode") == 0:
                tid0 = data.get("tid", "")
                sid0 = data.get("sid", "")
                with active_lock:
                    active_orders[o] = {
                        "phone": p,
                        "order_id": o,
                        "tid": tid0,
                        "sid": sid0,
                        "swuid": sw,
                        "rent_time": rent_time,
                        "req_time": time.time(),
                        "resent": 0,
                        "next_resend": time.time() + resend_after_sec,
                    }
                slog("[%s] 🔥 Swiggy OTP Requested! Waiting for SMS in parallel..." % p)
            else:
                slog("[%s] Swiggy sms_otp error (HTTP %d): %s -> Cancelling order %s in background" % (p, code, str(data)[:100], o))
                ss.cancel_async(provider, o, rent_time=rent_time)

    # 3. Poller Worker (Stage 3 & 4)
    def poller_worker():
        with ThreadPoolExecutor(max_workers=30) as poll_pool:
            while not is_stopped():
                with active_lock:
                    current_items = list(active_orders.items())

                if not current_items:
                    time.sleep(0.3)
                    continue

                def check_one(order_tuple):
                    o, item = order_tuple
                    p = item["phone"]
                    now = time.time()
                    if now - item["req_time"] > max_wait_sec:
                        with active_lock:
                            active_orders.pop(o, None)
                        slog("[%s] ⏰ OTP timeout -> Cancelling order %s in background & getting refund" % (p, o))
                        ss.cancel_async(provider, o, rent_time=item["rent_time"])
                        return

                    try:
                        raw = provider.fetch_otp(o) or ""
                    except Exception as e:
                        raw = ""

                    otp = ss.extract_otp(raw)
                    if otp:
                        with active_lock:
                            active_orders.pop(o, None)
                        finalize_order(item, otp)
                    else:
                        if now >= item["next_resend"] and hasattr(provider, "set_status"):
                            item["resent"] += 1
                            item["next_resend"] = now + resend_after_sec
                            try:
                                provider.set_status(o, 3)
                                slog("[%s] requested OTP resend #%d" % (p, item["resent"]))
                            except Exception:
                                pass

                list(poll_pool.map(check_one, current_items))
                time.sleep(poll_interval)

    # Start all threads
    threads = []
    for _ in range(hunter_concurrency):
        t = threading.Thread(target=hunter_worker, daemon=True)
        t.start()
        threads.append(t)

    for _ in range(10):
        t = threading.Thread(target=dispatcher_worker, daemon=True)
        t.start()
        threads.append(t)

    poller_thread = threading.Thread(target=poller_worker, daemon=True)
    poller_thread.start()
    threads.append(poller_thread)

    # Wait until batch is fulfilled or stopped
    while not is_stopped():
        time.sleep(0.5)

    stop_event.set()
    time.sleep(1.0)

    # Cleanup & refund all remaining numbers
    with active_lock:
        remaining_active = list(active_orders.items())
        active_orders.clear()
    for o, item in remaining_active:
        ss.cancel_async(provider, o, rent_time=item["rent_time"])

    while not fresh_queue.empty():
        try:
            p, o, rent_time = fresh_queue.get_nowait()
            ss.cancel_async(provider, o, rent_time=rent_time)
        except Exception:
            break

    return created_accounts


def main():
    ap = argparse.ArgumentParser(description="Swiggy direct-API signup (no device needed)")
    ap.add_argument("--config", default=ss.CONFIG_PATH)
    ap.add_argument("--phone", help="override phone number")
    ap.add_argument("--otp", help="pre-provided OTP")
    ap.add_argument("--name", help="override account name")
    ap.add_argument("--count", type=int, default=1)
    args = ap.parse_args()

    cfg = ss.load_config(args.config)
    if args.phone:
        cfg["signup"]["phone"] = args.phone
    if args.otp:
        cfg["signup"]["otp"] = args.otp
    if args.name:
        cfg["signup"]["name"] = args.name

    for i in range(args.count):
        slog(f"=== Creating Swiggy Account {i+1}/{args.count} ===")
        acct = create_api_account(cfg)
        if acct:
            print("Created Fresh Account:", json.dumps(acct, indent=2))
        else:
            print("Failed to create account.")


if __name__ == "__main__":
    main()