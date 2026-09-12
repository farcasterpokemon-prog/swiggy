import argparse
import base64
import json
import os
import random
import re
import ssl
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
import zipfile
from concurrent.futures import ThreadPoolExecutor

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

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE_DIR, "swiggy_signup.json")
UI_DUMP = os.path.join(BASE_DIR, "ui_dump.xml")
SMS_SH = os.path.join(BASE_DIR, "_read_sms.sh")
ACCOUNTS_PATH = os.path.join(BASE_DIR, "accounts.json")
ACTIVE_ORDERS_PATH = os.path.join(BASE_DIR, "active_orders.json")
ACTIVE_ORDERS_LOCK = threading.Lock()

CTX = ssl.create_default_context()
CTX.check_hostname = False
CTX.verify_mode = ssl.CERT_NONE
UNVERIFIED_CTX = CTX


def get_proxy_url():
    try:
        cfg = load_config(CONFIG_PATH)
        p_cfg = cfg.get("proxy") or {}
        if p_cfg.get("enabled") and p_cfg.get("url"):
            return p_cfg.get("url").strip()
    except Exception:
        pass
    return None


def get_opener(proxy_url=None):
    if proxy_url is None:
        proxy_url = get_proxy_url()
    handlers = [urllib.request.HTTPSHandler(context=UNVERIFIED_CTX)]
    if proxy_url:
        handlers.append(urllib.request.ProxyHandler({"http": proxy_url, "https": proxy_url}))
    return urllib.request.build_opener(*handlers)


LOG_HOOK = None


def notify(msg):
    if LOG_HOOK:
        try:
            LOG_HOOK(str(msg))
        except Exception:
            pass


def log(msg):
    ts = time.strftime("%H:%M:%S")
    msg_str = str(msg)
    try:
        print("[%s] %s" % (ts, msg_str), flush=True)
    except Exception:
        try:
            print("[%s] %s" % (ts, msg_str.encode("ascii", "replace").decode("ascii")), flush=True)
        except Exception:
            pass
    notify(msg_str)


def load_config(path):
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def run(cmd, timeout=90):
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return p.stdout or ""
    except Exception as e:
        return "ERR:" + str(e)


class Adb:
    def __init__(self, cfg):
        self.adb = cfg["device"]["adb_path"]
        self.serial = cfg["device"]["serial"]

    def raw(self, device_cmd, timeout=90):
        return run([self.adb, "-s", self.serial, "shell", device_cmd], timeout)

    def su(self, device_cmd, timeout=90):
        return self.raw('su -c "%s"' % device_cmd.replace('"', '\\"'), timeout)

    def tap(self, x, y):
        return self.su("input tap %d %d" % (x, y))

    def text(self, s):
        return self.su("input text %s" % s.replace(" ", "%s"))

    def key(self, code):
        return self.su("input keyevent %d" % code)

    def focus(self):
        return self.raw("dumpsys window | grep -E 'mCurrentFocus|mFocusedApp'")

    def foreground(self, package):
        return package in self.focus()

    def dump_ui(self):
        self.raw("rm -f /sdcard/ui.xml; uiautomator dump /sdcard/ui.xml >/dev/null 2>&1; chmod 644 /sdcard/ui.xml")
        run([self.adb, "-s", self.serial, "pull", "/sdcard/ui.xml", UI_DUMP])
        return UI_DUMP

    def push(self, local, remote):
        run([self.adb, "-s", self.serial, "push", local, remote])

    def su_sh(self, local, name, timeout=60):
        remote = "/data/local/tmp/" + name
        self.push(local, remote)
        return self.su("sh %s" % remote, timeout)

    def clear_app(self, package):
        self.su("am force-stop %s; pm clear %s" % (package, package))

    def appdata(self):
        out = self.su("echo $HOME")
        return "/data/data/in.swiggy.android"

    def launch(self, package):
        self.raw("am force-stop " + package)
        time.sleep(1)
        self.raw("monkey -p %s -c android.intent.category.LAUNCHER 1 >/dev/null 2>&1" % package)

    def rotate_identity(self):
        """Rotate device identifiers like the qute script: android_id + serialno + adid reset (no reboot)."""
        script = (
            "su -c rm /data/data/com.google.android.gms/shared_prefs/adid_settings.xml\n"
            "e=$(date +\"%s\" | sha1sum | cut -c -16)\n"
            "su -c settings put secure android_id $e\n"
            "d=$(date +\"%s\" | sha1sum | cut -c -10)\n"
            "su -c resetprop ro.serialno $d\n"
        )
        self.su_sh_str(script, "rotate.sh")
        return {
            "android_id": self.su("settings get secure android_id").strip(),
            "serialno": self.su("getprop ro.serialno").strip(),
        }

    def su_sh_str(self, content, name, timeout=60):
        local = os.path.join(tempfile.gettempdir(), name)
        with open(local, "w", encoding="utf-8", newline="\n") as f:
            f.write(content)
        self.push(local, "/data/local/tmp/" + name)
        return self.su("sh /data/local/tmp/%s" % name, timeout)

    def emulator_rotate(self, pkg="com.device.emulator.pro", activity="com.device.emulator.pro/.MainActivity"):
        """Launch Device Emulator Pro and tap 'Random all' to rotate full identity."""
        self.raw("am start -n %s" % activity)
        time.sleep(4)
        self.dump_ui()
        nodes = ui_nodes()
        target = find_node(nodes, desc="Random all") or find_node(nodes, resource_id="action_randomall")
        if target and tap_center(self, target):
            time.sleep(3)
            self.raw("am force-stop %s" % pkg)
            return True
        return False


def ui_nodes():
    if not os.path.exists(UI_DUMP):
        return []
    try:
        root = ET.parse(UI_DUMP).getroot()
    except Exception:
        return []
    nodes = []
    for n in root.iter("node"):
        nodes.append({
            "text": n.get("text", ""),
            "desc": n.get("content-desc", ""),
            "bounds": n.get("bounds", ""),
            "enabled": n.get("enabled", "true") != "false",
            "resource_id": n.get("resource-id", ""),
        })
    return nodes


