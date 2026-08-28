# Démarrer les bots

Guide de mise en route et d'usage quotidien. Pour le détail de chaque
réglage, voir [`TUTO.md`](TUTO.md) ; pour les choix techniques,
[`README.md`](README.md).

---

## 1. Installation — une seule fois

```powershell
cd C:\Users\stageinfo\alternance-radar
python -m venv .venv
.venv\Scripts\python.exe -m pip install httpx curl_cffi beautifulsoup4 lxml pyyaml playwright google-genai
```

**Pas besoin de `playwright install`** : les bots qui pilotent un navigateur
utilisent le **vrai Chrome** déjà installé sur la machine
(`channel="chrome"`), pas le Chromium téléchargé par Playwright.

Optionnel, seulement pour générer des CV en PDF :

```powershell
winget install MiKTeX.MiKTeX
```

Sans MiKTeX, `cv` produit quand même le `.tex`, compilable sur Overleaf.

---

## 2. Configuration — une seule fois

Deux fichiers personnels, **jamais versionnés** (ils sont dans `.gitignore`
parce qu'ils contiennent tes coordonnées) :

```powershell
copy config.example.yaml config.yaml
copy cv_source.example.yaml cv_source.yaml
```

| Fichier | À remplir |
|---|---|
| `config.yaml` | ton profil, tes coordonnées, les mots-clés de recherche |
| `cv_source.yaml` | ton parcours réel : expériences, formation, compétences |

Deux clés facultatives, à poser une fois pour toutes (elles survivent au
redémarrage) :

```powershell
setx LBA_API_KEY "..."      # La Bonne Alternance — compte gratuit sur api.apprentissage.beta.gouv.fr
setx GEMINI_API_KEY "..."   # accroche personnalisée du CV
```

Chacune est **optionnelle** : sans `LBA_API_KEY` la source est simplement
ignorée, sans `GEMINI_API_KEY` le CV se génère sans son paragraphe
d'accroche. Rien ne casse.

---

## 3. Les trois familles de bots

C'est la distinction qui compte : elles n'ont ni les mêmes contraintes, ni
les mêmes risques.

### A. Les 10 sources d'offres — sans session, automatisables

```powershell
.\radar.ps1 tout --depuis jour
```

Enchaîne LinkedIn, HelloWork, Indeed, JobTeaser, Welcome to the Jungle,
PASS, adopte1dev, DevITjobs, WeLoveDevs, Glassdoor, La Bonne Alternance,
puis génère le digest. Aucune session, aucune fenêtre : c'est la seule
famille qu'on peut mettre dans une tâche planifiée.

Chaque source se lance aussi seule : `.\radar.ps1 wttj`, `.\radar.ps1 pass`…

### B. Les posts LinkedIn — session requise, à lancer à la main

```powershell
.\radar.ps1 login      # la toute première fois seulement
.\radar.ps1 posts
```

L'intérêt : un post de recruteur donne souvent une **adresse e-mail
directe**, ce qu'une annonce classique ne donne jamais.

Au premier `login`, une fenêtre Chrome s'ouvre sur un profil dédié — **tu te
connectes à la main**, le programme ne saisit aucun identifiant. Ensuite la
session est réutilisée.

> **Ne mets jamais `posts` dans une tâche planifiée.** Un scraping du fil
> qui se déclenche seul à heure fixe est le meilleur moyen de faire
> restreindre ton compte. Le programme refuse d'ailleurs de repartir moins
> de 6 h après le run précédent.

### C. Les posts via moteur de recherche — sans session, sans risque

```powershell
.\radar.ps1 posts-web
```

Même objectif que `posts`, mais c'est **DuckDuckGo qui cherche** : aucune
requête n'atteint LinkedIn. Donc pas de session, pas de délai de 6 h, pas de
risque de coupure — lance-le autant que tu veux.

Une fenêtre Chrome s'ouvre quand même (DuckDuckGo rend une page vide à un
navigateur sans interface), donc à lancer à la main plutôt qu'en tâche
planifiée.

**Ce qu'il faut en attendre** : c'est un canal de *largeur*, pas de
fraîcheur. Ce qu'un moteur indexe est vieux — âge médian 141 jours, ~13 % à
moins de trois semaines. Sur un run réel : 290 posts balayés → 2 retenus,
dont 1 avec e-mail direct.

|  | A. `tout` | B. `posts` | C. `posts-web` |
|---|---|---|---|
| Session LinkedIn | non | **oui** | non |
| Ouvre une fenêtre | non | oui | oui |
| Délai entre runs | aucun | **6 h** | aucun |
| Risque pour le compte | nul | réel | nul |
| Automatisable | **oui** | non | non |

---

## 4. Au quotidien

```powershell
.\radar.ps1 tout --depuis jour     # collecte du jour + digest
.\radar.ps1 serve                  # ouvre le digest, cases « déjà postulé » actives
```

`serve` est le mode normal de consultation : les filtres et le suivi de
candidature y sont actifs, contrairement au `digest.html` ouvert en
`file://`.

Fenêtres possibles avec `--depuis` : `jour`, `3jours`, `semaine`,
`2semaines`.

---

## 5. Automatiser la collecte

```powershell
schtasks /create /tn "AlternanceRadar" /sc daily /st 07:00 `
  /tr "powershell -File C:\Users\stageinfo\alternance-radar\run_quotidien.ps1"
```

`run_quotidien.ps1` n'enchaîne que la famille A — sans session, sans
fenêtre. Les familles B et C en sont volontairement exclues.

Pour arrêter : `schtasks /delete /tn "AlternanceRadar" /f`

---

## 6. Vérifier que rien n'est cassé

```powershell
.\radar.ps1 canari
```

Un site change son HTML, le parseur ne trouve plus rien, et le programme
rapporte tranquillement « 0 offre » — indiscernable d'une journée sans
publication. Le canari valide **la donnée extraite**, pas le code HTTP :
volume plausible et champs obligatoires réellement remplis.

```
  OK     wttj         28 offres, tous champs remplis
  CASSÉ  indeed       0 offres, 10 attendues au minimum
  ?      lba          4 offres, 5 attendues — trop peu pour conclure
```

À lancer quand une source semble muette depuis quelques jours.

---

## 7. Exploiter les résultats

```powershell
.\radar.ps1 contacts                    # toutes les pistes joignables directement
.\radar.ps1 cv <uid ou lien>            # CV LaTeX adapté à l'offre
.\radar.ps1 postuler <uid ou lien>      # pré-remplit le formulaire (n'envoie RIEN)
.\radar.ps1 suivi                       # tableau de bord des candidatures
.\radar.ps1 comptes                     # quels comptes ajouter à posts.comptes_suivis
```

`cv` et `postuler` acceptent un `uid`, un bout de nom d'entreprise, **ou le
lien de l'offre** collé depuis le navigateur — y compris une offre jamais
collectée.

> `postuler` remplit et s'arrête. **Il n'envoie jamais rien** : c'est toi qui
> relis et qui cliques.

---

## En cas de problème

| Symptôme | Cause probable |
|---|---|
| `Le caractère perluète (&) n'est pas autorisé` | mets le lien entre `"guillemets"` |
| `cv_source.yaml introuvable` | étape 2 non faite |
| `posts` refuse de partir | moins de 6 h depuis le dernier run — `--force` pour outrepasser |
| `posts-web` ne trouve rien | DuckDuckGo a servi une page vide ; relance |
| une source rend 0 offre | lance `canari` pour savoir si c'est cassé ou juste vide |
| `pdflatex introuvable` | MiKTeX absent — le `.tex` reste utilisable sur Overleaf |
