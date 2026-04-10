---
modele: sonnet-4.6
mode: agent
contexte: codebase
produit: tasks/audits/resultats/audit_technical_spark.md
derniere_revision: 2026-04-09
creation: 2026-04-09
---

#codebase

Tu es un Senior Security Engineer et Python Expert spécialisé
en systèmes d'automatisation cloud et APIs publiques sensibles.
Tu réalises un audit EXCLUSIVEMENT technique et sécurité sur SPARK.

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
  tasks/audits/resultats/audit_technical_spark.md

Si trouvé, affiche :
"⚠️ Audit technique existant détecté :
 Fichier : tasks/audits/resultats/audit_technical_spark.md
 Date    : [date modification]
 Lignes  : [nombre approximatif]

 [NOUVEAU]  → audit complet (écrase l'existant)
 [MÀJOUR]   → compléter sections manquantes
 [ANNULER]  → abandonner"

Si absent → démarrer directement :
"✅ Aucun audit technique existant. Démarrage..."

─────────────────────────────────────────────
CONTEXTE PROJET
─────────────────────────────────────────────
SPARK publie automatiquement du contenu sur YouTube via OAuth2.
Il interroge l'API Reddit (PRAW) et Google Gemini avec des clés API.
Un compte YouTube (canal) et un compte Google Cloud sont en jeu.
Une compromission peut entraîner : copyright strike, suspension canal,
usage frauduleux des crédits Gemini/Google Cloud, exposition Reddit.

Fichiers sensibles à analyser en priorité :
  core/uploader.py     → OAuth2 flow, token storage, upload YouTube
  core/trend_hunter.py → credentials Reddit PRAW
  core/script_gen.py   → clé Gemini API
  pipeline.py          → load_env(), chargement variables sensibles
  dashboard/app.py     → exposition endpoints HTTP, validation inputs
  .env.example         → template secrets (vérifier pas de vraie valeur)
  .gitignore           → vérifier exclusion de .env, secrets/, token.json

─────────────────────────────────────────────
PÉRIMÈTRE STRICT
─────────────────────────────────────────────
Tu analyses UNIQUEMENT :
- Sécurité des credentials et secrets (API keys, OAuth2 tokens)
- Protection Git (fichiers sensibles exclus du versioning)
- Validation et sanitisation des inputs (topics, titres, descriptions)
- Robustesse async (tâches non awaited, exceptions non catchées)
- Exposition de la surface d'attaque du dashboard FastAPI
- Intégrité des fichiers temporaires (clips, audio, renders)
- Gestion sécurisée des tokens OAuth2 (refresh, stockage, expiration)

Tu n'analyses PAS :
- La logique métier de génération ou de SEO
- L'architecture des modules
- Les performances ou la latence
- La cohérence config → pipeline

─────────────────────────────────────────────
SOURCES — LIRE EN PREMIER
─────────────────────────────────────────────
1. .gitignore                          ← vérifier exclusions secrets
2. .env.example                        ← vérifier absence de vraies valeurs
3. core/uploader.py                    ← OAuth2 flow, token.json, scopes
4. core/trend_hunter.py                ← PRAW credentials depuis env
5. core/script_gen.py                  ← GEMINI_API_KEY depuis env
6. pipeline.py                         ← load_env(), injection env dans modules
7. dashboard/app.py                    ← endpoints publics, validation, CORS
8. scheduler.py                        ← SIGTERM/SIGINT handlers, état persisté

─────────────────────────────────────────────
CONTRAINTES ABSOLUES
─────────────────────────────────────────────
- Cite fichier:ligne pour chaque problème de sécurité
- Si un secret est potentiellement exposé : marquer 🔴 immédiatement
- Ne déduis pas — base-toi uniquement sur le code source Python et YAML
- Écris "À VÉRIFIER" si tu ne peux pas confirmer sans exécution

─────────────────────────────────────────────
DIMENSIONS D'ANALYSE
─────────────────────────────────────────────

## T1 — Secrets et credentials
- Les clés API (Gemini, Reddit) sont-elles chargées exclusivement depuis `os.environ` ?
- Y a-t-il une clé ou secret hardcodé dans le code source ?
- `.env.example` contient-il une vraie valeur ? (ex: clé commençant par `AIza`)
- `secrets/client_secret.json` et `secrets/token.json` sont-ils dans `.gitignore` ?
- Le token OAuth2 YouTube est-il stocké en clair ou chiffré ?
- Le refresh du token est-il sécurisé (rotation, scope minimal) ?

## T2 — Protection Git
- `.env` est-il dans `.gitignore` ?
- `secrets/*.json` est-il dans `.gitignore` ?
- `logs/` et `outputs/` sont-ils exclus pour éviter la fuite de contenu intermédiaire ?
- Y a-t-il un `.github/` avec des fichiers de workflow qui pourraient exposer des secrets ?

## T3 — Validation des inputs
- Le topic passé via CLI (`--topic`) ou dashboard (`POST /generate`) est-il validé ?
  (longueur max, caractères interdits, injection JSON/prompt)
- Le titre YouTube généré par Gemini est-il tronqué à 100 caractères avant upload ?
- La description YouTube est-elle limitée à 5000 caractères (limite API) ?
- Les tags sont-ils validés (max 500 caractères cumulés, pas de tags vides) ?
- Les inputs Reddit (subreddits) sont-ils validés contre une whitelist ?

## T4 — Surface d'attaque dashboard FastAPI
- Le dashboard écoute-t-il sur `0.0.0.0` (accessible réseau) ou `127.0.0.1` (local only) ?
- Y a-t-il une authentification sur les endpoints sensibles (`POST /generate`, `POST /upload`) ?
- Les CORS sont-ils configurés ? Permettent-ils n'importe quelle origine ?
- Les paramètres de routes (`run_id`) sont-ils validés (UUID format) avant usage fichier ?
  (risque path traversal : `../../../../etc/passwd`)
- L'endpoint `GET /video/{run_id}` sert-il des fichiers sans vérification du chemin ?

## T5 — Robustesse async et gestion d'exceptions
- Y a-t-il des `asyncio.create_task()` dont les exceptions sont ignorées ?
- Les appels Gemini, PRAW et YouTube sont-ils protégés par des timeouts ?
- Les erreurs d'I/O fichier (écriture clips, audio) sont-elles catchées avec context ?
- Le scheduler APScheduler logue-t-il explicitement les exceptions des jobs ?

## T6 — Intégrité des fichiers temporaires
- Les fichiers temporaires (clips, audio, renders) sont-ils isolés par `run_id` ?
- Un run concurrent peut-il écraser les fichiers d'un autre run ?
- Les fichiers temporaires sont-ils nettoyés après un run en erreur ?

─────────────────────────────────────────────
FORMAT DE SORTIE OBLIGATOIRE
─────────────────────────────────────────────
Crée le fichier :
  tasks/audits/resultats/audit_technical_spark.md

Structure imposée :
# AUDIT TECHNIQUE & SÉCURITÉ — SPARK — [DATE]
## Résumé exécutif
  Score global : X/10
  🔴 Critiques : X  🟠 Majeures : X  🟡 Mineures : X

## T1 — Secrets et credentials
## T2 — Protection Git
## T3 — Validation des inputs
## T4 — Surface d'attaque dashboard
## T5 — Robustesse async
## T6 — Fichiers temporaires

Chaque problème :
  **[T-XX]** 🔴/🟠/🟡 Titre
  Fichier : chemin:ligne
  Problème : description
  Risque : conséquence sécurité concrète
  Statut : CONFIRMÉ / À VÉRIFIER

Confirme dans le chat :
"✅ tasks/audits/resultats/audit_technical_spark.md créé
 🔴 X · 🟠 X · 🟡 X"
