"""Following the update terminal: what she says when it's done."""
import asyncio
import datetime as dt

from assistant import updates

LOG = """\
[2026-11-11T13:20:00+0100] [ALPM] upgraded old-thing (1 -> 2)
[2026-11-11T13:25:01+0100] [ALPM] upgraded systemd (258-1 -> 258-2)
[2026-11-11T13:25:01+0100] [ALPM] upgraded linux-cachyos (7.3.3-1 -> 7.3.4-1)
[2026-11-11T13:25:02+0100] [ALPM] upgraded pipewire (1:1.4-1 -> 1:1.4-2)
[2026-11-11T13:25:02+0100] [ALPM] installed new-dep (1.0-1)
[2026-11-11T13:25:03+0100] [ALPM] removed gone (1.0-1)
[2026-11-11T13:25:04+0100] [ALPM] transaction completed
"""
SINCE = dt.datetime.fromisoformat("2026-11-11T13:23:00+01:00")


def test_only_changes_after_the_update_started_count():
    assert updates.changes_since(LOG, SINCE) == [("upgraded", "systemd"), ("upgraded", "linux-cachyos"),
                                                 ("upgraded", "pipewire"), ("installed", "new-dep"),
                                                 ("removed", "gone")]


def test_a_kernel_update_asks_for_a_restart():
    said = updates.summary(0, updates.changes_since(LOG, SINCE), kernel_gone=False)
    assert said == ("The update is done, 4 packages updated. Restart your PC when you get a chance, "
                    "the kernel, pipewire and systemd were updated.")


def test_small_updates_need_no_restart():
    assert updates.summary(0, [("upgraded", "libical"), ("upgraded", "qt6-webengine")], False) == \
        "The update is done, 2 packages updated."


def test_running_kernel_gone_means_restart(tmp_path):
    (tmp_path / "7.3.5-1-cachyos").mkdir()
    assert updates.kernel_replaced("7.3.4-1-cachyos", tmp_path)
    assert not updates.kernel_replaced("7.3.5-1-cachyos", tmp_path)


def test_cancelled_and_failed_updates():
    assert updates.summary(1, [], False) == "The update didn't go through, so nothing was installed."
    assert updates.summary(0, [], False) == "The update finished, there was nothing to install."
    assert "stopped with an error after updating 1 package" in updates.summary(1, [("upgraded", "x")], False)


def test_the_terminal_writes_its_exit_code_before_waiting(tmp_path):
    status = tmp_path / "s"
    script = updates.terminal_script("true", status)
    assert script.index("echo $? >") < script.index("read -r")


async def test_watch_reports_when_the_terminal_is_done(tmp_path, monkeypatch):
    status = tmp_path / "s"
    log = tmp_path / "pacman.log"
    log.write_text(LOG)
    monkeypatch.setattr(updates, "PACMAN_LOG", log)
    monkeypatch.setattr(updates, "kernel_replaced", lambda: False)
    said = []

    async def done(text):
        said.append(text)

    async def finish():
        await asyncio.sleep(0.05)
        status.write_text("0\n")

    asyncio.get_running_loop().create_task(finish())
    await updates.watch(status, SINCE, done, every_s=0.01)
    assert said and said[0].startswith("The update is done, 4 packages updated.")
    assert not status.exists()
