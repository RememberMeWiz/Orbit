# HANDOFF_M0-LANE-A-123619_WORKER_TO_ORBIT.md

## Header
- Work Item: M0-LANE-A-123619
- From: WORKER
- To: ORBIT
- Status: COMPLETE
- Handoff ID: M0-LANE-A-123619-0001
- Sequence: 1

## Summary
The highest-risk interference is a focus race on the single visible ChatGPT conversation.
Lane A can correctly navigate to and verify its own conversation, then Lane B can switch the visible chat before Lane A performs the next consequential action. If Lane A relies on the earlier verification, it could type or Send M0-LANE-A-123619 content into Lane B's conversation, or later collect Lane B's assistant response as if it belonged to Lane A. Separate durable state directories and PM request IDs do not prevent this because the shared resource is the visible desktop UI.
Orbit should treat ChatGPT UI ownership as a single-writer critical section. A lane should acquire the shared UI lock before selecting its conversation and retain that ownership through the complete operation that depends on the selection.
The most important detection check is active-conversation identity immediately before actuation. After acquiring the UI lock, Lane A should focus its expected endpoint, re-read the active-chat identity, and require it to match Lane A's expected conversation. It should perform that check immediately before typing or Send, and independently again before transcript collection.
Orbit should also bind the pending UI action to Lane A's work item and PM request ID in control state. If the visible conversation identity changes or cannot be verified, Lane A should stop with a lane-local wrong-active-chat or equivalent blocked result. It should not retry the action against whatever conversation happens to be visible.
The invariant to enforce is: no lane may type, Send, or collect based on conversation identity observed before it acquired exclusive UI ownership, or after that ownership was lost.
