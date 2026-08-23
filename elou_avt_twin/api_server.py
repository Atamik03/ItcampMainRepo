from contextlib import asynccontextmanager
from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect, Depends, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, Field
from typing import Optional, Dict, Any, Union
import asyncio
import json
import os
import threading
import time
import logging
import traceback
import re
import uuid
from pathlib import Path

from models.base import SimulationConfig, OperatorAction, ActionType
from models.command import Command, CommandAction
from models.session import TrainingSession
from models.scenario import Scenario
from simulation_core.digital_twin import DigitalTwin
from scheme import (
    ProcessScheme,
    load_scheme,
    save_scheme,
    migrate_scheme_data,
)
from equipment.params_spec import editor_spec, coerce, spec_for, NON_EDITABLE_TYPES
from controls import ControlSystem
from scenarios.scenario_registry import SCENARIO_REGISTRY
from persistence.session_store import SessionStore
from persistence.session_recorder import SessionRecorder
from auth.deps import authenticate_websocket, get_auth_service, get_current_user, require_permission
from auth.models import (
    LoginRequest,
    Principal,
    RoleAssign,
    RoleCreate,
    RolePermissions,
    RoleUpdate,
    UserCreate,
)
from lms.api import router as lms_router
from lms.content_api import router as lms_content_router
from lms import runtime as lms_runtime
from lms.chat_hub import (
    register as chat_register,
    unregister as chat_unregister,
    redis_listener as chat_redis_listener,
)
from lms.content_store import LmsContentStore
from lms.scenario_service import to_engine_scenario
from realtime import redis_bus

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("elou_avt.api")

app = FastAPI(title="ELOU-AVT Digital Twin API", version="1.1.0")


def _cors_origins() -> list[str]:
    configured = os.environ.get("ELOU_CORS_ORIGINS", "")
    if configured.strip():
        origins = [value.strip() for value in configured.split(",") if value.strip()]
        if "*" in origins:
            raise RuntimeError("ELOU_CORS_ORIGINS='*' is not allowed with authenticated requests")
        return origins
    return [
        "http://localhost:5173",
        "http://127.0.0.1:5173",
        "http://localhost:4173",
        "http://127.0.0.1:4173",
    ]


app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins(),
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type", "Accept"],
)


@app.middleware("http")
async def security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "SAMEORIGIN")
    response.headers.setdefault("Referrer-Policy", "no-referrer")
    response.headers.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
    response.headers.setdefault("Cache-Control", "no-store")
    if request.url.scheme == "https":
        response.headers.setdefault("Strict-Transport-Security", "max-age=31536000; includeSubDomains")
    return response
app.include_router(lms_router)
app.include_router(lms_content_router)


