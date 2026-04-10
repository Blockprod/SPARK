# AUDIT STRATÉGIQUE — SPARK shorts-engine
## Objectif : vérifier l'alignement du projet avec un pipeline de monétisation YouTube Shorts maximal

---

## CONTEXTE PROJET

Tu analyses le projet **SPARK** (`C:\Users\averr\SPARK`), un pipeline automatisé de génération de YouTube Shorts.

**Stack technique actuelle :**
- `core/trend_hunter.py` — Google Trends (geo=FR, 7 jours) + Reddit scraping (pytrends + PRAW)
- `core/script_gen.py` — Script Gemini 2.0 Flash, niche "IA expliquée par l'Histoire", JSON strict
- `core/video_gen.py` — LTX-Video-2.3 local (diffusers), portrait 1080×1920 @ 24fps
- `core/audio_gen.py` — TTS Kokoro ONNX local (voix FR), stub Voxtral présent
- `core/post_prod.py` — FFmpeg concat + mix audio + sous-titres SRT via pysubs2
- `core/uploader.py` — YouTube Data API v3, OAuth2, upload résumable par chunks 8MB
- `pipeline.py` — Orchestrateur async 6 stages, CLI (`--topic`, `--upload`, `--publish-at`)
- `scheduler.py` — APScheduler cron, heures FR peak : 12h30 / 18h30 / 21h00 (Europe/Paris)
- `dashboard/app.py` — FastAPI + SSE temps réel, 7 endpoints dont `/trends`, `/generate`, `/upload`
- `config.yaml` — scoring weights Google(0.35 volume + 0.30 momentum) + Reddit(0.20 mentions + 0.15 engagement)
- `prompts/system_script.txt` — Hook fort, progression scènes courtes, CTA discret, output JSON

**Ce qui N'existe PAS encore :**
- Aucun module analytics / feedback loop (pas de lecture YouTube Analytics API)
- Pas de déduplication des topics déjà produits
- Pas de génération de thumbnail
- Pas de scoring SEO pré-upload
- Pas d'intégration 11Labs / ElevenLabs TTS (expressivité émotionnelle)
- Pas de tracking CTR / watch time / rétention par vidéo
- Pas de détection de trends spécifiques YouTube Shorts (Shorts feed, TikTok cross-signal)

---

## MISSION D'AUDIT

Pour chacune des **6 dimensions** ci-dessous, tu dois :
1. **Évaluer** l'implémentation actuelle dans SPARK (présente, partielle ou absente)
2. **Scorer** l'alignement de 0 à 10
3. **Identifier** les risques concrets sur la monétisation si le gap n'est pas comblé
4. **Proposer** des actions concrètes avec fichiers à modifier/créer dans SPARK

---

### DIMENSION 1 — Qualité et viralité du contenu

**Questions :**
- Le `system_script.txt` est-il optimisé pour générer des hooks suffisamment forts pour les 3 premières secondes ? (retention critique sur Shorts)
- La niche "IA expliquée par l'Histoire" est-elle suffisamment différenciée et monétisable ? (CPM FR moyen pour cette niche ?)
- Le script génère-t-il des appels à l'émotion (surprise, curiosité, peur FOMO) ou reste-t-il trop didactique ?
- La limite 50-60 secondes est-elle correctement appliquée scène par scène pour éviter les cutoffs YouTube ?
- Présence d'un mécanisme de variation de style (ne pas produire des Shorts identiques structurellement) ?

**Fichiers à inspecter :** `prompts/system_script.txt`, `config.yaml` → `script_generation`, `core/script_gen.py`

---

### DIMENSION 2 — Qualité audio/vidéo et rétention

