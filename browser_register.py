"""
真实 Chromium 浏览器自动注册 + Cloudflare 自动接码 + 邀请码兑换脚本。
当纯 HTTP 协议请求触发 403 registrationBlocked 时，使用真实浏览器完成注册并将 Cookie 交接给协议端完成激活与兑换。
"""

import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from playwright.sync_api import sync_playwright

from musepy.client import MuseClient, random_dob, random_name
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


def main():
    cfg = load_config()
    email = resolve_email(cfg)
    proxy = (cfg.get("proxy") or "").strip() or None
    invite_code = (cfg.get("invite_code") or "").strip()

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
        "confirm_status": None,
        "reg_status": None,
        "reg_body": None,
        "confirm_data": {},
    }

    with sync_playwright() as p:
        launch_kwargs = {
            "headless": True,
            "args": [
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
                "--disable-dev-shm-usage",
            ],
        }
        if proxy:
            launch_kwargs["proxy"] = {"server": proxy}

        browser = p.chromium.launch(**launch_kwargs)
        context = browser.new_context(
            locale=cfg.get("browser_locale", "zh-CN"),
            viewport={"width": 1440, "height": 900},
            device_scale_factor=2,
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/131.0.0.0 Safari/537.36"
            ),
        )
        context.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', {get: () => undefined});"
        )
        page = context.new_page()

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

        def on_response(resp):
            if "/api/auth/native/confirm-otp" in resp.url:
                state["confirm_status"] = resp.status
                try:
                    state["confirm_data"] = resp.json() or {}
                except Exception:
                    pass
            elif "/api/auth/native/register-completion" in resp.url:
                state["reg_status"] = resp.status
                try:
                    data = resp.json() or {}
                    state["reg_body"] = data
                    state["uid"] = (data.get("auth_meta_session") or {}).get("frl_account_id")
                except Exception:
                    state["reg_body"] = resp.text()

        page.on("response", on_response)

        print(f"[1] 真实浏览器打开首页  {email}")
        page.goto("https://muse.ai/", wait_until="domcontentloaded", timeout=60000)

        email_input = page.locator('input[autocomplete="username webauthn"], input[inputmode="email"]').first
        email_input.wait_for(state="visible", timeout=45000)
        time.sleep(1.5)

        sent_after_ms = int(time.time() * 1000)
        if otp_client:
            otp_client.clear(email)

        print("[2] 输入邮箱并发送验证码")
        email_input.click()
        email_input.fill(email)
        time.sleep(0.5)
        page.locator('form button[type="submit"]').first.click()

        # OTP 页面的真实输入框带 sr-only，等待可见的 6 格验证码区域
        page.locator('[data-slot="pin-digit"]').first.wait_for(state="visible", timeout=30000)

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

        # 等待确认并跳转至生日（和可选姓名）页面
        page.locator('button[role="combobox"]').first.wait_for(state="visible", timeout=30000)

        given_input = page.locator('input[autocomplete="given-name"]')
        if given_input.count() > 0 and given_input.first.is_visible():
            print(f"[4] 填写姓名与生日  {first} {last}  {dob}")
            given_input.first.fill(first)
            page.locator('input[autocomplete="family-name"]').first.fill(last)
        else:
            print(f"[4] 填写生日  {dob}")

        # 分别设置生日下拉框（年 / 月 / 日）
        try:
            combos = page.locator('button[role="combobox"]')
            for i in range(combos.count()):
                txt = combos.nth(i).inner_text().strip()
                digits = "".join(ch for ch in txt if ch.isdigit())
                if len(digits) == 4 and int(digits) >= 1900:
                    combos.nth(i).click()
                    page.get_by_role("option", name=dob_year, exact=True).click(timeout=5000)
                    break
            for i in range(combos.count()):
                txt = combos.nth(i).inner_text().strip()
                digits = "".join(ch for ch in txt if ch.isdigit())
                if digits and len(digits) <= 2 and "月" not in txt and int(digits) <= 31:
                    combos.nth(i).click()
                    page.get_by_role("option", name=str(int(dob_day)), exact=True).click(timeout=5000)
                    break
            for i in range(combos.count()):
                txt = combos.nth(i).inner_text().strip()
                digits = "".join(ch for ch in txt if ch.isdigit())
                is_year = len(digits) == 4 and int(digits) >= 1900
                is_day = bool(digits) and len(digits) <= 2 and "月" not in txt and int(digits) <= 31
                if not is_year and not is_day:
                    combos.nth(i).click()
                    page.locator('[role="option"]').nth(int(dob_month) - 1).click(timeout=5000)
                    break
        except Exception:
            try:
                page.keyboard.press("Escape")
            except Exception:
                pass

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
            browser.close()
            raise SystemExit(f"注册失败: register-completion {state['reg_status']}: {state['reg_body']}")

        # 等待浏览器完成会话跳转并落盘 hatch_sess cookie
        for _ in range(20):
            cookies = context.cookies("https://muse.ai")
            if any(c["name"] == "hatch_sess" for c in cookies):
                break
            page.wait_for_timeout(500)

        all_cookies = context.cookies()
        browser.close()

    uid = state["uid"]
    print(f"    注册成功! account_id = {uid}")

    # 将浏览器拿到的 Cookie 同步给 MuseClient 执行后续激活与邀请码兑换
    c = MuseClient(proxy=proxy, impersonate=cfg.get("impersonate", "chrome145"), lang=cfg.get("lang", "ja"))
    for ck in all_cookies:
        c.s.cookies.set(ck["name"], ck["value"], domain=ck.get("domain") or "muse.ai", path=ck.get("path") or "/")

    print("[6] 激活会话（accept-tos）")
    for _ in range(2):
        c.accept_tos()
        if c.is_live():
            break
        time.sleep(1.2)
    live = c.is_live()
    print(f"    invite = {'200 活号' if live else '401/403 无 hatch 权限'}")

    print("[7] 唤醒 VM")
    vm = c.wake_vm()
    print(f"    vm_id = {vm}")

    c.stateful_sharing()

    own = None
    print(f"[8] 兑换邀请码  {invite_code or '(未配置)'}")
    if invite_code:
        ok, reason = c.redeem(invite_code)
        print(f"    redeem = {ok} / {reason}")
        if ok:
            own = c.scratch_code()
            print(f"[9] 自己的邀请码 = {own}")
        else:
            print("    失败原因映射: already_redeemed=本账号已用过码  used_up=该码已达使用上限  invalid=码不存在")

    sess = c.session_cookie()
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
    c.close()


if __name__ == "__main__":
    main()
