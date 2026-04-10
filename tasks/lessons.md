# SPARK — Leçons apprises
**Création :** 2026-04-09   (Self-Improvement Loop)

> Lire ce fichier au début de chaque session de développement SPARK.
> Mettre à jour après toute correction ou incident.
> Chaque entrée = un pattern d'erreur à ne plus reproduire.

---

## L-01 · Clé API Gemini exposée dans `.env.example`

**Contexte** : Première préparation du commit Git, `.env.example` contenait la vraie clé Gemini.
**Erreur** : La clé `AIzaSyAUdogIIbGavH9gvZi7SvteGKcdfz9tRbw` était présente en clair dans un fichier prévu pour être versionné.
**Règle** : Toujours inspecter `.env.example` avant `git add` — remplacer toute vraie valeur par `YOUR_XXX_HERE`. Après exposition, révoquer immédiatement la clé sur https://aistudio.google.com/app/apikey et en générer une nouvelle.
**Ref** : `.env.example` — commit `127fbe6`

---

## L-02 · Push rejeté — repo distant non vide

**Contexte** : Premier `git push` vers `https://github.com/Blockprod/SPARK` rejeté (`rejected — fetch first`).
**Erreur** : Le repo distant contenait déjà un commit (README GitHub auto-généré). Un `git push` brut échoue si les historiques divergent.
**Règle** : Avant le premier push sur un repo distant existant, toujours faire `git pull --rebase --allow-unrelated-histories origin main` pour intégrer l'historique distant, puis `git push -u origin main`.
**Ref** : Session Git initiale — commit `127fbe6`

---

## L-03 · Projet à la racine, pas dans un sous-dossier

**Contexte** : Lors de la recherche du dossier projet, tentative de chercher un sous-dossier `shorts-engine/`.
**Erreur** : Le projet est directement à `C:\Users\averr\SPARK` — pas de sous-dossier intermédiaire.
**Règle** : La racine de travail est `C:\Users\averr\SPARK`. Tous les chemins relatifs dans `config.yaml`, `pipeline.py` et les scripts doivent être résolus depuis cette racine.
**Ref** : `config.yaml` → `paths.*`, `pipeline.py` → `load_config()`

---

## L-04 · `.env` non créé — pipeline ne démarre pas

**Contexte** : Le fichier `.env` (copie de `.env.example` avec vraies valeurs) doit être créé manuellement avant toute exécution.
**Erreur** : Sans `.env`, `load_dotenv()` dans `pipeline.py` ne charge aucune variable — `GEMINI_API_KEY`, `REDDIT_CLIENT_ID`, etc. sont `None` → erreurs silencieuses ou cryptiques.
**Règle** : Après clonage ou mise à jour du projet, vérifier que `.env` existe avec `Test-Path .env`. Si absent : `Copy-Item .env.example .env` puis remplir toutes les valeurs marquées `YOUR_XXX_HERE`.
**Ref** : `pipeline.py` → `load_env()`, `.env.example`

---

## L-05 · `secrets/client_secret.json` absent — upload YouTube impossible

**Contexte** : Le module `uploader.py` charge `client_secrets_file` depuis la config. Sans ce fichier, l'OAuth2 flow échoue immédiatement.
**Erreur** : `FileNotFoundError` ou `FlowExchangeError` à l'étape upload si `secrets/client_secret.json` n'a pas été téléchargé depuis Google Cloud Console.
**Règle** : Avant tout run avec `--upload`, vérifier `Test-Path secrets/client_secret.json`. Télécharger l'OAuth2 Desktop App credentials depuis Google Cloud Console → APIs → Identifiants → Clé OAuth 2.0 Desktop → Télécharger JSON → renommer `client_secret.json` → placer dans `secrets/`.
**Ref** : `core/uploader.py` → `UploaderConfig.from_mapping()`

---

## L-06 · Quota YouTube Data API — 10 000 unités/jour

**Contexte** : Chaque upload consomme 1 600 unités. Chaque appel `list` consomme 1-100 unités selon les parts demandées.
**Erreur** : Avec 6+ uploads/jour + appels de statut, le quota peut être atteint — résultat : `quotaExceeded` (HTTP 403), uploads silencieusement ignorés.
**Règle** : Configurer un maximum journalier dans `scheduler.py` (ex: `max_runs_per_day: 3`). Monitorer le quota dans Google Cloud Console → Quotas. En cas de `quotaExceeded`, attendre le reset à minuit PST (09h00 Paris).
**Ref** : `core/uploader.py` → `_upload_sync()`, `scheduler.py`

---

## L-07 · LTX-Video-2.3 — contrainte frames 4k+1

**Contexte** : LTX-Video-2.3 exige que le nombre de frames soit de type `4k+1` (ex: 25, 49, 73, 97...).
**Erreur** : Un nombre de frames quelconque (ex: 50, 60) fait lever une `ValueError` dans la pipeline diffusers.
**Règle** : Toujours passer par `_scene_num_frames()` dans `video_gen.py` qui applique la contrainte `(n // 4) * 4 + 1`. Ne jamais calculer `fps × duration` directement sans correction.
**Ref** : `core/video_gen.py` → `_scene_num_frames()`

---

## L-08 · APScheduler — timezone Europe/Paris obligatoire

**Contexte** : Les horaires de publication sont calibrés sur l'audience FR (12h30, 18h30, 21h00).
**Erreur** : Sans `timezone=Europe/Paris` dans `CronTrigger`, les heures sont interprétées en UTC → décalage de +1h (été) ou +2h (hiver) → publications hors fenêtre audience.
**Règle** : Toujours spécifier `timezone="Europe/Paris"` dans chaque `CronTrigger`. Vérifier le décalage actuel avant tout déploiement.
**Ref** : `scheduler.py` → `build_scheduler()`

---

## L-09 · Gemini — JSON strict attendu, markdown interdit

**Contexte** : `script_gen.py` utilise `output_format: strict_json` et parse le retour Gemini avec `json.loads()`.
**Erreur** : Si Gemini entoure la réponse d'un bloc ```json ... ```, le parsing échoue avec `JSONDecodeError`. Cela arrive si `temperature` est trop haute ou si le prompt est insuffisamment contraignant.
**Règle** : Le `system_script.txt` doit explicitement interdire markdown et blocs ``` dans la consigne de sortie. En cas d'erreur de parsing, logger le raw output et réessayer avec `temperature` réduite à 0.5.
**Ref** : `core/script_gen.py`, `prompts/system_script.txt`

---

## L-10 · FFmpeg — chemin absolu requis sur Windows

**Contexte** : `ffmpeg-python` appelle le binaire `ffmpeg` via subprocess.
**Erreur** : Si `ffmpeg` n'est pas dans le PATH Windows ou si un chemin relatif est utilisé, `FileNotFoundError` sur Windows même avec `ffmpeg` installé.
**Règle** : Vérifier `ffmpeg -version` dans le terminal avant tout run. Si absent, installer via `winget install ffmpeg` ou ajouter manuellement à `%PATH%`. Dans `post_prod.py`, utiliser `ffmpeg.run(cmd, quiet=True)` avec gestion d'exception explicite.
**Ref** : `core/post_prod.py`

---

*Mettre à jour ce fichier après chaque incident ou correction non triviale.*
