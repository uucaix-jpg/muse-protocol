"""
真实 Chromium 浏览器自动注册 + Cloudflare 自动接码 + 邀请码兑换脚本。
同时用于对比纯 HTTP 协议请求与真实浏览器环境，并在成功或失败时始终落盘 .last_browser_trace.json。
"""

import argparse
import json
import os
import platform
import re
import sys
import time
import uuid

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from playwright.sync_api import sync_playwright

from musepy.client import DISCLOSURE_TEXT_JA, random_dob, random_name
from musepy.otp_worker import WorkerOtpClient, next_email


def load_config(path="config.json"):
    if not os.path.exists(path):
        raise SystemExit(f"缺少配置文件 {path}，请从 config.example.json 复制并填写")
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def resolve_email(cfg):
    email = (cfg.get("email") or "").strip()
    if email and email != "your-mailbox@example.com":
        return email
    domain = (cfg.get("email_domain") or "").strip()
    if domain:
        prefix = cfg.get("email_prefix", "muse")
        start = cfg.get("email_start", 1)
        return next_email(domain, prefix=prefix, start=start)
    raise SystemExit("请在 config.json 中填写 email 或 email_domain")


def save_account(record, path="accounts.jsonl"):
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


def browser_api(page, method, path, body=None):
    return page.evaluate(
        """async ({method, path, body}) => {
            const opts = {method, credentials: "include", headers: {}};
            if (body !== null && body !== undefined) {
                opts.headers["Content-Type"] = "application/json";
                opts.body = typeof body === "string" ? body : JSON.stringify(body);
            }
            const r = await fetch(path, opts);
            const text = await r.text();
            let json = null;
            try { json = JSON.parse(text); } catch {}
            return {status: r.status, ok: r.ok, url: r.url, text, json};
        }""",
        {"method": method, "path": path, "body": body},
    )


def parse_args(argv=None):
    p = argparse.ArgumentParser(description="muse.ai 真实浏览器自动注册与会话探测")
    p.add_argument("--probe", action="store_true", help="输出上次 trace 并用已保存会话探测 /access 与 /invite 路由")
    p.add_argument("--zh", action="store_true", help="使用 zh-CN 且不覆盖时区（回归对照 c9fb8bd 行为）")
    p.add_argument("--ja", action="store_true", help="使用 ja-JP 与 Asia/Tokyo")
    p.add_argument("--locale", default=None, help="覆盖浏览器 locale（例如 zh-CN / ja-JP / en-US）")
    p.add_argument("--tz", default=None, help="覆盖浏览器 timezone_id（传 none 表示沿用系统默认时区）")
    p.add_argument("--mac-ua", action="store_true", help="强制使用 Macintosh UA（默认匹配宿主机 OS，避免与 Client Hints 冲突）")
    p.add_argument("--headless-shell", action="store_true", help="使用 Playwright 默认 headless_shell（默认优先尝试 channel=chromium）")
    p.add_argument("--headed", action="store_true", help="以有界面模式启动浏览器")
    return p.parse_args(argv)


def resolve_locale(cfg, args=None):
    if args is not None:
        if getattr(args, "locale", None):
            return args.locale.strip()
        if getattr(args, "zh", False):
            return "zh-CN"
        if getattr(args, "ja", False):
            return "ja-JP"
    loc = (cfg.get("browser_locale") or "").strip()
    if loc:
        return loc
    lang = (cfg.get("lang") or "ja").strip()
    low = lang.lower()
    if low in ("ja", "ja-jp"):
        return "ja-JP"
    if low in ("zh", "zh-cn"):
        return "zh-CN"
    if low in ("en", "en-us"):
        return "en-US"
    if "-" in lang or "_" in lang:
        return lang.replace("_", "-")
    return "ja-JP"


def resolve_timezone(cfg, browser_locale, args=None):
    if args is not None:
        tz_arg = getattr(args, "tz", None)
        if tz_arg is not None:
            tz_s = tz_arg.strip()
            return None if tz_s.lower() in ("", "none", "default") else tz_s
        if getattr(args, "zh", False):
            return None
        if getattr(args, "ja", False):
            return "Asia/Tokyo"
    tz = (cfg.get("browser_timezone") or "").strip()
    if tz:
        return None if tz.lower() in ("none", "default") else tz
    low = (browser_locale or "").lower()
    if low.startswith("ja"):
        return "Asia/Tokyo"
    if low.startswith("en-us"):
        return "America/Los_Angeles"
    return None


