from backend.llm.dao_client import DaoClient, DaoGenerationConfig
from backend.llm.json_repair import loads_with_repair
from backend.llm.output_guard import guard_tao_output
from backend.skills.pipeline import run_case_pipeline
from backend.skills.tao_report_generation_skill import tao_report_generation_skill


class BrokenJsonDaoClient(DaoClient):
    def __init__(self, text: str):
        super().__init__(DaoGenerationConfig(backend="mock"))
        self.text = text

    def generate(self, structured_rule_outputs):
        return self.text


def test_json_repair_handles_fenced_trailing_comma_and_single_quotes():
    parsed, meta = loads_with_repair("""```json
    {'markdown_report': '教学解释', 'final_diagnosis': null,}
    ```""")
    assert parsed["markdown_report"] == "教学解释"
    assert parsed["final_diagnosis"] is None
    assert meta["repaired"] is True


def test_tao_overlay_accepts_repaired_safe_json():
    safe = """以下为结果：
    {'markdown_report': '本案为规则命中教学解释，不构成诊断、处方或治疗建议。',
     'final_diagnosis': null,
     'complete_prescription': null,
     'patient_executable_dose': null,}
    """
    result = run_case_pipeline(
        "患者女，68岁，腰痛反复5年，加重1月，伴下肢麻木，畏寒，舌暗苔白腻，脉细缓，既往骨质疏松。",
        use_llm=True,
        dao_client=BrokenJsonDaoClient(safe),
    )
    assert result["tao_runtime"]["status"] == "accepted"
    assert result["tao_runtime"]["json_repair"]["repaired"] is True
    assert result["tao_runtime"]["fallback_used"] is False
    assert "Tao 教学解释补充" in result["markdown_report"]


def test_tao_overlay_rejects_prescriptive_output_and_falls_back():
    unsafe = '{"markdown_report": "最终诊断为腰椎间盘突出症，处方如下：细辛3g，水煎服。", "final_diagnosis": "腰椎间盘突出症"}'
    result = run_case_pipeline("患者男，50岁，腰痛伴腿麻。", use_llm=True, dao_client=BrokenJsonDaoClient(unsafe))
    assert result["tao_runtime"]["status"] == "guard_rejected"
    assert result["tao_runtime"]["fallback_used"] is True
    assert "最终诊断为腰椎间盘突出症" not in result["markdown_report"]


def test_tao_disabled_runtime_falls_back_to_deterministic_report():
    result = run_case_pipeline("患者女，68岁，腰痛反复5年。", use_llm=True, dao_client=DaoClient(DaoGenerationConfig(backend="disabled")))
    assert result["tao_runtime"]["status"] == "fallback"
    assert result["tao_runtime"]["fallback_used"] is True
    assert "沈钦荣腰痹经验规则分析报告" in result["markdown_report"]


def test_guard_detects_diagnosis_prescription_and_dose_patterns():
    guard = guard_tao_output("最终诊断为腰痹，处方如下：细辛3g，每日2次。")
    categories = {item["category"] for item in guard["violations"]}
    assert {"final_diagnosis", "patient_executable_prescription", "dose_instruction"}.issubset(categories)


def test_dao_mock_question_plan_preserves_candidate_ids():
    client = DaoClient(DaoGenerationConfig(backend="mock"))
    raw = client.generate_question_plan({
        "candidate_questions": [
            {"id": "Q1", "question": "问题1", "reason": "规则1"},
            {"id": "Q2", "question": "问题2", "reason": "规则2"},
        ]
    })
    parsed, _ = loads_with_repair(raw)
    assert [q["id"] for q in parsed["questions"]] == ["Q1", "Q2"]


def test_dao_direct_chat_mock_and_transformers_source_contract():
    client = DaoClient(DaoGenerationConfig(backend="mock"))
    assert "Tao mock direct reply" in client.chat([], "请解释规则线索")
    source = __import__("pathlib").Path("backend/llm/dao_client.py").read_text(encoding="utf-8")
    assert "TextIteratorStreamer" in source
    assert "AutoTokenizer.from_pretrained" in source
    assert "AutoModelForCausalLM.from_pretrained" in source
    assert "trust_remote_code=True" in source
    assert '"attn_implementation": self.config.attn_implementation' in source


def test_dao_from_env_reads_direct_transformers_knobs(monkeypatch):
    monkeypatch.setenv("TAO_BACKEND", "transformers")
    monkeypatch.setenv("TAO_TORCH_DTYPE", "bfloat16")
    monkeypatch.setenv("TAO_DEVICE_MAP", "cuda:0")
    monkeypatch.setenv("TAO_ATTN_IMPLEMENTATION", "eager")
    config = DaoGenerationConfig.from_env()
    assert config.backend == "transformers"
    assert config.torch_dtype == "bfloat16"
    assert config.device_map == "cuda:0"
    assert config.attn_implementation == "eager"


def test_dao_from_env_reads_quantization_knobs(monkeypatch):
    monkeypatch.setenv("TAO_LOAD_IN_4BIT", "true")
    config = DaoGenerationConfig.from_env()
    assert config.load_in_4bit is True
    assert config.load_in_8bit is False
    # Quantized loading (so the 30B MoE fits one GPU) is wired through BitsAndBytesConfig.
    source = __import__("pathlib").Path("backend/llm/dao_client.py").read_text(encoding="utf-8")
    assert "BitsAndBytesConfig" in source
    assert "quantization_config" in source


def test_dao_transformers_cache_is_configuration_aware():
    source = __import__("pathlib").Path("backend/llm/dao_client.py").read_text(encoding="utf-8")
    assert "_model_signature" in source
    assert "self.__class__._model_signature != signature" in source
    assert "self.__class__._model_signature = signature" in source


def test_dao_load_status_reports_backend_lifecycle():
    # mock/http need no local weights → ready; disabled → disabled (so /api/health can report it).
    assert DaoClient(DaoGenerationConfig(backend="disabled")).load_status()["state"] == "disabled"
    assert DaoClient(DaoGenerationConfig(backend="mock")).load_status()["state"] == "ready"
    assert DaoClient(DaoGenerationConfig(backend="http")).load_status()["state"] == "ready"


def test_dao_preload_mock_is_ready_and_disabled_is_reported():
    assert DaoClient(DaoGenerationConfig(backend="mock")).preload()["ok"] is True
    disabled = DaoClient(DaoGenerationConfig(backend="disabled")).preload()
    assert disabled["ok"] is False
    assert disabled["state"] == "disabled"


def test_dao_preload_surfaces_transformers_load_failure_without_crashing(monkeypatch):
    # A real load failure (e.g. an OOM-killed 30B FP16 load) must surface as a reported error,
    # not an opaque crash — this is what lets the server stay up and report the real cause
    # instead of leaving the warmup with "Connection refused". We monkeypatch the heavy loader
    # so the test never downloads weights regardless of whether transformers is installed.
    DaoClient._load_state = "idle"
    DaoClient._load_error = None

    def boom(self):
        raise RuntimeError("CUDA out of memory")

    monkeypatch.setattr(DaoClient, "_load_transformers_runtime", boom, raising=True)
    client = DaoClient(DaoGenerationConfig(backend="transformers"))
    status = client.preload()
    assert status["ok"] is False
    assert status["state"] == "error"
    assert "CUDA out of memory" in status["reason"]
    after = client.load_status()
    assert after["state"] == "error"
    assert "CUDA out of memory" in (after["error"] or "")
    # Reset shared class lifecycle so later tests reading transformers state aren't contaminated.
    DaoClient._load_state = "idle"
    DaoClient._load_error = None
