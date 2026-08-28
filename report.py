"""Génération du digest HTML.

Un fichier local, ouvrable dans le navigateur, trié par pertinence. Chaque
offre affiche son `uid` pour pouvoir enchaîner sur `python main.py mark <uid>`.

Chaque carte porte aussi une case **« déjà postulé »**. Elle n'écrit en base
que si la page est servie par `main.py serve` : un fichier ouvert en `file://`
n'a aucun moyen d'atteindre SQLite, et le navigateur bloque de toute façon la
requête. Dans ce cas la case est désactivée et un bandeau le dit — plutôt
qu'une case qui coche et perd silencieusement l'information.
"""

from __future__ import annotations

import html
import json
from datetime import date, datetime
from pathlib import Path

from core.store import Store

CSS = """
:root {
  --bg:#fbfbfa; --fg:#1a1a18; --muted:#6b6b63; --line:#e4e2dd;
  --card:#fff; --fort:#1f7a4d; --moyen:#9a6a12; --faible:#8a8a80;
}
@media (prefers-color-scheme: dark) {
  :root {
    --bg:#16161a; --fg:#e8e8e3; --muted:#9a9a92; --line:#2c2c32;
    --card:#1e1e23; --fort:#4ec98a; --moyen:#d9a441; --faible:#7a7a72;
  }
}
* { box-sizing:border-box; }
body { margin:0; padding:2rem 1rem 4rem; background:var(--bg); color:var(--fg);
  font:15px/1.55 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif; }
main { max-width:52rem; margin:0 auto; }
h1 { font-size:1.5rem; margin:0 0 .25rem; letter-spacing:-.01em; }
.meta { color:var(--muted); font-size:.875rem; margin-bottom:2rem; }
.job { background:var(--card); border:1px solid var(--line); border-radius:10px;
  padding:1rem 1.15rem; margin-bottom:.7rem; display:grid;
  grid-template-columns:3.2rem 1fr; gap:0 1rem; }
.score { font-size:1.15rem; font-weight:650; font-variant-numeric:tabular-nums;
  text-align:center; padding-top:.1rem; }
.score.fort { color:var(--fort); } .score.moyen { color:var(--moyen); }
.score.faible { color:var(--faible); }
.job h2 { font-size:1rem; font-weight:600; margin:0 0 .2rem; }
.job h2 a { color:inherit; text-decoration:none; }
.job h2 a:hover { text-decoration:underline; }
.sub { color:var(--muted); font-size:.875rem; margin-bottom:.5rem; }
.tags { display:flex; flex-wrap:wrap; gap:.3rem; margin-bottom:.45rem; }
.tag { font-size:.72rem; padding:.12rem .45rem; border:1px solid var(--line);
  border-radius:99px; color:var(--muted); }
.note { font-size:.82rem; margin-bottom:.45rem; padding:.3rem .6rem;
  border-left:3px solid var(--moyen); background:var(--bg); border-radius:0 5px 5px 0; }
.contacts { margin-bottom:.45rem; font-size:.82rem; }
.contacts a { display:inline-block; margin-right:.5rem; padding:.15rem .5rem;
  border-radius:5px; background:var(--fort); color:#fff; text-decoration:none;
  font-weight:550; }
.src { font-size:.68rem; text-transform:uppercase; letter-spacing:.04em;
  color:var(--muted); border:1px solid var(--line); border-radius:4px;
  padding:.05rem .3rem; margin-left:.4rem; }
.detail { font-size:.75rem; color:var(--muted); font-family:ui-monospace,
  "Cascadia Code",Consolas,monospace; word-break:break-word; }
.uid { font-size:.7rem; color:var(--muted); font-family:ui-monospace,monospace;
  opacity:.6; margin-top:.35rem; }
.vide { color:var(--muted); text-align:center; padding:3rem 0; }
.section { margin:2.6rem 0 1rem; font-size:1.05rem; font-weight:650;
  padding-bottom:.45rem; border-bottom:1px solid var(--line); }
.section em { font-weight:400; font-style:normal; color:var(--muted);
  font-size:.85rem; }

/* Suivi de candidature */
.suivi { display:flex; align-items:center; gap:.5rem; margin-top:.5rem; }
.suivi label { display:inline-flex; align-items:center; gap:.4rem;
  font-size:.82rem; color:var(--muted); cursor:pointer; user-select:none;
  padding:.2rem .55rem; border:1px solid var(--line); border-radius:99px; }
.suivi label:hover { border-color:var(--fort); color:var(--fg); }
.suivi input { accent-color:var(--fort); width:.95rem; height:.95rem;
  margin:0; cursor:pointer; }
.suivi input:disabled { cursor:not-allowed; }
.suivi label:has(input:disabled) { opacity:.45; cursor:not-allowed; }
.echo { font-size:.76rem; color:var(--muted); opacity:0; transition:opacity .15s; }
.echo.vu { opacity:1; }
.echo.rate { color:#c0392b; }
.job.postule { border-color:var(--fort); }
.job.postule .score { opacity:.5; }
.job.postule h2 { text-decoration:line-through; text-decoration-thickness:1px;
  text-decoration-color:var(--muted); }
.bandeau { background:var(--card); border:1px solid var(--moyen);
  border-left-width:3px; border-radius:0 8px 8px 0; padding:.7rem 1rem;
  margin-bottom:1.5rem; font-size:.85rem; }
.bandeau code { background:var(--bg); padding:.1rem .35rem; border-radius:4px;
  font-family:ui-monospace,Consolas,monospace; }

/* Panneau de filtres et de mise à jour */
.panneau { background:var(--card); border:1px solid var(--line);
  border-radius:10px; padding:1rem 1.15rem; margin-bottom:1.6rem; }
.panneau h3 { font-size:.78rem; text-transform:uppercase; letter-spacing:.05em;
  color:var(--muted); margin:0 0 .6rem; font-weight:600; }
.panneau h3:not(:first-child) { margin-top:1.1rem; padding-top:1rem;
  border-top:1px solid var(--line); }
.sources { display:flex; flex-wrap:wrap; gap:.4rem; margin-bottom:.8rem; }
.sources label { display:inline-flex; align-items:center; gap:.35rem;
  font-size:.82rem; padding:.25rem .6rem; border:1px solid var(--line);
  border-radius:99px; cursor:pointer; user-select:none; }
.sources label:hover { border-color:var(--fort); }
.sources label:has(input:checked) { border-color:var(--fort);
  background:color-mix(in srgb, var(--fort) 12%, transparent); }
.sources input { accent-color:var(--fort); margin:0; }
.sources .n { color:var(--muted); font-variant-numeric:tabular-nums; }
.reglages { display:flex; flex-wrap:wrap; gap:.7rem; align-items:flex-end; }
.reglages label { display:flex; flex-direction:column; gap:.2rem;
  font-size:.75rem; color:var(--muted); }
.reglages input, .reglages select, .panneau button {
  font:inherit; font-size:.85rem; padding:.32rem .5rem; border-radius:6px;
  border:1px solid var(--line); background:var(--bg); color:var(--fg); }
.panneau button { background:var(--fort); color:#fff; border-color:transparent;
  font-weight:550; cursor:pointer; padding:.38rem .9rem; }
.panneau button:hover { filter:brightness(1.08); }
.panneau button:disabled { opacity:.5; cursor:not-allowed; filter:none; }
.panneau .secondaire { background:transparent; color:var(--muted);
  border-color:var(--line); font-weight:400; }
.journal { margin-top:.7rem; font-size:.76rem; font-family:ui-monospace,
  Consolas,monospace; color:var(--muted); background:var(--bg);
  border:1px solid var(--line); border-radius:6px; padding:.5rem .6rem;
  max-height:11rem; overflow:auto; white-space:pre-wrap; word-break:break-word; }
.journal:empty { display:none; }
"""

