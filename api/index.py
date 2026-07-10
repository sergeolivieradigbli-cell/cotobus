"""CotoBus — Backend adapté à Vercel (serverless).

Différences avec la version serveur classique :
  * PAS de tâche de fond : les positions des bus, montées passagers et
    alertes maintenance sont CALCULÉES à partir de l'heure courante
    (fonctions déterministes du temps) → aucun processus permanent requis,
    et tous les visiteurs voient exactement le même réseau.
  * Le réseau (lignes/arrêts/flotte) est constant → défini en dur ici,
    zéro lecture DB.
  * Seuls les BILLETS et VALIDATIONS sont persistés :
      - DATABASE_URL défini (Neon Postgres gratuit)  -> persistance réelle ✅
      - sinon SQLite dans /tmp -> OK en local, éphémère sur Vercel ⚠

Toutes les routes sont préfixées /api (rewrites Vercel).
En local (`uvicorn api.index:app --port 8000`), le frontend statique
est servi automatiquement depuis la racine du projet.
"""
import base64
import io
import math
import os
import secrets
import time
from datetime import datetime, timedelta

import qrcode
from fastapi import Depends, FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
from sqlalchemy import (Column, DateTime, Integer, String, create_engine,
                        func)
from sqlalchemy.orm import Session, declarative_base, sessionmaker

# ============================================================ BASE DE DONNÉES
raw_url = os.getenv("DATABASE_URL", "sqlite:////tmp/cotobus.db")
# Neon fournit postgres:// ; SQLAlchemy 2 + psycopg veut postgresql+psycopg://
if raw_url.startswith("postgres://"):
    raw_url = raw_url.replace("postgres://", "postgresql+psycopg://", 1)
elif raw_url.startswith("postgresql://"):
    raw_url = raw_url.replace("postgresql://", "postgresql+psycopg://", 1)

connect_args = {"check_same_thread": False} if raw_url.startswith("sqlite") else {}
engine = create_engine(raw_url, connect_args=connect_args, pool_pre_ping=True)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)
Base = declarative_base()


class Ticket(Base):
    __tablename__ = "tickets"
    id = Column(Integer, primary_key=True, autoincrement=True)
    code = Column(String(40), unique=True, index=True, nullable=False)
    line_id = Column(String(2), nullable=False)
    from_stop = Column(String(60), nullable=False)
    to_stop = Column(String(60), nullable=False)
    fare_fcfa = Column(Integer, nullable=False)
    payment_method = Column(String(20), nullable=False)
    payment_ref = Column(String(30), nullable=False)
    status = Column(String(12), default="PAID")      # PAID/VALIDATED/EXPIRED
    created_at = Column(DateTime, default=datetime.utcnow)
    expires_at = Column(DateTime, nullable=False)


class Validation(Base):
    __tablename__ = "validations"
    id = Column(Integer, primary_key=True, autoincrement=True)
    ticket_code = Column(String(40), index=True, nullable=False)
    bus_id = Column(Integer, nullable=False)
    validated_at = Column(DateTime, default=datetime.utcnow)


Base.metadata.create_all(bind=engine)


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


# ============================================================ RÉSEAU (constant)
LINES = {
    "A": {"name": "Calavi ↔ Centre administratif", "color": "#12b072",
          "fare_fcfa": 150, "stops": [
              ("Calavi Carrefour",     6.4489, 2.3556),
              ("Godomey Togoudo",      6.3922, 2.3453),
              ("Fidjrossè",            6.3567, 2.3735),
              ("Cadjehoun",            6.3629, 2.3912),
              ("Étoile Rouge",         6.3703, 2.4184),
              ("Centre administratif", 6.3654, 2.4283)]},
    "B": {"name": "Akpakpa ↔ Zone portuaire", "color": "#f2b705",
          "fare_fcfa": 150, "stops": [
              ("Akpakpa PK6",    6.3639, 2.4787),
              ("Sodjatinmè",     6.3671, 2.4551),
              ("Étoile Rouge",   6.3703, 2.4184),
              ("Dantokpa",       6.3660, 2.4327),
              ("Ganhi",          6.3535, 2.4297),
              ("Zone portuaire", 6.3489, 2.4396)]},
    "C": {"name": "Godomey ↔ Ganhi", "color": "#3f8fe0",
          "fare_fcfa": 150, "stops": [
              ("Godomey Pont", 6.3838, 2.3282),
              ("Womey",        6.3766, 2.3390),
              ("Fidjrossè",    6.3567, 2.3735),
              ("Cadjehoun",    6.3629, 2.3912),
              ("Camp Guézo",   6.3585, 2.4210),
              ("Ganhi",        6.3535, 2.4297)]},
}