**Questions :**
- Kokoro ONNX local — quelle expressivité réelle comparée à 11Labs ? Impact sur le watch time ?
- LTX-Video-2.3 — stabilité production (artifacts, cohérence inter-scènes) ? Comparaison Runway/Kling/Pika ?
- `_extract_last_frame()` dans `video_gen.py` garantit-il la continuité visuelle inter-scènes ?
- Les sous-titres générés par `post_prod.py` respectent-ils les bonnes pratiques Shorts ? (taille police, position, durée d'affichage)
- Le mix audio final (-18dBFS ambient / -3dBFS voix) est-il conforme aux specs YouTube mastering ?
- Y a-t-il un fallback si LTX-Video échoue (GPU insuffisant, VRAM trop faible) ?

**Fichiers à inspecter :** `core/audio_gen.py`, `core/video_gen.py`, `core/post_prod.py`

---

### DIMENSION 3 — SEO et discoverabilité

**Questions :**
- Les `youtube_title`, `youtube_description`, `youtube_tags` sont-ils générés avec des contraintes SEO explicites dans le prompt Gemini ?
- Le titre respecte-t-il la limite de 100 caractères et inclut-il un mot-clé primaire en début ?
- La description inclut-elle les 3 premières lignes visibles sans "voir plus" avec des mots-clés ?
- Les hashtags respectent-ils la règle 3-5 hashtags Shorts spécifiques (ex: #Shorts, #IA, #Histoire) ?
- Y a-t-il un mécanisme de génération de thumbnail dynamique (frame extraite + overlay texte) ?
- Le `category_id` YouTube est-il correctement configuré pour la niche (Science & Tech = 28) ?

**Fichiers à inspecter :** `prompts/system_script.txt`, `core/script_gen.py` → validation JSON, `core/uploader.py`, `config.yaml`

---

### DIMENSION 4 — Scheduling et cadence de publication

**Questions :**
- Les horaires 12h30 / 18h30 / 21h00 (Paris) sont-ils basés sur des données d'audience FR réelles pour la niche Tech/Histoire ?
- La fréquence actuelle (potentiellement 3 Shorts/jour) est-elle soutenable sans pénalité algorithme YouTube (spam detection) ?
- Y a-t-il un mécanisme de régulation (ex: max N Shorts/semaine configurable) ?
- Le `--publish-at` dans `pipeline.py` est-il correctement passé à l'API YouTube (champ `publishAt` avec format RFC3339) ?
- La gestion des erreurs d'upload est-elle robuste ? (retry, quota YouTube 10k unités/jour)
- Y a-t-il un journal persistant des publications pour éviter re-upload accidentel ?

**Fichiers à inspecter :** `scheduler.py`, `pipeline.py`, `core/uploader.py`, `config.yaml`

---

### DIMENSION 5 — Feedback loop et amélioration continue

**Questions :**
- Y a-t-il une intégration YouTube Analytics API (`youtubeAnalytics.googleapis.com`) pour lire CTR, watch time, rétention ?
- Les logs JSON dans `logs/run_*.jsonl` capturent-ils des métriques de run exploitables pour diagnostic ?
- Y a-t-il un mécanisme de scoring post-publication (comparer topics → vidéos → vues réelles) ?
- Le `trend_hunter.py` peut-il ingérer des sujets performants passés pour pondérer sa sélection ?
- Le dashboard `dashboard/app.py` affiche-t-il des analytics post-publication (vues, likes, commentaires) ?
- Y a-t-il un système de blacklist automatique pour les topics qui ont mal performé ?

**Fichiers à inspecter :** `core/trend_hunter.py`, `dashboard/app.py`, `pipeline.py` → `_write_run_manifest()`

---

### DIMENSION 6 — Sécurité du compte et conformité YouTube

**Questions :**
- Y a-t-il une déduplication des topics pour éviter de publier des Shorts quasi-identiques dans la même semaine ?
- Le contenu généré est-il soumis à une validation anti-copyright automatique (musique, images, citations) ?
- Les visuels LTX-Video-2.3 sont-ils 100% originaux ou y a-t-il un risque de Content ID strike ?
- Le `system_script.txt` inclut-il des garde-fous explicites (pas contenu haineux, pas fausses affirmations historiques) ?
- La gestion des tokens OAuth2 (`token.json`) est-elle sécurisée (valeur dans `secrets/`, exclu de git) ?
- Y a-t-il un mécanisme d'alerte si une vidéo reçoit un copyright claim ou un strike ?
- La limite quota YouTube API (10k units/day) est-elle monitorée et protégée ?

**Fichiers à inspecter :** `.gitignore`, `core/uploader.py`, `prompts/system_script.txt`, `core/trend_hunter.py`

---

## FORMAT DE RÉPONSE ATTENDU

Pour chaque dimension, produire un bloc structuré :

```
### DIMENSION X — [Nom]
Score : X/10
Statut : ✅ Bien couvert | ⚠️ Partiel | ❌ Absent

Constats :
- [point précis lié au code]
- [point précis lié au code]

Risques si non corrigé :
- [impact concret sur monétisation/compte]

Actions prioritaires :
1. [fichier à modifier / créer] — [ce qu'il faut faire exactement]
2. ...
```

---

## SYNTHÈSE FINALE ATTENDUE

Après les 6 dimensions, produire :

1. **Score global** (moyenne pondérée, avec poids suggérés : contenu 25%, audio/vidéo 20%, SEO 15%, scheduling 10%, feedback 20%, sécurité 10%)
2. **Top 3 actions** qui auraient le plus grand impact sur le revenu mensuel estimé
3. **Roadmap priorisée** sur 4 semaines (Semaine 1 → Semaine 4) pour transformer SPARK en machine de monétisation robuste
4. **Estimation revenu potentiel** (fourchette basse/haute) après implémentation complète, basée sur : niche FR Tech/Histoire, CPM estimé, cadence réaliste, watch time moyen Shorts FR

---

## INSTRUCTIONS D'EXÉCUTION

Ce prompt est conçu pour être utilisé de deux façons :

### Option A — Via GitHub Copilot (mode Agent dans VS Code)
1. Ouvrir le chat Copilot en mode **Agent**
2. Coller ce fichier ou l'ouvrir avec `@workspace`
3. Demander : *"Exécute l'audit complet défini dans `tasks/audit_monetisation.prompt.md` en lisant tous les fichiers référencés"*

### Option B — Via Claude / ChatGPT (copier-coller)
1. Copier ce fichier entier
2. Ajouter en fin de message les contenus des fichiers clés :
   - `prompts/system_script.txt`
   - `config.yaml`
   - `core/trend_hunter.py` (lignes 1-100)
   - `core/uploader.py` (lignes 1-80)
   - `scheduler.py`
3. Demander l'audit complet

---

*Généré pour SPARK v1.0 — Pipeline shorts-engine — Dossier : `C:\Users\averr\SPARK`*
