"""
nika_client/app_discovery.py — Descubrimiento y Base de Datos Local de Apps
=============================================================================
Módulo que escanea Windows en busca de todas las aplicaciones instaladas,
las guarda en una base de datos SQLite local y provee búsqueda fuzzy por nombre.

Fuentes de descubrimiento (en orden):
  1. Registro de Windows — App Paths (HKLM + HKCU)
  2. Registro de Windows — Uninstall (HKLM + HKCU, 32 y 64 bits)
  3. Menú de Inicio — Accesos directos .lnk resueltos via PowerShell
  4. Lista semilla curada (apps populares con múltiples candidatos de ruta)

Búsqueda de apps:
  · Coincidencia exacta → alias → fuzzy (difflib) → parcial
  · Devuelve la app con mayor similitud o None si no hay match

Base de datos (SQLite — apps.db junto al script):
  Tabla apps:    canonical, name, exe_path, exe_name, category, source
  Tabla aliases: alias → canonical
"""

import os
import re
import sys
import glob
import shutil
import sqlite3
import logging
import difflib
import unicodedata
import subprocess
import winreg
from pathlib import Path
from typing import Optional

logger = logging.getLogger("nika.discovery")

# Ruta de la base de datos (misma carpeta que este script)
DB_PATH = Path(__file__).parent / "apps.db"

# ── Umbral de similitud mínima para aceptar un match ─────────────────────────
# Se usa el MÁXIMO de 5 estrategias de similitud, por lo que 0.65 es conservador.
SIM_THRESHOLD = 0.65

# ── Tabla de normalización fonética (STT español → texto limpio) ───────────────
# El motor de voz puede transcribir letras/palabras de forma fonética.
# Estas reglas convierten esas transcripciones al nombre real de la app.
# Se aplican ANTES de cualquier comparación de similitud.
PHONETIC_RULES: list[tuple[re.Pattern, str]] = [
    # Letras del abecedario en español
    (re.compile(r"\bequis\b",         re.I), "x"),
    (re.compile(r"\bdoble\s*[uw]\b",  re.I), "w"),
    (re.compile(r"\bi\s*griega\b",    re.I), "y"),
    (re.compile(r"\berre\b",          re.I), "r"),
    (re.compile(r"\bache\b",          re.I), "h"),
    (re.compile(r"\buve\b",           re.I), "v"),
    # Prefijos comunes mal transcritos
    (re.compile(r"\bex\s+box\b",      re.I), "xbox"),
    (re.compile(r"\bequis\s+box\b",   re.I), "xbox"),
    (re.compile(r"\bvirtual\s+box\b", re.I), "virtualbox"),
    (re.compile(r"\bopen\s+office\b", re.I), "openoffice"),
    (re.compile(r"\blibre\s+office\b",re.I), "libreoffice"),
    (re.compile(r"\bfire\s+fox\b",    re.I), "firefox"),
    (re.compile(r"\byou\s+tube\b",    re.I), "youtube"),
    (re.compile(r"\bwhat\s+s\s+app\b",re.I), "whatsapp"),
    (re.compile(r"\bwhat\s+app\b",    re.I), "whatsapp"),
    (re.compile(r"\bpower\s+shell\b", re.I), "powershell"),
    (re.compile(r"\bpower\s+point\b", re.I), "powerpoint"),
    (re.compile(r"\bword\s+pad\b",    re.I), "wordpad"),
    (re.compile(r"\bnote\s+pad\b",    re.I), "notepad"),
]

