"""A small in-memory page the application runner can fill without Playwright.

It understands the selectors, labels, and buttons the runner actually uses.
A forward button stays disabled until the phone field has a value, so a run
only continues after that field is filled.
"""

from __future__ import annotations

import re
from typing import Callable


class ScriptedNode:
    def __init__(
        self,
        tag: str,
        attrs: dict[str, str] | None = None,
        text: str = "",
        *,
        visible: bool = True,
    ) -> None:
        self.tag = tag
        self.attrs = dict(attrs or {})
        self.text = text
        self.visible = visible
        self.value = ""
        self.checked = False
        self.files = ""
        self.children: list[ScriptedNode] = []
        self.parent: ScriptedNode | None = None
        self.page: ScriptedPage | None = None
        self.on_click: Callable[[], None] | None = None

    def add(self, child: ScriptedNode) -> ScriptedNode:
        child.parent = self
        child.page = self.page
        self.children.append(child)
        for node in child.walk():
            node.page = self.page
        return child

    def walk(self):
        yield self
        for child in self.children:
            yield from child.walk()

    def is_visible(self) -> bool:
        node: ScriptedNode | None = self
        while node is not None:
            if not node.visible:
                return False
            node = node.parent
        return True

    def get_attribute(self, name: str) -> str | None:
        return self.attrs.get(name)

    def inner_text(self, timeout: int = 0) -> str:
        parts: list[str] = []
        if self.text:
            parts.append(self.text)
        for child in self.children:
            if child.is_visible():
                piece = child.inner_text()
                if piece:
                    parts.append(piece)
        return " ".join(parts)

    def input_value(self, timeout: int = 0) -> str:
        return self.value

    def fill(self, text: str, timeout: int = 0) -> None:
        self.value = text
        if self.page is not None:
            self.page.refresh_gates()

    def click(self, timeout: int = 0, force: bool = False) -> None:
        if not force and "disabled" in self.attrs:
            raise RuntimeError("disabled")
        if self.on_click is not None:
            self.on_click()

    def check(self, timeout: int = 0) -> None:
        self.checked = True

    def is_checked(self) -> bool:
        return self.checked

    def press(self, key: str, timeout: int = 0) -> None:
        return None

    def set_input_files(self, path: str) -> None:
        self.files = path
        self.value = path

    def scroll_into_view_if_needed(self, timeout: int = 0) -> None:
        return None

    def focus(self) -> None:
        return None

    def evaluate(self, script: str, arg: str | None = None):
        if "tagName" in script:
            return self.tag.upper()
        if "header, nav, footer" in script:
            current: ScriptedNode | None = self
            while current is not None:
                if current.tag in {"header", "nav", "footer"}:
                    return True
                current = current.parent
            return False
        if "selectedIndex" in script:
            return ""
        if "getElementById" in script and self.page is not None:
            found = self.page.node_by_id(str(arg or "").split()[0])
            return found.inner_text() if found is not None else ""
        if "closest" in script and "innerText" in script:
            current = self
            while current is not None:
                classes = current.attrs.get("class", "")
                if current.tag in {"fieldset", "li"} or any(
                    token in classes for token in ("field", "question")
                ):
                    return current.inner_text()[:280]
                current = current.parent
            return ""
        if "HTMLInputElement" in script or "HTMLTextAreaElement" in script:
            self.value = arg or ""
            if self.page is not None:
                self.page.refresh_gates()
            return None
        if "el.click" in script:
            self.click(force=True)
            return None
        return None

    def locator(self, selector: str) -> ScriptedLocator:
        return ScriptedLocator(query(self, selector))

    def get_by_role(self, role: str, name: re.Pattern[str] | None = None) -> ScriptedLocator:
        found: list[ScriptedNode] = []
        for node in self.walk():
            if node is self:
                continue
            if role == "button" and node.tag != "button":
                continue
            if name is not None and name.search(_accessible_name(node)) is None:
                continue
            found.append(node)
        return ScriptedLocator(found)


