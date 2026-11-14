"""Cloudflare Email Worker 自动接码客户端。"""

import random
import string
import time

from curl_cffi import requests as cr


def random_email(domain, prefix="muse"):
    suffix = "".join(random.choices(string.ascii_lowercase + string.digits, k=8))
    return f"{prefix}_{suffix}@{domain.lstrip('@')}"


class WorkerOtpClient:
    def __init__(self, base_url, api_key, timeout=15):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.timeout = timeout

    def _headers(self):
        return {"Authorization": f"Bearer {self.api_key}"}

    def clear(self, email):
        try:
            cr.delete(
                f"{self.base_url}/otp",
                params={"to": email},
                headers=self._headers(),
                timeout=self.timeout,
            )
        except Exception:
            pass

    def poll_otp(self, email, after_ms=0, wait_seconds=90, interval=2.0):
        deadline = time.time() + wait_seconds
        while time.time() < deadline:
            try:
                r = cr.get(
                    f"{self.base_url}/otp",
                    params={"to": email, "after": str(after_ms or 0)},
                    headers=self._headers(),
                    timeout=self.timeout,
                )
                if r.status_code == 200:
                    data = r.json()
                    if data.get("found") and data.get("otp"):
                        return data["otp"], data
            except Exception:
                pass
            time.sleep(interval)
        return None, None
