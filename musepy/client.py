"""
muse.ai 注册与邀请码兑换协议客户端（纯 HTTP，无浏览器）

仅供协议研究与个人学习使用。使用前请自行确认目标站点的服务条款。
"""

import json
import random
import re
import time
import uuid

from curl_cffi import requests as cr

BASE = "https://muse.ai"

# onboarding 页面提交的 TOS 版本号，随站点更新可能变化
TOS_VERSION = "ntm_create_account:default_v1_meta_account_v3+docs_v2"

HEADERS_JSON = {
    "accept": "*/*",
    "content-type": "application/json",
    "origin": BASE,
    "referer": BASE + "/",
    "sec-fetch-site": "same-origin",
    "sec-fetch-mode": "cors",
    "sec-fetch-dest": "empty",
    "Upgrade-Insecure-Requests": None,
    "Sec-Fetch-User": None,
}
HEADERS_GET = {
    "accept": "*/*",
    "referer": BASE + "/",
    "sec-fetch-site": "same-origin",
    "sec-fetch-mode": "cors",
    "sec-fetch-dest": "empty",
    "Upgrade-Insecure-Requests": None,
    "Sec-Fetch-User": None,
}
HEADERS_HTML = {
    "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "upgrade-insecure-requests": "1",
    "sec-fetch-mode": "navigate",
    "sec-fetch-dest": "document",
}

# accept-tos 携带的披露文案（日文区）。不同 nux_type 对应不同文案。
DISCLOSURE_TEXT_JA = (
    "次に進むことで、Muse利用規約およびMetaのAI利用規約に同意するものとします。"
    "これには利用者の権利と責任に関する重要な情報が含まれています。"
    "プライバシーに関するMuseのアプローチについて詳しくはMuseプライバシーポリシーをご覧ください。"
)


class MuseError(RuntimeError):
    """协议流程中的可预期失败"""