# (id, plaque, ligne, déphasage 0-1, taux de montées/minute)
FLEET = [
    (1, "CB-001-RB", "A", 0.00, 0.16),
    (2, "CB-002-RB", "A", 0.50, 0.12),
    (3, "CB-003-RB", "B", 0.15, 0.30),
    (4, "CB-004-RB", "B", 0.65, 0.22),
    (5, "CB-005-RB", "C", 0.30, 0.10),
    (6, "CB-006-RB", "C", 0.80, 0.06),
]
ROUND_TRIP_S = 480          # aller-retour ~8 min (rythme démo)
TICKET_VALIDITY_HOURS = 2
CO2_KG_PER_TRIP = 0.32
FUEL_L_PER_TRIP = 0.133


def _minutes_since_midnight() -> float:
    now = datetime.utcnow()
    return now.hour * 60 + now.minute + now.second / 60


def _bus_progress(offset: float):
    """Onde triangulaire 0→1→0 : position et sens, fonction pure du temps."""
    phase = (time.time() / ROUND_TRIP_S + offset) % 1.0
    if phase < 0.5:
        return phase * 2, 1
    return (1 - phase) * 2, -1


def _interpolate(stops, progress: float):
    n = len(stops)
    if progress <= 0:
        return stops[0][1], stops[0][2]
    if progress >= 1:
        return stops[-1][1], stops[-1][2]
    seg = progress * (n - 1)
    i, frac = int(seg), seg - int(seg)
    (_, la, lo), (_, lb, lob) = stops[i], stops[min(i + 1, n - 1)]
    return la + (lb - la) * frac, lo + (lob - lo) * frac


def _bus_snapshot():
    """État complet de la flotte à l'instant t (déterministe)."""
    mins = _minutes_since_midnight()
    out = []
    for bid, plate, line_id, offset, rate in FLEET:
        prog, direction = _bus_progress(offset)
        lat, lon = _interpolate(LINES[line_id]["stops"], prog)
        out.append({
            "id": bid, "plate": plate, "line_id": line_id,
            "progress": round(prog, 4), "direction": direction,
            "lat": round(lat, 6), "lon": round(lon, 6),
            "passenger_count": int(mins * rate),
        })
    return out


def _current_alerts():
    """Alertes maintenance « télémétrie » déterministes (fenêtres temporelles)."""
    mins = _minutes_since_midnight()
    alerts = []
    temp4 = 92 + 14 * math.sin(2 * math.pi * mins / 90)       # pic > 104 °C cyclique
    if temp4 > 104:
        alerts.append({"id": 1, "bus_id": 4, "bus_plate": "CB-004-RB",
                       "severity": "CRITICAL",
                       "title": "Bus CB-004-RB — température moteur anormale",
                       "detail": f"Tendance haussière détectée ({temp4:.0f} °C). "
                                 "Intervention à planifier avant la prochaine rotation.",
                       "created_at": datetime.utcnow()})
    wear2 = 55 + (mins % 600) / 600 * 40                       # 55→95 % sur 10 h
    if wear2 > 80:
        alerts.append({"id": 2, "bus_id": 2, "bus_plate": "CB-002-RB",
                       "severity": "WARNING",
                       "title": "Bus CB-002-RB — usure plaquettes avant",
                       "detail": f"Usure estimée à {wear2:.0f} %. "
                                 "Remplacement recommandé sous 10 jours.",
                       "created_at": datetime.utcnow()})
    return alerts


# ============================================================ SCHÉMAS
class TicketCreate(BaseModel):
    line_id: str = Field(..., examples=["A"])
    from_stop: str
    to_stop: str
    payment_method: str = Field(..., pattern="^(MTN_MOMO|MOOV|CELTIIS)$")
    phone: str = Field(..., min_length=8, max_length=15)


class ValidateRequest(BaseModel):
    code: str
    bus_id: int


# ============================================================ APP
app = FastAPI(title="CotoBus API (Vercel)", version="1.0.0")
app.add_middleware(CORSMiddleware, allow_origins=["*"],
                   allow_methods=["*"], allow_headers=["*"])


@app.get("/api/health")
def health():
    return {"status": "ok", "service": "cotobus-api",
            "db": "postgres" if "postgresql" in raw_url else "sqlite(/tmp)"}


