# HANDOFF_M0-WF-B1-LIVE-001_ARCHITECTURE_TO_ORBIT-3.md

## Header
- Work Item: M0-WF-B1-LIVE-001
- From: ARCHITECTURE
- To: ORBIT-3
- Status: COMPLETE
- Handoff ID: M0-WF-B1-LIVE-001-0005
- Sequence: 5

## Summary
Use a Windows named mutex as the authoritative single-writer primitive, with the mutex name derived from a stable Orbit installation/ledger identity. Under the current storage design, where every delivery record lives in one JSON ledger that is rewritten with 
os.replace
, the mutex must initially protect the whole ledger.
The mutex should be acquired before reading a record for transition, held through every durable ledger transition and through Send actuation, and released only after the post-actuation state has been persisted. A second runner must wait or report 
writer-busy
; it must never proceed concurrently.
A named mutex is the best fit because Windows itself arbitrates acquisition atomically across processes, and when the owning process dies the kernel marks the mutex abandoned. There is no lease timer that can expire underneath a merely slow Send operation.
The alternatives are weaker for this design:
The critical transition protocol should therefore be:
acquire mutex -> reload current ledger from disk -> verify expected revision/state -> persist next state -> verify the persisted state/revision -> if transitioning to 
SEND_ACTUATED
, only then perform Send -> persist 
SENT_UNCONFIRMED
, 
DELIVERED
, or 
AMBIGUOUS
 as appropriate -> release mutex.
The durable 
SEND_ACTUATED
 write must complete before the external input is generated. Orbit should not press Send based only on an in-memory transition.
Do not implement time-based ownership expiry.
A fixed lease or heartbeat timeout creates exactly the dangerous failure mode QA identified: a runner that is slow or blocked inside the actuation window can be declared “stale” while it is still alive, allowing another runner to enter and press Send again.
With a Windows named mutex, process death already supplies the safe ownership-expiry mechanism. Windows releases ownership when the process exits and reports the mutex as abandoned to the next waiter. A slow but living holder retains ownership indefinitely.
A waiting runner may use a finite wait only for operator responsiveness. If that wait expires, it should report 
writer-busy
 or equivalent and stop. Timeout must not grant takeover.
When a runner acquires an abandoned mutex, it must treat the durable ledger as the only recovery authority and reload it from disk before doing anything else.
The recovery rules should be:
For the 
SEND_ACTUATED
 guarantee to be meaningful, Orbit must strengthen the persistence boundary enough that “write returned successfully” means the transition was committed before Send. At minimum the transition file should be flushed before actuation and then reopened/re-read or otherwise verified before the input event is issued. The process must not actuate on a state transition that exists only in a Python object or buffered temporary file.
Owner metadata such as PID, process start time, host/session, acquisition timestamp, and runner ID is worth recording for diagnostics, but it must never authorize eviction. PID liveness checks are evidence for a human or log, not a substitute for kernel mutex ownership.
With the ledger as one atomically replaced JSON file, use one mutex for the whole ledger now.
Per-delivery-record locking is not safe with the present storage layout. Two runners operating on different delivery records could each legally own their own record mutex, read the same ledger version, modify different records, and then each replace the whole JSON file. The second replacement can erase the first runner's update. That is a lost-update race even though neither runner touched the other's logical record.
A per-work-item lock has the same defect when several work items share the same JSON ledger. It prevents concurrency within a work item but allows concurrent whole-file replacement across work items.
A whole-ledger mutex prevents both duplicate actuation and lost updates. Its cost is serialization: when Orbit later supervises several work items, a slow Send for one delivery temporarily blocks transitions for every other delivery. That is a throughput limitation, not a correctness failure, and is the right tradeoff for the current M0 representation.
If later concurrency becomes necessary, change the persistence boundary first. Store each delivery record, or each independently transactional shard, in a separately replaceable file/database row. At that point use a named mutex keyed to the delivery record, with any shared index protected separately. Do not introduce per-record locking while all records still share one replace-on-write JSON object.
Realistic Windows ranking for the current design is therefore:
Whole-ledger named mutex: strongest and simplest now.
Dedicated never-replaced lock file using a real Windows file lock: workable fallback, more edge cases.
Revision checking under a real lock: valuable secondary invariant.
Owner-metadata-only lock file: insufficient.
Revision CAS implemented only as JSON read/check/replace: insufficient.
What it still will not prove
A local single-writer mechanism cannot prevent the remote application/service from duplicating one accepted submission, cannot prevent a human from manually pressing Send, and cannot coordinate with any other sender that does not participate in Orbit's mutex.
Recommended narrow claim:
Orbit guarantees that participating Orbit runners on the same Windows installation will issue at most one local Send actuation for a delivery record unless a human explicitly resolves an 
AMBIGUOUS
 record for retry.
