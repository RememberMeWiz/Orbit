"""The single-writer guarantee, tested with real processes.

QA's standing objection to Claim 4 was that unit tests with a stubbed lock prove
nothing about concurrency, and it was right. Every contention test below spawns
genuine OS processes, contends on a genuine kernel object, and in the kill tests
uses TerminateProcess so no cleanup code runs at all.

Note the blockers are always *subprocesses*, never a second lock object in this
process. A Windows mutex is re-entrant for the owning thread, so an in-process
blocker would be acquired straight through and the test would prove nothing.
That re-entrancy is harmless for Orbit -- `deliver` acquires once and is not
called recursively -- but it makes an in-process test silently vacuous, which is
worse than no test.

The claim being defended is deliberately narrow:

    Orbit guarantees at-most-once local Send actuation per delivery record among
    participating Orbit runners on the same Windows installation.

It does not claim exactly-once remote delivery, and it does not constrain a
human pressing Send in the same conversation. Those belong in the wording, not
in the harness.
"""
from __future__ import annotations

import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

from standalone.bridge.singlewriter import (
    SingleWriterLock,
    WriterBusy,
    available,
    mutex_name,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
WINDOWS_ONLY = unittest.skipUnless(available(), "single-writer requires Windows")


def script(ledger: Path, timeout: float, body: str) -> str:
    """Build a child program. Each part is dedented separately."""
    head = textwrap.dedent(f"""
        import sys, time
        sys.path.insert(0, r"{REPO_ROOT}")
        from standalone.bridge.singlewriter import SingleWriterLock
        lock = SingleWriterLock(r"{ledger}", timeout_seconds={timeout})
    """)
    return head + textwrap.dedent(body)


def run(ledger: Path, timeout: float, body: str) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, "-c", script(ledger, timeout, body)],
                          capture_output=True, text=True, timeout=120)


def spawn(ledger: Path, timeout: float, body: str) -> subprocess.Popen:
    return subprocess.Popen([sys.executable, "-c", script(ledger, timeout, body)],
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)


HOLD_FOREVER = """
lock.acquire()
print("held", flush=True)
time.sleep(300)
"""


class NamingTests(unittest.TestCase):
    def test_SW_001_the_name_is_derived_from_the_ledger(self):
        with tempfile.TemporaryDirectory() as tmp:
            a = mutex_name(Path(tmp) / "delivery.json")
            b = mutex_name(Path(tmp) / "other.json")
            self.assertNotEqual(a, b)
            self.assertEqual(a, mutex_name(Path(tmp) / "delivery.json"))

    def test_SW_002_the_name_does_not_leak_the_path(self):
        """A mutex name is a global object name any process can enumerate."""
        with tempfile.TemporaryDirectory() as tmp:
            name = mutex_name(Path(tmp) / "delivery.json")
            self.assertNotIn("delivery", name)
            self.assertNotIn(Path(tmp).name, name)
            self.assertTrue(name.startswith("Local\\Orbit-Delivery-"))


class LockBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.ledger = self.dir / "delivery.json"
        self.children = []

    def tearDown(self):
        for child in self.children:
            if child.poll() is None:
                child.kill()
                child.wait(timeout=30)
        self.tmp.cleanup()

    def hold_in_subprocess(self, ledger: Path = None) -> subprocess.Popen:
        """A live holder in another process. The only kind that really blocks."""
        holder = spawn(ledger or self.ledger, 5.0, HOLD_FOREVER)
        self.children.append(holder)
        self.assertEqual(holder.stdout.readline().strip(), "held",
                         holder.stderr.read()[:400] if holder.poll() else "")
        return holder