class ScriptedLocator:
    def __init__(self, nodes: list[ScriptedNode]) -> None:
        self._nodes = nodes

    def count(self) -> int:
        return len(self._nodes)

    def nth(self, index: int) -> ScriptedLocator:
        return ScriptedLocator([self._nodes[index]])

    @property
    def first(self) -> ScriptedLocator:
        return ScriptedLocator(self._nodes[:1])

    def _only(self) -> ScriptedNode:
        if not self._nodes:
            raise RuntimeError("empty locator")
        return self._nodes[0]

    def locator(self, selector: str) -> ScriptedLocator:
        found: list[ScriptedNode] = []
        seen: set[int] = set()
        for node in self._nodes:
            for match in query(node, selector):
                if id(match) in seen:
                    continue
                seen.add(id(match))
                found.append(match)
        return ScriptedLocator(found)

    def get_by_role(self, role: str, name: re.Pattern[str] | None = None) -> ScriptedLocator:
        found: list[ScriptedNode] = []
        seen: set[int] = set()
        for node in self._nodes:
            for match in node.get_by_role(role, name=name)._nodes:
                if id(match) in seen:
                    continue
                seen.add(id(match))
                found.append(match)
        return ScriptedLocator(found)

    def is_visible(self) -> bool:
        return bool(self._nodes) and self._nodes[0].is_visible()

    def get_attribute(self, name: str) -> str | None:
        return self._only().get_attribute(name)

    def inner_text(self, timeout: int = 0) -> str:
        return self._only().inner_text(timeout)

    def input_value(self, timeout: int = 0) -> str:
        return self._only().input_value(timeout)

    def fill(self, text: str, timeout: int = 0) -> None:
        self._only().fill(text, timeout)

    def click(self, timeout: int = 0, force: bool = False) -> None:
        self._only().click(timeout, force)

    def check(self, timeout: int = 0) -> None:
        self._only().check(timeout)

    def is_checked(self) -> bool:
        return self._only().is_checked()

    def press(self, key: str, timeout: int = 0) -> None:
        self._only().press(key, timeout)

    def set_input_files(self, path: str) -> None:
        self._only().set_input_files(path)

    def scroll_into_view_if_needed(self, timeout: int = 0) -> None:
        self._only().scroll_into_view_if_needed(timeout)

    def evaluate(self, script: str, arg: str | None = None):
        return self._only().evaluate(script, arg)

    def wait_for(self, state: str = "visible", timeout: int = 0) -> None:
        return None


class _ExpectPage:
    def __enter__(self) -> _ExpectPage:
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        return False

    @property
    def value(self):
        raise TimeoutError("no popup")


class _Context:
    def expect_page(self, timeout: int = 0) -> _ExpectPage:
        return _ExpectPage()


class ScriptedPage:
    """One job page. ``events`` records Easy Apply, next, and submit clicks."""

    def __init__(self, url: str, title: str) -> None:
        self.url = url
        self._title = title
        self.context = _Context()
        self.frames: list[ScriptedPage] = []
        self.main_frame = self
        self.events: list[str] = []
        self.submitted = False
        self.body = ScriptedNode("body")
        self.body.page = self

    def node_by_id(self, element_id: str) -> ScriptedNode | None:
        for node in self.body.walk():
            if node.attrs.get("id") == element_id:
                return node
        return None

    def refresh_gates(self) -> None:
        phone = self.node_by_id("phone")
        ready = bool(phone and phone.value.strip())
        for node in self.body.walk():
            if node.attrs.get("data-gate") != "phone":
                continue
            if ready:
                node.attrs.pop("disabled", None)
            else:
                node.attrs["disabled"] = ""

    def add(self, node: ScriptedNode) -> ScriptedNode:
        return self.body.add(node)

    def goto(self, url: str, wait_until: str = "load", timeout: int = 0) -> None:
        self.url = url

    def title(self) -> str:
        return self._title

    def inner_text(self, selector: str, timeout: int = 0) -> str:
        if selector == "body":
            return self.body.inner_text()
        matches = query(self.body, selector)
        if not matches:
            return ""
        return matches[0].inner_text()

    def locator(self, selector: str) -> ScriptedLocator:
        return ScriptedLocator(query(self.body, selector))

    def get_by_role(self, role: str, name: re.Pattern[str] | None = None) -> ScriptedLocator:
        return self.body.get_by_role(role, name=name)

    def evaluate(self, script: str, arg: str | None = None):
        return None

    def wait_for_timeout(self, timeout: int) -> None:
        return None

    def wait_for_load_state(self, state: str = "load", timeout: int = 0) -> None:
        return None

    def close(self) -> None:
        return None