def _chrome_ua(browser, mac_ua=False, system=None):
    major = (getattr(browser, "version", "") or "131").split(".")[0] or "131"
    if mac_ua:
        plat = "Macintosh; Intel Mac OS X 10_15_7"
    else:
        sysname = system or platform.system()
        if sysname == "Darwin":
            plat = "Macintosh; Intel Mac OS X 10_15_7"
        elif sysname == "Windows":
            plat = "Windows NT 10.0; Win64; x64"
        else:
            plat = "X11; Linux x86_64"
    return (
        f"Mozilla/5.0 ({plat}) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        f"Chrome/{major}.0.0.0 Safari/537.36"
    )


def _launch_browser(p, proxy=None, headed=False, use_headless_shell=False):
    base_kwargs = {
        "headless": not headed,
        "args": [
            "--disable-blink-features=AutomationControlled",
            "--no-sandbox",
            "--disable-dev-shm-usage",
        ],
    }
    if proxy:
        base_kwargs["proxy"] = {"server": proxy}

    if not headed and not use_headless_shell:
        try:
            b = p.chromium.launch(channel="chromium", **base_kwargs)
            return b, "channel:chromium"
        except Exception:
            pass
    b = p.chromium.launch(**base_kwargs)
    return b, ("headed" if headed else "headless-shell")


def _extract_diag_headers(all_headers):
    if not all_headers:
        return {}
    low = {str(k).lower(): v for k, v in all_headers.items()}
    out = {}
    for hk, label in (
        ("x-hatch-auth-check-id", "auth_check_id"),
        ("x-vercel-id", "vercel_id"),
        ("cf-ray", "cf_ray"),
    ):
        if low.get(hk):
            out[label] = low[hk]
    return out


def _format_diag(diag):
    if not diag:
        return ""
    parts = [f"{k}={v}" for k, v in diag.items() if v]
    return f" ({', '.join(parts)})" if parts else ""


def _capture_nav_env(page):
    try:
        return page.evaluate(
            """async () => {
                let uad = null;
                try {
                    if (navigator.userAgentData) {
                        const he = await navigator.userAgentData.getHighEntropyValues(["platform", "platformVersion", "fullVersionList"]);
                        uad = {
                            mobile: navigator.userAgentData.mobile,
                            platform: navigator.userAgentData.platform,
                            brands: navigator.userAgentData.brands,
                            hePlatform: he.platform,
                            hePlatformVersion: he.platformVersion,
                        };
                    }
                } catch {}
                let tz = null;
                try { tz = Intl.DateTimeFormat().resolvedOptions().timeZone; } catch {}
                return {
                    userAgent: navigator.userAgent,
                    platform: navigator.platform,
                    language: navigator.language,
                    languages: navigator.languages,
                    webdriver: navigator.webdriver,
                    timezone: tz,
                    userAgentData: uad,
                };
            }"""
        )
    except Exception:
        return None


def _fill_birthday(page, dob_year, dob_month, dob_day):
    try:
        combos = page.locator('button[role="combobox"]')
        count = combos.count()
        used = set()

        def pick(matcher, select_fn):
            for i in range(count):
                if i in used:
                    continue
                cb = combos.nth(i)
                aria = (cb.get_attribute("aria-label") or "").strip()
                txt = cb.inner_text().strip()
                if matcher(aria, txt):
                    used.add(i)
                    cb.click()
                    select_fn()
                    return True
            return False

        # Year -> Month -> Day，避免月末天数校验截断
        picked_year = pick(
            lambda aria, txt: bool(re.match(r"^(year|年|西暦)$", aria, re.I))
            or (len("".join(c for c in txt if c.isdigit())) == 4 and int("".join(c for c in txt if c.isdigit())) >= 1900),
            lambda: page.get_by_role("option", name=dob_year, exact=True).click(timeout=5000),
        )
        picked_month = pick(
            lambda aria, txt: bool(re.match(r"^(month|月|月份)$", aria, re.I))
            or ("月" in txt)
            or (
                picked_year
                and not (txt.isdigit() and 1 <= int(txt) <= 31)
                and not (len("".join(c for c in txt if c.isdigit())) == 4 and int("".join(c for c in txt if c.isdigit())) >= 1900)
            ),
            lambda: page.locator('[role="option"]').nth(int(dob_month) - 1).click(timeout=5000),
        )
        pick(
            lambda aria, txt: bool(re.match(r"^(day|日)$", aria, re.I))
            or (txt.isdigit() and 1 <= int(txt) <= 31)
            or picked_month,
            lambda: page.get_by_role("option", name=str(int(dob_day)), exact=True).click(timeout=5000),
        )
    except Exception:
        try:
            page.keyboard.press("Escape")
        except Exception:
            pass