# ══════════════════════════════════════════════════════════════════════════════
#  ALIAS CURADOS — Nombres alternativos que puede decir el usuario
#  Se agregan a la BD y permiten buscar en español o inglés
# ══════════════════════════════════════════════════════════════════════════════
KNOWN_ALIASES: dict[str, str] = {
    # Navegadores
    "google chrome":          "chrome",
    "google":                 "chrome",
    "navegador":              "chrome",
    "buscador":               "chrome",
    "mozilla":                "firefox",
    "mozilla firefox":        "firefox",
    "microsoft edge":         "edge",
    "edge browser":           "edge",
    # Office
    "microsoft word":         "word",
    "word":                   "word",
    "microsoft excel":        "excel",
    "excel":                  "excel",
    "hoja de calculo":        "excel",
    "hojas de calculo":       "excel",
    "microsoft powerpoint":   "powerpoint",
    "powerpoint":             "powerpoint",
    "presentaciones":         "powerpoint",
    "diapositivas":           "powerpoint",
    # Editores de código
    "vs code":                "vscode",
    "visual studio code":     "vscode",
    "vscode":                 "vscode",
    "code":                   "vscode",
    "antigravity":            "vscode",
    "antigravity ide":        "vscode",
    "editor":                 "vscode",
    "notepad++":              "notepadpp",
    "notepad plus":           "notepadpp",
    # Utilidades del sistema
    "bloc de notas":          "notepad",
    "notepad":                "notepad",
    "calculadora":            "calculator",
    "calculator":             "calculator",
    "paint":                  "paint",
    "pintura":                "paint",
    "explorador":             "explorer",
    "explorador de archivos": "explorer",
    "archivos":               "explorer",
    "mis archivos":           "explorer",
    "administrador de tareas": "taskmgr",
    "task manager":           "taskmgr",
    "terminal":               "cmd",
    "consola":                "cmd",
    "simbolo del sistema":    "cmd",
    "powershell":             "powershell",
    # Gaming / Social
    "steam":                  "steam",
    "juegos":                 "steam",
    "discord":                "discord",
    "musica":                 "spotify",
    "música":                 "spotify",
    "spotify":                "spotify",
    "teams":                  "msteams",
    "microsoft teams":        "msteams",
    "reuniones":              "msteams",
    "whats":                  "whatsapp",
    "whatsapp":               "whatsapp",
    "whatssapp":              "whatsapp",
    # Multimedia
    "vlc":                    "vlc",
    "reproductor":            "vlc",
    "videos":                 "vlc",
}

