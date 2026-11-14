"""
falco 埋点上报（可选）

站点前端会通过 POST /api/falco 批量上报行为事件。缺失这些上报不会直接
导致注册失败，但会让会话的行为特征显著区别于真实浏览器。
"""

import json
import random
import time

from musepy.client import BASE, accept_language_header


def _ms():
    return int(time.time() * 1000)


def _device_locale(lang):
    raw = (lang or "ja").strip().split(",")[0].strip()
    low = raw.lower()
    if low in ("ja", "ja-jp"):
        return "ja-JP"
    if low in ("zh", "zh-cn"):
        return "zh-CN"
    if low in ("en", "en-us"):
        return "en-US"
    return raw.replace("_", "-") or "ja-JP"


def interaction(uri, action_type, action_target, event_name, meta=None, device_locale="ja"):
    if meta is not None:
        m = dict(meta)
    else:
        m = {"surface": "onboarding", "result": "success",
             "resolution_mode": "blocking", "resolved_destination": "main_app"}
    payload = {
        "is_cvm": False,
        "metadata": json.dumps(m, separators=(",", ":")),
        "uri": uri,
        "action_type": action_type,
        "action_target": action_target,
        "event_name": event_name,
        "device_locale": device_locale,
    }
    return {"event": "hatch_web_interaction", "timeInMs": _ms(), "rate": 1,
            "payload": json.dumps(payload, separators=(",", ":"))}


def impression(uri, surface, subsurface, entry_point=None, extra=None, device_locale="ja",
               action_target=None, event_name=None, action_type="one_px_impression"):
    m = {"surface": surface, "subsurface": subsurface}
    if entry_point:
        m["entry_point"] = entry_point
    if extra:
        m.update(extra)
    payload = {
        "is_cvm": False,
        "metadata": json.dumps(m, separators=(",", ":")),
        "uri": uri,
        "action_type": action_type,
        "action_target": action_target or (subsurface + "_ready"),
        "event_name": event_name or (subsurface + "_ready_impression"),
        "device_locale": device_locale,
    }
    return {"event": "hatch_web_impression", "timeInMs": _ms(), "rate": 1,
            "payload": json.dumps(payload, separators=(",", ":"))}


def perf(uri, nav_type="navigate", seconds=None):
    ann = {
        "tracePolicy": "hatch.chat",
        "traceType": "INITIAL_LOAD",
        "uri": uri,
        "httpProtocol": "h2",
        "navigationType": nav_type,
        "interactionClass": "responsive",
        "qpl__source": "client_js",
        "navigationTypeDetail": "normal",
        "page_load_id": "%08x-0000-4000-8000-%012x" % (
            random.getrandbits(32), random.getrandbits(48)),
    }
    payload = {
        "marker_id": random.randint(600000000, 610000000),
        "action_id": 2,
        "sample_rate": 1,
        "duration_ns": int((seconds or random.uniform(2.5, 12.0)) * 1e9),
        "annotations": ann,
    }
    return {"event": "perf", "timeInMs": _ms(),
            "payload": json.dumps(payload, separators=(",", ":"))}


def send(session, impersonate, lang, session_id, events, referer=BASE + "/", app_id=None):
    """发送一批事件；失败静默"""
    if not events:
        return None
    body = {"sessionId": session_id, "user": {"kind": "loggedOut"},
            "machineId": "not_set", "events": events}
    if app_id:
        body = {"appId": app_id, **body}
    h = {
        "accept": "*/*",
        "content-type": "application/json",
        "origin": BASE,
        "referer": referer or (BASE + "/"),
        "accept-language": accept_language_header(lang),
        "priority": "u=1, i",
        "sec-fetch-site": "same-origin",
        "sec-fetch-mode": "cors",
        "sec-fetch-dest": "empty",
        "Upgrade-Insecure-Requests": None,
        "Sec-Fetch-User": None,
    }
    try:
        return session.post(BASE + "/api/falco", impersonate=impersonate,
                            headers=h, data=json.dumps(body, separators=(",", ":")))
    except Exception:
        return None