def _write_trace(state, context, page, trace_path=".last_browser_trace.json"):
    try:
        page_url = None
        try:
            page_url = page.url
        except Exception:
            pass
        cookies = []
        try:
            cookies = [
                {"name": ck["name"], "domain": ck.get("domain"), "path": ck.get("path"), "len": len(ck.get("value") or "")}
                for ck in context.cookies()
            ]
        except Exception:
            pass
        payload = {
            "page_url": page_url,
            "launch_mode": state.get("launch_mode"),
            "locale": state.get("locale"),
            "timezone": state.get("timezone"),
            "stage_error": state.get("stage_error"),
            "nav_env": state.get("nav_env"),
            "diag": {
                "send_otp": state.get("send_otp_diag"),
                "confirm_otp": state.get("confirm_diag"),
                "register_completion": state.get("reg_diag"),
            },
            "post_reg_navs": state["post_reg_navs"],
            "cookies": cookies,
            "trace": state["trace"],
        }
        with open(trace_path, "w", encoding="utf-8") as tf:
            json.dump(payload, tf, ensure_ascii=False, indent=2)
        return True
    except Exception:
        return False


def _fetch_ip_info(proxy=None):
    try:
        from curl_cffi import requests as cr
        proxies = {"http": proxy, "https": proxy} if proxy else None
        r = cr.get("https://ipinfo.io/json", proxies=proxies, timeout=6)
        if r.status_code == 200:
            d = r.json() or {}
            return f"ip={d.get('ip')} country={d.get('country')} city={d.get('city')} org={d.get('org')}"
    except Exception:
        pass
    return None


