from __future__ import annotations

import argparse
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from getpass import getpass
import math
from pathlib import Path
import re
import time

from snatcher.client import (
    BrowserLogin,
    Course,
    RateLimited,
    TisClient,
    TisError,
    load_course_cache,
    load_course_names,
    login_with_password,
    search_courses,
)

DEFAULT_INTERVALS = (1.5, 1.0, 0.75, 0.5, 0.5)
DEFAULT_DISCOVERY_INTERVAL = 5.0
MIN_INTERVAL = 0.5
MAX_CANDIDATES = 4


@dataclass(frozen=True)
class ProbeObservation:
    course: Course
    planned_interval: float | None
    actual_interval: float | None
    response_time: float
    rate_limited: bool
    message: str


@dataclass(frozen=True)
class ProbeConclusion:
    fastest_accepted: float | None
    first_limited: float | None
    recommended_interval: float | None
    summary: str


def parse_intervals(value: str) -> tuple[float, ...]:
    try:
        intervals = tuple(float(item.strip()) for item in value.split(",") if item.strip())
    except ValueError as exc:
        raise argparse.ArgumentTypeError("间隔必须是逗号分隔的秒数") from exc
    if not intervals or len(intervals) > 6:
        raise argparse.ArgumentTypeError("必须提供 1–6 个测试间隔")
    if any(interval < MIN_INTERVAL for interval in intervals):
        raise argparse.ArgumentTypeError("测试间隔不能小于 0.5 秒")
    if any(left < right for left, right in zip(intervals, intervals[1:])):
        raise argparse.ArgumentTypeError("测试间隔必须从慢到快排列")
    return intervals


def conclude_probe(observations: Sequence[ProbeObservation]) -> ProbeConclusion:
    measured = [
        item
        for item in observations
        if item.actual_interval is not None and item.planned_interval is not None
    ]
    accepted = [item for item in measured if not item.rate_limited]
    limited = next((item for item in measured if item.rate_limited), None)
    fastest = min((item.actual_interval for item in accepted), default=None)
    first_limited = limited.actual_interval if limited else None

    if fastest is not None and first_limited is not None:
        recommendation = max(
            MIN_INTERVAL,
            math.ceil(max(fastest, first_limited + 0.25) * 4) / 4,
        )
        summary = (
            f"实际请求起始间隔 {first_limited:.3f} 秒触发限流，"
            f"{fastest:.3f} 秒未触发；正式脚本建议暂取 {recommendation:.2f} 秒。"
        )
    elif fastest is not None:
        near_minimum = [item for item in accepted if item.actual_interval <= 0.75]
        if len(near_minimum) >= 2:
            recommendation = MIN_INTERVAL
            summary = (
                f"最快实际请求起始间隔为 {fastest:.3f} 秒，且有 "
                f"{len(near_minimum)} 个不大于 0.75 秒的样本均未限流；"
                "正式脚本可使用 0.50 秒，并保留动态退避。"
            )
        else:
            recommendation = max(
                MIN_INTERVAL,
                math.ceil((fastest + 0.25) * 4) / 4,
            )
            summary = (
                f"最快实际请求起始间隔为 {fastest:.3f} 秒且未限流，"
                f"但接近 1 秒的重复样本不足；建议暂取 {recommendation:.2f} 秒。"
            )
    elif first_limited is not None:
        recommendation = max(5.0, math.ceil(first_limited * 2 * 4) / 4)
        summary = (
            f"首个有效样本在实际间隔 {first_limited:.3f} 秒即触发限流；"
            f"尚未测得安全边界，建议至少从 {recommendation:.2f} 秒开始。"
        )
    else:
        recommendation = None
        summary = "没有得到可用于判断间隔的样本，维持正式脚本当前设置。"

    return ProbeConclusion(fastest, first_limited, recommendation, summary)


def _is_reusable_target(success: bool, message: str) -> bool:
    return success or any(word in message for word in ("已选", "重复"))


