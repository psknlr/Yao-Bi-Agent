"""All provider backends (poe / minimax / azure) must drive every UI mode.

Regression guard for two v0.15 fixes:

1. The case-directed modes (经验推理 / 经验总结 / 智能体协作) operate on the case_state
   with no question text. Before the fix the frontend sent no chief complaint, so the
   scope gate blocked them to "未识别到腰痹相关主诉" and the model was never called — which
   read as "poe/minimax/azure don't support these modes". The chief_complaint now carried
   in the payload gives the lumbar anchor.
2. All generation tasks route through the OpenAI-compatible HTTP path, so a configured
   provider must actually be invoked for chat / interview / reasoning / summary /
   collaboration.
"""

from __future__ import annotations

import importlib
import json

import pytest

PROVIDERS = ["poe", "minimax", "azure"]

_PROVIDER_ENV = {
    "poe": {"POE_API_KEY": "k", "TAO_ENDPOINT_URL": "https://api.poe.com/v1/chat/completions"},
    "minimax": {"MINIMAX_API_KEY": "k", "TAO_ENDPOINT_URL": "https://api.minimax.chat/v1/text/chatcompletion_v2"},
    "azure": {"AZURE_OPENAI_API_KEY": "k", "AZURE_OPENAI_ENDPOINT": "https://r.openai.azure.com",
              "AZURE_OPENAI_DEPLOYMENT": "dep", "AZURE_OPENAI_API_VERSION": "2024-06-01"},
}

_CLEAR = ["TAO_API_KEY", "POE_API_KEY", "MINIMAX_API_KEY", "AZURE_OPENAI_API_KEY",
          "AZURE_OPENAI_ENDPOINT", "AZURE_OPENAI_DEPLOYMENT", "AZURE_OPENAI_API_VERSION",
          "TAO_ENDPOINT_URL", "YAOBI_CLINICIAN_TOKEN", "YAOBI_CLINICIAN_TOKENS", "TAO_BACKEND"]


@pytest.fixture(autouse=True)
def _restore_server(monkeypatch):
    """Reload backend.server (only) to its env-default CLIENT after each test, so a
    provider backend set here does not leak into later test files. dao_client is NEVER
    reloaded — that would mint a second DaoRuntimeError class and break `except
    DaoRuntimeError` in modules that imported the original."""
    yield
    for k in _CLEAR:
        monkeypatch.delenv(k, raising=False)
    import backend.server as srv
    importlib.reload(srv)


def _reply_for(user_prompt: str) -> str:
    """Correct JSON/text per task, keyed on prompt markers."""
    p = user_prompt
    if "技能路由器" in p:
        return json.dumps({"intent": "reasoning_inquiry"}, ensure_ascii=False)
    if "资深中医师" in p or "会诊" in p:  # consultation returns raw markdown
        return ("病机分析：本案属寒凝血瘀、肝肾亏虚之腰痹，证型倾向肾阳不足证，"
                "治法温阳散寒、补肾通络，可参考独活寄生汤加减方义（供执业医师审核）。")
    if "问诊智能体" in p or "老中医" in p:
        return "请问您的疼痛在夜间或受凉时是否加重？"
    if "信息抽取" in p:
        return json.dumps({"pain_slots": {"pain_location": "腰部", "cold_damp_trigger": True}}, ensure_ascii=False)
    return json.dumps({"reasoning_markdown": "## 推理\n寒凝阳虚（供医师审定）。",
                       "summary_markdown": "## 按语\n温阳补肾为主线。"}, ensure_ascii=False)


class _FakeResp:
    def __init__(self, payload): self._p = payload
    def read(self, limit=None):
        raw = json.dumps(self._p, ensure_ascii=False).encode()
        return raw if limit is None else raw[:limit]
    def __enter__(self): return self
    def __exit__(self, *a): return False