# Le suivi n'a de sens que servi : en `file://`, `fetch` vers localhost est
# bloqué par le navigateur, et une case qui coche sans rien enregistrer est
# pire qu'une case absente.
JS = """
(function () {
  var horsLigne = location.protocol === 'file:';
  var bandeau = document.getElementById('bandeau');
  if (horsLigne && bandeau) bandeau.hidden = false;

  function echo(carte, texte, rate) {
    var e = carte.querySelector('.echo');
    if (!e) return;
    e.textContent = texte;
    e.className = 'echo vu' + (rate ? ' rate' : '');
    clearTimeout(e._t);
    e._t = setTimeout(function () { e.className = 'echo'; }, 2500);
  }

  document.querySelectorAll('.suivi input[data-uid]').forEach(function (c) {
    if (horsLigne) { c.disabled = true; return; }
    c.addEventListener('change', function () {
      var carte = c.closest('.job');
      var vise = c.checked;
      c.disabled = true;
      fetch('api/statut', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ uid: c.dataset.uid,
                               statut: vise ? 'applied' : 'new' })
      }).then(function (r) {
        if (!r.ok) return r.text().then(function (t) { throw new Error(t); });
        carte.classList.toggle('postule', vise);
        echo(carte, vise ? 'candidature enregistrée' : 'retirée du suivi');
      }).catch(function (e) {
        c.checked = !vise;              // on ne ment pas sur l'état réel
        echo(carte, 'échec : ' + e.message, true);
      }).then(function () { c.disabled = false; });
    });
  });

  /* ---- Mise à jour : lance une collecte et suit son avancement ---- */
  var bouton = document.getElementById('lancer');
  var journal = document.getElementById('journal');
  var sonde = null;

  function ecrire(lignes, fini) {
    if (!journal) return;
    journal.textContent = (lignes || []).join('\\n');
    if (fini) {
      journal.textContent += '\\n\\n— terminé. Recharge la page pour voir '
                           + 'les nouvelles offres.';
    }
    journal.scrollTop = journal.scrollHeight;
  }

  function suivre() {
    fetch('api/collecte').then(function (r) { return r.json(); })
      .then(function (e) {
        ecrire(e.journal, !e.encours && e.journal.length > 0);
        bouton.disabled = e.encours;
        bouton.textContent = e.encours ? 'collecte en cours…' : 'mettre à jour';
        if (!e.encours && sonde) { clearInterval(sonde); sonde = null; }
      })
      .catch(function () { if (sonde) { clearInterval(sonde); sonde = null; } });
  }

  if (bouton && horsLigne) {
    bouton.disabled = true;
  } else if (bouton) {
    bouton.addEventListener('click', function () {
      bouton.disabled = true;
      fetch('api/collecte', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          source: document.getElementById('maj-source').value,
          depuis: document.getElementById('maj-depuis').value
        })
      }).then(function (r) {
        if (!r.ok) return r.text().then(function (t) { throw new Error(t); });
        if (!sonde) sonde = setInterval(suivre, 2000);
        suivre();
      }).catch(function (e) {
        bouton.disabled = false;
        ecrire(['échec du lancement : ' + e.message]);
      });
    });
    // Une collecte lancée avant un rechargement doit rester visible.
    suivre();
    sonde = setInterval(suivre, 2000);
  }
})();
"""


