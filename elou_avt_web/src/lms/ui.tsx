import { useCallback, useEffect, useRef, useState } from 'react';
import type { ReactNode } from 'react';
import type { LmsMastery, ModuleKind, ModuleStatus } from '../types';

// ---------------------------------------------------------------- format ---

export function fmtDate(ts?: number | null): string {
  if (!ts) return '—';
  return new Date(ts * 1000).toLocaleDateString('ru-RU');
}

export function fmtDateTime(ts?: number | null): string {
  if (!ts) return '—';
  return new Date(ts * 1000).toLocaleString('ru-RU', { day: '2-digit', month: '2-digit', year: '2-digit', hour: '2-digit', minute: '2-digit' });
}

export function fmtClock(ts?: number | null): string {
  if (ts == null || !Number.isFinite(ts)) return '—';
  const total = Math.max(0, Math.floor(ts));
  const hours = Math.floor(total / 3600);
  const minutes = Math.floor((total % 3600) / 60);
  const seconds = total % 60;
  return [hours, minutes, seconds].map((part) => String(part).padStart(2, '0')).join(':');
}

/** Simulation time (seconds from scenario start) as `t = MM:SS` / `t = H:MM:SS`. */
export function fmtSimTime(s?: number | null): string {
  if (s == null || !Number.isFinite(s)) return '—';
  const total = Math.max(0, Math.floor(s));
  const h = Math.floor(total / 3600);
  const m = Math.floor((total % 3600) / 60);
  const sec = total % 60;
  if (h > 0) {
    return `t = ${h}:${String(m).padStart(2, '0')}:${String(sec).padStart(2, '0')}`;
  }
  return `t = ${String(m).padStart(2, '0')}:${String(sec).padStart(2, '0')}`;
}

export function fmtDur(s?: number | null): string {
  if (s == null || !Number.isFinite(s)) return '—';
  const m = Math.floor(s / 60);
  const sec = Math.round(s % 60);
  if (m <= 0) return `${sec} с`;
  return `${m} мин ${sec} с`;
}

// ------------------------------------------------------------- components ---

export function Page({ title, subtitle, actions, children }: {
  title: ReactNode;
  subtitle?: ReactNode;
  actions?: ReactNode;
  children: ReactNode;
}) {
  return (
    <div className="page">
      <div className="page-inner">
        <div className="page-head">
          <div>
            <div className="page-title">{title}</div>
            {subtitle && <div className="page-sub">{subtitle}</div>}
          </div>
          {actions && <div className="page-actions">{actions}</div>}
        </div>
        {children}
      </div>
    </div>
  );
}

export function Card({ title, subtitle, actions, children, className = '' }: {
  title?: ReactNode;
  subtitle?: ReactNode;
  actions?: ReactNode;
  children: ReactNode;
  className?: string;
}) {
  return (
    <div className={`card ${className}`}>
      {(title || actions) && (
        <div className="card-head">
          <div>
            <div className="card-title">{title}</div>
            {subtitle && <div className="card-sub">{subtitle}</div>}
          </div>
          {actions && <div className="card-actions">{actions}</div>}
        </div>
      )}
      {children}
    </div>
  );
}

export function Grid({ children, min = 220 }: { children: ReactNode; min?: number }) {
  return (
    <div className="grid" style={{ gridTemplateColumns: `repeat(auto-fill, minmax(${min}px, 1fr))` }}>
      {children}
    </div>
  );
}

export function Stat({ label, value, hint, tone }: {
  label: string;
  value: ReactNode;
  hint?: ReactNode;
  tone?: 'ok' | 'warn' | 'bad' | 'accent';
}) {
  return (
    <div className="stat">
      <div className="stat-label">{label}</div>
      <div className={`stat-value${tone ? ` tone-${tone}` : ''}`}>{value}</div>
      {hint && <div className="stat-hint">{hint}</div>}
    </div>
  );
}

export function Bar({ value, tone = 'accent', height = 8 }: {
  value: number;
  tone?: 'ok' | 'warn' | 'bad' | 'accent' | 'gradient';
  height?: number;
}) {
  const v = Math.max(0, Math.min(100, value));
  const fill = tone === 'gradient' ? 'var(--bar-grad)' : `var(--${tone})`;
  return (
    <div className="bar" style={{ height }}>
      <div className="bar-fill" style={{ width: `${v}%`, background: fill }} />
    </div>
  );
}

