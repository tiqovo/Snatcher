from __future__ import annotations

from dataclasses import dataclass

import pytest

from snatcher.client import Course, RateLimited, SubmitResult
from tools.probe_submit_interval import (
    ProbeObservation,
    build_candidate_pool,
    build_parser,
    conclude_probe,
    find_reusable_course,
    parse_intervals,
    run_repeated_probe,
)


SEMESTER = {"p_xn": "2026", "p_xq": "1", "p_xnxq": "2026-20271"}
COURSES = [
    Course("课程甲-01班-中文", "1", "bxxk"),
    Course("课程甲-02班-中文", "2", "bxxk"),
    Course("课程乙-01班-中文", "3", "xxxk"),
    Course("课程丙-01班-中文", "4", "kzyxk"),
    Course("课程丁-01班-中文", "5", "xxxk"),
]


@dataclass
class FakeClock:
    value: float = 100.0

    def sleep(self, seconds: float) -> None:
        self.value += seconds

    def monotonic(self) -> float:
        return self.value


class FakeClient:
    def __init__(self, clock: FakeClock, outcomes):
        self.clock = clock
        self.outcomes = iter(outcomes)
        self.submitted: list[str] = []

    def submit(self, _semester, course):
        self.submitted.append(course.task_id)
        outcome, duration = next(self.outcomes)
        self.clock.value += duration
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def result(
    message: str = "操作成功",
    *,
    success: bool = True,
    terminal: bool = True,
    rate_limited: bool = False,
) -> SubmitResult:
    return SubmitResult(message, success, terminal, rate_limited)


def observation(
    actual: float | None,
    *,
    planned: float | None = 1.0,
    limited: bool = False,
) -> ProbeObservation:
    return ProbeObservation(COURSES[0], planned, actual, 0.2, limited, "测试响应")


def test_parser_supports_browser_login_with_persistent_profile() -> None:
    args = build_parser().parse_args(["--login-mode", "browser", "--profile", "test-profile"])
    assert args.login_mode == "browser"
    assert str(args.profile) == "test-profile"


def test_parse_intervals_allows_repeated_minimum_but_rejects_acceleration_reversal() -> None:
    assert parse_intervals("1.5,1,0.75,0.5,0.5") == (1.5, 1.0, 0.75, 0.5, 0.5)
    with pytest.raises(Exception, match="1–6"):
        parse_intervals("6,5,4,3,2,1,0.5")
    with pytest.raises(Exception, match="从慢到快"):
        parse_intervals("0.5,1")
    with pytest.raises(Exception, match="不能小于 0.5"):
        parse_intervals("0.49")


def test_candidate_pool_prefers_configured_name_and_uses_distinct_courses() -> None:
    candidates = build_candidate_pool(
        COURSES,
        preferred_names=["课程甲-02班-中文"],
        limit=4,
    )

    assert candidates[0].task_id == "2"
    assert len(candidates) == 4
    assert len({course.task_id for course in candidates}) == 4
    assert len({course.name.split("-")[0] for course in candidates}) == 4


def test_finder_skips_conflict_then_reuses_successful_course() -> None:
    clock = FakeClock()
    client = FakeClient(
        clock,
        [
            (result("上课时间冲突", success=False, terminal=True), 0.4),
            (result(), 0.3),
        ],
    )

    course, baseline, started_at = find_reusable_course(
        client,
        SEMESTER,
        COURSES[:2],
        sleep=clock.sleep,
        monotonic=clock.monotonic,
        output=lambda _message: None,
    )

    assert course == COURSES[1]
    assert client.submitted == ["1", "2"]
    assert baseline.message == "操作成功"
    assert started_at == pytest.approx(105.4)


def test_finder_accepts_already_selected_course_without_selecting_another() -> None:
    clock = FakeClock()
    client = FakeClient(
        clock,
        [(result("该课程已选", success=False, terminal=True), 0.2)],
    )

    course, _baseline, _started_at = find_reusable_course(
        client,
        SEMESTER,
        COURSES[:2],
        sleep=clock.sleep,
        monotonic=clock.monotonic,
        output=lambda _message: None,
    )

    assert course == COURSES[0]
    assert client.submitted == ["1"]


def test_repeated_probe_targets_request_start_times_not_response_plus_interval() -> None:
    clock = FakeClock(100.4)
    baseline = ProbeObservation(COURSES[0], None, None, 0.4, False, "操作成功")
    client = FakeClient(
        clock,
        [
            (result("该课程已选", success=False), 0.8),
            (result("该课程已选", success=False), 0.7),
            (result("该课程已选", success=False), 0.6),
        ],
    )

    observations, conclusion = run_repeated_probe(
        client,
        SEMESTER,
        COURSES[0],
        baseline,
        baseline_started_at=100.0,
        intervals=(2.0, 1.25, 1.0),
        sleep=clock.sleep,
        monotonic=clock.monotonic,
        output=lambda _message: None,
    )

    assert [item.actual_interval for item in observations] == [None, 2.0, 1.25, 1.0]
    assert client.submitted == ["1", "1", "1"]
    assert conclusion.fastest_accepted == 1.0


def test_repeated_probe_reports_latency_when_response_prevents_target_interval() -> None:
    clock = FakeClock(101.4)
    baseline = ProbeObservation(COURSES[0], None, None, 1.4, False, "操作成功")
    client = FakeClient(clock, [(result("该课程已选", success=False), 0.2)])

    observations, _conclusion = run_repeated_probe(
        client,
        SEMESTER,
        COURSES[0],
        baseline,
        baseline_started_at=100.0,
        intervals=(1.0,),
        sleep=clock.sleep,
        monotonic=clock.monotonic,
        output=lambda _message: None,
    )

    assert observations[1].actual_interval == pytest.approx(1.4)


def test_repeated_probe_stops_immediately_after_rate_limit() -> None:
    clock = FakeClock(100.2)
    baseline = ProbeObservation(COURSES[0], None, None, 0.2, False, "操作成功")
    client = FakeClient(
        clock,
        [
            (result("该课程已选", success=False), 0.2),
            (RateLimited("请求频繁"), 0.1),
            (result(), 0.2),
        ],
    )

    observations, conclusion = run_repeated_probe(
        client,
        SEMESTER,
        COURSES[0],
        baseline,
        baseline_started_at=100.0,
        intervals=(2.0, 1.5, 1.0),
        sleep=clock.sleep,
        monotonic=clock.monotonic,
        output=lambda _message: None,
    )

    assert client.submitted == ["1", "1"]
    assert len(observations) == 3
    assert conclusion.fastest_accepted == 2.0
    assert conclusion.first_limited == 1.5
    assert conclusion.recommended_interval == 2.0


def test_two_near_minimum_samples_support_half_second_recommendation() -> None:
    conclusion = conclude_probe(
        [
            observation(None, planned=None),
            observation(0.75, planned=0.75),
            observation(0.52, planned=0.5),
            observation(0.51, planned=0.5),
        ]
    )

    assert conclusion.fastest_accepted == 0.51
    assert conclusion.first_limited is None
    assert conclusion.recommended_interval == 0.5
