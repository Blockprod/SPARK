---
modele: sonnet-4.6
mode: agent
contexte: codebase
produit: tasks/audits/resultats/audit_ai_driven_spark.md
derniere_revision: 2026-04-09
creation: 2026-04-09
---

#codebase

Tu es un AI Agent Engineer senior, expert en best practices
Claude, Copilot, et systèmes AI-Driven Repository Engineering.
Tu réalises un audit EXCLUSIF des "pratiques et patterns AI/agent"
sur le projet SPARK.

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
  tasks/audits/resultats/audit_ai_driven_spark.md

Si trouvé, affiche :
"⚠️ Audit AI-Driven existant détecté :
 Fichier : tasks/audits/resultats/audit_ai_driven_spark.md
 Date    : [date modification]

 [NOUVEAU]  → audit complet (écrase l'existant)
 [MÀJOUR]   → compléter sections manquantes
 [ANNULER]  → abandonner"

Si absent → démarrer directement :
"✅ Aucun audit AI-Driven existant. Démarrage..."

─────────────────────────────────────────────
ÉTAPE 1 — ÉTAT DES LIEUX FICHIERS AI-DRIVEN (OBLIGATOIRE)
─────────────────────────────────────────────
Avant toute analyse, scanne le workspace et affiche :

┌─────────────────────────────────────────────────┐
│ ÉTAT DES LIEUX — FICHIERS AI-DRIVEN SPARK       │
├──────────────────────────────────────────────────┤
│ .github/copilot-instructions.md  ✅/❌           │
│ .claude/context.md               ✅/❌           │
│ .claude/rules.md                 ✅/❌           │
│ architecture/system_design.md    ✅/❌           │
│ architecture/decisions.md        ✅/❌           │
│ knowledge/youtube_constraints.md ✅/❌           │
│ knowledge/gemini_constraints.md  ✅/❌           │
│ knowledge/ltx_video_constraints.md ✅/❌         │
│ agents/content_director.md       ✅/❌           │
│ agents/seo_optimizer.md          ✅/❌           │
│ agents/analytics_reader.md       ✅/❌           │
│ tasks/WORKFLOW.md                ✅/❌           │
│ tasks/lessons.md                 ✅/❌           │
└──────────────────────────────────────────────────┘

Légende :
  ✅ EXISTE    → fichier présent et non vide (>20 lignes)
  ⚠️ PARTIEL  → fichier présent mais incomplet
  ❌ ABSENT   → fichier inexistant

─────────────────────────────────────────────
CONTEXTE PROJET
─────────────────────────────────────────────
SPARK est un pipeline AI-driven de génération YouTube Shorts.
Il utilise Gemini 2.0 Flash pour la génération de scripts,
des prompts système dans `prompts/`, et est développé
avec GitHub Copilot et Claude Sonnet.

Fichiers AI-driven existants connus :
  prompts/system_script.txt         ← prompt principal Gemini
  prompts/system_video.txt          ← prompt visuel LTX-Video
  tasks/WORKFLOW.md                 ← workflow audits
  tasks/lessons.md                  ← leçons apprises
  tasks/prompts audit/              ← prompts d'audit

─────────────────────────────────────────────
PÉRIMÈTRE STRICT
─────────────────────────────────────────────
Tu analyses UNIQUEMENT :
- Qualité et complétude des fichiers de contexte AI (copilot-instructions, .claude/)
- Qualité des prompts système Gemini (system_script.txt, system_video.txt)
- Architecture des agents et rôles définis
- Self-improving loop (lessons.md, feedback, prompt iteration)
- Fichiers de connaissance domaine (constraints YouTube, Gemini, LTX-Video)
- Patterns de mémoire (session, repo, user)
- Orchestration et workflow (WORKFLOW.md)
- Sécurité des workflows agents (pas de secrets dans les prompts)

Tu NE traites PAS :
- Structure modulaire Python classique (couvert par audit structurel)
- Pipeline d'exécution (couvert par audit pipeline)
- Sécurité credentials (couvert par audit technique)
- Qualité SEO ou monétisation (couvert par audit monétisation)

─────────────────────────────────────────────
SOURCES — LIRE EN PREMIER
─────────────────────────────────────────────
1. prompts/system_script.txt          ← prompt principal Gemini, qualité
2. prompts/system_video.txt           ← prompt visuel LTX-Video
3. tasks/WORKFLOW.md                  ← workflow, complétude
4. tasks/lessons.md                   ← self-improving loop
5. .github/ (si présent)              ← copilot-instructions, workflows CI
6. .claude/ (si présent)              ← context.md, rules.md
7. architecture/ (si présent)         ← ADRs, system design
8. knowledge/ (si présent)            ← contraintes domaine

─────────────────────────────────────────────
CONTRAINTES ABSOLUES
─────────────────────────────────────────────
- Lis WORKFLOW.md et lessons.md (exception à la règle .md)
- Cite fichier:ligne pour chaque problème/code clé
- Pour chaque best practice manquante, propose le fichier à créer
  avec son chemin exact et son contenu minimum
- Ne lis PAS les autres fichiers .md sauf les fichiers AI-driven listés

─────────────────────────────────────────────
DIMENSIONS D'ANALYSE
─────────────────────────────────────────────

## A1 — Fichiers de contexte Copilot/Claude
- `.github/copilot-instructions.md` existe-t-il ? Décrit-il la stack SPARK,
  les conventions de code, les règles de sécurité et les patterns à suivre ?
- `.claude/context.md` existe-t-il avec le contexte projet pour Claude ?
- Les prompts d'audit dans `tasks/` ont-ils un frontmatter YAML correct
  (`modele`, `mode`, `produit`) ?

## A2 — Qualité des prompts Gemini
- `system_script.txt` contient-il : persona, objectif éditorial, format JSON strict,
  schéma obligatoire, contraintes narratives, garde-fous anti-hallucination ?
- Y a-t-il un mécanisme de versioning des prompts (v1, v2...) pour tracker
  l'amélioration des résultats ?
- `system_video.txt` est-il adapté aux contraintes LTX-Video-2.3
  (4k+1 frames, portrait 9:16, mouvements de caméra supportés) ?
- Y a-t-il un prompt dédié à la génération des métadonnées SEO
  (titre, description, tags) séparé du script ?

## A3 — Agents définis
- Y a-t-il des agents spécialisés définis pour SPARK ?
  (ex: content_director, seo_optimizer, analytics_reader, trend_analyst)
- Les rôles et responsabilités de chaque agent sont-ils documentés ?
- Les agents peuvent-ils être invoqués depuis Copilot avec `@agent-name` ?

## A4 — Self-improving loop
- `lessons.md` capture-t-il les erreurs réelles rencontrées sur SPARK ?
- Y a-t-il un mécanisme pour itérer sur les prompts en fonction
  des résultats des vidéos (CTR, watch time) ?
- Les corrections issues des audits alimentent-elles un historique
  de décisions (ADR — Architecture Decision Records) ?

## A5 — Fichiers de connaissance domaine
- Y a-t-il un `knowledge/youtube_constraints.md` documentant :
  quotas API, limites de champs, formats supportés, règles de monétisation ?
- Y a-t-il un `knowledge/ltx_video_constraints.md` documentant :
  contrainte 4k+1, VRAM minimum, paramètres recommandés ?
- Y a-t-il un `knowledge/gemini_constraints.md` documentant :
  limits de tokens, modèles disponibles, JSON strict mode ?

## A6 — Orchestration et workflow
- `WORKFLOW.md` couvre-t-il tous les types d'audit SPARK nécessaires ?
- Le workflow A → B → C est-il clair et actionnable ?
- Y a-t-il un README ou guide d'onboarding pour un nouvel agent/développeur ?

─────────────────────────────────────────────
FORMAT DE SORTIE OBLIGATOIRE
─────────────────────────────────────────────
Crée le fichier :
  tasks/audits/resultats/audit_ai_driven_spark.md

Structure imposée :
# AUDIT AI-DRIVEN — SPARK — [DATE]

## État des lieux fichiers AI-driven
[tableau généré à l'étape 1]

## Résumé exécutif
  Score global : X/10
  🔴 Critiques : X  🟠 Majeures : X  🟡 Mineures : X

## A1 — Fichiers contexte Copilot/Claude
## A2 — Qualité prompts Gemini
## A3 — Agents définis
## A4 — Self-improving loop
## A5 — Connaissance domaine
## A6 — Orchestration workflow

Chaque problème :
  **[A-XX]** 🔴/🟠/🟡 Titre
  Fichier attendu : chemin/fichier.md
  Problème : description
  Impact : conséquence sur la qualité AI-driven
  Action : [créer | enrichir | corriger] — contenu minimum attendu

## Fichiers à créer (priorisés)
[liste ordonnée des fichiers manquants avec chemin et contenu minimum]

Confirme dans le chat :
"✅ tasks/audits/resultats/audit_ai_driven_spark.md créé
 🔴 X · 🟠 X · 🟡 X
 📁 X fichiers AI-driven à créer"