# ══════════════════════════════════════════════════════════════════════════════
#  LISTA SEMILLA — Apps populares con candidatos de ruta y nombre de proceso
#  Formato: (canonical, display_name, exe_name, category, [rutas_candidato])
# ══════════════════════════════════════════════════════════════════════════════
SEED_APPS: list[tuple] = [
    # (canonical, nombre_display, exe_name, categoria, [rutas...])

    # ── Navegadores ────────────────────────────────────────────────────────────
    ("chrome",     "Google Chrome",    "chrome.exe",    "browser", [
        r"%PROGRAMFILES%\Google\Chrome\Application\chrome.exe",
        r"%PROGRAMFILES(X86)%\Google\Chrome\Application\chrome.exe",
        r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe",
    ]),
    ("firefox",    "Mozilla Firefox",  "firefox.exe",   "browser", [
        r"%PROGRAMFILES%\Mozilla Firefox\firefox.exe",
        r"%PROGRAMFILES(X86)%\Mozilla Firefox\firefox.exe",
    ]),
    ("edge",       "Microsoft Edge",   "msedge.exe",    "browser", [
        r"%PROGRAMFILES(X86)%\Microsoft\Edge\Application\msedge.exe",
        r"%PROGRAMFILES%\Microsoft\Edge\Application\msedge.exe",
    ]),

    # ── Office ─────────────────────────────────────────────────────────────────
    ("word",       "Microsoft Word",       "WINWORD.EXE",   "productivity", [
        r"%PROGRAMFILES%\Microsoft Office\root\Office16\WINWORD.EXE",
        r"%PROGRAMFILES(X86)%\Microsoft Office\root\Office16\WINWORD.EXE",
        r"%PROGRAMFILES%\Microsoft Office\Office16\WINWORD.EXE",
        r"%PROGRAMFILES(X86)%\Microsoft Office\Office16\WINWORD.EXE",
    ]),
    ("excel",      "Microsoft Excel",      "EXCEL.EXE",     "productivity", [
        r"%PROGRAMFILES%\Microsoft Office\root\Office16\EXCEL.EXE",
        r"%PROGRAMFILES(X86)%\Microsoft Office\root\Office16\EXCEL.EXE",
        r"%PROGRAMFILES%\Microsoft Office\Office16\EXCEL.EXE",
    ]),
    ("powerpoint", "Microsoft PowerPoint", "POWERPNT.EXE",  "productivity", [
        r"%PROGRAMFILES%\Microsoft Office\root\Office16\POWERPNT.EXE",
        r"%PROGRAMFILES(X86)%\Microsoft Office\root\Office16\POWERPNT.EXE",
        r"%PROGRAMFILES%\Microsoft Office\Office16\POWERPNT.EXE",
    ]),
    ("msteams",    "Microsoft Teams",      "Teams.exe",     "productivity", [
        r"%LOCALAPPDATA%\Microsoft\Teams\current\Teams.exe",
        r"%PROGRAMFILES(X86)%\Microsoft\Teams\current\Teams.exe",
        r"%PROGRAMFILES%\Microsoft\Teams\current\Teams.exe",
    ]),

    # ── Editores ───────────────────────────────────────────────────────────────
    ("vscode",     "Visual Studio Code",   "Code.exe",      "development", [
        r"%LOCALAPPDATA%\Programs\Microsoft VS Code\Code.exe",
        r"%PROGRAMFILES%\Microsoft VS Code\Code.exe",
    ]),
    ("notepadpp",  "Notepad++",            "notepad++.exe", "development", [
        r"%PROGRAMFILES%\Notepad++\notepad++.exe",
        r"%PROGRAMFILES(X86)%\Notepad++\notepad++.exe",
    ]),

    # ── Sistema ────────────────────────────────────────────────────────────────
    ("notepad",    "Bloc de Notas",        "notepad.exe",   "system",  ["notepad.exe"]),
    ("calculator", "Calculadora",          "CalculatorApp.exe", "system",  ["calc.exe"]),
    ("paint",      "Paint",                "mspaint.exe",   "system",  ["mspaint.exe"]),
    ("explorer",   "Explorador de Archivos","explorer.exe", "system",  ["explorer.exe"]),
    ("taskmgr",    "Administrador de Tareas","Taskmgr.exe", "system",  ["taskmgr.exe"]),
    ("cmd",        "Símbolo del Sistema",  "cmd.exe",       "system",  ["cmd.exe"]),
    ("powershell", "PowerShell",           "powershell.exe","system",  ["powershell.exe"]),

    # ── Multimedia ─────────────────────────────────────────────────────────────
    ("spotify",    "Spotify",              "Spotify.exe",   "multimedia", [
        r"%APPDATA%\Spotify\Spotify.exe",
        r"%LOCALAPPDATA%\Microsoft\WindowsApps\Spotify.exe",
    ]),
    ("vlc",        "VLC Media Player",     "vlc.exe",       "multimedia", [
        r"%PROGRAMFILES%\VideoLAN\VLC\vlc.exe",
        r"%PROGRAMFILES(X86)%\VideoLAN\VLC\vlc.exe",
    ]),

    # ── Gaming / Social ────────────────────────────────────────────────────────
    ("steam",      "Steam",                "steam.exe",     "gaming", [
        r"%PROGRAMFILES(X86)%\Steam\steam.exe",
        r"%PROGRAMFILES%\Steam\steam.exe",
    ]),
    ("discord",    "Discord",              "Discord.exe",   "social", [
        r"%LOCALAPPDATA%\Discord\Update.exe",   # launcher oficial
        r"%LOCALAPPDATA%\Discord\app-*\Discord.exe",  # binario directo (glob)
    ]),
    ("whatsapp",   "WhatsApp",             "WhatsApp.exe",  "social", [
        r"%LOCALAPPDATA%\WhatsApp\WhatsApp.exe",
        r"%APPDATA%\WhatsApp\WhatsApp.exe",
        r"%LOCALAPPDATA%\Microsoft\WindowsApps\WhatsApp.exe",
    ]),
]

# ── Launcher especiales que necesitan argumentos extra ────────────────────────
SPECIAL_LAUNCHERS: dict[str, list[str]] = {
    "discord": ["--processStart", "Discord.exe"],
    "msteams": ["--processStart", "Teams.exe"],
}


# ══════════════════════════════════════════════════════════════════════════════
#  FUNCIONES DE NORMALIZACIÓN Y SIMILITUD
# ══════════════════════════════════════════════════════════════════════════════

def _remove_accents(text: str) -> str:
    """Elimina tildes y diacríticos: 'música' → 'musica'."""
    return "".join(
        c for c in unicodedata.normalize("NFD", text)
        if unicodedata.category(c) != "Mn"
    )


def _phonetic_normalize(text: str) -> str:
    """
    Aplica reglas de normalización fonética para corregir transcripciones
    del motor de voz en español.

    Ejemplos:
      "equis box"   → "xbox"
      "virtual box" → "virtualbox"
      "doble u"     → "w"
      "i griega"    → "y"
    """
    result = text.strip()
    for pattern, replacement in PHONETIC_RULES:
        result = pattern.sub(replacement, result)
    return result.strip()


