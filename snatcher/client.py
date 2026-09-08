from __future__ import annotations

from dataclasses import asdict, dataclass
from difflib import get_close_matches
from html.parser import HTMLParser
import json
from pathlib import Path
import time
from typing import Any, Iterable
from urllib.parse import urljoin

import requests
from playwright.sync_api import BrowserContext, Error as PlaywrightError, Playwright, sync_playwright

TIS_BASE = "https://tis.sustech.edu.cn"
TIS_HOME = f"{TIS_BASE}/"
CAS_LOGIN = (
    "https://cas.sustech.edu.cn/cas/login"
    "?service=https%3A%2F%2Ftis.sustech.edu.cn%2Fcas"
)
COURSE_TYPES = {
    "bxxk": "通识必修选课",
    "xxxk": "通识选修选课",
    "kzyxk": "培养方案内课程",
    "zynknjxk": "非培养方案内课程",
    "jhnxk": "计划内选课新生",
}


class TisError(RuntimeError):
    pass


class LoginRequired(TisError):
    pass


class ResponseError(TisError):
    pass


class RateLimited(TisError):
    pass


@dataclass(frozen=True)
class Course:
    name: str
    task_id: str
    course_type: str

    @property
    def category(self) -> str:
        return COURSE_TYPES.get(self.course_type, self.course_type)


@dataclass(frozen=True)
class SubmitResult:
    message: str
    success: bool
    terminal: bool
    rate_limited: bool


class _LoginFormParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.forms: list[dict[str, Any]] = []
        self._current: dict[str, Any] | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = dict(attrs)
        if tag == "form":
            self._current = {"action": attributes.get("action") or CAS_LOGIN, "fields": {}}
        elif tag == "input" and self._current is not None:
            name = attributes.get("name")
            if name:
                self._current["fields"][name] = attributes.get("value") or ""

    def handle_endtag(self, tag: str) -> None:
        if tag == "form" and self._current is not None:
            self.forms.append(self._current)
            self._current = None


def login_with_password(username: str, password: str, timeout: float = 20.0) -> requests.Session:
    session = requests.Session()
    session.headers.update(
        {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36"
            ),
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        }
    )
    try:
        page = session.get(CAS_LOGIN, timeout=timeout)
        page.raise_for_status()
    except requests.RequestException as exc:
        raise TisError(f"无法打开南科大统一认证：{exc}") from exc

    parser = _LoginFormParser()
    parser.feed(page.text)
    form = next(
        (
            item
            for item in parser.forms
            if "username" in item["fields"] and "password" in item["fields"]
        ),
        None,
    )
    if form is None:
        raise LoginRequired("统一认证登录表单已变化，请改用 --login-mode browser")

    data = dict(form["fields"])
    data.update(
        {
            "username": username,
            "password": password,
            "_eventId": "submit",
            "geolocation": "",
        }
    )
    try:
        result = session.post(
            urljoin(page.url, form["action"]),
            data=data,
            timeout=timeout,
            allow_redirects=True,
            headers={"Referer": page.url},
        )
    except requests.RequestException as exc:
        raise TisError(f"统一认证请求失败：{exc}") from exc

    if "cas.sustech.edu.cn/cas/login" in result.url or 'name="username"' in result.text:
        raise LoginRequired("登录未完成：请检查账号密码；如需验证码或额外验证，请改用 --login-mode browser")
    session.headers.update(
        {
            "X-Requested-With": "XMLHttpRequest",
            "Referer": TIS_HOME,
            "Origin": TIS_BASE,
            "Accept": "application/json, text/javascript, */*; q=0.01",
        }
    )
    return session


