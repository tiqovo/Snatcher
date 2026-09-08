from __future__ import annotations

import argparse
from collections.abc import Callable
from datetime import datetime, timedelta
from getpass import getpass
from pathlib import Path
import sys
import time

import requests

from .client import (
    BrowserLogin,
    LoginRequired,
    RateLimited,
    TisClient,
    TisError,
    load_course_cache,
    load_course_names,
    login_with_password,
    match_courses,
    save_course_cache,
    search_courses,
    suggest_course_names,
)


AUTO_START_INTERVAL = 1.5
MIN_INTERVAL = 0.5


def parse_interval(value: str) -> float | None:
    if value.casefold() == "auto":
        return None
    try:
        interval = float(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("间隔必须是 auto 或不小于 0.5 的秒数") from exc
    if interval < MIN_INTERVAL:
        raise argparse.ArgumentTypeError("提交间隔不能小于 0.5 秒")
    return interval


def default_data_path(name: str) -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent / name
    return Path(name)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="南科大 TIS 抢课助手")
    parser.add_argument(
        "--courses",
        type=Path,
        default=default_data_path("courses.txt"),
        help="课程列表文件",
    )
    parser.add_argument(
        "--login-mode",
        choices=("password", "browser"),
        default="password",
        help="登录方式",
    )
    parser.add_argument(
        "--profile",
        type=Path,
        default=default_data_path(".browser-profile"),
        help="浏览器登录资料目录",
    )
    parser.add_argument(
        "--cache",
        type=Path,
        default=default_data_path("course-cache.json"),
        help="课程信息缓存",
    )
    parser.add_argument(
        "--interval",
        type=parse_interval,
        default=None,
        metavar="auto|秒数",
        help="提交间隔；默认自动寻找可用间隔，最低 0.5 秒",
    )
    parser.add_argument("--duration", type=int, default=180, help="最多运行秒数")
    parser.add_argument(
        "--start-at",
        default="13:00:00",
        help="开始时间，格式 HH:MM:SS；默认 13:00:00",
    )
    parser.add_argument("--start-now", action="store_true", help="忽略开始时间并立即运行")
    parser.add_argument("--refresh", action="store_true", help="忽略课程缓存并重新读取课程列表")
    parser.add_argument("--search", metavar="关键词", help="搜索当前学期的完整教学班名称（只读）")
    parser.add_argument("--prepare", action="store_true", help="只登录并检查课程，不提交选课")
    parser.add_argument("--confirm-submit", action="store_true", help="明确允许发送选课请求")
    return parser


def prompt_start_time(args: argparse.Namespace) -> None:
    while True:
        value = input("[4/6] 开始时间 [13:00:00；输入 now 立即开始]：").strip()
        if not value:
            args.start_at = "13:00:00"
            args.start_now = False
            return
        if value.casefold() in {"now", "立即", "现在"}:
            args.start_now = True
            return
        try:
            datetime.strptime(value, "%H:%M:%S")
        except ValueError:
            print("时间格式不正确，请使用 HH:MM:SS，例如 13:00:00。")
            continue
        args.start_at = value
        args.start_now = False
        return


def prompt_interval(args: argparse.Namespace) -> None:
    while True:
        value = input("[5/6] 抢课间隔 [直接回车=自动，最低 0.5 秒]：").strip()
        try:
            args.interval = parse_interval(value or "auto")
        except argparse.ArgumentTypeError as exc:
            print(exc)
            continue
        return


def wait_until(value: str | None) -> None:
    if not value:
        return
    try:
        parsed = datetime.strptime(value, "%H:%M:%S").time()
    except ValueError as exc:
        raise TisError("--start-at 必须使用 HH:MM:SS 格式") from exc
    now = datetime.now()
    target = datetime.combine(now.date(), parsed)
    if target <= now:
        target += timedelta(days=1)
    print(f"等待到 {target:%Y-%m-%d %H:%M:%S} 开始。按 Ctrl+C 可取消。")
    while True:
        remaining = (target - datetime.now()).total_seconds()
        if remaining <= 0:
            print()
            return
        print(f"\r剩余 {remaining:7.1f} 秒", end="", flush=True)
        time.sleep(min(0.5, remaining))


def print_plan(courses: list, missing: list[str], all_courses: list) -> None:
    print("\n匹配到的课程：")
    for index, course in enumerate(courses, 1):
        print(f"  {index}. [{course.category}] {course.name} ({course.task_id})")
    if missing:
        print("\n未找到的课程：")
        for name in missing:
            print(f"  - {name}")
            suggestions = suggest_course_names(name, all_courses)
            if suggestions:
                print("    可能的教学班：")
                for suggestion in suggestions:
                    print(f"      · {suggestion}")