# ---------- Réseau ----------
@app.get("/api/network/lines")
def list_lines():
    return [{"id": lid, "name": L["name"], "color": L["color"],
             "fare_fcfa": L["fare_fcfa"],
             "stops": [{"id": i, "name": n, "position": i, "lat": la, "lon": lo}
                       for i, (n, la, lo) in enumerate(L["stops"])]}
            for lid, L in LINES.items()]


@app.get("/api/network/buses")
def list_buses():
    return _bus_snapshot()


@app.get("/api/network/stops/{stop_name}/eta")
def stop_eta(stop_name: str):
    LINE_DURATION_MIN = ROUND_TRIP_S / 2 / 60
    etas = []
    for lid, L in LINES.items():
        names = [s[0] for s in L["stops"]]
        if stop_name not in names:
            continue
        stop_prog = names.index(stop_name) / max(len(names) - 1, 1)
        gaps = []
        for bid, plate, bl, offset, _ in FLEET:
            if bl != lid:
                continue
            prog, direction = _bus_progress(offset)
            if direction == 1:
                gap = stop_prog - prog
                if gap < 0:
                    gap = (1 - prog) + (1 - stop_prog)
            else:
                gap = prog - stop_prog
                if gap < 0:
                    gap = prog + stop_prog
            gaps.append(gap)
        gaps.sort()
        first = max(1, round(gaps[0] * LINE_DURATION_MIN)) if gaps else 15
        second = (max(first + 2, round(gaps[1] * LINE_DURATION_MIN))
                  if len(gaps) > 1 else first + 8)
        etas.append({"line_id": lid, "line_name": L["name"],
                     "color": L["color"], "next_minutes": first,
                     "following_minutes": second})
    if not etas:
        raise HTTPException(404, f"Arrêt inconnu : {stop_name}")
    return {"stop_name": stop_name, "etas": etas}


# ---------- Billetterie ----------
def _qr_b64(payload: str) -> str:
    img = qrcode.make(payload)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return base64.b64encode(buf.getvalue()).decode()


def _ticket_out(t: Ticket, with_qr=False):
    d = {"code": t.code, "line_id": t.line_id, "from_stop": t.from_stop,
         "to_stop": t.to_stop, "fare_fcfa": t.fare_fcfa,
         "payment_method": t.payment_method, "payment_ref": t.payment_ref,
         "status": t.status, "created_at": t.created_at,
         "expires_at": t.expires_at, "qr_png_base64": None}
    if with_qr:
        d["qr_png_base64"] = _qr_b64(t.code)
    return d


@app.post("/api/tickets", status_code=201)
def buy_ticket(payload: TicketCreate, db: Session = Depends(get_db)):
    line = LINES.get(payload.line_id)
    if not line:
        raise HTTPException(404, f"Ligne inconnue : {payload.line_id}")
    names = {s[0] for s in line["stops"]}
    for s in (payload.from_stop, payload.to_stop):
        if s not in names:
            raise HTTPException(422, f"L'arrêt « {s} » n'est pas sur la ligne {payload.line_id}")
    if payload.from_stop == payload.to_stop:
        raise HTTPException(422, "Départ et arrivée identiques")

    ref = f"{payload.payment_method[:3]}-{secrets.token_hex(4).upper()}"
    t = Ticket(code=f"CB-{datetime.utcnow():%Y%m%d}-{secrets.token_hex(5).upper()}",
               line_id=payload.line_id, from_stop=payload.from_stop,
               to_stop=payload.to_stop, fare_fcfa=line["fare_fcfa"],
               payment_method=payload.payment_method, payment_ref=ref,
               status="PAID",
               expires_at=datetime.utcnow() + timedelta(hours=TICKET_VALIDITY_HOURS))
    db.add(t)
    db.commit()
    db.refresh(t)
    return _ticket_out(t, with_qr=True)


@app.get("/api/tickets/recent")
def recent_tickets(limit: int = 8, db: Session = Depends(get_db)):
    rows = (db.query(Ticket).order_by(Ticket.created_at.desc())
              .limit(min(limit, 50)).all())
    return [_ticket_out(t) for t in rows]


