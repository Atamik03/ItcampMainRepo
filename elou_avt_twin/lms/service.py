"""
lms/service.py
==============
Business logic of the LMS layer:

    dashboard      -> home screen payload for an operator (Визуал.txt: Главная)
    mastery        -> индекс мастерства + профессиональный статус (лестница)
    courses        -> course tree with per-user module progress
    competencies   -> professional profile (map of competencies)
    history        -> table of all sessions
    debrief        -> разбор выполнения практического задания
    analytics      -> instructor statistics (диаграммы)
    monitoring     -> live operator state
    groups         -> instructor-managed study groups
    profile        -> operator profile
"""

from __future__ import annotations

import time
from datetime import datetime
from typing import Any, Dict, List, Optional

from models.scenario import Scenario
from persistence.session_store import SessionStore
from auth.store import AuthStore

from .models import (
    AnalyticsView,
    CompetencyView,
    CourseView,
    DashboardView,
    DebriefError,
    DebriefStep,
    DebriefView,
    GroupCreate,
    GroupMemberProgress,
    GroupView,
    HistoryRow,
    MasteryView,
    ModuleStatus,
    ModuleView,
    MonitorOperatorView,
    ProfileView,
    Recommendation,
    StudyGroup,
)
from .content_store import LmsContentStore
from .store import LmsStore

STAGES = ["Стажер", "Оператор", "Оператор 2 категории",
          "Оператор 1 категории", "Старший оператор"]

ACTION_LABELS: Dict[str, str] = {
    "TURN_ON": "Пуск",
    "TURN_OFF": "Останов",
    "SET_VALUE": "Изменение значения",
    "SET_SPEED": "Изменение оборотов",
    "EMERGENCY_STOP": "Аварийный останов",
    "SET_SP": "Изменение уставки",
    "SET_MODE": "Смена режима",
    "ACK_ALARM": "Квитирование аварии",
}


def _now() -> float:
    return time.time()