def _load_server(monkeypatch, backend: str):
    for k in _CLEAR:
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("TAO_BACKEND", backend)
    for k, v in _PROVIDER_ENV[backend].items():
        monkeypatch.setenv(k, v)

    def fake_open(request, data=None, timeout=None):
        body = json.loads(request.data.decode())
        user = [m["content"] for m in body["messages"] if m["role"] == "user"][-1]
        payload = {"choices": [{"message": {"content": _reply_for(user)}}]}
        if backend == "minimax":
            payload["base_resp"] = {"status_code": 0}
        return _FakeResp(payload)
    monkeypatch.setattr("backend.llm.dao_client._NO_REDIRECT_OPENER.open", fake_open)

    import backend.server as srv
    importlib.reload(srv)
    return srv


_CASE = {
    "doctor_mode": True,
    "tags": ["deep_cold_pain", "cold_aversion", "elderly"],
    "chief_complaint": {"standard_text": "腰痛反复5年，畏寒，下肢麻木", "main_symptom": "腰痛"},
}


@pytest.mark.parametrize("backend", PROVIDERS)
def test_provider_drives_chat(monkeypatch, backend):
    srv = _load_server(monkeypatch, backend)
    turn = srv.handle_chat({**_CASE, "question": "这个腰痛患者的辨证思路是什么？"})["turn"]
    assert turn["used_llm"] is True, f"{backend} 未驱动智能问答"


@pytest.mark.parametrize("backend", PROVIDERS)
def test_provider_drives_reasoning_and_summary(monkeypatch, backend):
    srv = _load_server(monkeypatch, backend)
    reasoning = srv.handle_reasoning(dict(_CASE))["result"]
    summary = srv.handle_summary(dict(_CASE))["result"]
    assert reasoning["used_llm"] is True, f"{backend} 未驱动经验推理"
    assert summary["used_llm"] is True, f"{backend} 未驱动经验总结"


@pytest.mark.parametrize("backend", PROVIDERS)
def test_provider_drives_collaboration(monkeypatch, backend):
    srv = _load_server(monkeypatch, backend)
    result = srv.handle_collaboration(dict(_CASE))
    steps = result.get("collaboration_trace") or []
    assert any(s.get("used_llm") for s in steps), f"{backend} 未驱动智能体协作"


@pytest.mark.parametrize("backend", PROVIDERS)
def test_provider_drives_interview(monkeypatch, backend):
    srv = _load_server(monkeypatch, backend)
    res = srv.handle_interview({"message": "腰痛半年，畏寒，弯腰加重，遇冷更痛",
                                "doctor_mode": True, "session_id": f"iv-{backend}"})
    # The model-generated question (contains 夜间/受凉) replaces the deterministic one.
    assert "夜间" in (res.get("message") or "") or "受凉" in (res.get("message") or ""), f"{backend} 未驱动智能问诊追问"


def test_case_directed_modes_need_chief_complaint_or_lumbar_tag(monkeypatch):
    """Without a lumbar anchor the scope gate blocks reasoning before the model — the bug
    that made providers look unsupported. With the chief complaint it proceeds."""
    srv = _load_server(monkeypatch, "poe")
    no_anchor = srv.handle_reasoning({"doctor_mode": True, "tags": ["deep_cold_pain", "elderly"]})["result"]
    assert no_anchor["used_llm"] is False
    assert "未识别到腰痹" in no_anchor["answer"]
    with_anchor = srv.handle_reasoning(dict(_CASE))["result"]
    assert with_anchor["used_llm"] is True


def test_researcher_cannot_sign_off_referral(monkeypatch):
    """Physician sign-off (confirm/revise/override) is licensed-physician only; a
    researcher-declared request is rejected server-side even with clinician access."""
    for k in _CLEAR:
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("TAO_BACKEND", "mock")
    import backend.server as srv
    importlib.reload(srv)
    res = srv.handle_interview({"session_id": "r1", "review_action": "override",
                                "doctor_mode": True, "user_mode": "researcher"})
    assert res["ok"] is False
    assert res["error"] == "physician_signoff_required"
    # A physician-declared request is NOT blocked by this gate.
    ok = srv.handle_interview({"session_id": "r2", "review_action": "confirm",
                               "doctor_mode": True, "user_mode": "physician"})
    assert ok.get("error") != "physician_signoff_required"
