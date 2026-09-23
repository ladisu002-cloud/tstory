"""
네이버 스마트에디터(SmartEditor ONE) 입력 도우미 — 내 컴퓨터(로컬) 전용

1) build_editor_markdown / markdown_to_editor_html
   글 결과(article)를 스마트에디터에 붙여넣기 좋은 HTML로 바꿉니다.
   (소제목·굵게·표·목록·링크·FAQ·태그 포함)

2) 스크립트 실행 (Streamlit 앱이 백그라운드로 호출)
   python naver_editor.py <payload.json>
   - 내 컴퓨터의 Chrome을 전용 프로필로 열고
   - 네이버 블로그 글쓰기 화면에 제목과 본문을 넣은 뒤
   - (선택) '저장' 버튼으로 임시저장만 합니다. '발행'은 절대 누르지 않습니다.
   - 로그인은 처음 한 번 사용자가 직접 합니다. 비밀번호는 저장하지 않습니다.
   - 브라우저 창은 열어둔 채로 두니, 수정·이미지 삽입 후 직접 발행하세요.

필요 설치(로컬 PC에서 한 번):
    pip install playwright
    (Google Chrome이 설치돼 있어야 합니다)
"""
import html
import json
import os
import re
import sys
import time
from pathlib import Path


# ---------------------------------------------------------------------------
# 1. 글 결과 → 스마트에디터용 마크다운/HTML
# ---------------------------------------------------------------------------

def build_editor_markdown(article):
    """본문 마크다운에 공식 링크·FAQ·태그를 넣어 최종 마크다운을 만듭니다."""
    body = article.get("body_markdown", "") or ""

    # 공식 행동 링크: 관련 문장이 있는 줄 바로 아래에 링크 줄을 넣습니다.
    for link in sorted(article.get("inline_official_links", []) or [],
                       key=lambda x: len(str(x.get("insert_after", ""))), reverse=True):
        anchor = str(link.get("insert_after", "")).strip()
        url = str(link.get("url", "")).strip()
        label = str(link.get("label", "확인하기")).strip() or "확인하기"
        if not anchor or not url:
            continue
        idx = body.find(anchor)
        if idx < 0:
            continue
        line_end = body.find("\n", idx + len(anchor))
        if line_end < 0:
            line_end = len(body)
        body = body[:line_end] + f"\n\n[🔗 {label}]({url})\n" + body[line_end:]

    # FAQ: 마지막 '정리/마무리/결론' 소제목 바로 앞에 넣습니다.
    faq_items = article.get("faq", []) or []
    if faq_items:
        lines = ["## 자주 묻는 질문", ""]
        for item in faq_items:
            q = str(item.get("question", "")).strip()
            a = str(item.get("answer", "")).strip()
            if q:
                lines.append(f"**Q. {q}**")
            if a:
                lines.append(f"A. {a}")
            lines.append("")
        faq_md = "\n".join(lines).rstrip() + "\n\n"
        pattern = re.compile(r"(?m)^##\s+(?:\d+\.\s*)?(?:결론|마무리|[^\n]{0,15}정리)[^\n]{0,20}$")
        matches = list(pattern.finditer(body))
        if matches:
            m = matches[-1]
            body = body[:m.start()].rstrip() + "\n\n" + faq_md + body[m.start():]
        else:
            body = body.rstrip() + "\n\n" + faq_md

    # 태그: 본문 끝에 #태그 형태로 넣습니다.
    tags = []
    for raw in article.get("tags", []) or []:
        t = str(raw).strip().lstrip("#").replace(",", "").replace(" ", "")
        if t and t not in tags:
            tags.append(t)
    if tags:
        body = body.rstrip() + "\n\n" + " ".join(f"#{t}" for t in tags) + "\n"

    return re.sub(r"\n{3,}", "\n\n", body).strip() + "\n"