class MuseClient:
    """一个账号 = 一个 Session（一个出口 IP）"""

    def __init__(self, proxy=None, impersonate="chrome145", lang="ja", timeout=30):
        self.fp = impersonate
        self.lang = lang
        self.proxy = proxy
        self.timeout = timeout
        self.s = cr.Session(impersonate=impersonate,
                            proxies={"http": proxy, "https": proxy} if proxy else None,
                            timeout=timeout)
        self.csrf = None
        self.waterfall_id = _uuid()
        self.home_url = BASE + "/"
        self.requires_name = None
        self.tos_version = None
        self.account_id = None

    # ---------- 基础设施 ----------

    def _accept_language(self):
        if self.lang == "ja":
            return "ja-JP,ja;q=0.9,en-US;q=0.8,en;q=0.7"
        return self.lang

    def _region(self):
        if "-" in self.lang:
            part = self.lang.split("-", 1)[1].split(",")[0].split(";")[0].strip()
            if len(part) == 2 and part.isalpha():
                return part.upper()
        if self.lang.lower().startswith("ja"):
            return "JP"
        return None

    def _h(self, base, extra=None):
        h = dict(base)
        if "referer" in h:
            h["referer"] = self.home_url
        h["accept-language"] = self._accept_language()
        if extra:
            h.update(extra)
        return h

    def _json(self, resp):
        try:
            return resp.json() or {}
        except Exception:
            return {}

    def close(self):
        try:
            self.s.close()
        except Exception:
            pass

    # ---------- 注册流程 ----------

    def open_home(self):
        r = self.s.get(BASE + "/", impersonate=self.fp,
                       headers=self._h(HEADERS_HTML))
        if r.url:
            self.home_url = r.url
        self.s.cookies.set("wd", "1440x900", domain="muse.ai", path="/")
        self.s.cookies.set("dpr", "2", domain="muse.ai", path="/")
        try:
            self.s.get(BASE + "/api/consent/status", impersonate=self.fp,
                       headers=self._h(HEADERS_GET))
        except Exception:
            pass
        return r

    def restart(self, resume=False):
        """开启注册流程，返回 csrf_token"""
        payload = json.dumps({"resume": True}) if resume else "{}"
        r = self.s.post(BASE + "/api/auth/native/restart", impersonate=self.fp,
                        headers=self._h(HEADERS_JSON, {"X-Hatch-Waterfall-Id": self.waterfall_id}),
                        data=payload)
        if r.status_code != 201:
            raise MuseError(f"restart {r.status_code}: {r.text[:120]}")
        self.csrf = self._json(r).get("csrf_token")
        if not self.csrf:
            raise MuseError("restart 未返回 csrf_token")
        return self.csrf

    def _auth_headers(self):
        h = self._h(HEADERS_JSON)
        if self.csrf:
            h["X-Hatch-CSRF-Token"] = self.csrf
            h["X-Hatch-Waterfall-Id"] = self.waterfall_id
            h["X-Hatch-Caa-Reg-Entry-Point"] = "login_home"
        return h

    def send_otp(self, email, region=None):
        body = {"contact_point": email}
        reg = region or self._region()
        if reg:
            body["region"] = reg
        r = self.s.post(BASE + "/api/auth/native/send-otp", impersonate=self.fp,
                        headers=self._auth_headers(), data=json.dumps(body))
        if r.status_code not in (200, 201):
            raise MuseError(f"send-otp {r.status_code}: {r.text[:120]}")
        return self._json(r)

    def confirm_otp(self, code):
        """关键：supports_muse_link_transparency 必须为 true"""
        r = self.s.post(BASE + "/api/auth/native/confirm-otp", impersonate=self.fp,
                        headers=self._auth_headers(),
                        data=json.dumps({"otp_code": code,
                                         "remember_device": True,
                                         "supports_muse_link_transparency": True}))
        if r.status_code != 200:
            raise MuseError(f"confirm-otp {r.status_code}: {r.text[:120]}")
        data = self._json(r)
        self.requires_name = bool(data.get("requires_name"))
        tos_ver = (data.get("tos") or {}).get("content_version")
        if tos_ver:
            self.tos_version = tos_ver
        return data

    def register_completion(self, date_of_birth, first_name=None, last_name=None, remember_device=True):
        """
        完成注册。注意字段语义：
          first_name = 名（given name）
          last_name  = 姓（family name）
        仅当服务端 confirm-otp 返回 requires_name=true 时才需要传姓名。
        生日早于约 18 年前会被服务端拒绝（403）。
        """
        body = {
            "date_of_birth": date_of_birth,
            "tos_content_version": self.tos_version or TOS_VERSION,
            "remember_device": remember_device,
        }
        need_name = True if self.requires_name is None else self.requires_name
        if need_name and first_name and last_name:
            body["first_name"] = first_name
            body["last_name"] = last_name
        r = self.s.post(BASE + "/api/auth/native/register-completion", impersonate=self.fp,
                        headers=self._auth_headers(), data=json.dumps(body))
        if r.status_code != 200:
            raise MuseError(f"register-completion {r.status_code}: {r.text[:120]}")
        data = self._json(r)
        self.account_id = (data.get("auth_meta_session") or {}).get("frl_account_id")
        return data

    def session_cookie(self, name="hatch_sess"):
        for c in self.s.cookies.jar:
            if c.name == name:
                return c.value
        return None

    # ---------- 激活 ----------

    def accept_tos(self):
        """必须调用，否则后续 hatch 接口一律 403"""
        body = {
            "saw_meta_ai_tos": False,
            "client_request_timestamp": str(int(time.time())),
            "entry_point": "web_onboarding_flow",
            "nux_type": "ROW",
            "disclosure_version": "HATCH_ROW_v0",
            "disclosure_text": DISCLOSURE_TEXT_JA,
        }
        return self.s.post(BASE + "/api/hatch/accept-tos", impersonate=self.fp,
                           headers=self._h(HEADERS_JSON), data=json.dumps(body))

    def invite(self):
        return self.s.get(BASE + "/api/hatch/invite", impersonate=self.fp,
                          headers=self._h(HEADERS_GET))

    def is_live(self):
        """invite 200 = 有效账号；401/403 = 无 hatch 权限"""
        try:
            return self.invite().status_code == 200
        except Exception:
            return False

    def wake_vm(self):
        """必须调用，否则兑换会返回误导性的 invalid"""
        r = self.s.get(BASE + "/?aymh_complete=1", impersonate=self.fp,
                       headers=self._h(HEADERS_HTML))
        m = re.search(r'vmId[\\"]*:[\\"]*([0-9a-fA-F-]{36})', r.text)
        if not m:
            return None
        vm_id = m.group(1)
        self.s.post(BASE + "/api/hatch/vm/wake", impersonate=self.fp,
                    headers=self._h(HEADERS_JSON),
                    data=json.dumps({"vm_id": vm_id,
                                     "retry_count": 0,
                                     "connect_attempt_id": _uuid(),
                                     "wake_trigger": "resume"}))
        return vm_id

    def stateful_sharing(self):
        return self.s.post(BASE + "/api/hatch/stateful-sharing", impersonate=self.fp,
                           headers=self._h(HEADERS_JSON), data="")

    # ---------- 兑换 ----------

    def redeem(self, code):
        """
        兑换邀请码。返回 (ok, reason)。
          reason: redeemed | already_redeemed | used_up | invalid
        """
        r = self.s.post(BASE + "/api/hatch/invite-code/redeem", impersonate=self.fp,
                        headers=self._h(HEADERS_JSON),
                        data=json.dumps({"code": code, "supportsRedemptionStatus": True}))
        data = self._json(r)
        if r.status_code == 200 and (data.get("success") is True or data.get("redemptionStatus") == "redeemed"):
            return True, "redeemed"
        return False, data.get("reason") or f"http-{r.status_code}"

    def scratch_code(self):
        """兑换成功后刮出自己的邀请码"""
        r = self.s.post(BASE + "/api/hatch/invite", impersonate=self.fp,
                        headers=self._h(HEADERS_JSON), data="")
        if r.status_code != 200:
            return None
        return self._json(r).get("code")


def _uuid():
    return str(uuid.uuid4())


def random_dob(min_year=1970, max_year=1999):
    return (f"{random.randint(min_year, max_year)}-"
            f"{random.randint(1, 12):02d}-"
            f"{random.randint(1, 28):02d}")


_FIRST_NAMES = (
    "Alex", "Liam", "Noah", "Ethan", "Lucas", "Mason", "Logan", "James",
    "Emma", "Olivia", "Sophia", "Chloe", "Grace", "Hannah", "Emily", "Mia",
)
_LAST_NAMES = (
    "Smith", "Miller", "Davis", "Wilson", "Clark", "Taylor", "Moore", "Walker",
    "Hall", "Young", "Allen", "King", "Wright", "Scott", "Green", "Baker",
)


def random_name():
    return random.choice(_FIRST_NAMES), random.choice(_LAST_NAMES)