def _load_config() -> SimulationConfig:
    """Load SimulationConfig from config/default_config.json when present."""
    default = SimulationConfig(dt=1.0, random_seed=42)
    config_path = Path(__file__).resolve().parent / "config" / "default_config.json"
    if not config_path.exists():
        logger.warning("Config file %s not found; using defaults.", config_path)
        return default
    try:
        import json
        with open(config_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        merged = {
            k: v for k, v in data.items()
            if k in SimulationConfig.model_fields
        }
        return SimulationConfig(**merged)
    except Exception:
        logger.exception("Failed to load config from %s; using defaults.", config_path)
        return default

class ActionRequest(BaseModel):
    equipment_id: str
    action_type: ActionType
    value: Optional[float] = None
    # Kept for backward compatibility with old demo clients. HTTP handlers
    # always use the authenticated principal instead of trusting this field.
    operator_id: str = "demo"

class InputRequest(BaseModel):
    flow_kg_s: Optional[float] = Field(None, ge=0)
    temperature_c: Optional[float] = Field(None, ge=-50, le=1000)
    pressure_bar: Optional[float] = Field(None, gt=0, le=200)

class ScenarioRequest(BaseModel):
    scenario_id: str

class EquipmentParamsRequest(BaseModel):
    equipment_id: str
    params: Dict[str, float] = Field(default_factory=dict)

class CommandRequest(BaseModel):
    tag: str
    action: CommandAction
    value: Optional[Union[float, str]] = None
    operator_id: str = "demo"

class StartSessionRequest(BaseModel):
    scenario_id: str = "BASELINE"
    operator_id: str = "demo"

class AlarmSetpointRequest(BaseModel):
    low_low: Optional[float] = None
    low: Optional[float] = None
    high: Optional[float] = None
    high_high: Optional[float] = None
    unit: Optional[str] = None

class SimulationSpeedRequest(BaseModel):
    speed: float = Field(1.0, ge=1.0, le=30.0)


def _baseline_scenario() -> Scenario:
    """Базовый режим ядра, когда активный авторский сценарий не задан."""
    return Scenario(
        id="BASELINE",
        name="Нормальная работа",
        description="Базовый режим ядра симулятора.",
    )


lock = threading.RLock()
twin = DigitalTwin(_load_config())
twin.create_simulation()
twin.load_scenario_object(_baseline_scenario())
twin.start()

# Simulation time multiplier (1x = 1 sim-second per real second).
# The loop accumulates real time * speed and runs whole 1.0 s steps, so the
# physics timestep never changes and numerical behaviour stays identical.
SIM_SPEED_MIN = 1.0
SIM_SPEED_MAX = 30.0
_sim_speed = 1.0

def get_sim_speed() -> float:
    with lock:
        return _sim_speed

def set_sim_speed(speed: float) -> float:
    global _sim_speed
    speed = float(speed)
    if speed < SIM_SPEED_MIN:
        speed = SIM_SPEED_MIN
    if speed > SIM_SPEED_MAX:
        speed = SIM_SPEED_MAX
    with lock:
        _sim_speed = speed
    return _sim_speed

# Unified controller catalogue (Этап 3 contract) — the HMI renders faceplates
# from /controllers and sends changes via /command.
control_system = ControlSystem()

# Active training session (Training Layer, Этап 7 groundwork).
_training_session: Optional[TrainingSession] = None

# Persistent event log for the AI error-classification service. Created
# lazily on the first /training/session call so importing this module does
# not touch the filesystem.
session_store: Optional[SessionStore] = None
session_recorder: Optional[SessionRecorder] = None

# UI-facing input overrides. The underlying engine uses these values as its feed source.
inputs: Dict[str, float] = {"flow_kg_s": 100.0, "temperature_c": 25.0, "pressure_bar": 1.01325}

# P&ID scheme owned by the API layer and pushed into the engine on changes.
_DEFAULT_SCHEME = "process_elou_avt"
SCHEME_DIR = Path(__file__).resolve().parent / "schemes"
_LAST_SCHEME_FILE = SCHEME_DIR / ".last_scheme"
_SCHEME_NAME_RE = re.compile(r"^[\w .()—-]+$", re.UNICODE)


def _scheme_path(name: str) -> Path:
    """Resolve a scheme name inside SCHEME_DIR and reject path traversal."""
    clean = name.strip()
    if (
        not clean
        or len(clean) > 128
        or clean.startswith(".")
        or clean.endswith((".", " "))
        or "/" in clean
        or "\\" in clean
        or "\x00" in clean
        or not _SCHEME_NAME_RE.fullmatch(clean)
    ):
        raise ValueError("Invalid scheme name")
    path = (SCHEME_DIR / f"{clean}.json").resolve()
    if path.parent != SCHEME_DIR.resolve():
        raise ValueError("Invalid scheme path")
    return path


def _last_used_scheme() -> str:
    """Return the id of the most recently saved/loaded scheme (default otherwise)."""
    try:
        if _LAST_SCHEME_FILE.exists():
            name = _LAST_SCHEME_FILE.read_text(encoding="utf-8-sig").strip()
            if name and _scheme_path(name).exists():
                return name
    except (OSError, ValueError):
        logger.exception("Failed to read last-used scheme marker.")
    return _DEFAULT_SCHEME


def _remember_scheme(name: str) -> None:
    """Persist the id of the last active scheme so it re-opens on restart."""
    try:
        clean = name.strip()
        _scheme_path(clean)
        tmp = _LAST_SCHEME_FILE.with_suffix(".tmp")
        tmp.write_text(clean, encoding="utf-8")
        tmp.replace(_LAST_SCHEME_FILE)
    except (OSError, ValueError):
        logger.exception("Failed to write last-used scheme marker.")


def _restore_alarm_setpoints() -> None:
    """Apply manually saved alarm setpoints from the database at startup."""
    try:
        store = SessionStore()
        try:
            restored = 0
            for sp in store.load_alarm_setpoints():
                if twin._engine.restore_alarm_setpoint(
                    sp["parameter"], sp["low_low"], sp["low"], sp["high"], sp["high_high"], sp["unit"] or "",
                ):
                    restored += 1
            if restored:
                logger.info("Restored %d saved alarm setpoint(s).", restored)
        finally:
            store.close()
    except Exception:
        logger.exception("Failed to restore alarm setpoints")


scheme_store: ProcessScheme = load_scheme(_scheme_path(_last_used_scheme()))
twin._engine.set_scheme(scheme_store)
_restore_alarm_setpoints()

# The LMS content layer (lms/content_*.py) reads the simulator singletons
# through this bridge; re-configured whenever the session layer is created.
lms_runtime.configure(twin, scheme_store, session_store, session_recorder, inputs)


def ensure_session_layer() -> None:
    """Create the training session store/recorder on demand and repoint the
    LMS content layer at the same instances (one shared recorder for /action,
    /command and the practice runner)."""
    global session_store, session_recorder
    with lock:
        if session_store is None:
            session_store = SessionStore()
        if session_recorder is None:
            session_recorder = SessionRecorder(session_store)
        lms_runtime.configure(twin, scheme_store, session_store, session_recorder, inputs)


def _build_node_telemetry(twin) -> Dict[str, Any]:
    """Per-node live telemetry derived from the last engine step."""
    outputs = twin._engine.get_last_outputs()
    streams = twin._engine.get_last_streams()
    equip = twin._engine._equipment
    state = twin.get_state()
    telemetry: Dict[str, Any] = {}

    for node in scheme_store.nodes:
        nid, ntype = node.id, node.type
        out = outputs.get(nid, {})
        eq = equip.get(nid)
        s = out.get("outlet_stream")
        if s is None:
            s = streams.get(f"{nid}:out")

        item: Dict[str, Any] = {
            "type": ntype,
            "name": node.name,
            "running": bool(eq.state.running) if eq else None,
            "failed": bool(eq.state.failed) if eq else None,
            "failure_mode": (eq.state.failure_mode or "") if eq else None,
            "params": {},
        }
        p = item["params"]

        if ntype == "pump":
            p["flow_kg_s"] = out.get("flow_out", 0.0)
            p["power_w"] = out.get("power", 0.0)
            p["pressure_bar"] = round(s.pressure / 1e5, 3) if s else None
            p["temperature_c"] = round(s.temperature - 273.15, 2) if s else None
            p["efficiency"] = eq.efficiency if eq else None
            p["speed_rpm"] = round(eq.speed, 1) if eq else None
        elif ntype in ("valve", "angle_valve"):
            p["position"] = round(out.get("position", 0.0) * 100.0, 2)
            p["flow_kg_s"] = out.get("flow_out", 0.0)
            p["pressure_in_bar"] = round(out["inlet_pressure"] / 1e5, 3) if out.get("inlet_pressure") else None
            p["pressure_out_bar"] = round(out["outlet_pressure"] / 1e5, 3) if out.get("outlet_pressure") else None
        elif ntype == "gate_valve":
            p["open"] = bool(eq.is_open) if eq else None
            p["flow_kg_s"] = round(out.get("flow_out", 0.0), 3)
            p["blocked"] = bool(out.get("blocked", False))
        elif ntype in ("separator", "separator_s1k", "tank"):
            s_ref = s or out.get("out_b") or out.get("out_t")
            p["level_m"] = out.get("level")
            p["level_setpoint_m"] = out.get("setpoint")
            p["in_flow"] = round(out.get("in_flow", 0.0), 3)
            p["out_flow"] = round(out.get("out_flow", 0.0), 3)
            p["flow_kg_s"] = round(out.get("out_flow", 0.0), 3)
            p["pressure_bar"] = round(s_ref.pressure / 1e5, 3) if s_ref else None
            p["temperature_c"] = round(s_ref.temperature - 273.15, 2) if s_ref else None
            p["volume_m3"] = out.get("volume_m3")
        elif ntype == "mixer":
            p["flow_kg_s"] = round(s.mass_flow, 3) if s else 0.0
            p["temperature_c"] = round(s.temperature - 273.15, 2) if s else None
            p["pressure_bar"] = round(s.pressure / 1e5, 3) if s else None
        elif ntype == "elou":
            p["flow_kg_s"] = round(s.mass_flow, 3) if s else 0.0
            p["power_w"] = eq.power_consumption if eq else 0.0
            p["level_m"] = state.level.get("elou")
            p["temperature_c"] = round(s.temperature - 273.15, 2) if s else None
            p["pressure_bar"] = round(s.pressure / 1e5, 3) if s else None
            p["volume_m3"] = out.get("volume_m3")
        elif ntype == "heat_exchanger":
            p["duty_w"] = out.get("duty", 0.0)
            p["t_cold_in_c"] = round(out["t_cold_in"] - 273.15, 2) if out.get("t_cold_in") else None
            p["t_cold_out_c"] = round(out["t_cold_out"] - 273.15, 2) if out.get("t_cold_out") else None
            p["t_hot_in_c"] = round(out["t_hot_in"] - 273.15, 2) if out.get("t_hot_in") else None
            p["t_hot_out_c"] = round(out["t_hot_out"] - 273.15, 2) if out.get("t_hot_out") else None
        elif ntype == "heater":
            p["duty_w"] = eq.duty if eq else 0.0
            p["fuel_flow"] = eq.fuel_flow if eq else 0.0
            p["outlet_temp_c"] = round(eq.outlet_temp - 273.15, 2) if eq else None
            for in_port, out_port in (
                ("in", "out"), ("in2", "out2"), ("in3", "out3"),
                ("in4", "out4"), ("pp_in", "pp_out"),
            ):
                s_out = out.get(out_port)
                if s_out is not None:
                    p[f"flow_{out_port}"] = round(s_out.mass_flow, 3)
                    p[f"t_{out_port}_c"] = round(s_out.temperature - 273.15, 2)
            p["flow_kg_s"] = round(out.get("flow_out", 0.0), 3)
        elif ntype == "column":
            dist = out.get("distillate")
            side = out.get("side_draw")
            bott = out.get("bottoms")
            p["distillate_flow"] = dist.mass_flow if dist else 0.0
            p["side_draw_flow"] = side.mass_flow if side else 0.0
            p["bottoms_flow"] = bott.mass_flow if bott else 0.0
            p["flow_kg_s"] = ((dist.mass_flow if dist else 0.0)
                              + (side.mass_flow if side else 0.0)
                              + (bott.mass_flow if bott else 0.0))
            p["top_temp_c"] = round(dist.temperature - 273.15, 2) if dist else None
            p["bottom_temp_c"] = round(bott.temperature - 273.15, 2) if bott else None
            p["pressure_bar"] = round(dist.pressure / 1e5, 3) if dist else None
            p["level_m"] = state.level.get("column")
            p["volume_m3"] = out.get("volume_m3")
            p["converged"] = out.get("converged")
        elif ntype == "source":
            p["flow_kg_s"] = round(s.mass_flow, 3) if s else 0.0
            p["temperature_c"] = round(s.temperature - 273.15, 2) if s else None
            p["pressure_bar"] = round(s.pressure / 1e5, 3) if s else None
        elif ntype == "sink":
            # A sink is the boundary that accepts EVERY incoming stream, so its
            # telemetry is the mass-conserving aggregate of all edges targeting
            # it: flows sum, intensive properties are mass-weighted averaged
            # (identical to _merge_streams in the engine).
            in_streams = [
                streams.get(f"{edge.source}:{edge.source_port}")
                for edge in scheme_store.edges
                if edge.target == nid
            ]
            in_streams = [s for s in in_streams if s is not None and s.mass_flow > 0]
            if in_streams:
                total = sum(s.mass_flow for s in in_streams)
                p["flow_kg_s"] = round(total, 3)
                p["temperature_c"] = round(
                    sum(s.temperature * s.mass_flow for s in in_streams) / total - 273.15, 2
                )
                p["pressure_bar"] = round(
                    sum(s.pressure * s.mass_flow for s in in_streams) / total / 1e5, 3
                )
            else:
                p["flow_kg_s"] = None
                p["temperature_c"] = None
                p["pressure_bar"] = None

        telemetry[nid] = item

    return telemetry


def _build_history() -> Dict[str, Any]:
    """Time series of key process variables from the engine state history.

    Plant-wide aggregate series (feed_flow, column_*, ...) keep the legacy
    trend charts working, while each node also contributes its own series
    under '<node_id>:<param>' keys so the inspector can draw per-equipment
    graphs (e.g. 'elo_5:level_m', 'val_3:flow_kg_s').
    """
    history = twin.get_history()
    times: list = []
    series: Dict[str, list] = {
        "feed_flow": [],
        "column_pressure_bar": [],
        "column_temp_c": [],
        "furnace_temp_c": [],
        "preheat_temp_c": [],
        "elou_level": [],
        "column_level": [],
        "valve_fv101_position": [],
    }
    # Per-node series are materialised lazily on first use so a scheme change
    # never leaves stale keys behind.
    node_series: Dict[str, Dict[str, list]] = {}
    for st in history:
        times.append(round(st.timestamp, 1))
        series["feed_flow"].append(round(st.feed_flow, 3))
        series["column_pressure_bar"].append(round(st.pressure.get("column", 0.0) / 1e5, 4))
        series["column_temp_c"].append(round(st.temperature.get("column", 0.0) - 273.15, 2))
        series["furnace_temp_c"].append(round(st.temperature.get("furnace_outlet", 0.0) - 273.15, 2))
        series["preheat_temp_c"].append(round(st.temperature.get("preheat_outlet", 0.0) - 273.15, 2))
        series["elou_level"].append(round(st.level.get("elou", 0.0), 3))
        series["column_level"].append(round(st.level.get("column", 0.0), 3))
        valve_pos = st.valve_positions.get("valve_FV101")
        if valve_pos is None and st.valve_positions:
            valve_pos = next(iter(st.valve_positions.values()))
        series["valve_fv101_position"].append(round((valve_pos or 0.0) * 100.0, 2))
        for nid, params in st.node_params.items():
            bucket = node_series.setdefault(nid, {})
            for key, value in params.items():
                if not isinstance(value, (int, float)):
                    continue
                bucket.setdefault(key, []).append(value)
    for nid, params in node_series.items():
        for key, values in params.items():
            series[f"{nid}:{key}"] = values
    return {"times": times, "series": series}


def _serialize_state():
    with lock:
        s = twin.get_state()
        return _sanitize({
            "status": twin._status.value,
            "simulation_time": twin._simulation_time,
            "speed": _sim_speed,
            "feed": {
                "flow_kg_s": inputs["flow_kg_s"],
                "flow_m3_h": inputs["flow_kg_s"] * 4.235,
                "temperature_c": inputs["temperature_c"],
                "pressure_bar": inputs["pressure_bar"],
            },
            "pressure": {k: v for k, v in s.pressure.items()},
            "temperature": {k: v for k, v in s.temperature.items()},
            "feed_flow": s.feed_flow,
            "product_flow": s.product_flow,
            "feed_flow_kg_s": s.feed_flow,
            "feed_flow_m3_h": s.feed_flow / 850.0 * 3600.0,
            "heat_duty": {k: v for k, v in s.heat_duty.items()},
            "level": s.level,
            "pump_states": s.pump_states,
            "valve_positions": {k: round(v * 100.0, 2) for k, v in s.valve_positions.items()},
            "equipment_states": s.equipment_states,
            "equipment": _build_node_telemetry(twin),
            "active_failures": s.active_failures,
            "alarms": [a.model_dump() for a in s.alarms],
            "alarm_history": [
                a.model_dump() for a in twin._engine._alarm_system.get_alarm_history()
            ],
            "errors": [e.model_dump() for e in s.errors[-20:]],
            "controllers": control_system.snapshot(),
        })


# Read-path snapshot.  The heavy hydraulic step runs under `lock` and can take
# ~1-2 s, so polling endpoints that used to take the same lock (state, alarms,
# events, score, controllers, scheme) would block behind it for the whole step.
# Instead the simulation loop rebuilds this snapshot under the lock after every
# step, and read endpoints serve the cached copy without ever waiting on it.
_snapshot: Optional[Dict[str, Any]] = None
_snapshot_lock = threading.Lock()


def _refresh_snapshot() -> None:
    """Rebuild the read-path snapshot.  Caller must hold `lock` (RLock)."""
    global _snapshot
    data = {
        "state": _serialize_state(),
        "alarms": [a.model_dump() for a in twin.get_alarms()],
        "events": [e.model_dump() for e in twin.get_events()],
        "score": twin.get_score_data(),
        "scheme": scheme_store.model_dump(mode="json"),
    }
    with _snapshot_lock:
        _snapshot = data


def _snapshot_state() -> Optional[Dict[str, Any]]:
    """Latest serialized state without touching the simulation lock."""
    with _snapshot_lock:
        snap = _snapshot
    return snap["state"] if snap else None


def _snapshot_value(key: str) -> Any:
    """Serve one snapshot entry, building it under the lock on first use."""
    with _snapshot_lock:
        snap = _snapshot
    if snap is not None:
        return snap.get(key)
    with lock:
        _refresh_snapshot()
        with _snapshot_lock:
            return _snapshot[key]


def _sanitize(obj):
    """Replace non-finite floats with None so the JSON response can never fail."""
    if isinstance(obj, float):
        return None if not (obj == obj) or obj in (float("inf"), float("-inf")) else obj
    if isinstance(obj, dict):
        return {k: _sanitize(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_sanitize(v) for v in obj]
    return obj

def _node_type_for(equipment_id: str) -> Optional[str]:
    """Resolve the P&ID node type of an equipment id.

    Falls back to the engine equipment class for ids that are not in the
    current scheme (e.g. demo ids used by scenario reference actions).
    """
    node = scheme_store.node(equipment_id)
    if node is not None:
        return node.type
    eq = twin._engine._equipment.get(equipment_id)
    if eq is not None:
        return {
            "Pump": "pump", "Valve": "valve", "AngleValve": "angle_valve",
            "GateValve": "gate_valve",
            "Heater": "heater",
            "ELOU": "elou", "Tank": "tank", "HeatExchanger": "heat_exchanger",
            "DistillationColumn": "column", "Separator": "separator",
        }.get(type(eq).__name__)
    return None


def _current_regulated_value(equipment_id: str) -> Optional[float]:
    """Best-effort current value of the regulated parameter of an equipment.

    Used to fill `old_value` in operator actions so directional checks
    ("increase/decrease the parameter") can compare before vs after.
    """
    if twin._engine is None:
        return None
    eq = twin._engine._equipment.get(equipment_id)
    if eq is None:
        return None
    for attr in ("position", "fuel_flow", "reflux_ratio", "speed"):
        v = getattr(eq, attr, None)
        if isinstance(v, (int, float)):
            return float(v)
    return None


@app.get("/")
def root() -> RedirectResponse:
    return RedirectResponse(url="/docs", status_code=307)


@app.get("/favicon.ico")
def favicon() -> RedirectResponse:
    return RedirectResponse(url="/docs", status_code=307)


@app.get("/health")
def health():
    return {"ok": True, "service": "elou-avt-digital-twin"}

# ---------------------------------------------------------------------------
# Authentication & RBAC (см. auth/ — ролевая модель из Роли.txt)
# ---------------------------------------------------------------------------

_login_attempts: Dict[str, list[float]] = {}
_login_attempts_lock = threading.Lock()
_LOGIN_WINDOW_SECONDS = 60.0
_LOGIN_MAX_FAILURES = 5


def _login_key(request: Request, username: str) -> str:
    client = request.client.host if request.client else "unknown"
    return f"{client}:{username.strip().lower()}"


def _check_login_rate(key: str) -> None:
    now = time.monotonic()
    with _login_attempts_lock:
        recent = [stamp for stamp in _login_attempts.get(key, ())
                  if now - stamp < _LOGIN_WINDOW_SECONDS]
        if recent:
            _login_attempts[key] = recent
        else:
            _login_attempts.pop(key, None)
        if len(recent) >= _LOGIN_MAX_FAILURES:
            retry_after = max(1, int(_LOGIN_WINDOW_SECONDS - (now - recent[0])))
            raise HTTPException(
                status_code=429,
                detail="Слишком много попыток входа. Повторите позже.",
                headers={"Retry-After": str(retry_after)},
            )


def _record_login_result(key: str, success: bool) -> None:
    with _login_attempts_lock:
        if success:
            _login_attempts.pop(key, None)
        else:
            attempts = _login_attempts.setdefault(key, [])
            attempts.append(time.monotonic())
            # Prevent an unbounded dictionary if many synthetic usernames are tried.
            if len(_login_attempts) > 10_000:
                cutoff = time.monotonic() - _LOGIN_WINDOW_SECONDS
                stale = [item for item, stamps in _login_attempts.items()
                         if not stamps or stamps[-1] < cutoff]
                for item in stale:
                    _login_attempts.pop(item, None)
                while len(_login_attempts) > 10_000:
                    _login_attempts.pop(next(iter(_login_attempts)))

@app.post("/auth/login")
def auth_login(req: LoginRequest, request: Request):
    key = _login_key(request, req.username)
    _check_login_rate(key)
    resp = get_auth_service().authenticate(req.username, req.password)
    _record_login_result(key, resp is not None)
    if resp is None:
        raise HTTPException(status_code=401, detail="Неверный логин или пароль")
    return resp

@app.get("/auth/me", dependencies=[Depends(get_current_user)])
def auth_me(current_user=Depends(get_current_user)):
    return current_user.model_dump()

@app.get("/auth/roles", dependencies=[Depends(require_permission("manage_roles"))])
def auth_roles():
    return get_auth_service().list_roles()

@app.get("/auth/permissions", dependencies=[Depends(require_permission("manage_roles"))])
def auth_permissions():
    return get_auth_service().all_permissions()

@app.post("/auth/roles", dependencies=[Depends(require_permission("manage_roles"))])
def auth_create_role(req: RoleCreate):
    try:
        return get_auth_service().create_role(req)
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e))