def _similarity_score(query: str, candidate: str) -> float:
    """
    Calcula la similitud entre dos cadenas usando 5 estrategias combinadas.
    Retorna el MÁXIMO de todas las estrategias (valor entre 0.0 y 1.0).

    Estrategias:
      A. Ratio directo (difflib SequenceMatcher)
         → Bueno para nombres casi idénticos con pequeños errores tipográficos.

      B. Ratio normalizado (sin tildes, minúsculas)
         → Maneja diferencias de acentuación: "música" vs "musica".

      C. Ratio concatenado (sin espacios)
         → Maneja separación de palabras: "note pad" vs "notepad".

      D. Jaccard de tokens (intersección de palabras / unión)
         → Bueno cuando el orden de palabras difiere:
           "code visual studio" vs "visual studio code" → 1.0

      E. Token-set ratio (difflib del mejor subconjunto de tokens)
         → Robusto ante palabras extra: "abre el spotify" vs "spotify" → alto.
    """
    if not query or not candidate:
        return 0.0

    q = query.strip().lower()
    c = candidate.strip().lower()

    # Cortocircuito: match exacto = 1.0
    if q == c:
        return 1.0

    scores: list[float] = []

    # A. Ratio directo
    scores.append(difflib.SequenceMatcher(None, q, c).ratio())

    # B. Sin tildes
    q_clean = _remove_accents(q)
    c_clean = _remove_accents(c)
    scores.append(difflib.SequenceMatcher(None, q_clean, c_clean).ratio())

    # C. Sin espacios (concatenado)
    q_concat = q.replace(" ", "")
    c_concat = c.replace(" ", "")
    scores.append(difflib.SequenceMatcher(None, q_concat, c_concat).ratio())

    # D. Jaccard de tokens
    q_tokens = set(q.split())
    c_tokens = set(c.split())
    union = q_tokens | c_tokens
    if union:
        scores.append(len(q_tokens & c_tokens) / len(union))

    # E. Token-set ratio: comparar la intersección ordenada vs cada parte
    #    Inspirado en fuzzywuzzy token_set_ratio, pero sin dependencias externas
    intersection = sorted(q_tokens & c_tokens)
    remainder_q  = sorted(q_tokens - c_tokens)
    remainder_c  = sorted(c_tokens - q_tokens)
    base     = " ".join(intersection)
    full_q   = (base + " " + " ".join(remainder_q)).strip()
    full_c   = (base + " " + " ".join(remainder_c)).strip()
    if base:
        scores.append(max(
            difflib.SequenceMatcher(None, base, full_q).ratio(),
            difflib.SequenceMatcher(None, base, full_c).ratio(),
            difflib.SequenceMatcher(None, full_q, full_c).ratio(),
        ))

    return max(scores)


# ══════════════════════════════════════════════════════════════════════════════
#  BASE DE DATOS SQLITE
# ══════════════════════════════════════════════════════════════════════════════