# Nom lisible de chaque source, pour les cartes comme pour les filtres.
LIBELLES_SOURCE = {
    "linkedin": "LinkedIn",
    "hellowork": "HelloWork",
    "indeed": "Indeed",
    "jobteaser": "JobTeaser",
    "wttj": "Welcome to the Jungle",
    "lba": "La Bonne Alternance",
    "lba_entreprise": "Marché caché",
    "linkedin_post": "Posts LinkedIn",
    "pass": "PASS (fonction publique)",
    "glassdoor": "Glassdoor",
    "adopte1dev": "adopte1dev",
    "devitjobs": "DevITjobs.fr",
    "welovedevs": "WeLoveDevs",
}

# Ce qu'on peut relancer depuis la page. `posts` en est délibérément absent :
# il ouvre une fenêtre Chrome, exige une session LinkedIn et impose six heures
# entre deux passages. Un bouton l'aurait rendu trop facile à déclencher.
COLLECTES = [
    ("tout", "toutes les sources"),
    ("wttj", "Welcome to the Jungle"),
    ("pass", "PASS (fonction publique)"),
    ("adopte1dev", "adopte1dev"),
    ("devitjobs", "DevITjobs.fr"),
    ("welovedevs", "WeLoveDevs"),
    ("jobteaser", "JobTeaser"),
    ("indeed", "Indeed"),
    ("hellowork", "HelloWork"),
    ("glassdoor", "Glassdoor"),
    ("lba", "La Bonne Alternance"),
    ("collect", "LinkedIn (long)"),
]

