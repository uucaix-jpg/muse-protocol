"""完整注册 + 兑换流程示例。运行前请填写 config.json（见 config.example.json）。"""

import json
import os
import sys
import time
import uuid

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from musepy import caa, falco
from musepy.client import MuseClient, MuseError, random_dob, random_name
from musepy.otp_worker import WorkerOtpClient, random_email


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
        return random_email(domain)
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

    c = MuseClient(proxy=proxy, impersonate=cfg.get("impersonate", "chrome131"),
                   lang=cfg.get("lang", "ja"))

    print(f"[1] 打开首页  {email}")
    c.open_home()

    print("[2] 重启注册流程")
    c.restart()

    # ---- 可选的漏斗埋点 ----
    funnel = caa.Caa(c.fp, c.lang, waterfall_id=c.waterfall_id)
    if cfg.get("send_caa", True):
        funnel.page_shown(c.s)
        time.sleep(2)
        funnel.focus(c.s)
        time.sleep(0.5)
        funnel.continue_clicked(c.s)
        funnel.registration_start(c.s)

    sent_after_ms = int(time.time() * 1000)
    if otp_client:
        otp_client.clear(email)

    print("[3] 发送验证码")
    c.send_otp(email)

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

    if cfg.get("send_caa", True):
        funnel.otp_typed(c.s)

    print("[4] 校验验证码")
    c.confirm_otp(code)

    rand_first, rand_last = random_name()
    first = (cfg.get("first_name") or "").strip() or rand_first
    last = (cfg.get("last_name") or "").strip() or rand_last
    dob = cfg.get("date_of_birth") or random_dob()

    if cfg.get("send_caa", True):
        funnel.otp_success(c.s)
        funnel.birthday_start(c.s)
        time.sleep(3)
        funnel.birthday_next(c.s)

    print(f"[5] 完成注册  {first} {last}  {dob}")
    data = c.register_completion(dob, first, last)
    uid = (data.get("auth_meta_session") or {}).get("frl_account_id")
    print(f"    account_id = {uid}")

    if cfg.get("send_caa", True):
        funnel.create_success(c.s, uid)
        funnel.login_success(c.s, uid)

    # ---- falco 埋点（可选）----
    if cfg.get("send_falco", True):
        falco.send(c.s, c.fp, c.lang, str(uuid.uuid4()), [
            falco.perf("https://muse.ai/"),
            falco.interaction("https://muse.ai/", "page_load", "main_app", "page_load"),
        ])

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
            print(f"    失败原因映射: already_redeemed=本账号已用过码  "
                  f"used_up=该码已达使用上限  invalid=码不存在")
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
    c.close()


if __name__ == "__main__":
    try:
        main()
    except MuseError as e:
        print(f"协议失败: {e}")
        sys.exit(1)
