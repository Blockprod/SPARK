---
type: guide
projet: SPARK
stack: Python 3.11 · Gemini 2.0 Flash · LTX-Video-2.3 · Kokoro ONNX · FFmpeg · FastAPI · YouTube Data API v3
derniere_revision: 2026-04-09
creation: 2026-04-09
---

# WORKFLOW — Audit → Plan → Corrections
# SPARK — Pipeline automatisé de génération YouTube Shorts

Chaque audit suit le même pipeline en **3 étapes** :

| Étape | Prompt | Mode | Produit |
|:---:|---|:---:|---|
| **A** | `audit_<type>_prompt.md` | Agent | `tasks/audits/resultats/audit_<type>_spark.md` |
| **B** | `corrections/generate_action_plan_prompt.md` | Agent | `tasks/plans/PLAN_ACTION_<type>_[DATE].md` |
| **C** | `corrections/execute_corrections_prompt.md` | Agent | corrections appliquées · ⏳ → ✅ |

> Toujours exécuter **A → B → C** dans l'ordre strict.
> Ne jamais lancer B sans avoir l'audit A complet.

---

## AUDITS DISPONIBLES

| # | Audit | Dimension | Mode A | Fichier prompt |
|:---:|---|---|:---:|---|
| 1 | [Monétisation](#1--monétisation) | Qualité contenu · SEO · Scheduling · Feedback loop · Sécurité compte | Agent | `tasks/prompts audit/audit_monetisation.prompt.md` |
| 2 | [Structurel](#2--structurel) | Architecture modules · SRP · Couplage · Dette technique | Agent | `tasks/audits/code/audit_structural_prompt.md` |
| 3 | [Pipeline](#3--pipeline) | Cohérence config→pipeline→upload · Guards · Gestion erreurs · Quota API | Agent | `tasks/audits/code/audit_pipeline_prompt.md` |
| 4 | [Technique & Sécurité](#4--technique--sécurité) | Credentials OAuth2 · Secrets Git · Async · Robustesse · Validation inputs | Agent | `tasks/audits/code/audit_technical_prompt.md` |
| 5 | [AI-Driven](#5--ai-driven-file-engineering) | Fichiers agents · copilot-instructions · prompts · self-improving loop | Agent | `tasks/audits/methode/audit_ai_driven_prompt.md` |

---

## `1 · MONÉTISATION`

> Qualité contenu viral · SEO YouTube Shorts · Audio/Vidéo · Scheduling stratégique · Feedback loop analytics · Protection compte

**Produit A** : `tasks/audits/resultats/audit_monetisation_spark.md`

**A — Audit**
```
#file:tasks/audits/code/audit_monetisation.prompt.md
Lance cet audit sur le workspace.
```

**B — Plan d'action**
```
#file:tasks/corrections/generate_action_plan_prompt.md
Génère le plan d'action depuis l'audit disponible.
```

**C — Exécution**
```
#file:tasks/corrections/execute_corrections_prompt.md
Démarre l'exécution du plan d'action disponible.
```

---

## `2 · STRUCTUREL`

> Architecture modules · Couplage core/ dashboard/ · SRP · Doublons fonctionnels · Entrypoints

**Produit A** : `tasks/audits/resultats/audit_structural_spark.md`

**A — Audit**
```
#file:tasks/audits/code/audit_structural_prompt.md
Lance cet audit sur le workspace.
```

**B — Plan d'action**
```
#file:tasks/corrections/generate_action_plan_prompt.md
Génère le plan d'action depuis l'audit disponible.
```

**C — Exécution**
```
#file:tasks/corrections/execute_corrections_prompt.md
Démarre l'exécution du plan d'action disponible.
```

---

## `3 · PIPELINE`

> Cohérence config.yaml → pipeline.py → chaque core/ module · Guards all-or-nothing · Quota YouTube API · Retry/fallback

**Produit A** : `tasks/audits/resultats/audit_pipeline_spark.md`

**A — Audit**
```
#file:tasks/audits/code/audit_pipeline_prompt.md
Lance cet audit sur le workspace.
```

**B — Plan d'action**
```
#file:tasks/corrections/generate_action_plan_prompt.md
Génère le plan d'action depuis l'audit disponible.
```

**C — Exécution**
```
#file:tasks/corrections/execute_corrections_prompt.md
Démarre l'exécution du plan d'action disponible.
```

---

## `4 · TECHNIQUE & SÉCURITÉ`

> OAuth2 token sécurisé · Secrets hors Git · Validation inputs Gemini/Reddit · Async safety · Gestion erreurs

**Produit A** : `tasks/audits/resultats/audit_technical_spark.md`

**A — Audit**
```
#file:tasks/audits/code/audit_technical_prompt.md
Lance cet audit sur le workspace.
```

**B — Plan d'action**
```
#file:tasks/corrections/generate_action_plan_prompt.md
Génère le plan d'action depuis l'audit disponible.
```

**C — Exécution**
```
#file:tasks/corrections/execute_corrections_prompt.md
Démarre l'exécution du plan d'action disponible.
```

---

## `5 · AI-DRIVEN (FILE ENGINEERING)`

> État des fichiers AI-Driven · copilot-instructions · agents/ · architecture/ · knowledge/ · self-improving loop SPARK

**Produit A** : `tasks/audits/resultats/audit_ai_driven_spark.md`

**A — Audit**
```
#file:tasks/audits/methode/audit_ai_driven_prompt.md
Lance cet audit sur le workspace.
```

**B — Plan d'action**
```
#file:tasks/corrections/generate_action_plan_prompt.md
Génère le plan d'action depuis l'audit disponible.
```

**C — Exécution**
```
#file:tasks/corrections/execute_corrections_prompt.md
Démarre l'exécution du plan d'action disponible.
```

---

## ORDRE RECOMMANDÉ

Pour un premier passage complet :

```
1 → Monétisation    (priorité revenu, impact direct)
2 → Structurel      (base technique saine)
3 → Pipeline        (cohérence end-to-end)
4 → Technique       (sécurité compte YouTube)
5 → AI-Driven       (optimisation workflow agent)
```

---

## DOSSIERS DE SORTIE

| Dossier | Contenu |
|---|---|
| `tasks/audits/resultats/` | Résultats bruts des audits (Étape A) |
| `tasks/plans/` | Plans d'action générés (Étape B) |

*Créer ces dossiers si absents — ils sont dans `.gitignore` pour éviter les résultats intermédiaires en repo.*
