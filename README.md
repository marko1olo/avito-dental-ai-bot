# 🤖 Avito Dental AI Bot — 4-Stage Veto Pipeline & Clinical Lead Gen

[![Live Demo](https://img.shields.io/badge/Live_Showcase-GitHub_Pages-f59e0b?style=for-the-badge&logo=github)](https://marko1olo.github.io/avito-dental-ai-bot/)
[![PWA Ready](https://img.shields.io/badge/PWA-Installable-22c55e?style=for-the-badge&logo=pwa)](https://marko1olo.github.io/avito-dental-ai-bot/manifest.json)
[![AI Index](https://img.shields.io/badge/LLM_Search-llms.txt-38bdf8?style=for-the-badge)](https://marko1olo.github.io/avito-dental-ai-bot/llms.txt)
[![Python](https://img.shields.io/badge/Python-3.11+-3776AB?style=for-the-badge&logo=python)](https://www.python.org/)
[![Regulatory Safe](https://img.shields.io/badge/Compliance-FZ_323_&_FZ_38-22c55e?style=for-the-badge)](https://www.consultant.ru/)

A production-grade, zero-hallucination patient acquisition bot for medical marketplaces (Avito, ProDoctorov, Yandex.Maps) featuring a deterministic 4-stage safety veto layer, clinical price band estimators, and direct webhook ingestion into DENTE CRM.

---

## 🏛️ 4-Stage Safety Veto Pipeline

```mermaid
graph TD
    In[Patient Marketplace Message] --> Stage1[Stage 1: Intent & Category Classifier]
    Stage1 --> Stage2[Stage 2: Deterministic Price Band Engine]
    Stage2 --> Stage3[Stage 3: Medical Liability Veto Filter (No Diagnoses)]
    Stage3 --> Stage4[Stage 4: Anti-Injection Sanitizer & CRM Exporter]
    Stage4 --> CRM[(DENTE Dental CRM)]
```

---

## 🔬 Safety Guardrails & Compliance

- **No Remote Diagnoses:** Strict prohibition of definitive medical assertions (compliance with Russian Healthcare Law No. 323-FZ).
- **Price Band Guardrails:** Quotes dynamic service estimate ranges with consultation caveats.
- **Anti-Prompt-Injection Shield:** Zero leakage of underlying prompt templates or system instructions.
- **CRM Ingestion:** Automatic creation of patient consultation leads in DENTE CRM with category tags.

---

### 👨‍💻 Lead Architect
**Адольф Петушков (Adolf Petushkov)** — High-Concurrency Systems & Clinical AI Architecture.  
GitHub: [@marko1olo](https://github.com/marko1olo)