class BrowserLogin:
    def __init__(self, profile_dir: Path, headless: bool = False) -> None:
        self.profile_dir = profile_dir
        self.headless = headless
        self._playwright: Playwright | None = None
        self.context: BrowserContext | None = None

    def __enter__(self) -> "BrowserLogin":
        self.profile_dir.mkdir(parents=True, exist_ok=True)
        self._playwright = sync_playwright().start()
        try:
            self.context = self._playwright.chromium.launch_persistent_context(
                str(self.profile_dir),
                channel="chrome",
                headless=self.headless,
                viewport={"width": 1280, "height": 900},
            )
        except PlaywrightError as exc:
            self._playwright.stop()
            self._playwright = None
            raise TisError(f"无法启动 Chrome：{exc}") from exc
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        try:
            if self.context is not None:
                self.context.close()
        finally:
            if self._playwright is not None:
                self._playwright.stop()

    def open_login(self) -> None:
        assert self.context is not None
        page = self.context.pages[0] if self.context.pages else self.context.new_page()
        try:
            page.goto(CAS_LOGIN, wait_until="domcontentloaded", timeout=30_000)
            page.bring_to_front()
        except PlaywrightError as exc:
            raise TisError(f"无法打开南科大登录页：{exc}") from exc

    def create_http_session(self) -> requests.Session:
        assert self.context is not None
        session = requests.Session()
        pages = self.context.pages
        user_agent = pages[0].evaluate("navigator.userAgent") if pages else "Mozilla/5.0"
        session.headers.update(
            {
                "User-Agent": user_agent,
                "X-Requested-With": "XMLHttpRequest",
                "Referer": TIS_HOME,
                "Origin": TIS_BASE,
                "Accept": "application/json, text/javascript, */*; q=0.01",
            }
        )
        for cookie in self.context.cookies():
            session.cookies.set(
                cookie["name"],
                cookie["value"],
                domain=cookie.get("domain"),
                path=cookie.get("path", "/"),
            )
        return session


class TisClient:
    def __init__(self, session: requests.Session, timeout: float = 15.0) -> None:
        self.session = session
        self.timeout = timeout

    def _post_json(self, path: str, data: dict[str, Any]) -> dict[str, Any]:
        try:
            response = self.session.post(
                f"{TIS_BASE}{path}",
                data=data,
                timeout=self.timeout,
                allow_redirects=False,
            )
        except requests.RequestException as exc:
            raise TisError(f"网络请求失败：{exc}") from exc

        location = response.headers.get("Location", "")
        content_type = response.headers.get("Content-Type", "").lower()
        if response.status_code in {301, 302, 303, 307, 308} or "authentication/require" in location:
            raise LoginRequired("TIS 登录状态无效或已过期")
        if response.status_code == 401:
            raise LoginRequired("TIS 登录状态无效（HTTP 401）")
        if response.status_code == 429:
            raise RateLimited("TIS 返回 HTTP 429，请降低请求频率")
        if response.status_code == 403:
            raise ResponseError("TIS 返回 HTTP 403，可能是请求被防火墙或频率限制拦截")
        if response.status_code >= 400:
            raise ResponseError(f"TIS 返回 HTTP {response.status_code}")
        if "json" not in content_type and response.text.lstrip().startswith("<"):
            raise LoginRequired("TIS 返回了登录页面，而不是接口数据")
        try:
            payload = response.json()
        except ValueError as exc:
            preview = response.text[:160].replace("\n", " ")
            raise ResponseError(f"TIS 返回了无法解析的数据：{preview}") from exc
        if not isinstance(payload, dict):
            raise ResponseError("TIS 返回的数据不是 JSON 对象")
        message = str(payload.get("message") or payload.get("msg") or "")
        if any(word in message for word in ("频率过高", "请求频繁", "流量限制", "稍后重试")):
            raise RateLimited(message)
        return payload

    def get_semester(self) -> dict[str, Any]:
        data = self._post_json("/Xsxk/queryXkdqXnxq", {"mxpylx": 1})
        required = ("p_xn", "p_xq", "p_xnxq")
        if not all(data.get(key) for key in required):
            raise ResponseError(f"学期接口缺少必要字段：{data}")
        return data

    def fetch_courses(
        self,
        semester: dict[str, Any],
        page_size: int = 1000,
        query_interval: float = 8.0,
        max_rate_limit_retries: int = 4,
    ) -> list[Course]:
        courses: list[Course] = []
        last_query_at: float | None = None

        def query(course_type: str, page: int) -> dict[str, Any]:
            nonlocal last_query_at
            for attempt in range(max_rate_limit_retries + 1):
                if last_query_at is not None:
                    remaining = query_interval - (time.monotonic() - last_query_at)
                    if remaining > 0:
                        time.sleep(remaining)
                last_query_at = time.monotonic()
                try:
                    return self._post_json(
                        "/Xsxk/queryKxrw",
                        {
                            "p_xn": semester["p_xn"],
                            "p_xq": semester["p_xq"],
                            "p_xnxq": semester["p_xnxq"],
                            "p_pylx": 1,
                            "mxpylx": 1,
                            "p_xkfsdm": course_type,
                            "pageNum": page,
                            "pageSize": page_size,
                        },
                    )
                except RateLimited:
                    if attempt >= max_rate_limit_retries:
                        raise
                    time.sleep(min(query_interval * (2 ** (attempt + 1)), 60))
            raise AssertionError("unreachable")

        for course_type in COURSE_TYPES:
            page = 1
            while True:
                payload = query(course_type, page)
                if "kxrwList" not in payload:
                    message = payload.get("message") or payload.get("msg") or payload
                    raise ResponseError(f"课程接口缺少 kxrwList：{message}")
                container = payload["kxrwList"]
                if not isinstance(container, dict) or "list" not in container:
                    raise ResponseError("课程接口中的 kxrwList 格式异常")
                rows = container["list"]
                if rows is None:
                    rows = []
                if not isinstance(rows, list):
                    raise ResponseError("课程列表字段格式异常")
                for row in rows:
                    if not isinstance(row, dict):
                        raise ResponseError("课程列表中存在格式异常的记录")
                    name = row.get("rwmc")
                    task_id = row.get("id")
                    if name and task_id:
                        courses.append(Course(str(name).strip(), str(task_id), course_type))
                if len(rows) < page_size:
                    break
                page += 1
        return courses

    def submit(self, semester: dict[str, Any], course: Course) -> SubmitResult:
        payload = self._post_json(
            "/Xsxk/addGouwuche",
            {
                "p_pylx": 1,
                "p_xktjz": "rwtjzyx",
                "p_xn": semester["p_xn"],
                "p_xq": semester["p_xq"],
                "p_xnxq": semester["p_xnxq"],
                "p_xkfsdm": course.course_type,
                "p_id": course.task_id,
                "p_sfxsgwckb": 1,
            },
        )
        message = str(payload.get("message") or payload.get("msg") or payload)
        result_code = payload.get("jg")
        success_field = payload.get("success")
        if result_code is not None:
            success = str(result_code) == "1"
        elif isinstance(success_field, bool):
            success = success_field
        else:
            success = "成功" in message and not any(
                word in message for word in ("不成功", "未成功", "失败")
            )
        terminal = success or any(word in message for word in ("已选", "冲突", "重复"))
        rate_limited = any(word in message for word in ("频繁", "流量", "稍后", "限制"))
        return SubmitResult(message, success, terminal, rate_limited)