class AppDatabase:
    """
    Base de datos SQLite local que almacena todas las aplicaciones
    instaladas en el equipo junto con sus rutas y aliases de nombre.
    """

    def __init__(self, db_path: Path = DB_PATH):
        self.db_path = db_path
        # check_same_thread=False es seguro aquí porque usamos un Lock externo
        self.conn = sqlite3.connect(str(db_path), check_same_thread=False)
        self.conn.row_factory = sqlite3.Row   # Filas accesibles por nombre de columna
        self._init_schema()
        logger.info(f"[DB] Base de datos en: {db_path}")

    def _init_schema(self):
        """Crea las tablas si no existen."""
        self.conn.executescript("""
            CREATE TABLE IF NOT EXISTS apps (
                canonical    TEXT PRIMARY KEY,
                name         TEXT NOT NULL,
                exe_path     TEXT NOT NULL,
                exe_name     TEXT,
                category     TEXT DEFAULT 'general',
                source       TEXT DEFAULT 'seed',
                launch_args  TEXT DEFAULT '',
                available    INTEGER DEFAULT 1
            );

            CREATE TABLE IF NOT EXISTS aliases (
                alias     TEXT PRIMARY KEY,
                canonical TEXT NOT NULL REFERENCES apps(canonical) ON DELETE CASCADE
            );

            CREATE INDEX IF NOT EXISTS idx_aliases_canonical ON aliases(canonical);
        """)
        self.conn.commit()

    # ── Escritura ─────────────────────────────────────────────────────────────

    def upsert_app(
        self,
        canonical: str,
        name: str,
        exe_path: str,
        exe_name: str,
        category: str = "general",
        source: str = "discovery",
        launch_args: str = "",
    ):
        """Inserta o actualiza una app. Si ya existe y la ruta nueva es mejor, la actualiza."""
        self.conn.execute("""
            INSERT INTO apps (canonical, name, exe_path, exe_name, category, source, launch_args, available)
            VALUES (?, ?, ?, ?, ?, ?, ?, 1)
            ON CONFLICT(canonical) DO UPDATE SET
                name        = excluded.name,
                exe_path    = excluded.exe_path,
                exe_name    = excluded.exe_name,
                category    = excluded.category,
                source      = excluded.source,
                launch_args = excluded.launch_args,
                available   = 1
        """, (canonical, name, exe_path, exe_name, category, source, launch_args))
        self.conn.commit()

    def add_alias(self, alias: str, canonical: str):
        """Registra un alias si el canonical existe en la BD."""
        try:
            self.conn.execute(
                "INSERT OR IGNORE INTO aliases (alias, canonical) VALUES (?, ?)",
                (alias.lower().strip(), canonical)
            )
            self.conn.commit()
        except Exception:
            pass   # Ignorar si el canonical no existe todavía

    def mark_unavailable(self, canonical: str):
        """Marca una app como no disponible (ejecutable no encontrado)."""
        self.conn.execute(
            "UPDATE apps SET available = 0 WHERE canonical = ?", (canonical,)
        )
        self.conn.commit()

    # ── Lectura ───────────────────────────────────────────────────────────────

    def get_all(self) -> list[dict]:
        """Retorna todas las apps disponibles como lista de dicts."""
        rows = self.conn.execute(
            "SELECT * FROM apps WHERE available = 1 ORDER BY name"
        ).fetchall()
        return [dict(r) for r in rows]

    def get_app(self, canonical: str) -> Optional[dict]:
        """Busca una app por su nombre canónico exacto."""
        row = self.conn.execute(
            "SELECT * FROM apps WHERE canonical = ? AND available = 1", (canonical,)
        ).fetchone()
        return dict(row) if row else None

    def resolve(self, name: str) -> Optional[dict]:
        """
        Resuelve un nombre de app a su registro en la BD usando un pipeline de
        normalización + scoring multi-estrategia.

        Pipeline:
          1. Normalización fonética ("equis box" → "xbox", "virtual box" → "virtualbox")
          2. Coincidencia exacta (canonical o alias)
          3. Scoring multi-estrategia sobre todos los términos conocidos:
               a. difflib ratio directo
               b. ratio sobre texto normalizado (sin tildes, minúsculas)
               c. ratio sobre texto sin espacios (concatenado)
               d. Jaccard de tokens (intersección/unión de palabras)
               e. Token-set ratio (mejor subconjunto de tokens)
             → Se toma el MÁXIMO de las 5 métricas como score final
          4. Si el mejor candidato supera SIM_THRESHOLD, se retorna su app
          5. Búsqueda parcial como último recurso (contains)

        Retorna el dict de la app o None si no hay match suficientemente bueno.
        """
        raw_query = name.strip()

        # ── Paso 1: Normalización fonética ────────────────────────────────────
        query = _phonetic_normalize(raw_query).lower().strip()
        if query != raw_query.lower():
            logger.info(f"[DB] Normalización fonética: '{raw_query}' → '{query}'")

        # ── Paso 2a: Canonical exacto ─────────────────────────────────────────
        row = self.conn.execute(
            "SELECT * FROM apps WHERE canonical = ? AND available = 1", (query,)
        ).fetchone()
        if row:
            return dict(row)

        # ── Paso 2b: Alias exacto ─────────────────────────────────────────────
        row = self.conn.execute("""
            SELECT a.* FROM apps a
            JOIN aliases al ON a.canonical = al.canonical
            WHERE al.alias = ? AND a.available = 1
        """, (query,)).fetchone()
        if row:
            return dict(row)

        # ── Paso 3: Scoring multi-estrategia sobre todos los términos ─────────
        # Construir mapa: term → canonical (aliases + canonicals + nombres)
        all_terms = self.conn.execute("""
            SELECT alias AS term, canonical FROM aliases
            UNION
            SELECT canonical AS term, canonical FROM apps WHERE available = 1
            UNION
            SELECT LOWER(name) AS term, canonical FROM apps WHERE available = 1
        """).fetchall()

        terms_map = {r["term"]: r["canonical"] for r in all_terms}

        # Calcular score de similitud para cada término y quedarse con el mejor
        best_score     = 0.0
        best_canonical = None
        best_via       = None

        for term, canonical in terms_map.items():
            score = _similarity_score(query, term)
            if score > best_score:
                best_score     = score
                best_canonical = canonical
                best_via       = term

        if best_score >= SIM_THRESHOLD and best_canonical:
            row = self.conn.execute(
                "SELECT * FROM apps WHERE canonical = ? AND available = 1", (best_canonical,)
            ).fetchone()
            if row:
                logger.info(
                    f"[DB] Smart match: '{query}' → '{best_canonical}' "
                    f"(via '{best_via}', score={best_score:.2f})"
                )
                return dict(row)

        # ── Paso 4: Búsqueda parcial (último recurso) ─────────────────────────
        row = self.conn.execute("""
            SELECT * FROM apps
            WHERE available = 1 AND (
                canonical LIKE ? OR LOWER(name) LIKE ?
            )
            LIMIT 1
        """, (f"%{query}%", f"%{query}%")).fetchone()
        if row:
            logger.info(f"[DB] Match parcial: '{query}' → '{row['canonical']}'")
            return dict(row)

        logger.warning(f"[DB] Sin match para '{raw_query}' (normalizado: '{query}', mejor score: {best_score:.2f})")
        return None

    def count(self) -> int:
        """Retorna el número de apps disponibles en la BD."""
        return self.conn.execute("SELECT COUNT(*) FROM apps WHERE available = 1").fetchone()[0]


