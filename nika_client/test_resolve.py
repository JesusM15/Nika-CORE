import sys
sys.path.insert(0, 'nika_client')
import logging
logging.disable(logging.CRITICAL)  # silenciar logs durante el test
from app_discovery import AppDatabase

db = AppDatabase()

casos_db = [
    "musica",
    "antigravity ide",
    "equis box",
    "virtual box",
    "ex box",
    "bloc de notas",
    "visual studio code",
    "what app",
    "fire fox",
    "explorador de archivos",
    "note pad",
    "power point",
]

print("=" * 55)
print("TEST PIPELINE COMPLETO (BD.resolve)")
print("=" * 55)
ok = 0
for q in casos_db:
    r = db.resolve(q)
    if r:
        ok += 1
        print(f"  [OK]   \"{q}\" => {r['name']} ({r['canonical']})")
    else:
        print(f"  [FAIL] \"{q}\" => No encontrado")

print(f"\nResultado: {ok}/{len(casos_db)} resueltos correctamente")
