"""完整注册 + 兑换流程示例。运行前请填写 config.json（见 config.example.json）。"""

import json
import os
import sys
import time
import uuid

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from musepy import caa, falco
from musepy.client import MuseClient, MuseError, random_dob, random_name
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
    proxy = cfg.get("proxy") or None
    invite_code = cfg.get("invite_code") or ""
    otp_file = cfg.get("otp_file")  # 可选：从文件读取当前验证码

    worker_cfg = cfg.get("otp_worker") or {}
    otp_client = None
    if worker_cfg.get("url") and worker_cfg.get("api_key"):
        otp_client = WorkerOtpClient(worker_cfg["url"], worker_cfg["api_key"])

    c = MuseClient(proxy=proxy, impersonate=cfg.get("impersonate", "chrome145"),
                   lang=cfg.get("lang", "ja"))

    falco_sid = str(uuid.uuid4())
    send_falco = cfg.get("send_falco", True)
    send_caa = cfg.get("send_caa", True)

    try:
        print(f"[1] 打开首页  {email}")
        c.open_home()

        print("[2] 重启注册流程")
        c.restart()

        if send_falco:
            falco.send(
                c.s,
                c.fp,
                c.lang,
                falco_sid,
                [
                    falco.impression(
                        c.landing_url,
                        "login",
                        "login_home",
                        "login_home",
                        device_locale=falco._device_locale(c.lang),
                    ),
                ],
                referer=c.landing_url,
            )

        funnel = caa.Caa(c.fp, c.lang, waterfall_id=c.waterfall_id, referer=c.home_url)
        if send_caa:
            funnel.page_shown(c.s)
            time.sleep(1.5)
            funnel.focus(c.s)

        sent_after_ms = int(time.time() * 1000)
        if otp_client:
            otp_client.clear(email)

        print("[3] 发送验证码")
        c.send_otp(email)
        if send_caa:
            funnel.continue_and_start(c.s)

        if otp_client:
            print("    正在通过 Cloudflare Email Worker 自动等待验证码...")
            code, _ = otp_client.poll_otp(email, after_ms=sent_after_ms, wait_seconds=90, interval=2.0)
            if not code:
                code = input("自动接码超时，请手动输入收到的 6 位验证码: ").strip()
            else:
                print(f"    自动获取到验证码 = {code}")
        elif otp_file:
            with open(otp_file, encoding="utf-8") as f:
                code = f.read().strip()
        else:
            code = input("输入收到的 6 位验证码: ").strip()

        if send_caa:
            funnel.otp_typed(c.s)

        print("[4] 校验验证码")
        c.confirm_otp(code)

        rand_first, rand_last = random_name()
        first = (cfg.get("first_name") or "").strip() or rand_first
        last = (cfg.get("last_name") or "").strip() or rand_last
        dob = cfg.get("date_of_birth") or random_dob()

        if send_caa:
            funnel.otp_and_birthday_start(c.s)
            time.sleep(3)

        name_label = f"{first} {last}" if c.requires_name else "(无需姓名)"
        print(f"[5] 完成注册  {name_label}  {dob}")
        data = c.register_completion(dob, first, last)
        meta_sess = data.get("auth_meta_session") or {}
        uid = meta_sess.get("frl_account_id")
        frl_token = meta_sess.get("frl_access_token")
        print(f"    account_id = {uid}")

        if send_caa:
            funnel.birthday_next(c.s)
            funnel.create_success(c.s, uid)
            funnel.login_success(c.s, uid)

        if frl_token:
            print("    正在同步 auth.meta.com 会话...")
            bs = c.bootstrap_session(frl_token)
            if bs is not None:
                print(f"    同步跳转 -> {c.post_reg_url}")
            if c.is_geo_gated():
                print("    ⚠️ 服务端返回 isGeoGated=true（当前 IP/区域可能受限）")

        if send_falco:
            falco.send(
                c.s,
                c.fp,
                c.lang,
                falco_sid,
                [
                    falco.interaction(
                        "https://muse.ai/",
                        "page_load",
                        "main_app",
                        "page_load",
                        device_locale=falco._device_locale(c.lang),
                    ),
                ],
                referer=c.home_url,
            )

        print("[6] 激活会话（accept-tos）")
        for idx in range(2):
            if c.is_live():
                break
            tos_r = c.accept_tos()
            if tos_r.status_code != 200:
                print(f"    accept-tos[{idx + 1}] = {tos_r.status_code} {tos_r.text[:120]}")
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
            if ok and reason == "redeemed":
                own = c.scratch_code()
                print(f"[9] 自己的邀请码 = {own}")
            elif reason == "pending":
                print("    邀请码已提交为待处理状态（pending），暂不可刮取新码")
            else:
                print("    失败原因映射: already_redeemed=本账号已用过码  "
                      "used_up=该码已达使用上限  invalid=码不存在")
        else:
            print("    跳过兑换")

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
    except MuseError as e:
        print(f"协议失败: {e}")
        print(f"诊断信息: impersonate={c.fp} lang={c.lang} cookies={c.cookie_summary()}")
        if "registrationBlocked" in str(e):
            print("提示: 纯 HTTP 协议已完成完整 Cookie/埋点链但仍被服务端风控拦截，"
                  "请改用真实浏览器脚本: .venv/bin/python browser_register.py")
        sys.exit(1)
    finally:
        c.close()


if __name__ == "__main__":
    main()
