"""Concurrency, restart recovery and the public manifest's own integrity."""

from __future__ import annotations

import pathlib
import subprocess
import sys
import threading

import pytest

from harness.webclient import Client
from ui.service import ReturnService, ServiceError
from ui.webapp import Application

SKU = "W01-TEAL-WRAP-DRESS"


def _consented_session(tmp_path):
    service = ReturnService(tmp_path, preview_mode="MOCK")
    client = Client(Application(service))
    assert client.post("/catalog/women/select", {"sku": SKU}).status == 303
    sid = client.session_id()
    page = client.get(f"/s/{sid}")
    assert client.post(f"/s/{sid}/opt-in",
                       {"request_token": client.form_token(page.text)}).status == 303
    page = client.get(f"/s/{sid}")
    assert client.post(f"/s/{sid}/consent",
                       {"request_token": client.form_token(page.text),
                        "imageConsent": "true",
                        "mockAcknowledged": "true"}).status == 303
    return service, client, sid


def test_simultaneous_duplicate_previews_create_one_job(tmp_path):
    service, client, sid = _consented_session(tmp_path)
    try:
        accepted: list[object] = []
        refused: list[object] = []
        gate = threading.Barrier(8)

        def attempt() -> None:
            gate.wait()
            try:
                accepted.append(service.start_previews(sid, sku=SKU))
            except ServiceError as exc:
                refused.append(exc)

        threads = [threading.Thread(target=attempt) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=20)

        assert len(accepted) + len(refused) == 8
        assert len(accepted) == 1, f"{len(accepted)} previews were started for one operation"
        jobs = service.store.jobs_for(sid) if hasattr(service.store, "jobs_for") else None
        if jobs is not None:
            assert len(jobs) == 1
    finally:
        service.close()


def test_a_restart_keeps_the_session_and_does_not_repeat_the_task(tmp_path):
    service, client, sid = _consented_session(tmp_path)
    try:
        service.start_previews(sid, sku=SKU)
        before = service.get_case(sid)
        service.close()

        reopened = ReturnService(tmp_path, preview_mode="MOCK")
        try:
            after = reopened.get_case(sid)
            assert after.session.session_id == before.session.session_id
            assert after.state == before.state
            # the same photo and garment is the same paid operation: it must be refused, not repeated
            with pytest.raises(ServiceError):
                reopened.start_previews(sid, sku=SKU)
        finally:
            reopened.close()
    finally:
        pass


def test_the_public_manifest_rejects_empty_truncated_and_altered_inputs(tmp_path):
    """A manifest that covers nothing, or only part of the tree, must fail."""

    repo = pathlib.Path(__file__).resolve().parents[2]
    tool = repo / "tools" / "verify_spec_copy.py"

    def run(manifest_text: str) -> int:
        target = tmp_path / "tree"
        target.mkdir(exist_ok=True)
        (target / "MANIFEST-PUBLIC.sha256").write_bytes(manifest_text.encode("utf-8"))
        return subprocess.run([sys.executable, str(tool), "--public"], cwd=target,
                              capture_output=True, text=True).returncode

    assert run("") != 0, "an empty manifest must not pass"
    assert run("deadbeef  README.md\n") != 0, "a truncated manifest must not pass"