def node(tag: str, text: str = "", visible: bool = True, **attrs: str) -> ScriptedNode:
    cleaned = {key.replace("_", "-"): value for key, value in attrs.items()}
    return ScriptedNode(tag, cleaned, text, visible=visible)


def field(parent: ScriptedNode, element_id: str, label: str, input_type: str = "text") -> ScriptedNode:
    wrapper = parent.add(node("div", **{"class": "field"}))
    wrapper.add(node("label", label, **{"for": element_id}))
    control = wrapper.add(
        node(
            "input",
            id=element_id,
            name=element_id,
            type=input_type,
            required="",
        )
    )
    return control


def button(parent: ScriptedNode, label: str, *, gate: bool = False, visible: bool = True, **attrs: str) -> ScriptedNode:
    extra = dict(attrs)
    if gate:
        extra["data-gate"] = "phone"
        extra["disabled"] = ""
    return parent.add(node("button", label, visible=visible, **extra))


_DESCRIPTION = (
    "We are hiring a software engineer to design, build, and run internal tools. "
    "The work covers APIs, data pipelines, and the product surfaces people use every day. "
)


def application_page(kind: str) -> ScriptedPage:
    """A job page whose form asks for the contact fields, including phone."""
    if kind == "linkedin":
        url = "https://www.linkedin.com/jobs/view/1001"
    elif kind == "greenhouse":
        url = "https://job-boards.greenhouse.io/example/jobs/1001"
    elif kind == "indeed":
        url = "https://www.indeed.com/viewjob?jk=abc123"
    else:
        raise ValueError(f"Unknown application type: {kind}")

    page = ScriptedPage(url, "Software Engineer")
    page.add(node("h1", "Software Engineer"))
    page.add(node("div", "Northwind", **{"class": "company-name"}))
    page.add(node("div", _DESCRIPTION, id="jobDescriptionText"))

    if kind == "linkedin":
        _build_linkedin(page)
    elif kind == "greenhouse":
        _build_greenhouse(page)
    else:
        _build_indeed(page)
    page.refresh_gates()
    return page


def _add_contacts(parent: ScriptedNode, *, resume: bool) -> None:
    field(parent, "first_name", "First name")
    field(parent, "last_name", "Last name")
    field(parent, "email", "Email", "email")
    field(parent, "phone_prefix", "Country code")
    field(parent, "phone", "Phone", "tel")
    if resume:
        wrapper = parent.add(node("div", **{"class": "field"}))
        wrapper.add(node("label", "Resume", **{"for": "resume"}))
        wrapper.add(node("input", id="resume", name="resume", type="file"))


def _build_indeed(page: ScriptedPage) -> None:
    page.add(node("button", "Apply now"))
    form = page.add(node("form"))
    _add_contacts(form, resume=True)
    submit = button(form, "Submit application", gate=True, aria_label="Submit application")

    def submit_application() -> None:
        page.events.append("submit")
        page.submitted = True

    submit.on_click = submit_application


