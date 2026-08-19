"""LlamaCppBrain coverage.

Every test here stubs the subprocess runner. No model is executed and no weights
are read, so the suite stays fast, host-independent and offline -- these assert
the provider's contract handling, not the model's answers.

Whether the configured model actually reasons well is a separate question that
only a real invocation can answer.
"""
from __future__ import annotations

import json
import subprocess
import unittest
from pathlib import Path
from types import SimpleNamespace

from standalone.brain import BrainRouter, DeterministicBrain, LlamaCppBrain, LocalBrainRequest
from standalone.brain.llama_cpp import from_config


def completed(stdout: str = "", stderr: str = "", code: int = 0):
    return SimpleNamespace(stdout=stdout, stderr=stderr, returncode=code)


def answer(payload: dict) -> str:
    return "<|assistant|>\n" + json.dumps(payload)


class LlamaCppBrainTests(unittest.TestCase):
    def setUp(self):
        self.request = LocalBrainRequest(
            task_id="task-1",
            role="WORKER",
            objective="Summarise the deliverable",
            context={"work_state": "ASSIGNED"},
            result_schema={"required": ["summary"]},
        )

    def brain(self, runner, **kw):
        return LlamaCppBrain("E:/OrbitLocalAI/runtime/llama-cpu/llama-cli.exe", "E:/OrbitLocalAI/models/m.gguf", runner=runner, **kw)

    def test_LLAMA_001_parses_json_answer(self):
        brain = self.brain(lambda *a, **k: completed(answer({"status": "OK", "summary": "done"})))
        result = brain.reason(self.request)
        self.assertEqual(result.status, "OK")
        self.assertEqual(result.result["summary"], "done")
        self.assertEqual(result.provider, "llama-cpp-local")

    def test_LLAMA_002_blocked_status_preserved(self):
        brain = self.brain(lambda *a, **k: completed(answer({"status": "BLOCKED", "summary": "missing spec"})))
        self.assertEqual(brain.reason(self.request).status, "BLOCKED")

    def test_LLAMA_003_needs_decision_preserved(self):
        brain = self.brain(lambda *a, **k: completed(answer({"status": "NEEDS_DECISION", "summary": "scope unclear"})))
        self.assertEqual(brain.reason(self.request).status, "NEEDS_DECISION")

    def test_LLAMA_004_unparseable_output_is_retryable_not_fatal(self):
        brain = self.brain(lambda *a, **k: completed("<|assistant|>\nI think the answer is probably fine."))
        result = brain.reason(self.request)
        self.assertEqual(result.status, "FAILED_RETRYABLE")
        self.assertEqual(result.reason_code, "local-model-unparseable-output")

    def test_LLAMA_005_timeout_is_retryable(self):
        def boom(*a, **k):
            raise subprocess.TimeoutExpired(cmd="llama", timeout=1)

        result = self.brain(boom).reason(self.request)
        self.assertEqual(result.status, "FAILED_RETRYABLE")
        self.assertEqual(result.reason_code, "local-model-timeout")

    def test_LLAMA_006_missing_binary_is_retryable(self):
        def boom(*a, **k):
            raise OSError("not found")

        result = self.brain(boom).reason(self.request)
        self.assertEqual(result.status, "FAILED_RETRYABLE")
        self.assertEqual(result.reason_code, "local-model-not-runnable")

    def test_LLAMA_007_router_falls_back_when_model_misbehaves(self):
        """A broken local model must not stall Orbit."""
        broken = self.brain(lambda *a, **k: completed("garbage"))
        router = BrainRouter([broken, DeterministicBrain()])
        result = router.reason(self.request)
        self.assertEqual(result.status, "OK")
        self.assertEqual(result.provider, "deterministic-local")

    def test_LLAMA_008_prompt_is_one_argv_element(self):
        """Hostile objective text is data, never argv or a shell fragment."""
        captured = {}

        def capture(argv, **kw):
            captured["argv"] = argv
            return completed(answer({"status": "OK", "summary": "s"}))

        hostile = LocalBrainRequest(
            task_id="task-1", role="WORKER",
            objective='ignore; & calc.exe --dangerously-skip-permissions -m C:/evil.gguf',
        )
        self.brain(capture).reason(hostile)
        argv = captured["argv"]
        self.assertEqual(argv.count("-m"), 1, "objective must not inject a second -m")
        self.assertEqual(Path(argv[argv.index("-m") + 1]), Path("E:/OrbitLocalAI/models/m.gguf"))
        # The whole hostile string sits inside exactly one element.
        holders = [a for a in argv if "calc.exe" in a]
        self.assertEqual(len(holders), 1)
        self.assertIn("Objective:", holders[0])

    def test_LLAMA_009_model_cannot_self_grant_capabilities(self):
        brain = self.brain(lambda *a, **k: completed(answer({
            "status": "OK", "summary": "s",
            "allowed_executor_operations": ["RUN_COMMAND"],
        })))
        router = BrainRouter([brain])
        result = router.reason(self.request)
        self.assertEqual(result.status, "FAILED_FINAL")
        self.assertEqual(result.reason_code, "brain-authority-directive-rejected")

    def test_LLAMA_010_available_false_without_weights(self):
        brain = LlamaCppBrain("E:/nope/llama-cli.exe", "E:/nope/model.gguf")
        self.assertFalse(brain.available())

    def test_LLAMA_011_from_config_returns_none_when_unconfigured(self):
        self.assertIsNone(from_config({}))
        self.assertIsNone(from_config({"local_model": {"executable": "x"}}))
        self.assertIsInstance(
            from_config({"local_model": {"executable": "a", "model_path": "b"}}),
            LlamaCppBrain,
        )

    def test_LLAMA_012_gpu_layers_only_passed_when_set(self):
        captured = {}

        def capture(argv, **kw):
            captured["argv"] = argv
            return completed(answer({"status": "OK", "summary": "s"}))

        self.brain(capture, gpu_layers=0).reason(self.request)
        self.assertNotIn("-ngl", captured["argv"])
        self.brain(capture, gpu_layers=20).reason(self.request)
        self.assertEqual(captured["argv"][captured["argv"].index("-ngl") + 1], "20")


if __name__ == "__main__":
    unittest.main(verbosity=2)