@app.put("/auth/roles/{code}", dependencies=[Depends(require_permission("manage_roles"))])
def auth_update_role(code: str, req: RoleUpdate):
    try:
        return get_auth_service().update_role(code, req)
    except KeyError as e:
        raise HTTPException(status_code=404, detail=str(e))

@app.put("/auth/roles/{code}/permissions", dependencies=[Depends(require_permission("manage_roles"))])
def auth_set_role_permissions(code: str, req: RolePermissions):
    try:
        return get_auth_service().set_role_permissions(code, req.permission_codes)
    except KeyError as e:
        raise HTTPException(status_code=404, detail=str(e))

@app.delete("/auth/roles/{code}", dependencies=[Depends(require_permission("manage_roles"))])
def auth_delete_role(code: str):
    try:
        get_auth_service().delete_role(code)
    except KeyError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))
    return {"ok": True}

@app.get("/auth/users", dependencies=[Depends(require_permission("manage_users"))])
def auth_users():
    return get_auth_service().list_users()

@app.post("/auth/users", dependencies=[Depends(require_permission("manage_users"))])
def auth_create_user(req: UserCreate):
    return get_auth_service().create_user(req)

@app.post("/auth/users/{user_id}/roles", dependencies=[Depends(require_permission("manage_roles"))])
def auth_assign_roles(user_id: int, req: RoleAssign):
    return get_auth_service().set_user_roles(user_id, req.role_codes)