def _build_greenhouse(page: ScriptedPage) -> None:
    """Phone sits on the form, outside the question block, as Greenhouse does."""
    page.add(node("button", "Apply now"))
    form = page.add(node("form"))
    _add_contacts(form, resume=True)
    questions = form.add(node("div", **{"class": "application--questions"}))
    questions.add(node("p", "Answer the questions in this section."))
    continue_button = button(form, "Continue", gate=True)
    submit = button(form, "Submit application", visible=False, aria_label="Submit application")

    def continue_application() -> None:
        page.events.append("next")
        continue_button.visible = False
        submit.visible = True

    def submit_application() -> None:
        page.events.append("submit")
        page.submitted = True

    continue_button.on_click = continue_application
    submit.on_click = submit_application


def _build_linkedin(page: ScriptedPage) -> None:
    easy = button(
        page.body,
        "Easy Apply",
        aria_label="Easy Apply",
        **{"class": "jobs-apply-button"},
    )
    modal = page.add(
        node(
            "div",
            visible=False,
            **{"class": "jobs-easy-apply-modal", "role": "dialog"},
        )
    )
    progress = modal.add(node("progress", value="1"))
    _add_contacts(modal, resume=True)
    footer = modal.add(node("footer", **{"class": "jobs-easy-apply-footer"}))
    next_button = button(
        footer,
        "Next",
        gate=True,
        aria_label="Continue to next step",
        **{"data-easy-apply-next-button": ""},
    )
    submit = button(
        footer,
        "Submit application",
        visible=False,
        aria_label="Submit application",
    )
    sent = page.add(node("p", "Your application was sent.", visible=False))

    def open_easy_apply() -> None:
        page.events.append("easy_apply")
        modal.visible = True

    def continue_application() -> None:
        page.events.append("next")
        progress.attrs["value"] = "2"
        next_button.visible = False
        submit.visible = True

    def submit_application() -> None:
        page.events.append("submit")
        page.submitted = True
        modal.visible = False
        sent.visible = True

    easy.on_click = open_easy_apply
    next_button.on_click = continue_application
    submit.on_click = submit_application


def _accessible_name(node: ScriptedNode) -> str:
    return " ".join(
        part
        for part in (node.attrs.get("aria-label", ""), node.attrs.get("value", ""), node.inner_text())
        if part
    )


def query(root: ScriptedNode, selector: str) -> list[ScriptedNode]:
    found: list[ScriptedNode] = []
    seen: set[int] = set()
    for part in _split_list(selector):
        for match in _query_one(root, part):
            if id(match) in seen:
                continue
            seen.add(id(match))
            found.append(match)
    return found


def _query_one(root: ScriptedNode, selector: str) -> list[ScriptedNode]:
    if selector.startswith("xpath="):
        return _query_xpath(root, selector[len("xpath=") :])
    steps = _split_compound(selector)
    if not steps:
        return []
    current = [root]
    for step in steps:
        nxt: list[ScriptedNode] = []
        seen: set[int] = set()
        for node in current:
            for desc in _descendants(node):
                if id(desc) in seen or not _matches(desc, step):
                    continue
                seen.add(id(desc))
                nxt.append(desc)
        current = nxt
    return current


def _descendants(node: ScriptedNode):
    for child in node.children:
        yield child
        yield from _descendants(child)


def _query_xpath(node: ScriptedNode, expr: str) -> list[ScriptedNode]:
    expr = expr.strip()
    if expr.startswith("ancestor::form"):
        found = _closest(node, "form")
        return [found] if found is not None else []
    if expr.startswith("ancestor::label"):
        found = _closest(node, "label")
        return [found] if found is not None else []
    if "fieldset" in expr and "legend" in expr:
        fieldset = _closest(node, "fieldset")
        if fieldset is None:
            return []
        return [child for child in fieldset.children if child.tag == "legend"]
    if "dialog" in expr:
        current: ScriptedNode | None = node
        while current is not None:
            if current.tag == "dialog" or current.attrs.get("role") == "dialog":
                return [current]
            current = current.parent
        return []
    return []