# ══════════════════════════════════════════════════════════════════════════════
#  DESCUBRIMIENTO DE APPS
# ══════════════════════════════════════════════════════════════════════════════

def discover_apps(db: AppDatabase) -> int:
    """
    Escanea Windows en busca de aplicaciones instaladas y las guarda en la BD.
    Retorna el número total de apps encontradas.

    Fuentes de descubrimiento:
      1. Lista semilla curada (SEED_APPS)
      2. Registro — App Paths (HKLM + HKCU)
      3. Registro — Uninstall (HKLM + HKCU, 32 y 64 bits)
      4. Menú de Inicio — accesos directos .lnk via PowerShell
    """
    logger.info("[Discovery] Iniciando descubrimiento de aplicaciones...")

    _seed_known_apps(db)
    _discover_registry_app_paths(db)
    _discover_registry_uninstall(db)
    _discover_start_menu(db)
    _populate_aliases(db)

    total = db.count()
    logger.info(f"[Discovery] ✓ Descubrimiento completo. Apps disponibles: {total}")
    return total


def _resolve_candidate_path(raw: str) -> Optional[str]:
    """
    Expande variables de entorno y verifica si el ejecutable existe.
    Soporta globs (ej. 'app-*') para Discord y similares.
    Retorna la ruta absoluta o None si no se encuentra.
    """
    expanded = os.path.expandvars(raw)

    # Caso glob: Discord app-X.X.X
    if "*" in expanded:
        matches = sorted(glob.glob(expanded))
        return matches[-1] if matches else None

    # Ruta simple sin separador → buscar en PATH
    if os.sep not in expanded and "%" not in raw:
        return shutil.which(expanded)

    # Ruta absoluta
    exe = Path(expanded.split()[0])
    return str(exe) if exe.exists() else None


def _seed_known_apps(db: AppDatabase):
    """Agrega los apps de la lista semilla curada verificando que existan."""
    seeded = 0
    for canonical, name, exe_name, category, candidates in SEED_APPS:
        launch_args = ""
        # Argumentos extra para launchers especiales
        if canonical in SPECIAL_LAUNCHERS:
            launch_args = " ".join(SPECIAL_LAUNCHERS[canonical])

        for raw in candidates:
            resolved = _resolve_candidate_path(raw)
            if resolved:
                db.upsert_app(canonical, name, resolved, exe_name, category, "seed", launch_args)
                seeded += 1
                break
        # Si no se encontró en candidatos, NO se agrega (evita rutas fantasma)

    logger.info(f"[Discovery] Semilla: {seeded} apps verificadas")