def _inline(text):
    """굵게·링크만 HTML로 바꾸고 나머지는 이스케이프합니다."""
    out, pos = [], 0
    for m in re.finditer(r"\*\*(.+?)\*\*|\[([^\]]+)\]\((https?://[^)\s]+)\)", text):
        out.append(html.escape(text[pos:m.start()]))
        if m.group(1) is not None:
            out.append(f"<b>{html.escape(m.group(1))}</b>")
        else:
            out.append(f'<a href="{html.escape(m.group(3))}">{html.escape(m.group(2))}</a>')
        pos = m.end()
    out.append(html.escape(text[pos:]))
    return "".join(out)


def markdown_to_editor_html(md):
    """간단한 마크다운 → HTML 변환(소제목, 문단, 목록, 표, 인용, 굵게, 링크)."""
    lines = (md or "").split("\n")
    out, i = [], 0
    while i < len(lines):
        line = lines[i].rstrip()
        s = line.strip()
        if not s:
            i += 1
            continue
        m = re.match(r"^(#{1,6})\s+(.*)$", s)
        if m:
            level = 2 if len(m.group(1)) <= 2 else 3
            out.append(f"<h{level}>{_inline(m.group(2))}</h{level}>")
            i += 1
            continue
        if s.startswith("|"):
            rows = []
            while i < len(lines) and lines[i].strip().startswith("|"):
                rows.append(lines[i].strip())
                i += 1
            cells = [[c.strip() for c in r.strip("|").split("|")] for r in rows]
            cells = [r for r in cells if not all(re.fullmatch(r":?-{2,}:?", c or "") for c in r)]
            if cells:
                t = ['<table border="1" style="border-collapse:collapse;">']
                for ri, r in enumerate(cells):
                    tag = "th" if ri == 0 else "td"
                    t.append("<tr>" + "".join(f"<{tag}>{_inline(c)}</{tag}>" for c in r) + "</tr>")
                t.append("</table>")
                out.append("".join(t))
            continue
        if re.match(r"^[-*]\s+", s):
            items = []
            while i < len(lines) and re.match(r"^\s*[-*]\s+", lines[i]):
                items.append(re.sub(r"^\s*[-*]\s+", "", lines[i]).strip())
                i += 1
            out.append("<ul>" + "".join(f"<li>{_inline(x)}</li>" for x in items) + "</ul>")
            continue
        if re.match(r"^\d+\.\s+", s):
            items = []
            while i < len(lines) and re.match(r"^\s*\d+\.\s+", lines[i]):
                items.append(re.sub(r"^\s*\d+\.\s+", "", lines[i]).strip())
                i += 1
            out.append("<ol>" + "".join(f"<li>{_inline(x)}</li>" for x in items) + "</ol>")
            continue
        if s.startswith(">"):
            quote = []
            while i < len(lines) and lines[i].strip().startswith(">"):
                quote.append(lines[i].strip().lstrip(">").strip())
                i += 1
            out.append("<blockquote>" + "<br>".join(_inline(x) for x in quote) + "</blockquote>")
            continue
        out.append(f"<p>{_inline(s)}</p>")
        i += 1
    return "\n".join(out)


def markdown_to_plain(md):
    text = re.sub(r"^#{1,6}\s+", "", md or "", flags=re.M)
    text = re.sub(r"\*\*(.*?)\*\*", r"\1", text)
    text = re.sub(r"\[(.*?)\]\((https?://[^)\s]+)\)", r"\1 \2", text)
    return text


def build_editor_content(article, style=None):
    md = build_editor_markdown(article)
    return {
        "se_html": build_se_markup(article, style),
        "title": article.get("seo_title") or article.get("home_title") or "",
        "markdown": md,
        "html": markdown_to_editor_html(md),
        "plain": markdown_to_plain(md),
    }


# ---------------------------------------------------------------------------
# 2. 브라우저 자동 입력 (로컬 전용)
# ---------------------------------------------------------------------------

DEFAULT_PROFILE_DIR = str(Path.home() / ".naver_blog_writer_profile")


def playwright_available():
    try:
        import playwright  # noqa: F401
        return True
    except Exception:
        return False


