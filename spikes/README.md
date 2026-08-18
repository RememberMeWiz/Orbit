# Spikes and Technical Experiments

This directory isolates exploratory technical investigations from production codebase modules.

## Isolation Rules
1. **Throwaway Code**: Code written in `spikes/` is exploratory and must never be treated as production implementation.
2. **No Direct Merges**: Spikes may not be merged directly into `core/` or `android/`. Production code must be freshly implemented against approved architectural contracts.
3. **No Secret Ingestion**: Live user data, personal tokens, or production secrets are strictly prohibited.
