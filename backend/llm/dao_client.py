from __future__ import annotations

import importlib
import importlib.util
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from threading import Lock, Thread
from typing import Any, Literal

import yaml

from backend.llm.prompt_templates import (
    CONSULTATION_PROMPT_TEMPLATE,
    EMERGENCY_REFERRAL_PROMPT_TEMPLATE,
    EXPERIENCE_SUMMARY_PROMPT_TEMPLATE,
    FOLLOWUP_PROBE_PROMPT_TEMPLATE,
    INTERVIEW_EXTRACTION_PROMPT_TEMPLATE,
    INTERVIEW_QUESTION_PROMPT_TEMPLATE,
    PROBE_FREEFORM_PROMPT_TEMPLATE,
    QUESTION_PROMPT_TEMPLATE,
    REASONING_PROMPT_TEMPLATE,
    REPORT_PROMPT_TEMPLATE,
    SKILL_PLAN_PROMPT_TEMPLATE,
    SKILL_ROUTING_PROMPT_TEMPLATE,
    SYSTEM_PROMPT,
)

DaoBackend = Literal["disabled", "mock", "http", "poe", "minimax", "azure", "transformers"]

# Hosted chat-completions providers served by the shared OpenAI-compatible HTTP path.
# "http" is the generic bring-your-own endpoint; the named providers add correct
# default endpoints, auth-header conventions and provider-specific error surfaces:
#   poe     — https://api.poe.com/v1/chat/completions (Bearer POE_API_KEY,
#             model = Poe bot name, e.g. "Claude-Sonnet-4.5" / "GPT-4o")
#   minimax — https://api.minimax.io/v1/chat/completions (Bearer MINIMAX_API_KEY,
#             model e.g. "MiniMax-Text-01"). The legacy native endpoint
#             (api.minimax.chat/v1/text/chatcompletion_v2) is deprecated upstream;
#             mainland-China deployments use https://api.minimaxi.com via
#             TAO_ENDPOINT_URL. Errors may still arrive as HTTP 200 +
#             base_resp.status_code != 0 — handled either way.
#   azure   — {AZURE_OPENAI_ENDPOINT}/openai/deployments/{deployment}/chat/
#             completions?api-version=... ("api-key" header, model taken from
#             the deployment in the URL). TAO_AZURE_API_VERSION=v1 selects the
#             newer {endpoint}/openai/v1/chat/completions surface (no dated
#             api-version; deployment name travels as payload "model").
OPENAI_COMPATIBLE_BACKENDS = frozenset({"http", "poe", "minimax", "azure"})

_PROVIDER_DEFAULT_ENDPOINTS = {
    "poe": "https://api.poe.com/v1/chat/completions",
    "minimax": "https://api.minimax.io/v1/chat/completions",
}
_PROVIDER_KEY_ENVS = {
    "poe": ("TAO_API_KEY", "POE_API_KEY"),
    "minimax": ("TAO_API_KEY", "MINIMAX_API_KEY"),
    "azure": ("TAO_API_KEY", "AZURE_OPENAI_API_KEY"),
    "http": ("TAO_API_KEY",),
}

_CONFIG_DIR = Path(__file__).resolve().parents[2] / "config"

# Fallback profiles if config/model_config.yaml is missing — keep in sync with that file.
_DEFAULT_INFERENCE_PROFILES: dict[str, dict[str, Any]] = {
    "research_report": {"temperature": 0.3, "top_p": 0.85, "repetition_penalty": 1.1, "max_new_tokens": 3072, "do_sample": True},
    "teaching_explanation": {"temperature": 0.6, "top_p": 0.9, "repetition_penalty": 1.1, "max_new_tokens": 3072, "do_sample": True},
    "structured_json": {"temperature": 0.1, "top_p": 0.8, "repetition_penalty": 1.05, "max_new_tokens": 1024, "do_sample": False},
}

_PROFILE_CACHE: dict[str, dict[str, Any]] | None = None


def load_inference_profiles() -> dict[str, dict[str, Any]]:
    """Load per-task sampling profiles from config/model_config.yaml (cached).

    Structured JSON tasks (routing/planning/extraction) need greedy decoding for
    stability; long-form teaching tasks need a bigger token budget. Falling back to
    the in-code defaults keeps the client usable without the config file.
    """

    global _PROFILE_CACHE
    if _PROFILE_CACHE is None:
        profiles = dict(_DEFAULT_INFERENCE_PROFILES)
        try:
            with open(_CONFIG_DIR / "model_config.yaml", encoding="utf-8") as f:
                loaded = (yaml.safe_load(f) or {}).get("inference_profiles") or {}
            for name, params in loaded.items():
                if isinstance(params, dict):
                    profiles[name] = {**profiles.get(name, {}), **params}
        except (OSError, yaml.YAMLError):
            pass
        _PROFILE_CACHE = profiles
    return _PROFILE_CACHE


@dataclass
class DaoGenerationConfig:
    model_id: str = "CMLM/Dao1-30b-a3b"
    # Optional model revision pin (git tag / commit SHA on the model hub). None loads
    # "latest" — fine for the research prototype; production must pin a revision so
    # provenance can attribute an output to exact weights + remote code.
    model_revision: str | None = None
    backend: DaoBackend = "disabled"
    endpoint_url: str | None = None
    api_key: str | None = None
    # Azure OpenAI specifics: the deployment name replaces the model in the URL and
    # the api-version is a required query parameter. endpoint_url may be either the
    # resource base ("https://myres.openai.azure.com") or a full completions URL.
    azure_deployment: str | None = None
    azure_api_version: str = "2024-06-01"
    temperature: float = 0.3
    top_p: float = 0.85
    repetition_penalty: float = 1.1
    max_new_tokens: int = 2048
    do_sample: bool = True
    torch_dtype: str = "float16"
    device_map: str = "auto"
    attn_implementation: str = "eager"
    load_in_4bit: bool = False
    load_in_8bit: bool = False
    timeout_seconds: int = 120

    @classmethod
    def from_env(cls) -> "DaoGenerationConfig":
        backend = os.getenv("TAO_BACKEND", "disabled")
        # Provider-aware credential/endpoint resolution: TAO_API_KEY always wins; the
        # provider's conventional variable (POE_API_KEY / MINIMAX_API_KEY /
        # AZURE_OPENAI_API_KEY) is the fallback. Endpoints: explicit TAO_ENDPOINT_URL
        # first, then the provider default (Azure: AZURE_OPENAI_ENDPOINT resource base).
        api_key = None
        for env_name in _PROVIDER_KEY_ENVS.get(backend, ("TAO_API_KEY",)):
            api_key = os.getenv(env_name)
            if api_key:
                break
        endpoint_url = os.getenv("TAO_ENDPOINT_URL") or _PROVIDER_DEFAULT_ENDPOINTS.get(backend)
        if backend == "azure" and not os.getenv("TAO_ENDPOINT_URL"):
            endpoint_url = os.getenv("AZURE_OPENAI_ENDPOINT")
        return cls(
            model_id=os.getenv("TAO_MODEL_ID", cls.model_id),
            model_revision=os.getenv("TAO_MODEL_REVISION") or None,
            backend=backend,  # type: ignore[arg-type]
            endpoint_url=endpoint_url,
            api_key=api_key,
            azure_deployment=os.getenv("TAO_AZURE_DEPLOYMENT") or os.getenv("AZURE_OPENAI_DEPLOYMENT"),
            azure_api_version=os.getenv("TAO_AZURE_API_VERSION")
            or os.getenv("AZURE_OPENAI_API_VERSION") or cls.azure_api_version,
            temperature=float(os.getenv("TAO_TEMPERATURE", "0.3")),
            top_p=float(os.getenv("TAO_TOP_P", "0.85")),
            repetition_penalty=float(os.getenv("TAO_REPETITION_PENALTY", "1.1")),
            max_new_tokens=int(os.getenv("TAO_MAX_NEW_TOKENS", "2048")),
            do_sample=os.getenv("TAO_DO_SAMPLE", "true").lower() == "true",
            torch_dtype=os.getenv("TAO_TORCH_DTYPE", cls.torch_dtype),
            device_map=os.getenv("TAO_DEVICE_MAP", cls.device_map),
            attn_implementation=os.getenv("TAO_ATTN_IMPLEMENTATION", cls.attn_implementation),
            load_in_4bit=os.getenv("TAO_LOAD_IN_4BIT", "false").lower() == "true",
            load_in_8bit=os.getenv("TAO_LOAD_IN_8BIT", "false").lower() == "true",
            timeout_seconds=int(os.getenv("TAO_TIMEOUT_SECONDS", "120")),
        )


