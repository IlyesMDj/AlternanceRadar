"""Rend la racine du projet importable depuis les tests.

`core` et `collectors` sont des paquets à la racine, pas un paquet installé :
sans ça, `import core.models` échoue selon le répertoire d'où pytest est
lancé.
"""

import sys
from pathlib import Path

RACINE = Path(__file__).resolve().parent.parent
if str(RACINE) not in sys.path:
    sys.path.insert(0, str(RACINE))
