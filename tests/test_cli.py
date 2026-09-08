from pathlib import Path

import pytest
import requests

from snatcher import cli
from snatcher.client import Course


def test_parser_uses_daily_default_start_and_automatic_interval() -> None:
    args = cli.build_parser().parse_args([])
    assert args.start_at == "13:00:00"
    assert args.start_now is False
    assert args.interval is None


def test_packaged_defaults_use_executable_directory(monkeypatch, tmp_path: Path) -> None:
    executable = tmp_path / "Snatcher.exe"
    monkeypatch.setattr(cli.sys, "frozen", True, raising=False)
    monkeypatch.setattr(cli.sys, "executable", str(executable))

    args = cli.build_parser().parse_args([])

    assert args.courses == tmp_path / "courses.txt"
    assert args.cache == tmp_path / "course-cache.json"
    assert args.profile == tmp_path / ".browser-profile"


def test_interval_parser_accepts_minimum_and_rejects_lower_values() -> None:
    assert cli.parse_interval("0.5") == 0.5
    with pytest.raises(Exception, match="不能小于 0.5"):
        cli.parse_interval("0.49")


def test_automatic_interval_gradually_decreases_without_rate_limit() -> None:
    interval, floor, stable = 1.5, 0.5, 0
    for _ in range(4):
        interval, floor, stable = cli.auto_interval_after(interval, floor, stable, False)
    assert interval == 1.25
    assert floor == 0.5
    assert stable == 0


def test_automatic_interval_backs_off_and_raises_floor_on_rate_limit() -> None:
    interval, floor, stable = cli.auto_interval_after(1.0, 0.5, 3, True)
    assert interval == 5.0
    assert floor == 1.25
    assert stable == 0


def test_guided_run_prompts_for_basic_settings(monkeypatch) -> None:
    answers = iter(["student"])
    captured: dict[str, object] = {}

    monkeypatch.setattr("builtins.input", lambda _prompt="": next(answers))
    monkeypatch.setattr(cli, "getpass", lambda _prompt="": "test-value")
    monkeypatch.setattr(cli, "load_course_names", lambda *_args, **_kwargs: ["课程甲"])
    monkeypatch.setattr(cli, "login_with_password", lambda *_args, **_kwargs: requests.Session())

    def fake_execute(args, names, _factory):
        captured["args"] = args
        captured["names"] = names
        return 0

    monkeypatch.setattr(cli, "execute", fake_execute)

    assert cli.run([]) == 0
    args = captured["args"]
    assert args.guided is True
    assert args.refresh is False
    assert args.start_at == "13:00:00"
    assert args.interval is None
    assert captured["names"] == ["课程甲"]


def test_guided_execute_checks_cache_before_time_and_interval_prompts(monkeypatch) -> None:
    events: list[str] = []
    course = Course("课程甲", "1", "bxxk")

    class FakeClient:
        def __init__(self, _session):
            pass

        def get_semester(self):
            events.append("semester")
            return {"p_xn": "2026", "p_xq": "1", "p_xnxq": "2026-20271"}

        def fetch_courses(self, _semester):
            raise AssertionError("有效缓存存在时不应读取服务器课程列表")

    args = cli.build_parser().parse_args([])
    args.guided = True
    monkeypatch.setattr(cli, "TisClient", FakeClient)
    monkeypatch.setattr(
        cli,
        "load_course_cache",
        lambda _path, _semester: events.append("cache") or [course],
    )
    monkeypatch.setattr(cli, "prompt_start_time", lambda _args: events.append("start"))
    monkeypatch.setattr(cli, "prompt_interval", lambda _args: events.append("interval"))
    answers = iter(["", "", "n"])
    monkeypatch.setattr("builtins.input", lambda _prompt="": next(answers))

    assert cli.execute(args, ["课程甲"], requests.Session) == 0
    assert events == ["semester", "cache", "start", "interval"]


def test_guided_execute_can_update_current_semester_cache(monkeypatch) -> None:
    events: list[str] = []
    cached = Course("课程甲", "old", "bxxk")
    refreshed = Course("课程甲", "new", "bxxk")

    class FakeClient:
        def __init__(self, _session):
            pass

        def get_semester(self):
            events.append("semester")
            return {"p_xn": "2026", "p_xq": "1", "p_xnxq": "2026-20271"}

        def fetch_courses(self, _semester):
            events.append("fetch")
            return [refreshed]

    args = cli.build_parser().parse_args([])
    args.guided = True
    monkeypatch.setattr(cli, "TisClient", FakeClient)
    monkeypatch.setattr(cli, "load_course_cache", lambda *_args: events.append("cache") or [cached])
    monkeypatch.setattr(cli, "save_course_cache", lambda *_args: events.append("save"))
    monkeypatch.setattr(cli, "prompt_start_time", lambda _args: events.append("start"))
    monkeypatch.setattr(cli, "prompt_interval", lambda _args: events.append("interval"))
    answers = iter(["y", "", "n"])
    monkeypatch.setattr("builtins.input", lambda _prompt="": next(answers))

    assert cli.execute(args, ["课程甲"], requests.Session) == 0
    assert events == ["semester", "cache", "fetch", "save", "start", "interval"]


def test_choose_courses_searches_selects_and_saves(tmp_path: Path, monkeypatch) -> None:
    path = tmp_path / "courses.txt"
    courses = [
        Course("大学物理（下）-01班-双语", "1", "bxxk"),
        Course("普通化学-01班-中文", "2", "xxxk"),
    ]
    answers = iter(["物理", "1"])
    monkeypatch.setattr("builtins.input", lambda _prompt="": next(answers))

    names = cli.choose_courses(courses, path)

    assert names == ["大学物理（下）-01班-双语"]
    assert "大学物理（下）-01班-双语" in path.read_text(encoding="utf-8")