def _discover_registry_app_paths(db: AppDatabase):
    """
    Escanea HKLM/HKCU → SOFTWARE\\Microsoft\\Windows\\CurrentVersion\\App Paths.
    Cada subclave es el nombre del .exe y su valor por defecto es la ruta completa.
    """
    found = 0
    reg_path = r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths"

    for hive_name, hive in [("HKLM", winreg.HKEY_LOCAL_MACHINE),
                             ("HKCU", winreg.HKEY_CURRENT_USER)]:
        try:
            root = winreg.OpenKey(hive, reg_path)
        except FileNotFoundError:
            continue

        idx = 0
        while True:
            try:
                subkey_name = winreg.EnumKey(root, idx)
                idx += 1
            except OSError:
                break

            try:
                with winreg.OpenKey(root, subkey_name) as sk:
                    exe_path, _ = winreg.QueryValueEx(sk, "")
                    if not exe_path or not Path(exe_path).exists():
                        continue

                    exe_file = Path(exe_path)
                    canonical = exe_file.stem.lower().replace(" ", "_")
                    name      = exe_file.stem

                    # No sobreescribir si ya está en la BD con ruta verificada
                    if db.get_app(canonical):
                        continue

                    db.upsert_app(
                        canonical  = canonical,
                        name       = name,
                        exe_path   = str(exe_path),
                        exe_name   = exe_file.name,
                        category   = "general",
                        source     = f"registry_app_paths_{hive_name}",
                    )
                    found += 1
            except (FileNotFoundError, OSError):
                continue

        winreg.CloseKey(root)

    logger.info(f"[Discovery] Registro App Paths: {found} apps nuevas")


def _discover_registry_uninstall(db: AppDatabase):
    """
    Escanea las claves de Uninstall del registro para encontrar el InstallLocation
    y el DisplayName de aplicaciones instaladas con instalador.
    """
    found    = 0
    reg_path = r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"

    hives_and_flags = [
        (winreg.HKEY_LOCAL_MACHINE, winreg.KEY_READ | winreg.KEY_WOW64_64KEY, "HKLM_64"),
        (winreg.HKEY_LOCAL_MACHINE, winreg.KEY_READ | winreg.KEY_WOW64_32KEY, "HKLM_32"),
        (winreg.HKEY_CURRENT_USER,  winreg.KEY_READ, "HKCU"),
    ]

    for hive, flags, hive_label in hives_and_flags:
        try:
            root = winreg.OpenKey(hive, reg_path, 0, flags)
        except FileNotFoundError:
            continue

        idx = 0
        while True:
            try:
                subkey_name = winreg.EnumKey(root, idx)
                idx += 1
            except OSError:
                break

            try:
                with winreg.OpenKey(root, subkey_name, 0, flags) as sk:
                    def _get(key_name):
                        try:
                            v, _ = winreg.QueryValueEx(sk, key_name)
                            return v
                        except FileNotFoundError:
                            return ""

                    display_name  = _get("DisplayName")
                    install_loc   = _get("InstallLocation")
                    display_icon  = _get("DisplayIcon")
                    system_comp   = _get("SystemComponent")

                    # Filtrar entradas sin nombre o componentes del sistema
                    if not display_name or system_comp == 1:
                        continue

                    # Intentar obtener el ejecutable
                    exe_path = None

                    # Opción 1: DisplayIcon suele ser "ruta.exe,0"
                    if display_icon:
                        icon_exe = display_icon.split(",")[0].strip().strip('"')
                        if icon_exe.lower().endswith(".exe") and Path(icon_exe).exists():
                            exe_path = icon_exe

                    # Opción 2: Buscar en InstallLocation
                    if not exe_path and install_loc and Path(install_loc).is_dir():
                        # Buscar exe con el mismo nombre que la app
                        safe_name = re.sub(r"[^a-zA-Z0-9]", "", display_name)
                        for candidate_name in [safe_name + ".exe", display_name + ".exe"]:
                            candidate = Path(install_loc) / candidate_name
                            if candidate.exists():
                                exe_path = str(candidate)
                                break

                    if not exe_path:
                        continue

                    canonical = Path(exe_path).stem.lower().replace(" ", "_")

                    # No sobreescribir si ya existe en BD con fuente más confiable
                    existing = db.get_app(canonical)
                    if existing and existing["source"] in ("seed", "registry_app_paths_HKLM"):
                        continue

                    db.upsert_app(
                        canonical = canonical,
                        name      = display_name,
                        exe_path  = exe_path,
                        exe_name  = Path(exe_path).name,
                        category  = "general",
                        source    = f"uninstall_{hive_label}",
                    )
                    found += 1

            except (FileNotFoundError, OSError):
                continue

        winreg.CloseKey(root)

    logger.info(f"[Discovery] Registro Uninstall: {found} apps nuevas")