FENETRES = [("jour", "24 heures"), ("3jours", "3 jours"),
            ("semaine", "1 semaine"), ("2semaines", "2 semaines")]

# Valeurs en HEURES, pas en jours. « 1 jour » signifiait « hier ou
# aujourd'hui », soit jusqu'à 48 h ; en heures, 24 veut dire 24 pour les
# sources qui publient un horodatage (PASS, WTTJ, Indeed, JobTeaser).
AGES = [("24", "24 heures"), ("72", "3 jours"), ("168", "1 semaine"),
        ("336", "2 semaines"), ("720", "1 mois"), ("", "sans limite")]


def _panneau(store: Store, sources: list[str] | None, score_min: int,
             age_max_heures: int | None, tri: str) -> str:
    """Filtres de lecture + déclenchement d'une collecte.

    Les filtres passent par des paramètres d'URL et un rechargement plutôt
    que par du tri côté client : `store.selection()` sait déjà filtrer, et
    filtrer en base garantit que la page montre exactement ce que montrerait
    la même requête en ligne de commande.
    """
    dispo = store.sources_disponibles()
    coches = set(sources or [])

    cases = "".join(
        f'<label><input type="checkbox" name="src" value="{html.escape(s)}"'
        f'{" checked" if s in coches else ""}> '
        f'{html.escape(LIBELLES_SOURCE.get(s, s))} '
        f'<span class="n">{n}</span></label>'
        for s, n in dispo)

    def options(liste, courant):
        return "".join(
            f'<option value="{html.escape(v)}"'
            f'{" selected" if v == courant else ""}>{html.escape(t)}</option>'
            for v, t in liste)

    return f"""
    <div class="panneau">
      <form method="get">
        <h3>Sources <em></em></h3>
        <div class="sources">{cases}</div>
        <div class="reglages">
          <label>score minimum
            <input type="number" name="score" value="{score_min}" min="-999"
                   max="200" step="5" style="width:5.5rem"></label>
          <label>publiées depuis
            <select name="age">{options(AGES, "" if age_max_heures is None
                                        else str(age_max_heures))}</select></label>
          <label>trier par
            <select name="tri">{options([(t, t) for t in Store.TRIS],
                                        tri)}</select></label>
          <button type="submit">filtrer</button>
          <button type="submit" class="secondaire" name="raz" value="1"
                  formnovalidate>tout afficher</button>
        </div>
      </form>

      <h3>Mettre à jour les offres</h3>
      <div class="reglages">
        <label>source
          <select id="maj-source">{options(COLLECTES, "wttj")}</select></label>
        <label>fenêtre
          <select id="maj-depuis">{options(FENETRES, "semaine")}</select></label>
        <button type="button" id="lancer">mettre à jour</button>
      </div>
      <div class="journal" id="journal"></div>
    </div>"""


def _classe(score: int) -> str:
    if score >= 40:
        return "fort"
    return "moyen" if score >= 20 else "faible"


