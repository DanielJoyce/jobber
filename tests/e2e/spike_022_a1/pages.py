"""Hand-written fixture pages for the 022 A1 spike. Synthetic; canary values only."""

from __future__ import annotations

EMP = "https://jobs.example-employer.test"
GH = "https://boards.greenhouse.io"
JB = "https://job-boards.greenhouse.io"
WD = "https://acme.myworkdayjobs.com"

STYLE = """<style>
body{margin:0;padding:8px;font:14px sans-serif}
label,input,textarea,select,button,div,a{display:block;margin:6px 0}
input[type=file]{width:200px}
</style>"""

# Page-side (main world) recorder, as the ATS's own scripts would see events.
RECORDER = """<script>
window.__submits = 0; window.__events = []; window.__fileHash = null;
document.addEventListener('submit', (e) => {
  window.__submits++;
  window.__events.push({type: 'submit', isTrusted: e.isTrusted,
    submitter: e.submitter ? e.submitter.id : null,
    active: navigator.userActivation.isActive});
  e.preventDefault();
}, true);
document.addEventListener('click', (e) => {
  if (e.target.closest && e.target.closest('[data-watch]')) {
    window.__events.push({type: 'click', id: e.target.id, isTrusted: e.isTrusted,
      active: navigator.userActivation.isActive});
  }
}, true);
document.addEventListener('change', async (e) => {
  if (e.target.type !== 'file' || !e.target.files.length) return;
  const buf = await e.target.files[0].arrayBuffer();
  const h = await crypto.subtle.digest('SHA-256', buf);
  window.__fileHash = [...new Uint8Array(h)].map((b) => b.toString(16).padStart(2, '0')).join('');
  window.__fileSize = buf.byteLength;
});
</script>"""

FORM = f"""<!doctype html><meta charset="utf-8"><title>Apply</title>{STYLE}
<iframe name="sink" style="display:none"></iframe>
<form id="f" action="/apply" method="post" target="sink">
 <input id="first_name" name="first_name" aria-label="First name">
 <input id="email" type="email" aria-label="Email">
 <input id="resume" name="resume" type="file">
 <textarea id="cover_letter" name="cover_letter"></textarea>
 <input id="cb" type="checkbox"><select id="sel"><option>a</option><option>b</option></select>
 <input id="t2" type="text">
 <button id="submit_btn" type="submit" data-watch>Submit application</button>
 <button id="plain_btn" data-watch>Continue</button>
 <input id="input_submit" type="submit" value="Go" data-watch>
 <input id="img_submit" type="image" alt="go" data-watch
  src="data:image/gif;base64,R0lGODlhAQABAAAAACw=">
 <button id="btn_button" type="button">Add another</button>
 <button id="reset_btn" type="reset">Clear</button>
 <div id="div_btn" role="button">Submit</div>
 <a id="a_apply" href="#">Apply</a>
 <label id="lbl" for="submit_btn">Send it</label>
 <div id="wrapper"><button id="inner_submit" type="submit" data-watch>Go on</button></div>
</form>
<button id="outside" form="f" type="submit" data-watch>Outside</button>
{RECORDER}"""

# Selectors that must be refused, and ones that must be allowed.
SUBMIT_LIKE = [
    "#f",
    "#submit_btn",
    "#plain_btn",
    "#input_submit",
    "#img_submit",
    "#div_btn",
    "#a_apply",
    "#lbl",
    "#wrapper",
    "#inner_submit",
    "#outside",
]
SAFE = [
    "#first_name",
    "#email",
    "#resume",
    "#cover_letter",
    "#cb",
    "#sel",
    "#t2",
    "#btn_button",
    "#reset_btn",
]

EMPLOYER = f"""<!doctype html><meta charset="utf-8"><title>Careers</title>{STYLE}
<p id="emp">Employer page</p>
<iframe id="ats" width="500" height="560"
 src="{GH}/embed/job_app?for=boardsynthetic&token=7001"></iframe>
{RECORDER}"""

STEP1 = f"""<!doctype html><meta charset="utf-8"><title>step1</title>{STYLE}
<p id="step">step1</p><input id="resume" type="file">"""
STEP2 = f"""<!doctype html><meta charset="utf-8"><title>step2</title>{STYLE}
<p id="step">step2</p><input id="resume" type="file">"""
EMPLOYER_STEPS = f"""<!doctype html><meta charset="utf-8"><title>Steps</title>
<iframe id="ats" src="{GH}/embed/step1" width="400" height="200"></iframe>"""

# A "React-like" form: the root subtree is rebuilt on every state change (the worst case of a
# virtual-DOM remount) and a banner above shifts the layout. React itself is not vendored, so
# this stands in for it; the live check on job-boards.greenhouse.io covers the real library.
REACT = f"""<!doctype html><meta charset="utf-8"><title>Job board</title>{STYLE}
<div id="root"></div>
<script>
let n = 0, banner = false;
function render() {{
  n++;
  document.getElementById('root').innerHTML =
    (banner ? '<div id="banner" style="height:80px;background:#eee">notice</div>' : '') +
    '<input id="first_name" value="r' + n + '"><input id="resume" type="file">' +
    '<textarea id="cover_letter"></textarea><span id="count">' + n + '</span>';
}}
window.rerender = (b) => {{ if (b !== undefined) banner = b; render(); }};
render();
</script>"""

PLAIN = f"""<!doctype html><meta charset="utf-8"><title>wd</title>{STYLE}
<input id="resume" type="file">"""