def save_selected_courses(path: Path, names: list[str]) -> None:
    content = (
        "# 每行填写一个 TIS 当前显示的完整教学班名称。\n"
        "# 课程会按这里的顺序开始尝试。\n"
        + "\n".join(names)
        + "\n"
    )
    try:
        path.write_text(content, encoding="utf-8")
    except OSError as exc:
        raise TisError(f"无法保存课程列表 {path}：{exc}") from exc


def choose_courses(courses: list, path: Path) -> list[str]:
    print("\n[3/6] courses.txt 还没有实际目标课程，现在可以直接搜索并选择。")
    while True:
        query = input("请输入课程关键词（多个关键词用空格分隔，输入 q 取消）：").strip()
        if query.casefold() == "q":
            raise TisError("已取消课程选择")
        matches = search_courses(query, courses)
        if not matches:
            print("没有找到同时包含这些关键词的教学班，请更换关键词。")
            continue
        if len(matches) > 30:
            print(f"找到 {len(matches)} 门课程，请增加关键词缩小范围（最多显示 30 门）。")
            continue
        print()
        for index, course in enumerate(matches, 1):
            print(f"  {index}. [{course.category}] {course.name}")
        raw = input("请输入要选择的编号，多个编号用逗号分隔；输入 r 重新搜索：").strip()
        if raw.casefold() == "r":
            continue
        try:
            indexes = [int(item.strip()) for item in raw.replace("，", ",").split(",")]
            selected = [matches[index - 1] for index in indexes]
            if not indexes or any(index < 1 or index > len(matches) for index in indexes):
                raise ValueError
        except (ValueError, IndexError):
            print("课程编号无效，请重新搜索并选择。")
            continue
        names = list(dict.fromkeys(course.name for course in selected))
        save_selected_courses(path, names)
        print(f"已将 {len(names)} 门课程保存到 {path}。")
        return names


def auto_interval_after(
    interval: float,
    floor: float,
    stable_responses: int,
    rate_limited: bool,
) -> tuple[float, float, int]:
    if rate_limited:
        floor = max(floor, min(interval + 0.25, 60.0))
        return min(max(interval * 2, 5.0), 60.0), floor, 0
    stable_responses += 1
    if stable_responses >= 4 and interval > floor:
        return max(floor, interval - 0.25), floor, 0
    return interval, floor, stable_responses