@app.post("/auth/users/{user_id}/deactivate", dependencies=[Depends(require_permission("manage_users"))])
def auth_deactivate(user_id: int):
    get_auth_service().set_user_active(user_id, False)
    return {"ok": True}

@app.get("/state", dependencies=[Depends(require_permission("view_scheme"))])
def state():
    return _snapshot_value("state")

@app.get("/simulation/speed", dependencies=[Depends(require_permission("view_scheme"))])
def get_simulation_speed():
    snap = _snapshot_state()
    if snap is not None:
        return {"speed": snap["speed"]}
    return {"speed": get_sim_speed()}

@app.post("/simulation/speed", dependencies=[Depends(require_permission("send_commands"))])
def set_simulation_speed(req: SimulationSpeedRequest):
    return {"speed": set_sim_speed(req.speed)}

@app.get("/alarms", dependencies=[Depends(require_permission("view_scheme"))])
def alarms():
    return _snapshot_value("alarms")

@app.get("/alarms/setpoints", dependencies=[Depends(require_permission("view_scheme"))])
def alarm_setpoints():
    """Alarm thresholds of every measured parameter, grouped by equipment node.

    Parameter names encode the equipment: '<node_id>_<quantity>' for per-node
    limits (e.g. 'elo_5_level') and bare names (e.g. 'feed_flow') for the
    plant-wide aggregates.
    """
    with lock:
        node_ids = {n.id for n in scheme_store.nodes}
        result = []
        for name, s in twin._engine.get_alarm_setpoints().items():
            node_id = next((nid for nid in node_ids if name.startswith(f"{nid}_")), None)
            result.append({"parameter": name, "node_id": node_id, **s})
        return {"setpoints": result}

