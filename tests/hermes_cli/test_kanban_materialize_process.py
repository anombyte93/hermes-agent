"""Exercise materialization JSON and shell exit codes through the real CLI main."""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

from hermes_cli import kanban_db as kb
from hermes_cli.kanban_db_connect import connect_closing


ROOT = Path(__file__).parents[2]


def test_rename_race_uses_real_processes_and_one_card(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    launcher = tmp_path / "race.py"
    launcher.write_text(
        "import json, sys, time\n"
        "from pathlib import Path\n"
        "from hermes_cli.kanban_db_connect import connect_closing\n"
        "from hermes_cli.kanban_db_materialize import materialize_task\n"
        "ready, go = map(Path, sys.argv[1:])\n"
        "with connect_closing(board='default') as conn:\n"
        "    ready.touch()\n"
        "    deadline = time.monotonic() + 20\n"
        "    while not go.exists():\n"
        "        assert time.monotonic() < deadline\n"
        "        time.sleep(0.01)\n"
        "    print(json.dumps(materialize_task(conn, project='race', plan_revision='r2', proposed_task_id='y', intent_fingerprint='A', title='A', maps_from='r1:x')))\n")
    env = dict(HOME=str(tmp_path), PATH=os.defpath, HERMES_HOME=str(home),
               HERMES_KANBAN_HOME=str(home), PYTHONPATH=str(ROOT), TMPDIR=str(tmp_path),
               PYTHONPYCACHEPREFIX=str(tmp_path / 'pycache'))
    from hermes_cli.kanban_db_materialize import materialize_task
    with connect_closing(home / "kanban.db") as conn:
        tid = materialize_task(conn, project="race", plan_revision="r1", proposed_task_id="x",
                               intent_fingerprint="A", title="A")["task_id"]
    go = tmp_path / "go"
    ready = [tmp_path / str(i) for i in range(2)]
    processes = [subprocess.Popen([sys.executable, str(launcher), str(p), str(go)],
        cwd=tmp_path, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) for p in ready]
    import time
    deadline = time.monotonic() + 20
    while not all(p.exists() for p in ready):
        assert time.monotonic() < deadline
        time.sleep(0.01)
    go.touch()
    results = []
    for process in processes:
        out, err = process.communicate(timeout=30)
        assert process.returncode == 0, err
        results.append(json.loads(out))
    assert sorted(r["state"] for r in results) == ["existing", "mapped"]
    assert {r["task_id"] for r in results} == {tid}
    with connect_closing(home / "kanban.db") as conn:
        assert len(kb.list_tasks(conn)) == 1
        assert sum(e.kind == "materialization_mapped" for e in kb.list_events(conn, tid)) == 1


def test_materialize_process_contract_uses_one_canonical_card(tmp_path):
    home = tmp_path / "hermes"
    home.mkdir()
    launcher = tmp_path / "launch.py"
    # The CLI's unrelated dotenv edge must never read a checkout's real .env
    # or fetch external secrets. Parsing, dispatch, DB writes and exit are real.
    launcher.write_text(
        "from hermes_cli import env_loader\n"
        "env_loader.load_hermes_dotenv = lambda **kwargs: None\n"
        "from hermes_cli.main import main\n"
        "main()\n", encoding="utf-8")
    env = dict(HOME=str(tmp_path), USERPROFILE=str(tmp_path), PATH=os.defpath,
               HERMES_HOME=str(home), HERMES_KANBAN_HOME=str(home),
               HERMES_RUNTIME_DIR=str(tmp_path / "runtime"),
               HERMES_TEST_ISOLATION=str(home), PYTHONPATH=str(ROOT),
               PYTHONPYCACHEPREFIX=str(tmp_path / "pycache"), TMPDIR=str(tmp_path),
               TEMP=str(tmp_path), TMP=str(tmp_path), TZ="UTC", LANG="C.UTF-8")
    if "SYSTEMROOT" in os.environ:
        env["SYSTEMROOT"] = os.environ["SYSTEMROOT"]

    def run(revision, fingerprint, *extra, check=False):
        result = subprocess.run([sys.executable, str(launcher), "kanban", "--board", "default",
            "materialize-check" if check else "materialize", "--project", "fixture", "--plan-revision", revision,
            "--proposed-id", "work", "--fingerprint", fingerprint, "--title", "Acceptance",
            *extra, "--execution-kind", "external_session", "--json"],
            cwd=tmp_path, env=env, capture_output=True, text=True, timeout=30, check=False)
        assert result.stdout.strip(), result.stderr
        return result.returncode, json.loads(result.stdout)

    assert run("r1", "intent", check=True) == (0, {"state": "created", "task_id": None})
    assert not (home / "kanban.db").exists()
    rc, first = run("r1", "intent")
    tid = first["task_id"]
    assert run("r1", "changed", check=True) == (3, {
        "state": "conflict", "task_id": tid, "reason": "materialization_conflict"})
    assert run("r2", "changed", "--maps-from", "missing:work", check=True) == (
        1, {"state": "refused", "reason": "mapping_not_found"})
    assert (rc, first) == (0, {"state": "created", "task_id": tid})
    assert run("r1", "intent") == (0, {"state": "existing", "task_id": tid})
    assert run("r1", "changed") == (3, {"state": "conflict", "task_id": tid,
                                       "reason": "materialization_conflict"})
    assert run("r2", "intent") == (0, {"state": "existing", "task_id": tid})
    assert run("r3", "changed") == (3, {"state": "conflict", "task_id": tid,
                                       "reason": "revision_requires_mapping"})
    assert run("r3", "changed", "--maps-from", "r2:work") == (
        0, {"state": "mapped", "task_id": tid})
    assert run("r4", "changed", "--maps-from", "missing:work") == (
        1, {"state": "refused", "reason": "mapping_not_found"})
    assert run("r4", "intent") == (3, {
        "state": "conflict", "task_id": tid, "reason": "revision_requires_mapping"})
    assert run("r4", "intent", "--maps-from", "r3:work") == (0, {"state": "mapped", "task_id": tid})
    prior = tmp_path / "prior-rows.json"
    prior.write_text(json.dumps([dict(proposed_task_id="parent", intent_fingerprint="parent-intent", title="Parent")]))
    assert run("r5", "new-intent", "--maps-from", "r4:work", "--parent-proposed", "parent",
               "--prior-rows-file", str(prior), check=True) == (0, {"state": "mapped", "task_id": tid})
    other_pin = home / "other.db"
    env["HERMES_KANBAN_DB"] = str(other_pin)
    for check in (False, True):
        assert run("r5", "new-intent", check=check) == (
            1, {"state": "refused", "reason": "board_pin_mismatch"})
    assert not other_pin.exists()
    with connect_closing(home / "kanban.db") as observer:
        tasks = kb.list_tasks(observer)
        assert len(tasks) == 1 and tasks[0].id == tid
        assert tasks[0].execution_kind == "external_session"
        assert observer.execute("SELECT COUNT(*) FROM task_materializations").fetchone()[0] == 4
        assert [event.kind for event in kb.list_events(observer, tid)] == [
            "created", "materialization_mapped", "materialization_mapped"]
