# ruff: noqa: E501 - the page script below is JavaScript, kept readable as written
"""What the agent is told of a page: its address, its text, and a numbered list
of what can be pressed or filled. Each element is marked in the page with its
number, so "click 12" finds exactly what the agent was shown. A password field's
value is never read."""

READ_PAGE = r"""
() => {
  document.querySelectorAll('[data-office-n]').forEach((el) => el.removeAttribute('data-office-n'));
  const wanted = 'a[href],button,input,select,textarea,summary,[role=button],[role=link],' +
    '[role=tab],[role=menuitem],[role=checkbox],[role=option],[contenteditable=true]';
  const elements = [];
  let n = 0;
  for (const el of document.querySelectorAll(wanted)) {
    const box = el.getBoundingClientRect();
    const style = getComputedStyle(el);
    if (box.width < 2 || box.height < 2 || style.visibility === 'hidden' || style.display === 'none') continue;
    if (el.disabled || (el.tagName === 'INPUT' && el.type === 'hidden')) continue;
    n += 1;
    if (n > 200) break;
    el.setAttribute('data-office-n', String(n));
    const type = el.tagName === 'INPUT' ? (el.type || 'text') : (el.tagName === 'BUTTON' ? (el.type || 'button') : '');
    const raw = el.getAttribute('aria-label') || el.innerText || el.value || el.placeholder ||
      el.title || el.alt || el.name || '';
    const label = String(raw).trim().replace(/\s+/g, ' ').slice(0, 120);
    let value = '';
    if (el.tagName === 'SELECT') value = [...el.options].slice(0, 30).map((o) => o.text.trim()).join(' | ');
    else if (type !== 'password' && 'value' in el) value = String(el.value || '').slice(0, 80);
    elements.push({
      n,
      tag: el.tagName.toLowerCase(),
      type,
      role: el.getAttribute('role') || '',
      label,
      href: el.href ? String(el.href).slice(0, 300) : '',
      value,
      in_view: box.top < innerHeight && box.bottom > 0,
    });
  }
  const text = (document.body ? document.body.innerText : '').replace(/\n{3,}/g, '\n\n').slice(0, 8000);
  return { url: location.href, title: document.title, text, elements };
}
"""
