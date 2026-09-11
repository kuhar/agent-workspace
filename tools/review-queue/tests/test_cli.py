from __future__ import annotations

import json
from argparse import Namespace

from review_queue import cli
from review_queue.scheduler import SingleInstanceLock


def test_cli_manual_mode_persists_and_pause_resumes_it(config, monkeypatch, capsys):
    monkeypatch.setattr(cli, "load_config", lambda *_args, **_kwargs: config)
    assert cli.main(["mode", "manual"]) == 0
    assert cli.main(["pause"]) == 0
    assert cli.main(["resume"]) == 0
    capsys.readouterr()
    assert cli.main(["state", "--json"]) == 0
    snapshot = json.loads(capsys.readouterr().out)
    assert snapshot["mode"] == "manual"
    assert snapshot["manual_only"] and not snapshot["paused"]


async def test_state_can_run_while_dispatcher_lock_is_held(config, monkeypatch, capsys) -> None:
    monkeypatch.setattr(cli, "load_config", lambda *_args, **_kwargs: config)
    lock = SingleInstanceLock(config.state_dir / "process.lock")
    try:
        result = await cli._run(
            Namespace(
                demo=False,
                command="state",
                config=config.config_path,
                state_dir=config.state_dir,
                theme=None,
                json=True,
            )
        )
    finally:
        lock.close()

    assert result == 0
    assert '"queue": []' in capsys.readouterr().out