@app.put("/alarms/setpoints/{parameter}", dependencies=[Depends(require_permission("manage_twin"))])
def update_alarm_setpoint(parameter: str, req: AlarmSetpointRequest):
    """Manually override the alarm thresholds of one parameter (persisted)."""
    with lock:
        try:
            updated = twin._engine.update_alarm_setpoint(
                parameter,
                low_low=req.low_low,
                low=req.low,
                high=req.high,
                high_high=req.high_high,
                unit=req.unit,
            )
        except KeyError:
            raise HTTPException(status_code=404, detail=f"Параметр '{parameter}' не найден")
        try:
            ensure_session_layer()
            session_store.save_alarm_setpoint(
                updated["parameter"],
                updated["low_low"],
                updated["low"],
                updated["high"],
                updated["high_high"],
                updated["unit"] or "",
            )
        except Exception:
            logger.exception("Failed to persist alarm setpoint '%s'", parameter)
        return {"ok": True, "setpoint": updated}

@app.delete("/alarms/setpoints", dependencies=[Depends(require_permission("manage_twin"))])
def reset_alarm_setpoints():
    """Discard all manual overrides and restore the scheme defaults."""
    with lock:
        twin._engine.reset_alarm_setpoints()
        try:
            ensure_session_layer()
            session_store.clear_alarm_setpoints()
        except Exception:
            logger.exception("Failed to clear persisted alarm setpoints")
        return {"ok": True}

@app.get("/events", dependencies=[Depends(require_permission("view_scheme"))])
def events():
    return _snapshot_value("events")

@app.get("/score", dependencies=[Depends(require_permission("view_scheme"))])
def score():
    return _snapshot_value("score")

@app.get("/controllers", dependencies=[Depends(require_permission("view_scheme"))])
def controllers():
    """Faceplate snapshot of every PID loop in the unified catalogue."""
    snap = _snapshot_state()
    if snap is not None:
        ctrls = snap["controllers"]
        return {"count": len(ctrls), "controllers": ctrls}
    with lock:
        ctrls = control_system.snapshot()
        return {"count": len(ctrls), "controllers": ctrls}

@app.get("/controllers/{tag}", dependencies=[Depends(require_permission("view_scheme"))])
def controller_detail(tag: str):
    """Faceplate snapshot of one control loop."""
    snap = _snapshot_state()
    if snap is not None:
        face = snap["controllers"].get(tag)
        if face is None:
            raise HTTPException(status_code=404, detail=f"Регулятор '{tag}' не найден")
        return face
    with lock:
        if tag not in control_system.controllers:
            raise HTTPException(status_code=404, detail=f"Регулятор '{tag}' не найден")
        return control_system.faceplate(tag)

def _request_actor(current_user: object, legacy_value: str = "demo") -> str:
    """Use the authenticated identity; keep direct Python calls compatible."""
    if isinstance(current_user, Principal):
        return current_user.username
    return legacy_value


@app.post("/command", dependencies=[Depends(require_permission("send_commands"))])
def command(
    req: CommandRequest,
    current_user: Principal = Depends(get_current_user),
):
    """Apply one operator command to a control loop."""
    actor = _request_actor(current_user, req.operator_id)
    with lock:
        cmd = Command(
            tag=req.tag,
            action=req.action,
            value=req.value,
            operator_id=actor,
            timestamp=twin._simulation_time,
            source="hmi",
        )
        try:
            ctrl = control_system.apply_command(cmd)
        except ValueError as e:
            raise HTTPException(status_code=422, detail=str(e))
        if session_recorder is not None and session_recorder.active:
            op_action = OperatorAction(
                timestamp=twin._simulation_time,
                operator_id=actor,
                equipment_id=req.tag,
                action_type=ActionType(req.action.value),
                old_value=getattr(ctrl, "sp", None),
                new_value=req.value if isinstance(req.value, (int, float)) else None,
                source="hmi",
            )
            session_recorder.record_action(op_action, node_type="controller")
        _refresh_snapshot()
        return {"ok": True, "controller": control_system.faceplate(ctrl.tag)}

def _resolve_scenario(scenario_id: str) -> Optional[Scenario]:
    """Сценарий для тренировочной сессии: авторский (LMS-{id}) или из реестра."""
    if scenario_id == "BASELINE":
        return _baseline_scenario()
    if scenario_id.startswith("LMS-"):
        try:
            sc = LmsContentStore().get_scenario(int(scenario_id.split("-", 1)[1]))
        except (TypeError, ValueError):
            sc = None
        return to_engine_scenario(sc) if sc is not None else None
    return SCENARIO_REGISTRY.get(scenario_id)


@app.post("/training/session", dependencies=[Depends(require_permission("start_training"))])
def start_training_session(
    req: StartSessionRequest,
    current_user: Principal = Depends(get_current_user),
):
    """Open a training session for a scenario (contract; scoring in Этап 7)."""
    global _training_session
    scenario_obj = _resolve_scenario(req.scenario_id)
    if scenario_obj is None:
        raise HTTPException(status_code=404, detail=f"Сценарий '{req.scenario_id}' не найден")
    with lock:
        ensure_session_layer()
        # If a previous session is still open, close it as aborted first.
        if session_recorder.active:
            session_recorder.abort(reason="superseded by a new session")
        requested_operator = req.operator_id.strip()
        if isinstance(current_user, Principal):
            # Instructors may launch a session for a selected operator. Regular
            # operators can only create a session under their own identity.
            can_assign = current_user.has_permission("monitor_operators")
            operator_id = requested_operator if can_assign and requested_operator not in {"", "demo"} else current_user.username
        else:
            operator_id = requested_operator or "demo"
        session_id = f"TR-{time.time_ns()}-{uuid.uuid4().hex[:8]}"
        session_recorder.begin(
            scenario_id=req.scenario_id,
            operator_id=operator_id,
            scheme_version=scheme_store.id,
            reference_actions=scenario_obj.reference_actions,
            sim_start=twin._simulation_time,
            session_id=session_id,
        )
        _training_session = TrainingSession(
            session_id=session_id,
            scenario_id=req.scenario_id,
            operator_id=operator_id,
        )
        _training_session.start(twin._simulation_time)
        return _training_session.model_dump()

@app.get("/training/session", dependencies=[Depends(get_current_user)])
def get_training_session():
    """Current training session, if any."""
    with lock:
        if _training_session is None:
            raise HTTPException(status_code=404, detail="Нет активной тренировочной сессии")
        return _training_session.model_dump()

@app.post("/training/session/finish", dependencies=[Depends(require_permission("start_training"))])
def finish_training_session():
    """Complete the current session and persist score + AI verdict."""
    global _training_session
    with lock:
        if _training_session is None or session_recorder is None or not session_recorder.active:
            raise HTTPException(status_code=404, detail="Нет активной тренировочной сессии")
        score_data = twin.get_score_data()
        score = float(score_data.get("performance_score", 0.0))
        session_recorder.end(
            sim_end=twin._simulation_time,
            score=score,
            qualification="",
            ai_verdict={"error_events": score_data.get("error_events", [])},
        )
        _training_session.finish(twin._simulation_time, score)
        return _training_session.model_dump()

@app.get("/training/sessions", dependencies=[Depends(require_permission("view_training_sessions"))])
def list_training_sessions():
    """Persisted training sessions (the AI training corpus)."""
    with lock:
        if session_store is None:
            return []
        return session_store.list_sessions()

