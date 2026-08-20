"""ChatGPT desktop adapter: typed operations over the semantic UIA driver.

Every step that could go wrong in a way that matters is verified *after* it
happens, not assumed from a successful click:

* focusing a chat is confirmed by reading the active-chat header back;
* a staged message is read back from the composer before sending;
* send is only attempted when the app reports idle, never mid-stream;
* completion is detected from the app's own streaming state, never a sleep.

The endpoint is always resolved from the registry by `endpoint_id`. No chat is
ever selected from message text, and no operation accepts a coordinate.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional

import hashlib
import io
import zipfile
from pathlib import Path

from workflow.core.validation import NAME_RE, parse_header

from .contracts import ChatEndpoint, ChatTransportResult
from .registry import ChatEndpointRegistry
from .uia import UiaDriver

ADAPTER_APP = "CHATGPT_DESKTOP"

# How long to wait for a reply before handing the question back to a human.
DEFAULT_RESPONSE_TIMEOUT = 900.0
POLL_INTERVAL = 3.0
# A reply is only "done" once the app has looked idle for this long. ChatGPT
# briefly shows no stop control between thinking and streaming, so a single
# idle observation is not proof of completion.
IDLE_CONFIRM_SECONDS = 6.0
# How long to wait for a pasted message to appear in the accessibility tree.
STAGE_VERIFY_SECONDS = 8.0


@dataclass
class ResponseObservation:
    state: str
    elapsed: float
    polls: int
    detail: str = ""


class ChatGptAdapter:
    def __init__(
        self,
        registry: ChatEndpointRegistry,
        *,
        driver: Optional[UiaDriver] = None,
        project_scope: str,
        workflow_scope: str,
        chat_list_name: str,
        sleeper: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ):
        self.registry = registry
        self.driver = driver or UiaDriver()
        self.project_scope = project_scope
        self.workflow_scope = workflow_scope
        self.chat_list_name = chat_list_name
        self._sleep = sleeper
        self._now = clock

    # -- readiness -------------------------------------------------------

    def surface_ready(self) -> ChatTransportResult:
        """The adapter refuses to act on a window it cannot read."""
        snap = self.driver.snapshot(chat_list_name=self.chat_list_name)
        if not snap.ok:
            return ChatTransportResult.deny("FOCUS_REGISTERED_CHAT", snap.reason_code, str(snap.get("detail", "")))
        data = snap.data
        missing = [
            name for name, present in (
                ("composer", data.get("composer_present")),
                ("send", data.get("send_present")),
                ("attach", data.get("attach_present")),
            ) if not present
        ]
        if missing:
            return ChatTransportResult.deny(
                "FOCUS_REGISTERED_CHAT", "semantic-surface-incomplete", "missing: " + ",".join(missing))
        if not data.get("chat_items"):
            return ChatTransportResult.deny("FOCUS_REGISTERED_CHAT", "chat-list-empty", self.chat_list_name)
        return ChatTransportResult.allow("FOCUS_REGISTERED_CHAT", dict(data))

    def await_surface(self, *, timeout: float = 20.0) -> ChatTransportResult:
        """Wait for the app to finish rendering, rather than sleeping a guess.

        Switching conversations tears down and rebuilds the composer, so a
        readiness check run immediately after a switch legitimately sees no
        composer. Poll until the controls exist or give up with a typed reason;
        never assume a fixed delay was long enough.
        """
        started = self._now()
        last = ChatTransportResult.deny("FOCUS_REGISTERED_CHAT", "surface-never-ready")
        while True:
            last = self.surface_ready()
            if last.ok:
                return last
            if (self._now() - started) >= timeout:
                return ChatTransportResult.deny(
                    "FOCUS_REGISTERED_CHAT", "surface-not-ready-in-time",
                    f"{last.reason_code}: {last.detail}")
            self._sleep(1.0)

    def observed_titles(self) -> List[str]:
        snap = self.driver.snapshot(chat_list_name=self.chat_list_name)
        return list(snap.data.get("chat_items", [])) if snap.ok else []

    # -- FOCUS_REGISTERED_CHAT -------------------------------------------

    def resolve(self, endpoint_id: str) -> ChatEndpoint:
        return self.registry.resolve(
            endpoint_id,
            project_scope=self.project_scope,
            workflow_scope=self.workflow_scope,
            observed_titles=self.observed_titles(),
        )

    def focus(self, endpoint_id: str) -> ChatTransportResult:
        ready = self.await_surface()
        if not ready.ok:
            return ready
        try:
            endpoint = self.resolve(endpoint_id)
        except Exception as exc:  # BridgeError and friends
            return ChatTransportResult.deny("FOCUS_REGISTERED_CHAT", str(exc))

        result = self.driver.focus_chat(chat_list_name=self.chat_list_name, chat_title=endpoint.display_title)
        if not result.ok:
            return ChatTransportResult.deny("FOCUS_REGISTERED_CHAT", result.reason_code, str(result.get("detail", "")))

        settled = self.await_surface()
        if not settled.ok:
            return settled

        # Post-condition: the app must now say it is showing that chat. A click
        # that "succeeded" but landed elsewhere is caught here, before anything
        # is typed or sent.
        active = self.driver.active_chat()
        if not active.ok:
            return ChatTransportResult.deny("FOCUS_REGISTERED_CHAT", active.reason_code)
        shown = str(active.data.get("active_chat_title", ""))
        if shown != endpoint.display_title:
            return ChatTransportResult.deny(
                "FOCUS_REGISTERED_CHAT", "focus-verification-failed",
                f"expected {endpoint.display_title!r}, header shows {shown!r}")

        return ChatTransportResult.allow("FOCUS_REGISTERED_CHAT", {
            "endpoint_id": endpoint.endpoint_id,
            "display_title": endpoint.display_title,
            "active_chat_title": shown,
            "project_markers": active.data.get("project_markers", []),
        })

    # -- SEND_BOUNDED_MESSAGE --------------------------------------------

    def stage_message(self, text: str, *, verify_token: str = "") -> ChatTransportResult:
        if not text or not text.strip():
            return ChatTransportResult.deny("SEND_BOUNDED_MESSAGE", "message-empty")
        state = self.driver.response_state()
        if state.ok and state.data.get("state") == "streaming":
            # Typing into a composer mid-stream is how messages get interleaved
            # into the wrong turn.
            return ChatTransportResult.deny("SEND_BOUNDED_MESSAGE", "response-in-progress")
        result = self.driver.set_message(text)
        if not result.ok:
            return ChatTransportResult.deny("SEND_BOUNDED_MESSAGE", result.reason_code, str(result.get("detail", "")))

        # Read the composer back. Paste can be swallowed by a re-render or land
        # in a different field; only the app's own report of its contents proves
        # what would actually be transmitted.
        #
        # ProseMirror commits a paste to the accessibility tree asynchronously,
        # so a single immediate sample can legitimately miss content that is
        # already there. Poll instead of guessing a delay -- and keep failing
        # closed if it never appears.
        deadline = self._now() + STAGE_VERIFY_SECONDS
        actual = ""
        while True:
            staged = self.driver.read_composer()
            if not staged.ok:
                return ChatTransportResult.deny("SEND_BOUNDED_MESSAGE", staged.reason_code)
            actual = str(staged.data.get("text", ""))
            if not verify_token or verify_token in actual:
                break
            if self._now() >= deadline:
                return ChatTransportResult.deny(
                    "SEND_BOUNDED_MESSAGE", "staged-message-verification-failed",
                    f"token {verify_token!r} absent from composer after "
                    f"{STAGE_VERIFY_SECONDS:.0f}s")
            self._sleep(0.5)
        return ChatTransportResult.allow("SEND_BOUNDED_MESSAGE", {
            "staged_length": len(text),
            "composer_length": len(actual),
            "verified_token": verify_token or "",
        })

    def send(self, *, expect_endpoint_id: str) -> ChatTransportResult:
        """Press Send, re-verifying the destination immediately beforehand."""
        try:
            endpoint = self.resolve(expect_endpoint_id)
        except Exception as exc:
            return ChatTransportResult.deny("SEND_BOUNDED_MESSAGE", str(exc), delivery_state="FAILED")

        active = self.driver.active_chat()
        if not active.ok:
            return ChatTransportResult.deny("SEND_BOUNDED_MESSAGE", active.reason_code, delivery_state="FAILED")
        shown = str(active.data.get("active_chat_title", ""))
        if shown != endpoint.display_title:
            # The chat changed between focus and send. Never send into it.
            return ChatTransportResult.deny(
                "SEND_BOUNDED_MESSAGE", "destination-changed-before-send",
                f"expected {endpoint.display_title!r}, header shows {shown!r}", delivery_state="FAILED")

        before = self.driver.response_state()
        if before.ok and before.data.get("state") == "streaming":
            return ChatTransportResult.deny("SEND_BOUNDED_MESSAGE", "response-in-progress", delivery_state="FAILED")

        pressed = self.driver.call("press_send", {})
        if not pressed.ok:
            return ChatTransportResult.deny(
                "SEND_BOUNDED_MESSAGE", pressed.reason_code, str(pressed.get("detail", "")),
                delivery_state="FAILED")

        return ChatTransportResult.allow("SEND_BOUNDED_MESSAGE", {
            "endpoint_id": endpoint.endpoint_id,
            "display_title": endpoint.display_title,
        }, delivery_state="SENT_UNCONFIRMED")

    # -- WAIT_FOR_RESPONSE -----------------------------------------------

    def wait_for_response(self, *, timeout: float = DEFAULT_RESPONSE_TIMEOUT) -> ResponseObservation:
        """Wait on the app's own streaming state, never on a fixed sleep."""
        started = self._now()
        polls = 0
        saw_streaming = False
        idle_since: Optional[float] = None

        while True:
            polls += 1
            state = self.driver.response_state()
            now = self._now()
            elapsed = now - started

            if not state.ok:
                # Losing the tree mid-task is a halt condition, not a retry loop.
                return ResponseObservation("error", elapsed, polls, state.reason_code)

            current = str(state.data.get("state", "unknown"))
            if current == "streaming":
                saw_streaming = True
                idle_since = None
            elif current == "idle":
                if idle_since is None:
                    idle_since = now
                elif saw_streaming and (now - idle_since) >= IDLE_CONFIRM_SECONDS:
                    return ResponseObservation("complete", elapsed, polls)

            if elapsed >= timeout:
                return ResponseObservation("timeout", elapsed, polls,
                                           f"last state {current}, streaming_seen={saw_streaming}")
            self._sleep(POLL_INTERVAL)

    # -- COLLECT_EXPECTED_ARTIFACT (enumeration half) --------------------

    def list_artifacts(self) -> ChatTransportResult:
        result = self.driver.list_artifacts()
        if not result.ok:
            return ChatTransportResult.deny("COLLECT_EXPECTED_ARTIFACT", result.reason_code)
        return ChatTransportResult.allow("COLLECT_EXPECTED_ARTIFACT", dict(result.data))

    def find_expected_artifact(self, expected_name: str) -> ChatTransportResult:
        """Require exactly one saveable card matching the expected filename."""
        listed = self.list_artifacts()
        if not listed.ok:
            return listed
        saveable = [str(n) for n in listed.data.get("saveable", [])]
        matches = [n for n in saveable if n == expected_name]
        if not matches:
            return ChatTransportResult.deny("COLLECT_EXPECTED_ARTIFACT", "artifact-not-present", expected_name)
        if len(matches) > 1:
            return ChatTransportResult.deny(
                "COLLECT_EXPECTED_ARTIFACT", "artifact-ambiguous",
                f"{len(matches)} cards named {expected_name}")
        return ChatTransportResult.allow("COLLECT_EXPECTED_ARTIFACT", {"filename": matches[0], "candidates": saveable})

    def collect_artifact(
        self,
        *,
        endpoint_id: str,
        expected_name: str,
        inbox_dir: "Path",
        work_item: str,
        expected_sender: str = "",
    ) -> ChatTransportResult:
        """Materialise exactly one expected artifact and validate its identity.

        The destination path is supplied by Orbit, so the file is written where
        Orbit asked rather than into a shared Downloads folder. There is no
        window in which a neighbouring file could be mistaken for the result.

        The endpoint is focused and verified first. Collecting from whichever
        conversation happens to be open would reintroduce the wrong-chat hazard
        by the back door: the app can change the active chat between a dispatch
        and a collection, and it was observed doing exactly that.
        """
        focused = self.focus(endpoint_id)
        if not focused.ok:
            return ChatTransportResult.deny(
                "COLLECT_EXPECTED_ARTIFACT", focused.reason_code, focused.detail)

        found = self.find_expected_artifact(expected_name)
        if not found.ok:
            return found

        match = NAME_RE.match(expected_name)
        if not match:
            return ChatTransportResult.deny(
                "COLLECT_EXPECTED_ARTIFACT", "artifact-name-not-handoff-shaped", expected_name)
        if match.groupdict()["work"] != work_item:
            return ChatTransportResult.deny(
                "COLLECT_EXPECTED_ARTIFACT", "artifact-work-item-mismatch",
                f"filename declares {match.groupdict()['work']}, expected {work_item}")

        inbox = Path(inbox_dir)
        inbox.mkdir(parents=True, exist_ok=True)
        destination = inbox / expected_name
        if destination.exists():
            # Never silently overwrite an already-collected artifact; a second
            # collection has to be an explicit decision.
            return ChatTransportResult.deny(
                "COLLECT_EXPECTED_ARTIFACT", "artifact-already-collected", str(destination))

        saved = self.driver.save_artifact_as(filename=expected_name, destination=str(destination))
        if not saved.ok:
            return ChatTransportResult.deny(
                "COLLECT_EXPECTED_ARTIFACT", saved.reason_code, str(saved.get("detail", "")))

        # The dialog closing is not proof the bytes arrived; check the file.
        for _ in range(10):
            if destination.is_file() and destination.stat().st_size > 0:
                break
            self._sleep(0.5)
        else:
            return ChatTransportResult.deny(
                "COLLECT_EXPECTED_ARTIFACT", "artifact-not-materialised", str(destination))

        data = destination.read_bytes()
        digest = hashlib.sha256(data).hexdigest()

        # The accepted envelope allows .md or .zip, and a zip carries its header
        # in a root HANDOFF.md. Mirror the engine's rules rather than inventing
        # a second, weaker set here.
        if destination.suffix.lower() == ".zip":
            try:
                with zipfile.ZipFile(io.BytesIO(data), "r") as archive:
                    names = archive.namelist()
                    if "HANDOFF.md" not in names:
                        return ChatTransportResult.deny(
                            "COLLECT_EXPECTED_ARTIFACT", "artifact-zip-missing-root-handoff", str(destination))
                    for name in names:
                        normalized = name.replace("\\", "/")
                        if normalized.startswith("/") or "../" in normalized or normalized == "..":
                            return ChatTransportResult.deny(
                                "COLLECT_EXPECTED_ARTIFACT", "artifact-zip-path-traversal", name)
                        if normalized != "HANDOFF.md" and not normalized.startswith("artifacts/"):
                            return ChatTransportResult.deny(
                                "COLLECT_EXPECTED_ARTIFACT", "artifact-zip-unsupported-root-entry", name)
                    text = archive.read("HANDOFF.md").decode("utf-8")
            except (zipfile.BadZipFile, UnicodeDecodeError, OSError):
                return ChatTransportResult.deny("COLLECT_EXPECTED_ARTIFACT", "artifact-malformed-zip", str(destination))
        else:
            try:
                text = data.decode("utf-8")
            except UnicodeDecodeError:
                return ChatTransportResult.deny("COLLECT_EXPECTED_ARTIFACT", "artifact-not-utf8", str(destination))

        parsed = parse_header(text)
        if not parsed.ok:
            return ChatTransportResult.deny(
                "COLLECT_EXPECTED_ARTIFACT", f"artifact-{parsed.reason}", str(destination))

        fields = parsed.fields
        if fields.get("work item") != work_item:
            return ChatTransportResult.deny(
                "COLLECT_EXPECTED_ARTIFACT", "artifact-header-work-item-mismatch",
                f"header declares {fields.get('work item')!r}, expected {work_item!r}")
        if expected_sender and str(fields.get("from", "")).upper() != expected_sender.upper():
            return ChatTransportResult.deny(
                "COLLECT_EXPECTED_ARTIFACT", "artifact-sender-mismatch",
                f"header declares {fields.get('from')!r}, expected {expected_sender!r}")

        return ChatTransportResult.allow("COLLECT_EXPECTED_ARTIFACT", {
            "filename": expected_name,
            "path": str(destination),
            "sha256": digest,
            "size_bytes": len(data),
            "work_item": fields.get("work item"),
            "sender": fields.get("from"),
            "recipient": fields.get("to"),
            "status": fields.get("status"),
            "handoff_id": fields.get("handoff id"),
            "sequence": fields.get("sequence"),
        })