@app.get("/api/tickets/validations/recent")
def recent_validations(limit: int = 10, db: Session = Depends(get_db)):
    plates = {bid: plate for bid, plate, *_ in FLEET}
    rows = (db.query(Validation, Ticket)
              .join(Ticket, Ticket.code == Validation.ticket_code)
              .order_by(Validation.validated_at.desc())
              .limit(min(limit, 50)).all())
    return [{"code": t.code, "line_id": t.line_id, "from_stop": t.from_stop,
             "to_stop": t.to_stop, "bus_plate": plates.get(v.bus_id, "?"),
             "validated_at": v.validated_at} for v, t in rows]


@app.get("/api/tickets/{code}")
def get_ticket(code: str, db: Session = Depends(get_db)):
    t = db.query(Ticket).filter(Ticket.code == code).first()
    if not t:
        raise HTTPException(404, "Billet introuvable")
    return _ticket_out(t, with_qr=True)


@app.post("/api/tickets/validate")
def validate_ticket(payload: ValidateRequest, db: Session = Depends(get_db)):
    t = db.query(Ticket).filter(Ticket.code == payload.code).first()
    if not t:
        return {"ok": False, "message": "Billet inconnu — accès refusé."}
    if t.status == "VALIDATED":
        return {"ok": False, "message": "Billet déjà utilisé — accès refusé."}
    if datetime.utcnow() > t.expires_at:
        t.status = "EXPIRED"
        db.commit()
        return {"ok": False, "message": "Billet expiré — accès refusé."}
    bus = next((b for b in FLEET if b[0] == payload.bus_id), None)
    if not bus or bus[2] != t.line_id:
        return {"ok": False, "message": "Ce billet ne correspond pas à cette ligne."}
    t.status = "VALIDATED"
    db.add(Validation(ticket_code=t.code, bus_id=payload.bus_id))
    db.commit()
    return {"ok": True, "message": "Bon voyage !", "ticket": _ticket_out(t)}


# ---------- Dashboard ----------
@app.get("/api/dashboard")
def get_dashboard(db: Session = Depends(get_db)):
    today = datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0)

    revenue = (db.query(func.coalesce(func.sum(Ticket.fare_fcfa), 0))
                 .filter(Ticket.created_at >= today).scalar())
    by_method = dict(
        db.query(Ticket.payment_method,
                 func.coalesce(func.sum(Ticket.fare_fcfa), 0))
          .filter(Ticket.created_at >= today)
          .group_by(Ticket.payment_method).all())
    validations_today = (db.query(func.count(Validation.id))
                           .filter(Validation.validated_at >= today).scalar())

    buses = _bus_snapshot()
    sensor = sum(b["passenger_count"] for b in buses)
    gap = sensor - int(validations_today)
    gap_pct = round(100 * gap / sensor, 1) if sensor else 0.0

    hourly_rows = (db.query(func.extract("hour", Validation.validated_at),
                            func.count(Validation.id))
                     .filter(Validation.validated_at >= today)
                     .group_by(func.extract("hour", Validation.validated_at))
                     .all())
    hourly = [{"hour": int(h), "passengers": int(c)} for h, c in hourly_rows]

    line_loads = []
    for lid, L in LINES.items():
        boarded = sum(b["passenger_count"] for b in buses if b["line_id"] == lid)
        n_bus = sum(1 for b in buses if b["line_id"] == lid)
        load = round(min(100.0, 100 * boarded / max(n_bus * 180, 1)), 1)
        reco = None
        if load > 85:
            reco = f"+1 navette recommandée sur la ligne {lid} en heure de pointe"
        elif load < 40:
            reco = f"Fréquence réductible sur la ligne {lid} hors pointe"
        line_loads.append({"line_id": lid, "name": L["name"],
                           "color": L["color"], "load_pct": load,
                           "recommendation": reco})

    return {"revenue_today_fcfa": int(revenue),
            "revenue_by_method": {k: int(v) for k, v in by_method.items()},
            "validations_today": int(validations_today),
            "fraud": {"validations_today": int(validations_today),
                      "sensor_boardings_today": sensor,
                      "gap": gap, "gap_pct": gap_pct},
            "co2_saved_kg": round(sensor * CO2_KG_PER_TRIP, 1),
            "fuel_saved_l": round(sensor * FUEL_L_PER_TRIP, 1),
            "hourly": hourly, "line_loads": line_loads,
            "alerts": _current_alerts()}


# ---------- Frontend statique en développement local uniquement ----------
if not os.getenv("VERCEL"):
    from fastapi.staticfiles import StaticFiles
    ROOT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "public")
    app.mount("/", StaticFiles(directory=ROOT, html=True), name="static")
