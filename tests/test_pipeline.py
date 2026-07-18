from backend.skills.pipeline import run_case_pipeline


def test_pipeline_matches_expected_rule_signals():
    result = run_case_pipeline("患者女，68岁，腰痛反复5年，加重1月，伴下肢麻木，畏寒，舌暗苔白腻，脉细缓，既往骨质疏松。")

    tags = set(result["normalized_tags"])
    assert "elderly" in tags
    assert "chronic_yabi" in tags
    assert "lower_limb_numbness" in tags
    assert "osteoporosis" in tags
    assert result["syndrome_candidates"][0]["name"] == "肝肾不足证"
    assert result["primary_route"]["name"] in {"独活寄生汤加减", "补肾类方", "当归四逆汤加减"}
    assert "不构成诊断、处方或治疗建议" in result["markdown_report"]


def test_safety_flags_raw_red_flag():
    result = run_case_pipeline("患者男，70岁，跌倒后腰痛，出现会阴麻木和尿不出来，想自己买药开方。")

    assert result["safety"]["safety_status"] == "urgent"
    messages = "\n".join(flag["message"] for flag in result["safety"]["red_flags"])
    assert "原文红旗线索" in messages
    # v0.11: 自行购药/开方是策略旗（policy_flags），不与临床红旗混同（P1-3）。
    policy = "\n".join(flag["message"] for flag in result["safety"]["policy_flags"])
    assert "自行" in policy


def test_direct_tao_cli_disabled_returns_friendly_error():
    import os
    import subprocess
    import sys

    # Subprocess must see TAO_BACKEND=disabled regardless of the host environment
    # (a Colab notebook may have set TAO_BACKEND=transformers so the UI works) —
    # otherwise the CLI would try to load the real model instead of hitting the
    # "disabled" branch this test guards.
    env = {k: v for k, v in os.environ.items() if not k.startswith("TAO_")}
    env["TAO_BACKEND"] = "disabled"

    result = subprocess.run(
        [sys.executable, "-m", "backend.main", "--tao-chat", "测试"],
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )
    assert result.returncode == 2
    assert "Tao direct chat is disabled" in result.stderr
    assert "Traceback" not in result.stderr