@app.get("/training/sessions/{session_id}", dependencies=[Depends(require_permission("view_training_sessions"))])
def export_training_session(session_id: str):
    """Full persisted event log of one session (AI dataset row)."""
    with lock:
        if session_store is None:
            raise HTTPException(status_code=404, detail="Сессия не найдена")
        data = session_store.export_session(session_id)
        if data is None:
            raise HTTPException(status_code=404, detail=f"Сессия '{session_id}' не найдена")
        return data

@app.post("/input", dependencies=[Depends(require_permission("run_simulation"))])
def set_input(req: InputRequest):
    with lock:
        if req.flow_kg_s is not None:
            inputs["flow_kg_s"] = req.flow_kg_s
        if req.temperature_c is not None:
            inputs["temperature_c"] = req.temperature_c
        if req.pressure_bar is not None:
            inputs["pressure_bar"] = req.pressure_bar
        twin._engine.set_feed_override(inputs)
        # Keep the src_feed node params in sync so per-node editing and the
        # global feed override always agree.
        src = scheme_store.node("src_feed")
        if src is not None:
            for key, val in (("flow_kg_s", req.flow_kg_s),
                             ("temperature_c", req.temperature_c),
                             ("pressure_bar", req.pressure_bar)):
                if val is not None:
                    src.params[key] = val
        _refresh_snapshot()
        return _snapshot_state()

@app.post("/action", dependencies=[Depends(require_permission("send_commands"))])
def action(
    req: ActionRequest,
    current_user: Principal = Depends(get_current_user),
):
    actor = _request_actor(current_user, req.operator_id)
    with lock:
        node = scheme_store.node(req.equipment_id)
        value = req.value
        if (req.action_type == "SET_VALUE" and value is not None and node is not None
                and node.type in ("valve", "angle_valve") and value > 1.0):
            value = value / 100.0
        action = OperatorAction(
            timestamp=twin._simulation_time,
            operator_id=actor,
            equipment_id=req.equipment_id,
            action_type=req.action_type,
            old_value=_current_regulated_value(req.equipment_id),
            new_value=value,
            source="operator_panel",
        )
        if session_recorder is not None and session_recorder.active:
            action_id = session_recorder.record_action(
                action, node_type=_node_type_for(req.equipment_id))
            session_recorder.record_snapshot(
                twin.get_state(), reason="action", action_id=action_id)
        twin.apply_operator_action(action)
        # Advance immediately so the UI shows the result without waiting for the loop.
        if twin._status.value != "RUNNING":
            twin.start()
        _safe_step()
        _refresh_snapshot()
        return _snapshot_state()

@app.get("/equipment/spec/{node_id}", dependencies=[Depends(require_permission("view_scheme"))])
def equipment_spec(node_id: str):
    """Editable physical-property spec + current values for one node."""
    with lock:
        node = scheme_store.node(node_id)
        if node is None:
            raise HTTPException(status_code=404, detail=f"Node '{node_id}' not found")
        if node.type in NON_EDITABLE_TYPES or not spec_for(node.type):
            return {"node_id": node_id, "node_type": node.type, "editable": False, "params": []}
        eq = twin._engine._equipment.get(node_id)
        return {
            "node_id": node_id,
            "node_type": node.type,
            "editable": True,
            "params": editor_spec(node.type, eq, node),
        }

@app.post("/equipment/params", dependencies=[Depends(require_permission("manage_twin"))])
def update_equipment_params(req: EquipmentParamsRequest):
    """Apply physical-property corrections to a node and persist them."""
    with lock:
        node = scheme_store.node(req.equipment_id)
        if node is None:
            raise HTTPException(status_code=404, detail=f"Node '{req.equipment_id}' not found")
        if node.type in NON_EDITABLE_TYPES:
            raise HTTPException(status_code=422, detail=f"Тип '{node.type}' не имеет настраиваемых свойств")
        eq = twin._engine._equipment.get(req.equipment_id)
        updates: Dict[str, float] = {}
        for key, display_val in req.params.items():
            stored = coerce(key, float(display_val), node.type)
            if stored is None:
                raise HTTPException(status_code=422, detail=f"Параметр '{key}' не настраивается для типа '{node.type}'")
            updates[key] = stored
        if eq is not None:
            eq.update_params(updates)
        for key, val in updates.items():
            node.params[key] = val
        if req.equipment_id == "src_feed":
            for key, val in updates.items():
                if key in ("flow_kg_s", "temperature_c", "pressure_bar"):
                    inputs[key] = val
                    twin._engine.set_feed_override({key: val})
        _save_current_scheme()
        _safe_step()
        _refresh_snapshot()
        return _snapshot_state()

@app.post("/scenario/start", dependencies=[Depends(require_permission("run_simulation"))])
def start_scenario(req: ScenarioRequest):
    with lock:
        scenario_obj = _resolve_scenario(req.scenario_id)
        if scenario_obj is None:
            raise HTTPException(status_code=404, detail=f"Сценарий '{req.scenario_id}' не найден")
        try:
            twin.create_simulation()
            twin._engine.set_scheme(scheme_store)
            _restore_alarm_setpoints()
            twin.load_scenario_object(scenario_obj)
            twin._engine.set_feed_override(inputs)
            twin.start()
            for _ in range(30):
                _safe_step()
            _refresh_snapshot()
            return _snapshot_state()
        except ValueError as e:
            raise HTTPException(status_code=404, detail=str(e))

@app.post("/scenario/reset", dependencies=[Depends(require_permission("run_simulation"))])
def reset_scenario():
    with lock:
        twin.reset()
        twin._engine.set_feed_override(inputs)
        _refresh_snapshot()
        return _snapshot_state()

@app.post("/scenario/step", dependencies=[Depends(require_permission("run_simulation"))])
def step():
    with lock:
        if twin._status.value not in ("RUNNING", "IDLE"):
            twin.start()
        twin._engine.set_feed_override(inputs)
        _safe_step()
        _refresh_snapshot()
        return _snapshot_state()

@app.post("/failure/{equipment_id}", dependencies=[Depends(require_permission("manage_twin"))])
def failure(equipment_id: str):
    with lock:
        twin.inject_failure(equipment_id, "MECHANICAL_FAILURE")
        # Advance one step so the failure's effect is visible immediately.
        if twin._status.value != "RUNNING":
            twin.start()
        _safe_step()
        _refresh_snapshot()
        return _snapshot_state()


@app.get("/history", dependencies=[Depends(require_permission("view_scheme"))])
def history(limit: int = 600):
    """Return recent time series for trend charts."""
    data = _build_history()
    if limit > 0:
        data["times"] = data["times"][-limit:]
        for k in data["series"]:
            data["series"][k] = data["series"][k][-limit:]
    return data

@app.get("/history/node/{node_id}", dependencies=[Depends(require_permission("view_scheme"))])
def history_node(node_id: str, limit: int = 600):
    """Return recent time series of one equipment node (per-node params).

    The times axis is the same as the plant-wide history; series are keyed by
    the bare parameter name ('level_m', 'flow_kg_s', ...) so the inspector can
    render per-equipment graphs directly.
    """
    data = _build_history()
    prefix = f"{node_id}:"
    out_series = {k[len(prefix):]: v for k, v in data["series"].items() if k.startswith(prefix)}
    times = data["times"]
    if limit > 0:
        times = times[-limit:]
        for k in out_series:
            out_series[k] = out_series[k][-limit:]
    return {"times": times, "series": out_series}

