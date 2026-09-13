"""Répartition du budget de fiches détaillées.

Une offre stockée sans description n'est scorée que sur son titre, et le
manque se voit : -32 de score moyen contre +8. Le rattrapage n'est donc pas
un confort, c'est ce qui empêche le digest d'enterrer des offres muettes
comme si elles étaient mauvaises.
"""

import pytest

from main import budgets


def test_un_run_charge_laisse_quand_meme_de_quoi_rattraper():
    """Le bug : `plafond - len(nouveaux)` tombait à zéro les jours chargés,
    et 178 offres LinkedIn restaient à `detail_essais = 0` — pas trois échecs,
    zéro tentative."""
    pour_neuf, pour_backlog = budgets(250, nouveaux=500)
    assert pour_backlog > 0
    assert pour_neuf + pour_backlog == 250


def test_un_run_calme_rend_tout_le_reste_au_backlog():
    """La réserve est un plancher, pas un plafond."""
    pour_neuf, pour_backlog = budgets(250, nouveaux=54)
    assert pour_backlog == 250 - 54


def test_sans_nouveaute_tout_va_au_backlog():
    assert budgets(250, nouveaux=0)[1] == 250


def test_le_total_ne_depasse_jamais_le_plafond():
    """Le plafond protège la source ; le dépasser serait pire que le backlog."""
    for plafond in (1, 4, 60, 120, 250):
        for nouveaux in (0, 1, 50, 500):
            pour_neuf, pour_backlog = budgets(plafond, nouveaux)
            # Ce qui part RÉELLEMENT en requêtes : les nouveautés traitées,
            # plus le rattrapage. `pour_neuf` seul est une borne, pas une
            # consommation — un run sans nouveauté n'en dépense aucune.
            consomme = min(nouveaux, pour_neuf) + pour_backlog
            assert consomme <= plafond, (plafond, nouveaux, consomme)
            assert pour_neuf >= 0 and pour_backlog >= 0


def test_la_part_est_reglable():
    assert budgets(100, nouveaux=1000, part_backlog=0.5) == (50, 50)
    assert budgets(100, nouveaux=1000, part_backlog=0.0) == (100, 0)


def test_un_plafond_minuscule_reste_utilisable():
    """`--max-details 4` en test ne doit pas rendre le collecteur inerte."""
    pour_neuf, _ = budgets(4, nouveaux=10)
    assert pour_neuf >= 1
