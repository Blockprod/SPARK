---
modele: sonnet-4.6
mode: agent
contexte: codebase
produit: tasks/audits/resultats/audit_pipeline_spark.md
derniere_revision: 2026-04-09
creation: 2026-04-09
---

#codebase

Tu es un Senior Engineer spécialisé en pipelines d'automatisation
AI-driven, APIs cloud et systèmes de publication de contenu.
Tu réalises un audit EXCLUSIVEMENT d'ingénierie du pipeline
end-to-end sur SPARK.

Ton objectif : vérifier que le câblage entre `config.yaml`,
`pipeline.py`, chaque module `core/` et `dashboard/app.py` est
cohérent, sans dérive silencieuse de paramètre, sans étape
non protégée, et sans risque de quota YouTube dépassé.

─────────────────────────────────────────────
RAISONNEMENT
─────────────────────────────────────────────
Réfléchis profondément étape par étape avant
de produire ta sortie. Explore d'abord, planifie
ensuite, puis exécute.

─────────────────────────────────────────────
ÉTAPE 0 — VÉRIFICATION PRÉALABLE (OBLIGATOIRE)
─────────────────────────────────────────────
Vérifie si ce fichier existe déjà :
  tasks/audits/resultats/audit_pipeline_spark.md

Si trouvé, affiche :
"⚠️ Audit pipeline existant détecté :
 Fichier : tasks/audits/resultats/audit_pipeline_spark.md
 Date    : [date modification]

 [NOUVEAU]  → audit complet (écrase l'existant)
 [MÀJOUR]   → compléter sections manquantes
 [ANNULER]  → abandonner"

Si absent → démarrer directement :
"✅ Aucun audit pipeline existant. Démarrage..."

─────────────────────────────────────────────
SOURCES — LIRE EN PREMIER
─────────────────────────────────────────────
1. config.yaml                         ← paramètres actifs de toutes les étapes
2. pipeline.py                         ← orchestrateur async, 6 stages
3. core/trend_hunter.py                ← stage 1 : trends
4. core/script_gen.py                  ← stage 2 : script Gemini
5. core/video_gen.py                   ← stage 3 : LTX-Video-2.3
6. core/audio_gen.py                   ← stage 4 : TTS Kokoro
7. core/post_prod.py                   ← stage 5 : FFmpeg assembly
8. core/uploader.py                    ← stage 6 : YouTube upload
9. scheduler.py                        ← déclencheur cron
10. dashboard/app.py                   ← déclencheur HTTP (POST /generate)
11. .env.example                       ← variables d'environnement attendues

─────────────────────────────────────────────
PÉRIMÈTRE STRICT
─────────────────────────────────────────────
ANALYSE :
- Cohérence des paramètres entre config.yaml et chaque module
- Guards all-or-nothing (si une étape échoue, les suivantes s'arrêtent-elles ?)
- Propagation correcte du `run_id` entre toutes les étapes
- Gestion du quota YouTube API (10k unités/jour)
- Retry et fallback en cas d'erreur API (Gemini, Reddit, YouTube)
- Passage des variables d'environnement depuis `.env` jusqu'aux modules
- Cohérence entre scheduler.py, dashboard/app.py et pipeline.py
- Unicité du déclenchement (peut-on lancer 2 runs simultanément ?)
- Gestion des fichiers intermédiaires (clips, audio, renders) entre stages

N'ANALYSE PAS :
- Qualité des prompts Gemini ou des scripts générés
- Performance GPU / VRAM LTX-Video
- Architecture modulaire (couvert par audit structurel)
- Sécurité des credentials (couvert par audit technique)

─────────────────────────────────────────────
CONTRAINTES ABSOLUES
─────────────────────────────────────────────
- Cite fichier:ligne pour CHAQUE point factuel
- Conclus chaque sous-section par : CONFORME / NON CONFORME / À VÉRIFIER
- Ne déduis pas la logique interne des modules fermés — vérifie les interfaces

─────────────────────────────────────────────
DIMENSIONS D'ANALYSE
─────────────────────────────────────────────

## P1 — Cohérence config → modules
- Chaque clé de config.yaml est-elle lue par le module correspondant ?
- Les valeurs par défaut hardcodées dans les modules divergent-elles de config.yaml ?
- Exemple : `pipeline.target_width` (1080) est-il bien transmis à video_gen.py ?
- Exemple : `script_generation.temperature` (0.8) est-il bien passé à Gemini ?

## P2 — Guards all-or-nothing
- Si `trend_hunter` échoue, pipeline.py s'arrête-t-il proprement ?
- Si Gemini retourne un JSON malformé, script_gen.py lève-t-il une exception typée ?
- Si LTX-Video échoue (OOM, timeout), les étapes audio et post_prod sont-elles bloquées ?
- Si l'upload YouTube échoue (quota, token expiré), le run est-il marqué en erreur ?
- Y a-t-il un état intermédiaire persisté permettant de reprendre un run partiel ?

## P3 — Propagation du run_id
- Le `run_id` est-il généré une seule fois (pipeline.py) et transmis à tous les stages ?
- Les fichiers de sortie (clips, audio, renders) sont-ils nommés avec le `run_id` ?
- Le manifest JSON (`_write_run_manifest()`) capture-t-il tous les outputs ?

## P4 — Quota YouTube et régulation
- Combien d'unités YouTube API sont consommées par run complet ?
- Y a-t-il un mécanisme de comptage ou de protection du quota ?
- Le scheduler peut-il déclencher plus de runs que le quota autorise sur 24h ?
- En cas de `quotaExceeded` (HTTP 403), le retry est-il géré ?

## P5 — Retry et fallback API
- Gemini : retry en cas de `ResourceExhausted` ou `ServiceUnavailable` ?
- Reddit (PRAW) : fallback si les subreddits sont inaccessibles ?
- YouTube upload : retry sur `ServerError` (5xx) avec backoff exponentiel ?
- Kokoro / LTX-Video : timeout configuré ?

## P6 — Cohérence des déclencheurs
- `scheduler.py` et `dashboard/app.py` appellent-ils `run_pipeline()` avec les mêmes paramètres ?
- Peut-on lancer deux runs en parallèle (race condition sur les fichiers outputs) ?
- Le dashboard peut-il annuler un run en cours ?

─────────────────────────────────────────────
FORMAT DE SORTIE OBLIGATOIRE
─────────────────────────────────────────────
Crée le fichier :
  tasks/audits/resultats/audit_pipeline_spark.md

Structure imposée :
# AUDIT PIPELINE — SPARK — [DATE]
## Résumé exécutif
  Score global : X/10
  🔴 Critiques : X  🟠 Majeures : X  🟡 Mineures : X

## P1 — Cohérence config
## P2 — Guards all-or-nothing
## P3 — run_id
## P4 — Quota YouTube
## P5 — Retry/Fallback
## P6 — Déclencheurs

Chaque problème :
  **[P-XX]** 🔴/🟠/🟡 Titre
  Fichier : chemin:ligne
  Problème : description
  Impact : conséquence concrète
  Statut : CONFIRMÉ / À VÉRIFIER

Confirme dans le chat :
"✅ tasks/audits/resultats/audit_pipeline_spark.md créé
 🔴 X · 🟠 X · 🟡 X"