class DaoRuntimeError(RuntimeError):
    """Raised when Tao runtime generation cannot complete."""


class DaoClient:
    """Runtime client for Tao/Dao1 report explanation.

    The client is safe-by-default (`backend="disabled"`). Production deployments can
    opt into either an OpenAI-compatible HTTP endpoint or local Transformers loading.
    Deterministic rule outputs remain the source of truth; Tao only rewrites them into
    a teaching explanation and must pass output guards before being surfaced.
    """

    _model_lock = Lock()
    _tokenizer: Any = None
    _model: Any = None
    _model_signature: tuple[str, str, str, str] | None = None
    # Load lifecycle for the heavy transformers backend so callers (server health,
    # Colab warmup) can observe progress instead of blocking blindly on the first
    # request: "idle" → "loading" → "ready"/"error". This is what lets the UI report
    # "still loading" vs. "load failed" instead of surfacing an opaque Connection refused.
    _load_state: Literal["idle", "loading", "ready", "error"] = "idle"
    _load_error: str | None = None

    # Env var that hard-overrides the same-named profile parameter when explicitly set.
    _PARAM_ENV = {
        "temperature": "TAO_TEMPERATURE",
        "top_p": "TAO_TOP_P",
        "repetition_penalty": "TAO_REPETITION_PENALTY",
        "max_new_tokens": "TAO_MAX_NEW_TOKENS",
        "do_sample": "TAO_DO_SAMPLE",
    }

    def __init__(self, config: DaoGenerationConfig | None = None) -> None:
        self.config = config or DaoGenerationConfig.from_env()

    def _profile_params(self, profile: str) -> dict[str, Any]:
        """Resolve sampling params for a task profile: explicit env > profile > config."""

        params = {
            "temperature": self.config.temperature,
            "top_p": self.config.top_p,
            "repetition_penalty": self.config.repetition_penalty,
            "max_new_tokens": self.config.max_new_tokens,
            "do_sample": self.config.do_sample,
        }
        params.update(load_inference_profiles().get(profile, {}))
        for key, env_name in self._PARAM_ENV.items():
            if env_name in os.environ:
                # The operator asked for this value explicitly — honor it over the profile.
                params[key] = getattr(self.config, key)
        return params

    def load_status(self) -> dict[str, Any]:
        """Non-blocking snapshot of the model load lifecycle (safe to poll from /api/health).

        ``mock``/``http`` need no local weights, so they report ``ready`` immediately;
        ``disabled`` reports ``disabled``; ``transformers`` reflects the real load state
        (``idle`` → ``loading`` → ``ready``/``error``). This is read without the model lock
        on purpose so a health check never blocks behind a multi-minute 30B load.
        """

        backend = self.config.backend
        if backend == "disabled":
            state = "disabled"
        elif backend == "mock" or backend in OPENAI_COMPATIBLE_BACKENDS:
            state = "ready"
        else:
            state = self.__class__._load_state
        return {
            "backend": backend,
            "state": state,
            "model_loaded": self.__class__._model is not None,
            "error": self.__class__._load_error,
        }

    def preload(self) -> dict[str, Any]:
        """Eagerly load the runtime so the first real request is fast and failures are visible.

        Decoupling the heavy ``transformers`` load from the HTTP request handler is what
        prevents a load failure (e.g. an OOM-killed 30B FP16 load) from masquerading as an
        opaque ``Connection refused`` on the next warmup. Catchable errors are returned as
        ``{"ok": False, ...}`` with the real cause; ``mock``/``http``/``disabled`` are no-ops.
        """

        backend = self.config.backend
        if backend == "disabled":
            return {"ok": False, "state": "disabled", "backend": backend, "reason": "Tao backend disabled (set TAO_BACKEND=transformers/http/poe/minimax/azure/mock)."}
        if backend == "mock" or backend in OPENAI_COMPATIBLE_BACKENDS:
            self.__class__._load_state = "ready"
            return {"ok": True, "state": "ready", "backend": backend, "model_id": self.config.model_id}
        if backend == "transformers":
            self.__class__._load_state = "loading"
            self.__class__._load_error = None
            try:
                self._load_transformers_runtime()
            except Exception as exc:  # noqa: BLE001 — surface the real cause instead of crashing the preload thread
                self.__class__._load_state = "error"
                self.__class__._load_error = f"{type(exc).__name__}: {exc}"
                return {"ok": False, "state": "error", "backend": backend, "model_id": self.config.model_id, "reason": self.__class__._load_error}
            return {"ok": True, "state": "ready", "backend": backend, "model_id": self.config.model_id}
        return {"ok": False, "state": "error", "backend": backend, "reason": f"Unsupported Tao backend: {backend}"}

    def build_prompt(self, user_content: str, history: list[dict[str, str]] | None = None) -> str:
        text = f"<|im_start|>system\n{SYSTEM_PROMPT}<|im_end|>\n"
        for turn in history or []:
            text += f"<|im_start|>{turn['role']}\n{turn['content']}<|im_end|>\n"
        text += f"<|im_start|>user\n{user_content}<|im_end|>\n<|im_start|>assistant\n"
        return text

    def build_report_prompt(self, structured_rule_outputs: dict[str, Any]) -> str:
        rule_outputs = json.dumps(structured_rule_outputs, ensure_ascii=False, indent=2, default=str)
        return REPORT_PROMPT_TEMPLATE.format(rule_outputs=rule_outputs)

    def build_question_prompt(self, question_context: dict[str, Any]) -> str:
        context = json.dumps(question_context, ensure_ascii=False, indent=2, default=str)
        return QUESTION_PROMPT_TEMPLATE.format(question_context=context)

    def generate(self, structured_rule_outputs: dict[str, Any]) -> str:
        body = self.build_report_prompt(structured_rule_outputs)
        return self._dispatch(body, self._generate_mock(structured_rule_outputs), "report", profile="research_report")

    def generate_question_plan(self, question_context: dict[str, Any]) -> str:
        body = self.build_question_prompt(question_context)
        return self._dispatch(body, self._generate_question_mock(question_context), "question planning", profile="structured_json")

    def _dispatch(
        self,
        body: str,
        mock_value: str,
        task: str,
        profile: str = "structured_json",
        history: list[dict[str, str]] | None = None,
    ) -> str:
        """Backend dispatch shared by all generation tasks.

        ``body`` is the raw task text. The Qwen chat template is applied only on the
        local ``transformers`` path; the ``http`` path sends plain system+user messages
        so an OpenAI-compatible endpoint applies its own template exactly once.
        Deterministic callers remain responsible for guarding output before it
        can surface as clinical text; this only routes prompt → backend.
        """

        if self.config.backend == "disabled":
            raise DaoRuntimeError(f"Tao {task} runtime is disabled. Set TAO_BACKEND=http/poe/minimax/azure or transformers to enable.")
        # Model-call budget is charged HERE — the single funnel every generation task
        # passes through — so nested skill calls can never under-count model usage
        # the way planner-side guessing did (harness review v0.12).
        from backend.runtime.execution_context import charge_active_run

        exhausted = charge_active_run("model_call")
        if exhausted is not None:
            raise DaoRuntimeError(f"model-call budget exhausted before Tao {task} call ({exhausted.value}).")
        if self.config.backend == "mock":
            charge_active_run("model_output_chars", len(mock_value))
            return mock_value
        params = self._profile_params(profile)
        if self.config.backend in OPENAI_COMPATIBLE_BACKENDS:
            text = self._generate_http(body, history=history, params=params)
        elif self.config.backend == "transformers":
            text = self._generate_transformers(self.build_prompt(body, history), params=params)
        else:
            raise DaoRuntimeError(f"Unsupported Tao backend: {self.config.backend}")
        charge_active_run("model_output_chars", len(text or ""))
        return text

    def generate_followup_probes(self, probe_context: dict[str, Any]) -> str:
        max_probes = int(probe_context.get("max_probes", 2))
        body = FOLLOWUP_PROBE_PROMPT_TEMPLATE.format(
            max_probes=max_probes,
            probe_context=json.dumps(probe_context, ensure_ascii=False, indent=2, default=str),
        )
        return self._dispatch(body, self._mock_followup_probes(probe_context), "follow-up probe", profile="structured_json")

    def generate_reasoning(self, reasoning_context: dict[str, Any]) -> str:
        body = REASONING_PROMPT_TEMPLATE.format(
            reasoning_context=json.dumps(reasoning_context, ensure_ascii=False, indent=2, default=str)
        )
        return self._dispatch(body, self._mock_reasoning(reasoning_context), "reasoning", profile="research_report")

    def generate_experience_summary(self, summary_context: dict[str, Any]) -> str:
        body = EXPERIENCE_SUMMARY_PROMPT_TEMPLATE.format(
            summary_context=json.dumps(summary_context, ensure_ascii=False, indent=2, default=str)
        )
        return self._dispatch(body, self._mock_experience_summary(summary_context), "experience summary", profile="research_report")

    def route_skill(self, routing_context: dict[str, Any]) -> str:
        body = SKILL_ROUTING_PROMPT_TEMPLATE.format(
            routing_context=json.dumps(routing_context, ensure_ascii=False, indent=2, default=str)
        )
        return self._dispatch(body, self._mock_route_skill(routing_context), "skill routing", profile="structured_json")

    def generate_consultation(self, consultation_context: dict[str, Any]) -> str:
        """Tao-primary professional answer: the model is the main reasoner.

        Unlike the JSON overlay tasks, this returns free-form professional Markdown grounded
        in the supplied rule/mined evidence — the model combines those experience cues with
        its own TCM knowledge. The caller guards it with ``guard_consultation`` before use.
        """

        body = CONSULTATION_PROMPT_TEMPLATE.format(
            scope=consultation_context.get("scope", "全面会诊"),
            question=consultation_context.get("question", ""),
            evidence=json.dumps(consultation_context.get("evidence", {}), ensure_ascii=False, indent=2, default=str),
        )
        return self._dispatch(body, self._mock_consultation(consultation_context), "consultation", profile="teaching_explanation")

    def extract_slots(self, user_text: str) -> str:
        """Tao extracts structured YaoBi slots from one free-text turn (JSON object)."""

        body = INTERVIEW_EXTRACTION_PROMPT_TEMPLATE.format(user_text=user_text)
        return self._dispatch(body, self._mock_extract_slots(user_text), "slot extraction", profile="structured_json")

    def generate_interview_question(self, interview_context: dict[str, Any]) -> str:
        """Tao autonomously asks the next follow-up turn, grounded in stage/slots/patterns."""

        body = INTERVIEW_QUESTION_PROMPT_TEMPLATE.format(
            stage=interview_context.get("stage", ""),
            stage_goal=interview_context.get("stage_goal", ""),
            target_slots=", ".join(interview_context.get("target_slots", []) or []) or "（无指定，按鉴别价值自选）",
            candidate_patterns=json.dumps(interview_context.get("candidate_patterns", []), ensure_ascii=False, default=str),
            case_summary=json.dumps(interview_context.get("case_summary", {}), ensure_ascii=False, default=str),
        )
        return self._dispatch(body, self._mock_interview_question(interview_context), "interview question", profile="teaching_explanation")

    def generate_emergency_referral(self, referral_context: dict[str, Any]) -> str:
        """Tao reasons over detected red flags and produces ER referral clinical guidance.

        Output is free-form Markdown for clinicians (not a diagnosis or prescription):
        clinical significance of each flag, what to bring to the ER, precautions, and urgency
        classification.  The caller guards the result with ``guard_consultation`` before use.
        """

        flags = referral_context.get("red_flags") or []
        body = EMERGENCY_REFERRAL_PROMPT_TEMPLATE.format(
            red_flags="\n".join(f"- {f}" for f in flags) or "- （危险信号待明确）",
            case_summary=json.dumps(referral_context.get("case_summary", {}), ensure_ascii=False, indent=2, default=str),
        )
        return self._dispatch(body, self._mock_emergency_referral(referral_context), "emergency referral", profile="teaching_explanation")

    def _mock_emergency_referral(self, ctx: dict[str, Any]) -> str:
        return deterministic_emergency_guidance(ctx.get("red_flags") or [], ctx.get("safety_level", "high"))

    def generate_probe_questions(self, probe_context: dict[str, Any]) -> str:
        """Tao-primary follow-up: the model freely asks the next clarifying questions."""

        max_probes = int(probe_context.get("max_probes", 2))
        body = PROBE_FREEFORM_PROMPT_TEMPLATE.format(
            max_probes=max_probes,
            theme=probe_context.get("current_state_theme", "本状态主题"),
            context=json.dumps(
                {
                    "last_answers": probe_context.get("last_answers", {}),
                    "normalized_tags": probe_context.get("normalized_tags", []),
                    # Rule-engine grounding (tags / top syndromes / formula routes) so the
                    # model can probe the most discriminative gap, not just the last answer.
                    "rule_context": probe_context.get("rule_context", {}),
                },
                ensure_ascii=False, default=str,
            ),
        )
        return self._dispatch(body, self._mock_probe_questions(probe_context), "probe questions", profile="teaching_explanation")

    def plan_skills(self, plan_context: dict[str, Any]) -> str:
        max_steps = int(plan_context.get("max_steps", 4))
        body = SKILL_PLAN_PROMPT_TEMPLATE.format(
            max_steps=max_steps,
            plan_context=json.dumps(plan_context, ensure_ascii=False, indent=2, default=str),
        )
        return self._dispatch(body, self._mock_plan_skills(plan_context), "skill planning", profile="structured_json")

    def chat(
        self,
        history: list[dict[str, str]],
        user_input: str,
        stream_callback: Any | None = None,
    ) -> str:
        """Direct Tao chat inference without a FastAPI wrapper.

        This mirrors the Dao1/Tao model-card style: build a Qwen chat prompt, load
        `CMLM/Dao1-30b-a3b` through Transformers when `backend=transformers`, and
        optionally stream decoded tokens to a callback. The caller still remains
        responsible for applying task-specific guards before surfacing clinical text.
        """

        if self.config.backend == "disabled":
            raise DaoRuntimeError("Tao direct chat is disabled. Set TAO_BACKEND=transformers for local model inference.")
        from backend.runtime.execution_context import charge_active_run

        exhausted = charge_active_run("model_call")
        if exhausted is not None:
            raise DaoRuntimeError(f"model-call budget exhausted before Tao chat call ({exhausted.value}).")
        if self.config.backend == "mock":
            return "Tao mock direct reply: 已收到问题；当前项目中模型输出仍需规则与安全 guard 复核。"
        params = self._profile_params("teaching_explanation")
        if self.config.backend in OPENAI_COMPATIBLE_BACKENDS:
            text = self._generate_http(user_input, history=history, params=params)
        elif self.config.backend == "transformers":
            text = self._generate_transformers(self.build_prompt(user_input, history), stream_callback=stream_callback, params=params)
        else:
            raise DaoRuntimeError(f"Unsupported Tao backend: {self.config.backend}")
        charge_active_run("model_output_chars", len(text or ""))
        return text

    def _generate_mock(self, structured_rule_outputs: dict[str, Any]) -> str:
        tags = "、".join(structured_rule_outputs.get("normalized_tags", [])[:8]) or "未提供"
        route = (structured_rule_outputs.get("formula_route") or {}).get("name", "未形成稳定路线")
        return json.dumps(
            {
                "markdown_report": (
                    "# Tao 教学解释补充\n\n"
                    f"- 规则证据标签：{tags}\n"
                    f"- 方剂路线信号：{route}。\n"
                    "- 以上仅为沈钦荣腰痹经验规则的教学解释，需医生结合查体、影像与舌脉复核。\n"
                    "- 本内容不构成诊断、处方或治疗建议。"
                ),
                "final_diagnosis": None,
                "complete_prescription": None,
                "patient_executable_dose": None,
                "administration_instruction": None,
            },
            ensure_ascii=False,
        )

    def _generate_question_mock(self, question_context: dict[str, Any]) -> str:
        questions = []
        for question in question_context.get("candidate_questions", [])[:3]:
            questions.append({
                "id": question.get("id"),
                "question": question.get("question"),
                "reason": f"Tao结合当前规则线索建议追问：{question.get('reason', '补齐关键变量')}"
            })
        return json.dumps({
            "questions": questions,
            "final_diagnosis": None,
            "complete_prescription": None,
            "patient_executable_dose": None,
            "administration_instruction": None,
        }, ensure_ascii=False)

    def _mock_followup_probes(self, probe_context: dict[str, Any]) -> str:
        allowed = probe_context.get("allowed_fields") or []
        theme = probe_context.get("current_state_theme", "本状态主题")
        max_probes = int(probe_context.get("max_probes", 2))
        probes = []
        for field in allowed[:max_probes]:
            probes.append({
                "probe_text": f"围绕{theme}，能否再具体描述与“{field}”相关的细节？",
                "field_hint": field,
                "reason": "Tao结合上一轮回答，在本状态主题内补充澄清，用于区分鉴别线索（待医师复核）。",
            })
        if not probes:
            probes.append({
                "probe_text": f"关于{theme}，还有没有补充的细节想告诉医生？",
                "field_hint": None,
                "reason": "Tao在本状态主题内做开放补充，仅作为线索，不做判断。",
            })
        return json.dumps({
            "probes": probes[:max_probes],
            "final_diagnosis": None,
            "complete_prescription": None,
            "patient_executable_dose": None,
            "administration_instruction": None,
        }, ensure_ascii=False)

    def _mock_reasoning(self, reasoning_context: dict[str, Any]) -> str:
        chain = reasoning_context.get("reasoning_chain") or []
        lines = ["# Tao 辨证推理教学解释（叠加层）", ""]
        for step in chain:
            lines.append(f"- **{step.get('title', '')}**：{step.get('content', '')}")
        lines.append("")
        lines.append("以上为基于规则结论的推理过程语言化表达，全部为倾向性、非最终口吻，需医师结合查体、影像、舌脉审定。")
        return json.dumps({
            "reasoning_markdown": "\n".join(lines),
            "final_diagnosis": None,
            "complete_prescription": None,
            "patient_executable_dose": None,
            "administration_instruction": None,
        }, ensure_ascii=False)

    def _mock_experience_summary(self, summary_context: dict[str, Any]) -> str:
        mode = summary_context.get("mode", "case")
        points = list(summary_context.get("key_points_seed") or [])
        if mode == "experience":
            body = "# 沈钦荣腰痹经验规律总结（脱敏统计，教学用）\n\n以上规律来自脱敏聚合统计，体现高频证候、核心方剂路线与用药特色，均为待专家审核的研究信号。"
        else:
            body = "# 医案按语（教学复盘）\n\n本案辨证、治法与方剂路线倾向见上，体现沈老益气养血、温通经络、顾护肝肾脾胃的思路，仅作教学复盘，需医师审核。"
        if not points:
            points = ["辨证以证候倾向为纲，结合腰腿症状与神经线索", "治法体现温通与扶正并重", "用药特色需结合安全复核"]
        return json.dumps({
            "summary_markdown": body,
            "key_points": points,
            "final_diagnosis": None,
            "complete_prescription": None,
            "patient_executable_dose": None,
            "administration_instruction": None,
        }, ensure_ascii=False)

    def _mock_route_skill(self, routing_context: dict[str, Any]) -> str:
        # The mock honours the deterministic keyword hint so routing is testable;
        # a real backend would let the model choose from allowed_intents itself.
        allowed = routing_context.get("allowed_intents") or []
        hint = routing_context.get("hint_intent")
        intent = hint if hint in allowed else (allowed[0] if allowed else "capabilities")
        return json.dumps({"intent": intent, "reason": "Tao 依据问题语义选择该技能（mock 沿用关键词提示）。"}, ensure_ascii=False)

    def _mock_plan_skills(self, plan_context: dict[str, Any]) -> str:
        # Honour the deterministic hint plan so multi-step planning stays testable;
        # a real backend would decompose the question into ordered intents itself.
        allowed = set(plan_context.get("allowed_intents") or [])
        hint = [s for s in (plan_context.get("hint_plan") or []) if s.get("intent") in allowed]
        if not hint:
            hint = [{"intent": (plan_context.get("allowed_intents") or ["capabilities"])[0], "reason": "默认单步"}]
        return json.dumps({"plan": hint}, ensure_ascii=False)

    def _mock_consultation(self, ctx: dict[str, Any]) -> str:
        ev = ctx.get("evidence", {}) or {}
        question = str(ctx.get("question", "")).strip()
        scope = ctx.get("scope", "全面会诊")
        syns = ev.get("syndrome_candidates") or []
        routes = ev.get("formula_routes") or []
        modules = ev.get("herb_modules") or []
        tags = ev.get("normalized_tags") or []
        safety = ev.get("safety") or {}
        clue = "、".join(tags[:8]) or "待四诊补充"
        # Abstain instead of confabulating: with no rule-backed syndrome candidate and no
        # formula route in the evidence bundle, the consultation must not fill the gap
        # with default TCM content ("气血痹阻"/"独活寄生汤") — it asks for the missing
        # four-diagnosis facts instead. Real backends are held to the same contract by
        # tao_consultation_skill's groundedness downgrade.
        if not syns and not routes:
            return (
                f"# 腰痹病案分析（{scope} · 供执业医师审核）\n\n"
                f"## 证据不足，暂不给出证型与方剂路线\n"
                f"就所述「{question[:80]}」，当前病例线索（{clue}）不足以在规则证据下形成任何证型倾向或方剂路线；"
                "为避免无依据推断，本轮不提出具体证型、方剂或药物模块。\n\n"
                "## 建议补充的关键信息\n"
                "- 疼痛性质（酸/刺/胀/冷痛）、部位与放射情况，加重与缓解因素（遇冷/劳累/夜间）；\n"
                "- 舌象（舌色、苔质）与脉象；\n"
                "- 病程、既往诊断与影像资料；纳眠、二便与合并病用药。\n\n"
                f"## 安全提示\n安全状态参考：{safety.get('status', '待评估')}。"
                "如出现大小便异常、会阴麻木、进行性无力、发热寒战、外伤后剧痛等红旗信号，请先急诊/线下评估。\n\n"
                "> 本分析为供执业医师审核的研究 / 教学草案，最终诊断与处方须医师面诊后确定，患者不可据此自行用药。"
            )
        top = (syns[0].get("name") or syns[0].get("pattern")) if syns else "（规则证据未给出证型候选，待医师判定）"
        alt = "、".join(filter(None, ((s.get("name") or s.get("pattern") or "") for s in syns[1:3]))) or "（无次选）"
        route = routes[0].get("name") if routes else None
        route_line = (
            f"可考虑「{route}」为底化裁：其组方思路与本案病机相合，具体药味取舍、"
            f"配伍比例与随证加减须医师按面诊所见审定。"
            if route
            else "规则证据未给出稳定方剂路线，本轮不提出具体方剂；建议补充关键证候变量后由医师判定。"
        )
        mods = "、".join(m.get("name", "") for m in modules[:4]) or "（无匹配模块，待医师补充）"
        return (
            f"# 腰痹病案分析（{scope} · 结合沈钦荣经验 · 供执业医师审核）\n\n"
            f"## 一、四诊辨析与病机\n"
            f"据所述「{question[:80]}」，本案以腰痛为主症。结合线索（{clue}）：跌扑、负重每致经筋损伤、气血瘀阻；"
            f"久则气血亏虚、筋脉失养；遇冷加重、得温则缓提示寒凝经脉、阳气不展；脉细多为气血不足之象。"
            f"病位在腰府筋脉，与肝、肾、脾相关，病性多属本虚标实、虚实夹杂。\n\n"
            f"## 二、证型判断（倾向性，供医师审定）\n"
            f"主证倾向「{top}」；次选可虑「{alt}」。辨证依据：上述症舌脉与既往史的相互印证，"
            f"以及沈老“益气养血、温通经络、顾护肝肾脾胃”之经验思路。\n\n"
            f"## 三、治法\n以益气养血、温经通络为主，兼顾活血化瘀、补益肝肾，标本同治。\n\n"
            f"## 四、选方与方义\n{route_line}\n\n"
            f"## 五、用药功效模块与经验剂量范围\n"
            f"涉及模块：{mods}。各药经验用量区间需由医师按体质、合并病与耐受审定，此处不作患者可执行医嘱。\n\n"
            f"## 六、安全、鉴别与随访\n"
            f"安全状态参考：{safety.get('status', '待评估')}。附片、细辛、虫类等须医师重点审核配伍与用量；"
            f"注意排除马尾综合征、肿瘤、感染、骨折等红旗信号；合并病及肝肾功能、胃肠耐受需复核；"
            f"建议面诊查体与影像复核后随访调整。\n\n"
            f"> 本分析为供执业医师审核的研究 / 教学草案，最终诊断与处方须医师面诊后确定，患者不可据此自行用药。"
        )

    def _mock_probe_questions(self, ctx: dict[str, Any]) -> str:
        theme = ctx.get("current_state_theme", "本状态主题")
        bank = {
            "疼痛": ["这种腰痛更像胀痛、刺痛还是冷痛？", "受凉或阴雨天疼痛会不会明显加重，热敷能缓解吗？"],
            "放射": ["腰痛会不会串到臀部或下肢？走一段路是否加重、休息能否缓解？", "咳嗽或用力时腿部串痛会加重吗？"],
            "中医": ["平时怕冷还是怕热？口苦、口干明显吗？", "睡眠和胃口怎么样，和腰痛发作有没有一起变化？"],
            "合并": ["以前吃过的止痛药有没有引起胃部不适或反酸？", "是否在用抗凝、激素或降糖类药物？"],
        }
        chosen: list[str] = []
        for key, qs in bank.items():
            if key in theme:
                chosen = qs
                break
        if not chosen:
            chosen = ["关于" + theme + "，还有哪些细节想补充给医生？", "这些症状最近有没有新的变化？"]
        return "\n".join(chosen[: int(ctx.get("max_probes", 2))])

    def _mock_extract_slots(self, user_text: str) -> str:
        import re as _re

        t = user_text or ""
        def has(*ks: str) -> bool:
            return any(k in t for k in ks)

        def tri(*ks: str) -> bool | None:
            # negation-aware tri-state: True (present) / False (explicitly denied) / None (unmentioned),
            # so "没有无力 / 无发热 / 否认外伤" records an answered-absent slot rather than a missing one.
            found_pos = found_neg = False
            for k in ks:
                for m in _re.finditer(_re.escape(k), t):
                    pre = t[max(0, m.start() - 3):m.start()]
                    if any(n in pre for n in ("无", "没", "否", "非", "排除", "未")):
                        found_neg = True
                    else:
                        found_pos = True
            return True if found_pos else (False if found_neg else None)

        demo: dict[str, Any] = {}
        pain: dict[str, Any] = {}
        ortho: dict[str, Any] = {}
        tcm: dict[str, Any] = {}
        hist: dict[str, Any] = {}

        m = _re.search(r"(\d{1,3})\s*岁", t)
        if m:
            demo["age"] = int(m.group(1))
        elif "青年" in t:
            demo["age"] = 28
        if "女" in t:
            demo["sex"] = "女"
        elif "男" in t:
            demo["sex"] = "男"

        if has("腰腿", "腿痛", "下肢"):
            pain["pain_location"] = "腰部及下肢"
        elif has("腰"):
            pain["pain_location"] = "腰部"
        for key, kws in (
            ("radiation", ("放射", "串", "窜")), ("numbness", ("麻", "麻木")),
            ("weakness", ("无力", "乏力", "抬不起")), ("night_pain", ("夜间痛", "夜里痛", "夜间加重", "夜痛")),
            ("cold_damp_trigger", ("遇冷", "受寒", "受凉", "阴雨", "涉水", "天冷")),
            ("trauma_history", ("跌", "摔", "扭", "外伤", "跌扑", "闪了", "搬")),
        ):
            v = tri(*kws)
            if v is not None:
                pain[key] = v
        for nature in ("刺痛", "冷痛", "胀痛", "灼痛", "酸痛"):
            if nature in t:
                pain["pain_nature"] = nature
                break
        md = _re.search(r"(\d+\s*[年月周天])", t)
        if md:
            pain["duration"] = md.group(1)
        elif "反复" in t:
            pain["onset"] = "反复发作"

        for key, kws in (
            ("bowel_bladder_dysfunction", ("大小便", "失禁", "尿不", "解不出")), ("saddle_anesthesia", ("会阴", "鞍区")),
            ("progressive_weakness", ("进行性", "越来越无力", "越来越没力")), ("fever", ("发热", "发烧", "寒战")),
            ("tumor_history", ("肿瘤", "癌")), ("unexplained_weight_loss", ("消瘦", "体重下降", "变瘦")),
            ("severe_trauma", ("车祸", "高处坠", "重物砸", "严重外伤")),
        ):
            v = tri(*kws)
            if v is not None:
                ortho[key] = v

        if has("怕冷", "畏寒"):
            tcm["cold_heat"] = "怕冷"
        elif has("怕热", "五心烦热"):
            tcm["cold_heat"] = "怕热"
        if has("冷痛"):
            tcm["cold_pain"] = True
        if has("困重", "沉重"):
            tcm["limb_heaviness"] = True
        if has("定处", "固定"):
            tcm["fixed_pain"] = True
        if has("腰膝酸软", "酸软"):
            tcm["waist_knee_soreness"] = True
        if has("口干", "口渴"):
            tcm["thirst"] = "口干"
        if has("失眠", "睡不", "多梦", "早醒"):
            tcm["sleep"] = "欠佳"
        if has("纳差", "胃口差", "食少"):
            tcm["appetite"] = "纳差"
        if "舌淡" in t:
            tcm["tongue_body"] = "淡"
        elif has("舌暗", "暗紫", "紫暗"):
            tcm["tongue_body"] = "暗紫"
        elif "舌红" in t:
            tcm["tongue_body"] = "红"
        if "薄白" in t:
            tcm["tongue_coating"] = "薄白"
        elif "白腻" in t:
            tcm["tongue_coating"] = "白腻"
        elif "黄腻" in t:
            tcm["tongue_coating"] = "黄腻"
        for pulse in ("细", "弦", "沉", "滑", "数", "紧"):
            if f"脉{pulse}" in t or f"脉象{pulse}" in t:
                tcm["pulse"] = pulse
                break

        if "骨质疏松" in t:
            hist["osteoporosis"] = True
        if has("椎间盘", "椎管狭窄", "滑脱"):
            hist["western_diagnosis"] = t[:60]
        if has("MRI", "CT", "X线", "X光", "核磁", "片子", "磁共振"):
            hist["imaging"] = "有影像检查"

        out: dict[str, Any] = {}
        if demo:
            out["demographics"] = demo
        if "腰" in t:
            out["chief_complaint"] = t[:40]
        if pain:
            out["pain_slots"] = pain
        if ortho:
            out["ortho_neuro_slots"] = ortho
        if tcm:
            out["tcm_slots"] = tcm
        if hist:
            out["history_slots"] = hist
        return json.dumps(out, ensure_ascii=False)

    def _mock_interview_question(self, ctx: dict[str, Any]) -> str:
        targets = ctx.get("target_slots", []) or []
        phrase = {
            "chief_complaint": "目前最主要的不适是什么，痛了多久了",
            "bowel_bladder_dysfunction": "最近有没有大小便控制困难、或突然解不出来",
            "saddle_anesthesia": "会阴部（坐着接触座位的部位）有没有发麻",
            "progressive_weakness": "下肢力气是不是越来越差、走路发软或拖步",
            "severe_trauma": "这次发作前有没有明显的摔伤、扭伤或外伤",
            "fever": "有没有发热、寒战或近期感染",
            "tumor_history": "既往有没有肿瘤病史，近期有没有不明原因消瘦",
            "night_pain": "夜里痛得明显吗，会不会痛醒",
            "radiation": "腰痛会不会向臀部或腿脚放射",
            "numbness": "腿脚有没有发麻",
            "weakness": "腿有没有发软、使不上劲",
            "cold_damp_trigger": "受凉或阴雨天会不会加重，热敷能不能缓解",
            "pain_nature": "疼痛更像酸痛、胀痛、刺痛还是冷痛",
            "duration": "这次腰痛大概持续多久了，是急性还是反复发作",
            "tongue_body": "方便的话看下舌头，舌质偏淡、偏红还是偏暗紫",
            "tongue_coating": "舌苔是薄白、白腻还是黄腻",
            "pulse": "如果量过脉，脉象偏细、偏弦还是偏沉",
            "cold_heat": "平时是怕冷还是怕热",
            "waist_knee_soreness": "有没有腰膝酸软、乏力的感觉",
            "sleep": "睡眠怎么样",
            "stool": "大便情况如何",
            "urine": "小便清长还是偏黄",
            "western_diagnosis": "之前医院有没有诊断过（如椎间盘突出、椎管狭窄）",
            "imaging": "做过腰椎 X 线、CT 或核磁吗",
            "osteoporosis": "有没有骨质疏松",
        }
        chosen = [phrase[s] for s in targets if s in phrase][:4]
        if not chosen:
            chosen = ["还可以多说说目前最困扰你的症状，以及加重或缓解的因素"]
        return "为了更准确地帮医生判断，我想再了解几点：" + "；".join(chosen) + "？"

    # Transient HTTP failures (timeouts, 5xx, connection resets) are retried this many
    # times with a short backoff before the caller falls back to deterministic rules.
    _HTTP_MAX_ATTEMPTS = 3
    _HTTP_BACKOFF_SECONDS = 1.0
    # Bounded response read: chat completions are text; anything past this is a
    # misbehaving/hostile endpoint, not a longer answer.
    _HTTP_MAX_RESPONSE_BYTES = 8 * 1024 * 1024

    def _check_egress(self, url: str) -> None:
        """Minimal outbound policy for model endpoints (v0.14).

        Patient narratives and the API key travel to this URL, so two invariants are
        enforced before any request: (1) non-local endpoints must use HTTPS unless
        the operator explicitly sets ``YAOBI_ALLOW_INSECURE_EGRESS=1`` (self-hosted
        lab setups); (2) when ``YAOBI_EGRESS_ALLOWED_HOSTS`` (comma-separated) is
        configured, the endpoint host must be on it — a mistyped TAO_ENDPOINT_URL
        then fails fast instead of shipping PHI + credentials to an arbitrary host.
        """

        parts = urllib.parse.urlsplit(url)
        host = (parts.hostname or "").lower()
        local = host in {"localhost", "127.0.0.1", "::1"} or host.endswith(".local")
        if parts.scheme != "https" and not local \
                and os.getenv("YAOBI_ALLOW_INSECURE_EGRESS", "").lower() not in {"1", "true", "yes"}:
            raise DaoRuntimeError(
                f"insecure model egress blocked: {parts.scheme}://{host} is not HTTPS "
                "(set YAOBI_ALLOW_INSECURE_EGRESS=1 only for trusted lab networks)."
            )
        allowed_raw = os.getenv("YAOBI_EGRESS_ALLOWED_HOSTS", "").strip()
        if allowed_raw:
            allowed = {h.strip().lower() for h in allowed_raw.split(",") if h.strip()}
            if host not in allowed:
                raise DaoRuntimeError(
                    f"model egress host '{host}' is not in YAOBI_EGRESS_ALLOWED_HOSTS."
                )

    def _resolve_http_provider(self) -> tuple[str, dict[str, str], str | None]:
        """Resolve (request_url, auth_headers, payload_model_or_None) per provider.

        The named providers share the OpenAI-compatible wire shape but differ in
        exactly the three things this returns:
        - azure (dated api-version, default): URL is ``{resource}/openai/deployments/
          {deployment}/chat/completions?api-version=...`` (built here when
          endpoint_url is a resource base), auth is an ``api-key`` header, and the
          model is selected by the deployment in the URL — no in-payload "model".
        - azure (``TAO_AZURE_API_VERSION=v1``/``preview``): the newer
          ``{resource}/openai/v1/chat/completions`` surface — no dated api-version
          query parameter; the deployment name travels as the payload "model".
        - poe / minimax / http: Bearer auth, model in the payload. The hosted
          providers must have a key (an unauthenticated call can only fail); the
          generic ``http`` backend keeps supporting keyless self-hosted endpoints.
        """

        backend = self.config.backend
        endpoint = self.config.endpoint_url
        if backend == "azure":
            if not endpoint:
                raise DaoRuntimeError(
                    "Azure OpenAI endpoint is required. Set AZURE_OPENAI_ENDPOINT "
                    "(resource base) or TAO_ENDPOINT_URL (full completions URL)."
                )
            if not self.config.api_key:
                raise DaoRuntimeError("Azure OpenAI API key is required. Set TAO_API_KEY or AZURE_OPENAI_API_KEY.")
            api_version = (self.config.azure_api_version or "").strip().lower()
            if api_version in {"v1", "preview"}:
                url = endpoint
                if "/chat/completions" not in url:
                    url = f"{endpoint.rstrip('/')}/openai/v1/chat/completions"
                if api_version == "preview" and "api-version=" not in url:
                    separator = "&" if "?" in url else "?"
                    url = f"{url}{separator}api-version=preview"
                self._check_egress(url)
                # v1 surface: the deployment name travels as the payload "model".
                return url, {"api-key": self.config.api_key}, self.config.azure_deployment or self.config.model_id
            url = endpoint
            if "/chat/completions" not in url:
                deployment = self.config.azure_deployment or self.config.model_id
                url = (
                    f"{endpoint.rstrip('/')}/openai/deployments/"
                    f"{urllib.parse.quote(deployment, safe='')}/chat/completions"
                )
            if "api-version=" not in url:
                separator = "&" if "?" in url else "?"
                url = f"{url}{separator}api-version={urllib.parse.quote(self.config.azure_api_version, safe='')}"
            self._check_egress(url)
            return url, {"api-key": self.config.api_key}, None
        if not endpoint:
            raise DaoRuntimeError(f"TAO_ENDPOINT_URL is required when TAO_BACKEND={backend}.")
        if backend in {"poe", "minimax"} and not self.config.api_key:
            provider_env = _PROVIDER_KEY_ENVS[backend][-1]
            raise DaoRuntimeError(f"{backend} API key is required. Set TAO_API_KEY or {provider_env}.")
        self._check_egress(endpoint)
        headers = {"Authorization": f"Bearer {self.config.api_key}"} if self.config.api_key else {}
        return endpoint, headers, self.config.model_id

    def _generate_http(
        self,
        user_content: str,
        history: list[dict[str, str]] | None = None,
        params: dict[str, Any] | None = None,
    ) -> str:
        """Call an OpenAI-compatible endpoint (http/poe/minimax/azure) with chat messages.

        The endpoint applies its own chat template, so we must NOT send the locally
        templated ``<|im_start|>`` prompt string here — only raw message contents.
        Every network/parse failure is converted to ``DaoRuntimeError`` so callers keep
        their deterministic-fallback guarantee instead of surfacing an HTTP 500.
        """

        url, auth_headers, payload_model = self._resolve_http_provider()
        label = self.config.backend
        params = params or self._profile_params("teaching_explanation")
        messages = [{"role": "system", "content": SYSTEM_PROMPT}]
        for turn in history or []:
            messages.append({"role": turn["role"], "content": turn["content"]})
        messages.append({"role": "user", "content": user_content})
        payload = {
            "messages": messages,
            "temperature": params["temperature"],
            "top_p": params["top_p"],
            "max_tokens": params["max_new_tokens"],
        }
        if payload_model:
            payload["model"] = payload_model
        data = json.dumps(payload).encode("utf-8")
        headers = {"Content-Type": "application/json", **auth_headers}

        last_error: Exception | None = None
        for attempt in range(self._HTTP_MAX_ATTEMPTS):
            if attempt:
                # Each RETRY is a real provider request: cost, latency, quota and an
                # additional outbound transmission of the payload — charge it against
                # the model-call budget instead of hiding 3 requests behind 1 charge
                # (v0.14 review; the first attempt was charged in _dispatch/chat).
                from backend.runtime.execution_context import charge_active_run

                exhausted = charge_active_run("model_call")
                if exhausted is not None:
                    raise DaoRuntimeError(
                        f"model-call budget exhausted during retry {attempt + 1} ({exhausted.value})."
                    ) from last_error
            request = urllib.request.Request(url, data=data, headers=headers, method="POST")
            try:
                with urllib.request.urlopen(request, timeout=self.config.timeout_seconds) as response:
                    # Bounded read: a misbehaving endpoint must not OOM the server.
                    raw = response.read(self._HTTP_MAX_RESPONSE_BYTES + 1)
                    if len(raw) > self._HTTP_MAX_RESPONSE_BYTES:
                        raise DaoRuntimeError(
                            f"Tao {label} endpoint response exceeded {self._HTTP_MAX_RESPONSE_BYTES} bytes."
                        )
                    body = json.loads(raw.decode("utf-8"))
                break
            except urllib.error.HTTPError as exc:
                last_error = exc
                if exc.code < 500:
                    # Client errors (bad key, bad payload) will not heal on retry.
                    raise DaoRuntimeError(f"Tao {label} endpoint returned {exc.code}: {exc.reason}") from exc
            except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError) as exc:
                last_error = exc
            if attempt < self._HTTP_MAX_ATTEMPTS - 1:
                time.sleep(self._HTTP_BACKOFF_SECONDS * (2**attempt))
        else:
            raise DaoRuntimeError(
                f"Tao {label} endpoint failed after {self._HTTP_MAX_ATTEMPTS} attempts: "
                f"{type(last_error).__name__}: {last_error}"
            ) from last_error

        # MiniMax reports failures as HTTP 200 + base_resp.status_code != 0 (e.g. 1004
        # auth failed, 1008 insufficient balance) — surface those as runtime errors
        # instead of returning an empty/garbage completion downstream.
        base_resp = body.get("base_resp") if isinstance(body, dict) else None
        if isinstance(base_resp, dict) and base_resp.get("status_code") not in (None, 0):
            raise DaoRuntimeError(
                f"Tao {label} endpoint error {base_resp.get('status_code')}: {base_resp.get('status_msg')}"
            )

        # Robust completion extraction (v0.14): empty choices, null content, provider
        # refusals and content-filter finishes are distinct, explicit failures — a
        # filtered/empty completion must never be treated as a successful answer.
        if "choices" in body:
            choices = body.get("choices")
            if not isinstance(choices, list) or not choices:
                raise DaoRuntimeError(f"Tao {label} endpoint returned an empty choices list.")
            choice = choices[0] or {}
            finish_reason = choice.get("finish_reason")
            if finish_reason == "content_filter":
                raise DaoRuntimeError(f"Tao {label} endpoint filtered the completion (finish_reason=content_filter).")
            message = choice.get("message") or {}
            refusal = message.get("refusal")
            if refusal:
                raise DaoRuntimeError(f"Tao {label} endpoint refused the request: {str(refusal)[:200]}")
            content = message.get("content") or choice.get("text")
            if content:
                return content
            raise DaoRuntimeError(
                f"Tao {label} endpoint returned an empty completion (finish_reason={finish_reason})."
            )
        if "text" in body:
            return body["text"]
        if "generated_text" in body:
            return body["generated_text"]
        raise DaoRuntimeError(f"Tao {label} endpoint response did not contain choices/text/generated_text.")

    def _load_transformers_runtime(self) -> tuple[Any, Any, Any]:
        if importlib.util.find_spec("transformers") is None:
            raise DaoRuntimeError("transformers is not installed. Install optional runtime dependencies before TAO_BACKEND=transformers.")
        if importlib.util.find_spec("torch") is None:
            raise DaoRuntimeError("torch is not installed. Install optional runtime dependencies before TAO_BACKEND=transformers.")

        transformers = importlib.import_module("transformers")
        torch = importlib.import_module("torch")

        quant = "4bit" if self.config.load_in_4bit else "8bit" if self.config.load_in_8bit else "none"
        signature = (self.config.model_id, self.config.model_revision, f"{self.config.torch_dtype}:{quant}", self.config.device_map, self.config.attn_implementation)
        with self._model_lock:
            if (
                self.__class__._tokenizer is None
                or self.__class__._model is None
                or self.__class__._model_signature != signature
            ):
                self.__class__._load_state = "loading"
                self.__class__._load_error = None
                try:
                    dtype = getattr(torch, self.config.torch_dtype, torch.float16)
                    from_pretrained_kwargs: dict[str, Any] = {
                        "trust_remote_code": True,
                        "device_map": self.config.device_map,
                        "attn_implementation": self.config.attn_implementation,
                    }
                    if self.config.model_revision:
                        from_pretrained_kwargs["revision"] = self.config.model_revision
                    # Optional 4-bit / 8-bit quantization lets large models (e.g. the 30B MoE
                    # CMLM/Dao1-30b-a3b) fit a single A100/L4; requires the bitsandbytes package.
                    if self.config.load_in_4bit or self.config.load_in_8bit:
                        from_pretrained_kwargs["quantization_config"] = transformers.BitsAndBytesConfig(
                            load_in_4bit=self.config.load_in_4bit,
                            load_in_8bit=self.config.load_in_8bit and not self.config.load_in_4bit,
                            bnb_4bit_compute_dtype=dtype,
                            bnb_4bit_quant_type="nf4",
                            bnb_4bit_use_double_quant=True,
                        )
                    else:
                        from_pretrained_kwargs["torch_dtype"] = dtype
                    self.__class__._tokenizer = transformers.AutoTokenizer.from_pretrained(
                        self.config.model_id, trust_remote_code=True,
                        **({"revision": self.config.model_revision} if self.config.model_revision else {}),
                    )
                    self.__class__._model = transformers.AutoModelForCausalLM.from_pretrained(
                        self.config.model_id,
                        **from_pretrained_kwargs,
                    )
                    self.__class__._model_signature = signature
                except Exception as exc:  # noqa: BLE001 — record cause so health/warmup report it, then re-raise
                    self.__class__._load_state = "error"
                    self.__class__._load_error = f"{type(exc).__name__}: {exc}"
                    raise
            self.__class__._load_state = "ready"
        return transformers, self.__class__._tokenizer, self.__class__._model

    def _model_input_device(self, model: Any) -> Any | None:
        embeddings = model.get_input_embeddings() if hasattr(model, "get_input_embeddings") else None
        weight = getattr(embeddings, "weight", None)
        if getattr(weight, "device", None) is not None:
            return weight.device
        if getattr(model, "device", None) is not None:
            return model.device
        device_map = getattr(model, "hf_device_map", None) or {}
        for device in device_map.values():
            if str(device) not in {"cpu", "disk"}:
                return device
        return None

    def _tokenize_for_model(self, tokenizer: Any, model: Any, prompt: str) -> Any:
        inputs = tokenizer(prompt, return_tensors="pt", add_special_tokens=False)
        device = self._model_input_device(model)
        return inputs.to(device) if device is not None else inputs

    # HF generation is not thread-safe on a shared model instance (KV cache / CUDA state),
    # and the ThreadingHTTPServer serves each request on its own thread — serialize inference.
    _generate_lock = Lock()

    def _generate_transformers(
        self,
        prompt: str,
        stream_callback: Any | None = None,
        params: dict[str, Any] | None = None,
    ) -> str:
        # Convert any transformers/torch/load/OOM failure into DaoRuntimeError so every caller
        # (routing, consultation, probe, interview) degrades gracefully to deterministic rules
        # instead of surfacing an opaque HTTP 500 — and the real cause is preserved in the message.
        try:
            transformers, tokenizer, model = self._load_transformers_runtime()
            params = params or self._profile_params("teaching_explanation")
            inputs = self._tokenize_for_model(tokenizer, model, prompt)
            generate_kwargs = dict(
                **inputs,
                max_new_tokens=params["max_new_tokens"],
                do_sample=params["do_sample"],
                temperature=params["temperature"],
                top_p=params["top_p"],
                repetition_penalty=params["repetition_penalty"],
                use_cache=True,
            )
            with self._generate_lock:
                if stream_callback is not None:
                    streamer = transformers.TextIteratorStreamer(tokenizer, skip_prompt=True, skip_special_tokens=True)
                    generate_error: list[Exception] = []

                    def _run_generate() -> None:
                        try:
                            model.generate(**generate_kwargs, streamer=streamer)
                        except Exception as exc:  # noqa: BLE001 — relayed to the caller after join
                            generate_error.append(exc)
                            streamer.end()

                    thread = Thread(target=_run_generate)
                    thread.start()
                    response = ""
                    for token in streamer:
                        stream_callback(token)
                        response += token
                    thread.join()
                    if generate_error:
                        raise generate_error[0]
                    return response

                outputs = model.generate(**generate_kwargs)
            generated = outputs[0][inputs["input_ids"].shape[-1] :]
            return tokenizer.decode(generated, skip_special_tokens=True)
        except DaoRuntimeError:
            raise
        except Exception as exc:  # noqa: BLE001 — surface cause, never crash the request
            raise DaoRuntimeError(f"transformers backend failed: {type(exc).__name__}: {exc}") from exc


