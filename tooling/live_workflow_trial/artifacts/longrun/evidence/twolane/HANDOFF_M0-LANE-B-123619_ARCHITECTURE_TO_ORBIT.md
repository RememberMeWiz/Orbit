# HANDOFF_M0-LANE-B-123619_ARCHITECTURE_TO_ORBIT.md

## Header
- Work Item: M0-LANE-B-123619
- From: ARCHITECTURE
- To: ORBIT
- Status: COMPLETE
- Handoff ID: M0-LANE-B-123619-0001
- Sequence: 1

## Summary
Use aging-aware priority scheduling with bounded turns and dependency-aware priority inheritance.
Each runnable lane should have a PM-assigned base priority plus a waiting-age component. HOLD lanes are excluded from scheduling entirely. Time spent on HOLD should not accumulate starvation credit because PM deliberately suspended that lane. When released, it re-enters the runnable pool without an artificial priority burst.
Choose the runnable lane with the highest effective priority. Among lanes with equal effective priority, choose the one that has waited longest since its last completed window turn. After one bounded interaction, return that lane to the scheduling pool behind equally eligible peers. A lane may retain the window only across an operation that must remain atomic to preserve workflow correctness, not merely because more conversational work remains.
Strict priority without aging is unsafe. A continuous stream of newly runnable high-priority lanes could starve ordinary lanes indefinitely. Orbit should therefore guarantee eventual service through monotonic aging or a configured maximum eligible-wait bound.
Orbit must also guard against priority inversion. If a high-priority lane is blocked on an artifact, approval, or result owned by a lower-priority lane, temporarily elevate that dependency-owning lane enough to clear the blocker. Unrelated medium-priority lanes should not repeatedly preempt it. The inherited priority must disappear once the dependency is resolved.
Orbit should explicitly flag a fairness fault when a continuously runnable, non-HOLD lane exceeds the maximum allowed wait, or when a high-priority lane remains blocked because its dependency owner is repeatedly denied the window.
The governing policy is: PM priority determines urgency, waiting age guarantees eventual service, HOLD removes eligibility, bounded turns prevent monopolization, and temporary dependency inheritance prevents priority inversion.