def _log(status_path, message):
    line = f"[{time.strftime('%H:%M:%S')}] {message}"
    # 파일 기록을 먼저 합니다(콘솔 인코딩 문제로 print가 실패해도 진행 상황은 남도록).
    if status_path:
        try:
            with open(status_path, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except Exception:
            pass
    try:
        print(line, flush=True)
    except Exception:
        pass


EDITOR_SELECTORS = ".se-documentTitle, .se-title-text, [class*='se-documentTitle'], .se-content"


def _editor_frame(ctx):
    """열린 모든 탭·프레임에서 글쓰기 에디터를 찾습니다. 반환: (page, frame) 또는 (None, None)"""
    for pg in ctx.pages:
        for fr in pg.frames:
            try:
                if fr.locator(EDITOR_SELECTORS).count() > 0:
                    return pg, fr
            except Exception:
                continue
    return None, None


def _describe_pages(ctx):
    out = []
    for pg in ctx.pages:
        try:
            out.append(pg.url + " | 프레임: " + ", ".join(fr.url[:80] for fr in pg.frames[1:4]))
        except Exception:
            continue
    return " / ".join(out)[:600]


def _click_if_visible(frame, selector, timeout=1500):
    try:
        loc = frame.locator(selector).first
        loc.wait_for(state="visible", timeout=timeout)
        loc.click()
        return True
    except Exception:
        return False


def _click_save_button(frame):
    """'저장'(임시저장) 버튼만 누릅니다. '발행' 버튼은 절대 누르지 않습니다."""
    try:
        buttons = frame.locator("button").filter(has_text=re.compile(r"^\s*저장"))
        for i in range(buttons.count()):
            b = buttons.nth(i)
            label = (b.inner_text() or "").strip()
            if "발행" in label or not b.is_visible():
                continue
            b.click()
            return True
    except Exception:
        pass
    try:
        b = frame.locator("button[class*='save_btn']").first
        if b.is_visible() and "발행" not in (b.inner_text() or ""):
            b.click()
            return True
    except Exception:
        pass
    return False


def _body_text_length(frame):
    try:
        return frame.evaluate(
            "() => { const el = document.querySelector('.se-content') || document.querySelector('.se-main-container'); "
            "return el ? el.innerText.length : 0; }"
        )
    except Exception:
        return 0


def send_to_naver(payload, status_path=None):
    from playwright.sync_api import sync_playwright

    blog_id = payload["blog_id"].strip()
    title = payload.get("title", "")
    html_body = payload.get("se_html") or payload.get("html", "")
    plain_body = payload.get("plain", "")
    save_draft = bool(payload.get("save_draft", True))
    profile_dir = payload.get("profile_dir") or DEFAULT_PROFILE_DIR
    write_url = payload.get("write_url") or f"https://blog.naver.com/{blog_id}?Redirect=Write"
    keep_open = payload.get("keep_open", True)
    paste_key = "Meta+V" if sys.platform == "darwin" else "Control+V"

    with sync_playwright() as p:
        try:
            ctx = p.chromium.launch_persistent_context(
                user_data_dir=profile_dir,
                channel=payload.get("browser_channel", "chrome") or None,
                headless=bool(payload.get("headless", False)),
                viewport=None,
                args=["--start-maximized"],
            )
        except Exception as e:
            msg = str(e)
            if "already in use" in msg or "ProcessSingleton" in msg or "user data" in msg.lower():
                _log(status_path, "❌ 이전에 연 자동 입력용 Chrome 창이 아직 열려 있어요. 그 창을 닫고 다시 '스마트에디터에 입력하기'를 눌러주세요.")
            elif "chrome" in msg.lower() and ("not found" in msg.lower() or "executable" in msg.lower()):
                _log(status_path, "❌ Google Chrome을 찾지 못했어요. Chrome을 설치한 뒤 다시 시도해 주세요.")
            else:
                _log(status_path, f"❌ Chrome을 여는 중 오류: {msg[:300]}")
            return
        try:
            from urllib.parse import urlparse
            u = urlparse(write_url)
            ctx.grant_permissions(["clipboard-read", "clipboard-write"], origin=f"{u.scheme}://{u.netloc}")
        except Exception:
            pass
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        _log(status_path, "네이버 블로그 글쓰기 화면을 여는 중…")
        page.goto(write_url)

        # 로그인이 필요하면 사용자가 직접 로그인할 때까지 최대 5분 기다립니다.
        frame = None
        deadline = time.time() + 300
        login_notice = False
        last_beat = time.time()
        _log(status_path, "글쓰기 에디터를 찾는 중… (팝업이 뜨면 직접 '취소'를 눌러도 괜찮아요)")
        while time.time() < deadline:
            if any("nid.naver.com" in pg.url for pg in ctx.pages):
                if not login_notice:
                    _log(status_path, "로그인이 필요해요. 열린 창에서 직접 로그인해 주세요(최대 5분 대기). 로그인 상태는 이 전용 창에 저장돼요.")
                    login_notice = True
                time.sleep(2)
                continue
            if login_notice:
                # 로그인 직후: 글쓰기 화면으로 다시 이동하고 대기 시간을 넉넉히 늘립니다.
                login_notice = False
                deadline = time.time() + 180
                _log(status_path, "로그인 확인! 글쓰기 화면으로 이동해요.")
                page = ctx.pages[-1] if ctx.pages else ctx.new_page()
                if "Redirect=Write" not in page.url and "postwrite" not in page.url.lower() and "PostWriteForm" not in page.url:
                    page.goto(write_url)
            page_found, frame = _editor_frame(ctx)
            if frame:
                page = page_found
                break
            if time.time() - last_beat > 10:
                last_beat = time.time()
                _log(status_path, "아직 에디터를 찾는 중… 현재 화면: " + _describe_pages(ctx))
            time.sleep(1.5)
        if not frame:
            _log(status_path, "❌ 글쓰기 에디터를 찾지 못했어요. 현재 화면: " + _describe_pages(ctx))
            _log(status_path, "이 로그를 캡처해서 보여주시면 맞춰서 고칠게요.")
            if keep_open:
                page.wait_for_event("close", timeout=0)
            return

        # '작성 중인 글이 있습니다' 팝업 → 새 글로 시작(취소), 도움말 패널 닫기
        _click_if_visible(frame, ".se-popup-button-cancel")
        _click_if_visible(frame, ".se-help-panel-close-button")

        # 제목 입력
        _log(status_path, "제목 입력 중…")
        title_ok = False
        for sel in (".se-documentTitle .se-text-paragraph", ".se-title-text", ".se-documentTitle"):
            try:
                frame.locator(sel).first.click(timeout=3000)
                title_ok = True
                break
            except Exception:
                try:
                    frame.locator(sel).first.click(timeout=1000, force=True)
                    title_ok = True
                    break
                except Exception:
                    continue
        if not title_ok:
            # 마지막 방법: 제목 문단에 직접 커서를 둡니다.
            try:
                title_ok = frame.evaluate(
                    """() => {
                        const el = document.querySelector('.se-documentTitle .se-text-paragraph')
                                || document.querySelector('.se-title-text');
                        if (!el) return false;
                        const editable = el.closest('[contenteditable="true"]') || el;
                        window.focus(); editable.focus();
                        if (!el.firstChild) el.appendChild(document.createElement('br'));
                        const r = document.createRange(); r.setStart(el, 0); r.collapse(true);
                        const s = window.getSelection(); s.removeAllRanges(); s.addRange(r);
                        return true;
                    }"""
                )
            except Exception:
                title_ok = False
        if title_ok:
            page.keyboard.insert_text(title)
        else:
            _log(status_path, "⚠️ 제목 칸을 찾지 못했어요. 제목은 직접 입력해 주세요.")

        # 본문 입력: 서식 포함 붙여넣기 → 실패하면 synthetic paste → 실패하면 클립보드에 남겨둠
        _log(status_path, "본문 붙여넣는 중…")
        body_para = frame.locator(".se-component.se-text .se-text-paragraph").first
        try:
            body_para.click(timeout=4000)
        except Exception:
            try:
                body_para.click(timeout=2000, force=True)
            except Exception:
                page.keyboard.press("Enter")
        before = _body_text_length(frame)

        pasted = False
        try:
            page.evaluate(
                """async ([h, t]) => {
                    const item = new ClipboardItem({
                        'text/html': new Blob([h], {type: 'text/html'}),
                        'text/plain': new Blob([t], {type: 'text/plain'})
                    });
                    await navigator.clipboard.write([item]);
                }""",
                [html_body, plain_body],
            )
            page.keyboard.press(paste_key)
            time.sleep(2.5)
            pasted = _body_text_length(frame) > before + 50
        except Exception as e:
            _log(status_path, f"클립보드 붙여넣기 실패, 다른 방법으로 시도해요: {e}")

        if not pasted:
            try:
                frame.evaluate(
                    """([h, t]) => {
                        const target = document.activeElement || document.querySelector('.se-content');
                        const dt = new DataTransfer();
                        dt.setData('text/html', h);
                        dt.setData('text/plain', t);
                        target.dispatchEvent(new ClipboardEvent('paste', {clipboardData: dt, bubbles: true, cancelable: true}));
                    }""",
                    [html_body, plain_body],
                )
                time.sleep(2.5)
                pasted = _body_text_length(frame) > before + 50
            except Exception as e:
                _log(status_path, f"두 번째 붙여넣기 방식도 실패했어요: {e}")

        if not pasted:
            _log(status_path, "⚠️ 본문 자동 입력에 실패했어요. 앱의 '서식 포함 복사' 버튼으로 복사한 뒤, 본문 첫 줄을 클릭하고 Ctrl+V로 붙여넣어 주세요.")
        else:
            _log(status_path, "✅ 제목과 본문을 입력했어요.")
            try:
                n_quote = frame.evaluate("() => document.querySelectorAll('.se-component.se-quotation').length")
                n_table = frame.evaluate("() => document.querySelectorAll('.se-component.se-table').length")
                if n_quote:
                    _log(status_path, f"서식 확인: 인용구 {n_quote}개, 표 {n_table}개가 에디터 요소로 만들어졌어요.")
                else:
                    _log(status_path, "⚠️ 인용구가 에디터 요소로 바뀌지 않았어요. 이 로그를 캡처해 보여주세요(버튼 클릭 방식으로 바꿔야 해요).")
            except Exception:
                pass
            if save_draft:
                saved = _click_save_button(frame)
                if saved:
                    _log(status_path, "✅ 임시저장했어요. 창에서 확인·수정·이미지 삽입 후 직접 '발행'을 눌러주세요.")
                else:
                    _log(status_path, "⚠️ 임시저장 버튼을 찾지 못했어요. 창 오른쪽 위 '저장'을 직접 눌러주세요.")
            else:
                _log(status_path, "창에서 확인·수정·이미지 삽입 후 직접 저장 또는 발행해 주세요.")

        # 사용자가 창을 닫을 때까지 기다립니다(자동으로 닫지 않음).
        try:
            if keep_open:
                page.wait_for_event("close", timeout=0)
        except Exception:
            pass
        try:
            ctx.close()
        except Exception:
            pass


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("사용법: python naver_editor.py <payload.json>")
        sys.exit(1)
    payload_path = sys.argv[1]
    with open(payload_path, encoding="utf-8") as f:
        data = json.load(f)
    status = data.get("status_path")
    try:
        send_to_naver(data, status_path=status)
    except Exception as e:
        _log(status, f"❌ 오류: {e}")
    finally:
        try:
            os.remove(payload_path)
        except Exception:
            pass


# ---------------------------------------------------------------------------
# 3. 스마트에디터 '고유 서식' HTML (인용구·목록·표·정렬·글자크기 포함)
#    스마트에디터가 자기 글을 복사할 때 쓰는 구조(se-component)와 같은 형태로 만들어,
#    붙여넣었을 때 인용구·포스트잇·말풍선 등이 에디터 요소로 복원되도록 합니다.
#    기본값은 사용자의 실제 발행 글(국가유산청 여권신청 글) 서식을 따른 것입니다.
# ---------------------------------------------------------------------------

SE_DEFAULT_STYLE = {
    "align": "",                 # "" = 왼쪽(기본), "center" = 가운데
    "font_size": 16,             # 본문 글자 크기
    "line_height": "2.1",        # 줄간격
    "h2_quote": "default",       # 소제목 인용구: default(따옴표)
    "h3_quote": "quotation_line",      # 작은 소제목: 버티컬 라인
    "summary_quote": "quotation_bubble",  # 핵심 요약: 말풍선
    "toc_quote": "quotation_postit",   # 목차: 포스트잇
    "faq_quote": "default",      # FAQ 제목: 따옴표
    "link_color": "#ff0010",     # 공식 링크 강조 문구 색
    "table_header_bg": "#fff8b2",
    "image_placeholders": True,  # 이미지 넣을 자리 표시
    "faq_position": "end",       # "end" = 글 맨 끝(사용자 글 기준), "before_summary" = 마지막 정리 앞
}

ZWSP = "\u200b"


def _se_inline(text):
    out, pos = [], 0
    for m in re.finditer(r"\*\*(.+?)\*\*|\[([^\]]+)\]\((https?://[^)\s]+)\)", text or ""):
        out.append(html.escape(text[pos:m.start()]))
        if m.group(1) is not None:
            out.append(f"<b>{html.escape(m.group(1))}</b>")
        else:
            out.append(f'<a href="{html.escape(m.group(3))}" target="_blank">{html.escape(m.group(2))}</a>')
        pos = m.end()
    out.append(html.escape((text or "")[pos:]))
    return "".join(out) or ZWSP


def _se_p(inner_html, st, align=None, fs=None, color=None, bold=False, line_height=True):
    a = st["align"] if align is None else align
    size = st["font_size"] if fs is None else fs
    lh = f'line-height:{st["line_height"]};' if line_height else ""
    style = f"color:{color};" if color else ""
    content = f"<b>{inner_html}</b>" if bold else inner_html
    return (f'<p class="se-text-paragraph se-text-paragraph-align-{a} " style="{lh}">'
            f'<span style="{style}" class="{("se-fs-fs" + str(size)) if size else "se-fs-"} se-ff- ">{content}</span></p>')


def _se_text_component(paragraphs_html):
    return ('<div class="se-component se-text se-l-default"><div class="se-component-content">'
            '<div class="se-section se-section-text se-l-default"><div class="se-module se-module-text">'
            + "".join(paragraphs_html) + '</div></div></div></div>')


def _se_quote_component(lines_html, style_name):
    return (f'<div class="se-component se-quotation se-l-{style_name}"><div class="se-component-content">'
            f'<div class="se-section se-section-quotation se-l-{style_name}"><blockquote class="se-quotation-container">'
            '<div class="se-module se-module-text se-quote">' + "".join(lines_html) + '</div></blockquote></div></div></div>')


def _se_list_html(items, st, ordered=False):
    kind = "number" if ordered else "bullet-disc"
    tag = "ol" if ordered else "ul"
    lis = "".join(f'<li class="se-text-list-item">{_se_p(_se_inline(x), st)}</li>' for x in items)
    return f'<{tag} class="se-text-list se-text-list-type-{kind}">{lis}</{tag}>'


def _se_table_component(rows, st):
    trs = []
    ncol = max(len(r) for r in rows) if rows else 1
    width = round(100 / ncol, 2)
    for ri, r in enumerate(rows):
        tds = []
        for c in r:
            bg = f"background-color:{st['table_header_bg']};" if ri == 0 else ""
            cell = _se_inline(c)
            if ri == 0:
                cell = f"<b>{cell}</b>"
            tds.append(
                f'<td class="se-cell" colspan="1" rowspan="1" style="width: {width}%; height: 40.0px; '
                f'border:1px solid rgba(0, 0, 0, 0.1);{bg}"><div class="se-module se-module-text">'
                f'<p class="se-text-paragraph se-text-paragraph-align-center " style="">'
                f'<span style="" class="se-fs-fs{st["font_size"]} se-ff- ">{cell}</span></p></div></td>'
            )
        trs.append('<tr class="se-tr">' + "".join(tds) + "</tr>")
    return ('<div class="se-component se-table se-l-default"><div class="se-component-content">'
            '<div class="se-section se-section-table se-l-default"><div class="se-table-container">'
            '<table class="se-table-content" style="border:none;border-collapse:collapse;"><tbody>'
            + "".join(trs) + '</tbody></table></div></div></div></div>')


def build_se_markup(article, style=None):
    """글 결과를 스마트에디터 고유 서식 HTML로 변환합니다."""
    st = dict(SE_DEFAULT_STYLE)
    st.update(style or {})
    body = article.get("body_markdown", "") or ""

    # 공식 링크 줄 삽입(관련 문장 바로 아래)
    for link in sorted(article.get("inline_official_links", []) or [],
                       key=lambda x: len(str(x.get("insert_after", ""))), reverse=True):
        anchor = str(link.get("insert_after", "")).strip()
        url = str(link.get("url", "")).strip()
        label = str(link.get("label", "확인하기")).strip() or "확인하기"
        if not anchor or not url:
            continue
        idx = body.find(anchor)
        if idx < 0:
            continue
        line_end = body.find("\n", idx + len(anchor))
        if line_end < 0:
            line_end = len(body)
        body = body[:line_end] + f"\n\n@@LINK@@{label}@@{url}\n" + body[line_end:]

    lines = body.split("\n")
    comps = []          # 완성된 컴포넌트 HTML 목록
    paras = []          # 현재 모으는 본문 문단들

    def flush():
        if paras:
            paras.append(_se_p(ZWSP, st))   # 다음 인용구·표 앞 여백
            comps.append(_se_text_component(paras[:]))
            paras.clear()

    def add_para(html_p):
        if paras:
            paras.append(_se_p(ZWSP, st))   # 문단 사이 빈 줄
        paras.append(html_p)

    def image_slot(label="📷 이미지 넣을 자리"):
        if st.get("image_placeholders"):
            flush()
            comps.append(_se_text_component([_se_p(html.escape(label), st, align="center", color="#999999")]))

    image_slot("📷 대표 이미지(썸네일) 넣을 자리")

    i = 0
    while i < len(lines):
        raw = lines[i].rstrip()
        s = raw.strip()
        if not s:
            i += 1
            continue

        # 목차 → 포스트잇 인용구
        if re.match(r"^#{1,3}\s*목차\s*$", s):
            flush()
            items = []
            i += 1
            while i < len(lines) and (not lines[i].strip() or re.match(r"^\s*(\d+\.|[-*])\s+", lines[i])):
                if lines[i].strip():
                    items.append(re.sub(r"^\s*[-*]\s+", "", lines[i].strip()))
                i += 1
            q = [_se_p("목차", st, align="", line_height=False)] + [_se_p(_se_inline(x), st, align="") for x in items]
            comps.append(_se_quote_component(q, st["toc_quote"]))
            continue

        # 핵심 요약 → 말풍선 인용구 + 글머리 목록
        if re.match(r"^\**\s*📌?\s*핵심\s*요약\s*\**$", s):
            flush()
            items = []
            i += 1
            while i < len(lines) and (not lines[i].strip() or re.match(r"^\s*[-*]\s+", lines[i])):
                if lines[i].strip():
                    items.append(re.sub(r"^\s*[-*]\s+", "", lines[i].strip()))
                i += 1
            comps.append(_se_quote_component([_se_p("핵심 요약", st, align="", fs="", line_height=False)], st["summary_quote"]))
            if items:
                comps.append(_se_text_component([_se_list_html(items, st), _se_p(ZWSP, st)]))
            continue

        # 소제목
        m = re.match(r"^(#{1,6})\s+(.*)$", s)
        if m:
            flush()
            level = len(m.group(1))
            text = m.group(2).strip()
            style_name = st["h2_quote"] if level <= 2 else st["h3_quote"]
            comps.append(_se_quote_component([_se_p(_se_inline(text), st, align="", fs="", line_height=False)], style_name))
            if level <= 2:
                image_slot()
            i += 1
            continue

        # 공식 링크
        if s.startswith("@@LINK@@"):
            _, label, url = s.split("@@", 3)[1:4] if s.count("@@") >= 3 else ("", "확인하기", "")
            if paras:
                paras.append(_se_p(ZWSP, st))
            paras.append(_se_p(html.escape(f"[{label}]"), st, align="center", color=st["link_color"], bold=True))
            paras.append(_se_p(f'<a href="{html.escape(url)}" target="_blank">{html.escape(url)}</a>', st, align="center"))
            i += 1
            continue

        # 표
        if s.startswith("|"):
            rows = []
            while i < len(lines) and lines[i].strip().startswith("|"):
                cells = [c.strip() for c in lines[i].strip().strip("|").split("|")]
                if not all(re.fullmatch(r":?-{2,}:?", c or "") for c in cells):
                    rows.append(cells)
                i += 1
            if rows:
                flush()
                comps.append(_se_table_component(rows, st))
            continue

        # 목록
        if re.match(r"^[-*]\s+", s):
            items = []
            while i < len(lines) and re.match(r"^\s*[-*]\s+", lines[i]):
                items.append(re.sub(r"^\s*[-*]\s+", "", lines[i]).strip())
                i += 1
            if paras:
                paras.append(_se_p(ZWSP, st))
            paras.append(_se_list_html(items, st))
            continue

        # 인용(>) → 라인&따옴표 인용구
        if s.startswith(">"):
            flush()
            q = []
            while i < len(lines) and lines[i].strip().startswith(">"):
                q.append(_se_p(_se_inline(lines[i].strip().lstrip(">").strip()), st, align="", line_height=False))
                i += 1
            comps.append(_se_quote_component(q, "quotation_underline"))
            continue

        add_para(_se_p(_se_inline(s), st))
        i += 1
    flush()

    # FAQ
    faq_items = [x for x in (article.get("faq", []) or []) if str(x.get("question", "")).strip()]
    if faq_items:
        faq_comps = [_se_quote_component([_se_p("FAQ", st, align="", fs="", line_height=False)], st["faq_quote"])]
        ps = [_se_p(ZWSP, st)]
        for item in faq_items:
            ps.append(_se_p(html.escape("Q. " + str(item.get("question", "")).strip()), st, bold=True))
            ps.append(_se_p(_se_inline("A. " + str(item.get("answer", "")).strip()), st))
            ps.append(_se_p(ZWSP, st))
        faq_comps.append(_se_text_component(ps))
        if st.get("faq_position") == "before_summary":
            # 마지막 '정리/마무리' 인용구 앞에 넣습니다.
            idx = None
            for k in range(len(comps) - 1, -1, -1):
                if "se-quotation" in comps[k] and re.search(r"정리|마무리|결론", re.sub(r"<[^>]+>", "", comps[k])):
                    idx = k
                    break
            comps = comps[:idx] + faq_comps + comps[idx:] if idx is not None else comps + faq_comps
        else:
            comps += faq_comps

    # 태그
    tags = []
    for raw in article.get("tags", []) or []:
        t = str(raw).strip().lstrip("#").replace(",", "").replace(" ", "")
        if t and t not in tags:
            tags.append(t)
    if tags:
        comps.append(_se_text_component([_se_p(html.escape(" ".join("#" + t for t in tags)), st)]))

    return '<div class="se-main-container">' + "".join(comps) + "</div>"
