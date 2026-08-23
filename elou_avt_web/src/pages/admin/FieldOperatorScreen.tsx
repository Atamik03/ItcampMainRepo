import { useCallback, useEffect, useRef, useState } from 'react';
import { api } from '../../api';
import { useAuth } from '../../auth';
import { useTheme } from '../../lms/theme';
import type { FieldErrorsResponse } from '../../types';

const ROLE_LABELS: Record<string, string> = {
  operator: 'Консольный оператор',
  field_operator: 'Полевой оператор',
};

export default function FieldOperatorScreen() {
  const { theme } = useTheme();
  const { user } = useAuth();
  const canParticipate = (user?.roles ?? []).some(
    (r) => r === 'operator' || r === 'field_operator',
  );
  const frameRef = useRef<HTMLIFrameElement | null>(null);
  // Keep iframe URL stable: theme changes are delivered with postMessage and
  // must not rebuild the expensive 3D scene from scratch.
  const [frameSrc] = useState(() => `/avt4_3d_model_v7.html?theme=${theme}`);
  const [modelLoading, setModelLoading] = useState(true);
  const [modelError, setModelError] = useState('');
  const [ctx, setCtx] = useState<FieldErrorsResponse>({
    active: false,
    session_id: null,
    scenario_id: null,
    title: '',
    errors: [],
    status: 'NO_ACTIVE',
  });
  const ctxRef = useRef(ctx);
  ctxRef.current = ctx;
  const [confirming, setConfirming] = useState(false);
  const [confirmError, setConfirmError] = useState('');
  const [handledSession, setHandledSession] = useState<string | null>(null);
  const [fsExited, setFsExited] = useState<string | null>(null);
  const [elapsed, setElapsed] = useState(0);

  // Полноэкранный режим практики (как у консольного оператора): активен,
  // когда инструктор запустил практику именно для этого оператора.
  const inPractice =
    ctx.active &&
    ctx.invited_me === true &&
    ctx.status !== 'NO_ACTIVE' &&
    ctx.session_id !== fsExited;

  const pending =
    inPractice &&
    ctx.status === 'PENDING_CONFIRM' &&
    ctx.session_id !== handledSession;

  // Счётчик времени практики после старта (стена).
  useEffect(() => {
    if (!inPractice || ctx.status !== 'RUNNING') {
      setElapsed(0);
      return;
    }
    const start = Date.now();
    setElapsed(0);
    const t = window.setInterval(() => setElapsed(Math.round((Date.now() - start) / 1000)), 1000);
    return () => window.clearInterval(t);
  }, [inPractice, ctx.status, ctx.session_id]);

  const syncTheme = useCallback(() => {
    const frame = frameRef.current;
    if (frame && frame.contentWindow) {
      // Target origin can't be a concrete origin string here: the iframe
      // below is `sandbox="allow-scripts"` WITHOUT `allow-same-origin`,
      // which forces its content into an opaque ("null") origin by spec --
      // postMessage would silently fail to deliver against any real origin
      // string, since it would never match. The message itself carries no
      // sensitive data (UI theme only).
      frame.contentWindow.postMessage({ type: 'elou-theme', theme }, '*'); // nosemgrep: javascript.browser.security.wildcard-postmessage-configuration.wildcard-postmessage-configuration
    }
  }, [theme]);

  useEffect(() => {
    syncTheme();
  }, [syncTheme]);

  const syncErrors = useCallback(() => {
    const frame = frameRef.current;
    if (frame && frame.contentWindow) {
      // Same opaque-sandboxed-origin constraint as syncTheme() above.
      // nosemgrep: javascript.browser.security.wildcard-postmessage-configuration.wildcard-postmessage-configuration
      frame.contentWindow.postMessage(
        { type: 'elou-field-errors', errors: ctx.errors, session_id: ctx.session_id },
        '*',
      );
    }
  }, [ctx.errors, ctx.session_id]);

  useEffect(() => {
    syncErrors();
  }, [syncErrors]);

  useEffect(() => {
    const onMessage = async (e: MessageEvent) => {
      if (!e.data || e.source !== frameRef.current?.contentWindow) return;
      if (e.data.type === 'elou-model-ready') {
        setModelLoading(false);
        setModelError('');
      } else if (e.data.type === 'elou-model-error') {
        setModelLoading(false);
        setModelError('Не удалось подготовить 3D-модель. Обновите экран или проверьте поддержку WebGL.');
      } else if (e.data.type === 'elou-pick') {
        api.logScadaEvent({
          event_type: 'click',
          object_id: e.data.tag ?? '',
          object_name: e.data.name ?? '',
        }).catch(() => {});
      } else if (e.data.type === 'elou-field-send') {
        const c = ctxRef.current;
        const tag = String(e.data.object_id ?? '');
        const err = c.errors.find((x) => x.object_id === tag);
        if (!c.session_id || !err) return;
        try {
          await api.chatSend({
            session_id: c.session_id,
            kind: 'field_error',
            object_id: tag,
            text: `${err.title}: ${err.description}`,
          });
          const frame = frameRef.current;
          if (frame && frame.contentWindow) {
            // Same opaque-sandboxed-origin constraint as syncTheme() above.
            frame.contentWindow.postMessage({ type: 'elou-field-sent', object_id: tag }, '*'); // nosemgrep: javascript.browser.security.wildcard-postmessage-configuration.wildcard-postmessage-configuration
          }
          api.logScadaEvent({
            event_type: 'inspector_open',
            object_id: tag,
            object_name: err.title,
          }).catch(() => {});
        } catch {
          /* ignore send errors */
        }
      }
    };
    window.addEventListener('message', onMessage);
    return () => window.removeEventListener('message', onMessage);
  }, []);

  useEffect(() => {
    let alive = true;
    const poll = async () => {
      if (document.hidden) return;
      try {
        const res = await api.lmsFieldErrors();
        if (alive) setCtx(res);
      } catch {
        /* server may be unreachable */
      }
    };
    poll();
    const t = window.setInterval(poll, 3000);
    return () => {
      alive = false;
      window.clearInterval(t);
    };
  }, []);

  const confirmPractice = useCallback(async () => {
    if (!ctx.session_id || confirming) return;
    setConfirming(true);
    setConfirmError('');
    try {
      const s = await api.lmsPracticeConfirm(ctx.session_id);
      setHandledSession(ctx.session_id);
      setCtx((c) => ({ ...c, status: s.status }));
    } catch (e) {
      setConfirmError(e instanceof Error ? e.message : String(e));
    } finally {
      setConfirming(false);
    }
  }, [ctx.session_id, confirming]);

  const declinePractice = useCallback(async () => {
    if (!ctx.session_id || confirming) return;
    setConfirming(true);
    setConfirmError('');
    try {
      await api.lmsPracticeDecline(ctx.session_id);
      setHandledSession(ctx.session_id);
      setCtx((c) => ({ ...c, status: 'NO_ACTIVE' }));
    } catch (e) {
      setConfirmError(e instanceof Error ? e.message : String(e));
    } finally {
      setConfirming(false);
    }
  }, [ctx.session_id, confirming]);

  const exitFullscreen = useCallback(() => {
    if (ctx.status === 'PENDING_CONFIRM' && ctx.session_id) {
      void declinePractice();
    } else if (ctx.session_id) {
      setFsExited(ctx.session_id);
    }
  }, [ctx.status, ctx.session_id, declinePractice]);

  return (
    <div className={`field-operator-screen${inPractice ? ' fullscreen' : ''}`}>
      <iframe
        ref={frameRef}
        src={frameSrc}
        onLoad={() => {
          syncTheme();
          syncErrors();
        }}
        title="Экран полевого оператора — 3D-модель установки"
        className="field-operator-frame"
        loading="eager"
        referrerPolicy="no-referrer"
        sandbox="allow-scripts"
      />
      {modelLoading && (
        <div className="field-model-loading" role="status" aria-live="polite">
          <span className="field-model-spinner" aria-hidden="true" />
          <strong>Загрузка 3D-модели установки…</strong>
          <span>Подготавливаем оборудование и трубопроводы</span>
        </div>
      )}
      {modelError && (
        <div className="field-model-error" role="alert">
          <strong>3D-модель недоступна</strong>
          <span>{modelError}</span>
          <button
            className="btn"
            type="button"
            onClick={() => window.location.reload()}
          >
            Повторить
          </button>
        </div>
      )}
      {inPractice ? (
        <div className="field-practice-bar">
          <span className="field-practice-title">{ctx.title || 'Практика'}</span>
          <span className="grow" />
          {ctx.session_id && <span className="chip chip-info mono">сессия {ctx.session_id.slice(0, 12)}</span>}
          <span className={`chip ${ctx.status === 'RUNNING' ? 'chip-ok' : 'chip-info'}`}>
            {ctx.status === 'RUNNING'
              ? 'Практика активна'
              : ctx.status === 'PENDING_CONFIRM'
                ? 'Ожидает подтверждения'
                : ctx.status}
          </span>
          {ctx.status === 'RUNNING' && <span className="chip chip-info">t = {elapsed} с</span>}
          <button className="btn" onClick={exitFullscreen}>Выйти</button>
        </div>
      ) : (
        ctx.active && (
          <div className="field-session-status">
            <span className={`chip ${ctx.status === 'RUNNING' ? 'chip-ok' : 'chip-info'}`}>
              {ctx.status === 'RUNNING'
                ? 'Сессия практики активна'
                : ctx.status === 'PENDING_CONFIRM'
                  ? 'Ожидает подтверждения'
                  : ctx.status}
            </span>
            {ctx.session_id && <span className="chip chip-info mono">сессия {ctx.session_id.slice(0, 12)}</span>}
            {ctx.title && <span className="chip chip-info">{ctx.title}</span>}
          </div>
        )
      )}
      {pending && (
        <div className="field-confirm-overlay">
          <div className="field-confirm-card">
            <div className="field-confirm-title">Практика запущена инструктором</div>
            <div className="field-confirm-text">
              Подтвердите готовность — практика стартует, когда готовность подтвердит консольный оператор.
            </div>
            <div className="field-confirm-roles">
              {(ctx.required_roles ?? []).map((role) => {
                const ok = (ctx.confirmed_roles ?? []).includes(role);
                return (
                  <div className={`field-confirm-role ${ok ? 'ok' : ''}`} key={role}>
                    <span className={`dot ${ok ? 'dot-ok' : 'dot-wait'}`} />
                    {ROLE_LABELS[role] ?? role}
                    <span className="muted">{ok ? ' · подтвердил' : ' · ожидание'}</span>
                  </div>
                );
              })}
            </div>
            {ctx.timeout_seconds != null && ctx.timeout_seconds > 0 && (
              <div className="field-confirm-sub">Автостарт через {ctx.timeout_seconds} с, если второй оператор не подтвердит.</div>
            )}
            {confirmError && <div className="field-confirm-error">{confirmError}</div>}
            {canParticipate ? (
              <div className="field-confirm-actions">
                <button className="field-confirm-btn" disabled={confirming} onClick={() => void confirmPractice()}>
                  {confirming ? 'Отправляем…' : 'Я готов, подтвердить'}
                </button>
                <button className="field-confirm-btn field-confirm-decline" disabled={confirming} onClick={() => void declinePractice()}>
                  Отклонить
                </button>
              </div>
            ) : (
              <div className="field-confirm-sub">Ваша роль не участвует в практике — подтверждение доступно консольному и полевому операторам.</div>
            )}
          </div>
        </div>
      )}
    </div>
  );
}
