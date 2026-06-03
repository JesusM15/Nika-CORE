"""
database.py — Capa de persistencia de Nika OS
==============================================
Usa SQLAlchemy con motor async de SQLite (aiosqlite).

Esquema:
  - Mode        → grupos de aplicaciones (ej. "Modo Trabajo")
  - ModeApp     → apps dentro de un modo, asignadas a un dispositivo
  - Device      → laptops/PCs descubiertos vía MQTT
  - Setting     → pares clave-valor de configuración del sistema

La base de datos se crea automáticamente con init_db() al arrancar el servidor.
"""

import json
import logging
from datetime import datetime
from typing import AsyncGenerator

from sqlalchemy import (
    Column, Integer, String, DateTime, Text, ForeignKey,
    select, delete as sa_delete
)
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.orm import declarative_base, relationship, sessionmaker

logger = logging.getLogger("nika.db")

# ── Motor SQLite async ───────────────────────────────────────────────────────
# echo=False evita el log de todas las queries en producción
DATABASE_URL = "sqlite+aiosqlite:///./nika.db"

engine = create_async_engine(
    DATABASE_URL,
    echo=False,
    connect_args={"check_same_thread": False},  # Requerido para SQLite con threads
)

# Fábrica de sesiones async. expire_on_commit=False evita problemas con objetos
# que se acceden después del commit en contexto async.
AsyncSessionLocal = sessionmaker(
    bind=engine,
    class_=AsyncSession,
    expire_on_commit=False,
)

Base = declarative_base()


# ══════════════════════════════════════════════════════
#  MODELOS ORM
# ══════════════════════════════════════════════════════

class Mode(Base):
    """
    Un 'Modo' agrupa un conjunto de aplicaciones para lanzarlas
    juntas en dispositivos específicos con un solo comando.

    Ejemplo: Modo "Trabajo" → abre Word en laptop-1 y Spotify en laptop-2.
    """
    __tablename__ = "modes"

    id         = Column(Integer, primary_key=True, index=True, autoincrement=True)
    name       = Column(String(100), nullable=False, unique=True)
    icon       = Column(String(10),  default="🚀")           # Emoji para el card UI
    color      = Column(String(20),  default="#7c3aed")      # Color accent del card (hex)
    created_at = Column(DateTime,    default=datetime.utcnow)
    updated_at = Column(DateTime,    default=datetime.utcnow, onupdate=datetime.utcnow)

    # Relación 1→N: un modo tiene muchas apps
    # cascade="all, delete-orphan" → al borrar un modo, se borran sus apps automáticamente
    apps = relationship("ModeApp", back_populates="mode", cascade="all, delete-orphan", lazy="selectin")

    def to_dict(self) -> dict:
        return {
            "id":         self.id,
            "name":       self.name,
            "icon":       self.icon,
            "color":      self.color,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "apps":       [a.to_dict() for a in self.apps],
        }


class ModeApp(Base):
    """
    Aplicación individual dentro de un Modo.
    Cada app está asignada a un dispositivo específico (por hostname MQTT).
    """
    __tablename__ = "mode_apps"

    id        = Column(Integer, primary_key=True, index=True, autoincrement=True)
    mode_id   = Column(Integer, ForeignKey("modes.id", ondelete="CASCADE"), nullable=False)
    app_name  = Column(String(200), nullable=False)      # Nombre display: "Spotify"
    app_path  = Column(String(500), nullable=False)      # Ejecutable: "spotify" o ruta completa
    device_id = Column(String(100), nullable=False)      # Hostname del dispositivo: "laptop-gamer"

    mode = relationship("Mode", back_populates="apps")

    def to_dict(self) -> dict:
        return {
            "id":        self.id,
            "mode_id":   self.mode_id,
            "app_name":  self.app_name,
            "app_path":  self.app_path,
            "device_id": self.device_id,
        }


class Device(Base):
    """
    Dispositivo remoto (laptop/PC) descubierto vía MQTT.
    El campo apps_json almacena la lista de aplicaciones disponibles
    tal como la reportó el nika_client al responder el ping de descubrimiento.
    """
    __tablename__ = "devices"

    id           = Column(Integer,     primary_key=True, index=True, autoincrement=True)
    hostname     = Column(String(100), nullable=False, unique=True)
    display_name = Column(String(100))
    status       = Column(String(20),  default="offline")     # online | offline
    last_seen    = Column(DateTime)
    apps_json    = Column(Text,        default="[]")           # JSON: [{"name": "spotify", "path": "..."}]
    ip_address   = Column(String(50))
    platform     = Column(String(50))                          # windows | linux | darwin

    def get_apps(self) -> list:
        """Deserializa la lista de apps desde JSON."""
        try:
            return json.loads(self.apps_json or "[]")
        except json.JSONDecodeError:
            return []

    def to_dict(self) -> dict:
        return {
            "id":           self.id,
            "hostname":     self.hostname,
            "display_name": self.display_name or self.hostname,
            "status":       self.status,
            "last_seen":    self.last_seen.isoformat() if self.last_seen else None,
            "apps":         self.get_apps(),
            "ip_address":   self.ip_address,
            "platform":     self.platform,
        }


class Setting(Base):
    """
    Configuración de Nika almacenada como pares clave-valor.
    Todos los valores se guardan como strings; la conversión de tipo
    es responsabilidad del consumidor.
    """
    __tablename__ = "settings"

    key   = Column(String(100), primary_key=True)
    value = Column(Text, nullable=False)

    def to_dict(self) -> dict:
        return {"key": self.key, "value": self.value}


# ══════════════════════════════════════════════════════
#  INICIALIZACIÓN
# ══════════════════════════════════════════════════════

async def init_db():
    """
    Crea todas las tablas (si no existen) e inserta los valores de
    configuración por defecto. Es idempotente: seguro de llamar múltiples veces.
    """
    # Crear tablas usando el motor async
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    logger.info("[DB] Tablas creadas/verificadas correctamente.")

    # Insertar configuración por defecto solo si no existe
    defaults = {
        "nika_name":   "Nika",
        "theme":       "dark",
        "tts_enabled": "true",
        "tts_rate":    "145",
        "wake_words":  json.dumps(["nika", "nica", "nyka", "nicas"]),
    }

    async with AsyncSessionLocal() as session:
        for key, value in defaults.items():
            existing = await session.get(Setting, key)
            if not existing:
                session.add(Setting(key=key, value=value))
                logger.debug(f"[DB] Configuración por defecto insertada: {key}={value}")
        await session.commit()

    logger.info("[DB] Configuración por defecto verificada.")


# ── Dependency para FastAPI (inyección de sesión) ────────────────────────────

async def get_db() -> AsyncGenerator[AsyncSession, None]:
    """
    Dependency de FastAPI. Provee una sesión async por request y
    garantiza que se cierre al terminar, incluso en caso de error.

    Uso en un endpoint:
        async def my_route(session = Depends(get_db)): ...
    """
    async with AsyncSessionLocal() as session:
        try:
            yield session
        except Exception:
            await session.rollback()
            raise
        finally:
            await session.close()
