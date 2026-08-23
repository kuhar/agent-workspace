from __future__ import annotations

from argparse import Namespace

from review_queue import cli
from review_queue.scheduler import SingleInstanceLock


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
