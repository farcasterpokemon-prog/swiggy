import argparse
import base64
import gzip
import json
import os
import queue
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


BASE = "https://profile.swiggy.com"
CTX = ssl.create_default_context()
CTX.check_hostname = False
CTX.verify_mode = ssl.CERT_NONE
UNVERIFIED_CTX = CTX


def get_proxy_url(cfg=None):
    """Retrieves configured proxy URL if enabled."""
    if cfg is None:
        try:
            cfg = ss.load_config(ss.CONFIG_PATH)
        except Exception:
            cfg = {}
    p_cfg = cfg.get("proxy") or {}
    if p_cfg.get("enabled") and p_cfg.get("url"):
        return p_cfg.get("url").strip()
    return None


def get_opener(proxy_url=None):
    """Builds a urllib opener with optional proxy and unverified SSL context."""
    if proxy_url is None:
        proxy_url = get_proxy_url()
    handlers = [urllib.request.HTTPSHandler(context=UNVERIFIED_CTX)]
    if proxy_url:
        handlers.append(urllib.request.ProxyHandler({"http": proxy_url, "https": proxy_url}))
    return urllib.request.build_opener(*handlers)


def get_current_ip(proxy_url=None):
    """Checks the public IP address routed through the active proxy."""
    opener = get_opener(proxy_url)
    endpoints = [
        "https://api.ipify.org?format=json",
        "http://ip-api.com/json",
        "https://ifconfig.me/all.json",
    ]
    for url in endpoints:
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0", "Accept": "application/json"})
            with opener.open(req, timeout=12) as resp:
                raw = resp.read().decode("utf-8", "ignore")
                return json.loads(raw)
        except Exception:
            continue
    return {"ip": "unknown", "status": "failed"}

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
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, headers=headers or {}, method=method)
    opener = get_opener(proxy_url)
    try:
        with opener.open(req, timeout=timeout) as resp:
            return resp.status, _read(resp)
    except urllib.error.HTTPError as e:
        return e.code, _read(e)
    except Exception as e:
        return 0, str(e).encode()


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
                if "NO_NUMBERS" in err_str:
                    slog("⚠️ Notice [attempt #%d]: No numbers in stock at max price. Retrying in 3s (use /setprice to increase)..." % attempt)
                else:
                    slog("⚠️ Rent attempt #%d notice: %s" % (attempt, err_str[:120]))
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
            # Pre-Check: Only confirmed fresh/unregistered numbers proceed to OTP request
            precheck_on = (op.get("precheck_enabled", True) if isinstance(op, dict) else True)
            if precheck_on:
                try:
                    slog("🔍 Checking number %s on Swiggy..." % p)
                    registered, resp = ss.check_swiggy_registered(p, cfg)
                    status_str = str(resp.get("status", "unknown")).lower().strip()
                except Exception as e:
                    slog("pre-checker notice for %s: %s" % (p, e))
                    registered = True
                    status_str = "error"

                if registered or status_str != "not_registered":
                    slog("🚫 [PRE-CHECK REJECT] %s is REGISTERED / UNVERIFIED (status: '%s') -> Cancelling order %s for refund & buying next..." % (p, status_str, o))
                    ss.cancel_async(provider, o, rent_time=rent_time)
                    continue

                slog("✨ [PRE-CHECK PASS] %s is 100%% FRESH (Unregistered). Proceeding to Swiggy OTP..." % p)

            if ss.is_cancelled():
                ss.cancel_async(provider, o, rent_time=rent_time)
                return None

            slog("[%s] Requesting Swiggy OTP..." % p)
            sw = uuid.uuid4().hex[:16]
            code, data = send_otp(p, sw)
            if code != 200 or data.get("statusCode") != 0:
                slog("[%s] sms_otp failed (HTTP %d): %s -> Cancelling in background & buying next..." % (p, code, str(data)[:120]))
                ss.cancel_async(provider, o, rent_time=rent_time)
                continue

            tid0 = data.get("tid", "")
            sid0 = data.get("sid", "")
            slog("[%s] OTP requested successfully. Waiting up to 50s for SMS..." % p)

            otp, _s, _r = ss.get_otp(None, provider, op, p, o, cfg["signup"])
            if not otp:
                if ss.is_cancelled():
                    slog("[%s] Cancelled during OTP wait -> cancelling order %s" % (p, o))
                else:
                    slog("[%s] ⏰ OTP timeout -> Cancelling order %s in background & buying next number..." % (p, o))
                ss.cancel_async(provider, o, rent_time=rent_time)
                if ss.is_cancelled():
                    return None
                continue

            if ss.is_cancelled():
                ss.cancel_async(provider, o, rent_time=rent_time)
                return None

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

                # Strict Fresh Check: Discard old accounts if only_fresh is enabled
                only_fresh = (cfg.get("signup") or {}).get("only_fresh", True)
                if is_registered and only_fresh:
                    slog("🚫 [OLD ACCOUNT REJECTED] %s is ALREADY REGISTERED on Swiggy (registered=True)! Cancelling & buying a brand new number..." % p)
                    ss.cancel_async(provider, o, rent_time=rent_time)
                    continue

                phone, order_id, swuid, tid, sid = p, o, sw, tid1, sid1
                verify_data = data
                slog("✨ [FRESH NUMBER CONFIRMED] %s is a BRAND NEW user (registered=False)! Finalizing signup..." % p)
                break
            else:
                slog("⚠️ [%s] Swiggy rejected OTP (HTTP %d status=%s: '%s') -> Cancelling order %s in background & buying next..." % (p, code, status_code, status_msg or str(data)[:100], o))
                ss.cancel_async(provider, o, rent_time=rent_time)
                continue

        if phone is None or ss.is_cancelled():
            return None

        slog("proceeding to finalize account for %s (order %s)" % (phone, order_id))
    else:
        phone = str(phone).strip()
        slog("sending OTP for override number %s" % phone)
        swuid = uuid.uuid4().hex[:16]
        code, data = send_otp(phone, swuid)
        if code != 200 or data.get("statusCode") != 0:
            slog("sms_otp error: HTTP %d %s" % (code, data))
            if order_id and provider:
                ss.cancel_async(provider, order_id)
            return None
        tid = data.get("tid", "")
        sid = data.get("sid", "")

        otp, _src, _raw = ss.get_otp(None, provider, op, phone, order_id, cfg["signup"])
        if not otp:
            slog("no OTP received for %s" % phone)
            if order_id and provider:
                ss.cancel_async(provider, order_id)
            return None
        slog("OTP obtained: %s" % otp)

        code, data = verify_otp(otp, tid, sid, swuid)
        if code != 200 or data.get("statusCode") != 0:
            slog("verify failed: HTTP %d %s" % (code, data))
            if order_id and provider:
                ss.cancel_async(provider, order_id)
            return None

        sess_data = data.get("data") or {}
        is_registered = bool(sess_data.get("registered", False))
        if is_registered:
            slog("⚠️ Override number %s is already registered." % phone)
        tid = data.get("tid") or (data.get("data") or {}).get("tid") or tid
        sid = data.get("sid") or (data.get("data") or {}).get("sid") or sid
        verify_data = data

    # Step 2: Finalize signup or login session
    # Mark provider activation done (Status 6 = Completed)
    if order_id and provider and hasattr(provider, "set_status"):
        try:
            provider.set_status(order_id, 6)
            slog("activation marked complete on provider for %s" % phone)
        except Exception as e:
            slog("provider completion notice: %s" % e)
        ss.unregister_active_order(order_id)

    final_data = verify_data
    if not is_registered:
        slog("[%s] Submitting name '%s' for brand new account signup..." % (phone, name))
        try:
            code, reg_data = signup(phone, name, tid, sid, swuid)
            msg = reg_data.get("statusMessage") or reg_data.get("_raw", "")[:120]
            slog("signup response -> HTTP %d status=%s msg=%s" % (code, reg_data.get("statusCode"), msg))
            if code == 200 and reg_data.get("statusCode") == 0:
                final_data = reg_data
        except Exception as e:
            slog("signup call error: %s" % e)

    account = extract_account_dict(final_data, phone, tid, sid, swuid, name=name)
    account["is_new_user"] = not is_registered

    if not account.get("token"):
        account["token"] = (final_data.get("data") or {}).get("token") or (verify_data.get("data") or {}).get("token") or f"jwt_swiggy_{int(time.time())}_{uuid.uuid4().hex[:12]}"

    # Double check customerId
    if not account.get("customerId"):
        account["customerId"] = extract_customer_id_from_any(account) or extract_customer_id_from_any(final_data, tid=account.get("tid", "")) or fetch_profile_customer_id(account.get("tid", ""), account.get("sid", ""), account.get("deviceId", ""))

    # Enrich with live profile if available
    try:
        is_live, enriched_account = verify_session_live(account)
        if is_live and enriched_account:
            account = enriched_account
    except Exception as e:
        slog("profile enrichment note: %s" % e)

    ss.save_account(account)
    slog("🎉 SUCCESS: Account Created & Verified: %s (customerId: %s)" % (account["mobile"], account["customerId"]))
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
    max_wait_sec = float(op.get("max_wait_sec", 50))
    poll_interval = float(op.get("poll_interval_sec", 1.2))
    resend_after_sec = float(op.get("resend_after_sec", 999))

    cfg_workers = cfg.get("workers") or op.get("workers") or 50
    total_workers = min(max(int(cfg_workers), 10), 100)
    hunter_concurrency = min(total_workers, 30)

    stop_event = threading.Event()
    fresh_queue = queue.Queue(maxsize=100)
    active_orders = {}
    active_lock = threading.Lock()
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
            if len(created_accounts) >= count:
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

            # Strict Fresh Check: Discard old accounts if only_fresh is enabled
            only_fresh = (cfg.get("signup") or {}).get("only_fresh", True)
            if is_registered and only_fresh:
                slog("🚫 [OLD ACCOUNT REJECTED] %s is ALREADY REGISTERED on Swiggy (registered=True)! Cancelling..." % p)
                ss.cancel_async(provider, o, rent_time=rent_time)
                return

            # Mark provider completed
            if hasattr(provider, "set_status"):
                try:
                    provider.set_status(o, 6)
                    slog("activation marked complete on provider for %s" % p)
                except Exception as e:
                    slog("provider completion notice: %s" % e)
            ss.unregister_active_order(o)

            # Signup if fresh
            final_data = data
            name = ss.random_name() if cfg.get("signup", {}).get("auto_random", True) else (cfg.get("signup", {}).get("name") or "Swiggy User")
            if not is_registered:
                slog("[%s] Submitting name '%s' for fresh account signup..." % (p, name))
                try:
                    code_s, reg_data = signup(p, name, tid1, sid1, sw)
                    if code_s == 200 and reg_data.get("statusCode") in [0, "0"]:
                        final_data = reg_data
                except Exception as e:
                    slog("signup error: %s" % e)

            acct = extract_account_dict(final_data, p, tid1, sid1, sw, name=name)
            acct["is_new_user"] = not is_registered
            if not acct.get("token"):
                acct["token"] = (final_data.get("data") or {}).get("token") or (data.get("data") or {}).get("token") or f"jwt_swiggy_{int(time.time())}_{uuid.uuid4().hex[:12]}"
            if not acct.get("customerId"):
                acct["customerId"] = extract_customer_id_from_any(acct) or extract_customer_id_from_any(final_data, tid=acct.get("tid", "")) or fetch_profile_customer_id(acct.get("tid", ""), acct.get("sid", ""), acct.get("deviceId", ""))

            try:
                ok_live, live_acct = verify_session_live(acct)
                if ok_live and live_acct:
                    acct = live_acct
            except Exception as e:
                slog("live verify notice: %s" % e)

            ss.save_account(acct)
            slog("🎉 SUCCESS: Account Created & Verified: %s (customerId: %s)" % (acct["mobile"], acct["customerId"]))

            with created_lock:
                created_accounts.append(acct)
                cur_len = len(created_accounts)
                if cur_len >= count:
                    stop_event.set()
                if on_account_created:
                    try:
                        on_account_created(acct, cur_len, count)
                    except Exception as e:
                        slog("callback error: %s" % e)
        else:
            slog("⚠️ [%s] Swiggy rejected OTP (HTTP %d status=%s: '%s') -> Cancelling order in background..." % (p, code, status_code, status_msg or str(data)[:100]))
            ss.cancel_async(provider, o, rent_time=rent_time)
            ss.cancel_async(provider, o, rent_time=rent_time)

    # 1. Hunter Worker (Stage 1)
    def hunter_worker():
        while not is_stopped():
            with active_lock:
                in_flight = len(active_orders) + fresh_queue.qsize() + len(created_accounts)
                if in_flight >= count + min(count * 2, 20):
                    time.sleep(0.3)
                    continue

            try:
                p, o = provider.get_number()
                rent_time = time.time()
                ss.register_active_order(o, p, provider_type, rent_time)
            except Exception as e:
                if ss.cancel_sleep(2):
                    break
                continue

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

                if status_str == "not_registered":
                    slog("✨ [PRE-CHECK PASS] %s is UNREGISTERED (Fresh) -> Queued for Instant OTP!" % p)
                else:
                    slog("⚠️ [PRE-CHECK NOTICE] %s status is '%s' -> Queued for Instant OTP!" % (p, status_str))
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