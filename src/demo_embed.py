"""Mount the published V3 portfolio in the read-only Streamlit branch."""

import json
import logging
from pathlib import Path

import streamlit as st

from evals.validate_demo_data import read_json


DOCS = Path(__file__).resolve().parents[1] / "docs"
PAGES_URL = "https://studing-endless.github.io/ai-last-mile-delivery-assistant/"
LOGGER = logging.getLogger(__name__)

_STYLESHEET = '<link href="./assets/css/portfolio.css" rel="stylesheet"/>'
_MODULE = '<script src="./assets/js/portfolio.js" type="module"></script>'
_WORKBENCH_LINK = 'href="https://ai-last-mile-assistant.streamlit.app"'

# The iframe owns the V3 viewport and CSS; the outer Streamlit page owns scrolling.
_PARENT = """
export default function ({parentElement, data}) {
  const frame = parentElement.querySelector("iframe");
  const page = parentElement.host.closest('[data-testid="stMain"]');

  function receive(event) {
    if (event.source !== frame.contentWindow || event.origin !== "null") return;
    const message = event.data;
    if (message?.type === "lastmile-resize" &&
        Number.isInteger(message.height) && message.height > 0 && message.height <= 100000) {
      frame.style.height = `${message.height}px`;
    } else if (message?.type === "lastmile-anchor" &&
               Number.isFinite(message.top) && message.top >= 0 && message.top <= frame.offsetHeight) {
      const top = frame.getBoundingClientRect().top + message.top;
      const behavior = message.smooth === true ? "smooth" : "instant";
      if (page) page.scrollTo({top: page.scrollTop + top - page.getBoundingClientRect().top, behavior});
      else window.scrollTo({top: window.scrollY + top, behavior});
    }
  }

  window.addEventListener("message", receive);
  frame.srcdoc = data.document;
  return () => window.removeEventListener("message", receive);
}
"""

_CHILD = """
function sendHeight() {
  // The root scrollHeight also includes the iframe viewport, so it cannot
  // measure a decrease after a disclosure collapses.
  parent.postMessage({
    type: "lastmile-resize",
    height: Math.ceil(document.body.getBoundingClientRect().height),
  }, "*");
}
new ResizeObserver(sendHeight).observe(document.body);
sendHeight();

for (const link of document.querySelectorAll('a[href^="https://"]')) {
  link.target = "_blank";
  link.rel = "noopener noreferrer";
}
document.addEventListener("click", (event) => {
  const link = event.target.closest('a[href^="#"]');
  if (!link) return;
  const target = document.getElementById(link.hash.slice(1));
  if (!target) return;
  event.preventDefault();
  target.tabIndex = -1;
  target.focus({preventScroll: true});
  parent.postMessage({
    type: "lastmile-anchor",
    top: target.getBoundingClientRect().top + window.scrollY,
    smooth: !matchMedia("(prefers-reduced-motion: reduce)").matches,
  }, "*");
});
"""

_LAYOUT = """
<style>
[data-testid="stHeader"] {display: none}
[data-testid="stMainBlockContainer"] {max-width: none; padding: 0}
</style>
"""


def build_document(docs: Path = DOCS) -> str:
    """Inline local public assets without depending on a file-serving route."""
    page = (docs / "index.html").read_text(encoding="utf-8")
    stylesheet = (docs / "assets/css/portfolio.css").read_text(encoding="utf-8")
    module = (docs / "assets/js/portfolio.js").read_text(encoding="utf-8")
    payload = read_json(docs / "assets/data/portfolio.json")
    if not isinstance(payload, dict) or payload.get("schema_version") != 1:
        raise ValueError("Unsupported portfolio snapshot")
    for marker in (_STYLESHEET, _MODULE, _WORKBENCH_LINK, "现有线上工作台 ↗"):
        if page.count(marker) != 1:
            raise ValueError("Unexpected portfolio markup")
    # Escape '<' so text in the JSON cannot close the data script element.
    data = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), allow_nan=False).replace("<", "\\u003c")
    scripts = (
        f'<script id="portfolio-data" type="application/json">{data}</script>'
        f'<script type="module">{module}</script><script>{_CHILD}</script>'
    )
    return (page.replace(_STYLESHEET, f"<style>{stylesheet}</style>", 1)
            .replace(_MODULE, scripts, 1)
            .replace(_WORKBENCH_LINK, f'href="{PAGES_URL}"', 1)
            .replace("现有线上工作台 ↗", "打开独立作品集 ↗", 1))


def show_portfolio() -> bool:
    try:
        document = build_document()
    except (OSError, ValueError) as exc:
        LOGGER.warning("V3 demo assets unavailable (%s); using the D4 demo.", type(exc).__name__)
        return False
    component = st.components.v2.component(
        "lastmile_v3_portfolio",
        html='<iframe title="末端配送静态交互作品集" sandbox="allow-scripts allow-popups allow-popups-to-escape-sandbox"></iframe>',
        css="iframe {display:block; width:100%; height:1px; border:0}",
        js=_PARENT,
    )
    component(data={"document": document}, key="lastmile_v3")
    st.html(_LAYOUT)
    return True
