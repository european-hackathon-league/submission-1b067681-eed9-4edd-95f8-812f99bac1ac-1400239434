# How the four websites behave

Mechanics only — no values from the public dataset, since the private one differs. Written from what the
pages actually do, verified against them. `sites.py` implements the access parts.

## Shared by all four

| Behaviour | What it means for extraction |
|---|---|
| `/login?returnTo=…`, CSRF form, `input[name="username"]` | The supplier portal also has an unnamed search box in its header — never pick "the first text input". |
| `/consent?returnTo=…`, necessary cookies only | The supplier portal renders its **logout** form *before* the consent form. Clicking "the first button" signs you out and loops. Scope to `form[action="/consent"]`. |
| Identifiers padded with zero-width chars | `VG-2027-7093` is `V​G​-​2…` in the DOM. Strip before matching (`sites.plain`). |
| Decoy links | Document rows carry a second "Öffnen" pointing at `/portal-survey`, and a banner offers "Akzeptieren und fortfahren" to the same. Follow the row's real href. |
| "Ich bin kein Roboter" slider | Gates the ERP notice and the webmail reading pane; disables the real form until satisfied. Supply `sites.unlock`. |

## tender-portal (Vergabeportal) — the anchor, and where the answer is submitted

- Search page lists tenders; the tender lives at `/tenders/<reference>`.
- Tabs: `?tab=documents`, `?tab=questions`, and `/submission` for the bid.
- **Unterlagen**: `/tenders/<ref>/documents/<n>`; content is collapsed behind "Inhalt anzeigen" and must be
  expanded. Typically the Leistungsverzeichnis (original quantities), a Leistungsbeschreibung and a
  Positionsübersicht (position, short text, quantity, unit, requirement reference).
- **Bieterfragen live in an iframe**: the tab page itself is nearly empty, the rows come from
  `/tenders/<ref>/questions-content`, paginated with `?page=N`. Read that url directly.
- **Q&A dates are in a hover tooltip**, `[role=tooltip]`, `display:none` until `:hover`. `innerText` does not
  see them; `textContent` does. The text carries both a local time and an ISO instant.
- **Q&A numbers are not chronological.** A lower number can be the newer record. Always sort by the tooltip
  date, never by the number — several rules say "the latest dated" record wins.
- A Q&A answer can **withdraw** an earlier one by number; the withdrawn record drops out and the previous one
  stands again.
- Answers sit in `<details>` inside a scrollable box; `textContent` returns all of it, but strip `<style>` and
  `<script>` nodes first or CSS leaks into the text.

## customer-portal (Lieferantenportal) — the technical specification

Landing page offers Dokumentenübersicht, Register öffnen and Lieferantenverwaltung. The specification
(Lastenheft) and its revisions live here; the revision marked current governs. **Not yet mapped in detail.**

## erp — materials, prices, stock

Presents itself as "SAP GUI für HTML", client PRD/100. Every path first serves an **error page** at `/notice`
("Dienst vorübergehend nicht verfügbar"). This is part of the puzzle, not an outage:

- "Erneut versuchen" is a **link**, not a button, and re-requesting the url keeps serving the error. Only
  clicking it gets through.
- The page behind it has the **same `/notice` url**, so tell them apart by content, not by address.
- Behind it: a Systemmitteilung gate wanting an **Empfangsnummer painted into a `<canvas>`** (invisible to the
  DOM — read it with `models.read_image`), a "Betriebsbedingungen" acknowledgement, and the robot slider.

**Nothing beyond that gate is mapped yet.**

## mail (Werkpost) — approvals, rates and the signing TAN

- Mailbox is the bidder's own; the domain in the "Angemeldet als" line is what the brief means by "the
  bidder's mailbox domain" when it names senders.
- Folders: Posteingang and Archiv, both paginated.
- Search combines words with **AND**, capped at 20 hits; the page itself advises combining the tender
  reference with a subject word.
- Subjects and senders are readable in the list; **message bodies are behind the robot slider**, including via
  the "Original öffnen" link to `/messages/<id>`.

**Message contents not read yet.**

## customer-portal, in detail

- Document register at `/documents`, **one row per page** (`?page=N`); the Lastenheft is a row of type
  "Technische Spezifikation" whose Zuordnung is the tender reference.
- Almost every link on these pages points at `/documents/supplier-guide` — the banner, the toolbar, the sidebar.
  The row's real link is the one labelled "Dokument anzeigen".
- The specification sits behind a **Dokumentenüberlassung** form, with no robot gate:
  `purpose` is a radio of `preview` / `reference` / `archive`, and the page states plainly that archive and
  preview grant **no** read permission — only `reference` ("Verbindliche Unterlagen zur Kenntnis genommen")
  reaches the governing revision. Picking either of the others is how you end up on a superseded one.
- The form also wants a **Belegnummer painted into a `<canvas>`**, a "Überlassungsbedingungen gelesen"
  acknowledgement, and submits with `operation=read`.
- The receipt field's label is CSS-generated (`content: attr(data-label)`), so it is not in the text either;
  read `data-label` if you need it.
- Canvases are painted by a script after load: capture them a moment later or you get a blank image.

## Reading obfuscated values — the one rule that covers most of it

The sites hide values from naive scraping in four different ways, and **the accessible name is the reliable
channel**: `aria-label` carries the clean value in most cases, so prefer it over `innerText` everywhere.

| Hidden how | Where seen | Read it from |
|---|---|---|
| Zero-width padding | every identifier | `aria-label`, else `plain()` |
| Painted into `<canvas>` | ERP Empfangsnummer, supplier-portal Belegnummer, spec Bohrung | `aria-label` when present, else `models.read_image` |
| CSS hover tooltip, `display:none` | Q&A dates, certification | `[role=tooltip]` textContent |
| CSS-generated label | receipt field | `data-label` attribute |

A cell holding a canvas contributes **no** `innerText`, so a row read as text silently loses a column and every
later value shifts left. Read tables cell by cell, never by splitting the row's text.

### Specification viewer

- Requirements live in an iframe: `/documents/specification/viewer?revision=<rev>`, one requirement per page
  (`&page=N`). The header says `gültig` on the governing revision.
- Numeric cells are tagged with a locale, `200 (en-US)` / `210 (de-DE)`. Parse per the tag: thousands and
  decimal separators swap between them.
- Revision dates in the history are each tagged `(MM/TT)` or `(TT/MM)`, and the tag varies between renders of
  the same table. Normalise per row; sorting by revision number gives the wrong answer.