def main(args=None):
    if args is None:
        args = parse_args()

    cfg = load_config()
    email = resolve_email(cfg)
    proxy = (cfg.get("proxy") or "").strip() or None
    invite_code = (cfg.get("invite_code") or "").strip()
    browser_locale = resolve_locale(cfg, args)
    browser_tz = resolve_timezone(cfg, browser_locale, args)
    use_headless_shell = bool(args.headless_shell or cfg.get("browser_channel") == "headless-shell")

    rand_first, rand_last = random_name()
    first = (cfg.get("first_name") or "").strip() or rand_first
    last = (cfg.get("last_name") or "").strip() or rand_last
    dob = (cfg.get("date_of_birth") or "").strip() or random_dob()
    dob_year, dob_month, dob_day = dob.split("-")

    worker_cfg = cfg.get("otp_worker") or {}
    otp_client = None
    if worker_cfg.get("url") and worker_cfg.get("api_key"):
        otp_client = WorkerOtpClient(worker_cfg["url"], worker_cfg["api_key"])

    state = {
        "uid": None,
        "launch_mode": None,
        "locale": browser_locale,
        "timezone": browser_tz or "default",
        "nav_env": None,
        "stage_error": None,
        "send_otp_status": None,
        "send_otp_body": None,
        "send_otp_diag": {},
        "confirm_status": None,
        "confirm_body": None,
        "confirm_diag": {},
        "reg_status": None,
        "reg_body": None,
        "reg_diag": {},
        "confirm_data": {},
        "post_reg_navs": [],
        "post_reg_loaded": False,
        "hatch_logs": [],
        "trace": [],
    }

    with sync_playwright() as p:
        browser, launch_mode = _launch_browser(
            p,
            proxy=proxy,
            headed=args.headed,
            use_headless_shell=use_headless_shell,
        )
        state["launch_mode"] = launch_mode
        ua = (cfg.get("browser_ua") or "").strip() or _chrome_ua(browser, mac_ua=args.mac_ua)
        ctx_kwargs = {
            "locale": browser_locale,
            "viewport": {"width": 1440, "height": 900},
            "device_scale_factor": 2,
            "user_agent": ua,
        }
        if browser_tz:
            ctx_kwargs["timezone_id"] = browser_tz
        context = browser.new_context(**ctx_kwargs)
        context.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', {get: () => undefined});"
        )
        page = context.new_page()

        try:
            def on_route(route):
                req = route.request
                if req.method == "POST" and "/api/auth/native/register-completion" in req.url:
                    try:
                        body = json.loads(req.post_data or "{}")
                        body["date_of_birth"] = dob
                        if state["confirm_data"].get("requires_name", True):
                            body.setdefault("first_name", first)
                            body.setdefault("last_name", last)
                        route.continue_(post_data=json.dumps(body))
                        return
                    except Exception:
                        pass
                route.continue_()

            page.route("**/api/auth/native/register-completion", on_route)

            def _is_traced(url):
                return (
                    ("muse.ai" in url or "auth.meta.com" in url)
                    and (
                        "/api/" in url
                        or "/hatch/" in url
                        or "/aymh/" in url
                        or "/oidc/" in url
                        or "/access" in url
                        or url.rstrip("/") in ("https://muse.ai", "https://muse.ai/?aymh_complete=1")
                    )
                )

            def on_request(req):
                url = req.url
                if state["reg_status"] == 200 and req.is_navigation_request() and req.frame == page.main_frame:
                    state["post_reg_navs"].append(f"{req.method} {url}")
                if _is_traced(url):
                    try:
                        hdrs = dict(req.headers)
                        cookie_raw = hdrs.pop("cookie", "")
                        cookie_names = [kv.split("=", 1)[0].strip() for kv in cookie_raw.split(";") if "=" in kv]
                        if "x-hatch-csrf-token" in hdrs:
                            hdrs["x-hatch-csrf-token"] = f"<len:{len(hdrs['x-hatch-csrf-token'])}>"
                        post = req.post_data
                        if post and "frl_access_token=" in post:
                            post = "<form frl_access_token masked>"
                        elif post and '"otp_code"' in post:
                            try:
                                pj = json.loads(post)
                                if "otp_code" in pj:
                                    pj["otp_code"] = f"<len:{len(str(pj['otp_code']))}>"
                                post = json.dumps(pj, ensure_ascii=False)
                            except Exception:
                                pass
                        state["trace"].append({
                            "t": round(time.time(), 3),
                            "method": req.method,
                            "url": url,
                            "type": req.resource_type,
                            "nav": req.is_navigation_request(),
                            "cookie_names": sorted(set(cookie_names)),
                            "headers": hdrs,
                            "post": post,
                        })
                    except Exception:
                        pass

            page.on("request", on_request)

            def on_load():
                if state["reg_status"] == 200 and "muse.ai" in page.url and "/api/" not in page.url:
                    state["post_reg_loaded"] = True

            page.on("load", on_load)

            def on_response(resp):
                url = resp.url
                ah = {}
                diag = {}
                if _is_traced(url):
                    try:
                        ah = resp.all_headers()
                        diag = _extract_diag_headers(ah)
                    except Exception:
                        ah = {}
                    for item in reversed(state["trace"]):
                        if item["url"] == url and "status" not in item:
                            item["status"] = resp.status
                            if diag:
                                item["resp_headers"] = diag
                            sc = ah.get("set-cookie", "")
                            if sc:
                                item["set_cookies"] = [ln.split("=", 1)[0].strip() for ln in sc.splitlines() if "=" in ln]
                            if resp.status >= 400 and ("/api/auth/" in url or "/api/hatch/" in url):
                                try:
                                    item["resp_body"] = resp.text()[:400]
                                except Exception:
                                    pass
                            break
                if "/api/auth/native/send-otp" in url:
                    state["send_otp_status"] = resp.status
                    state["send_otp_diag"] = diag
                    try:
                        state["send_otp_body"] = resp.text()[:200]
                    except Exception:
                        pass
                elif "/api/auth/native/confirm-otp" in url:
                    state["confirm_status"] = resp.status
                    state["confirm_diag"] = diag
                    try:
                        state["confirm_data"] = resp.json() or {}
                    except Exception:
                        try:
                            state["confirm_body"] = resp.text()[:200]
                        except Exception:
                            pass
                elif "/api/auth/native/register-completion" in url:
                    state["reg_status"] = resp.status
                    state["reg_diag"] = diag
                    try:
                        data = resp.json() or {}
                        state["reg_body"] = data
                        state["uid"] = (data.get("auth_meta_session") or {}).get("frl_account_id")
                    except Exception:
                        state["reg_body"] = resp.text()
                elif state["reg_status"] == 200 and "/api/hatch/" in url:
                    state["hatch_logs"].append(f"{resp.request.method} {url.split('muse.ai')[-1]} -> {resp.status}")

            page.on("response", on_response)

            print(
                f"[1] 真实浏览器打开首页  {email}  "
                f"(mode={launch_mode}, locale={browser_locale}, tz={browser_tz or 'default'})"
            )
            page.goto("https://muse.ai/", wait_until="domcontentloaded", timeout=60000)

            email_input = page.locator('input[autocomplete="username webauthn"], input[inputmode="email"]').first
            email_input.wait_for(state="visible", timeout=45000)
            state["nav_env"] = _capture_nav_env(page)
            time.sleep(1.5)

            sent_after_ms = int(time.time() * 1000)
            if otp_client:
                otp_client.clear(email)

            print("[2] 输入邮箱并发送验证码")
            email_input.click()
            email_input.fill(email)
            time.sleep(0.5)
            page.locator('form button[type="submit"]').first.click()

            # OTP 页面的真实输入框带 sr-only，等待可见的 6 格验证码区域（若 send-otp 报错则立即失败）
            pin_digit = page.locator('[data-slot="pin-digit"]').first
            for _ in range(60):
                if pin_digit.is_visible():
                    break
                if state["send_otp_status"] and state["send_otp_status"] >= 400:
                    msg = f"发送验证码失败: send-otp {state['send_otp_status']}{_format_diag(state['send_otp_diag'])}: {state['send_otp_body']}"
                    state["stage_error"] = msg
                    raise SystemExit(msg + "\n完整 trace 已保存到 .last_browser_trace.json")
                page.wait_for_timeout(500)
            else:
                pin_digit.wait_for(state="visible", timeout=1000)

            if otp_client:
                print("    正在通过 Cloudflare Email Worker 自动等待验证码...")
                code, _ = otp_client.poll_otp(email, after_ms=sent_after_ms, wait_seconds=90, interval=2.0)
                if not code:
                    code = input("自动接码超时，请手动输入收到的 6 位验证码: ").strip()
                else:
                    print(f"    自动获取到验证码 = {code}")
            else:
                code = input("输入收到的 6 位验证码: ").strip()

            print("[3] 填写并校验验证码")
            page.locator('[data-slot="pin-digit"]').first.click()
            page.keyboard.type(code, delay=80)

            # 等待确认并跳转至生日（和可选姓名）页面（若 confirm-otp 报错则立即失败）
            first_combo = page.locator('button[role="combobox"]').first
            for _ in range(60):
                if first_combo.is_visible():
                    break
                if state["confirm_status"] and state["confirm_status"] >= 400:
                    err = state["confirm_data"] or state["confirm_body"]
                    msg = f"校验验证码失败: confirm-otp {state['confirm_status']}{_format_diag(state['confirm_diag'])}: {err}"
                    state["stage_error"] = msg
                    raise SystemExit(msg + "\n完整 trace 已保存到 .last_browser_trace.json")
                page.wait_for_timeout(500)
            else:
                first_combo.wait_for(state="visible", timeout=1000)

            given_input = page.locator('input[autocomplete="given-name"]')
            if given_input.count() > 0 and given_input.first.is_visible():
                print(f"[4] 填写姓名与生日  {first} {last}  {dob}")
                given_input.first.fill(first)
                page.locator('input[autocomplete="family-name"]').first.fill(last)
            else:
                print(f"[4] 填写生日  {dob}")

            _fill_birthday(page, dob_year, dob_month, dob_day)

            print("[5] 提交完成注册")
            submit_btn = page.locator('form button[type="submit"]').first
            submit_btn.click(timeout=30000)

            # 如果弹出条款二次确认框，点击确认按钮
            for _ in range(30):
                if state["reg_status"] is not None:
                    break
                dialog_btn = page.locator('[role="dialog"] button[type="button"], [role="alertdialog"] button').last
                if dialog_btn.count() > 0 and dialog_btn.is_visible():
                    try:
                        dialog_btn.click()
                    except Exception:
                        pass
                page.wait_for_timeout(500)

            if state["reg_status"] != 200:
                msg = f"注册失败: register-completion {state['reg_status']}{_format_diag(state['reg_diag'])}: {state['reg_body']}"
                state["stage_error"] = msg
                raise SystemExit(msg + "\n完整 trace 已保存到 .last_browser_trace.json")

            uid = state["uid"]
            print(f"    注册成功! account_id = {uid}")

            # 等待 auth.meta.com/hatch/session/bootstrap/ 表单跳转回 muse.ai 完成会话同步
            print("    正在等待会话同步跳转回 muse.ai...")
            left_muse = False
            for step in range(40):
                cur_url = page.url
                if "muse.ai" not in cur_url:
                    left_muse = True
                if state["post_reg_loaded"] and "muse.ai" in cur_url and "/api/" not in cur_url:
                    break
                if left_muse and "muse.ai" in cur_url and "/api/" not in cur_url:
                    break
                if step >= 10 and not state["post_reg_navs"] and "muse.ai" in cur_url:
                    break
                page.wait_for_timeout(500)
            try:
                page.wait_for_load_state("domcontentloaded", timeout=15000)
            except Exception:
                pass
            page.wait_for_timeout(1500)
            if state["post_reg_navs"]:
                print(f"    同步跳转链 ({len(state['post_reg_navs'])} 跳) -> {page.url}")
            else:
                print(f"    当前页面: {page.url}")

            html_sync = page.content().replace('\\"', '"')
            geo_m = re.search(r'"isGeoGated"\s*:\s*(true|false)', html_sync)
            is_geogated = geo_m.group(1) == "true" if geo_m else None
            if is_geogated:
                ip_info = _fetch_ip_info(proxy)
                print(f"    ⚠️ 服务端返回 isGeoGated=true (exit: {ip_info or 'unknown'})")

            # 在同一浏览器会话内执行激活、唤醒、兑换
            print("[6] 激活会话（accept-tos）")
            inv = browser_api(page, "GET", "/api/hatch/invite")
            if inv["status"] != 200:
                for attempt in range(2):
                    tos_res = browser_api(
                        page,
                        "POST",
                        "/api/hatch/accept-tos",
                        {
                            "saw_meta_ai_tos": False,
                            "client_request_timestamp": str(int(time.time())),
                            "entry_point": "web_onboarding_flow",
                            "nux_type": "ROW",
                            "disclosure_version": "HATCH_ROW_v0",
                            "disclosure_text": DISCLOSURE_TEXT_JA,
                        },
                    )
                    print(f"    accept-tos[{attempt+1}] = {tos_res['status']} {tos_res['text'][:160]}")
                    inv = browser_api(page, "GET", "/api/hatch/invite")
                    if inv["status"] == 200:
                        break
                    time.sleep(1.2)
            live = inv["status"] == 200
            inv_desc = "200 活号" if live else f"{inv['status']} ({inv['text'][:120]})"
            print(f"    invite = {inv_desc}")
            if not live:
                try:
                    btns = [b.strip() for b in page.locator("button:visible").all_inner_texts() if b.strip()][:8]
                    body_txt = re.sub(r"\s+", " ", page.locator("body").inner_text()).strip()[:160]
                    print(f"    页面调试: isGeoGated={is_geogated} buttons={btns} auto_hatch={state['hatch_logs']} text={body_txt}")
                except Exception:
                    pass

            print("[7] 唤醒 VM")
            html = page.content()
            m = re.search(r'(?:vmId|vm_id)[\\"]*:[\\"]*([0-9a-fA-F-]{36})', html)
            home_status = None
            if not m:
                home_res = browser_api(page, "GET", "/?aymh_complete=1")
                home_status = home_res["status"]
                m = re.search(r'(?:vmId|vm_id)[\\"]*:[\\"]*([0-9a-fA-F-]{36})', home_res["text"])
            vm = m.group(1) if m else None
            if vm:
                wake_res = browser_api(
                    page,
                    "POST",
                    "/api/hatch/vm/wake",
                    {
                        "vm_id": vm,
                        "retry_count": 0,
                        "connect_attempt_id": str(uuid.uuid4()),
                        "wake_trigger": "resume",
                    },
                )
                print(f"    vm_id = {vm} (wake={wake_res['status']})")
            else:
                print(f"    vm_id = None (aymh_home={home_status})")

            browser_api(page, "POST", "/api/hatch/stateful-sharing", "")

            own = None
            print(f"[8] 兑换邀请码  {invite_code or '(未配置)'}")
            if invite_code:
                red = browser_api(
                    page,
                    "POST",
                    "/api/hatch/invite-code/redeem",
                    {"code": invite_code, "supportsPendingRedemption": True},
                )
                data = red["json"] or {}
                status_val = data.get("redemptionStatus")
                ok = red["status"] == 200 and (data.get("success") is True or status_val == "redeemed")
                if ok and status_val == "pending":
                    ok = False
                    reason = "pending"
                else:
                    reason = "redeemed" if ok else (data.get("reason") or f"http-{red['status']}: {red['text'][:160]}")
                print(f"    redeem = {ok} / {reason}")
                if ok and reason == "redeemed":
                    sc = browser_api(page, "POST", "/api/hatch/invite", "")
                    if sc["status"] == 200 and sc["json"]:
                        own = sc["json"].get("code")
                    print(f"[9] 自己的邀请码 = {own}")
                elif reason == "pending":
                    print("    邀请码已提交为待处理状态（pending），暂不可刮取新码")
                else:
                    print("    失败原因映射: already_redeemed=本账号已用过码  used_up=该码已达使用上限  invalid=码不存在")

            cookies = context.cookies("https://muse.ai")
            sess = next((ck["value"] for ck in cookies if ck["name"] == "hatch_sess"), None)
        except SystemExit:
            raise
        except Exception as e:
            state["stage_error"] = repr(e)
            raise
        finally:
            _write_trace(state, context, page)
            try:
                browser.close()
            except Exception:
                pass

    print(f"\nhatch_sess 长度 = {len(sess or '')}")

    save_account({
        "email": email,
        "account_id": uid,
        "live": live,
        "vm_id": vm,
        "redeemed_invite_code": invite_code or None,
        "own_invite_code": own,
        "hatch_sess": sess,
        "created_at": int(time.time()),
    })
    print("已保存到 accounts.jsonl")


