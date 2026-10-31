"""Bounded asynchronous mock preview coordination.

The provider contract is create → poll, and the app limits are the frozen ones:
at most three candidates per session, at most two tasks in flight, one total
deadline of 60 seconds, and MOCK plans that consume zero provider units.

The job lives in a JSON-serialisable dict so it can be persisted between requests
and across a restart.  Polling is driven by an ordinary page GET — there is no
background thread, no timer and no network.  A cancelled, revoked or expired job
returns ``DISCARDED`` and the caller must not apply its result.
"""

from __future__ import annotations

import inspect
import secrets
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from typing import Any, Mapping

from adapters.youcam_stub import AdapterError, FailureCode, PreviewTask
from domain.preview import PreviewPlan, PreviewResult

MAX_CONCURRENCY = 2
MAX_CANDIDATES = 3
DEADLINE_SECONDS = 60


@dataclass(frozen=True, slots=True)
class TickOutcome:
    status: str  # PENDING | COMPLETE | DISCARDED
    result: PreviewResult | None = None
    reason: str | None = None
    progress: Mapping[str, Any] | None = None


def _iso(value: datetime) -> str:
    return value.isoformat()


def _dt(value: str) -> datetime:
    return datetime.fromisoformat(value)


class PreviewCoordinator:
    def __init__(
        self,
        adapter: Any,
        *,
        clock,
        concurrency: int = MAX_CONCURRENCY,
        latency_seconds: float = 0.0,
    ) -> None:
        self.adapter = adapter
        self._clock = clock
        self.concurrency = int(concurrency)
        self.latency_seconds = float(latency_seconds)
        # Identifies this process's in-memory adapter.  A job created by another
        # process cannot be polled here: its task ids are gone, and re-creating
        # them would be a blind retry.  Such a job is closed as INTERRUPTED.
        self.instance_id = secrets.token_hex(8)

    # -- job creation ---------------------------------------------------- #
    def start(self, plan: PreviewPlan, *, generation: int,
              photo_version: str | None = None) -> dict[str, Any]:
        candidates = tuple(plan.candidate_ids)
        if len(candidates) > MAX_CANDIDATES:
            raise ValueError("a preview job carries at most three candidates")
        now = self._clock()
        job: dict[str, Any] = {
            "sessionId": plan.session_id,
            "consentVersion": plan.consent_version,
            "generation": int(generation),
            "mode": plan.mode,
            "authorizationRef": plan.authorization_ref,
            "estimatedUnits": int(plan.estimated_units),
            "photoVersion": photo_version,
            "providerVersion": plan.provider_version,
            "candidates": list(candidates),
            "createdAt": _iso(now),
            "deadline": _iso(plan.deadline),
            "planDeadline": _iso(plan.deadline),
            "tasks": {},
            "queue": list(candidates),
            "running": [],
            "instanceId": self.instance_id,
        }
        self._launch(job, plan)
        return job

    # -- progress -------------------------------------------------------- #
    @staticmethod
    def progress(job: Mapping[str, Any]) -> dict[str, Any]:
        tasks = job.get("tasks", {})
        done = sum(1 for task in tasks.values() if task["state"] in ("DONE", "FAILED"))
        failed = sum(1 for task in tasks.values() if task["state"] == "FAILED")
        return {
            "candidates": len(job.get("candidates", [])),
            "started": len(tasks),
            "completed": sum(1 for task in tasks.values() if task["state"] == "DONE"),
            "failed": failed,
            "pending": len(job.get("queue", [])),
            "running": len(job.get("running", [])),
            "finished": done,
            "concurrency": MAX_CONCURRENCY,
            "capacity": MAX_CANDIDATES,
            "units": 0,
            "mode": job.get("mode", "MOCK"),
            "deadline": job.get("deadline"),
        }

    def _launch(self, job: dict[str, Any], plan: PreviewPlan | None) -> None:
        session_id = str(job["sessionId"])
        while job["queue"] and len(job["running"]) < self.concurrency:
            candidate = job["queue"].pop(0)
            if plan is None:
                plan = PreviewPlan(
                    session_id=session_id,
                    consent_version=str(job["consentVersion"]),
                    candidate_ids=tuple(job["candidates"]),
                    provider_version=str(job["providerVersion"]),
                    estimated_units=0,
                    authorization_ref=None,
                    deadline=_dt(str(job["planDeadline"])),
                    mode=str(job["mode"]),
                )
            try:
                single = replace(plan, candidate_ids=(candidate,))
                tasks = self.adapter.create_preview(single)
                task = tasks[0]
            except AdapterError as exc:
                job["tasks"][candidate] = {
                    "state": "FAILED",
                    "failure": exc.code.value,
                    "taskId": None,
                    "handle": None,
                    "readyAt": _iso(self._clock()),
                }
                continue
            except Exception:  # a broken adapter must not break the page
                job["tasks"][candidate] = {
                    "state": "FAILED",
                    "failure": FailureCode.PROVIDER_FAILURE.value,
                    "taskId": None,
                    "handle": None,
                    "readyAt": _iso(self._clock()),
                }
                continue
            job["tasks"][candidate] = {
                "state": "RUNNING",
                "failure": None,
                "taskId": task.task_id,
                "handle": None,
                "readyAt": _iso(self._clock() + timedelta(seconds=self.latency_seconds)),
            }
            job["running"].append(candidate)

    def _adopt(self, job: dict[str, Any]) -> bool:
        adopter = getattr(self.adapter, "adopt_preview", None)
        if not callable(adopter):
            return False
        running = [record for record in job.get("tasks", {}).values()
                   if record.get("state") == "RUNNING" and record.get("taskId")]
        if not running:
            return False
        plan = PreviewPlan(
            session_id=str(job["sessionId"]),
            consent_version=str(job["consentVersion"]),
            candidate_ids=tuple(job["candidates"]),
            provider_version=str(job["providerVersion"]),
            estimated_units=int(job.get("estimatedUnits", 0)),
            authorization_ref=job.get("authorizationRef"),
            deadline=_dt(str(job["planDeadline"])),
            mode=str(job["mode"]),
        )
        try:
            return all(adopter(str(record["taskId"]), session_id=str(job["sessionId"]), plan=plan)
                       for record in running)
        except Exception:
            return False

    # -- tick ------------------------------------------------------------ #
    def tick(self, job: dict[str, Any], *, generation_valid: bool) -> TickOutcome:
        if not generation_valid:
            return TickOutcome(status="DISCARDED", reason="CONSENT_REVOKED", progress=self.progress(job))

        now = self._clock()
        if job.get("instanceId") != self.instance_id:
            # Another process created these tasks. Adopt them from the durable record if the
            # adapter can address them; never re-create, and never invent an outcome.
            if self._adopt(job):
                job["instanceId"] = self.instance_id
            else:
                return TickOutcome(
                    status="COMPLETE",
                    result=self._interrupted(job, now),
                    reason="PROCESS_RESTART",
                    progress=self.progress(job),
                )
        deadline = _dt(str(job["deadline"]))
        session_id = str(job["sessionId"])
        plan = PreviewPlan(
            session_id=session_id,
            consent_version=str(job["consentVersion"]),
            candidate_ids=tuple(job["candidates"]),
            provider_version=str(job["providerVersion"]),
            estimated_units=int(job.get("estimatedUnits", 0)),
            authorization_ref=job.get("authorizationRef"),
            deadline=deadline,
            mode=str(job["mode"]),
        )

        if now < deadline:
            self._launch(job, plan)

        for candidate in list(job["running"]):
            task = job["tasks"][candidate]
            if datetime.fromisoformat(task["readyAt"]) > now:
                continue
            # Past the wait deadline we still make the bounded query below: a late success is
            # delivered, and a still-silent provider becomes reconcilable rather than "failed".
            try:
                polled = self.adapter.poll_preview(task["taskId"], session_id=session_id)
            except AdapterError as exc:
                task.update(state="FAILED", failure=exc.code.value, handle=None)
                job["running"].remove(candidate)
                continue
            if polled.status == "PENDING":
                # A provider that is still working is not a failure; keep the job alive so the
                # asynchronous task survives the tick that observed it.
                task["readyAt"] = _iso(now + timedelta(seconds=max(self.latency_seconds, 0.05)))
                continue
            if polled.status == "UNRESOLVED":
                task.update(state="UNRESOLVED", handle=None,
                            failure=FailureCode.RECONCILIATION_PENDING.value)
                job["running"].remove(candidate)
                continue
            if polled.succeeded:
                task.update(state="DONE", handle=polled.media_handle, failure=None)
            else:
                task.update(
                    state="FAILED",
                    handle=None,
                    failure=(polled.failure.value if polled.failure else None),
                )
            job["running"].remove(candidate)

        if now >= deadline:
            # Candidates never launched cost nothing and are reported as such.
            for candidate in job["queue"]:
                job["tasks"].setdefault(
                    candidate,
                    {"state": "FAILED", "failure": FailureCode.NOT_STARTED.value,
                     "taskId": None, "handle": None},
                )
            job["queue"] = []
            for candidate in list(job["running"]):
                record = job["tasks"][candidate]
                if record.get("state") == "RUNNING":
                    # The task exists at the provider: keep its id for reconciliation, and do not
                    # call it a provider failure.
                    record.update(state="UNRESOLVED", handle=None,
                                  failure=FailureCode.RECONCILIATION_PENDING.value)
            job["running"] = []

        answered = [cid for cid in job["candidates"]
                    if job["tasks"].get(cid, {}).get("state") in ("DONE", "FAILED", "UNRESOLVED")]
        if len(answered) < len(job["candidates"]):
            return TickOutcome(status="PENDING", progress=self.progress(job))

        return TickOutcome(
            status="COMPLETE",
            result=self._result_for(job, now),
            progress=self.progress(job),
        )

    def _tasks_of(self, job):
        """The canonical (session_id, plan, tasks) shape shared with the adapters."""
        out = []
        for candidate in job["candidates"]:
            record = job["tasks"].get(candidate, {})
            if record.get("state") not in ("DONE", "FAILED", "UNRESOLVED"):
                continue
            state = record.get("state")
            out.append(PreviewTask(
                task_id=str(record.get("taskId") or "missing-" + candidate),
                session_id=str(job["sessionId"]),
                candidate_id=candidate,
                mode=str(job["mode"]),
                status=("SUCCEEDED" if state == "DONE" else
                        "UNRESOLVED" if state == "UNRESOLVED" else "FAILED"),
                created_at=self._clock(),
                deadline=_dt(str(job["planDeadline"])),
                media_handle=record.get("handle"),
                failure=FailureCode(record["failure"]) if record.get("failure") else None,
            ))
        return tuple(out)

    def _result_for(self, job, now):
        planner = getattr(self.adapter, "build_result", None)
        if callable(planner):
            plan = PreviewPlan(
                session_id=str(job["sessionId"]),
                consent_version=str(job["consentVersion"]),
                candidate_ids=tuple(job["candidates"]),
                provider_version=str(job["providerVersion"]),
                estimated_units=int(job.get("estimatedUnits", 0)),
                authorization_ref=job.get("authorizationRef"),
                deadline=_dt(str(job["planDeadline"])),
                mode=str(job["mode"]),
            )
            shared = {"session_id": str(job["sessionId"]), "plan": plan,
                      "tasks": self._tasks_of(job)}
            try:
                inspect.signature(planner).bind(**shared)
            except TypeError:
                # Only an adapter that cannot accept the shared contract falls back. A TypeError
                # raised *inside* a conforming adapter still propagates: bind() runs before the call.
                return self._build_result(job, now)
            return planner(**shared)
        return self._build_result(job, now)

    def _interrupted(self, job: Mapping[str, Any], now: datetime) -> PreviewResult:
        candidates = tuple(job.get("candidates", []))
        return PreviewResult(
            session_id=str(job["sessionId"]),
            mode=str(job.get("mode", "MOCK")),
            status="FAILED",
            candidate_ids=candidates,
            task_ids=tuple(f"interrupted-{index}" for index in range(len(candidates))),
            created_at=now,
            expires_at=now + timedelta(seconds=DEADLINE_SECONDS),
            failure_code="INTERRUPTED",
        )

    def _build_result(self, job: dict[str, Any], now: datetime) -> PreviewResult:
        session_id = str(job["sessionId"])
        created_at = now
        deadline = _dt(str(job["deadline"]))
        tasks: list[PreviewTask] = []
        failures: list[str] = []
        for candidate in job["candidates"]:
            record = job["tasks"].get(candidate, {})
            state = record.get("state")
            if state == "DONE" and record.get("handle"):
                tasks.append(
                    PreviewTask(
                        task_id=str(record["taskId"]),
                        session_id=session_id,
                        candidate_id=candidate,
                        mode=str(job["mode"]),
                        status="SUCCEEDED",
                        created_at=created_at,
                        deadline=deadline,
                        media_handle=str(record["handle"]),
                    )
                )
            else:
                failures.append(str(record.get("failure") or FailureCode.PROVIDER_FAILURE.value))
                tasks.append(
                    PreviewTask(
                        task_id=str(record.get("taskId") or f"unscheduled-{candidate}"),
                        session_id=session_id,
                        candidate_id=candidate,
                        mode=str(job["mode"]),
                        status="FAILED",
                        created_at=created_at,
                        deadline=deadline,
                        media_handle=None,
                        failure=FailureCode(record.get("failure") or FailureCode.PROVIDER_FAILURE.value),
                    )
                )
        overall = None
        if all(task.status != "SUCCEEDED" for task in tasks):
            overall = FailureCode(next((code for code in failures if code in FailureCode.__members__), "PROVIDER_FAILURE"))
        return self.adapter.build_result(
            session_id=session_id, tasks=tasks, created_at=created_at, failure_code=overall
        )
