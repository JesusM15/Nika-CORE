import sys
sys.path.insert(0, 'nika_client')
from app_discovery import _phonetic_normalize, _similarity_score

casos = [
    ('equis box',      'xbox'),
    ('ex box',         'xbox'),
    ('virtual box',    'virtualbox'),
    ('fire fox',       'firefox'),
    ('what s app',     'whatsapp'),
    ('what app',       'whatsapp'),
    ('power point',    'powerpoint'),
    ('power shell',    'powershell'),
    ('note pad',       'notepad'),
    ('google chrome',  'chrome'),
    ('spotify',        'spotify'),
    ('musica',         'spotify'),
    ('visual studio code', 'vscode'),
    ('bloc de notas',  'notepad'),
    ('antigravity ide','vscode'),
]

print('=' * 65)
print('TEST DE SIMILITUD — umbral SIM_THRESHOLD = 0.65')
print('=' * 65)

ok_count = 0
for query, expected in casos:
    norm  = _phonetic_normalize(query)
    score = _similarity_score(norm.lower(), expected)
    ok    = 'OK' if score >= 0.65 else 'FAIL'
    if ok == 'OK':
        ok_count += 1
    norm_str = f' (norm: "{norm}")' if norm.lower() != query.lower() else ''
    print(f'  [{ok}] "{query}"{norm_str} => "{expected}"  score={score:.2f}')

print(f'\nResultado: {ok_count}/{len(casos)} casos superan el umbral')
