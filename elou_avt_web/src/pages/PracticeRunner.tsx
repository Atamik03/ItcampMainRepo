import { useCallback, useEffect, useRef, useState } from 'react';
import { useNavigate, useParams } from 'react-router-dom';
import { api } from '../api';
import { useAuth } from '../auth';
import type { LmsPracticeTask, TaskCondition, PracticeStatus } from '../types';
import ScadaScheme from '../scada/ScadaScheme';
import { useSimulation } from '../lms/sim';
import { Err, Loader, fmtClock } from '../lms/ui';
import { attrLabel, relationLabel } from '../lms/scenarioEditor';

const SEVERITY_LABELS: Record<string, string> = {
  CRITICAL: 'Критическая',
  HIGH: 'Высокая',
  WARNING: 'Предупреждение',
  LOW: 'Низкая',
};

const ROLE_LABELS: Record<string, string> = {
  operator: 'Консольный оператор',
  field_operator: 'Полевой оператор',
};

function PracticeGate({
  phase,
  status,
  confirming,
  declining,
  canParticipate,
  onConfirm,
  onDecline,
}: {
  phase: 'waiting' | 'confirm';
  status: PracticeStatus | null;
  confirming: boolean;
  declining: boolean;
  canParticipate: boolean;
  onConfirm: () => void;
  onDecline: () => void;
}) {
  const confirmed = new Set(status?.confirmed_roles ?? []);
  return (
    <div className="practice-gate">
      <div className="practice-gate-card">
        <div className="practice-gate-title">
          {status?.task_title ?? status?.scenario_name ?? 'Практическое задание'}
        </div>
        {phase === 'waiting' ? (
          <>
            <div className="practice-gate-text">Инструктор ещё не запустил практику.</div>
            <div className="practice-gate-sub">Это окно автоматически обновится, когда практика будет готова.</div>
          </>
        ) : (
          <>
            <div className="practice-gate-text">
              Практика запущена инструктором. Подтвердите готовность — практика стартует, когда
              готовность подтвердит консольный оператор.
            </div>
            <div className="practice-gate-roles">
              {(status?.required_roles ?? []).map((role) => (
                <div className={`practice-gate-role ${confirmed.has(role) ? 'ok' : ''}`} key={role}>
                  <span className={`dot ${confirmed.has(role) ? 'dot-ok' : 'dot-wait'}`} />
                  {ROLE_LABELS[role] ?? role}
                  <span className="muted">{confirmed.has(role) ? ' · подтвердил' : ' · ожидание'}</span>
                </div>
              ))}
            </div>
            {status?.timeout_seconds != null && status.timeout_seconds > 0 && (
              <div className="practice-gate-sub">Автостарт через {status.timeout_seconds} с, если второй оператор не подтвердит.</div>
            )}
            {canParticipate ? (
              <>
                <button className="btn btn-start" disabled={confirming} onClick={onConfirm}>
                  {confirming ? 'Отправляем…' : 'Я готов, подтвердить'}
                </button>
                <button className="btn btn-danger" disabled={confirming || declining} onClick={onDecline}>
                  Отклонить
                </button>
              </>
            ) : (
              <div className="practice-gate-sub">Ваша роль не участвует в практике — подтверждение доступно консольному и полевому операторам.</div>
            )}
          </>
        )}
      </div>
    </div>
  );
}

function condLabel(c: TaskCondition): string {
  const rel = c.relation ?? '==';
  const v = typeof c.value === 'number' ? String(c.value) : String(c.value ?? '');
  if (rel === 'between') return `${c.object_id}.${attrLabel(c.attribute)} ∈ [${v}; ${String(c.value2 ?? '')}]`;
  return `${c.object_id}.${attrLabel(c.attribute)} ${relationLabel(rel)} ${v}`;
}

function InstructorWait({ status }: { status: PracticeStatus | null }) {
  const confirmed = new Set(status?.confirmed_roles ?? []);
  return (
    <div className="practice-gate">
      <div className="practice-gate-card">
        <div className="practice-gate-title">
          {status?.task_title ?? status?.scenario_name ?? 'Практическое задание'}
        </div>
        {!status ? (
          <>
            <div className="practice-gate-text">Практика ещё не запущена операторами.</div>
            <div className="practice-gate-sub">Режим инструктора: мнемосхема появится, когда практика начнётся.</div>
          </>
        ) : (
          <>
            <div className="practice-gate-text">Практика ожидает подтверждения готовности операторов.</div>
            <div className="practice-gate-roles">
              {(status?.required_roles ?? []).map((role) => (
                <div className={`practice-gate-role ${confirmed.has(role) ? 'ok' : ''}`} key={role}>
                  <span className={`dot ${confirmed.has(role) ? 'dot-ok' : 'dot-wait'}`} />
                  {ROLE_LABELS[role] ?? role}
                  <span className="muted">{confirmed.has(role) ? ' · подтвердил' : ' · ожидание'}</span>
                </div>
              ))}
            </div>
            {status?.invited_users != null && status.invited_users.length > 0 && (
              <div className="practice-gate-sub">
                Приглашённые: {status.invited_users.map((i) => i.full_name || i.username).join(', ')}
              </div>
            )}
            {status?.timeout_seconds != null && status.timeout_seconds > 0 && (
              <div className="practice-gate-sub">Автостарт через {status.timeout_seconds} с, если второй оператор не подтвердит.</div>
            )}
          </>
        )}
      </div>
    </div>
  );
}