class LmsService:
    """High-level LMS service. Depends only on stores and the scenario catalog."""

    def __init__(self, store: LmsStore, session_store: SessionStore,
                 auth_store: AuthStore, scenarios: Dict[str, Scenario],
                 content_store: Optional[LmsContentStore] = None):
        self.store = store
        self.sessions = session_store
        self.auth = auth_store
        self.scenarios = scenarios
        self.content = content_store

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _user_id(self, username: str) -> Optional[int]:
        u = self.auth.get_user_by_name(username)
        if u is None and username == "system":
            # ELOU_AUTH_MODE=disabled даёт principal="system" без записи в БД —
            # демо-операции ведём от имени аккаунта operator.
            u = self.auth.get_user_by_name("operator")
        return int(u["id"]) if u else None

    def _subject(self, username: str) -> str:
        """Имя пользователя, по которому выполняется выборка данных сессий."""
        if username == "system":
            u = self.auth.get_user_by_name("operator")
            return u["username"] if u else username
        return username

    def _scenario_name(self, scenario_id: str) -> str:
        s = self.scenarios.get(scenario_id)
        if s:
            return s.name
        parts = scenario_id.split("-")
        if len(parts) == 2 and parts[0] == "LMS" and self.content is not None:
            try:
                sc = self.content.get_scenario(int(parts[1]))
                return sc.get("title", scenario_id) if sc else scenario_id
            except (TypeError, ValueError):
                return scenario_id
        return scenario_id

    def _stage(self, mastery: float) -> Dict[str, Any]:
        thresholds = [20.0, 40.0, 60.0, 80.0]
        settings = self.store.get_settings()
        for i, key in enumerate(["mastery_threshold_stage_2", "mastery_threshold_stage_3",
                                 "mastery_threshold_stage_4", "mastery_threshold_stage_5"]):
            try:
                thresholds[i] = float(settings.get(key, thresholds[i]))
            except (TypeError, ValueError):
                pass
        stage_index = 0
        for i, t in enumerate(thresholds):
            if mastery >= t:
                stage_index = i + 1
        next_stage = STAGES[stage_index + 1] if stage_index + 1 < len(STAGES) else None
        to_next = 0.0
        if next_stage:
            lower = thresholds[stage_index - 1] if stage_index > 0 else 0.0
            upper = thresholds[stage_index]
            to_next = round(upper - mastery, 1)
        return {
            "index": round(mastery, 1),
            "stage_index": stage_index,
            "stage": STAGES[stage_index],
            "stages": STAGES,
            "next_stage": next_stage,
            "to_next": to_next,
        }

    def _mastery(self, user_id: int, username: str) -> float:
        username = self._subject(username)
        comp_level = self.store.avg_competency_level(user_id)
        recent = self.sessions.list_sessions(limit=5)
        own = [s for s in recent if s.get("operator_id") == username
               and s.get("performance_score") is not None]
        practice_avg = 0.0
        if own:
            practice_avg = sum(float(s["performance_score"]) for s in own) / len(own)
        if practice_avg > 0:
            return 0.7 * comp_level + 0.3 * practice_avg
        return comp_level

    def _module_percent(self, status: str, score: Optional[float]) -> float:
        if status == ModuleStatus.COMPLETED.value:
            return 100.0
        if status == ModuleStatus.IN_PROGRESS.value:
            return 50.0
        return 0.0

    def _course_view(self, course: Dict[str, Any], user_id: Optional[int]) -> CourseView:
        modules = self.store.get_modules(int(course["id"]))
        task_ids = {t["id"]: t for t in self.store.list_tasks()}
        progress = {}
        if user_id is not None:
            for p in self.store.get_all_progress(user_id):
                progress[int(p["module_id"])] = p
        views: List[ModuleView] = []
        for m in modules:
            mid = int(m["id"])
            p = progress.get(mid)
            status = p["status"] if p else ModuleStatus.NOT_STARTED.value
            score = p.get("score") if p else None
            attempts = int(p["attempts"]) if p else 0
            task = task_ids.get(m.get("practice_task_id"))
            views.append(ModuleView(
                id=mid, kind=m["kind"], title=m["title"], description=m.get("description", ""),
                seq=int(m.get("seq", 0)), content=m.get("content", ""),
                scenario_id=m.get("scenario_id"), practice_task_id=m.get("practice_task_id"),
                status=status, score=score, attempts=attempts,
                percent=self._module_percent(status, score),
                practice_title=task["title"] if task else "",
            ))
        completed = sum(1 for v in views if v.status == ModuleStatus.COMPLETED.value)
        in_progress = sum(1 for v in views if v.status == ModuleStatus.IN_PROGRESS.value)
        total = len(views)
        percent = round((completed + 0.5 * in_progress) / total * 100, 1) if total else 0.0
        return CourseView(
            id=int(course["id"]), title=course["title"], description=course.get("description", ""),
            status=course.get("status", "DRAFT"), progress_percent=percent, modules=views,
        )

    def _competency_views(self, user_id: int) -> List[CompetencyView]:
        levels = {c["code"]: float(c["level_percent"])
                  for c in self.store.get_user_competencies(user_id)}
        out: List[CompetencyView] = []
        for c in self.store.list_competencies():
            out.append(CompetencyView(
                code=c["code"], title=c["title"], description=c.get("description", ""),
                level_percent=levels.get(c["code"], 0.0),
            ))
        return out

    def _history_rows(self, username: str, limit: int = 200) -> List[HistoryRow]:
        username = self._subject(username)
        task_by_scenario: Dict[str, str] = {
            t["scenario_id"]: t["title"] for t in self.store.list_tasks()
        }
        rows: List[HistoryRow] = []
        for s in self.sessions.list_sessions(limit=limit):
            if s.get("operator_id") != username:
                continue
            dur = None
            if s.get("sim_start") is not None and s.get("sim_end") is not None:
                dur = max(0.0, float(s["sim_end"]) - float(s["sim_start"]))
            rows.append(HistoryRow(
                session_id=s["id"], scenario_id=s.get("scenario_id") or "",
                scenario_name=self._scenario_name(s.get("scenario_id") or ""),
                task_title=task_by_scenario.get(s.get("scenario_id") or "", ""),
                operator_id=s.get("operator_id") or "", status=s.get("status") or "",
                performance_score=s.get("performance_score"),
                qualification=s.get("qualification") or "",
                sim_start=float(s.get("sim_start", 0.0) or 0.0), sim_end=s.get("sim_end"),
                wall_start=float(s.get("wall_start", 0.0) or 0.0), wall_end=s.get("wall_end"),
                duration_s=dur,
            ))
        return rows

    def _task_title(self, scenario_id: str) -> str:
        for t in self.store.list_tasks():
            if t["scenario_id"] == scenario_id:
                return t["title"]
        parts = scenario_id.split("-")
        if len(parts) == 2 and parts[0] == "LMS" and self.content is not None:
            try:
                sc = self.content.get_scenario(int(parts[1]))
                if sc is not None:
                    task = self.content.get_task_by_module(int(sc["module_id"]))
                    if task:
                        return task["title"]
            except (TypeError, ValueError):
                pass
        return self._scenario_name(scenario_id)

    # ------------------------------------------------------------------
    # Operator views
    # ------------------------------------------------------------------

    def dashboard(self, username: str) -> DashboardView:
        user_id = self._user_id(username) or 0
        mastery = self._mastery(user_id, username)
        stage = self._stage(mastery)

        courses = self.store.list_courses(status="ACTIVE")
        current_course = self._course_view(courses[0], user_id) if courses else None

        nearest_module: Optional[ModuleView] = None
        nearest_exam: Optional[ModuleView] = None
        if current_course:
            for m in current_course.modules:
                if m.status != ModuleStatus.COMPLETED.value:
                    nearest_module = m
                    break
            for m in current_course.modules:
                if m.kind == "exam" and m.status != ModuleStatus.COMPLETED.value:
                    nearest_exam = m
                    break

        competencies = self._competency_views(user_id)
        recent = self._history_rows(username, limit=5)

        recommendations: List[Recommendation] = []
        min_score = 60.0
        weak = [c for c in competencies if c.level_percent < min_score and c.level_percent > 0]
        weak.sort(key=lambda c: c.level_percent)
        if weak:
            worst = weak[0]
            tasks = [t for t in self.store.list_tasks(enabled_only=True)
                     if worst.code in t["required_competencies"]]
            if tasks:
                t = tasks[0]
                recommendations.append(Recommendation(
                    kind="practice",
                    text=f"Рекомендуется практика «{t['title']}» для развития компетенции «{worst.title}».",
                    task_id=t["id"]))
        if nearest_module:
            recommendations.append(Recommendation(
                kind="module",
                text=f"Продолжите обучение: модуль «{nearest_module.title}» курса «{current_course.title if current_course else ''}».",
                module_id=nearest_module.id))
        if not recommendations:
            recommendations.append(Recommendation(
                kind="info",
                text="Уровень подготовки высокий. Для закрепления навыков выполняйте случайные практические задания."))

        notifications = self.store.list_notifications(user_id, limit=20)

        return DashboardView(
            username=username,
            full_name=self._full_name(username),
            mastery=MasteryView(**stage),
            current_course=current_course,
            nearest_module=nearest_module,
            nearest_exam=nearest_exam,
            recent_practices=recent,
            recommendations=recommendations,
            competencies=competencies,
            notifications=notifications,
        )

    def my_courses(self, username: str) -> List[CourseView]:
        user_id = self._user_id(username) or 0
        courses = self.store.list_courses()
        return [self._course_view(c, user_id) for c in courses]

    def course_detail(self, course_id: int, username: str) -> Optional[CourseView]:
        user_id = self._user_id(username) or 0
        course = self.store.get_course(course_id)
        return self._course_view(course, user_id) if course else None

    def competencies(self, username: str) -> List[CompetencyView]:
        user_id = self._user_id(username) or 0
        return self._competency_views(user_id)

    def history(self, username: str, limit: int = 200) -> List[HistoryRow]:
        return self._history_rows(username, limit=limit)

    def profile(self, username: str) -> ProfileView:
        username = self._subject(username)
        user_id = self._user_id(username) or 0
        u = self.auth.get_user_by_name(username)
        mastery = self._mastery(user_id, username)
        rows = self._history_rows(username, limit=500)
        completed = [r for r in rows if r.performance_score is not None]
        avg = (sum(r.performance_score for r in completed) / len(completed)
               if completed else 0.0)
        roles = self.auth.roles_for_user(username) if u else []
        permissions = self.auth.permissions_for_user(username) if u else []
        return ProfileView(
            username=username,
            full_name=self._full_name(username),
            roles=roles,
            permissions=permissions,
            created_at=float(u.get("created_at", 0.0)) if u else 0.0,
            mastery=MasteryView(**self._stage(mastery)),
            competencies=self._competency_views(user_id),
            total_sessions=len(rows),
            avg_score=round(avg, 1),
        )

    # ------------------------------------------------------------------
    # Debrief (разбор выполнения задания)
    # ------------------------------------------------------------------

    def debrief(self, session_id: str, username: str) -> Optional[DebriefView]:
        username = self._subject(username)
        session = self.sessions.get_session(session_id)
        if session is None:
            return None
        # Доступ к отчёту: владелец сессии, участник практики или
        # инструктор/администратор (view_analytics).
        participants = session.get("participants") or []
        can_view = (
            session.get("operator_id") == username
            or username in participants
            or "view_analytics" in (self.auth.permissions_for_user(username) or [])
        )
        if not can_view:
            return None
        actions = self.sessions.get_actions(session_id)
        errors = self.sessions.get_errors(session_id)
        alarms = self.sessions.get_alarms(session_id)
        sim_start = float(session.get("sim_start", 0.0) or 0.0)

        def relative_time(value: Any) -> float:
            return max(0.0, float(value or 0.0) - sim_start)

        steps: List[DebriefStep] = []
        for i, a in enumerate(actions, start=1):
            value = a.get("new_value")
            desc = ACTION_LABELS.get(a.get("action_type", ""), a.get("action_type", ""))
            detail = f"{a.get('equipment_id', '')}"
            if value is not None:
                detail += f" → {value}"
            if not a.get("accepted", 1):
                detail += " · отклонено"
            steps.append(DebriefStep(
                seq=i, kind="action", timestamp=relative_time(a.get("sim_time")),
                equipment_id=a.get("equipment_id", ""),
                action_type=a.get("action_type", ""),
                description=desc, status="rejected" if not a.get("accepted", 1) else "ok",
                detail=detail,
            ))

        error_views: List[DebriefError] = []
        for e in errors:
            error_views.append(DebriefError(
                rule_error_type=e.get("rule_error_type", e.get("error_type", "")),
                severity=e.get("severity", ""),
                expected_action=e.get("expected_action", ""),
                cause=e.get("cause", ""), consequence=e.get("consequence", ""),
                timestamp=relative_time(e.get("sim_time")),
            ))

        # PRACTICE_FEEDBACK is assessment prose, not an operator error. Old
        # databases may still contain such rows; never expose/count them as
        # ErrorTracker events. If a missed deadline was later completed, keep
        # only the final DELAYED_ACTION explanation.
        tracker_errors = [e for e in error_views if e.rule_error_type != "PRACTICE_FEEDBACK"]
        delayed_expected = [e.expected_action.strip() for e in tracker_errors
                            if e.rule_error_type == "DELAYED_ACTION"]
        tracker_errors = [e for e in tracker_errors if not (
            e.rule_error_type == "MISSED_ACTION"
            and any(e.expected_action.strip().startswith(delayed)
                    for delayed in delayed_expected if delayed)
        )]

        alarm_views: List[Dict[str, Any]] = []
        for alarm in alarms:
            item = dict(alarm)
            for key in ("raised_at", "acked_at", "cleared_at"):
                if item.get(key) is not None:
                    item[key] = relative_time(item[key])
            alarm_views.append(item)

        dur = 0.0
        if session.get("sim_start") is not None and session.get("sim_end") is not None:
            dur = max(0.0, float(session["sim_end"]) - float(session["sim_start"]))

        recommendations: List[str] = []
        score = float(session.get("performance_score") or 0.0)
        if score < 70:
            recommendations.append("Рекомендуется повторить практику и изучить теоретический материал "
                                   "по действиям в аварийных ситуациях.")
        elif score < 90:
            recommendations.append("Хороший результат. Обратите внимание на скорость реакции "
                                   "и полноту выполняемых действий.")
        else:
            recommendations.append("Отличный результат. Можно переходить к случайным заданиям "
                                   "для проверки устойчивости навыков.")

        competency_delta = self._debrief_competency_delta(session_id, username, score)

        # The error block has one authoritative source: ErrorTracker.  Result
        # summaries such as “goal 0%” and “expected actions 33%” belong to
        # criteria, not to individual operator errors, and must not inflate
        # the visible error count.
        remarks: List[str] = []

        cause_review = self.sessions.get_cause_review(session_id)
        if cause_review:
            # Feature vectors are intentionally retained in the database for
            # retraining, but are not sent to the browser.
            cause_review.pop("features_json", None)
            if cause_review.get("instructor_agrees") is not None:
                cause_review["instructor_agrees"] = bool(cause_review["instructor_agrees"])
        return DebriefView(
            session_id=session_id,
            task_title=self._task_title(session.get("scenario_id", "")),
            scenario_id=session.get("scenario_id", ""),
            scenario_name=self._scenario_name(session.get("scenario_id", "")),
            operator_id=session.get("operator_id", ""),
            performance_score=score,
            qualification=session.get("qualification", "") or self._qualification(score),
            duration_s=round(dur, 1),
            sim_start=0.0,
            sim_end=relative_time(session.get("sim_end")),
            steps=steps,
            alarms=alarm_views,
            # ErrorTracker events are hidden from reports; they remain in
            # error_events and in the 34-feature inference vector.
            errors=tracker_errors,
            remarks=remarks,
            recommendations=recommendations,
            competency_delta=competency_delta,
            cause_review=cause_review,
            can_review_as_instructor="view_analytics" in (self.auth.permissions_for_user(username) or []),
        )

    def _debrief_competency_delta(self, session_id: str, username: str, score: float) -> List[Dict[str, Any]]:
        session = self.sessions.get_session(session_id)
        if session is None:
            return []
        scenario_id = session.get("scenario_id", "")
        tasks = [t for t in self.store.list_tasks() if t["scenario_id"] == scenario_id]
        codes = tasks[0]["required_competencies"] if tasks else []
        user_id = self._user_id(username)
        if user_id is None or not codes:
            return []
        out: List[Dict[str, Any]] = []
        for code in codes:
            old = self.store.get_user_competencies(user_id)
            old_level = next((float(c["level_percent"]) for c in old if c["code"] == code), 0.0)
            new_level = self.store.blend_user_competency(user_id, code, score)
            title = self.store.get_competency(code)
            out.append({
                "code": code,
                "title": title["title"] if title else code,
                "old": round(old_level, 1),
                "new": round(new_level, 1),
                "delta": round(new_level - old_level, 1),
            })
        return out

    def _qualification(self, score: float) -> str:
        if score >= 90:
            return "ОТЛИЧНО"
        if score >= 80:
            return "ХОРОШО"
        if score >= 70:
            return "УДОВЛЕТВОРИТЕЛЬНО"
        return "НЕ СДАНО"

    # ------------------------------------------------------------------
    # Practice tasks
    # ------------------------------------------------------------------

    def list_practice_tasks(self, username: str, include_exam: bool = True) -> List[Dict[str, Any]]:
        tasks = [t for t in self.store.list_tasks(enabled_only=True)
                 if include_exam or t["category"] != "exam"]
        user_id = self._user_id(username) or 0
        comp_levels = {c["code"]: float(c["level_percent"])
                       for c in self.store.get_user_competencies(user_id)}
        # Привязка задания к модулю через авторский сценарий (LMS-{id}):
        # библиотечное задание запускается в контексте модуля-владельца сценария.
        scenario_modules: Dict[str, int] = {}
        if self.content is not None:
            for s in self.content.list_scenarios():
                sid, mid = s.get("id"), s.get("module_id")
                if sid is not None and mid is not None:
                    scenario_modules[f"LMS-{sid}"] = int(mid)
        out = []
        for t in tasks:
            req = t.get("required_competencies", [])
            avg_req = (sum(comp_levels.get(c, 0.0) for c in req) / len(req)) if req else 100.0
            item = {
                **t,
                "is_ready": avg_req >= 50.0,
                "readiness_percent": round(avg_req, 1),
                "scenario_name": self._scenario_name(t["scenario_id"]),
            }
            mid = scenario_modules.get(t.get("scenario_id", ""))
            if mid:
                m = self.store.get_module(mid)
                item["module_id"] = mid
                if m:
                    item["module_title"] = m.get("title")
            out.append(item)
        return out

    def practice_task(self, task_id: int) -> Optional[Dict[str, Any]]:
        return self.store.get_task(task_id)

    # ------------------------------------------------------------------
    # Groups (instructor)
    # ------------------------------------------------------------------

    def list_groups(self) -> List[StudyGroup]:
        groups = self.store.list_groups()
        courses = {int(c["id"]): c for c in self.store.list_courses()}
        out: List[StudyGroup] = []
        for g in groups:
            course = courses.get(g.get("course_id"))
            out.append(StudyGroup(
                id=int(g["id"]), name=g["name"], description=g.get("description", ""),
                course_id=g.get("course_id"), course_title=course["title"] if course else "",
                instructor_id=g.get("instructor_id"), created_at=float(g.get("created_at", 0.0)),
                member_count=self.store.group_member_count(int(g["id"])),
            ))
        return out

    def group_view(self, group_id: int) -> Optional[GroupView]:
        g = self.store.get_group(group_id)
        if g is None:
            return None
        members = self.store.member_ids(group_id)
        members_progress: List[GroupMemberProgress] = []
        for uid in members:
            uv = self.auth.user_view(uid)
            username = uv.username
            mastery = self._mastery(uid, username)
            comps = [CompetencyView(code=c["code"], title=c["title"],
                                    description=c.get("description", ""),
                                    level_percent=float(c["level_percent"]))
                     for c in self.store.get_user_competencies(uid)]
            course_progress = 0.0
            if g.get("course_id"):
                course = self.store.get_course(g["course_id"])
                if course:
                    course_progress = self._course_view(course, uid).progress_percent
            last = self._history_rows(username, limit=1)
            members_progress.append(GroupMemberProgress(
                user_id=uid, username=username, full_name=uv.full_name,
                course_progress=course_progress, mastery=round(mastery, 1),
                stage=self._stage(mastery)["stage"], competencies=comps,
                last_session=last[0] if last else None,
            ))
        group = next((sg for sg in self.list_groups() if sg.id == group_id), None)
        course_view = None
        if group and group.course_id:
            course = self.store.get_course(group.course_id)
            if course:
                course_view = self._course_view(course, None)
        return GroupView(group=group or StudyGroup(name=g["name"]),
                         course=course_view, members=members_progress)

    def create_group(self, name: str, description: str, course_id: Optional[int],
                     instructor_id: int) -> int:
        gid = self.store.create_group(
            GroupCreate(
                name=name, description=description, course_id=course_id), instructor_id)
        self.store.add_log(f"Создана группа «{name}»", username=str(instructor_id))
        return gid

    # ------------------------------------------------------------------
    # Analytics (instructor)
    # ------------------------------------------------------------------

    def analytics(self) -> AnalyticsView:
        sessions = self.sessions.list_sessions(limit=500)
        completed = [s for s in sessions if s.get("performance_score") is not None]
        avg_score = (sum(float(s["performance_score"]) for s in completed) / len(completed)
                     if completed else 0.0)

        durations = []
        for s in completed:
            if s.get("wall_start") and s.get("wall_end"):
                durations.append(max(0.0, float(s["wall_end"]) - float(s["wall_start"])))
        avg_dur = sum(durations) / len(durations) if durations else 0.0

        groups = self.list_groups()
        group_rating: List[Dict[str, Any]] = []
        for g in groups:
            member_ids = self.store.member_ids(int(g.id))
            scores = []
            for uid in member_ids:
                uv = self.auth.user_view(uid)
                for s in sessions:
                    if s.get("operator_id") == uv.username and s.get("performance_score") is not None:
                        scores.append(float(s["performance_score"]))
            group_rating.append({
                "group_id": g.id, "group_name": g.name, "member_count": g.member_count,
                "avg_score": round(sum(scores) / len(scores), 1) if scores else 0.0,
                "sessions": len(scores),
            })

        error_counter: Dict[str, int] = {}
        for s in sessions:
            for e in self.sessions.get_errors(s["id"]):
                key = e.get("rule_error_type", e.get("error_type", "UNKNOWN"))
                error_counter[key] = error_counter.get(key, 0) + 1
        frequent_errors = [{"rule_error_type": k, "count": v}
                           for k, v in sorted(error_counter.items(), key=lambda x: -x[1])][:12]

        member_ids = self.store.all_members_users()
        comp_accum: Dict[str, Dict[str, Any]] = {}
        for uid in member_ids:
            for c in self.store.get_user_competencies(uid):
                if c["code"] not in comp_accum:
                    comp_accum[c["code"]] = {"title": c["title"], "sum": 0.0, "n": 0}
                comp_accum[c["code"]]["sum"] += float(c["level_percent"])
                comp_accum[c["code"]]["n"] += 1
        competency_distribution = [
            {"code": k, "title": v["title"],
             "avg_level": round(v["sum"] / v["n"], 1) if v["n"] else 0.0}
            for k, v in comp_accum.items()
        ]

        dynamics: Dict[str, Dict[str, Any]] = {}
        for s in completed:
            day = datetime.fromtimestamp(float(s["wall_start"])).strftime("%Y-%m-%d")
            d = dynamics.setdefault(day, {"sum": 0.0, "n": 0})
            d["sum"] += float(s["performance_score"])
            d["n"] += 1
        learning_dynamics = [
            {"date": k, "avg_score": round(v["sum"] / v["n"], 1), "count": v["n"]}
            for k, v in sorted(dynamics.items())
        ]

        status_counter: Dict[str, int] = {}
        for s in sessions:
            st = s.get("status", "UNKNOWN")
            status_counter[st] = status_counter.get(st, 0) + 1
        status_distribution = [{"status": k, "count": v} for k, v in status_counter.items()]

        return AnalyticsView(
            avg_score=round(avg_score, 1), total_sessions=len(sessions),
            completed_sessions=len(completed), avg_duration_s=round(avg_dur, 1),
            group_rating=group_rating, frequent_errors=frequent_errors,
            competency_distribution=competency_distribution,
            learning_dynamics=learning_dynamics, status_distribution=status_distribution,
        )

    # ------------------------------------------------------------------
    # Monitoring (instructor)
    # ------------------------------------------------------------------

    def monitoring(self) -> List[MonitorOperatorView]:
        sessions = self.sessions.list_sessions(limit=200)
        running = [s for s in sessions if s.get("status") == "RUNNING"]
        out: List[MonitorOperatorView] = []
        for s in running:
            actions = self.sessions.get_actions(s["id"])
            errors = self.sessions.get_errors(s["id"])
            alarms = self.sessions.get_alarms(s["id"])
            last_action = actions[-1] if actions else None
            out.append(MonitorOperatorView(
                username=s.get("operator_id", ""),
                full_name=self._full_name(s.get("operator_id", "")),
                session_id=s["id"], scenario_id=s.get("scenario_id", ""),
                scenario_name=self._scenario_name(s.get("scenario_id", "")),
                task_id=self._task_id_for_scenario(s.get("scenario_id", "")),
                status=s.get("status", ""), sim_time=float(s.get("sim_start", 0.0)),
                performance_score=s.get("performance_score"),
                alarms=[dict(a) for a in alarms[-20:]],
                actions_count=len(actions), errors_count=len(errors),
                last_action=last_action, is_system=s.get("operator_id") == "system",
            ))
        return out

    def _task_id_for_scenario(self, scenario_id: str) -> Optional[int]:
        """id задания (lms_training_tasks / lms_practice_tasks) по scenario_id —
        чтобы инструктор мог перейти в живое задание оператора из мониторинга."""
        if not scenario_id:
            return None
        if scenario_id.startswith("LMS-"):
            if self.content is None:
                return None
            try:
                s = self.content.get_scenario(int(scenario_id.split("-", 1)[1]))
                module_id = int(s["module_id"]) if s and s.get("module_id") else None
                if module_id:
                    task = self.content.get_task_by_module(module_id)
                    return int(task["id"]) if task else None
            except (TypeError, ValueError):
                return None
        else:
            for t in self.store.list_tasks():
                if t.get("scenario_id") == scenario_id:
                    return t.get("id")
        return None

    def _full_name(self, username: str) -> str:
        username = self._subject(username)
        u = self.auth.get_user_by_name(username)
        return u.get("full_name", "") if u else ""

    # ------------------------------------------------------------------
    # Notifications
    # ------------------------------------------------------------------

    def notifications(self, username: str) -> Dict[str, Any]:
        user_id = self._user_id(username) or 0
        return {
            "items": self.store.list_notifications(user_id),
            "unread": self.store.unread_notification_count(user_id),
        }

    def mark_notifications_read(self, username: str) -> None:
        user_id = self._user_id(username) or 0
        self.store.mark_notifications_read(user_id)

    # ------------------------------------------------------------------
    # Settings & logs (admin)
    # ------------------------------------------------------------------

    def settings(self) -> Dict[str, str]:
        return self.store.get_settings()

    def update_settings(self, values: Dict[str, str], username: str) -> None:
        self.store.set_settings(values)
        self.store.add_log("Настройки системы обновлены", username=username, category="settings")

    def logs(self, limit: int = 200) -> List[Dict[str, Any]]:
        return self.store.list_logs(limit=limit)