def _course_base_name(name: str) -> str:
    return re.sub(r"-\d+班.*$", "", name)


def build_candidate_pool(
    courses: Sequence[Course],
    preferred_names: Iterable[str] = (),
    query: str | None = None,
    limit: int = MAX_CANDIDATES,
) -> list[Course]:
    available = search_courses(query, courses) if query else list(courses)
    by_name = {course.name: course for course in available}
    candidates: list[Course] = []
    seen_keys: set[tuple[str, str]] = set()
    seen_bases: set[str] = set()

    def add(course: Course) -> None:
        key = (course.task_id, course.course_type)
        base = _course_base_name(course.name)
        if key not in seen_keys and base not in seen_bases and len(candidates) < limit:
            candidates.append(course)
            seen_keys.add(key)
            seen_bases.add(base)

    for name in preferred_names:
        course = by_name.get(name)
        if course is not None:
            add(course)

    priority = {"xxxk": 0, "kzyxk": 1, "bxxk": 2, "zynknjxk": 3, "jhnxk": 4}
    for course in sorted(
        available,
        key=lambda item: (priority.get(item.course_type, 9), item.name, item.task_id),
    ):
        add(course)
    return candidates


def find_reusable_course(
    client: TisClient,
    semester: dict,
    candidates: Sequence[Course],
    *,
    discovery_interval: float = DEFAULT_DISCOVERY_INTERVAL,
    sleep: Callable[[float], None] = time.sleep,
    monotonic: Callable[[], float] = time.monotonic,
    output: Callable[[str], None] = print,
) -> tuple[Course, ProbeObservation, float]:
    previous_started_at: float | None = None
    for index, course in enumerate(candidates):
        if previous_started_at is not None:
            sleep(discovery_interval)
        started_at = monotonic()
        actual = None if previous_started_at is None else started_at - previous_started_at
        previous_started_at = started_at
        try:
            result = client.submit(semester, course)
        except RateLimited as exc:
            raise TisError(f"寻找测试课程时触发限流，已停止：{exc}") from exc
        finished_at = monotonic()
        observation = ProbeObservation(
            course,
            None if index == 0 else discovery_interval,
            actual,
            finished_at - started_at,
            result.rate_limited,
            result.message,
        )
        output(f"候选 {index + 1}/{len(candidates)}：{course.name}：{result.message}")
        if result.rate_limited:
            raise TisError("寻找测试课程时触发限流，已停止")
        if _is_reusable_target(result.success, result.message):
            return course, observation, started_at
    raise TisError("候选课程中没有找到成功选上或已经选过的课程；可用 --query 更换候选范围")


