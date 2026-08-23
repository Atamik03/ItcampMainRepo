"""
lms/api.py
==========
REST API of the LMS layer — кабинеты оператора / инструктора / администратора
(по «Визуал.txt»). Все маршруты защищены RBAC-правами из `auth/`; операторские
маршруты определяют вызывающего через `get_current_user`.

Дерево маршрутов:

    /lms/dashboard                      оператор   — Главная
    /lms/profile                        оператор   — Профиль
    /lms/courses                        оператор   — Мои курсы
    /lms/courses/{course_id}            оператор   — курс с прогрессом
    /lms/competencies                   оператор   — Мои компетенции
    /lms/history                        оператор   — История
    /lms/sessions/{id}/debrief          оператор   — разбор выполнения
    /lms/practice-tasks                 оператор   — Практика
    /lms/scenarios                      оператор   — каталог сценариев
    /lms/notifications                  оператор   — уведомления
    /lms/modules/{id}/start|theory|complete  оператор
    /lms/groups                         инструктор — группы
    /lms/analytics                      инструктор — аналитика
    /lms/monitoring                     инструктор — живой мониторинг
    /lms/settings                       администратор
    /lms/logs                           администратор
    /lms/courses|modules|practice-tasks администратор — CRUD справочников
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from auth.deps import get_current_user, require_permission
from auth.models import Principal
from auth.store import AuthStore
from persistence.session_store import SessionStore
from scenarios.scenario_registry import SCENARIO_REGISTRY

from .models import (
    AnalyticsView,
    CompetencyView,
    CourseCreate,
    CourseUpdate,
    CourseView,
    DashboardView,
    DebriefView,
    GroupCreate,
    GroupMembersRequest,
    GroupUpdate,
    GroupView,
    HistoryRow,
    ModuleCreate,
    MonitorOperatorView,
    PracticeTask,
    ProfileView,
    SettingsUpdate,
    StudyGroup,
    TaskCreate,
    TaskUpdate,
)
from .content_store import LmsContentStore
from .seeds import seed
from .service import LmsService
from .store import LmsStore

router = APIRouter(prefix="/lms", tags=["lms"])


class OperatorCauseReviewWrite(BaseModel):
    answers: Dict[str, bool]
    selected_cause: str = ""


class InstructorCauseReviewWrite(BaseModel):
    agrees: bool
    causes: List[str] = []

_service: Optional[LmsService] = None


def get_service() -> LmsService:
    """Lazily build the LMS service (shared SQLite `sessions.db`)."""
    global _service
    if _service is None:
        lms_store = LmsStore()
        seed(lms_store)
        auth_store = AuthStore()
        auth_store.ensure_catalog()
        auth_store.ensure_default_users()
        _service = LmsService(
            store=lms_store,
            session_store=SessionStore(),
            auth_store=auth_store,
            scenarios=SCENARIO_REGISTRY,
            content_store=LmsContentStore(),
        )
    return _service


def _user_id(service: LmsService, username: str) -> int:
    uid = service._user_id(username)
    if uid is not None:
        return uid
    if username == "system":
        # ELOU_AUTH_MODE=disabled резолвит principal="system" без записи в БД —
        # операции ведём от имени демо-аккаунтов.
        for name in ("operator", "instructor", "admin"):
            u = service.auth.get_user_by_name(name)
            if u:
                return int(u["id"])
    raise HTTPException(status_code=404, detail=f"Пользователь '{username}' не найден")


def _module_or_404(service: LmsService, module_id: int) -> Dict[str, Any]:
    m = service.store.get_module(module_id)
    if m is None:
        raise HTTPException(status_code=404, detail=f"Модуль '{module_id}' не найден")
    return m


# ---------------------------------------------------------------------------
# Оператор: Главная / Профиль / Курсы / Компетенции / История / Практика
# ---------------------------------------------------------------------------


@router.get("/dashboard", response_model=DashboardView,
            dependencies=[Depends(require_permission("view_dashboard"))])
def dashboard(current_user: Principal = Depends(get_current_user)):
    return get_service().dashboard(current_user.username)


@router.get("/profile", response_model=ProfileView,
            dependencies=[Depends(require_permission("view_profile"))])
def profile(current_user: Principal = Depends(get_current_user)):
    return get_service().profile(current_user.username)


@router.get("/courses", response_model=List[CourseView],
            dependencies=[Depends(require_permission("view_courses"))])
def courses(current_user: Principal = Depends(get_current_user)):
    return get_service().my_courses(current_user.username)


@router.get("/courses/{course_id}", response_model=CourseView,
            dependencies=[Depends(require_permission("view_courses"))])
def course_detail(course_id: int, current_user: Principal = Depends(get_current_user)):
    view = get_service().course_detail(course_id, current_user.username)
    if view is None:
        raise HTTPException(status_code=404, detail=f"Курс '{course_id}' не найден")
    return view


@router.get("/competencies", response_model=List[CompetencyView],
            dependencies=[Depends(require_permission("view_competencies"))])
def competencies(current_user: Principal = Depends(get_current_user)):
    return get_service().competencies(current_user.username)


@router.get("/history", response_model=List[HistoryRow],
            dependencies=[Depends(require_permission("view_history"))])
def history(limit: int = Query(200, ge=1, le=1000),
            current_user: Principal = Depends(get_current_user)):
    return get_service().history(current_user.username, limit=limit)


@router.get("/sessions/{session_id}/debrief", response_model=DebriefView,
            dependencies=[Depends(require_permission("view_history"))])
def session_debrief(session_id: str,
                    current_user: Principal = Depends(get_current_user)):
    view = get_service().debrief(session_id, current_user.username)
    if view is None:
        raise HTTPException(status_code=404, detail=f"Сессия '{session_id}' не найдена")
    return view


@router.post("/sessions/{session_id}/cause-review",
             dependencies=[Depends(require_permission("view_history"))])
def operator_cause_review(session_id: str, req: OperatorCauseReviewWrite,
                          current_user: Principal = Depends(get_current_user)):
    service = get_service()
    session = service.sessions.get_session(session_id)
    review = service.sessions.get_cause_review(session_id)
    if session is None or review is None:
        raise HTTPException(status_code=404, detail="Прогноз для сессии не найден")
    participants = session.get("participants") or []
    if session.get("operator_id") != current_user.username and current_user.username not in participants:
        raise HTTPException(status_code=403, detail="Разметить прогноз может только оператор сессии")
    prediction_ids = {p.get("cause_id") for p in review.get("predictions_json") or []}
    if set(req.answers) != prediction_ids:
        raise HTTPException(status_code=422, detail="Нужен ответ по каждой предложенной причине")
    if req.answers and not any(req.answers.values()) and not req.selected_cause.strip():
        raise HTTPException(status_code=422, detail="Укажите достоверную причину")
    from ml.error_cause_inference import CAUSE_LABELS
    allowed_causes = set(CAUSE_LABELS.values())
    if req.selected_cause.strip() and req.selected_cause.strip() not in allowed_causes:
        raise HTTPException(status_code=422, detail="Неизвестная причина")
    service.sessions.save_operator_cause_review(session_id, req.answers, req.selected_cause.strip())
    return {"ok": True}


@router.post("/sessions/{session_id}/instructor-cause-review",
             dependencies=[Depends(require_permission("view_analytics"))])
def instructor_cause_review(session_id: str, req: InstructorCauseReviewWrite,
                            current_user: Principal = Depends(get_current_user)):
    service = get_service()
    if service.sessions.get_cause_review(session_id) is None:
        raise HTTPException(status_code=404, detail="Прогноз для сессии не найден")
    causes = [cause.strip() for cause in req.causes if cause.strip()]
    from ml.error_cause_inference import CAUSE_LABELS
    if any(cause not in set(CAUSE_LABELS.values()) for cause in causes):
        raise HTTPException(status_code=422, detail="Неизвестная причина")
    if not req.agrees and not causes:
        raise HTTPException(status_code=422, detail="Выберите хотя бы одну достоверную причину")
    service.sessions.save_instructor_cause_review(
        session_id, current_user.username, req.agrees, causes,
    )
    return {"ok": True}


@router.get("/instructor/operators/{username}/session-history", response_model=List[HistoryRow],
            dependencies=[Depends(require_permission("view_analytics"))])
def instructor_operator_session_history(username: str, limit: int = Query(100, ge=1, le=500)):
    """Completed session history used by the instructor review workflow."""
    return get_service()._history_rows(username, limit=limit)


@router.get("/instructor/session-reviews",
            dependencies=[Depends(require_permission("view_analytics"))])
def instructor_session_reviews(limit: int = Query(500, ge=1, le=1000)):
    """All completed operator sessions with ML/human review status."""
    service = get_service()
    rows = []
    for session in service.sessions.list_sessions(limit=limit):
        if session.get("status") != "COMPLETED":
            continue
        review = service.sessions.get_cause_review(session["id"])
        rows.append({
            "session_id": session["id"],
            "operator_id": session.get("operator_id", ""),
            "scenario_id": session.get("scenario_id", ""),
            "scenario_name": service._scenario_name(session.get("scenario_id", "")),
            "wall_end": session.get("wall_end"),
            "performance_score": session.get("performance_score"),
            "model_status": review.get("model_status") if review else "missing",
            "operator_reviewed": bool(review and review.get("operator_reviewed_at")),
            "instructor_reviewed": bool(review and review.get("instructor_reviewed_at")),
            "instructor_id": review.get("instructor_id") if review else None,
        })
    return rows


@router.get("/practice-tasks", response_model=List[PracticeTask],
            dependencies=[Depends(require_permission("view_courses"))])
def practice_tasks(include_exam: bool = True,
                   current_user: Principal = Depends(get_current_user)):
    """Библиотека практических заданий с признаком готовности оператора."""
    return get_service().list_practice_tasks(current_user.username, include_exam=include_exam)


@router.get("/practice-tasks/{task_id}", response_model=PracticeTask,
            dependencies=[Depends(require_permission("view_courses"))])
def practice_task(task_id: int, current_user: Principal = Depends(get_current_user)):
    task = get_service().practice_task(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail=f"Задание '{task_id}' не найдено")
    return task


@router.get("/scenarios", dependencies=[Depends(require_permission("view_courses"))])
def scenarios(current_user: Principal = Depends(get_current_user)):
    """Каталог сценариев тренажёра для запуска практики (авторские сценарии)."""
    svc = get_service()
    return [
        {
            "id": f"LMS-{s['id']}",
            "name": s["title"],
            "description": s.get("description", ""),
            "multi_operator": bool(s.get("multi_operator")),
        }
        for s in svc.content.list_scenarios()
        if s.get("status", "DRAFT") == "PUBLISHED"
    ]


@router.get("/notifications", dependencies=[Depends(require_permission("view_dashboard"))])
def notifications(current_user: Principal = Depends(get_current_user)):
    return get_service().notifications(current_user.username)


@router.post("/notifications/read", dependencies=[Depends(require_permission("view_dashboard"))])
def notifications_read(current_user: Principal = Depends(get_current_user)):
    get_service().mark_notifications_read(current_user.username)
    return {"ok": True}


# ---------------------------------------------------------------------------
# Оператор: прогресс по модулям
# ---------------------------------------------------------------------------


class ModuleCompleteRequest(BaseModel):
    score: float
    session_id: Optional[str] = None


@router.post("/modules/{module_id}/start", dependencies=[Depends(require_permission("view_courses"))])
def module_start(module_id: int, current_user: Principal = Depends(get_current_user)):
    service = get_service()
    _module_or_404(service, module_id)
    service.store.mark_module_started(_user_id(service, current_user.username), module_id)
    return {"ok": True}


@router.post("/modules/{module_id}/theory", dependencies=[Depends(require_permission("view_courses"))])
def module_theory(module_id: int, current_user: Principal = Depends(get_current_user)):
    service = get_service()
    m = _module_or_404(service, module_id)
    if m["kind"] != "theory":
        raise HTTPException(status_code=422, detail="Модуль не является теоретическим")
    service.store.set_theory_completed(_user_id(service, current_user.username), module_id)
    return {"ok": True}


@router.post("/modules/{module_id}/complete", dependencies=[Depends(require_permission("view_courses"))])
def module_complete(module_id: int, req: ModuleCompleteRequest,
                    current_user: Principal = Depends(get_current_user)):
    service = get_service()
    m = _module_or_404(service, module_id)
    user_id = _user_id(service, current_user.username)
    service.store.mark_module_completed(user_id, module_id, req.score, req.session_id)
    service.store.add_notification(
        user_id, f"Модуль «{m['title']}» завершён с оценкой {req.score:.0f}.",
        kind="module")
    return {"ok": True}


# ---------------------------------------------------------------------------
# Инструктор: группы
# ---------------------------------------------------------------------------


@router.get("/groups", response_model=List[StudyGroup],
            dependencies=[Depends(require_permission("view_group_progress"))])
def list_groups(current_user: Principal = Depends(get_current_user)):
    return get_service().list_groups()


@router.get("/groups/{group_id}", response_model=GroupView,
            dependencies=[Depends(require_permission("view_group_progress"))])
def group_detail(group_id: int, current_user: Principal = Depends(get_current_user)):
    view = get_service().group_view(group_id)
    if view is None:
        raise HTTPException(status_code=404, detail=f"Группа '{group_id}' не найдена")
    return view


@router.post("/groups", response_model=StudyGroup,
             dependencies=[Depends(require_permission("manage_groups"))])
def create_group(req: GroupCreate, current_user: Principal = Depends(get_current_user)):
    service = get_service()
    uid = _user_id(service, current_user.username)
    gid = service.create_group(req.name, req.description, req.course_id, uid)
    return service.store.get_group(gid)


@router.put("/groups/{group_id}", response_model=StudyGroup,
            dependencies=[Depends(require_permission("manage_groups"))])
def update_group(group_id: int, req: GroupUpdate,
                 current_user: Principal = Depends(get_current_user)):
    service = get_service()
    if service.store.get_group(group_id) is None:
        raise HTTPException(status_code=404, detail=f"Группа '{group_id}' не найдена")
    service.store.update_group(group_id, req)
    return service.store.get_group(group_id)


@router.delete("/groups/{group_id}", dependencies=[Depends(require_permission("manage_groups"))])
def delete_group(group_id: int, current_user: Principal = Depends(get_current_user)):
    service = get_service()
    if service.store.get_group(group_id) is None:
        raise HTTPException(status_code=404, detail=f"Группа '{group_id}' не найдена")
    service.store.delete_group(group_id)
    return {"ok": True}


@router.put("/groups/{group_id}/members", dependencies=[Depends(require_permission("manage_groups"))])
def set_group_members(group_id: int, req: GroupMembersRequest,
                      current_user: Principal = Depends(get_current_user)):
    service = get_service()
    if service.store.get_group(group_id) is None:
        raise HTTPException(status_code=404, detail=f"Группа '{group_id}' не найдена")
    service.store.set_members(group_id, req.user_ids)
    return {"ok": True}


# ---------------------------------------------------------------------------
# Инструктор: аналитика и мониторинг
# ---------------------------------------------------------------------------


@router.get("/analytics", response_model=AnalyticsView,
            dependencies=[Depends(require_permission("view_analytics"))])
def analytics(current_user: Principal = Depends(get_current_user)):
    return get_service().analytics()


@router.get("/monitoring", response_model=List[MonitorOperatorView],
            dependencies=[Depends(require_permission("monitor_operators"))])
def monitoring(current_user: Principal = Depends(get_current_user)):
    return get_service().monitoring()


# ---------------------------------------------------------------------------
# Администратор: настройки и журнал
# ---------------------------------------------------------------------------


@router.get("/settings", dependencies=[Depends(require_permission("manage_settings"))])
def get_settings(current_user: Principal = Depends(get_current_user)):
    return get_service().settings()


@router.put("/settings", dependencies=[Depends(require_permission("manage_settings"))])
def update_settings(req: SettingsUpdate, current_user: Principal = Depends(get_current_user)):
    get_service().update_settings(req.values, current_user.username)
    return {"ok": True}


@router.get("/logs", dependencies=[Depends(require_permission("view_logs"))])
def system_logs(limit: int = Query(200, ge=1, le=1000),
                current_user: Principal = Depends(get_current_user)):
    return get_service().logs(limit=limit)


# ---------------------------------------------------------------------------
# Администратор: CRUD курсов, модулей, заданий
# ---------------------------------------------------------------------------


@router.post("/courses", dependencies=[Depends(require_permission("manage_courses"))])
def create_course(req: CourseCreate, current_user: Principal = Depends(get_current_user)):
    service = get_service()
    cid = service.store.create_course(req.title, req.description,
                                      status=req.status.value if hasattr(req.status, "value") else req.status)
    service.store.add_log(f"Создан курс «{req.title}»", username=current_user.username, category="course")
    return service.store.get_course(cid)


@router.put("/courses/{course_id}", dependencies=[Depends(require_permission("manage_courses"))])
def update_course(course_id: int, req: CourseUpdate,
                  current_user: Principal = Depends(get_current_user)):
    service = get_service()
    if service.store.get_course(course_id) is None:
        raise HTTPException(status_code=404, detail=f"Курс '{course_id}' не найден")
    fields: Dict[str, Any] = {}
    if req.title is not None:
        fields["title"] = req.title
    if req.description is not None:
        fields["description"] = req.description
    if req.status is not None:
        fields["status"] = req.status.value if hasattr(req.status, "value") else req.status
    service.store.update_course(course_id, fields)
    return service.store.get_course(course_id)


@router.post("/courses/{course_id}/modules", dependencies=[Depends(require_permission("manage_courses"))])
def add_module(course_id: int, req: ModuleCreate,
               current_user: Principal = Depends(get_current_user)):
    service = get_service()
    if service.store.get_course(course_id) is None:
        raise HTTPException(status_code=404, detail=f"Курс '{course_id}' не найден")
    mid = service.store.add_module(course_id, req)
    service.store.add_log(f"Добавлен модуль «{req.title}» в курс #{course_id}",
                          username=current_user.username, category="course")
    return service.store.get_module(mid)


@router.delete("/courses/{course_id}/modules/{module_id}",
               dependencies=[Depends(require_permission("manage_courses"))])
def remove_module(course_id: int, module_id: int,
                  current_user: Principal = Depends(get_current_user)):
    service = get_service()
    m = service.store.get_module(module_id)
    if m is None or int(m["course_id"]) != course_id:
        raise HTTPException(status_code=404, detail=f"Модуль '{module_id}' не найден в курсе '{course_id}'")
    service.store.remove_module(module_id)
    return {"ok": True}


@router.post("/practice-tasks", dependencies=[Depends(require_permission("manage_practice_tasks"))])
def create_task(req: TaskCreate, current_user: Principal = Depends(get_current_user)):
    service = get_service()
    tid = service.store.create_task(req)
    service.store.add_log(f"Создано задание «{req.title}»", username=current_user.username, category="task")
    return service.store.get_task(tid)


@router.put("/practice-tasks/{task_id}", dependencies=[Depends(require_permission("manage_practice_tasks"))])
def update_task(task_id: int, req: TaskUpdate,
                current_user: Principal = Depends(get_current_user)):
    service = get_service()
    if service.store.get_task(task_id) is None:
        raise HTTPException(status_code=404, detail=f"Задание '{task_id}' не найдено")
    service.store.update_task(task_id, req)
    return service.store.get_task(task_id)