def execute(
    args: argparse.Namespace,
    names: list[str],
    session_factory: Callable[[], requests.Session],
) -> int:
    client = TisClient(session_factory())
    semester = client.get_semester()
    print(f"当前选课学期：{semester['p_xn']} 学年第 {semester['p_xq']} 学期 ({semester['p_xnxq']})")

    courses = None if args.refresh else load_course_cache(args.cache, semester)
    if courses is not None and args.guided:
        print(f"检测到当前学期的课程缓存，共 {len(courses)} 门课程。")
        update = input("是否从服务器重新获取并更新缓存？[y/N]：").strip().casefold()
        if update in {"y", "yes"}:
            courses = None
        else:
            print("保留并使用当前学期缓存。")

    if courses is None:
        print("正在读取服务器课程列表；各分类之间会主动等待以避免限流……")
        courses = client.fetch_courses(semester)
        save_course_cache(args.cache, semester, courses)
        print(f"课程列表读取完成并已更新缓存，共 {len(courses)} 门。")
    elif not args.guided:
        print(f"已从缓存读取 {len(courses)} 门课程。使用 --refresh 可强制更新。")

    if args.search:
        matches = search_courses(args.search, courses)
        print(f"\n搜索结果（{len(matches)} 门）：")
        for course in matches:
            print(f"  [{course.category}] {course.name} ({course.task_id})")
        if not matches:
            print("  没有找到包含全部关键词的教学班。")
        print("\n搜索模式只读取课程列表，没有发送选课请求。")
        return 0

    if args.guided and names:
        print("\n[3/6] 当前目标课程：")
        for index, name in enumerate(names, 1):
            print(f"  {index}. {name}")
        if input("使用这些课程？[Y/n]：").strip().casefold() in {"n", "no"}:
            names = []
    if not names:
        names = choose_courses(courses, args.courses)

    if args.guided:
        prompt_start_time(args)
        prompt_interval(args)

    selected, missing = match_courses(names, courses)
    print_plan(selected, missing, courses)
    if missing:
        raise TisError("存在未找到的课程，请根据建议重新选择或修改 courses.txt")
    if not selected:
        raise TisError("没有可提交的课程")
    if args.prepare:
        print("\n检查完成；--prepare 模式没有发送选课请求。")
        return 0

    if args.guided and not args.confirm_submit:
        answer = input("\n[6/6] 确认按上述配置执行抢课？[y/N]：").strip().casefold()
        if answer not in {"y", "yes"}:
            print("已取消，没有发送选课请求。")
            return 0

    wait_until(None if args.start_now else args.start_at)
    client = TisClient(session_factory())
    current_semester = client.get_semester()
    if current_semester["p_xnxq"] != semester["p_xnxq"]:
        raise TisError("等待期间选课学期发生变化，请重新启动并刷新课程")
    semester = current_semester

    automatic = args.interval is None
    base_interval = AUTO_START_INTERVAL if automatic else args.interval
    interval = base_interval
    interval_floor = MIN_INTERVAL
    stable_responses = 0
    mode = (
        f"自动模式，从 {AUTO_START_INTERVAL:.2f} 秒开始"
        if automatic
        else f"固定 {interval:.2f} 秒"
    )
    print(f"\n开始按配置顺序轮询提交；抢课间隔：{mode}。")
    print("名额已满时会轮询下一门；成功、已选、冲突或重复时停止重试对应课程。")

    deadline = time.monotonic() + args.duration
    queue = list(selected)
    while queue and time.monotonic() < deadline:
        course = queue[0]
        rate_limited = False
        try:
            result = client.submit(semester, course)
        except RateLimited as exc:
            rate_limited = True
            print(f"[{datetime.now():%H:%M:%S}] {exc}")
        else:
            stamp = datetime.now().strftime("%H:%M:%S")
            print(f"[{stamp}] {course.name}: {result.message}")
            rate_limited = result.rate_limited
            if result.terminal:
                queue.pop(0)
            elif len(queue) > 1:
                queue.append(queue.pop(0))

        previous_interval = interval
        if automatic:
            interval, interval_floor, stable_responses = auto_interval_after(
                interval,
                interval_floor,
                stable_responses,
                rate_limited,
            )
        elif rate_limited:
            interval = min(max(interval * 2, 5.0), 60.0)
        elif interval > base_interval:
            interval = max(base_interval, interval / 2)

        if interval != previous_interval:
            reason = "检测到频率限制" if rate_limited else "连续请求未触发限流"
            print(f"{reason}，下一次提交间隔调整为 {interval:.2f} 秒。")
        if queue and time.monotonic() < deadline:
            time.sleep(min(interval, max(0, deadline - time.monotonic())))

    if queue:
        print("运行时间结束，仍未完成：")
        for course in queue:
            print(f"  - {course.name}")
        return 2
    print("所有目标课程均已得到终态响应。")
    return 0


def run(argv: list[str] | None = None) -> int:
    raw_argv = sys.argv[1:] if argv is None else argv
    guided = len(raw_argv) == 0
    args = build_parser().parse_args(raw_argv)
    args.guided = guided

    if args.duration <= 0:
        raise TisError("--duration 必须大于 0")
    if not guided and not args.search and not args.prepare and not args.confirm_submit:
        raise TisError("实际提交必须添加 --confirm-submit；如只检查，请添加 --prepare")

    names = [] if args.search else load_course_names(args.courses, allow_empty=guided)
    if guided:
        print("南科大 TIS 抢课助手\n")
        print("直接按回车会使用方括号中的默认值。密码不会显示或保存。")

    if args.login_mode == "password":
        label = "[1/6] 请输入学号：" if guided else "请输入学号："
        username = input(label).strip()
        if not username:
            raise TisError("学号不能为空")
        password_label = (
            "[2/6] 请输入统一认证密码（输入不显示且不会保存）："
            if guided
            else "请输入统一认证密码（输入不显示且不会保存）："
        )
        password = getpass(password_label)
        try:
            print("正在登录统一认证……")
            session = login_with_password(username, password)
        finally:
            password = ""
        print("登录成功。")
        return execute(args, names, lambda: session)

    print("将打开 Chrome。请在浏览器中完成南科大统一身份认证并进入 TIS。")
    with BrowserLogin(args.profile) as login:
        login.open_login()
        input("登录完成后回到此窗口，按回车继续：")
        return execute(args, names, login.create_http_session)


def main() -> int:
    try:
        return run()
    except KeyboardInterrupt:
        print("\n已取消。", file=sys.stderr)
        return 130
    except LoginRequired as exc:
        print(f"登录失效：{exc}", file=sys.stderr)
        return 3
    except TisError as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
