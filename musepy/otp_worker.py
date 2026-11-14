"""Cloudflare Email Worker 自动接码客户端。"""

from datetime import datetime
import json
import os
import re
import time

from curl_cffi import requests as cr


def next_email(
    domain,
    prefix="muse",
    start=1,
    accounts_path="accounts.jsonl",
    seq_path=".email_seq.json",
):
    domain = domain.lstrip("@").strip()
    prefix = "muse" if prefix is None else str(prefix).strip()
    today = datetime.now().strftime("%y%m%d")
    key = f"{prefix}_{today}@{domain}"
    pattern = re.compile(rf"^{re.escape(prefix)}{today}_(\d+)@{re.escape(domain)}$")

    max_seq = max(0, int(start or 1) - 1)

    if os.path.exists(accounts_path):
        try:
            with open(accounts_path, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        email = json.loads(line).get("email", "")
                    except Exception:
                        continue
                    m = pattern.match(email)
                    if m:
                        max_seq = max(max_seq, int(m.group(1)))
        except Exception:
            pass

    seq_data = {}
    if os.path.exists(seq_path):
        try:
            with open(seq_path, encoding="utf-8") as f:
                seq_data = json.load(f) or {}
            max_seq = max(max_seq, int(seq_data.get(key, 0)))
        except Exception:
            seq_data = {}

    seq = max_seq + 1
    seq_data[key] = seq
    try:
        with open(seq_path, "w", encoding="utf-8") as f:
            json.dump(seq_data, f, ensure_ascii=False, indent=2)
    except Exception:
        pass

    return f"{prefix}{today}_{seq:02d}@{domain}"


def random_email(domain, prefix="muse", start=1):
    return next_email(domain, prefix=prefix, start=start)


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