def load_course_names(path: Path, allow_empty: bool = False) -> list[str]:
    if not path.exists():
        if allow_empty:
            return []
        raise TisError(f"课程文件不存在：{path}")
    names: list[str] = []
    for raw_line in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw_line.strip()
        if line and not line.startswith("#"):
            names.append(line)
    if not names and not allow_empty:
        raise TisError(f"课程文件中没有课程：{path}")
    return names


def search_courses(query: str, courses: Iterable[Course]) -> list[Course]:
    terms = [term.casefold() for term in query.split() if term]
    if not terms:
        raise TisError("课程搜索关键词不能为空")
    matches = [
        course
        for course in courses
        if all(term in course.name.casefold() for term in terms)
    ]
    return sorted(matches, key=lambda course: (course.name, course.course_type, course.task_id))


def suggest_course_names(name: str, courses: Iterable[Course], limit: int = 5) -> list[str]:
    unique_names = list(dict.fromkeys(course.name for course in courses))
    return get_close_matches(name, unique_names, n=limit, cutoff=0.25)


def match_courses(names: Iterable[str], courses: Iterable[Course]) -> tuple[list[Course], list[str]]:
    by_name: dict[str, list[Course]] = {}
    for course in courses:
        by_name.setdefault(course.name, []).append(course)

    selected: list[Course] = []
    missing: list[str] = []
    seen: set[tuple[str, str]] = set()
    for name in names:
        matches = by_name.get(name, [])
        unique = {(course.task_id, course.course_type): course for course in matches}
        if not unique:
            missing.append(name)
            continue
        if len(unique) > 1:
            details = ", ".join(
                f"{course.category}/{course.task_id}" for course in unique.values()
            )
            raise TisError(f"课程名称不唯一：{name}（{details}）")
        course = next(iter(unique.values()))
        key = (course.task_id, course.course_type)
        if key not in seen:
            selected.append(course)
            seen.add(key)
    return selected, missing


def save_course_cache(path: Path, semester: dict[str, Any], courses: Iterable[Course]) -> None:
    payload = {
        "semester": str(semester["p_xnxq"]),
        "courses": [asdict(course) for course in courses],
    }
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError as exc:
        raise TisError(f"无法保存课程缓存 {path}：{exc}") from exc


def load_course_cache(path: Path, semester: dict[str, Any]) -> list[Course] | None:
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        if payload.get("semester") != str(semester["p_xnxq"]):
            return None
        return [Course(**item) for item in payload["courses"]]
    except (OSError, ValueError, KeyError, TypeError):
        return None
