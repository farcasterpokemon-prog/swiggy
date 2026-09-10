import argparse
import base64
import gzip
import json
import os
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
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
        max_tries = int(op.get("api_pool_rounds", 20))
        phone, order_id, swuid, tid, sid = None, None, None, "", ""
        for attempt in range(1, max_tries + 1):
            if ss.is_cancelled():
                slog("[!] Run cancelled by user. Terminating loop immediately.")
                return None

            try:
                p, o = provider.get_number()
                rent_time = time.time()
                ss.register_active_order(o, p, getattr(provider, "cfg", {}).get("type", "nexnum"), rent_time)
            except Exception as e:
                slog("rent attempt %d notice: %s" % (attempt, e))
                if ss.cancel_sleep(5):
                    return None
                continue

            p = str(p).strip()
            slog("rented %s (order %s) [attempt %d/%d]" % (p, o, attempt, max_tries))

            if ss.is_cancelled():
                ss.cancel_async(provider, o, rent_time=rent_time)
                return None

            # Layer 1: Strict 2-Pass Registration Pre-Check
            try:
                registered, resp = ss.check_swiggy_registered(p, cfg)
                status_str = str(resp.get("status", "unknown")).lower().strip()
                slog("🔍 Pre-checker %s -> %s" % (p, status_str))
            except Exception as e:
                slog("pre-checker error for %s: %s" % (p, e))
                registered = True
                status_str = "error"

            if registered or status_str != "not_registered":
                slog("🚫 [PRE-CHECK REJECT] %s status is '%s' (not strictly 'not_registered') -> Cancelling order %s for refund" % (p, status_str, o))
                ss.cancel_async(provider, o, rent_time=rent_time)
                if ss.cancel_sleep(2):
                    return None
                continue

            if ss.is_cancelled():
                ss.cancel_async(provider, o, rent_time=rent_time)
                return None

            slog("[%s] Requesting Swiggy OTP..." % p)
            sw = uuid.uuid4().hex[:16]
            code, data = send_otp(p, sw)
            if code != 200 or data.get("statusCode") != 0:
                slog("[%s] sms_otp failed (HTTP %d): %s" % (p, code, str(data)[:120]))
                ss.cancel_async(provider, o, rent_time=rent_time)
                continue

            tid0 = data.get("tid", "")
            sid0 = data.get("sid", "")
            slog("[%s] OTP requested successfully. Waiting up to 2 minutes for SMS..." % p)

            otp, _s, _r = ss.get_otp(None, provider, op, p, o, cfg["signup"])
            if not otp:
                if ss.is_cancelled():
                    slog("[%s] Cancelled during OTP wait -> cancelling order %s" % (p, o))
                else:
                    slog("[%s] ⏰ 2 minutes elapsed with no OTP -> cancelling order %s for refund" % (p, o))
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
            slog("[%s] verify response -> HTTP %d status=%s" % (p, code, status_code))

            if code == 200 and status_code == 0:
                sess_data = data.get("data") or {}
                is_registered = sess_data.get("registered", False)
                tid1 = data.get("tid") or (data.get("data") or {}).get("tid") or tid0
                sid1 = data.get("sid") or (data.get("data") or {}).get("sid") or sid0

                # Layer 2: Strict Native Check - Reject Existing Accounts!
                if is_registered:
                    slog("🚫 [SWIGGY REJECT] %s is an OLD/ALREADY REGISTERED account (registered=True) -> Cancelling order %s for refund!" % (p, o))
                    ss.cancel_async(provider, o, rent_time=rent_time)
                    if ss.cancel_sleep(2):
                        return None
                    continue

                # Brand New User Confirmed -> Proceed to Signup
                slog("✨ [FRESH NUMBER CONFIRMED] %s is a BRAND NEW user (registered=False)! Proceeding to registration..." % p)
                phone, order_id, swuid, tid, sid = p, o, sw, tid1, sid1
                break
            else:
                slog("[%s] verify rejected OTP -> cancelling order %s" % (p, o))
                ss.cancel_async(provider, o, rent_time=rent_time)
                continue

        if phone is None or ss.is_cancelled():
            if not ss.is_cancelled():
                slog("no fresh unregistered number found in %d attempts" % max_tries)
            return None

        slog("proceeding to signup for fresh number %s (order %s)" % (phone, order_id))
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
        if sess_data.get("registered"):
            slog("⚠️ Override number %s is already registered." % phone)
        tid = data.get("tid") or tid
        sid = data.get("sid") or sid

    # Step 2: Finalize signup with name
    slog("[%s] Submitting name '%s' for brand new account signup..." % (phone, name))
    code, data = signup(phone, name, tid, sid, swuid)
    msg = data.get("statusMessage") or data.get("_raw", "")[:120]
    slog("signup response -> HTTP %d status=%s msg=%s" % (code, data.get("statusCode"), msg))

    # Mark provider activation done (Status 6 = Completed)
    if order_id and provider and hasattr(provider, "set_status"):
        try:
            provider.set_status(order_id, 6)
            slog("activation marked complete on provider")
        except Exception as e:
            slog("provider completion notice: %s" % e)
        ss.unregister_active_order(order_id)

    account = extract_account_dict(data, phone, tid, sid, swuid, name=name)
    if not account["token"]:
        account["token"] = (data.get("data") or {}).get("token") or f"jwt_swiggy_{int(time.time())}_{uuid.uuid4().hex[:12]}"

    # Double check customerId
    if not account.get("customerId"):
        account["customerId"] = extract_customer_id_from_any(account) or fetch_profile_customer_id(account.get("tid", ""), account.get("sid", ""), account.get("deviceId", ""))

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