"""The daily copy of her learned state."""
import datetime as dt
import stat
import tarfile

from assistant import statebackup


def _state(tmp_path):
    s = tmp_path / "assistant"
    (s / "reviews").mkdir(parents=True)
    (s / "bench_unlock").mkdir()
    (s / "skills.json").write_text("{}")
    (s / "skills.json.bak-20261024").write_text("old")
    (s / "reviews" / "2026-11-13.md").write_text("# review")
    (s / "bench_unlock" / "x.json").write_text("{}")
    return s


def test_one_private_archive_per_day_without_leftovers(tmp_path):
    s, dest = _state(tmp_path), tmp_path / "backups"
    day = dt.date(2026, 11, 14)
    out = statebackup.backup(s, dest, today=day)
    assert out.name == "state-2026-11-14.tar.gz"
    assert stat.S_IMODE(out.stat().st_mode) == 0o600
    with tarfile.open(out) as tar:
        names = set(tar.getnames())
    assert {"assistant/skills.json", "assistant/reviews/2026-11-13.md"} <= names
    assert not any(".bak-" in n or "bench_unlock" in n for n in names)
    assert statebackup.backup(s, dest, today=day) is None  # already done today


def test_only_the_last_week_is_kept(tmp_path):
    s, dest = _state(tmp_path), tmp_path / "backups"
    for i in range(10):
        statebackup.backup(s, dest, keep=7, today=dt.date(2026, 11, 1) + dt.timedelta(days=i))
    kept = sorted(p.name for p in dest.glob("state-*.tar.gz"))
    assert len(kept) == 7 and kept[0] == "state-2026-11-04.tar.gz" and kept[-1] == "state-2026-11-10.tar.gz"
