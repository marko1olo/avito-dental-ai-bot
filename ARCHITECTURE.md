# Avito Dental AI Bot — Architecture Specification

## 1. 4-Stage Veto Pipeline
Every incoming user prompt passes through 4 deterministic gates before LLM invocation.

1. **Gate 1 (Intent Classifier):** Filters spam, off-topic requests, and non-dental inquiries.
2. **Gate 2 (Price Band Estimator):** Replaces exact claims with certified clinical price ranges.
3. **Gate 3 (Medical Liability Veto):** Blocks all diagnostic assertions (Compliance with FZ № 323).
4. **Gate 4 (Anti-Injection Sanitizer):** Strips system prompt extraction vectors.