def deterministic_emergency_guidance(flags: list[str], safety_level: str = "high") -> str:
    """Rule-templated clinician referral guidance — NO model involved.

    This is the A0/A1 emergency content source (v0.14 safety invariant: an emergency
    response never waits on, and never leaks the narrative to, a language model).
    ``DaoClient._mock_emergency_referral`` delegates here so mock runs and the
    deterministic interview referral render identical content.
    """

    is_emergency = safety_level == "emergency"
    level_label = "**Ⅰ级急诊（立即就诊）**" if is_emergency else "**Ⅱ级急诊（2小时内就诊）**"
    flags_md = "\n".join(f"- {f}" for f in flags) or "- 高风险信号（详见问诊记录）"
    clinical_meaning = (
        "马尾神经位于脊髓圆锥以下，受压后若未及时手术减压，可能导致永久性大小便功能障碍及下肢感觉运动缺失。"
        "神经功能保护的时间窗有限（通常以48小时为关键节点），尽早影像确认和手术决策是预后的关键。"
        if is_emergency else
        "上述危险信号提示可能存在感染性、肿瘤性或骨折性脊柱病变，需尽快影像学确认与专科评估。"
    )
    urgency_reason = (
        "马尾神经受压的可能性不能排除，时间窗决定预后，需立即脊柱外科/神经外科评估。"
        if is_emergency else
        "危险信号提示需要专科排除严重病变，建议尽快就医而非等待普通门诊。"
    )
    return (
        f"## 急诊转诊临床参考（供执业医师审核 · 非患者自用）\n\n"
        f"### 一、危险信号临床意义\n"
        f"{flags_md}\n\n"
        f"{clinical_meaning}\n\n"
        f"### 二、就诊时建议携带的信息\n"
        f"1. 各症状出现/加重的确切时间（尤其大小便障碍、肢体无力的起始时刻）\n"
        f"2. 近期腰椎影像资料（MRI/CT/X线报告及原始图像，如有）\n"
        f"3. 目前用药清单（止痛药/抗凝/激素/降糖等）\n"
        f"4. 既往脊柱手术史、肿瘤病史及药物过敏史\n\n"
        f"### 三、转诊前注意事项\n"
        f"- 避免剧烈弯腰、扭转或搬运重物，防止加重神经损伤\n"
        f"- 建议平卧位等待或由120急救担架搬运，避免长时间坐位移动\n"
        f"- 如症状骤然加重（双下肢完全无力/完全失禁），立即拨打120急救\n"
        f"- 镇痛药物是否使用请由急诊接诊医师判断，就医前请勿擅自用药以免影响评估\n\n"
        f"### 四、紧迫度评级\n"
        f"{level_label}\n"
        f"理由：{urgency_reason}\n\n"
        f"> 本内容为供执业医师参考的急诊转诊辅助信息，最终处置由接诊医师决定，患者不可据此自行用药。"
    )