def _discover_start_menu(db: AppDatabase):
    """
    Resuelve los accesos directos .lnk del Menú de Inicio usando PowerShell.
    Esto captura apps que no están en el registro de App Paths.
    """
    # Carpetas del Menú de Inicio (usuario + sistema)
    start_menu_dirs = [
        os.path.expandvars(r"%APPDATA%\Microsoft\Windows\Start Menu\Programs"),
        os.path.expandvars(r"%ProgramData%\Microsoft\Windows\Start Menu\Programs"),
    ]

    # Script PowerShell que resuelve cada .lnk y devuelve "nombre|ruta_target"
    ps_script = r"""
$shell = New-Object -COM WScript.Shell
$dirs = @($args)
foreach ($dir in $dirs) {
    if (-not (Test-Path $dir)) { continue }
    Get-ChildItem -Path $dir -Recurse -Filter "*.lnk" -ErrorAction SilentlyContinue | ForEach-Object {
        try {
            $sc = $shell.CreateShortcut($_.FullName)
            $target = $sc.TargetPath
            if ($target -and $target.ToLower().EndsWith('.exe') -and (Test-Path $target)) {
                Write-Output "$($_.BaseName)|$target"
            }
        } catch {}
    }
}
""".strip()

    try:
        result = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", ps_script]
            + start_menu_dirs,
            capture_output=True, text=True, timeout=20
        )
        lines = [l.strip() for l in result.stdout.splitlines() if "|" in l]
    except Exception as e:
        logger.warning(f"[Discovery] PowerShell Start Menu falló: {e}")
        lines = []

    found = 0
    for line in lines:
        parts = line.split("|", 1)
        if len(parts) != 2:
            continue
        shortcut_name, exe_path = parts[0].strip(), parts[1].strip()
        if not Path(exe_path).exists():
            continue

        canonical = Path(exe_path).stem.lower().replace(" ", "_")
        # No sobreescribir apps más confiables
        if db.get_app(canonical):
            continue

        db.upsert_app(
            canonical = canonical,
            name      = shortcut_name,
            exe_path  = exe_path,
            exe_name  = Path(exe_path).name,
            category  = "general",
            source    = "start_menu",
        )
        found += 1

    logger.info(f"[Discovery] Menú de Inicio: {found} apps nuevas")


def _populate_aliases(db: AppDatabase):
    """Agrega todos los aliases conocidos de KNOWN_ALIASES a la BD."""
    added = 0
    for alias, canonical in KNOWN_ALIASES.items():
        # Solo agregar el alias si el canonical existe en la BD
        if db.get_app(canonical):
            db.add_alias(alias, canonical)
            added += 1

    logger.info(f"[Discovery] {added} aliases registrados en BD")


# ══════════════════════════════════════════════════════════════════════════════
#  FUNCIÓN PÚBLICA DE APERTURA
# ══════════════════════════════════════════════════════════════════════════════

def launch_app(app: dict) -> bool:
    """
    Lanza un app usando los datos almacenados en la BD.
    Maneja casos especiales como Discord y Teams que usan launchers con flags.

    Args:
        app: Dict con campos 'exe_path', 'exe_name', 'canonical', 'launch_args'

    Returns:
        True si se lanzó correctamente, False si hubo un error.
    """
    exe_path    = app.get("exe_path", "")
    canonical   = app.get("canonical", "")
    launch_args = (app.get("launch_args") or "").strip()
    category    = app.get("category", "")

    if not exe_path:
        return False

    try:
        if launch_args:
            # Launcher especial (Discord, Teams): pasar argumentos extra
            cmd = [exe_path] + launch_args.split()
            subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

        elif canonical in ("notepad", "calculator", "paint", "explorer",
                           "taskmgr", "cmd", "powershell"):
            # Comandos del sistema: shell=True para que Windows los resuelva
            subprocess.Popen(exe_path, shell=True,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        else:
            # App normal con ruta absoluta
            exe = Path(exe_path)
            subprocess.Popen(
                [str(exe)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                cwd=str(exe.parent),
            )
        return True

    except FileNotFoundError:
        logger.error(f"[Discovery] Ejecutable no encontrado: {exe_path}")
    except PermissionError:
        logger.error(f"[Discovery] Sin permisos: {exe_path}")
    except Exception as e:
        logger.error(f"[Discovery] Error lanzando '{canonical}': {e}")
    return False
