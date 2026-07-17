from __future__ import annotations

import argparse
import json

from backend.llm.dao_client import DaoClient, DaoRuntimeError
from backend.skills.pipeline import run_case_pipeline


def main() -> None:
    parser = argparse.ArgumentParser(description="Run YaoBi-Skill rule pipeline or direct Tao chat")
    parser.add_argument("--text", help="De-identified lumbar Bi case text")
    parser.add_argument("--json", action="store_true", help="Print full JSON instead of Markdown report")
    parser.add_argument("--use-llm", action="store_true", help="Enable optional Tao runtime overlay; falls back to deterministic report if unavailable or unsafe")
    parser.add_argument("--tao-chat", help="Direct Tao/Dao1 local chat input; set TAO_BACKEND=transformers for direct model inference")
    parser.add_argument("--stream", action="store_true", help="Stream direct Tao chat tokens to stdout")
    parser.add_argument("--ask", help="Conversational mode: route a free-text question to a skill and answer from rules/mined data")
    parser.add_argument("--autonomous", action="store_true", help="With --ask: use the autonomous multi-step agent (plan + subagent delegation)")
    args = parser.parse_args()
    if args.ask:
        if args.autonomous:
            from backend.agents.autonomous_agent import AutonomousQAAgent

            turn = AutonomousQAAgent(use_llm=args.use_llm).run(args.ask)
            plan = " → ".join(p["label"] for p in turn["plan"]) or "(blocked)"
            print(f"[autonomous plan via={turn.get('plan_method')}: {plan}]\n{turn['answer']}")
            return
        from backend.agents.conversation import ConversationSession

        turn = ConversationSession(use_llm=args.use_llm).ask(args.ask)
        print(f"[intent={turn['intent']} via={turn['method']} skills={','.join(turn['skills'])}]\n{turn['answer']}")
        return
    if args.tao_chat:
        client = DaoClient()
        try:
            if args.stream:
                reply = client.chat([], args.tao_chat, stream_callback=lambda token: print(token, end="", flush=True))
                if not reply.endswith("\n"):
                    print()
            else:
                print(client.chat([], args.tao_chat))
        except DaoRuntimeError as exc:
            parser.error(str(exc))
        return
    if not args.text:
        parser.error("--text is required unless --tao-chat is provided")
    result = run_case_pipeline(args.text, use_llm=args.use_llm)
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(result["markdown_report"])


if __name__ == "__main__":
    main()
