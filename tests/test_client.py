from pathlib import Path

import pytest
import requests
import responses

from snatcher.client import (
    Course,
    LoginRequired,
    RateLimited,
    ResponseError,
    TisClient,
    TisError,
    load_course_names,
    match_courses,
    search_courses,
    suggest_course_names,
)


def test_load_course_names_handles_bom_comments_and_blanks(tmp_path: Path) -> None:
    path = tmp_path / "courses.txt"
    path.write_text("\ufeff# comment\n\n课程甲\n 课程乙 \n", encoding="utf-8")
    assert load_course_names(path) == ["课程甲", "课程乙"]


def test_load_course_names_can_allow_an_unconfigured_file(tmp_path: Path) -> None:
    path = tmp_path / "courses.txt"
    path.write_text("# 仅有示例\n", encoding="utf-8")
    assert load_course_names(path, allow_empty=True) == []


def test_match_courses_reports_missing_and_preserves_priority() -> None:
    all_courses = [
        Course("课程乙", "2", "xxxk"),
        Course("课程甲", "1", "bxxk"),
    ]
    selected, missing = match_courses(["课程甲", "不存在", "课程乙"], all_courses)
    assert [course.task_id for course in selected] == ["1", "2"]
    assert missing == ["不存在"]


def test_search_courses_requires_all_space_separated_terms() -> None:
    courses = [
        Course("大学物理（下）-01班-双语", "1", "bxxk"),
        Course("大学物理（上）-02班-中文", "2", "bxxk"),
        Course("普通化学-01班-双语", "3", "xxxk"),
    ]
    assert search_courses("物理 双语", courses) == [courses[0]]


def test_suggest_course_names_returns_similar_names() -> None:
    courses = [
        Course("大学物理（下）-01班-双语", "1", "bxxk"),
        Course("普通化学-01班-中文", "2", "xxxk"),
    ]
    suggestions = suggest_course_names("大学物理（下）-02班-双语", courses)
    assert suggestions[0] == "大学物理（下）-01班-双语"


@responses.activate
def test_redirect_is_reported_as_login_required() -> None:
    responses.post(
        "https://tis.sustech.edu.cn/Xsxk/queryXkdqXnxq",
        status=302,
        headers={"Location": "https://tis.sustech.edu.cn/authentication/require"},
    )
    with pytest.raises(LoginRequired):
        TisClient(requests.Session()).get_semester()


@responses.activate
def test_submit_does_not_treat_full_as_terminal() -> None:
    responses.post(
        "https://tis.sustech.edu.cn/Xsxk/addGouwuche",
        json={"message": "教学班人数已满"},
        status=200,
    )
    client = TisClient(requests.Session())
    semester = {"p_xn": "2026", "p_xq": "1", "p_xnxq": "2026-20271"}
    result = client.submit(semester, Course("课程甲", "id", "bxxk"))
    assert result.success is False
    assert result.terminal is False


@responses.activate
def test_submit_success_is_terminal() -> None:
    responses.post(
        "https://tis.sustech.edu.cn/Xsxk/addGouwuche",
        json={"message": "选课成功"},
        status=200,
    )
    client = TisClient(requests.Session())
    semester = {"p_xn": "2026", "p_xq": "1", "p_xnxq": "2026-20271"}
    result = client.submit(semester, Course("课程甲", "id", "bxxk"))
    assert result.success is True
    assert result.terminal is True


@responses.activate
def test_unsuccessful_message_is_not_success() -> None:
    responses.post(
        "https://tis.sustech.edu.cn/Xsxk/addGouwuche",
        json={"message": "操作不成功"},
        status=200,
    )
    client = TisClient(requests.Session())
    semester = {"p_xn": "2026", "p_xq": "1", "p_xnxq": "2026-20271"}
    result = client.submit(semester, Course("课程甲", "id", "bxxk"))
    assert result.success is False
    assert result.terminal is False


@responses.activate
def test_submit_uses_jg_as_authoritative_result() -> None:
    responses.post(
        "https://tis.sustech.edu.cn/Xsxk/addGouwuche",
        json={"jg": "1", "message": "已处理"},
        status=200,
    )
    client = TisClient(requests.Session())
    semester = {"p_xn": "2026", "p_xq": "1", "p_xnxq": "2026-20271"}
    result = client.submit(semester, Course("课程甲", "id", "bxxk"))
    assert result.success is True
    assert result.terminal is True


@responses.activate
def test_http_429_is_rate_limited() -> None:
    responses.post("https://tis.sustech.edu.cn/Xsxk/addGouwuche", status=429)
    client = TisClient(requests.Session())
    semester = {"p_xn": "2026", "p_xq": "1", "p_xnxq": "2026-20271"}
    with pytest.raises(RateLimited):
        client.submit(semester, Course("课程甲", "id", "bxxk"))


def test_ambiguous_course_name_is_rejected() -> None:
    courses = [Course("课程甲", "1", "bxxk"), Course("课程甲", "2", "xxxk")]
    with pytest.raises(TisError, match="课程名称不唯一"):
        match_courses(["课程甲"], courses)


@responses.activate
def test_missing_course_container_is_response_error() -> None:
    responses.post(
        "https://tis.sustech.edu.cn/Xsxk/queryKxrw",
        json={"message": "参数错误"},
        status=200,
    )
    client = TisClient(requests.Session())
    semester = {"p_xn": "2026", "p_xq": "1", "p_xnxq": "2026-20271"}
    with pytest.raises(ResponseError, match="缺少 kxrwList"):
        client.fetch_courses(semester)