def run_repeated_probe(
    client: TisClient,
    semester: dict,
    course: Course,
    baseline: ProbeObservation,
    baseline_started_at: float,
    intervals: Sequence[float] = DEFAULT_INTERVALS,
    *,
    sleep: Callable[[float], None] = time.sleep,
    monotonic: Callable[[], float] = time.monotonic,
    output: Callable[[str], None] = print,
) -> tuple[list[ProbeObservation], ProbeConclusion]:
    observations = [baseline]
    previous_started_at = baseline_started_at
    for index, planned in enumerate(intervals, 1):
        target = previous_started_at + planned
        remaining = target - monotonic()
        if remaining > 0:
            sleep(remaining)
        started_at = monotonic()
        actual = started_at - previous_started_at
        previous_started_at = started_at
        try:
            result = client.submit(semester, course)
        except RateLimited as exc:
            finished_at = monotonic()
            observation = ProbeObservation(
                course, planned, actual, finished_at - started_at, True, str(exc)
            )
            observations.append(observation)
            output(
                f"样本 {index}/{len(intervals)} | 目标 {planned:.2f} 秒 | "
                f"实际 {actual:.3f} 秒 | 限流：{exc}"
            )
            break
        finished_at = monotonic()
        observation = ProbeObservation(
            course,
            planned,
            actual,
            finished_at - started_at,
            result.rate_limited,
            result.message,
        )
        observations.append(observation)
        status = "限流" if result.rate_limited else "未限流"
        output(
            f"样本 {index}/{len(intervals)} | 目标 {planned:.2f} 秒 | "
            f"实际 {actual:.3f} 秒 | 响应 {observation.response_time:.3f} 秒 | "
            f"{status}：{result.message}"
        )
        if result.rate_limited:
            break
    conclusion = conclude_probe(observations)
    return observations, conclusion


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="一次性真实选课请求间隔探测工具")
    parser.add_argument("--cache", type=Path, default=Path("course-cache.json"))
    parser.add_argument("--courses", type=Path, default=Path("courses.txt"))
    parser.add_argument(
        "--login-mode",
        choices=("password", "browser"),
        default="password",
        help="登录方式；统一认证重置连接时可改用 browser",
    )
    parser.add_argument(
        "--profile",
        type=Path,
        default=Path(".browser-profile"),
        help="Chrome 浏览器登录资料目录",
    )
    parser.add_argument("--query", help="可选：限制自动候选课程的关键词")
    parser.add_argument(
        "--intervals",
        type=parse_intervals,
        default=DEFAULT_INTERVALS,
        metavar="1.5,1,0.75,0.5,0.5",
        help="按请求开始时间计算、由慢到快的 1–6 个测试间隔",
    )
    return parser


def execute_probe(args: argparse.Namespace, session: object) -> int:
    client = TisClient(session)
    semester = client.get_semester()
    courses = load_course_cache(args.cache, semester)
    if courses is None:
        raise TisError("当前学期课程缓存不可用，请先运行正式程序刷新课程缓存")
    preferred = load_course_names(args.courses, allow_empty=True)
    candidates = build_candidate_pool(courses, preferred, args.query)
    if not candidates:
        raise TisError("没有找到候选课程")

    print("\n工具将依次尝试以下候选，找到第一门已选或能成功选上的课程后停止寻找：")
    for index, course in enumerate(candidates, 1):
        print(f"  {index}. [{course.category}] {course.name}")
    print(
        f"找到目标后只会反复提交同一门课程，测试请求起始间隔："
        f"{', '.join(f'{value:g}' for value in args.intervals)} 秒。"
    )
    print("最多新增选中一门课程；若候选均不可用，寻找阶段最多发送 4 次请求。")
    if input("输入 PROBE 开始：").strip() != "PROBE":
        print("已取消，没有发送选课请求。")
        return 0

    course, baseline, baseline_started_at = find_reusable_course(
        client, semester, candidates
    )
    print(f"\n使用测试课程：{course.name}")
    observations, conclusion = run_repeated_probe(
        client,
        semester,
        course,
        baseline,
        baseline_started_at,
        args.intervals,
    )
    measured = [item for item in observations if item.planned_interval is not None]
    print("\n探测结论：")
    print(f"  测试课程上的请求：{len(observations)} 次")
    print(f"  有效间隔样本：{len(measured)} 个")
    print(f"  {conclusion.summary}")
    print("  结论按请求开始时间计算，只适用于本次账号、会话和服务器负载。")
    return 0


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    print("此工具会自动寻找一门可复用的测试课程，并发送真实选课请求。")
    if args.login_mode == "browser":
        print("将打开 Chrome。请完成统一身份认证并进入 TIS。")
        with BrowserLogin(args.profile) as login:
            login.open_login()
            input("浏览器登录完成后回到这里，按回车继续：")
            return execute_probe(args, login.create_http_session())

    username = input("学号：").strip()
    if not username:
        raise TisError("学号不能为空")
    password = getpass("统一认证密码（不显示且不保存）：")
    try:
        session = login_with_password(username, password)
    finally:
        password = ""
    return execute_probe(args, session)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("\n已取消。")
        raise SystemExit(130)
    except TisError as exc:
        print(f"错误：{exc}")
        raise SystemExit(1)
