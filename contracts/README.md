# Shared Technical Contracts

This directory contains the authoritative, versioned schema definitions and test fixtures for Orbit.

## Structure
- `schemas/`: Machine-readable JSON Schemas (e.g., `event.schema.json`, `memory.schema.json`, `candidate_intervention.schema.json`, `action.schema.json`, `permission.schema.json`).
- `fixtures/`: Golden test vectors and serialized examples validating schema conformity.

## Governance Rules
1. **Single Source of Truth**: All domain models in `core/` and Android models in `android/` must conform to contracts defined here.
2. **Architecture TL Ownership**: Contract modifications require explicit approval from the Architecture Team Lead.
3. **Immutability & Versioning**: Schema changes require bumping `schema_version`. Breaking changes require a major version increment.