@app.get("/scheme", dependencies=[Depends(require_permission("view_scheme"))])
def get_scheme():
    """Return the current P&ID scheme graph (nodes + edges)."""
    return _snapshot_value("scheme")

def _sync_runtime_params() -> None:
    """Mirror current operator slider values back into the scheme node params.

    Slider actions (SET_VALUE on valves / heaters / setpoints) are applied to
    the live equipment instances only; writing them back into
    ``scheme_store`` here makes ``_save_current_scheme`` persist them, so a
    saved scheme reopens with the same valve openings, fuel flows and level
    setpoints the operator left them at.
    """
    equip = twin._engine._equipment
    for node in scheme_store.nodes:
        eq = equip.get(node.id)
        if eq is None:
            continue
        try:
            if node.type in ("valve", "angle_valve"):
                node.params["initial_position"] = float(eq.target_position)
            elif node.type == "gate_valve":
                node.params["initial_open"] = 1.0 if eq.is_open else 0.0
            elif node.type == "heater":
                node.params["initial_fuel_flow"] = float(eq.target_fuel_flow)
            elif node.type in ("tank", "separator", "separator_s1k"):
                node.params["setpoint_level"] = float(eq.setpoint)
            elif node.type == "pump":
                node.params["initial_running"] = 1.0 if eq.state.running else 0.0
            elif node.type == "elou":
                node.params["initial_running"] = 1.0 if eq.state.running else 0.0
            elif node.type == "column":
                node.params["initial_reflux_ratio"] = float(eq.reflux_ratio)
        except Exception:
            logger.exception("Failed to sync runtime params for %s", node.id)


def _save_current_scheme() -> None:
    """Persist the current scheme to its own JSON file (not the default)."""
    _sync_runtime_params()
    save_scheme(scheme_store, _scheme_path(scheme_store.id))
    _remember_scheme(scheme_store.id)


@app.get("/schemes", dependencies=[Depends(require_permission("view_scheme"))])
def list_schemes():
    """List available P&ID scheme files (without the .json extension)."""
    return {"current": scheme_store.id,
            "schemes": [p.stem for p in sorted(SCHEME_DIR.glob("*.json"))]}


class SchemeLoadRequest(BaseModel):
    name: str


def _reconfigure(new_scheme: ProcessScheme) -> None:
    """Point the engine at a scheme, reload normal operation and warm it up
    so the returned state reflects the new P&ID layout immediately."""
    global scheme_store
    scheme_store = new_scheme
    _remember_scheme(new_scheme.id)
    twin.create_simulation()
    twin._engine.set_scheme(new_scheme)
    twin._engine.set_feed_override(inputs)
    _restore_alarm_setpoints()
    twin.load_scenario_object(_baseline_scenario())
    twin.start()
    for _ in range(5):
        _safe_step()


@app.post("/scheme/load", dependencies=[Depends(require_permission("manage_scheme"))])
def load_scheme_endpoint(req: SchemeLoadRequest):
    """Load a P&ID scheme by name and reconfigure the engine on it."""
    with lock:
        try:
            path = _scheme_path(req.name)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        if not path.exists():
            raise HTTPException(status_code=404, detail=f"Scheme '{req.name}' not found")
        try:
            new_scheme = load_scheme(path)
        except Exception as e:
            raise HTTPException(status_code=422, detail=f"Invalid scheme: {e}")
        # A number of legacy copies contain id="default" internally. The file
        # name is the authoritative identity; otherwise saving a loaded copy
        # could overwrite default.json.
        new_scheme.id = path.stem
        _reconfigure(new_scheme)
        _refresh_snapshot()
        return _snapshot_state()

class SchemeCreateRequest(BaseModel):
    name: str

@app.post("/scheme/new", dependencies=[Depends(require_permission("manage_scheme"))])
def create_scheme_endpoint(req: SchemeCreateRequest):
    """Create an empty P&ID scheme by name and switch the engine to it."""
    with lock:
        name = req.name.strip()
        try:
            path = _scheme_path(name)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        if path.exists():
            raise HTTPException(status_code=409, detail=f"Scheme '{name}' already exists")
        new_scheme = ProcessScheme(id=name, name=f"Схема «{name}»", nodes=[], edges=[])
        save_scheme(new_scheme, path)
        _reconfigure(new_scheme)
        _refresh_snapshot()
        return _snapshot_state()

class SchemeRequest(BaseModel):
    id: str = ""
    name: str = ""
    nodes: list = Field(default_factory=list)
    edges: list = Field(default_factory=list)

@app.post("/scheme", dependencies=[Depends(require_permission("manage_scheme"))])
def post_scheme(req: SchemeRequest):
    """Replace the P&ID scheme, persist it and reconfigure the engine.

    The scheme keeps its own id/name (from the loaded scheme), so a save does
    not silently rename it to "default" and overwrite default.json."""
    with lock:
        global scheme_store
        try:
            raw = migrate_scheme_data({
                "id": req.id or scheme_store.id,
                "name": req.name or req.id or scheme_store.name,
                "nodes": req.nodes,
                "edges": req.edges,
            })
            new_scheme = ProcessScheme.model_validate(raw)
        except Exception as e:
            raise HTTPException(status_code=422, detail=f"Invalid scheme: {e}")
        try:
            _scheme_path(new_scheme.id)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        scheme_store = new_scheme
        _save_current_scheme()
        _reconfigure(new_scheme)
        _refresh_snapshot()
        return _snapshot_state()

@app.get("/scheme/templates", dependencies=[Depends(require_permission("view_scheme"))])
def scheme_templates():
    """Return the palette of object types available to the scheme editor."""
    return {
        "types": [
            {"type": "source", "label": "Источник сырья", "category": "boundary"},
            {"type": "sink", "label": "Продукт / отбор", "category": "boundary"},
            {"type": "pump", "label": "Насос", "category": "equipment"},
            {"type": "valve", "label": "Регулирующий клапан", "category": "equipment"},
            {"type": "angle_valve", "label": "Угловой клапан", "category": "equipment"},
            {"type": "gate_valve", "label": "Задвижка", "category": "equipment"},
            {"type": "mixer", "label": "Смеситель", "category": "equipment"},
            {"type": "elou", "label": "ЭЛОУ (электродегидратор)", "category": "equipment"},
            {"type": "heat_exchanger", "label": "Теплообменник", "category": "equipment"},
            {"type": "heater", "label": "Печь", "category": "equipment"},
            {"type": "column", "label": "Колонна ректификации", "category": "equipment"},
            {"type": "separator", "label": "Сепаратор", "category": "equipment"},
            {"type": "separator_s1k", "label": "Сепаратор С-1К", "category": "equipment"},
        ]
    }

def _websocket_credential(websocket: WebSocket) -> tuple[str, Optional[str]]:
    """Read a bearer token from a WebSocket subprotocol, not from the URL.

    The query parameter remains a temporary compatibility fallback for older
    clients, but the current frontend uses ``auth.<token>`` in the handshake so
    credentials do not leak into access logs and browser history.
    """
    protocols = [
        item.strip()
        for item in websocket.headers.get("sec-websocket-protocol", "").split(",")
        if item.strip()
    ]
    token = next((item[5:] for item in protocols if item.startswith("auth.")), "")
    selected = "elou-avt" if "elou-avt" in protocols else None
    if not token:
        token = websocket.query_params.get("token", "")
    return token, selected


