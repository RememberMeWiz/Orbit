# Behavioral Evaluation Harness

This module houses the golden evaluation scenarios and test harness for Orbit.

## Purpose
Proves that Orbit:
1. **Intervenes appropriately** when high-value context exists (`INTERVENE`).
2. **Maintains correct silence** when intervention is unnecessary or intrusive (`SILENT`).
3. **Respects permission boundaries** and fails closed when unauthorized (`BLOCKED`).

## Structure
- `scenarios/`: Structured scenario vectors (target: 40-60 golden scenarios).
- `harness/`: Scenario execution engine that evaluates `:core` policy decisions.
- `reports/`: Transient run outputs and scenario pass/fail metrics.
