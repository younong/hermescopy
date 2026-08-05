"""Cron no longer uses process-global terminal cwd state."""


def test_scheduler_has_no_terminal_cwd_lock_or_global_mutation():
    import cron.scheduler as scheduler

    assert not hasattr(scheduler, "_ReadWriteLock")
    assert not hasattr(scheduler, "_terminal_cwd_lock")


def test_script_subprocess_receives_cwd_without_chdir(tmp_path, monkeypatch):
    import os
    from cron.jobs import CronStore, use_store
    from cron.scheduler import _run_job_script

    owner_home = tmp_path / "owner"
    owner_home.joinpath("scripts").mkdir(parents=True)
    workdir = owner_home / "workspaces" / "default"
    workdir.mkdir(parents=True)
    owner_home.joinpath("scripts/cwd.py").write_text(
        "import os\nprint(os.getcwd())\n", encoding="utf-8"
    )
    monkeypatch.setattr(
        os,
        "chdir",
        lambda _path: (_ for _ in ()).throw(AssertionError("global chdir forbidden")),
    )

    with use_store(CronStore(owner_home)):
        success, output = _run_job_script("cwd.py", cwd=workdir)

    assert success is True
    assert output == str(workdir.resolve())