@app.websocket("/ws/simulation")
async def websocket_simulation(websocket: WebSocket):
    token, subprotocol = _websocket_credential(websocket)
    principal = authenticate_websocket(token)
    if principal is None:
        await websocket.close(code=4401)
        return
    if not principal.has_permission("view_scheme"):
        await websocket.close(code=4403)
        return
    await websocket.accept(subprotocol=subprotocol)
    pubsub = None
    try:
        payload = _snapshot_state()
        if payload is None:
            with lock:
                _refresh_snapshot()
                payload = _snapshot_state()
        if payload is not None:
            await websocket.send_json(payload)
        pubsub = await redis_bus.subscribe(SIM_SNAPSHOT_CHANNEL)
        async for message in pubsub.listen():
            if message is None or message.get("type") != "message":
                continue
            try:
                payload = json.loads(message["data"])
            except Exception:
                continue
            await websocket.send_json(payload)
    except WebSocketDisconnect:
        return
    except Exception:
        logger.exception("WebSocket connection error")
        try:
            await websocket.close()
        except Exception:
            pass
    finally:
        if pubsub is not None:
            try:
                await pubsub.unsubscribe(SIM_SNAPSHOT_CHANNEL)
                await pubsub.close()
            except Exception:
                pass


@app.websocket("/ws/chat")
async def websocket_chat(websocket: WebSocket):
    token, subprotocol = _websocket_credential(websocket)
    principal = authenticate_websocket(token)
    if principal is None:
        await websocket.close(code=4401)
        return
    if not (
        principal.has_permission("view_dashboard")
        or principal.has_permission("monitor_operators")
    ):
        await websocket.close(code=4403)
        return
    await websocket.accept(subprotocol=subprotocol)
    chat_register(websocket)
    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        return
    finally:
        chat_unregister(websocket)


def _sync_recorder(action_id: Optional[int] = None) -> None:
    """Persist the latest step context into the active training session."""
    if session_recorder is None or not session_recorder.active:
        return
    session_recorder.record_snapshot(twin.get_state(), reason="step")
    session_recorder.sync_alarms(twin._engine._alarm_system.get_alarm_history())
    session_recorder.sync_errors(twin._engine.get_events(), action_id=action_id)


def _safe_step() -> None:
    """Step the simulation, logging any exception instead of swallowing it."""
    try:
        twin._engine.set_feed_override(inputs)
        twin.step(1.0)
        _sync_recorder()
    except Exception:
        logger.error("Simulation step failed:\n%s", traceback.format_exc())


_simulation_stop = threading.Event()
_simulation_thread: Optional[threading.Thread] = None
_simulation_thread_lock = threading.Lock()

SIM_SNAPSHOT_CHANNEL = "sim:snapshot"
# Set once in `_lifespan` at startup so `simulation_loop` (a plain background
# thread, not an asyncio task) can hand work back to the main event loop via
# `asyncio.run_coroutine_threadsafe` -- mirrors how `lms/chat_hub.py` captures
# its own `_loop`.
_main_event_loop: Optional[asyncio.AbstractEventLoop] = None


def _publish_snapshot(payload: Dict[str, Any]) -> None:
    """Fire-and-forget publish of the simulation snapshot to Redis.

    Called from `simulation_loop`, which runs in a plain thread -- never
    `await` here; schedule the coroutine on the captured main loop instead.
    """
    loop = _main_event_loop
    if loop is None or loop.is_closed():
        return
    try:
        asyncio.run_coroutine_threadsafe(
            redis_bus.publish_json(SIM_SNAPSHOT_CHANNEL, payload), loop
        )
    except Exception:
        logger.warning("Failed to schedule sim snapshot publish", exc_info=True)


def simulation_loop(stop_event: threading.Event):
    """Advance the simulation by 1.0 s whole steps at a rate of speed * real time.

    Whole steps keep the physical timestep constant; the speed multiplier only
    changes how many steps wall-clock time buys. The accumulator never runs
    more than SIM_SPEED_MAX steps per tick, and if the steps are SLOWER than
    real time (heavy MESH columns), the accumulated lag is dropped instead of
    being paid back in a burst -- otherwise a slow step would queue up a burst
    of steps under the simulation lock and starve the HTTP layer for tens of
    seconds (e.g. scheme save requests hang).
    """
    acc = 0.0
    last = time.monotonic()
    while not stop_event.wait(0.05):
        now = time.monotonic()
        dt = now - last
        last = now
        if dt > 1.0:
            dt = 1.0
        acc += dt * get_sim_speed()
        if acc < 1.0:
            continue
        steps = int(acc)
        if steps > SIM_SPEED_MAX:
            steps = SIM_SPEED_MAX
        acc -= steps
        t0 = time.monotonic()
        with lock:
            if twin._status.value == "RUNNING":
                for _ in range(steps):
                    _safe_step()
            # Rebuild the read-path snapshot so polling endpoints / the
            # WebSocket serve the fresh state without blocking on this step.
            _refresh_snapshot()
            snapshot_payload = _snapshot_state()
        if snapshot_payload is not None:
            _publish_snapshot(snapshot_payload)
        elapsed = time.monotonic() - t0
        # Симуляция отстаёт от реального времени (шаг дольше 1 реальной
        # секунды): долг не копим, иначе следующий тик стартует пачку
        # тяжёлых шагов под lock и заблокирует HTTP-слой.
        if elapsed > 1.0:
            acc = 0.0

def _start_simulation_thread() -> None:
    global _simulation_thread
    with _simulation_thread_lock:
        if _simulation_thread is not None and _simulation_thread.is_alive():
            return
        _simulation_stop.clear()
        if twin._status.value != "RUNNING":
            twin.start()
        with lock:
            _refresh_snapshot()
        _simulation_thread = threading.Thread(
            target=simulation_loop,
            args=(_simulation_stop,),
            name="elou-avt-simulation",
            daemon=True,
        )
        _simulation_thread.start()


def _stop_simulation_thread() -> None:
    global _simulation_thread, session_store, session_recorder
    with _simulation_thread_lock:
        thread = _simulation_thread
        _simulation_stop.set()
    if thread is not None:
        thread.join(timeout=5.0)
    with _simulation_thread_lock:
        _simulation_thread = None
    with lock:
        twin.stop()
        if session_store is not None:
            session_store.close()
            session_store = None
            session_recorder = None
            lms_runtime.configure(twin, scheme_store, None, None, inputs)


def _preload_error_cause_model() -> None:
    """Pay sklearn/joblib import cost in the background, not on Finish click."""
    try:
        from ml.error_cause_inference import MODEL
        MODEL._load()
        logger.info("Error-cause model preloaded: %s", MODEL.name)
    except Exception:
        logger.exception("Error-cause model preload failed")


@asynccontextmanager
async def _lifespan(_app: FastAPI):
    global _main_event_loop
    _main_event_loop = asyncio.get_event_loop()
    _start_simulation_thread()
    threading.Thread(target=_preload_error_cause_model, name="ml-preload", daemon=True).start()
    chat_listener_task = asyncio.create_task(chat_redis_listener())
    try:
        yield
    finally:
        _stop_simulation_thread()
        chat_listener_task.cancel()
        try:
            await chat_listener_task
        except (asyncio.CancelledError, Exception):
            pass
        _main_event_loop = None


app.router.lifespan_context = _lifespan

if __name__ == "__main__":
    import uvicorn
    # Pass the app object (not an import string): the module is already loaded
    # as `__main__`, and re-importing "api_server" would create a second
    # DigitalTwin with its own simulation loop, doubling CPU load and GIL
    # contention (each loop runs a ~2 s hydraulic solve per step).
    uvicorn.run(app, host="127.0.0.1", port=8000, reload=False)
