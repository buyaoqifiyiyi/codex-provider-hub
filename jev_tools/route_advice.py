"""Optional bounded Jev routing advice; actual model selection stays with the host."""
from __future__ import annotations

import json
import math


def advise_route(task: str, facts: list[str], purpose: str, ask) -> dict:
    if not isinstance(task, str) or not task.strip() or len(task.encode("utf-8")) > 3000:
        raise ValueError("任务描述须为不超过 3000 字节的非空文字。")
    if purpose not in {"main", "subagent"}:
        raise ValueError("purpose 须为 main 或 subagent。")
    if not isinstance(facts, list) or len(facts) > 8 or any(
        not isinstance(item, str) or not item.strip() or len(item.encode("utf-8")) > 500 for item in facts
    ):
        raise ValueError("facts 须为最多 8 条简短的已知事实。")
    state = json.dumps({"task": task.strip(), "facts": [x.strip() for x in facts], "purpose": purpose}, ensure_ascii=False)
    questions = {
        "complex": {"type": "noul", "instructions": "Do the supplied facts explicitly show competing root causes, cross-module causal interactions, architecture tradeoffs, concurrency, data integrity, difficult compatibility, or serious unresolved logical issues? Do not infer missing facts."},
        "bounded": {"type": "noul", "instructions": "Do the supplied facts show an independent, well-scoped task whose result can be checked with concrete evidence? Do not assume independence or verification when not stated."},
        "external_block": {"type": "noul", "instructions": "Do the supplied facts explicitly show a missing credential, permission, required file, network, service, dependency, or unsupported tool that blocks the task?"},
    }
    try:
        result = ask(state, questions)
    except Exception as exc:
        return {"status": "unavailable", "recommendation": "use_existing_rules", "reason": type(exc).__name__}
    answers = result.get("answers", {}) if isinstance(result, dict) else {}
    if not isinstance(answers, dict):
        answers = {}
    scores = {}
    for key in questions:
        answer = answers.get(key)
        value = answer.get("noul") if isinstance(answer, dict) else None
        scores[key] = value if type(value) in (int, float) and math.isfinite(value) and 0 <= value <= 1 else None
    if any(value is None for value in scores.values()):
        recommendation = "use_existing_rules"
        status = "partial"
    elif scores["external_block"] >= 0.75:
        recommendation = "check_external_block"
        status = "advisory"
    elif scores["complex"] >= 0.6:
        recommendation = "consider_critical"
        status = "advisory"
    elif purpose == "subagent" and scores["bounded"] >= 0.75 and scores["complex"] < 0.4:
        recommendation = "consider_bounded"
        status = "advisory"
    else:
        recommendation = "use_existing_rules"
        status = "advisory"
    return {"status": status, "recommendation": recommendation, "scores": scores,
            "note": "阈值为初步试验值，仅提供待核对的建议；用户指定、实际证据、模型目录和现有守卫优先，工具不会切换或创建代理。"}
