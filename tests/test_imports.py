"""Chaque nom importé quelque part existe-t-il vraiment ?

Ce fichier existe à cause d'une vraie casse : en déplaçant `extraire_emails`
vers `core/contacts.py`, une suppression trop large a emporté avec elle
`lire_post`, `normalize_accents` et `est_offre_recruteur`. Rien ne l'a
signalé — ces fonctions sont importées À L'INTÉRIEUR des commandes, pour ne
pas charger Playwright à chaque `main.py stats`. `python main.py posts --help`
répondait donc parfaitement, et la commande aurait planté au premier usage
réel, c'est-à-dire le jour où on en a besoin.

Le test relit les `from X import a, b, c` du projet — y compris ceux nichés
dans un corps de fonction — et vérifie que chaque nom se résout. C'est le
filet que `--help` ne peut pas tendre.
"""

import ast
import importlib
from pathlib import Path

import pytest

RACINE = Path(__file__).resolve().parent.parent

# Modules du projet, ceux dont on sait résoudre le nom.
PREFIXES = ("core", "collectors", "report", "cv", "postuler", "serveur", "main")


def _imports_internes(fichier: Path):
    """(module, nom) pour chaque import interne, imbriqué compris."""
    arbre = ast.parse(fichier.read_text(encoding="utf-8"))
    for noeud in ast.walk(arbre):
        if not isinstance(noeud, ast.ImportFrom) or not noeud.module:
            continue
        module = noeud.module
        if noeud.level:                       # « from .http import … »
            module = f"{fichier.parent.name}.{module}"
        if not module.startswith(PREFIXES):
            continue
        for alias in noeud.names:
            if alias.name != "*":
                yield module, alias.name


def _fichiers():
    for motif in ("*.py", "core/*.py", "collectors/*.py"):
        for f in sorted(RACINE.glob(motif)):
            if f.name != "__init__.py":
                yield f


CAS = [(f.relative_to(RACINE).as_posix(), m, n)
       for f in _fichiers() for m, n in _imports_internes(f)]


def test_il_y_a_bien_des_imports_a_verifier():
    """Garde-fou du garde-fou : si la collecte casse, le test doit crier au
    lieu de passer sur zéro cas."""
    assert len(CAS) > 40


@pytest.mark.parametrize("fichier, module, nom", CAS,
                         ids=[f"{f}:{m}.{n}" for f, m, n in CAS])
def test_chaque_nom_importe_existe(fichier, module, nom):
    mod = importlib.import_module(module)
    assert hasattr(mod, nom), f"{fichier} importe {nom} de {module}, absent"