export function Score({ value, size = 96 }: { value: number; size?: number }) {
  const v = Math.max(0, Math.min(100, value));
  const deg = (v / 100) * 360;
  const tone = v >= 80 ? 'var(--ok)' : v >= 60 ? 'var(--warn)' : 'var(--danger)';
  return (
    <div
      className="gauge"
      style={{
        width: size,
        height: size,
        background: `conic-gradient(${tone} ${deg}deg, var(--panel-2) ${deg}deg)`,
      }}
    >
      <div className="gauge-inner">
        <span className="gauge-value">{Math.round(v)}</span>
        <span className="gauge-unit">/ 100</span>
      </div>
    </div>
  );
}

export function Chip({ children, tone = 'muted' }: {
  children: ReactNode;
  tone?: 'ok' | 'warn' | 'bad' | 'accent' | 'muted';
}) {
  return <span className={`badge badge-${tone}`}>{children}</span>;
}

export function ModuleIcon({ status }: { status: ModuleStatus }) {
  if (status === 'COMPLETED') return <span className="mi mi-done">✓</span>;
  if (status === 'IN_PROGRESS') return <span className="mi mi-half">◐</span>;
  return <span className="mi mi-empty">□</span>;
}

const KIND_LABEL: Record<ModuleKind, string> = {
  theory: 'Теория',
  practice: 'Практика',
  exam: 'Экзамен',
};

export function KindTag({ kind }: { kind: ModuleKind }) {
  return <span className={`badge tag-${kind}`}>{KIND_LABEL[kind] ?? kind}</span>;
}

export function StatusTag({ status }: { status: ModuleStatus }) {
  const label = status === 'COMPLETED' ? 'Завершён' : status === 'IN_PROGRESS' ? 'В процессе' : 'Не начат';
  const tone = status === 'COMPLETED' ? 'ok' : status === 'IN_PROGRESS' ? 'accent' : 'muted';
  return <Chip tone={tone}>{label}</Chip>;
}

export function DifficultyTag({ d }: { d: string }) {
  const tone = d === 'EASY' ? 'ok' : d === 'HARD' ? 'bad' : 'warn';
  const label = d === 'EASY' ? 'Просто' : d === 'HARD' ? 'Сложно' : 'Средне';
  return <Chip tone={tone}>{label}</Chip>;
}

export function StageLadder({ mastery }: { mastery: LmsMastery }) {
  const stages = mastery.stages;
  return (
    <div className="ladder">
      {stages.map((s, i) => {
        const state = i < mastery.stage_index ? 'done' : i === mastery.stage_index ? 'on' : '';
        return (
          <div key={s} className="ladder-step-wrap">
            <div className={`ladder-step ${state}`}>
              <span className="ladder-dot" />
              <span className="ladder-name">{s}</span>
            </div>
            {i < stages.length - 1 && <div className={`ladder-arrow${state === 'done' ? ' done' : ''}`}>↓</div>}
          </div>
        );
      })}
    </div>
  );
}

export function Empty({ text = 'Данных пока нет' }: { text?: string }) {
  return <div className="empty">{text}</div>;
}

export function Loader({ text = 'Загрузка…' }: { text?: string }) {
  return <div className="empty">{text}</div>;
}

export function Err({ text }: { text: string }) {
  return <div className="login-error">{text}</div>;
}

// ------------------------------------------------------------ async hook ---

export function useAsync<T>(fn: () => Promise<T>, deps: unknown[]) {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState('');
  const [loading, setLoading] = useState(true);
  const seq = useRef(0);

  const reload = useCallback(() => {
    const id = ++seq.current;
    setLoading(true);
    setError('');
    Promise.resolve()
      .then(fn)
      .then((d) => {
        if (seq.current === id) setData(d);
      })
      .catch((e: unknown) => {
        if (seq.current === id) setError(e instanceof Error ? e.message : String(e));
      })
      .finally(() => {
        if (seq.current === id) setLoading(false);
      });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, deps);

  useEffect(() => {
    reload();
    return () => {
      seq.current += 1;
    };
     
  }, [reload]);

  return { data, error, loading, reload };
}

export function usePoll<T>(fn: () => Promise<T>, ms: number) {
  const { data, error, loading, reload } = useAsync<T>(fn, []);
  useEffect(() => {
    const id = setInterval(() => void reload(), ms);
    return () => clearInterval(id);
  }, [reload, ms]);
  return { data, error, loading, reload };
}

export function notifyToast(msg: string): void {
  const el = document.createElement('div');
  el.className = 'toast';
  el.textContent = msg;
  document.body.appendChild(el);
  setTimeout(() => el.remove(), 3000);
}