def probe(args=None):
    if args is None:
        args = parse_args()

    cfg = load_config()
    proxy = (cfg.get("proxy") or "").strip() or None
    invite_code = (cfg.get("invite_code") or "").strip()
    browser_locale = resolve_locale(cfg, args)
    browser_tz = resolve_timezone(cfg, browser_locale, args)
    use_headless_shell = bool(args.headless_shell or cfg.get("browser_channel") == "headless-shell")
    print("exit_ip:", _fetch_ip_info(proxy) or "unknown")

    trace_domain = "muse.ai"
    if os.path.exists(".last_browser_trace.json"):
        print("=== .last_browser_trace.json ===")
        with open(".last_browser_trace.json", encoding="utf-8") as f:
            tr = json.load(f)
        print("page_url:", tr.get("page_url"))
        print("env:", {"launch_mode": tr.get("launch_mode"), "locale": tr.get("locale"), "timezone": tr.get("timezone"), "nav_env": tr.get("nav_env")})
        if tr.get("stage_error"):
            print("stage_error:", tr.get("stage_error"))
        if tr.get("diag"):
            print("diag:", tr.get("diag"))
        print("post_reg_navs:", tr.get("post_reg_navs"))
        print("cookies:", tr.get("cookies"))
        for ck in tr.get("cookies") or []:
            if ck.get("name") == "hatch_sess" and ck.get("domain"):
                trace_domain = ck["domain"]
        for item in tr.get("trace", []):
            hdrs = item.get("headers") or {}
            xh = {k: v for k, v in hdrs.items() if k.startswith("x-") or k.startswith("sec-ch-") or k in ("referer", "origin", "accept-language", "user-agent")}
            print(
                f"  {item.get('status')} {item.get('method')} {item.get('url')} "
                f"| sc={item.get('set_cookies')} | rh={item.get('resp_headers')} | cks={item.get('cookie_names')} | h={xh}"
            )
            if item.get("post"):
                print(f"      body={item['post'][:400]}")
            if item.get("resp_body"):
                print(f"      resp={item['resp_body'][:400]}")
    else:
        print("未找到 .last_browser_trace.json")

    if not os.path.exists("accounts.jsonl"):
        raise SystemExit("未找到 accounts.jsonl")
    records = []
    with open("accounts.jsonl", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                records.append(json.loads(line))
    rec = next((r for r in reversed(records) if r.get("hatch_sess")), None)
    if not rec:
        raise SystemExit("accounts.jsonl 中没有带 hatch_sess 的记录")

    print(f"\n=== 使用已保存会话探测: {rec.get('email')} (id={rec.get('account_id')}, locale={browser_locale}, tz={browser_tz or 'default'}) ===")
    with sync_playwright() as p:
        browser, launch_mode = _launch_browser(
            p,
            proxy=proxy,
            headed=args.headed,
            use_headless_shell=use_headless_shell,
        )
        ua = (cfg.get("browser_ua") or "").strip() or _chrome_ua(browser, mac_ua=args.mac_ua)
        ctx_kwargs = {
            "locale": browser_locale,
            "viewport": {"width": 1440, "height": 900},
            "device_scale_factor": 2,
            "user_agent": ua,
        }
        if browser_tz:
            ctx_kwargs["timezone_id"] = browser_tz
        context = browser.new_context(**ctx_kwargs)
        context.add_cookies([{
            "name": "hatch_sess",
            "value": rec["hatch_sess"],
            "domain": trace_domain,
            "path": "/",
            "httpOnly": True,
            "secure": True,
            "sameSite": "Lax",
        }])
        page = context.new_page()
        api_hits = []

        def on_resp(resp):
            if "/api/" in resp.url:
                try:
                    txt = resp.text()[:240]
                except Exception:
                    txt = ""
                api_hits.append(f"{resp.request.method} {resp.url} -> {resp.status} {txt}")

        page.on("response", on_resp)

        routes = ["/", "/access", "/access/invite", "/access/terms", "/access/disclosure", "/access/waitlist"]
        if invite_code:
            routes.append(f"/invite/{invite_code}")

        for path in routes:
            api_hits.clear()
            try:
                page.goto("https://muse.ai" + path, wait_until="domcontentloaded", timeout=30000)
                page.wait_for_timeout(2000)
                btns = [b.strip() for b in page.locator("button:visible").all_inner_texts() if b.strip()][:8]
                txt = re.sub(r"\s+", " ", page.locator("body").inner_text()).strip()[:200]
                html = page.content().replace('\\"', '"')
                flags = re.findall(r'"(?:isGeoGated|state|waitlistState|admissionState|region|country|eligible|admitted|accessState)"\s*:\s*[^,}\]]+', html)
                print(f"[GET {path}] ({launch_mode}) -> {page.url} | btns={btns} | flags={flags[:12]} | text={txt}")
                for h in api_hits:
                    print(f"    api: {h}")
            except Exception as e:
                print(f"[GET {path}] ERR: {e}")

        if invite_code:
            res = browser_api(
                page,
                "POST",
                "/api/hatch/invite-code/redeem",
                {"code": invite_code, "supportsPendingRedemption": True},
            )
            print(f"[API redeem(supportsPendingRedemption)] -> {res['status']} {res['text'][:240]}")

        browser.close()


if __name__ == "__main__":
    cli_args = parse_args()
    if cli_args.probe:
        probe(cli_args)
    else:
        main(cli_args)