def center(bounds):
    m = re.match(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]", bounds or "")
    if not m:
        return None
    x1, y1, x2, y2 = map(int, m.groups())
    return ((x1 + x2) // 2, (y1 + y2) // 2)


def find_node(nodes, **attrs):
    for n in nodes:
        ok = True
        for k, v in attrs.items():
            if k == "enabled":
                if n["enabled"] != v:
                    ok = False
            else:
                if v.lower() not in n[k].lower():
                    ok = False
        if ok:
            return n
    return None


def wait_focus(adb, needles, timeout):
    end = time.time() + timeout
    while time.time() < end:
        f = adb.focus()
        for nd in needles:
            if nd in f:
                return f
        time.sleep(2)
    return adb.focus()


def dismiss_permission(adb):
    """Tap ALLOW if a system permission dialog is covering the app."""
    adb.dump_ui()
    nodes = ui_nodes()
    allow = find_node(nodes, text="ALLOW") or find_node(nodes, text="Allow") or find_node(nodes, text="While using the app")
    if allow and tap_center(adb, allow):
        log("tapped permission ALLOW")
        time.sleep(2)
        return True
    return False


def wait_for_home(adb, app, timeout=90):
    end = time.time() + timeout
    while time.time() < end:
        f = adb.focus()
        if app["home_activity"] in f:
            return f
        if "GrantPermissionsActivity" in f or "permission" in f.lower():
            dismiss_permission(adb)
            continue
        time.sleep(2)
    return adb.focus()


def launch_swiggy(adb, app, tries=3):
    for i in range(tries):
        adb.launch(app["package"])
        end = time.time() + 35
        while time.time() < end:
            if adb.foreground(app["package"]):
                time.sleep(3)
                log("swiggy in foreground (try %d)" % (i + 1))
                return True
            time.sleep(2)
    log("could not bring swiggy to foreground")
    return False


def extract_otp(out):
    if not out:
        return None
    if isinstance(out, bytes):
        out = out.decode("utf-8", "replace")
    s = str(out).strip()
    if s.startswith("STATUS_OK:"):
        s = s[len("STATUS_OK:") :].strip()

    # 1. Direct 4-6 digit numeric string
    if re.fullmatch(r"\d{4,6}", s):
        return s

    # 2. Look for 6-digit OTP near keywords (swiggy / otp / code / verification / is)
    m_key = re.search(r"(?:swiggy|otp|code|verification|login|is)\D{0,15}(\d{6})\b", s, re.I)
    if m_key:
        return m_key.group(1)

    # 3. Any 6-digit numeric match
    m6 = re.findall(r"\b\d{6}\b", s)
    if m6:
        return m6[0]

    # 4. Keyword search with 4-6 digits
    lines = [l for l in s.splitlines() if re.search(r"swiggy|otp|one[ -]?time|code|verification|login", l, re.I)]
    for l in lines:
        for m in re.findall(r"\b\d{4,6}\b", l):
            return m

    # 5. Any 4-6 digit match in the full text
    for m in re.findall(r"\b\d{4,6}\b", s):
        return m

    return None


def ensure_sms_script():
    with open(SMS_SH, "w", encoding="utf-8") as fh:
        fh.write("content query --uri content://sms --projection body --sort 'date DESC' --limit 12\n")


def json_path(data, path):
    if not path:
        return data
    cur = data
    for part in path.split("."):
        if isinstance(cur, dict):
            cur = cur.get(part)
        elif isinstance(cur, list) and part.isdigit() and int(part) < len(cur):
            cur = cur[int(part)]
        else:
            return None
        if cur is None:
            return None
    return cur


class OtpProvider:
    def __init__(self, cfg):
        self.cfg = cfg

    def _call(self, spec, order_id=None):
        base = self.cfg["base_url"].rstrip("/")
        path = spec.get("path", "").replace("{order_id}", order_id or "")
        url = base + path
        if spec.get("query"):
            qs = urllib.parse.urlencode(spec["query"])
            url += ("&" if "?" in url else "?") + qs
        headers = dict(self.cfg.get("headers", {}))
        body = None
        if spec.get("json_body"):
            body = json.dumps(spec["json_body"]).encode()
            headers.setdefault("Content-Type", "application/json")
        req = urllib.request.Request(url, data=body, headers=headers, method=spec.get("method", "GET"))
        try:
            with urllib.request.urlopen(req, timeout=30, context=UNVERIFIED_CTX) as resp:
                raw = resp.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as he:
            try:
                raw = he.read().decode("utf-8", "replace")
            except Exception:
                raw = f"HTTP_{he.code}"
        except Exception as e:
            raw = f"ERROR_{e}"
        try:
            return json.loads(raw)
        except Exception:
            return {"_raw": raw}

    def get_number(self):
        data = self._call(self.cfg["get_number"])
        phone = json_path(data, self.cfg["get_number"].get("number_field", "number"))
        order = json_path(data, self.cfg["get_number"].get("order_id_field", "order_id"))
        return str(phone), str(order)

    def fetch_otp(self, order_id):
        data = self._call(self.cfg["get_otp"], order_id)
        sms = json_path(data, self.cfg["get_otp"].get("sms_field", "sms"))
        if sms is None:
            sms = data.get("_raw", "")
        return sms


PROVIDER_CALL_LOCK = threading.Lock()
LAST_PROVIDER_CALL_TIME = 0.0


def throttled_provider_call(url, headers=None, timeout=35, max_retries=5):
    global LAST_PROVIDER_CALL_TIME
    last_raw = ""
    for attempt in range(1, max_retries + 1):
        with PROVIDER_CALL_LOCK:
            now = time.time()
            gap = now - LAST_PROVIDER_CALL_TIME
            min_gap = 1.15
            if gap < min_gap:
                time.sleep(min_gap - gap)
            LAST_PROVIDER_CALL_TIME = time.time()

        req = urllib.request.Request(
            url,
            headers=headers or {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"},
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout, context=UNVERIFIED_CTX) as r:
                raw = r.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as he:
            try:
                raw = he.read().decode("utf-8", "replace")
            except Exception:
                raw = f"HTTP_{he.code}"
            if he.code == 429 or "RATE_LIMIT" in raw:
                time.sleep(2.0 * attempt)
                last_raw = raw
                continue
        except Exception as e:
            raw = f"ERROR_{e}"

        last_raw = raw
        if "RATE_LIMIT_EXCEEDED" in raw:
            time.sleep(2.0 * attempt)
            continue

        try:
            return json.loads(raw)
        except Exception:
            return {"_raw": raw}

    try:
        return json.loads(last_raw)
    except Exception:
        return {"_raw": last_raw}


class SmsActivateBaseProvider:
    BASE = "https://api.sms-activate.org/stubs/handler_api.php"

    def __init__(self, cfg):
        self.cfg = cfg
        self.key = str(cfg.get("api_key", "")).strip()
        self.service = str(cfg.get("service", "jx")).strip()
        self.country = cfg.get("country", 22)
        self.phone_code = str(cfg.get("phone_code", "91")).strip()
        self.base = (cfg.get("base_url") or self.BASE).rstrip("/")

    def _call(self, action, **params):
        q = {"action": action, "api_key": self.key}
        q.update(params)
        url = self.base + "?" + urllib.parse.urlencode(q)
        return throttled_provider_call(url)

    def get_number(self):
        params = {"service": self.service, "country": self.country}
        if self.cfg.get("provider"):
            params["provider"] = str(self.cfg["provider"])
        if self.cfg.get("max_price") is not None:
            params["maxPrice"] = self.cfg["max_price"]

        data = self._call("getNumber", **params)
        raw = data.get("_raw", "")
        if isinstance(raw, str) and raw.startswith("ACCESS_NUMBER"):
            parts = raw.split(":")
            if len(parts) >= 3:
                order_id = parts[1]
                phone = parts[2]
                if phone.startswith(self.phone_code):
                    phone = phone[len(self.phone_code):]
                return str(phone), str(order_id)
        if isinstance(data, dict) and (data.get("activationId") or data.get("id")):
            order_id = str(data.get("activationId") or data.get("id"))
            number = str(data.get("phoneNumber") or data.get("phone") or "")
            if number.startswith(self.phone_code):
                number = number[len(self.phone_code):]
            return number, order_id
        raise RuntimeError(f"{self.__class__.__name__} getNumber failed: {raw or data}")

    def fetch_otp(self, order_id):
        data = self._call("getStatus", id=order_id)
        raw = data.get("_raw", "")
        if isinstance(raw, str):
            raw = raw.strip()
            if raw.startswith("STATUS_OK:"):
                return raw[len("STATUS_OK:") :].strip()
            if raw.startswith("STATUS_OK"):
                parts = raw.split(":", 1)
                return parts[1].strip() if len(parts) > 1 else raw
            if "STATUS_WAIT" in raw:
                return ""
            if "STATUS_CANCEL" in raw or "NO_ACTIVATION" in raw:
                return ""
            return raw
        if isinstance(data, dict):
            if data.get("sms"):
                sms = data["sms"]
                if isinstance(sms, dict):
                    return str(sms.get("code") or sms.get("text") or "")
                return str(sms)
            if data.get("code"):
                return str(data.get("code"))
            if data.get("status") == "STATUS_OK":
                return str(data.get("code") or data.get("text") or data.get("msg") or "")
        return ""

    def set_status(self, order_id, status=6, retries=3, delay=5):
        last = None
        for _ in range(retries):
            try:
                return self._call("setStatus", id=order_id, status=status)
            except Exception as e:
                last = e
                time.sleep(delay)
        if last:
            raise last

    def get_balance(self):
        try:
            data = self._call("getBalance")
            raw = data.get("_raw", "") if isinstance(data, dict) else str(data)
            if isinstance(raw, str):
                raw_clean = raw.strip()
                if "ACCESS_BALANCE:" in raw_clean:
                    val = raw_clean.split("ACCESS_BALANCE:")[1].strip()
                    m = re.search(r"^([0-9\.]+)", val)
                    return m.group(1) if m else val
                if raw_clean in ["BAD_KEY", "NO_KEY", "ERROR_KEY"]:
                    return "Invalid API Key (BAD_KEY)"
                if "DOCTYPE html" in raw_clean or "<html" in raw_clean:
                    return "Blocked / Cloudflare Protected"
                if raw_clean.startswith("ERROR_") or raw_clean.startswith("HTTP_"):
                    if "getaddrinfo" in raw_clean:
                        return "DNS / Host Unreachable"
                    return f"API Error ({raw_clean[:35]})"
                if len(raw_clean) > 50:
                    return "Unexpected Response"
                if raw_clean:
                    return raw_clean
            if isinstance(data, dict):
                if "balance" in data:
                    return str(data["balance"])
                if "error" in data:
                    return f"Error: {data['error']}"
            return str(raw or data)
        except Exception as e:
            return f"Error: {e}"


class NexnumProvider(SmsActivateBaseProvider):
    BASE = "https://nexnum.in/stubs/handler_api.php"
    DEFAULT_PROVIDERS = ["9779", "4591", ""]

    def __init__(self, cfg):
        super().__init__(cfg)
        raw_provider = cfg.get("provider")
        if raw_provider:
            if isinstance(raw_provider, list):
                self.providers = [str(x) for x in raw_provider]
            elif "," in str(raw_provider):
                self.providers = [x.strip() for x in str(raw_provider).split(",") if x.strip()]
            else:
                self.providers = [str(raw_provider)] + [str(x) for x in self.DEFAULT_PROVIDERS if str(x) != str(raw_provider)]
        else:
            self.providers = list(self.DEFAULT_PROVIDERS)

        raw_price = cfg.get("max_price", 8)
        if isinstance(raw_price, (int, float)) and raw_price <= 1.0:
            self.max_price = int(round(raw_price * 100))
        else:
            try:
                self.max_price = int(raw_price)
            except Exception:
                self.max_price = 8

    def get_number(self):
        last_err = None
        for prov_id in self.providers:
            params = {"service": self.service, "country": self.country}
            if prov_id:
                params["provider"] = prov_id
            if self.max_price is not None:
                params["maxPrice"] = self.max_price

            data = self._call("getNumber", **params)
            raw = data.get("_raw", "")
            if isinstance(raw, str) and raw.startswith("ACCESS_NUMBER"):
                parts = raw.split(":")
                if len(parts) >= 3:
                    order_id = parts[1]
                    phone = parts[2]
                    if phone.startswith(self.phone_code):
                        phone = phone[len(self.phone_code):]
                    return str(phone), str(order_id)
            if isinstance(data, dict) and (data.get("activationId") or data.get("id")):
                order_id = str(data.get("activationId") or data.get("id"))
                number = str(data.get("phoneNumber") or data.get("phone") or "")
                if number.startswith(self.phone_code):
                    number = number[len(self.phone_code):]
                return number, order_id
            last_err = raw or data
            continue
        raise RuntimeError(f"Nexnum getNumber failed across providers {self.providers}: {last_err}")

    def set_status(self, order_id, status=6, retries=3, delay=5):
        """On Nexnum API: status=-1 is Cancel & Refund, status=6/8 is Complete Line."""
        target_status = -1 if status in [8, -1, "8", "-1"] else status
        last = None
        for _ in range(retries):
            try:
                res = self._call("setStatus", id=order_id, status=target_status)
                res_str = str(res.get("_raw", "") if isinstance(res, dict) else res).strip()
                if target_status == -1 and "BAD_STATUS" in res_str:
                    res8 = self._call("setStatus", id=order_id, status=8)
                    res8_str = str(res8.get("_raw", "") if isinstance(res8, dict) else res8).strip()
                    if "BAD_STATUS" not in res8_str:
                        return res8
                return res
            except Exception as e:
                last = e
                time.sleep(delay)
        if last:
            raise last


class GrizzlyProvider(SmsActivateBaseProvider):
    BASE = "https://api.grizzlysms.com/stubs/handler_api.php"

    def __init__(self, cfg):
        super().__init__(cfg)
        self.service = cfg.get("service", "hp")


class TigerProvider(SmsActivateBaseProvider):
    BASE = "https://api.tiger-sms.com/stubs/handler_api.php"

    def __init__(self, cfg):
        super().__init__(cfg)
        self.service = cfg.get("service", "jx")


class SmsActivateProvider(SmsActivateBaseProvider):
    BASE = "https://api.sms-activate.org/stubs/handler_api.php"

    def __init__(self, cfg):
        super().__init__(cfg)
        self.service = cfg.get("service", "cw")


class SMSHubProvider(SmsActivateBaseProvider):
    BASE = "https://smshub.org/stubs/handler_api.php"

    def __init__(self, cfg):
        super().__init__(cfg)
        self.service = cfg.get("service", "cw")


class DaisySMSProvider(SmsActivateBaseProvider):
    BASE = "https://daisysms.com/stubs/handler_api.php"

    def __init__(self, cfg):
        super().__init__(cfg)
        self.service = cfg.get("service", "swiggy")


class UotpProvider(SmsActivateBaseProvider):
    BASE = "https://uotp.store/api/stubs/handler_api.php"
    DEFAULT_OPERATORS = [3, 4, 5, 8, 11]

    def __init__(self, cfg):
        super().__init__(cfg)
        self.service = str(cfg.get("service", "swiggy")).strip()
        custom_op = cfg.get("operator")
        if custom_op:
            if isinstance(custom_op, list):
                self.operators = [str(x) for x in custom_op]
            elif "," in str(custom_op):
                self.operators = [x.strip() for x in str(custom_op).split(",") if x.strip()]
            else:
                self.operators = [str(custom_op)] + [str(x) for x in self.DEFAULT_OPERATORS if str(x) != str(custom_op)]
        else:
            self.operators = [str(x) for x in self.DEFAULT_OPERATORS]
        self.base = (cfg.get("base_url") or self.BASE).rstrip("/")

    def get_number(self):
        last_err = None
        for op in self.operators:
            params = {"service": self.service, "country": self.country, "operator": op}
            data = self._call("getNumber", **params)
            raw = data.get("_raw", "")
            if isinstance(raw, str) and raw.startswith("ACCESS_NUMBER"):
                parts = raw.split(":")
                if len(parts) >= 3:
                    order_id = parts[1]
                    phone = parts[2]
                    if phone.startswith(self.phone_code):
                        phone = phone[len(self.phone_code):]
                    return str(phone), str(order_id)
            if isinstance(data, dict) and (data.get("activationId") or data.get("id")):
                order_id = str(data.get("activationId") or data.get("id"))
                number = str(data.get("phoneNumber") or data.get("phone") or "")
                if number.startswith(self.phone_code):
                    number = number[len(self.phone_code):]
                return number, order_id
            last_err = raw or data
            if "NO_NUMBERS" in str(last_err) or "NO_CONNECTION" in str(last_err) or "BAD_OPERATOR" in str(last_err):
                continue
        raise RuntimeError(f"Uotp getNumber failed across operators {self.operators}: {last_err}")


class FiveSimProvider:
    BASE = "https://5sim.net/v1/user"

    def __init__(self, cfg):
        self.cfg = cfg
        self.key = str(cfg.get("api_key", "")).strip()
        self.service = cfg.get("service", "swiggy")
        self.country = cfg.get("country_name", "india")
        self.operator = cfg.get("operator", "any")
        self.phone_code = str(cfg.get("phone_code", "91")).strip()

    def _headers(self):
        return {
            "Authorization": f"Bearer {self.key}",
            "Accept": "application/json",
            "User-Agent": "Mozilla/5.0",
        }

    def get_number(self):
        url = f"{self.BASE}/buy/activation/{self.country}/{self.operator}/{self.service}"
        req = urllib.request.Request(url, headers=self._headers())
        with urllib.request.urlopen(req, timeout=30, context=UNVERIFIED_CTX) as r:
            data = json.loads(r.read().decode("utf-8"))
        order_id = str(data.get("id"))
        phone = str(data.get("phone", ""))
        if phone.startswith(self.phone_code):
            phone = phone[len(self.phone_code):]
        return phone, order_id

    def fetch_otp(self, order_id):
        url = f"{self.BASE}/check/{order_id}"
        req = urllib.request.Request(url, headers=self._headers())
        with urllib.request.urlopen(req, timeout=30, context=UNVERIFIED_CTX) as r:
            data = json.loads(r.read().decode("utf-8"))
        sms = data.get("sms") or []
        if sms and isinstance(sms, list):
            return str(sms[0].get("code") or sms[0].get("text") or "")
        return ""

    def set_status(self, order_id, status=6):
        action = "finish" if status == 6 else "cancel"
        url = f"{self.BASE}/{action}/{order_id}"
        req = urllib.request.Request(url, headers=self._headers(), method="GET")
        try:
            with urllib.request.urlopen(req, timeout=20, context=UNVERIFIED_CTX) as r:
                return json.loads(r.read().decode("utf-8"))
        except Exception as e:
            return {"error": str(e)}

    def get_balance(self):
        try:
            url = f"{self.BASE}/profile"
            req = urllib.request.Request(url, headers=self._headers())
            with urllib.request.urlopen(req, timeout=20, context=UNVERIFIED_CTX) as r:
                data = json.loads(r.read().decode("utf-8"))
            return str(data.get("balance", "0"))
        except urllib.error.HTTPError as he:
            if he.code == 401:
                return "Invalid API Key (HTTP 401)"
            return f"HTTP {he.code}: {he.reason}"
        except urllib.error.URLError as ue:
            return f"Network Error: {ue.reason}"
        except Exception as e:
            return f"Error: {e}"


class GenericSmsProvider(SmsActivateBaseProvider):
    pass


def check_single_pass(mobile, url, timeout=8):
    headers = {
        "Content-Type": "application/json",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept": "application/json, text/plain, */*",
    }
    payload = json.dumps({"mobile": str(mobile).strip()}).encode("utf-8")
    req = urllib.request.Request(url, data=payload, headers=headers, method="POST")
    opener = get_opener()
    with opener.open(req, timeout=timeout) as r:
        res_raw = r.read().decode("utf-8", "replace")
        try:
            data = json.loads(res_raw)
        except Exception:
            data = {"_raw": res_raw}

    status = str(data.get("status") or "").lower().strip()
    msg = str(data.get("message") or data.get("msg") or data.get("statusMessage") or "").lower()

    # 1. Strictly Unregistered Check
    if status in ["not_registered", "unregistered", "notregistered", "new_user", "fresh"]:
        return "not_registered", data
    elif data.get("registered") is False or str(data.get("registered", "")).lower() == "false":
        return "not_registered", data
    elif data.get("is_registered") is False or str(data.get("is_registered", "")).lower() == "false":
        return "not_registered", data

    # 2. Registered Check
    if status in ["registered", "exists", "already_registered", "true"]:
        return "registered", data
    elif data.get("registered") is True or str(data.get("registered", "")).lower() == "true":
        return "registered", data
    elif data.get("is_registered") is True or str(data.get("is_registered", "")).lower() == "true":
        return "registered", data
    elif "already registered" in msg or "account exists" in msg or "user exists" in msg:
        return "registered", data

    # 3. Check nested structures
    nested = data.get("data") or data.get("result")
    if isinstance(nested, dict):
        nested_status = str(nested.get("status") or "").lower().strip()
        nested_msg = str(nested.get("message") or nested.get("msg") or "").lower()
        if nested_status in ["not_registered", "unregistered", "notregistered", "new_user", "fresh"]:
            return "not_registered", data
        elif nested.get("registered") is False or str(nested.get("registered", "")).lower() == "false":
            return "not_registered", data
        elif nested.get("is_registered") is False or str(nested.get("is_registered", "")).lower() == "false":
            return "not_registered", data

        if nested_status in ["registered", "exists", "already_registered", "true"]:
            return "registered", data
        elif nested.get("registered") is True or str(nested.get("registered", "")).lower() == "true":
            return "registered", data
        elif nested.get("is_registered") is True or str(nested.get("is_registered", "")).lower() == "true":
            return "registered", data
        elif "already registered" in nested_msg or "account exists" in nested_msg or "user exists" in nested_msg:
            return "registered", data

    # 4. If status is anything else (e.g. unknown, empty, invalid schema), treat as unknown
    return "unknown", data


def check_swiggy_registered(mobile, cfg):
    """
    Strict Double-Pass Registration Checker.
    ONLY returns is_registered=False (proceed) when BOTH Pass 1 and Pass 2 are STRICTLY 'not_registered'.
    In ALL OTHER SCENARIOS ('registered', 'unknown', 'error', 'timeout'):
    returns is_registered=True (REJECT & CANCEL number).
    """
    url = cfg.get("check_url") or "https://checker.otpcart.xyz/api/check-swiggy"
    mobile = str(mobile).strip()

    # Pass 1
    try:
        status1, data1 = check_single_pass(mobile, url)
    except Exception as e:
        status1, data1 = "error", {"error": str(e), "status": "error"}

    # If Pass 1 is NOT strictly 'not_registered' (e.g. 'unknown', 'registered', 'error'), reject immediately!
    if status1 != "not_registered":
        data1.setdefault("status", status1)
        return True, data1

    time.sleep(0.4)

    # Pass 2 (Verification Pass)
    try:
        status2, data2 = check_single_pass(mobile, url)
    except Exception as e:
        status2, data2 = "error", {"error": str(e), "status": "error"}

    # If Pass 2 is NOT strictly 'not_registered', reject immediately!
    if status2 != "not_registered":
        data2.setdefault("status", status2)
        return True, data2

    # Both Pass 1 and Pass 2 are 100% verified 'not_registered'
    return False, {"status": "not_registered", "mobile": mobile, "verified_2pass": True}


def make_provider(op):
    if not op or op.get("enabled") is False:
        return None
    ptype = str(op.get("type", "")).lower().strip()
    if ptype in ["nexnum", "nexnum.in"]:
        return NexnumProvider(op)
    if ptype in ["grizzly", "grizzlysms", "grizzly-sms"]:
        return GrizzlyProvider(op)
    if ptype in ["tiger", "tigersms", "tiger-sms"]:
        return TigerProvider(op)
    if ptype in ["smsactivate", "sms-activate", "sms_activate"]:
        return SmsActivateProvider(op)
    if ptype in ["smshub", "sms-hub", "smshub.org"]:
        return SMSHubProvider(op)
    if ptype in ["5sim", "fivesim", "5sim.net"]:
        return FiveSimProvider(op)
    if ptype in ["daisy", "daisysms", "daisysms.com"]:
        return DaisySMSProvider(op)
    if ptype in ["uotp", "uotp.store", "u_otp"]:
        return UotpProvider(op)
    if op.get("base_url") or ptype in ["generic", "custom"]:
        return GenericSmsProvider(op)
    return OtpProvider(op)


def get_provider_balance(op=None):
    if op is None:
        cfg = load_config(CONFIG_PATH)
        op = cfg.get("otp_provider") or {}
    prov = make_provider(op)
    if not prov:
        return "OTP provider is disabled or not configured."
    if hasattr(prov, "get_balance"):
        try:
            return prov.get_balance()
        except Exception as e:
            return f"Error: {e}"
    return "Balance check not supported on this provider."


CANCEL_THREADS = []
SAVE_LOCK = threading.Lock()
CANCEL_EVENT = threading.Event()


def is_cancelled():
    return CANCEL_EVENT.is_set()


def trigger_cancel():
    CANCEL_EVENT.set()
    log("[!] CANCEL EVENT SIGNALLED - Aborting all active workers immediately.")


def reset_cancel():
    CANCEL_EVENT.clear()


def cancel_sleep(seconds):
    """Sleep for `seconds` but awake immediately if cancel signal is triggered. Returns True if cancelled."""
    return CANCEL_EVENT.wait(timeout=seconds)


def cancel_async(provider, order_id, rent_time=None):
    if not order_id or not provider:
        return None
    if rent_time is None:
        rent_time = time.time()
    t = threading.Thread(target=_cancel_worker, args=(provider, order_id, rent_time), daemon=True)
    t.start()
    CANCEL_THREADS.append(t)
    return t


def load_active_orders():
    if os.path.exists(ACTIVE_ORDERS_PATH):
        try:
            with open(ACTIVE_ORDERS_PATH, "r", encoding="utf-8") as fh:
                return json.load(fh)
        except Exception:
            pass
    return {}


def save_active_orders(orders):
    try:
        tmp = ACTIVE_ORDERS_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(orders, fh, indent=2)
        os.replace(tmp, ACTIVE_ORDERS_PATH)
    except Exception:
        try:
            with open(ACTIVE_ORDERS_PATH, "w", encoding="utf-8") as fh:
                json.dump(orders, fh, indent=2)
        except Exception as e:
            log(f"save_active_orders error: {e}")


def register_active_order(order_id, phone, provider_type="nexnum", rent_time=None):
    if not order_id:
        return
    if rent_time is None:
        rent_time = time.time()
    with ACTIVE_ORDERS_LOCK:
        orders = load_active_orders()
        orders[str(order_id)] = {
            "order_id": str(order_id),
            "phone": str(phone),
            "provider_type": str(provider_type),
            "rent_time": float(rent_time),
            "registered_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        }
        save_active_orders(orders)
        log(f"📌 Registered active rental: {phone} (Order {order_id})")


def unregister_active_order(order_id):
    if not order_id:
        return
    with ACTIVE_ORDERS_LOCK:
        orders = load_active_orders()
        if str(order_id) in orders:
            del orders[str(order_id)]
            save_active_orders(orders)
            log(f"Unregistered active rental: order {order_id}")


def get_active_orders_list():
    with ACTIVE_ORDERS_LOCK:
        orders = load_active_orders()
        return list(orders.values())


def cancel_all_active_numbers(provider=None):
    """
    Cancels all currently rented/active numbers registered across runs in 1 click,
    triggers instant refund or 2-min guaranteed queue, and returns refund summary & live balance.
    """
    trigger_cancel()
    with ACTIVE_ORDERS_LOCK:
        orders = load_active_orders()

    cfg = load_config(CONFIG_PATH)
    if not provider:
        provider = make_provider(cfg.get("otp_provider"))

    if not orders:
        bal = get_provider_balance(cfg.get("otp_provider"))
        return {
            "total": 0,
            "cancelled": 0,
            "failed": 0,
            "orders": [],
            "balance": bal,
            "message": "No active numbers found in queue.",
        }

    cancelled_orders = []
    failed_orders = []

    for oid, oinfo in list(orders.items()):
        phone = oinfo.get("phone", "Unknown")
        rent_time = float(oinfo.get("rent_time", time.time()))
        ptype = str(oinfo.get("provider_type", "")).lower()

        target_prov = provider
        if ptype and hasattr(provider, "cfg") and str(provider.cfg.get("type", "")).lower() != ptype:
            presets = cfg.get("otp_presets", {})
            if ptype in presets:
                target_prov = make_provider(presets[ptype])

        try:
            if hasattr(target_prov, "set_status"):
                res = target_prov.set_status(oid, 8)
                res_str = str(res.get("_raw", "") if isinstance(res, dict) else res).strip()
                if "EARLY_CANCEL_DENIED" in res_str:
                    elapsed = time.time() - rent_time
                    if elapsed < 120.0:
                        cancel_async(target_prov, oid, rent_time=rent_time)
                        cancelled_orders.append({
                            "order_id": oid,
                            "phone": phone,
                            "status": "queued_refund",
                            "wait_sec": int(max(2.0, 122.0 - elapsed)),
                        })
                    else:
                        res2 = target_prov.set_status(oid, 8)
                        res2_str = str(res2.get("_raw", "") if isinstance(res2, dict) else res2).strip()
                        cancelled_orders.append({
                            "order_id": oid,
                            "phone": phone,
                            "status": "refunded",
                            "result": res2_str or "ACCESS_CANCEL",
                        })
                        unregister_active_order(oid)
                else:
                    cancelled_orders.append({
                        "order_id": oid,
                        "phone": phone,
                        "status": "refunded",
                        "result": res_str or "ACCESS_CANCEL",
                    })
                    unregister_active_order(oid)
            else:
                cancelled_orders.append({
                    "order_id": oid,
                    "phone": phone,
                    "status": "cancelled",
                })
                unregister_active_order(oid)
        except Exception as e:
            log(f"Failed to cancel order {oid}: {e}")
            failed_orders.append({"order_id": oid, "phone": phone, "error": str(e)})

    try:
        bal = get_provider_balance(cfg.get("otp_provider"))
    except Exception:
        bal = "N/A"

    return {
        "total": len(orders),
        "cancelled": len(cancelled_orders),
        "failed": len(failed_orders),
        "orders": cancelled_orders,
        "failed_orders": failed_orders,
        "balance": bal,
    }


def _cancel_worker(provider, order_id, rent_time):
    for attempt in range(1, 4):
        try:
            if hasattr(provider, "set_status"):
                res = provider.set_status(order_id, 8)
                res_str = str(res.get("_raw", "") if isinstance(res, dict) else res).strip()
                if "EARLY_CANCEL_DENIED" in res_str:
                    elapsed = time.time() - rent_time
                    wait_sec = max(2.0, 122.0 - elapsed)
                    time.sleep(wait_sec)
                    for sub_attempt in range(3):
                        res2 = provider.set_status(order_id, 8)
                        res2_str = str(res2.get("_raw", "") if isinstance(res2, dict) else res2).strip()
                        if "EARLY_CANCEL_DENIED" in res2_str:
                            time.sleep(4)
                            continue
                        log(f"[Background Refund] Order {order_id} -> {res2_str or 'ACCESS_CANCEL'}")
                        break
                    unregister_active_order(order_id)
                    return
                log(f"[Background Refund] Order {order_id} -> {res_str or 'ACCESS_CANCEL'}")
                unregister_active_order(order_id)
                return
        except Exception:
            time.sleep(3)
    unregister_active_order(order_id)



def b64u_dec(s):
    s += "=" * (-len(s) % 4)
    return base64.urlsafe_b64decode(s).decode("utf-8", "replace")


def capture_session(adb, cfg):
    """Pull Swiggy session credentials from the app's prefs after login."""
    app = cfg["app"]
    local = os.path.join(BASE_DIR, "_session_prefs.xml")
    remote = "/data/local/tmp/_swiggy_prefs.xml"
    adb.su("cp %s/shared_prefs/in.swiggy.android_preferences.xml %s; chmod 644 %s" % (adb.appdata(), remote, remote))
    run([adb.adb, "-s", adb.serial, "pull", remote, local])
    if not os.path.exists(local):
        return None
    f = open(local, encoding="utf-8", errors="replace").read()
    prefs = dict(re.findall(r'<string name="([^"]+)">(.*?)</string>', f))
    user = ""
    try:
        root = ET.fromstring(f)
        for child in root:
            if child.tag == "string" and child.get("name") == "user.PRODUCTION":
                user = child.text or ""
                break
    except Exception:
        pass
    if not user:
        for k, v in prefs.items():
            if k.startswith("user.") and "PRODUCTION" in k:
                user = v.replace("&quot;", '"').replace("&#10;", "\n").replace("&amp;", "&")
                break
    if user and "&quot;" in user:
        user = user.replace("&quot;", '"').replace("&#10;", "\n").replace("&amp;", "&")
    tid = ""
    customer = ""
    mobile = ""
    name = ""
    token = ""
    sid = ""
    device = ""
    if user:
        try:
            ud = json.loads(user)
            tid = ud.get("tid") or ""
            customer = str(ud.get("customerId") or ud.get("customer_id") or ud.get("userId") or "")
            mobile = str(ud.get("phoneNumber") or ud.get("mobile") or "")
            name = str(ud.get("userName") or ud.get("name") or "")
            token = str(ud.get("token") or ud.get("accessToken") or "")
            sid = str(ud.get("sessionId") or ud.get("sid") or "")
            device = str(ud.get("swuid") or ud.get("deviceId") or "")
        except Exception:
            pass
    if not token:
        token = prefs.get("access_token", "") if prefs else ""
    if tid:
        try:
            pl = json.loads(b64u_dec(tid.split(".")[1]))
            if not sid:
                sid = str(pl.get("sid") or "")
            if not customer:
                customer = str(pl.get("user_id") or pl.get("customerId") or pl.get("customer_id") or "")
        except Exception:
            pass
    if not device:
        device = prefs.get("swuid", "")
    return {
        "token": token,
        "tid": tid,
        "sid": sid,
        "deviceId": device,
        "customerId": customer,
        "mobile": mobile,
        "userName": name,
    }


def wait_cancels(timeout=None):
    end = time.time() + timeout if timeout else None
    for t in CANCEL_THREADS:
        try:
            t.join(timeout=(end - time.time()) if end else None)
        except Exception:
            pass


def rent_and_check(provider, cfg, count, retries=6):
    """Rent and check several numbers in parallel. Registered ones are cancelled
    in the background (refund comes later, no blocking). Returns usable (phone, order)."""
    results = []
    lock = threading.Lock()

    def worker(_):
        try:
            phone, order = provider.get_number()
            register_active_order(order, phone, getattr(provider, "cfg", {}).get("type", "nexnum"), time.time())
        except Exception as e:
            log("rent failed: %s" % e)
            return None
        log("rented %s (order %s)" % (phone, order))
        try:
            registered, resp = check_swiggy_registered(phone, cfg)
            status_str = str(resp.get("status", "unknown")).lower()
            log("checker %s -> %s" % (phone, status_str))
        except Exception as e:
            log("checker error for %s (%s); cancelling number" % (phone, e))
            cancel_async(provider, order)
            return None
        if registered or status_str != "not_registered":
            log("number %s status is '%s' (not strictly unregistered) -> cancelling in background" % (phone, status_str))
            cancel_async(provider, order)
            return None
        return (phone, order)

    with ThreadPoolExecutor(max_workers=max(1, int(count))) as ex:
        for r in ex.map(worker, range(count)):
            if r:
                with lock:
                    results.append(r)
    return results


def load_accounts():
    if os.path.exists(ACCOUNTS_PATH):
        try:
            with open(ACCOUNTS_PATH, "r", encoding="utf-8") as fh:
                return json.load(fh)
        except Exception:
            pass
    return []


def save_account(entry):
    with SAVE_LOCK:
        accs = load_accounts()
        accs.append(entry)
        with open(ACCOUNTS_PATH, "w", encoding="utf-8") as fh:
            json.dump(accs, fh, indent=2)
    log("account saved to %s" % ACCOUNTS_PATH)
    save_account_zip(entry)


def save_account_zip(entry):
    mobile = str(entry.get("mobile") or "").strip()
    if not mobile:
        mobile = str(entry.get("customerId") or "account").strip()
    if not mobile:
        mobile = "account"
    mobile = re.sub(r"[^0-9A-Za-z_-]", "_", mobile)
    zpath = os.path.join(BASE_DIR, "account_%s.zip" % mobile)
    try:
        with zipfile.ZipFile(zpath, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("account.json", json.dumps(entry, indent=2))
        log("account zip saved to %s" % zpath)
    except Exception as e:
        log("failed to save account zip: %s" % e)


def tap_center(adb, node):
    c = center(node["bounds"])
    if c:
        adb.tap(*c)
        return True
    return False


def get_otp(adb, provider, op, phone, order_id, s):
    source = s.get("otp_source", "device")
    if source == "provider" and provider:
        max_wait = op.get("max_wait_sec", 180)
        interval = op.get("poll_interval_sec", 4)
        resend_at = op.get("resend_after_sec", 45)
        log("waiting for OTP from provider (max %ds)" % max_wait)
        end = time.time() + max_wait
        raw = ""
        resent = 0
        next_resend = time.time() + resend_at
        while time.time() < end:
            if is_cancelled():
                log("[%s] OTP wait cancelled instantly!" % phone)
                return None, "cancelled", ""
            try:
                raw = provider.fetch_otp(order_id) or ""
            except Exception as e:
                log("provider fetch notice (%s); retrying" % e)
                raw = ""
            otp = extract_otp(raw)
            if otp:
                return otp, "provider", raw
            if time.time() >= next_resend and hasattr(provider, "set_status"):
                resent += 1
                next_resend = time.time() + resend_at
                try:
                    provider.set_status(order_id, 3)
                    log("requested OTP resend #%d" % resent)
                except Exception as e:
                    log("resend notice: %s" % e)
            if cancel_sleep(interval):
                log("[%s] Cancelled during sleep interval." % phone)
                return None, "cancelled", ""
        return None, "provider", raw

    if source == "manual":
        if s.get("otp"):
            return str(s["otp"]), "manual", ""
        try:
            if sys.stdin.isatty():
                otp = input("OTP for %s (from your OTP service): " % phone).strip()
                return otp, "manual", ""
        except Exception:
            pass
        log("manual OTP required but stdin is not interactive; use --otp <code>")
        return None, "manual", ""
    end = time.time() + s["otp_timeout_sec"]
    log("waiting for OTP on device SMS (max %ds)" % s["otp_timeout_sec"])
    while time.time() < end:
        otp = extract_otp(adb.su_sh(SMS_SH, "sms.sh"))
        if otp:
            return otp, "device", ""
        time.sleep(4)
    return None, "device", ""


def random_name():
    firsts = ["Aarav", "Vivaan", "Aditya", "Vihaan", "Arjun", "Sai", "Ayaan", "Krishna", "Rohan",
              "Kabir", "Ishaan", "Ananya", "Diya", "Saanvi", "Aadhya", "Aarohi", "Myra", "Pari",
              "Navya", "Sara", "Anika", "Aisha", "Kiara", "Ira", "Reyansh", "Dhruv", "Shaurya", "Advik"]
    lasts = ["Sharma", "Verma", "Gupta", "Singh", "Mehta", "Nair", "Patel", "Reddy", "Iyer", "Menon",
             "Bose", "Das", "Kulkarni", "Joshi", "Chauhan", "Rathore", "Bajwa", "Khan", "Sheikh", "Rao"]
    return random.choice(firsts) + " " + random.choice(lasts)


def create_account(cfg, phone=None, order_id=None, name=None):
    adb = Adb(cfg)
    app = cfg["app"]
    s = cfg["signup"]
    ui = cfg["ui"]
    ensure_sms_script()

    provider = make_provider(cfg.get("otp_provider"))
    op = cfg.get("otp_provider") or {}
    if provider:
        log("using OTP provider: %s" % type(provider).__name__)

    if not name:
        name = s.get("name", "") or ""
    if s.get("auto_random", True) and not name:
        name = random_name()
        log("generated random name: %s" % name)

    def refund_if_rented():
        if order_id and hasattr(provider, "set_status"):
            log("cancelling order %s for refund" % order_id)
            cancel_async(provider, order_id)

    if phone is None:
        phone = s["phone"]
    if phone is None or str(phone).startswith("X"):
        if not provider:
            log("no provider configured and no phone set")
            return None
        log("renting+checking number from OTP provider")
        pool = rent_and_check(provider, cfg, 1, op.get("max_registered_retries", 6))
        if not pool:
            log("no unregistered number available")
            return None
        phone, order_id = pool[0]
        log("usable number %s (order %s)" % (phone, order_id))

    if s["clear_data_on_start"]:
        log("clearing app data (fresh state)")
        adb.clear_app(app["package"])
        time.sleep(2)

    if s.get("rotate_identity", True):
        if s.get("rotate_via", "qute") == "emulator":
            log("rotating full device identity via Device Emulator Pro (Random all)")
            if adb.emulator_rotate():
                log("Device Emulator Pro: random all applied")
            else:
                log("Device Emulator Pro randomize failed, falling back to root rotation")
                ids = adb.rotate_identity()
                log("root rotation: android_id=%s serialno=%s" % (ids["android_id"], ids["serialno"]))
        else:
            ids = adb.rotate_identity()
            log("identity rotated: android_id=%s serialno=%s" % (ids["android_id"], ids["serialno"]))
        if s.get("rotate_gsf", True):
            log("clearing Google Play Services (new GSF/Firebase id)")
            adb.su("am force-stop com.google.android.gms; pm clear com.google.android.gms")
            time.sleep(1)

    if not launch_swiggy(adb, app):
        refund_if_rented()
        return None

    login_ready = False
    end = time.time() + 90
    while time.time() < end and not login_ready:
        adb.dump_ui()
        nodes = ui_nodes()
        if find_node(nodes, desc="mobile number") or find_node(nodes, resource_id="phoneNumberField"):
            login_ready = True
            break
        skip = find_node(nodes, text="skip")
        if skip and tap_center(adb, skip):
            log("tapped skip")
            time.sleep(2)
            continue
        for kw in (["get started"], ["next"], ["allow"], ["got it"], ["continue"]):
            n = find_node(nodes, text=kw[0]) or find_node(nodes, desc=kw[0])
            if n and tap_center(adb, n):
                log("tapped %s" % kw[0])
                time.sleep(2)
                break
        else:
            if not adb.foreground(app["package"]):
                log("swiggy lost focus, relaunching")
                launch_swiggy(adb, app)
            time.sleep(2)
    if not login_ready:
        log("login screen not detected")
        adb.dump_ui()
        print("\n".join(str(n) for n in ui_nodes() if n["text"] or n["desc"]))
        refund_if_rented()
        return None

    log("login screen detected, entering phone %s" % phone)
    field = find_node(ui_nodes(), desc="mobile number")
    if not field:
        field = find_node(ui_nodes(), resource_id="phoneNumberField")
    if field:
        tap_center(adb, field)
    else:
        adb.tap(360, 440)
    time.sleep(1)
    adb.text(phone)
    time.sleep(1)
    adb.key(4)
    time.sleep(1)

    btn = None
    otp_ready = False
    end = time.time() + 15
    while time.time() < end:
        adb.dump_ui()
        nodes = ui_nodes()
        if find_node(nodes, text="Enter verification code") or find_node(nodes, desc="otp field"):
            otp_ready = True
            break
        btn = find_node(nodes, text="continue", enabled=True)
        if btn:
            break
        time.sleep(1)
    if otp_ready:
        log("OTP screen already open")
    elif btn:
        tap_center(adb, btn)
        log("continue tapped")
    else:
        log("continue button not enabled after phone entry")
        refund_if_rented()
        return None

    otp, src, raw = get_otp(adb, provider, op, phone, order_id, s)
    if not otp:
        if src == "provider":
            log("no OTP from provider. raw: " + (raw[:500] if raw else ""))
        elif src == "device":
            log("OTP not detected on device. raw SMS output:")
            print(adb.su_sh(SMS_SH, "sms.sh"))
        refund_if_rented()
        return None

    log("OTP obtained (%s): %s" % (src, otp))
    adb.dump_ui()
    otp_field = find_node(ui_nodes(), desc="otp field")
    if not otp_field:
        otp_field = find_node(ui_nodes(), desc="otp")
    if not otp_field:
        otp_field = find_node(ui_nodes(), desc="text field")
    if otp_field:
        tap_center(adb, otp_field)
    else:
        adb.tap(360, 700)
    time.sleep(1)
    adb.text(otp)
    time.sleep(3)

    if name:
        end = time.time() + 30
        while time.time() < end:
            adb.dump_ui()
            if "HomeActivity" in adb.focus():
                break
            nodes = ui_nodes()
            field = find_node(nodes, desc="text field") or find_node(nodes, desc="edit text")
            if field:
                tap_center(adb, field)
                time.sleep(1)
                adb.text(name)
                adb.key(4)
                time.sleep(1)
                adb.dump_ui()
                cont = find_node(ui_nodes(), text="continue", enabled=True) or find_node(ui_nodes(), text="next")
                if cont:
                    tap_center(adb, cont)
                break
            cont = find_node(nodes, text="continue", enabled=True) or find_node(nodes, text="next")
            if cont:
                tap_center(adb, cont)
                break
            time.sleep(2)

    if s["apply_referral"] and s["refer_code"]:
        adb.dump_ui()
        link = find_node(ui_nodes(), desc="refer") or find_node(ui_nodes(), text="refer")
        if link and tap_center(adb, link):
            time.sleep(2)
            adb.dump_ui()
            rf = find_node(ui_nodes(), desc="referral code") or find_node(ui_nodes(), text="referral code")
            if rf and tap_center(adb, rf):
                time.sleep(1)
                adb.text(s["refer_code"])
                adb.key(4)
                time.sleep(1)
                sub = find_node(ui_nodes(), text="apply") or find_node(ui_nodes(), desc="apply")
                if sub:
                    tap_center(adb, sub)

    f = wait_for_home(adb, app, 90)
    if app["home_activity"] in f:
        log("SUCCESS: account created, on home screen")
        if src == "provider" and provider and order_id and hasattr(provider, "set_status"):
            try:
                provider.set_status(order_id, 6)
                log("activation marked complete on provider")
            except Exception as e:
                log("could not complete provider activation: %s" % e)
            unregister_active_order(order_id)
        sess = None
        try:
            sess = capture_session(adb, cfg)
            if sess:
                log("session captured for %s (customerId %s)" % (phone, sess.get("customerId")))
        except Exception as e:
            log("session capture failed: %s" % e)
        if sess:
            account = {"token": sess.get("token", ""), "tid": sess.get("tid", ""),
                       "sid": sess.get("sid", ""), "deviceId": sess.get("deviceId", ""),
                       "customerId": sess.get("customerId", ""), "mobile": sess.get("mobile", "")}
            if not account["mobile"]:
                account["mobile"] = phone
            save_account(account)
            return account
        account = {"token": "", "tid": "", "sid": "", "deviceId": "",
                   "customerId": "", "mobile": phone}
        save_account(account)
        return account
    log("did not reach home. focus: " + f)
    sess = None
    try:
        sess = capture_session(adb, cfg)
    except Exception as e:
        log("session capture failed: %s" % e)
    if sess and sess.get("customerId"):
        log("session exists in prefs even though UI did not reach home -> saving account")
        if src == "provider" and provider and order_id and hasattr(provider, "set_status"):
            try:
                provider.set_status(order_id, 6)
                log("activation marked complete on provider")
            except Exception as e:
                log("could not complete provider activation: %s" % e)
            unregister_active_order(order_id)
        account = {"token": sess.get("token", ""), "tid": sess.get("tid", ""),
                   "sid": sess.get("sid", ""), "deviceId": sess.get("deviceId", ""),
                   "customerId": sess.get("customerId", ""), "mobile": sess.get("mobile", "") or phone}
        save_account(account)
        return account
    return None


def main():
    ap = argparse.ArgumentParser(description="Swiggy signup automation")
    ap.add_argument("--config", default=CONFIG_PATH)
    ap.add_argument("--dump-ui", action="store_true", help="dump current UI nodes and exit")
    ap.add_argument("--clear-only", action="store_true", help="only clear app data and exit")
    ap.add_argument("--provider-test", action="store_true", help="rent number and print raw provider responses")
    ap.add_argument("--phone", help="override phone number")
    ap.add_argument("--otp", help="pre-provided OTP (manual source)")
    ap.add_argument("--name", help="override account name")
    ap.add_argument("--no-random", action="store_true", help="do not auto-generate random name/email")
    ap.add_argument("--otp-source", choices=["manual", "device", "provider"], help="override OTP source")
    ap.add_argument("--keep-session", action="store_true", help="do not clear app data before starting")
    ap.add_argument("--count", type=int, default=1, help="number of accounts to create in this run")
    ap.add_argument("--pool", type=int, default=0,
                    help="rent+check this many numbers in parallel first (default: --count)")
    args = ap.parse_args()

    cfg = load_config(args.config)
    adb = Adb(cfg)

    if args.phone:
        cfg["signup"]["phone"] = args.phone
    if args.otp:
        cfg["signup"]["otp"] = args.otp
    if args.name:
        cfg["signup"]["name"] = args.name
    if args.no_random:
        cfg["signup"]["auto_random"] = False
    if args.otp_source:
        cfg["signup"]["otp_source"] = args.otp_source
    if args.keep_session:
        cfg["signup"]["clear_data_on_start"] = False

    if args.provider_test:
        op = cfg.get("otp_provider")
        if not op or not op.get("enabled"):
            print("otp_provider.enabled is false in config")
            return
        p = make_provider(op)
        phone, order = p.get_number()
        print("number=%s order_id=%s" % (phone, order))
        end = time.time() + op.get("max_wait_sec", 180)
        while time.time() < end:
            sms = p.fetch_otp(order) or ""
            otp = extract_otp(sms)
            print("raw=%r otp=%s" % (sms[:400], otp))
            if otp:
                break
            time.sleep(op.get("poll_interval_sec", 5))
        return

    if args.clear_only:
        adb.clear_app(cfg["app"]["package"])
        print("cleared " + cfg["app"]["package"])
        return

    if args.dump_ui:
        adb.dump_ui()
        for n in ui_nodes():
            if n["text"] or n["desc"]:
                print(n)
        return

    provider = make_provider(cfg.get("otp_provider"))
    if provider and args.pool and cfg["signup"].get("phone", "").startswith("X"):
        pool_size = args.pool if args.pool > 0 else args.count
        log("renting+checking %d numbers in parallel" % pool_size)
        pool = rent_and_check(provider, cfg, pool_size)
        log("usable numbers found: %d -> %s" % (len(pool), pool))
        created = 0
        attempted = 0
        for phone, order in pool:
            if created >= args.count:
                break
            attempted += 1
            log("=== account %d/%d using %s ===" % (created + 1, args.count, phone))
            acct = create_account(cfg, phone=phone, order_id=order)
            if acct:
                created += 1
            else:
                log("account creation failed for %s" % phone)
        log("waiting for background refunds...")
        wait_cancels()
        print("created %d/%d accounts (attempted %d numbers)" % (created, args.count, attempted))
        sys.exit(0 if created else 1)
    else:
        if args.count > 1:
            created = 0
            for i in range(args.count):
                log("=== account %d/%d ===" % (i + 1, args.count))
                try:
                    acct = create_account(cfg)
                except Exception as e:
                    log("account creation error: %s" % e)
                    acct = None
                if acct:
                    created += 1
            wait_cancels()
            print("created %d/%d accounts" % (created, args.count))
            sys.exit(0 if created else 1)
        else:
            try:
                acct = create_account(cfg)
            except Exception as e:
                log("account creation error: %s" % e)
                acct = None
            wait_cancels()
            sys.exit(0 if acct else 1)


if __name__ == "__main__":
    main()