That is an at-most-once local-actuation claim, not an exactly-once remote-delivery claim.
Before making an unqualified claim about duplicate external action, run a real multi-process adversarial test on Windows using the production named mutex implementation, production ledger persistence, production CLI runner, and the real accessibility/input actuation path. Do not stub the mutex and do not replace Send with an in-process mock.
Rank the tests by realistic desktop failure modes:
First, concurrent launch race. Start at least two independent Python runner processes against the same 
PENDING_SEND
 record and release them from a synchronization barrier at nearly the same instant. Repeat this many times with scheduler/watchdog-style overlapping starts. Measure the number of actual Send input events, final ledger revision/state, and every runner's mutex acquisition result. Required result: one actuation, one monotonic transition chain, no lost ledger update.
Second, kill immediately before actuation. Terminate the owning runner after staging but before durable 
SEND_ACTUATED
. Start another runner. Required result: takeover is permitted only from the durable pre-actuation state after complete revalidation, with exactly one eventual Send.
Third, kill immediately after durable 
SEND_ACTUATED
 but before the Send input. Start another runner. Required result: the abandoned mutex is acquired, 
SEND_ACTUATED
 becomes 
AMBIGUOUS
, and the replacement runner performs zero Send actuations. This intentionally sacrifices automatic delivery rather than risk duplication.
Fourth, kill immediately after the Send input but before any post-actuation persistence. This is the most important crash test. Required result: on restart the durable record is already 
SEND_ACTUATED
, is reconciled to 
AMBIGUOUS
, and no second Send occurs.
Fifth, kill during the transition from 
SEND_ACTUATED
 to 
SENT_UNCONFIRMED
, and separately during confirmation handling. Required result: every recovered post-actuation state remains non-auto-retryable.
Sixth, simulate a slow living owner. Pause or block the holder after it owns the mutex and before/during actuation for longer than any watchdog's normal patience. Start additional runners. Required result: they wait or return 
writer-busy
; none may evict the holder or actuate.
Seventh, run concurrent deliveries for different records and different work items against the same JSON ledger. Required result with the whole-ledger mutex: serialization with no lost updates. This establishes why per-record mutexes cannot yet be introduced without changing persistence layout.
Eighth, exercise ordinary abnormal Windows exits: console close, unhandled Python exception, runner crash, user logoff/process teardown where practical, and watchdog starting a replacement after apparent failure. Verify abandoned-mutex recovery each time. Orbit must not terminate competing runners as part of the test.
For measurement, use at least three independent evidence sources: durable ledger transition/revision history; timestamped mutex acquisition/abandonment/release records including runner IDs; and an observation outside the runner process that counts actual Send actuations. Prefer a controlled disposable target where the receiving UI records each submitted input, while also instrumenting the local input boundary so a remote duplicate can be distinguished from Orbit pressing Send twice.
Run enough repeated contention trials to make scheduling races routine rather than lucky. Include CPU load and random delays around read, persist, staging, mutex acquisition, and actuation. The pass criterion for the local guarantee is zero trials with more than one Orbit-generated Send actuation for the same delivery record.
Even after that passes, the wording must remain scoped to local Orbit actuation unless the remote service supplies an idempotency key or acknowledgement protocol that itself proves deduplication.
Recommended Claim Wording: Orbit guarantees at-most-once local Send actuation per delivery record among participating Orbit runners on the same Windows installation; it does not guarantee exactly-once remote delivery or prevent independent human or third-party submissions.
