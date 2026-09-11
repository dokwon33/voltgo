"""
출력 조립기 (설계서 2.4)

모델이 준 ModelDecision(후보 ID + 설명) 과 Session 의 검증된 값을 합쳐 VoltGoResponse 를 만든다.
시각·상호·소요시간은 전부 Session 에서 가져온다. 모델 문장에 숫자가 있어도 근거로 쓰지 않는다.
"""
from langchain.messages import AIMessage

from voltgo.agent.schemas import ModelDecision, VoltGoResponse
from voltgo.agent.state import Context


def _base(context: Context) -> dict:
    s = context.session
    return dict(
        generated_at=context.clock(),
        charging_source=s.charging.source if s.charging else "unknown",
        location_source=s.origin.source if s.origin else "unknown",
        estimate_basis=s.time_budget.estimate_basis if s.time_budget else "unknown",
        finish_at=s.time_budget.finish_at if s.time_budget else None,
        return_deadline=s.time_budget.return_deadline if s.time_budget else None,
        buffer_min=context.buffer_min,
    )


def collect_warnings(context: Context) -> list[str]:
    """출처 경고는 Session 값에서 다시 만든다 (재개 뒤에도 빠지지 않게)"""
    s = context.session
    w = []
    if s.charging and s.charging.source == "mock":
        w.append("충전 정보는 Mock 데이터입니다")
    if s.time_budget and s.time_budget.estimate_basis == "energy_power":
        w.append("잔여시간은 배터리 용량·평균 전력 정책값으로 추정한 값입니다")
    if s.charging and s.charging.reported_target_soc_pct is not None and s.target_soc_pct != s.charging.reported_target_soc_pct:
        api_t, user_t = s.charging.reported_target_soc_pct, s.target_soc_pct
        if user_t < api_t:
            w.append(f"차량은 {api_t:.0f}%까지 자동 충전되도록 설정돼있어요. {user_t:.0f}% 충전 도달 시각은 추정치입니다.")
        else:
            w.append(f"차량은 {api_t:.0f}%에서 자동으로 충전이 멈추도록 설정돼있어요. {user_t:.0f}%까지 채우려면 차량 앱에서 직접 목표를 올려주세요.")
    if s.places and all(p.poi_source == "mock" for p in s.places.values()):
        w.append("장소 정보는 Mock 데이터입니다")
    if s.routes and all(r.route_source == "mock" for r in s.routes.values()):
        w.append("보행 경로는 Mock 데이터입니다")
    if s.candidates and any(c.opening_status == "unknown" for c in s.candidates.values()):
        w.append("영업 여부는 확인되지 않았습니다")
    for extra in s.warnings:
        if extra not in w and "제외된 후보" in extra:
            w.append(extra)
    return w[:5]


def _missing_fields(context: Context) -> list[str]:
    s = context.session
    missing = []
    if s.charging is None:
        missing.append("charging")
    elif s.time_budget is None:
        missing.append("remaining_min")
    if s.origin is None:
        missing.append("origin")
    return missing or ["intent"]


def _last_ai_text(result: dict) -> str:
    for m in reversed(result.get("messages", [])):
        if isinstance(m, AIMessage) and not m.tool_calls:
            return m.content if isinstance(m.content, str) else ""
    return ""


def render_message(context: Context, candidates, explanation: str, status: str) -> str:
    s = context.session
    lines = []
    if s.time_budget:
        b = s.time_budget
        lines.append(f"충전 완료 예정 {b.finish_at:%H:%M}, 복귀 마감 {b.return_deadline:%H:%M} "
                     f"(버퍼 {b.buffer_min}분, 가용 {b.available_sec // 60}분)")
    for i, c in enumerate(candidates, 1):
        lines.append(f"{i}) {c.name} — 도보 편도 {c.outbound_sec // 60}분, 체류 {c.dwell_sec // 60}분 "
                     f"→ {c.return_at:%H:%M} 복귀 (여유 {c.slack_sec // 60}분), 늦어도 {c.leave_by:%H:%M} 출발")
    if status == "confirmed" and s.confirmed:
        lines.append(f"확정했습니다. {s.confirmed.leave_by:%H:%M} 까지는 장소에서 출발하세요.")
    if explanation:
        lines.append(explanation.strip())
    warnings = collect_warnings(context)
    if warnings:
        lines.append("※ " + " / ".join(warnings))
    text = "\n".join(lines).strip() or "응답을 만들지 못했습니다."
    return text[:1000]


def error_response(context: Context, code: str, message: str) -> VoltGoResponse:
    return VoltGoResponse(status="error", message=f"[{code}] {message}", warnings=collect_warnings(context),
                          **_base(context))


def assemble(result: dict, context: Context) -> VoltGoResponse:
    s = context.session
    base = _base(context)

    # 1. 승인 대기 (HITL interrupt) - 실행 래퍼가 awaiting_approval 을 만든다
    if result.get("__interrupt__"):
        req = result["__interrupt__"][0].value
        action = (req.get("action_requests") or [{}])[0]
        name = action.get("name", "")
        args = action.get("args", {})
        if name == "confirm_plan":
            plan = s.candidates.get(args.get("plan_id"))
            what = f"'{plan.name}' 계획을 확정할까요?" if plan else "계획을 확정할까요?"
            cands = [plan] if plan else []
        else:
            what = f"선호를 저장할까요? ({args})"
            cands = []
        msg = render_message(context, cands, what + " (approve / reject)", "awaiting_approval")
        return VoltGoResponse(status="awaiting_approval", message=msg, candidates=cands,
                              warnings=collect_warnings(context), **base)

    # 2. 구조화 출력이 없으면 (가드레일 종료, 한도 초과 등) 마지막 AI 문장으로 응답
    decision = result.get("structured_response")
    if not isinstance(decision, ModelDecision):
        text = _last_ai_text(result) or "응답을 만들지 못했습니다."
        return VoltGoResponse(status="need_input", message=text[:1000], missing_fields=_missing_fields(context),
                              warnings=collect_warnings(context), **base)

    # 3. 후보 ID 는 통과 후보 안에 있는 것만 (사실 검증)
    ids = []
    for cid in decision.candidate_ids:
        if cid in s.candidates and cid not in ids:
            ids.append(cid)
    candidates = [s.candidates[i] for i in ids][:3]
    if not candidates and decision.next_action == "choose":
        candidates = list(s.candidates.values())[:3]

    # 4. 상태 결정
    selected = None
    missing = []
    if s.confirmed and decision.next_action in ("done", "choose"):
        status = "confirmed"
        selected = s.confirmed.plan_id
        candidates = [s.candidates[selected]] if selected in s.candidates else []
    elif decision.next_action == "clarify":
        status = "need_input"
        candidates = []
        missing = _missing_fields(context)
    elif candidates:
        status = "ok"
    elif s.selected_ran or (s.charging and s.charging.charging is False):
        status = "no_feasible"
    elif decision.next_action == "stop":
        status = "ok"          # 범위 밖 요청 등: 후보 없이 안내만
    else:
        status = "need_input"
        missing = _missing_fields(context)

    return VoltGoResponse(
        status=status,
        message=render_message(context, candidates, decision.explanation, status),
        candidates=candidates,
        selected_plan_id=selected,
        warnings=collect_warnings(context),
        missing_fields=missing,
        **base,
    )
