import os
import re
import csv
import json
from dataclasses import dataclass, asdict
from typing import List, Dict, Any, Optional

from dotenv import load_dotenv
from openai import OpenAI
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError


load_dotenv()


@dataclass
class FieldDraft:
    url: str
    field_name: str
    label: str
    field_type: str
    required: bool
    generated_answer: str
    filled: bool
    error: str = ""


class SafeJobApplyAssistant:
    """
    Semi-automated job application helper.

    Opens each URL, tries to start the application, drafts answers from the
    candidate profile, fills visible fields, and waits for a typed "submit"
    before clicking a submit button.
    """

    APPLY_BUTTON_SELECTORS = [
        "button:has-text('Apply')",
        "button:has-text('Easy Apply')",
        "a:has-text('Apply')",
        "a:has-text('Easy Apply')",
        "[role='button']:has-text('Apply')",
        "[aria-label*='Apply' i]",
    ]

    NEXT_OR_CONTINUE_SELECTORS = [
        "button:has-text('Continue')",
        "button:has-text('Next')",
        "button:has-text('Review')",
        "[role='button']:has-text('Continue')",
        "[role='button']:has-text('Next')",
    ]

    SUBMIT_SELECTORS = [
        "button:has-text('Submit')",
        "button:has-text('Submit application')",
        "[role='button']:has-text('Submit')",
    ]

    INPUT_SELECTORS = "input:not([type='hidden']):not([type='file']), textarea, select"
    FILE_INPUT_SELECTOR = "input[type='file']"
    CV_CANDIDATES = [
        "assets/cvs/ANA_CV.pdf",
        "assets/cvs/ANA_CV_VA.pdf",
        "assets/cvs/Ana_Fandino_CV.pdf",
    ]

    def __init__(self, profile_context: str, model: str = "gpt-4.1-mini", headless: bool = False, cv_path: Optional[str] = None):
        api_key = os.getenv("OPENAI_API_KEY")
        if not api_key:
            raise ValueError("OPENAI_API_KEY is not set in the environment.")
        self.client = OpenAI(api_key=api_key)
        self.model = model
        self.profile_context = profile_context
        self.headless = headless
        self.logs: List[FieldDraft] = []
        self.cv_path = self._resolve_cv_path(cv_path)

    def _resolve_cv_path(self, preferred_cv_path: Optional[str]) -> Optional[str]:
        candidates = []
        if preferred_cv_path:
            candidates.append(preferred_cv_path)
        candidates.extend(self.CV_CANDIDATES)
        for candidate in candidates:
            if candidate and os.path.exists(candidate):
                return candidate
        return None

    def generate_answer(self, question: str) -> str:
        prompt = (
            "You are helping draft professional, truthful job application answers.\n"
            "Return only a concise answer text suitable for direct form input.\n\n"
            f"Candidate profile context:\n{self.profile_context}\n\n"
            f"Question/Field label:\n{question}\n"
        )
        try:
            response = self.client.responses.create(
                model=self.model,
                input=prompt,
            )
            return (response.output_text or "").strip()
        except Exception as e:
            return f"Draft unavailable ({str(e)})"

    @staticmethod
    def _clean_text(value: str) -> str:
        value = value or ""
        value = re.sub(r"\s+", " ", value).strip()
        return value

    def _find_label_for_input(self, page, handle) -> str:
        try:
            input_id = handle.get_attribute("id")
            if input_id:
                label = page.locator(f"label[for='{input_id}']").first
                if label.count() > 0:
                    text = self._clean_text(label.inner_text(timeout=300))
                    if text:
                        return text
        except Exception:
            pass

        try:
            parent_label = handle.locator("xpath=ancestor::label[1]").first
            if parent_label.count() > 0:
                text = self._clean_text(parent_label.inner_text(timeout=300))
                if text:
                    return text
        except Exception:
            pass

        for attr in ["aria-label", "placeholder", "name"]:
            try:
                v = handle.get_attribute(attr)
                if v and self._clean_text(v):
                    return self._clean_text(v)
            except Exception:
                continue

        return "Unlabeled field"

    def _field_required(self, handle) -> bool:
        try:
            if handle.get_attribute("required") is not None:
                return True
            aria_required = handle.get_attribute("aria-required")
            return str(aria_required).lower() == "true"
        except Exception:
            return False

    def _field_type(self, handle) -> str:
        try:
            tag = (handle.evaluate("el => el.tagName") or "").lower()
            if tag == "textarea":
                return "textarea"
            if tag == "select":
                return "select"
            input_type = handle.get_attribute("type") or "text"
            return f"input:{input_type}"
        except Exception:
            return "unknown"

    def _extract_fields(self, page) -> List[Dict[str, Any]]:
        fields = []
        loc = page.locator(self.INPUT_SELECTORS)
        count = loc.count()
        for i in range(count):
            handle = loc.nth(i)
            try:
                if not handle.is_visible():
                    continue
                label = self._find_label_for_input(page, handle)
                if not label:
                    continue
                field_name = handle.get_attribute("name") or handle.get_attribute("id") or f"field_{i}"
                fields.append(
                    {
                        "handle": handle,
                        "label": label,
                        "name": field_name,
                        "required": self._field_required(handle),
                        "field_type": self._field_type(handle),
                    }
                )
            except Exception:
                continue
        return fields

    def _fill_field(self, handle, value: str, field_type: str) -> bool:
        value = self._clean_text(value)
        if not value:
            return False
        try:
            if field_type.startswith("input:radio") or field_type.startswith("input:checkbox"):
                handle.check(timeout=1000)
                return True
            if field_type == "select":
                opts = handle.locator("option")
                if opts.count() > 1:
                    choice = opts.nth(1).get_attribute("value")
                    if choice:
                        handle.select_option(choice)
                        return True
                return False
            handle.fill(value, timeout=1500)
            return True
        except Exception:
            return False

    def _try_click_any(self, page, selectors: List[str], timeout_ms: int = 1500) -> bool:
        for sel in selectors:
            try:
                el = page.locator(sel).first
                if el.count() > 0 and el.is_visible():
                    el.click(timeout=timeout_ms)
                    return True
            except Exception:
                continue
        return False

    def _upload_cv_if_requested(self, page, url: str) -> bool:
        file_inputs = page.locator(self.FILE_INPUT_SELECTOR)
        if file_inputs.count() == 0:
            return False
        if not self.cv_path:
            print("File input detected but CV path not found.")
            self.logs.append(
                FieldDraft(
                    url=url,
                    field_name="file_upload",
                    label="CV Upload",
                    field_type="file",
                    required=True,
                    generated_answer="",
                    filled=False,
                    error="CV file not found. Place a PDF in assets/cvs/ or pass --cv.",
                )
            )
            return False
        uploaded_any = False
        for i in range(file_inputs.count()):
            inp = file_inputs.nth(i)
            try:
                if inp.is_visible():
                    inp.set_input_files(self.cv_path)
                    uploaded_any = True
            except Exception as e:
                self.logs.append(
                    FieldDraft(
                        url=url,
                        field_name=f"file_upload_{i}",
                        label="CV Upload",
                        field_type="file",
                        required=True,
                        generated_answer=self.cv_path,
                        filled=False,
                        error=str(e),
                    )
                )
        if uploaded_any:
            print(f"Uploaded CV: {self.cv_path}")
            self.logs.append(
                FieldDraft(
                    url=url,
                    field_name="file_upload",
                    label="CV Upload",
                    field_type="file",
                    required=True,
                    generated_answer=self.cv_path,
                    filled=True,
                    error="",
                )
            )
        return uploaded_any

    def process_url(self, page, url: str):
        print(f"\nOpening: {url}")
        page.goto(url, wait_until="domcontentloaded", timeout=45000)
        page.wait_for_timeout(1500)

        clicked_apply = self._try_click_any(page, self.APPLY_BUTTON_SELECTORS)
        if clicked_apply:
            print("Apply button clicked.")
            page.wait_for_timeout(1500)
        else:
            print("No apply button detected; attempting to parse current page form.")

        safety_cycles = 0
        while safety_cycles < 8:
            safety_cycles += 1
            self._upload_cv_if_requested(page, url)
            fields = self._extract_fields(page)
            if not fields:
                moved = self._try_click_any(page, self.NEXT_OR_CONTINUE_SELECTORS)
                if moved:
                    page.wait_for_timeout(1200)
                    continue
                break

            for field in fields:
                question = field["label"]
                draft = self.generate_answer(question)
                filled = self._fill_field(field["handle"], draft, field["field_type"])
                log = FieldDraft(
                    url=url,
                    field_name=field["name"],
                    label=question,
                    field_type=field["field_type"],
                    required=field["required"],
                    generated_answer=draft,
                    filled=filled,
                    error="" if filled else "Could not fill field",
                )
                self.logs.append(log)
                print(f"- {question} -> {'filled' if filled else 'not filled'}")

            moved = self._try_click_any(page, self.NEXT_OR_CONTINUE_SELECTORS)
            if moved:
                print("Moved to next step.")
                page.wait_for_timeout(1500)
                continue
            break

        print("Manual review required.")
        decision = input("Type 'submit' to click submit button, or press Enter to skip this URL: ").strip().lower()
        if decision == "submit":
            submitted = self._try_click_any(page, self.SUBMIT_SELECTORS, timeout_ms=2500)
            print("Submitted." if submitted else "Submit button not found/clickable.")
        else:
            print("Skipped submission for this URL.")

    def run(self, urls: List[str]):
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=self.headless)
            context = browser.new_context()
            page = context.new_page()
            for url in urls:
                try:
                    self.process_url(page, url)
                except PlaywrightTimeoutError:
                    print(f"Timeout processing URL: {url}")
                except Exception as e:
                    print(f"Error processing URL {url}: {e}")
            browser.close()

    def save_logs(self, json_path: str = "logs/application_drafts_log.json", csv_path: str = "logs/application_drafts_log.csv"):
        for path in (json_path, csv_path):
            directory = os.path.dirname(path)
            if directory:
                os.makedirs(directory, exist_ok=True)

        with open(json_path, "w", encoding="utf-8") as f:
            json.dump([asdict(x) for x in self.logs], f, ensure_ascii=False, indent=2)

        with open(csv_path, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(
                f,
                fieldnames=["url", "field_name", "label", "field_type", "required", "generated_answer", "filled", "error"],
            )
            writer.writeheader()
            for row in self.logs:
                writer.writerow(asdict(row))

        print(f"Logs saved:\n- {json_path}\n- {csv_path}")
