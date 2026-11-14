"""
caa-events 上报（Create Account Analytics）

前端通过 navigator.sendBeacon 逐步骤上报注册漏斗事件，同一 waterfall_id
贯穿整个流程。这些事件不参与注册判定，但构成完整的漏斗数据。
"""

import json
import time
import uuid

from musepy.client import BASE, accept_language_header

T_LOGIN = "caa_login_client_events_rl"
T_ACQ = "caa_acquisition_client_events_rl"


class Caa:
    def __init__(self, impersonate, lang, waterfall_id=None, referer=BASE + "/", activation_source=None):
        self.fp = impersonate
        self.lang = lang
        self.wid = waterfall_id or str(uuid.uuid4())
        self.referer = referer
        self.activation_source = activation_source
        self.sent = 0

    def _send(self, session, events):
        if not events:
            return None
        payload = {"events": events}
        if self.activation_source:
            payload["activation_source"] = self.activation_source
        h = {
            "accept": "*/*",
            "content-type": "application/json",
            "origin": BASE,
            "referer": self.referer or (BASE + "/"),
            "accept-language": accept_language_header(self.lang),
            "priority": "u=4, i",
            "sec-fetch-site": "same-origin",
            "sec-fetch-mode": "cors",
            "sec-fetch-dest": "empty",
            "Upgrade-Insecure-Requests": None,
            "Sec-Fetch-User": None,
        }
        try:
            r = session.post(BASE + "/api/auth/caa-events", impersonate=self.fp,
                             headers=h, data=json.dumps(payload, separators=(",", ":")))
            self.sent += 1
            return r
        except Exception:
            return None

    # ---- 联系点页 ----

    def _ev_page_shown(self):
        return {
            "table": T_LOGIN, "event_name": "contactpoint_page_shown",
            "event_category": "contactpoint_page_interaction", "event_flow": "login_manual",
            "event_step": "contactpoint", "extra_client_data": {"is_native": "1"},
            "waterfall_id": self.wid, "client_time_ms": _ms(),
        }

    def _ev_focus(self):
        return {
            "table": T_LOGIN, "event_name": "contactpoint_focus",
            "event_category": "contactpoint_page_interaction", "event_flow": "login_manual",
            "event_step": "contactpoint", "extra_client_data": {"is_native": "1"},
            "waterfall_id": self.wid, "client_time_ms": _ms(),
        }

    def _ev_continue_clicked(self):
        return {
            "table": T_LOGIN, "event_name": "contactpoint_continue_clicked",
            "event_category": "contactpoint_page_interaction", "event_flow": "login_manual",
            "event_step": "contactpoint", "contact_point_type": "EMAIL_ADDRESS",
            "extra_client_data": {"is_native": "1"},
            "waterfall_id": self.wid, "client_time_ms": _ms(),
        }

    def page_shown(self, s):
        return self._send(s, [self._ev_page_shown()])

    def focus(self, s):
        return self._send(s, [self._ev_focus()])

    def continue_clicked(self, s):
        return self._send(s, [self._ev_continue_clicked()])

    # ---- 注册漏斗 ----

    def _evs_registration_start(self):
        return [
            {"table": T_LOGIN, "event_name": "password_saving_start", "event_category": "impression",
             "event_flow": "ntm", "event_step": "otp",
             "extra_client_data": {"is_native": "1", "is_checked": "1"},
             "waterfall_id": self.wid, "client_time_ms": _ms()},
            {"table": T_ACQ, "event_name": "registration_flow_state", "state": "registration_start",
             "event_category": "registration_funnel", "event_step": "contactpoint", "event_flow": "ntm",
             "entry_point": "login_home", "contact_point_type": "EMAIL_ADDRESS",
             "extra_client_data": {"is_native": "1", "flow_source": "server"},
             "waterfall_id": self.wid, "client_time_ms": _ms()},
            {"table": T_ACQ, "event_name": "confirmation_flow_state", "state": "confirmation_start",
             "event_category": "confirmation_funnel", "event_step": "confirmation",
             "contact_point_type": "EMAIL_ADDRESS",
             "extra_client_data": {"is_native": "1", "flow_source": "server"},
             "event_flow": "ntm", "entry_point": "login_home",
             "waterfall_id": self.wid, "client_time_ms": _ms()},
        ]

    def registration_start(self, s):
        return self._send(s, self._evs_registration_start())

    def continue_and_start(self, s):
        return self._send(s, [self._ev_continue_clicked(), *self._evs_registration_start()])

    def otp_typed(self, s):
        return self._send(s, [{
            "table": T_ACQ, "event_name": "confirmation_flow_state", "state": "confirmation_next",
            "event_category": "confirmation_funnel", "event_step": "confirmation",
            "contact_point_type": "EMAIL_ADDRESS",
            "extra_client_data": {"is_native": "1", "flow_source": "server"},
            "event_flow": "ntm", "entry_point": "login_home",
            "waterfall_id": self.wid, "client_time_ms": _ms()}])

    def _ev_otp_success(self):
        return {
            "table": T_ACQ, "event_name": "confirmation_flow_state", "state": "confirmation_success",
            "event_category": "confirmation_funnel", "event_step": "confirmation",
            "contact_point_type": "EMAIL_ADDRESS",
            "extra_client_data": {"is_native": "1", "flow_source": "server"},
            "event_flow": "ntm", "entry_point": "login_home",
            "waterfall_id": self.wid, "client_time_ms": _ms(),
        }

    def _ev_birthday_start(self):
        return {
            "table": T_ACQ, "event_name": "registration_flow_state", "state": "birthday_start",
            "event_category": "registration_funnel", "event_step": "birthday", "event_flow": "ntm",
            "entry_point": "login_home", "extra_client_data": {"is_native": "1"},
            "waterfall_id": self.wid, "client_time_ms": _ms(),
        }

    def otp_success(self, s):
        return self._send(s, [self._ev_otp_success()])

    def birthday_start(self, s):
        return self._send(s, [self._ev_birthday_start()])

    def otp_and_birthday_start(self, s):
        return self._send(s, [self._ev_otp_success(), self._ev_birthday_start()])

    def birthday_next(self, s):
        return self._send(s, [
            {"table": T_ACQ, "event_name": "registration_flow_state", "state": "birthday_next",
             "event_category": "registration_funnel", "event_step": "birthday", "event_flow": "ntm",
             "entry_point": "login_home", "extra_client_data": {"is_native": "1"},
             "waterfall_id": self.wid, "client_time_ms": _ms()},
            {"table": T_ACQ, "event_name": "registration_flow_state", "state": "create_attempt",
             "event_category": "registration_funnel", "event_step": "account_creation", "event_flow": "ntm",
             "entry_point": "login_home", "contact_point_type": "EMAIL_ADDRESS",
             "extra_client_data": {"is_native": "1"},
             "waterfall_id": self.wid, "client_time_ms": _ms()},
        ])

    def create_failure(self, s, exception_type="registrationBlocked"):
        return self._send(s, [{
            "table": T_ACQ, "event_name": "registration_flow_state", "state": "create_failure",
            "event_category": "registration_funnel", "event_step": "account_creation", "event_flow": "ntm",
            "entry_point": "login_home", "exception_type": exception_type or "registrationBlocked",
            "extra_client_data": {"is_native": "1"},
            "waterfall_id": self.wid, "client_time_ms": _ms(),
        }])

    def create_success(self, s, user_id):
        return self._send(s, [{
            "table": T_ACQ, "event_name": "registration_flow_state", "state": "create_success",
            "event_category": "registration_funnel", "event_step": "account_creation", "event_flow": "ntm",
            "entry_point": "login_home", "contact_point_type": "EMAIL_ADDRESS",
            "created_user_id": str(user_id or ""), "extra_client_data": {"is_native": "1"},
            "waterfall_id": self.wid, "client_time_ms": _ms()}])

    def login_success(self, s, user_id):
        return self._send(s, [
            {"table": T_LOGIN, "event_name": "login_success", "event_category": "login_success",
             "event_flow": "ntm", "event_step": "tos",
             "extra_client_data": {"is_native": "1", "login_source": "Registration",
                                   "login_credential_type": "new_account", "is_2fa": "false"},
             "logged_in_identifier": str(user_id or ""),
             "waterfall_id": self.wid, "client_time_ms": _ms()},
            {"table": T_LOGIN, "event_name": "password_saving_opt_in", "event_category": "interaction",
             "event_flow": "ntm", "event_step": "otp",
             "extra_client_data": {"is_native": "1"},
             "waterfall_id": self.wid, "client_time_ms": _ms()},
        ])


def _ms():
    return int(time.time() * 1000)
