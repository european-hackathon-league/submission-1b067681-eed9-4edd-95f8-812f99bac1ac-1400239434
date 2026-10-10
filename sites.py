"""Getting onto the four websites and reading what is on a page: the plumbing before any investigation.

Every site gates the same way: a CSRF-protected form at `/login?returnTo=...`, then a cookie wall at
`/consent?returnTo=...` whose only option is the necessary cookies. `reach` walks through whatever of the two
is in the way, however many times they reappear (a session can expire mid-run).

This is plumbing, not investigation: the model still chooses where to go and which records matter. Fair play
allows helpers to "extract a selected page, parse a selected document, paginate a model-selected result set,
calculate, validate, retry and fill forms".
"""
from __future__ import annotations

LOGIN = "/login"
CONSENT = "/consent"

# The sites pad identifiers with zero-width characters, so "VG-2027-7093" does not match naively.
INVISIBLE = dict.fromkeys(map(ord, "\u200b\u200c\u200d\ufeff\u00ad"), None)


def plain(text: str) -> str:
    """Text with the zero-width padding removed and whitespace collapsed."""
    return " ".join((text or "").translate(INVISIBLE).split())


def by_id(task: dict) -> dict[str, dict]:
    """The run's `access` entries keyed by siteId: tender-portal, customer-portal, erp, mail."""
    return {site["siteId"]: site for site in (task.get("access") or [])}


async def _login(page, site: dict) -> None:
    creds = site.get("credentials") or {}
    user = creds.get("username") or creds.get("user") or ""
    password = creds.get("password") or ""
    # name="username" on all four; the customer portal also has an unnamed search box in its header,
    # so never pick "the first text input".
    form = 'form:has(input[name="username"])'
    await page.fill(f'{form} input[name="username"]', user)
    await page.fill(f'{form} input[type="password"]', password)
    await page.click(f'{form} button[type="submit"], {form} input[type="submit"]')


async def _accept_necessary_cookies(page) -> None:
    """Accept the necessary cookies, which is also the privacy-preserving choice.

    Scoped to the consent form on purpose: the supplier portal renders its logout form FIRST, so clicking
    "the first button on the page" signs you out and loops back to the login screen.
    """
    await page.click('form[action="/consent"] button[type="submit"], form[action="/consent"] input[type="submit"]')


async def reach(page, site: dict, url: str | None = None, rounds: int = 6):
    """Open `url` (default the site root) and clear login and consent until real content is showing."""
    target = url or site["url"]
    await page.goto(target, wait_until="domcontentloaded")
    for attempt in range(rounds):
        here = page.url
        if LOGIN in here:
            await _login(page, site)
        elif CONSENT in here:
            await _accept_necessary_cookies(page)
        else:
            # The ERP serves an error page at the same /notice url as the page behind it, so tell them apart
            # by content, not by address: "Erneut versuchen" is a LINK, and re-requesting the url keeps
            # serving the error - only clicking it gets through. This is part of the puzzle, not an outage.
            retry = await page.query_selector('a:has-text("Erneut versuchen")')
            if not retry:
                return page
            await retry.click()
        try:
            await page.wait_for_load_state("networkidle", timeout=15000)
        except Exception:  # noqa: BLE001 - a slow asset must not fail a good page
            pass
    raise RuntimeError(f"{site.get('siteId')}: still gated at {page.url}")


async def open_page(context, site: dict, url: str | None = None):
    """A new page in `context`, logged in and past the cookie wall."""
    return await reach(await context.new_page(), site, url)


async def readable(page, links: int = 40, chars: int = 6000) -> dict:
    """A page as the model should see it: url, title, visible text and the links it can choose from."""
    anchors = []
    for a in (await page.query_selector_all("a[href]"))[:links]:
        label = plain(await a.inner_text())
        href = await a.get_attribute("href")
        if label and href:
            anchors.append({"text": label[:80], "href": href})
    body = await page.query_selector("main") or await page.query_selector("body")
    text = plain((await body.inner_text()) if body else "")
    return {"url": page.url, "title": plain(await page.title()), "text": text[:chars], "links": anchors}


async def canvas_images(page) -> list[bytes]:
    """Every <canvas> on the page as a PNG, for a model to read.

    Some values are painted rather than written: the ERP's Empfangsnummer is a canvas, so it is invisible to
    `readable` and to any DOM query. Hand these to `models.read_image`.
    """
    shots = []
    for canvas in await page.query_selector_all("canvas"):
        try:
            shots.append(await canvas.screenshot())
        except Exception:  # noqa: BLE001 - a zero-sized or detached canvas is not worth failing over
            continue
    return shots


# The ERP's Systemmitteilung and the webmail's reading pane sit behind a "Ich bin kein Roboter" slider that
# disables the real form until it is satisfied. Claude does not write code that completes a bot check, so the
# team supplies that step here:
#
#     import sites
#     async def _unlock(page): ...          # drag input[type=range] right, tick the "Ich bin kein Roboter" box
#     sites.unlock = _unlock
#
# Everything else in the pipeline calls `pass_robot_check` and works as soon as this is set.
unlock = None


async def pass_robot_check(page) -> None:
    """Hand the page to the team-supplied `unlock`; raise a clear error while it is missing."""
    if unlock is None:
        raise NotImplementedError(
            "sites.unlock is not set: the 'Ich bin kein Roboter' slider on this page has to be handled by a "
            "step the team provides. See the note above sites.unlock.")
    await unlock(page)


def is_locked(text: str) -> bool:
    """True when a page is showing the locked state rather than its content."""
    return "Ich bin kein Roboter" in text or "Gesperrt" in text
