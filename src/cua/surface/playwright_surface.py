"""
Playwright (Chromium) implementation of the Surface contract.

Perception is accessibility-tree-first: observe() returns Playwright's
AI-mode aria snapshot, in which every node carries a short ephemeral ref,
plus a screenshot. Resolution of recorded TargetDescriptors uses only public,
role-based Playwright locators; refs never leak into artifacts.

The browser runs headed when `headless=False`, which is what the human
handoff relies on: the operator takes over the very same window this object
is driving, and automation simply stops issuing commands until resumed.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any, cast

from playwright.sync_api import Browser, BrowserContext, Dialog, Page, Playwright, sync_playwright
from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import Locator as PWLocator
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

from cua.artifact.targets import BBox, Locator, RecordedElement, TargetDescriptor
from cua.escalation.session import SessionControl

from .base import ActionFailed, Handle, Observation, ReadAttribute, Surface, TargetNotFound, UnknownRef
from .snapshot import parse_refs

log = logging.getLogger(__name__)

DEFAULT_VIEWPORT = {"width": 1280, "height": 900}

# ancestor_role -> XPath predicate. Implicit HTML roles are mapped here so the
# artifact can stay in role vocabulary while this adapter deals with markup.
_ANCESTOR_PREDICATES: dict[str, str] = {
    "row": "self::tr or @role='row'",
    "table": "self::table or @role='table' or @role='grid'",
    "cell": "self::td or self::th or @role='cell' or @role='gridcell'",
    "rowgroup": "self::tbody or self::thead or self::tfoot or @role='rowgroup'",
    "list": "self::ul or self::ol or @role='list'",
    "listitem": "self::li or @role='listitem'",
    "form": "self::form or @role='form'",
    "group": "self::fieldset or @role='group'",
}

_DESCRIBE_JS = """
el => {
  const tag = el.tagName.toLowerCase();
  const cssPath = (e) => {
    const parts = [];
    while (e && e.nodeType === 1 && e.tagName.toLowerCase() !== 'html') {
      let s = e.tagName.toLowerCase();
      if (e.id) { parts.unshift(s + '#' + CSS.escape(e.id)); break; }
      const nm = e.getAttribute('name');
      if (nm) { parts.unshift(s + '[name="' + nm.replace(/"/g, '\\\\"') + '"]'); break; }
      const p = e.parentElement;
      if (p) {
        const sib = Array.from(p.children).filter(c => c.tagName === e.tagName);
        if (sib.length > 1) s += ':nth-of-type(' + (sib.indexOf(e) + 1) + ')';
      }
      parts.unshift(s);
      e = p;
    }
    return parts.join(' > ');
  };
  let row_cells = null, cell_index = null;
  if (tag === 'td' || tag === 'th' || el.getAttribute('role') === 'cell') {
    const tr = el.closest('tr');
    if (tr) {
      const cells = Array.from(tr.children).filter(c => c.tagName === 'TD' || c.tagName === 'TH');
      row_cells = cells.map(c => (c.innerText || '').trim());
      cell_index = cells.indexOf(el);
    }
  }
  const isField = tag === 'input' || tag === 'select' || tag === 'textarea';
  const text = isField ? (el.type === 'submit' || el.type === 'button' ? el.value : '') : (el.innerText || '');
  return { tag, text: text.trim().slice(0, 200), css: cssPath(el), row_cells, cell_index };
}
"""


_INIT_JS = """
(() => {
  if (window.__cuaInstalled) return;
  window.__cuaInstalled = true;
  const implicitRole = (el) => {
    const tag = el.tagName.toLowerCase();
    if (tag === 'a') return 'link';
    if (tag === 'button') return 'button';
    if (tag === 'select') return 'combobox';
    if (tag === 'textarea') return 'textbox';
    if (tag === 'input') return ({submit:'button', button:'button', reset:'button', checkbox:'checkbox', radio:'radio'})[el.type] || 'textbox';
    return el.getAttribute('role') || tag;
  };
  const describe = (el) => {
    if (!el || el.nodeType !== 1) return {tag: 'document'};
    const tag = el.tagName.toLowerCase();
    const isBtn = tag === 'input' && (el.type === 'submit' || el.type === 'button');
    const name = el.getAttribute('aria-label') || el.getAttribute('title') || (isBtn ? el.value : '')
      || (tag === 'select' || tag === 'input' || tag === 'textarea' ? '' : (el.innerText || '').trim().slice(0, 80))
      || el.getAttribute('name') || '';
    return {tag, role: implicitRole(el), name, field: el.getAttribute('name') || undefined};
  };
  const send = (kind, el, extra) => {
    try { window.__cuaHumanAction(Object.assign({kind, url: location.href}, describe(el), extra || {})); } catch (e) {}
  };
  document.addEventListener('click', (e) => {
    const el = e.target && e.target.closest ? (e.target.closest('a,button,input,select,label,[role]') || e.target) : e.target;
    send('click', el);
  }, true);
  document.addEventListener('change', (e) => {
    const el = e.target;
    if (el.tagName === 'SELECT') {
      const opt = el.options[el.selectedIndex];
      send('change', el, {option: opt ? opt.label : ''});
    } else {
      send('change', el, {value_length: (el.value || '').length});
    }
  }, true);
  document.addEventListener('submit', (e) => send('submit', e.target), true);
  document.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' || e.key === 'Escape') send('key', e.target, {key: e.key});
  }, true);
  const renderBanner = async () => {
    let text = '';
    try { text = await window.__cuaBannerText(); } catch (e) { return; }
    window.__cuaSetBanner(text);
  };
  window.__cuaSetBanner = (text) => {
    let el = document.getElementById('__cua_banner');
    if (!text) { if (el) el.remove(); return; }
    if (!el) {
      el = document.createElement('div');
      el.id = '__cua_banner';
      el.style.cssText = 'position:fixed;top:0;left:0;right:0;z-index:2147483647;background:#b30000;color:#fff;'
        + 'font:bold 14px/1.4 Verdana,Arial,sans-serif;padding:8px 14px;box-shadow:0 2px 6px rgba(0,0,0,.4)';
      (document.body || document.documentElement).appendChild(el);
    }
    el.textContent = text;
  };
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', renderBanner);
  else renderBanner();
})();
"""


class PlaywrightSurface(Surface):
    def __init__(
        self,
        *,
        headless: bool = True,
        viewport: dict[str, int] | None = None,
        default_timeout_ms: int = 10_000,
        slow_mo_ms: int = 0,
        control: SessionControl | None = None,
    ) -> None:
        self.control = control or SessionControl()
        self.headless = headless
        self.viewport = viewport or dict(DEFAULT_VIEWPORT)
        self.default_timeout_ms = default_timeout_ms
        self.slow_mo_ms = slow_mo_ms
        self._pw: Playwright | None = None
        self._browser: Browser | None = None
        self._context: BrowserContext | None = None
        self._page: Page | None = None
        self._last_elements: dict[str, Any] = {}
        self._dialogs: list[str] = []
        self._recording = False
        self._human_actions: list[dict[str, Any]] = []
        self._banner_text: str | None = None

    # ---------------------------------------------------------------- session
    def open(self) -> None:
        self._pw = sync_playwright().start()
        self._browser = self._pw.chromium.launch(headless=self.headless, slow_mo=self.slow_mo_ms or None)
        self._context = self._browser.new_context(viewport=cast(Any, self.viewport))
        self._context.set_default_timeout(self.default_timeout_ms)
        # Installed before the first page so every document (including after navigation) gets them.
        self._context.expose_binding("__cuaHumanAction", self._on_human_action)
        self._context.expose_binding("__cuaBannerText", lambda _source: self._banner_text or "")
        self._context.add_init_script(_INIT_JS)
        self._page = self._context.new_page()
        self._page.on("dialog", self._on_dialog)
        self._page.on("framenavigated", self._on_navigated)

    def close(self) -> None:
        if self._browser is not None:
            self._browser.close()
        if self._pw is not None:
            self._pw.stop()
        self._pw = self._browser = self._context = self._page = None

    @property
    def page(self) -> Page:
        if self._page is None:
            raise RuntimeError("surface is not open")
        return self._page

    def url(self) -> str:
        return self.page.url

    def title(self) -> str:
        return self.page.title()

    def _on_dialog(self, dialog: Dialog) -> None:
        self._dialogs.append(f"{dialog.type}: {dialog.message}")
        dialog.dismiss()

    def _on_human_action(self, _source: Any, payload: Any) -> None:
        if self._recording and isinstance(payload, dict):
            self._human_actions.append({k: v for k, v in payload.items() if v not in (None, "")})

    def _on_navigated(self, frame: Any) -> None:
        if self._recording and self._page is not None and frame == self._page.main_frame:
            self._human_actions.append({"kind": "navigated", "url": frame.url})

    # ------------------------------------------------------------- perception
    def observe(self) -> Observation:
        page = self.page
        snapshot = page.locator("body").aria_snapshot(mode="ai")
        elements = parse_refs(snapshot)
        self._last_elements = dict(elements)
        dialogs, self._dialogs = self._dialogs, []
        return Observation(
            url=page.url,
            title=page.title(),
            snapshot=snapshot,
            elements=elements,
            screenshot_png=page.screenshot(type="png"),
            dialogs=dialogs,
        )

    def screenshot(self) -> bytes:
        return self.page.screenshot(type="png")

    def exists(self, locator: Locator, timeout_ms: int = 0) -> bool:
        if locator.by == "bbox":
            return False
        pl = self._build(locator, self.page)
        if timeout_ms > 0:
            try:
                pl.first.wait_for(state="visible", timeout=timeout_ms)
                return True
            except PlaywrightTimeoutError:
                return False
        try:
            return pl.count() > 0 and pl.first.is_visible()
        except PlaywrightError:  # invalid selector, detached frame, ...
            return False

    def describe(self, handle: Handle) -> RecordedElement:
        pl = self._native(handle)
        with self._action(f"describe {handle.candidate.describe()}"):
            info = cast(dict[str, Any], pl.evaluate(_DESCRIBE_JS))
            box = pl.bounding_box()
        role = handle.candidate.role
        name = handle.candidate.name
        if handle.ref is not None and handle.ref in self._last_elements:
            el = self._last_elements[handle.ref]
            role, name = el.role, el.name
        return RecordedElement(
            role=role,
            name=name,
            tag=info.get("tag"),
            text=info.get("text") or None,
            css=info.get("css"),
            bbox=BBox(**box) if box else None,
            row_cells=info.get("row_cells"),
            cell_index=info.get("cell_index"),
        )

    # ------------------------------------------------------------- resolution
    def handle_for_ref(self, ref: str) -> Handle:
        if ref not in self._last_elements:
            raise UnknownRef(ref)
        el = self._last_elements[ref]
        pl = self.page.locator(f"aria-ref={ref}")
        candidate = Locator(by="role", role=el.role, name=el.name, exact=True)
        return Handle(native=pl, candidate=candidate, candidate_index=0, ref=ref)

    def resolve(self, target: TargetDescriptor, timeout_ms: int | None = None) -> Handle:
        """
        Poll the candidate chain until one resolves to exactly one visible
        element. Polling the whole chain (rather than waiting the full timeout
        per candidate) means a still-loading page and a broken primary locator
        are both handled without multiplying the wait. bbox candidates are
        skipped while polling and used only after everything else has failed.
        """
        deadline = time.monotonic() + (timeout_ms if timeout_ms is not None else self.default_timeout_ms) / 1000
        attempts: dict[int, str] = {}
        while True:
            for i, cand in enumerate(target.candidates):
                if cand.by == "bbox":
                    continue
                pl = self._build(cand, self.page)
                try:
                    n = pl.count()
                except PlaywrightError as ex:  # invalid selector, detached frame, ...
                    attempts[i] = f"[{cand.describe()}] error: {type(ex).__name__}"
                    continue
                if n == 1 and pl.first.is_visible():
                    return Handle(native=pl.first, candidate=cand, candidate_index=i)
                attempts[i] = f"[{cand.describe()}] matched {n}" + ("" if n != 1 else " (not visible)")
            if time.monotonic() >= deadline:
                break
            time.sleep(0.25)
        for i, cand in enumerate(target.candidates):
            if cand.by == "bbox" and cand.bbox is not None:
                return Handle(native=None, candidate=cand, candidate_index=i, bbox=cand.bbox)
        raise TargetNotFound(target, [attempts[i] for i in sorted(attempts)])

    def _build(self, loc: Locator, root: Page | PWLocator) -> PWLocator:
        scope: Page | PWLocator = self._build(loc.within, root) if loc.within is not None else root
        pl: PWLocator
        if loc.by == "role":
            role = cast(Any, loc.role)
            pl = scope.get_by_role(role, name=loc.name, exact=loc.exact) if loc.name is not None else scope.get_by_role(role)
        elif loc.by == "text":
            pl = scope.get_by_text(cast(str, loc.text), exact=loc.exact)
        elif loc.by == "label":
            pl = scope.get_by_label(cast(str, loc.text), exact=loc.exact)
        elif loc.by == "css":
            pl = scope.locator(cast(str, loc.selector))
        elif loc.by == "xpath":
            pl = scope.locator(f"xpath={loc.selector}")
        else:
            raise ValueError(f"cannot build a locator for by={loc.by!r}")
        if loc.ancestor_role:
            predicate = _ANCESTOR_PREDICATES.get(loc.ancestor_role, f"@role='{loc.ancestor_role}'")
            pl = pl.locator(f"xpath=ancestor::*[{predicate}][1]")
        if loc.nth is not None:
            pl = pl.nth(loc.nth)
        return pl

    def _native(self, handle: Handle) -> PWLocator:
        if handle.native is None:
            raise ActionFailed("handle has no native locator (bbox-only handles support click only)")
        return cast(PWLocator, handle.native)

    @contextmanager
    def _action(self, description: str) -> Iterator[None]:
        """Refuse to act while a human holds the session; translate driver exceptions into ActionFailed."""
        self.control.assert_automation(description)
        try:
            yield
        except PlaywrightError as ex:
            first_line = str(ex).splitlines()[0][:200] if str(ex) else type(ex).__name__
            raise ActionFailed(f"{description}: {first_line}") from ex

    # ----------------------------------------------------------------- action
    def navigate(self, url: str) -> None:
        with self._action(f"navigate {url}"):
            self.page.goto(url, wait_until="domcontentloaded")

    def click(self, handle: Handle) -> None:
        with self._action(f"click {handle.candidate.describe()}"):
            if handle.native is None and handle.bbox is not None:
                cx, cy = handle.bbox.center
                self.page.mouse.click(cx, cy)
                # Unlike Locator.click(), a coordinate click does not auto-wait for a
                # navigation it triggers; give one a moment to start before settling.
                self.page.wait_for_timeout(250)
            else:
                self._native(handle).click()
        self.settle()

    def type_text(self, handle: Handle, text: str) -> None:
        with self._action(f"type into {handle.candidate.describe()}"):
            self._native(handle).fill(text)

    def select_option(self, handle: Handle, label: str) -> None:
        with self._action(f"select {label!r} in {handle.candidate.describe()}"):
            self._native(handle).select_option(label=label)

    def press(self, key: str) -> None:
        with self._action(f"press {key}"):
            self.page.keyboard.press(key)
        self.settle()

    def read(self, handle: Handle, attribute: ReadAttribute = "text") -> str:
        with self._action(f"read {handle.candidate.describe()}"):
            pl = self._native(handle)
            if attribute == "value":
                return pl.input_value()
            tag = cast(str, pl.evaluate("el => el.tagName.toLowerCase()"))
            if tag in ("input", "select", "textarea"):
                return pl.input_value()
            return pl.inner_text().strip()

    def settle(self, timeout_ms: int | None = None) -> None:
        try:
            self.page.wait_for_load_state("load", timeout=timeout_ms or self.default_timeout_ms)
        except PlaywrightTimeoutError:
            # Not an error by itself: a page that never reaches "load" surfaces through
            # the next observe()/resolve(), which is where the replay engine classifies it.
            log.debug("settle: page did not reach 'load' within %sms", timeout_ms or self.default_timeout_ms)

    def wait_idle(self, ms: int) -> None:
        self.page.wait_for_timeout(ms)

    # ---------------------------------------------------------------- handoff
    def start_human_recording(self) -> None:
        self._human_actions = []
        self._recording = True

    def stop_human_recording(self) -> list[dict[str, Any]]:
        self._recording = False
        actions, self._human_actions = self._human_actions, []
        return actions

    def set_banner(self, text: str | None) -> None:
        self._banner_text = text
        try:
            self.page.evaluate("t => window.__cuaSetBanner && window.__cuaSetBanner(t)", text or "")
        except PlaywrightError as ex:
            raise ActionFailed(f"banner: {str(ex).splitlines()[0][:120]}") from ex