def _closest(node: ScriptedNode, tag: str) -> ScriptedNode | None:
    current = node.parent
    while current is not None:
        if current.tag == tag:
            return current
        current = current.parent
    return None


def _matches(node: ScriptedNode, compound: str) -> bool:
    rest = compound.strip()
    saw = False
    while rest:
        if rest.startswith(":not("):
            inner, rest = _take_group(rest[4:])
            if _matches(node, inner):
                return False
            saw = True
            continue
        if rest.startswith(":has-text("):
            inner, rest = _take_group(rest[9:])
            needle = _unquote(inner)
            haystack = f"{node.attrs.get('aria-label', '')} {node.inner_text()}"
            if needle not in haystack:
                return False
            saw = True
            continue
        if rest[0] == ".":
            name, rest = _take_ident(rest[1:])
            if name not in node.attrs.get("class", "").split():
                return False
            saw = True
            continue
        if rest[0] == "#":
            name, rest = _take_ident(rest[1:])
            if node.attrs.get("id") != name:
                return False
            saw = True
            continue
        if rest[0] == "[":
            body, rest = _take_brackets(rest)
            if not _attr_matches(node, body):
                return False
            saw = True
            continue
        if rest[0] == "*" or rest[0].isalnum():
            if rest[0] == "*":
                tag, rest = "*", rest[1:]
            else:
                tag, rest = _take_ident(rest)
            if tag != "*" and node.tag.lower() != tag.lower():
                return False
            saw = True
            continue
        return False
    return saw


def _attr_matches(node: ScriptedNode, body: str) -> bool:
    match = re.match(r"([\w:-]+)(\*=|\^=|=)?(.*)", body.strip())
    if match is None:
        return False
    name, operator, raw = match.group(1), match.group(2), match.group(3)
    if not operator:
        return name in node.attrs
    value = _unquote(raw.strip())
    current = node.attrs.get(name)
    if current is None:
        return False
    if operator == "=":
        return current == value
    if operator == "*=":
        return value in current
    if operator == "^=":
        return current.startswith(value)
    return False


def _take_ident(text: str) -> tuple[str, str]:
    index = 0
    while index < len(text) and (text[index].isalnum() or text[index] in "-_"):
        index += 1
    return text[:index], text[index:]


def _take_group(text: str) -> tuple[str, str]:
    if not text.startswith("("):
        raise ValueError(text)
    depth = 0
    quote = ""
    for index, char in enumerate(text):
        if quote:
            if char == quote:
                quote = ""
            continue
        if char in "'\"":
            quote = char
            continue
        if char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
            if depth == 0:
                return text[1:index], text[index + 1 :]
    raise ValueError(text)


def _take_brackets(text: str) -> tuple[str, str]:
    depth = 0
    quote = ""
    for index, char in enumerate(text):
        if quote:
            if char == quote:
                quote = ""
            continue
        if char in "'\"":
            quote = char
            continue
        if char == "[":
            depth += 1
        elif char == "]":
            depth -= 1
            if depth == 0:
                return text[1:index], text[index + 1 :]
    raise ValueError(text)


def _unquote(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
        return value[1:-1]
    return value


def _split_list(selector: str) -> list[str]:
    return [part for part in _split(selector, ",") if part]


def _split_compound(selector: str) -> list[str]:
    return [part for part in _split(selector, " ") if part]


def _split(selector: str, separator: str) -> list[str]:
    parts: list[str] = []
    buf: list[str] = []
    depth = 0
    quote = ""
    for char in selector:
        if quote:
            buf.append(char)
            if char == quote:
                quote = ""
            continue
        if char in "'\"":
            quote = char
            buf.append(char)
            continue
        if char in "([":
            depth += 1
        elif char in ")]":
            depth = max(0, depth - 1)
        if depth == 0 and char == separator:
            parts.append("".join(buf).strip())
            buf = []
            continue
        buf.append(char)
    parts.append("".join(buf).strip())
    return parts