def age_relatif(publie: str | None) -> str:
    """« hier », « il y a 5 jours », « il y a 3 semaines », « il y a 5 mois ».

    L'âge relatif est plus parlant qu'une date brute quand on cherche du
    frais : « il y a 2 jours » se lit sans calcul mental. Mais au-delà de
    deux semaines le compte en jours cesse de se lire — « il y a 141 jours »
    demande le même calcul qu'une date. On change donc d'unité, ce qui
    compte surtout depuis `posts-web` : ce qu'un moteur indexe est vieux, et
    c'est justement l'information qu'on veut voir d'un coup d'œil.

    Retourne "" si la date est absente ou illisible : c'est à l'appelant de
    décider quoi afficher à la place, une date manquante n'ayant pas le même
    sens partout (le marché caché n'en a jamais).
    """
    if not publie:
        return ""
    try:
        jours = (date.today() - date.fromisoformat(publie)).days
    except ValueError:
        return ""
    if jours <= 0:
        return "aujourd'hui"
    if jours == 1:
        return "hier"
    if jours < 14:
        return f"il y a {jours} jours"
    if jours < 60:
        semaines = jours // 7
        return f"il y a {semaines} semaine{'s' if semaines > 1 else ''}"
    return f"il y a {jours // 30} mois"


def _carte(o) -> str:
    """Rend une offre ou une entreprise en carte HTML."""
    tags = json.loads(o["tags"] or "[]")
    lieu = " · ".join(x for x in (o["company"], o["location"]) if x)
    if o["posted_at"]:
        age = age_relatif(o["posted_at"])
        date_pub = f" · {age}" if age else f" · {o['posted_at']}"
    elif o["source"] == "lba_entreprise":
        date_pub = ""      # liste permanente, une date n'aurait aucun sens
    else:
        date_pub = " · ⚠ date inconnue"
    statut_txt = "" if o["status"] == "new" else f" · {o['status']}"
    # Une offre sans description a été scorée sur son seul titre : son
    # score n'est pas comparable aux autres, il faut le signaler.
    # Sans objet pour les entreprises du marché caché, qui n'ont par
    # construction aucune description de poste.
    if (o["source"] != "lba_entreprise"
            and len(o["description"] or "") < Store.SEUIL_DESCRIPTION):
        tags = tags + ["⚠ score sur titre seul"]

    # Les contacts directs sont la raison d'être des posts de fil et du
    # marché caché : ils passent avant le reste de la carte. Un numéro de
    # téléphone devient un lien `tel:`, une adresse un lien `mailto:`.
    contacts = json.loads(o["contacts"] or "[]")
    bloc_contacts = ""
    if contacts:
        liens = []
        for c in contacts:
            if "@" in c:
                liens.append(f'<a href="mailto:{html.escape(c)}">✉ {html.escape(c)}</a>')
            else:
                tel = html.escape(c.replace(" ", ""))
                liens.append(f'<a href="tel:{tel}">☎ {html.escape(c)}</a>')
        bloc_contacts = f'<div class="contacts">{"".join(liens)}</div>'

    source = LIBELLES_SOURCE.get(o["source"], o["source"])

    postule = o["status"] == "applied"
    uid = html.escape(o["uid"])

    return f"""
    <article class="job{' postule' if postule else ''}">
      <div class="score {_classe(o['score'])}">{o['score']}</div>
      <div>
        <h2><a href="{html.escape(o['url'] or '#')}" target="_blank"
               rel="noopener">{html.escape(o['title'])}</a><span class="src">{html.escape(source)}</span></h2>
        <div class="sub">{html.escape(lieu)}{date_pub}{statut_txt}</div>
        {f'<div class="note">📌 {html.escape(o["notes"])}</div>' if o["notes"] else ''}
        {bloc_contacts}
        <div class="tags">{''.join(f'<span class="tag">{html.escape(t)}</span>'
                                   for t in tags)}</div>
        <div class="detail">{html.escape(o['score_detail'] or '')}</div>
        <div class="suivi">
          <label><input type="checkbox" data-uid="{uid}"
                        {'checked' if postule else ''}> déjà postulé</label>
          <span class="echo"></span>
        </div>
        <div class="uid">{uid}</div>
      </div>
    </article>"""


