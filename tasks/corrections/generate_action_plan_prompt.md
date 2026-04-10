---
modele: sonnet-4.6
mode: agent
contexte: codebase
produit: tasks/plans/PLAN_ACTION_[NOM_AUDIT]_[DATE].md
derniere_revision: 2026-04-09
creation: 2026-04-09
---

#codebase

Je suis le chef de projet SPARK.

Lit tous les fichiers `*.md` présents dans le dossier
`tasks/audits/resultat/` et affiche-les numérotés.
Ne pas scanner ailleurs dans le workspace.

Si le dossier est vide ou absent, afficher :
"Aucun audit disponible dans tasks/audits/resultat/ — lance d'abord une étape A."

Demande : "Quel(s) audit(s) utiliser ?
[TOUS] ou [1][2]..."

Puis génère dans `tasks/plans/` le fichier plan
en nommant le fichier d'après l'audit source :
  PLAN_ACTION_[NOM_AUDIT]_[DATE].md

Exemple : audit source = `audit_structural_spark.md`
  → `tasks/plans/PLAN_ACTION_audit_structural_spark_2026-04-09.md`

Exemple : audit source = `audit_pipeline_spark.md`
  → `tasks/plans/PLAN_ACTION_audit_pipeline_spark_2026-04-09.md`

─────────────────────────────────────────────
STRUCTURE OBLIGATOIRE DU FICHIER
─────────────────────────────────────────────
# PLAN D'ACTION — SPARK — [DATE]
Sources : [audits utilisés]
Total : 🔴 X · 🟠 X · 🟡 X · Effort estimé : X jours

## PHASE 1 — CRITIQUES 🔴
## PHASE 2 — MAJEURES 🟠
## PHASE 3 — MINEURES 🟡

Pour chaque correction :
### [C-XX] Titre
Fichier : chemin/fichier.py:ligne
Problème : [description]
Correction : [ce qui doit être fait exactement]
Validation :
  .venv\Scripts\python.exe -c "import ast;
  ast.parse(open('chemin/fichier.py').read()); print('OK')"
  # Attendu : [résultat attendu]
Dépend de : [C-XX ou Aucune]
Statut : ⏳

## SÉQUENCE D'EXÉCUTION
[ordre tenant compte des dépendances]

## CRITÈRES PASSAGE EN PRODUCTION
- [ ] Zéro 🔴 ouvert
- [ ] Pipeline end-to-end testé en dry-run (sans --upload)
- [ ] Upload YouTube testé sur une vidéo de test (privacy: private)
- [ ] Dashboard FastAPI accessible sur http://127.0.0.1:8000
- [ ] Aucun secret exposé dans le code ou les prompts
- [ ] .env rempli avec vraies valeurs opérationnelles
- [ ] secrets/client_secret.json en place
- [ ] APScheduler démarre sans erreur
- [ ] Logs JSON générés correctement dans logs/

## TABLEAU DE SUIVI
| ID | Titre | Sévérité | Fichier | Effort | Statut | Date |

─────────────────────────────────────────────
RÈGLES
─────────────────────────────────────────────
- Ne modifier aucun fichier de code source dans ce prompt
- Ne jamais introduire de vraie clé API dans le plan
- Un problème dans plusieurs audits = une seule entrée
- Effort inconnu → "À ESTIMER"
- Nommer le fichier plan d'après l'audit source (voir en-tête)
- Fichier compatible avec execute_corrections_prompt.md

Confirme dans le chat uniquement :
"✅ tasks/corrections/plans/PLAN_ACTION_[NOM]_[DATE].md créé
 🔴 X · 🟠 X · 🟡 X · Effort : X jours
 👉 Lance tasks/corrections/execute_corrections_prompt.md pour démarrer."