@WINDOWS_ONLY
class AdversarialTests(LockBase):
    """Real processes. No stubs anywhere in this class."""

    def test_SW_010_a_second_live_process_is_refused(self):
        self.hold_in_subprocess()
        result = run(self.ledger, 1.0, 'print(lock.acquire().reason_code)')
        self.assertEqual(result.stdout.strip(), "writer-busy", result.stderr[:400])

    def test_SW_011_release_hands_it_on(self):
        held = SingleWriterLock(self.ledger, timeout_seconds=2.0)
        held.acquire()
        held.release()
        result = run(self.ledger, 5.0, 'print(lock.acquire().reason_code)')
        self.assertEqual(result.stdout.strip(), "ok", result.stderr[:400])

    def test_SW_012_a_killed_holder_does_not_block_forever(self):
        """TerminateProcess: no finally, no atexit, no cleanup of any kind."""
        holder = self.hold_in_subprocess()
        holder.kill()
        holder.wait(timeout=30)

        after = SingleWriterLock(self.ledger, timeout_seconds=10.0)
        self.assertTrue(after.acquire().acquired)
        after.release()

    def test_SW_013_a_waiter_present_at_the_death_learns_it_was_abandoned(self):
        """Windows only raises this when a handle was already open."""
        holder = self.hold_in_subprocess()
        waiter = spawn(self.ledger, 60.0, """
            print("waiting", flush=True)
            print(lock.acquire().reason_code, flush=True)
        """)
        self.children.append(waiter)
        self.assertEqual(waiter.stdout.readline().strip(), "waiting")
        holder.kill()
        holder.wait(timeout=30)
        self.assertEqual(waiter.stdout.readline().strip(), "recovered-from-abandoned")

    def test_SW_014_a_fresh_runner_after_a_death_gets_no_abandoned_signal(self):
        """Measured, and the reason `recovered` must never be branched on.

        With no handle open at the moment of death the kernel object is
        destroyed, so the next runner creates a brand new one and sees nothing
        unusual. Safety comes from reloading the ledger, not from this flag.
        """
        holder = self.hold_in_subprocess()
        holder.kill()
        holder.wait(timeout=30)

        after = SingleWriterLock(self.ledger, timeout_seconds=10.0)
        outcome = after.acquire()
        self.assertTrue(outcome.acquired)
        self.assertFalse(outcome.recovered)
        after.release()

    def test_SW_015_only_one_of_many_racers_holds_it_at_a_time(self):
        """Eight processes started together, each announcing entry and exit."""
        racers = [spawn(self.ledger, 0.2, """
            out = lock.acquire()
            if out.acquired:
                print("in", flush=True)
                time.sleep(0.6)
                lock.release()
                print("out", flush=True)
            else:
                print(out.reason_code, flush=True)
        """) for _ in range(8)]
        self.children.extend(racers)

        results = [racer.communicate(timeout=120)[0].split() for racer in racers]
        entered = [r for r in results if r and r[0] == "in"]
        busy = [r for r in results if r and r[0] == "writer-busy"]
        self.assertEqual(len(entered) + len(busy), 8, results)
        self.assertGreaterEqual(len(busy), 1, "no contention: this test proved nothing")
        for result in entered:
            self.assertEqual(result, ["in", "out"], "a holder did not finish its section")

    def test_SW_016_a_timeout_never_grants_takeover(self):
        """The whole point. A lease design would break exactly here."""
        self.hold_in_subprocess()
        for _ in range(3):
            result = run(self.ledger, 0.3, """
                out = lock.acquire()
                print(f"{out.acquired}:{out.reason_code}")
            """)
            self.assertEqual(result.stdout.strip(), "False:writer-busy", result.stderr[:300])

    def test_SW_017_the_context_manager_refuses_rather_than_waiting_out(self):
        self.hold_in_subprocess()
        with self.assertRaises(WriterBusy):
            with SingleWriterLock(self.ledger, timeout_seconds=0.3):
                self.fail("entered a lock another process holds")

    def test_SW_018_different_ledgers_do_not_contend(self):
        """Otherwise supervising two work items would serialise them needlessly."""
        self.hold_in_subprocess()
        other = SingleWriterLock(self.dir / "other.json", timeout_seconds=2.0)
        self.assertTrue(other.acquire().acquired)
        other.release()


@WINDOWS_ONLY
class DeliveryIntegrationTests(LockBase):
    """The lock is enforced by `deliver`, not merely available beside it."""

    def build(self):
        from standalone.bridge import DeliveryLedger
        from standalone.tests.test_chatgpt_adapter import StubDriver, build
        driver = StubDriver()
        ledger = DeliveryLedger(self.ledger, work_item="W")
        return driver, ledger, build(driver)

    def test_SW_020_a_held_lock_blocks_delivery_without_touching_the_app(self):
        driver, ledger, adapter = self.build()
        self.hold_in_subprocess(ledger.path)
        result = adapter.deliver(ledger=ledger, request_id="r1", endpoint_id="orbit-pm",
                                 message="TOKEN body", verify_token="TOKEN",
                                 lock_timeout=0.3)
        self.assertFalse(result.ok)
        self.assertEqual(result.reason_code, "writer-busy")
        # Nothing was staged and nothing was sent.
        self.assertNotIn("set_message", driver.calls)
        self.assertNotIn("press_send", driver.calls)

    def test_SW_021_the_lock_is_released_after_a_delivery(self):
        _driver, ledger, adapter = self.build()
        result = adapter.deliver(ledger=ledger, request_id="r1", endpoint_id="orbit-pm",
                                 message="TOKEN body", verify_token="TOKEN")
        self.assertTrue(result.ok, result.reason_code)
        after = run(ledger.path, 5.0, 'print(lock.acquire().reason_code)')
        self.assertEqual(after.stdout.strip(), "ok", after.stderr[:300])

    def test_SW_022_the_lock_is_released_even_when_delivery_fails(self):
        """A refused delivery must not strand the ledger for everyone else."""
        from standalone.tests.test_chatgpt_adapter import deny as stub_deny

        driver, ledger, adapter = self.build()
        driver.send_result = stub_deny("send-control-disabled")
        result = adapter.deliver(ledger=ledger, request_id="r1", endpoint_id="orbit-pm",
                                 message="TOKEN body", verify_token="TOKEN")
        self.assertFalse(result.ok)
        after = run(ledger.path, 5.0, 'print(lock.acquire().reason_code)')
        self.assertEqual(after.stdout.strip(), "ok", after.stderr[:300])

    def test_SW_023_stop_outranks_the_lock(self):
        """STOP must not have to queue behind another runner to take effect."""
        driver, ledger, adapter = self.build()
        stop = self.dir / "STOP"
        stop.write_text("stopped", encoding="utf-8")
        self.hold_in_subprocess(ledger.path)
        result = adapter.deliver(ledger=ledger, request_id="r1", endpoint_id="orbit-pm",
                                 message="TOKEN body", verify_token="TOKEN",
                                 stop_path=stop, lock_timeout=0.3)
        self.assertEqual(result.reason_code, "stop-active")

    def test_SW_024_two_runners_cannot_both_actuate_the_same_record(self):
        """The claim itself, over the delivery path rather than the raw lock."""
        driver, ledger, adapter = self.build()
        self.hold_in_subprocess(ledger.path)
        for _ in range(4):
            result = adapter.deliver(ledger=ledger, request_id="r1", endpoint_id="orbit-pm",
                                     message="TOKEN body", verify_token="TOKEN",
                                     lock_timeout=0.2)
            self.assertFalse(result.ok)
        self.assertEqual(driver.calls.count("press_send"), 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