def construire(store: Store, cfg: dict,
               score_min: int = 0, statut: str | None = None,
               age_max_heures: int | None = None, tri: str = "score",
               sources: list[str] | None = None,
               servi: bool = False) -> str:
    """Rend le digest en mémoire.

    Séparé de `generer` pour que le serveur puisse le reconstruire à chaque
    chargement : après avoir coché une case, un rafraîchissement doit montrer
    la liste à jour, pas le HTML figé du dernier run.

    `sources` filtre sur les plateformes retenues ; `None` les prend toutes.
    """
    # Deux populations distinctes, deux sections. Une entreprise du marché
    # caché n'a aucune description de poste : la classer sur la même échelle
    # qu'une annonce détaillée l'enterrerait systématiquement, alors que c'est
    # le canal avec le meilleur taux de retour en alternance.
    # Une offre déjà traitée ne doit plus encombrer la liste de travail :
    # c'est tout l'intérêt de la marquer. Elle reparaît en bas, dans la
    # section des candidatures.
    traites = ["applied", "rejected"] if not statut else None

    # Le marché caché a sa propre section : il sort du filtre de sources des
    # offres, et n'apparaît que s'il a été explicitement coché (ou qu'aucun
    # filtre n'est posé).
    choisies = [s for s in (sources or []) if s != "lba_entreprise"]
    veut_cache = sources is None or "lba_entreprise" in sources

    offres = (store.selection(statut=statut, score_min=score_min,
                              alternance_seulement=True, limite=300,
                              sources=choisies or None,
                              exclure_sources=["lba_entreprise"],
                              age_max_heures=age_max_heures, tri=tri,
                              exclure_statuts=traites)
              if sources is None or choisies else [])
    # Le filtre d'âge ne s'applique pas au marché caché : ces entreprises n'ont
    # aucune date de publication, il les supprimerait toutes. En revanche on
    # n'en montre que la tête : des milliers d'entrées noieraient les offres.
    r = cfg.get("rapport", {})
    entreprises = (store.selection(
        statut=statut,
        score_min=int(r.get("marche_cache_score_min", 45)),
        alternance_seulement=True,
        limite=int(r.get("marche_cache_limite", 40)),
        sources=["lba_entreprise"], tri=tri) if veut_cache else [])
    stats = store.stats()
    profil = cfg.get("profil", {})

    # Combien d'offres la fenêtre laisse de côté. Sans ce chiffre, une fenêtre
    # étroite ressemble à une base vide, et une fenêtre large laisse croire
    # qu'on regarde le jour même — c'est exactement la confusion qui a fait
    # apparaître une annonce de huit jours dans ce qu'on croyait être la vue
    # du jour.
    hors_fenetre = 0
    if age_max_heures is not None and (sources is None or choisies):
        hors_fenetre = max(0, len(store.selection(
            statut=statut, score_min=score_min, alternance_seulement=True,
            limite=2000, sources=choisies or None,
            exclure_sources=["lba_entreprise"], tri=tri,
            exclure_statuts=traites)) - len(offres))

    filtre = f"score ≥ {score_min}"
    if age_max_heures is not None:
        duree = (f"{age_max_heures} h" if age_max_heures < 48
                 else f"{age_max_heures // 24} jours")
        filtre += f", publiées il y a moins de {duree} — date vérifiée"
    filtre += f", triées par {tri}"
    if choisies:
        filtre += (" · sources : "
                   + ", ".join(LIBELLES_SOURCE.get(s, s) for s in choisies))

    corps = ""
    if offres:
        reste = (f' · <b>{hors_fenetre} autres hors de la fenêtre</b>'
                 if hors_fenetre else '')
        corps += (f'<div class="section">Offres <em>— {len(offres)} annonces · '
                  f'{filtre}{reste}</em></div>')
        corps += "".join(_carte(o) for o in offres)
    if entreprises:
        corps += (f'<div class="section">Marché caché <em>— {len(entreprises)} '
                  f'entreprises recrutant en alternance sans offre publiée '
                  f'(candidature spontanée)</em></div>')
        corps += "".join(_carte(e) for e in entreprises)

    # Candidatures envoyées : sorties de la liste de travail, mais gardées
    # sous les yeux — c'est là qu'on suit les relances.
    #
    # Volontairement HORS du filtre de sources : en cochant « WTTJ seul », on
    # veut restreindre sa recherche, pas perdre de vue une candidature envoyée
    # sur LinkedIn. La mention le dit, faute de quoi l'écran mélange des
    # plateformes sans expliquer pourquoi.
    if not statut:
        envoyees = store.selection(statut="applied", score_min=-999,
                                   alternance_seulement=False, limite=100,
                                   inclure_exclus=True, tri=tri)
        if envoyees:
            corps += (f'<div class="section">Mes candidatures <em>— '
                      f'{len(envoyees)} envoyées'
                      f'{" · toutes sources" if choisies else ""}</em></div>')
            corps += "".join(_carte(o) for o in envoyees)

    if not corps:
        corps = ('<p class="vide">Aucun résultat ne correspond aux critères.'
                 + (f'<br>{hors_fenetre} offres attendent au-delà de la '
                    f'fenêtre choisie.' if hors_fenetre > 0 else '')
                 + ('<br>Élargis la fenêtre, baisse le score minimum, ou coche '
                    'd\'autres sources.' if servi else '') + '</p>')

    # Le panneau n'a de sens que servi : ses filtres passent par un
    # rechargement côté serveur, et un formulaire soumis depuis un fichier
    # local rechargerait la même page figée sans rien filtrer du tout.
    panneau = _panneau(store, sources, score_min, age_max_heures,
                       tri) if servi else ""

    # Transparence sur ce qui a été retiré : sans ça, le digest laisse croire
    # qu'il montre tout ce qui a été collecté.
    retraits = []
    if stats.get("exclus"):
        detail = ", ".join(f"{m} : {n}" for m, n in
                           list(store.exclusions_par_motif().items())[:6])
        retraits.append(f"{stats['exclus']} écartées par secteur ({detail})")
    if age_max_heures is not None:
        sans_date = store.compte_sans_date(max(1, age_max_heures // 24))
        if sans_date:
            retraits.append(f"{sans_date} sans date de publication, donc "
                            f"non vérifiables et écartées")

    genere_le = datetime.now().strftime("%d/%m/%Y à %H:%M")

    return f"""<!doctype html>
<html lang="fr"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Radar alternance — {html.escape(str(profil.get('nom', '')))}</title>
<style>{CSS}</style></head><body><main>
<h1>Radar alternance</h1>
<div class="meta">
  {len(offres)} offres · {len(entreprises)} entreprises du marché caché ·
  {stats['alternances']} alternances en base sur {stats['total']} collectées<br>
  {(' · '.join(retraits) + '<br>') if retraits else ''}
  {html.escape(str(profil.get('niveau', '')))} ·
  {html.escape(str(profil.get('rythme', '')))} ·
  démarrage {html.escape(str(profil.get('demarrage', '')))}<br>
  généré le {genere_le}
</div>
{panneau}
<div class="bandeau" id="bandeau" hidden>
  Ce digest est ouvert comme simple fichier : les cases « déjà postulé » sont
  désactivées, car une page en <code>file://</code> ne peut pas écrire dans la
  base. Lance <code>.\\radar.ps1 serve</code> pour un digest où les cases
  s'enregistrent vraiment.
</div>
{corps}
</main><script>{JS}</script></body></html>"""


def generer(store: Store, cfg: dict, sortie: Path,
            score_min: int = 0, statut: str | None = None,
            age_max_heures: int | None = None, tri: str = "score") -> Path:
    """Écrit le digest sur disque."""
    sortie.write_text(
        construire(store, cfg, score_min, statut, age_max_heures, tri),
        encoding="utf-8")
    return sortie