export default function PracticeRunner() {
  const { taskId } = useParams<{ taskId: string }>();
  const { user } = useAuth();
  const navigate = useNavigate();

  const sim = useSimulation();
  const canParticipate = (user?.roles ?? []).some(
    (r) => r === 'operator' || r === 'field_operator',
  );
  const isInstructor =
    new URLSearchParams(window.location.search).get('instructor') === '1' ||
    (user?.roles ?? []).some((r) => r === 'instructor');
  const [task, setTask] = useState<LmsPracticeTask | null>(null);
  const [error, setError] = useState('');
  const [sessionId, setSessionId] = useState('');
  const [sessionSimStart, setSessionSimStart] = useState(0);
  const [ready, setReady] = useState(false);
  const [busy, setBusy] = useState(false);
  const [alarmsOpen, setAlarmsOpen] = useState(false);
  const [phase, setPhase] = useState<'waiting' | 'confirm' | 'running'>('waiting');
  const [confirmInfo, setConfirmInfo] = useState<PracticeStatus | null>(null);
  const [confirming, setConfirming] = useState(false);
  const [declining, setDeclining] = useState(false);
  const started = useRef(false);
  const runningRef = useRef(false);
  const knownAlarmIds = useRef(new Set<string>());
  const simRef = useRef(sim);
  simRef.current = sim;

  const activeAlarms = sim.live?.alarms ?? [];
  const alarmHistory = sim.live?.alarm_history ?? activeAlarms;
  const activeAlarmIds = new Set(activeAlarms.map((alarm) => alarm.id));

  const run = useCallback(async () => {
    if (!taskId) return;
    setError('');
    setSessionId('');
    setSessionSimStart(0);
    setReady(false);
    setAlarmsOpen(false);
    knownAlarmIds.current.clear();
    runningRef.current = false;
    setPhase('waiting');
    setConfirmInfo(null);
    sim.reset();
    try {
      const t = await api.lmsPracticeCatalogTask(Number(taskId));
      setTask(t);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  }, [taskId, sim]);

  useEffect(() => {
    if (started.current) return;
    started.current = true;
    void run();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // Опрос запущенной инструктором практики: ожидание -> подтверждение -> запуск.
  useEffect(() => {
    let alive = true;
    const tick = async () => {
      try {
        const cur = await api.lmsPracticeCurrent();
        if (!alive) return;
        if (!cur) {
          runningRef.current = false;
          setPhase('waiting');
          setSessionId('');
          setReady(false);
          setConfirmInfo(null);
          return;
        }
        setSessionId(cur.session_id);
        setConfirmInfo(cur);
        if (cur.status === 'RUNNING') {
          setSessionSimStart(cur.sim_time ?? 0);
          setReady(true);
          if (!runningRef.current) {
            runningRef.current = true;
            setPhase('running');
            await simRef.current.refresh();
          }
        } else {
          runningRef.current = false;
          setReady(false);
          setPhase('confirm');
        }
      } catch {
        /* сервер недоступен — ждём следующего опроса */
      }
    };
    tick();
    const t = window.setInterval(tick, 3000);
    return () => {
      alive = false;
      window.clearInterval(t);
    };
     
  }, []);

  const confirmPractice = useCallback(async () => {
    if (!sessionId || confirming) return;
    setConfirming(true);
    try {
      const s = await api.lmsPracticeConfirm(sessionId);
      setConfirmInfo(s);
      if (s.status === 'RUNNING') {
        runningRef.current = true;
        setPhase('running');
        setSessionSimStart(s.sim_time ?? 0);
        setReady(true);
        await simRef.current.refresh();
      }
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setConfirming(false);
    }
  }, [sessionId, confirming]);

  const declinePractice = useCallback(async () => {
    if (!sessionId || confirming || declining) return;
    setDeclining(true);
    try {
      await api.lmsPracticeDecline(sessionId);
      runningRef.current = false;
      setSessionId('');
      setConfirmInfo(null);
      setPhase('waiting');
      setReady(false);
      navigate('/practice', { replace: true });
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setDeclining(false);
    }
  }, [sessionId, confirming, declining, navigate]);

  useEffect(() => {
    if (!ready || alarmHistory.length === 0) return;
    const hasNewAlarm = alarmHistory.some((alarm) => !knownAlarmIds.current.has(alarm.id));
    alarmHistory.forEach((alarm) => knownAlarmIds.current.add(alarm.id));
    if (hasNewAlarm) setAlarmsOpen(true);
  }, [alarmHistory, ready]);

  useEffect(() => {
    if (!alarmsOpen) return;
    const closeOnEscape = (event: KeyboardEvent) => {
      if (event.key === 'Escape') setAlarmsOpen(false);
    };
    window.addEventListener('keydown', closeOnEscape);
    return () => window.removeEventListener('keydown', closeOnEscape);
  }, [alarmsOpen]);

  const finish = async () => {
    if (!sessionId || !ready) return;
    setBusy(true);
    try {
      const a = await api.lmsPracticeFinish(sessionId);
      navigate(`/debrief/${a.session_id ?? sessionId}`, { replace: true });
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
      setBusy(false);
    }
  };

  const abort = async () => {
    navigate('/practice', { replace: true });
  };

  return (
    <div className="practice-frame">
      <div className="practice-bar">
        <span className="practice-bar-title">{task?.title ?? 'Практическое задание'}</span>
        {task?.scenario_name && <span className="muted">{task.scenario_name}</span>}
        {sessionId && <span className="muted mono">сессия {sessionId.slice(0, 12)}</span>}
        <span className="grow" />
        <span className={`chip ${sim.connected ? 'chip-ok' : 'chip-bad'}`}>
          <span className="dot" /> {sim.connected ? 'LIVE' : 'НЕТ СВЯЗИ'}
        </span>
        <span className="chip chip-info">
          t = {Math.max(0, (sim.live?.simulation_time ?? sessionSimStart) - sessionSimStart).toFixed(0)} с
        </span>
        <button
          type="button"
          className={`chip practice-alarm-trigger ${activeAlarms.length > 0 ? 'chip-alarm' : 'chip-ok'}`}
          aria-expanded={alarmsOpen}
          aria-controls="practice-alarm-panel"
          onClick={() => setAlarmsOpen((open) => !open)}
        >
          <span aria-hidden="true">⚠</span>
          {activeAlarms.length > 0
            ? `${activeAlarms.length} активных тревог`
            : `Журнал аварий${alarmHistory.length > 0 ? ` · ${alarmHistory.length}` : ''}`}
        </button>
        <button className="btn btn-stop" disabled={busy} onClick={() => void abort()}>
          Прервать
        </button>
        <button className="btn btn-start" disabled={busy || !ready} onClick={() => void finish()}>
          Завершить задание
        </button>
      </div>

      {task?.goal && (
        <div className="practice-goal">
          <span className="muted">Цель:</span> {task.goal}
          {(task.target_state ?? []).length > 0 && (
            <span className="practice-goal-conds">
              {task.target_state!.map((c, i) => (
                <span className="chip chip-info" key={i}>{condLabel(c)}</span>
              ))}
            </span>
          )}
        </div>
      )}

      {alarmsOpen && (
        <section id="practice-alarm-panel" className="practice-alarm-panel" aria-label="Журнал аварий">
          <div className="practice-alarm-head">
            <div>
              <strong>Журнал аварий</strong>
              <span>{activeAlarms.length} активных · {alarmHistory.length} за сессию</span>
            </div>
            <button
              type="button"
              className="practice-alarm-close"
              aria-label="Закрыть журнал аварий"
              title="Закрыть"
              onClick={() => setAlarmsOpen(false)}
            >
              ×
            </button>
          </div>
          {alarmHistory.length === 0 ? (
            <div className="practice-alarm-empty">Аварий в текущей сессии нет</div>
          ) : (
            <div className="practice-alarm-list">
              {[...alarmHistory].reverse().map((alarm) => {
                const active = activeAlarmIds.has(alarm.id);
                return (
                  <article className={`practice-alarm-row severity-${alarm.severity.toLowerCase()}`} key={alarm.id}>
                    <div className="practice-alarm-row-top">
                      <time>{fmtClock(Math.max(0, alarm.timestamp - sessionSimStart))}</time>
                      <span className={active ? 'alarm-state-active' : 'alarm-state-cleared'}>
                        {active ? 'АКТИВНА' : 'СНЯТА'}
                      </span>
                      <span>{SEVERITY_LABELS[alarm.severity] ?? alarm.severity}</span>
                    </div>
                    <strong>{alarm.description || alarm.parameter}</strong>
                    <div className="practice-alarm-values">
                      <span className="mono">{alarm.parameter}</span>
                      <span>Значение: {alarm.actual_value}</span>
                      <span>Порог: {alarm.threshold}</span>
                    </div>
                  </article>
                );
              })}
            </div>
          )}
        </section>
      )}

      {error ? (
        <div style={{ padding: 20 }}>
          <Err text={error} />
          <button className="btn" style={{ marginTop: 10 }} onClick={() => void run()}>
            Повторить
          </button>
        </div>
      ) : !task ? (
        <Loader text="Загрузка задания…" />
      ) : phase === 'running' && sessionId ? (
        <div className="mnemo-wrap">
          <ScadaScheme key={sessionId} live={sim.live} user={user?.username} />
        </div>
      ) : isInstructor ? (
        <InstructorWait status={confirmInfo} />
      ) : (
        <PracticeGate
          phase={phase === 'running' ? 'waiting' : phase}
          status={confirmInfo}
          confirming={confirming}
          declining={declining}
          canParticipate={canParticipate}
          onConfirm={() => void confirmPractice()}
          onDecline={() => void declinePractice()}
        />
      )}
    </div>
  );
}
