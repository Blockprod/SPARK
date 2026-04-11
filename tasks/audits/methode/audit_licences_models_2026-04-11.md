---
title: "Audit licences — LTX-Video-2.3 et Kokoro-82M"
creation: 2026-04-11 à 13:31
scope: modèles de génération (vidéo + audio)
verdict: CONFORME sous conditions documentées
---

# Audit licences — LTX-Video-2.3 & Kokoro-82M

> Contexte SPARK : pipeline de production de Shorts YouTube monétisés, publiés publiquement, revenus annuels < 10 M USD.

---

## 1. Kokoro-82M (`hexgrad/Kokoro-82M`)

**Licence :** Apache 2.0  
**Source :** https://huggingface.co/hexgrad/Kokoro-82M

### Droits accordés (Apache 2.0)
- Usage commercial : ✅ **Librement autorisé**
- Distribution du modèle : ✅ autorisée avec copie de la licence
- Modification : ✅ autorisée
- Attribution obligatoire dans les publications : ❌ **Non requise**

### Statut SPARK
**PLEINEMENT CONFORME. Aucune action requise.**

La licence Apache 2.0 autorise explicitement le déploiement en production commerciale, y compris la génération de contenus distribués publiquement et monétisés. Aucune mention de Kokoro ou de hexgrad n'est requise dans les descriptions YouTube.

### Source fine-tuning (données CC BY)
Le modèle a été entraîné sur des données en partie CC BY 3.0 et CC BY 4.0 (voir tableau dans la model card). La licence Apache 2.0 du modèle en lui-même couvre l'utilisation des poids — les obligations CC BY s'appliquent aux données d'entraînement (non redistribuées) et non aux outputs générés.

---

## 2. LTX-Video (`Lightricks/LTX-Video-2.3`)

**Licence :** LTXV Open Weights License 0.X (custom, Lightricks Ltd.)  
**Date licence :** 15 avril 2025 (applicable à toutes versions ≥ v0.9.6)  
**Source :** https://huggingface.co/Lightricks/LTX-Video/blob/main/LTX-Video-Open-Weights-License-0.X.txt

### Droits accordés (§2)
- Usage pour toute finalité : ✅ sous réserve restrictions Annexe A
- Usage commercial libre : ✅ **pour entités avec CA annuel < 10 M USD**
- Entités ≥ 10 M USD/an : ⚠️ nécessitent une licence commerciale payante auprès de Lightricks

### Restriction critique — Annexe A point (e)
> "To generate or disseminate information and/or content [...] without expressly and intelligibly disclaiming that the information and/or content is machine generated"

**Traduction :** Il est interdit de diffuser du contenu généré par le modèle **sans déclarer explicitement** que ce contenu est généré par machine.

### Statut SPARK
**CONFORME** — la déclaration est déjà assurée automatiquement :
- `containsSyntheticMedia: true` dans le corps du upload YouTube (API v3)
- Mention `"Contenu généré avec l'aide de l'IA."` injectée en fin de description via le prompt système
- `payload["ai_generated"] = True` pour traçabilité interne

### Attribution Lightricks requise dans les publications ?
**Non.** La licence ne requiert pas de mentionner Lightricks, LTX-Video, ni de lien vers le dépôt dans les descriptions YouTube ou ailleurs dans les contenus publiés.

### Autres restrictions (Annexe A)
Les restrictions standard s'appliquent (contenu illégal, deepfakes sans consentement, discrimination, malware, etc.) — sans impact sur le cas d'usage SPARK (Shorts IA éducatifs/divertissants).

---

## 3. Synthèse

| Modèle | Licence | Usage commercial | Attribution requise | Disclosure IA |
|--------|---------|-----------------|--------------------|-----------------------------|
| Kokoro-82M | Apache 2.0 | ✅ Libre | ❌ Non | N/A |
| LTX-Video v0.9.6+ | LTXV OWL 0.X | ✅ Libre < 10M USD/an | ❌ Non | ✅ **Obligatoire** → déjà implémentée |

---

## 4. Recommandations

1. **Aucune modification de pipeline requise** — la disclosure IA est déjà en place.
2. **Surveillance CA annuel** : si les revenus du projet atteignent **10 M USD/an**, contacter Lightricks pour une licence commerciale LTX-Video avant de continuer la production.
3. **Mise à jour licence LTX** : vérifier périodiquement si Lightricks publie une version 1.X de sa licence susceptible de modifier les conditions.
4. Si des nouvelles voix Kokoro nécessitent des données CC BY dans des versions futures, vérifier que les obligations de redistribution ne s'appliquent pas aux poids mis à jour.

---

## 5. Références

- Apache License 2.0 : https://www.apache.org/licenses/LICENSE-2.0
- LTXV Open Weights License 0.X : https://huggingface.co/Lightricks/LTX-Video/blob/main/LTX-Video-Open-Weights-License-0.X.txt
- Kokoro model card : https://huggingface.co/hexgrad/Kokoro-82M
- LTX-Video model card : https://huggingface.co/Lightricks/LTX-Video
