// jobhunter capture (specs/017 phase 1e "Extraction"). Injected by the service worker into the
// top frame of the one tab you clicked, under activeTab, in the isolated world.
//
// The whole file is one arrow-function expression, called at once: it declares nothing at the
// top level and leaves nothing behind, so a repeat click (a second injection into the same
// document) cannot collide with the first. Its value is the result.
//
// It READS RAW FACTS ONLY. No DOM writes, no events, no timers, no network, nothing clicked,
// focused or scrolled (lint-checked, and the e2e compares the page before and after). The
// console decides what the facts mean.
(() => {
  const MAX_TEXT = 100000;
  const MAX_STR = 1000;
  const MAX_URL = 2048;
  const MAX_JSONLD = 400000;
  const MAX_POSTINGS = 5;
  const MAX_IFRAMES = 10;
  const MAX_MICRO = 100;
  const MIN_SELECTION = 40;
  // Host suffix -> selectors, for a site with no structured data. Empty at launch; a row is
  // added only with a fixture and a test, and never for LinkedIn or Indeed.
  const SITES = [];
  const SKIP = new Set([
    "NAV", "HEADER", "FOOTER", "ASIDE", "FORM", "DIALOG", "SCRIPT", "STYLE", "TEMPLATE",
    "INPUT", "TEXTAREA", "SELECT", "NOSCRIPT", "SVG", "BUTTON", "IFRAME", "OBJECT",
  ]);
  const BLOCK = new Set([
    "P", "DIV", "SECTION", "ARTICLE", "MAIN", "LI", "UL", "OL", "DL", "DT", "DD", "TR",
    "TABLE", "TBODY", "THEAD", "PRE", "BLOCKQUOTE", "H1", "H2", "H3", "H4", "H5", "H6", "BR",
    "HR", "FIGURE", "FIGCAPTION", "ADDRESS",
  ]);
  const truncated = [];

  const cap = (text, limit, name) => {
    const s = typeof text === "string" ? text : "";
    if (s.length <= limit) {
      return s;
    }
    if (!truncated.includes(name)) {
      truncated.push(name);
    }
    return s.slice(0, limit);
  };
  const short = (text, name) => {
    const s = cap((text || "").replace(/\s+/g, " ").trim(), MAX_STR, name);
    return s || null;
  };
  const httpUrl = (raw, name) => {
    if (!raw) {
      return null;
    }
    let u;
    try {
      u = new URL(raw, document.baseURI);
    } catch (e) {
      return null;
    }
    if (u.protocol !== "http:" && u.protocol !== "https:") {
      return null;
    }
    if (u.href.length > MAX_URL) {
      if (!truncated.includes(name)) {
        truncated.push(name);
      }
      return null; // a cut URL is a wrong URL: dropped, never truncated
    }
    return u.href;
  };
  const host = location.hostname.toLowerCase();
  const onHost = (re) => re.test(host);
  const LINKEDIN = /(^|\.)linkedin\.com$/;
  const INDEED = /(^|\.)indeed\.(com|co\.[a-z]{2}|com\.[a-z]{2}|[a-z]{2})$/;

  // ── JSON-LD: every JobPosting object, as parsed, re-serialized ─────────────
  const jsonld = [];
  let jsonldDropped = 0;
  const isPosting = (obj) => {
    const t = obj["@type"];
    const types = Array.isArray(t) ? t : [t];
    return types.some((x) => typeof x === "string" && /(^|[/:])JobPosting$/.test(x));
  };
  const walk = (node, depth) => {
    if (depth > 4 || jsonld.length >= MAX_POSTINGS || node === null) {
      return;
    }
    if (Array.isArray(node)) {
      node.forEach((item) => walk(item, depth + 1));
    } else if (typeof node === "object") {
      if (isPosting(node)) {
        const blob = JSON.stringify(node);
        if (blob.length > MAX_JSONLD) {
          jsonldDropped += 1;
        } else {
          jsonld.push(blob);
        }
      } else if ("@graph" in node) {
        walk(node["@graph"], depth + 1);
      }
    }
  };
  for (const el of document.querySelectorAll('script[type="application/ld+json"]')) {
    let parsed = null;
    try {
      parsed = JSON.parse(el.textContent || "");
    } catch (e) {
      parsed = null;
    }
    walk(parsed, 0);
  }

  // ── Microdata: itemprop name and content-or-text pairs ─────────────────────
  const microdata = [];
  const scope = document.querySelector('[itemtype*="schema.org/JobPosting"]');
  if (scope) {
    for (const el of scope.querySelectorAll("[itemprop]")) {
      if (microdata.length >= MAX_MICRO) {
        break;
      }
      const name = (el.getAttribute("itemprop") || "").trim().slice(0, 100);
      const raw = el.hasAttribute("content") ? el.getAttribute("content") : el.textContent;
      if (name) {
        microdata.push({name, value: cap((raw || "").trim(), MAX_TEXT, "microdata")});
      }
    }
  }

  // ── Visible text: a read-only recursive walk, hidden text never collected ──
  const hidden = (el) => {
    if (typeof el.checkVisibility === "function"
        && !el.checkVisibility({opacityProperty: true, visibilityProperty: true})) {
      return true;
    }
    const box = el.getBoundingClientRect();
    if (box.width <= 1 && box.height <= 1) {
      return true; // a visually hidden ("sr-only") span
    }
    const cs = getComputedStyle(el);
    return cs.clip === "rect(0px, 0px, 0px, 0px)" || cs.clipPath === "inset(50%)";
  };
  const textOf = (root) => {
    const out = [];
    const visit = (node) => {
      if (node.nodeType === Node.TEXT_NODE) {
        out.push(node.data.replace(/\s+/g, " "));
        return;
      }
      if (node.nodeType !== Node.ELEMENT_NODE || SKIP.has(node.tagName.toUpperCase())) {
        return;
      }
      if (node.tagName.toUpperCase() === "BR") {
        out.push("\n");
        return;
      }
      if (node.getAttribute("aria-hidden") === "true" || hidden(node)) {
        return;
      }
      const block = BLOCK.has(node.tagName.toUpperCase());
      if (block) {
        out.push("\n");
      }
      for (const child of node.childNodes) {
        visit(child);
      }
      if (block) {
        out.push("\n");
      }
    };
    visit(root);
    return out.join("").replace(/[ \t]+/g, " ").replace(/ *\n */g, "\n")
      .replace(/\n{3,}/g, "\n\n").trim();
  };
  const root = document.querySelector("main") || document.querySelector("article")
    || document.querySelector("[role=main]") || document.body;
  const pageText = root ? cap(textOf(root), MAX_TEXT, "page_text") : "";

  // ── Site table row (none at launch) ────────────────────────────────────────
  let site = null;
  const row = SITES.find((r) => host === r.host || host.endsWith("." + r.host));
  if (row) {
    const pick = (sel) => {
      const el = sel ? document.querySelector(sel) : null;
      return el ? textOf(el) : "";
    };
    site = {
      title: short(pick(row.title), "site"),
      employer: short(pick(row.employer), "site"),
      location: short(pick(row.location), "site"),
      description: cap(pick(row.description), MAX_TEXT, "site") || null,
    };
  }

  // ── Selection: ignored inside an editable element ──────────────────────────
  let selection = "";
  const sel = window.getSelection();
  const active = document.activeElement;
  const editing = active && (active.isContentEditable || /^(INPUT|TEXTAREA|SELECT)$/.test(active.tagName));
  if (sel && !editing && sel.rangeCount > 0) {
    const anchor = sel.anchorNode && (sel.anchorNode.nodeType === Node.ELEMENT_NODE
      ? sel.anchorNode : sel.anchorNode.parentElement);
    const inEditable = anchor && anchor.closest("input, textarea, select, [contenteditable]");
    const text = sel.toString();
    if (!inEditable && text.trim().length >= MIN_SELECTION) {
      selection = cap(text.trim(), MAX_TEXT, "selection");
    }
  }

  // ── URLs ───────────────────────────────────────────────────────────────────
  const canonicalEl = document.querySelector('link[rel="canonical"]');
  const canonical = canonicalEl ? httpUrl(canonicalEl.getAttribute("href"), "canonical") : null;
  const iframes = [];
  for (const frame of document.querySelectorAll("iframe[src]")) {
    if (iframes.length >= MAX_IFRAMES) {
      break;
    }
    const src = httpUrl(frame.getAttribute("src"), "iframes");
    if (src && !iframes.includes(src)) {
      iframes.push(src);
    }
  }

  // ── LinkedIn and Indeed only: the Apply control, read, never touched ───────
  let applyControl = null;
  if (onHost(LINKEDIN) || onHost(INDEED)) {
    const label = (el) => ((el.getAttribute("aria-label") || "") + " " + (el.textContent || ""))
      .replace(/\s+/g, " ").trim();
    const candidates = Array.from(document.querySelectorAll("a, button"))
      .filter((el) => /\bapply\b/i.test(label(el)) && !hidden(el));
    const el = candidates[0];
    if (el) {
      const text = label(el);
      let kind = "unknown";
      if (/easy apply/i.test(text) || (onHost(INDEED) && /apply now/i.test(text))) {
        kind = "easy_apply";
      } else if (/company site|company website|^apply\b|\bapply on\b|\bapply\b/i.test(text)) {
        kind = "offsite";
      }
      const href = el.tagName.toUpperCase() === "A"
        ? httpUrl(el.getAttribute("href"), "apply_control") : null;
      applyControl = {kind, label: cap(text, MAX_STR, "apply_control"), href};
    }
  }

  const meta = (prop) => {
    const el = document.querySelector('meta[property="' + prop + '"]');
    return el ? el.getAttribute("content") : null;
  };
  return {
    url: httpUrl(location.href, "url") || location.href.slice(0, MAX_URL),
    canonical,
    iframes,
    jsonld,
    jsonld_dropped: jsonldDropped,
    microdata,
    site,
    page_text: pageText,
    og_title: short(meta("og:title"), "og_title"),
    og_site_name: short(meta("og:site_name"), "og_site_name"),
    document_title: short(document.title, "document_title"),
    selection,
    apply_control: applyControl,
    truncated,
  };
})();
