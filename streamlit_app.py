
import os
import re
from datetime import datetime
import json
import time
import base64
import hashlib
import hmac
from datetime import date, timedelta
from urllib.parse import urlparse, urljoin
from concurrent.futures import ThreadPoolExecutor
from html import unescape as html_unescape
import requests
import streamlit as st
from google import genai
from google.genai import types
from dotenv import load_dotenv

load_dotenv()

st.set_page_config(
    page_title="네이버 콘텐츠 기회 분석기 V3.1 SEO/GEO",
    page_icon="🔎",
    layout="wide",
)

CATEGORIES = ["리뷰", "맛집", "일상", "쇼핑정보", "여행정보", "핫이슈", "기타정보"]
MODEL = os.getenv("GEMINI_MODEL", "gemini-3.6-flash")
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-5.6-luna")
AI_PROVIDER_OPTIONS = ["GEMINI", "OPENAI"]
AI_PROVIDER_LABELS = {
    "GEMINI": "Gemini — 분석/제목/본문",
    "OPENAI": "OpenAI — 분석/제목/본문",
}

def clean_html(text):
    return re.sub(r"<[^>]+>", "", text or "").replace("&quot;", '"').replace("&amp;", "&").strip()

# NAVER API HUB 공통 엔드포인트
NAVER_API_HUB_BASE = "https://naverapihub.apigw.ntruss.com"
NAVER_SEARCHAD_BASE = "https://api.searchad.naver.com"

def searchad_signature(timestamp, method, uri, secret_key):
    """NAVER Search Ads API HMAC-SHA256 signature."""
    message = f"{timestamp}.{method}.{uri}"
    digest = hmac.new(
        (secret_key or "").encode("utf-8"),
        message.encode("utf-8"),
        hashlib.sha256,
    ).digest()
    return base64.b64encode(digest).decode("utf-8")

def naver_searchad_keyword_tool(keyword, access_license, secret_key, customer_id):
    """NAVER Search Ads /keywordstool.
    기준 키워드와 연관 키워드의 PC/모바일 월간 검색수, 경쟁도 등을 반환합니다.
    조회 전용이며 광고 캠페인/입찰/예산을 변경하지 않습니다.
    """
    keyword = (keyword or "").strip()
    if not keyword:
        return {"keywordList": []}
    if not access_license or not secret_key or not customer_id:
        raise ValueError("네이버 검색광고 API의 Access License Key, Secret Key, Customer ID를 모두 입력해 주세요.")

    uri = "/keywordstool"
    timestamp = str(int(time.time() * 1000))
    headers = {
        "X-Timestamp": timestamp,
        "X-API-KEY": access_license.strip(),
        "X-Customer": str(customer_id).strip(),
        "X-Signature": searchad_signature(timestamp, "GET", uri, secret_key.strip()),
        "Content-Type": "application/json; charset=UTF-8",
    }
    params = {
        "hintKeywords": keyword.replace(" ", ""),
        "includeHintKeywords": "1",
        "showDetail": "1",
    }
    r = requests.get(f"{NAVER_SEARCHAD_BASE}{uri}", headers=headers, params=params, timeout=20)
    if not r.ok:
        try:
            detail = r.json()
        except Exception:
            detail = r.text[:500]
        raise requests.HTTPError(f"네이버 검색광고 키워드 도구 실패: HTTP {r.status_code} · {detail}", response=r)
    return r.json()

def normalize_searchad_count(value):
    """검색량의 '< 10' 같은 문자열을 안전하게 표시하기 위한 함수."""
    if isinstance(value, (int, float)):
        return int(value)
    text = str(value or "").strip()
    if text.startswith("<"):
        return text
    digits = re.sub(r"[^0-9]", "", text)
    return int(digits) if digits else 0

def compact_searchad_keywords(data, limit=50):
    rows = []
    for item in (data or {}).get("keywordList", [])[:limit]:
        pc = normalize_searchad_count(item.get("monthlyPcQcCnt"))
        mobile = normalize_searchad_count(item.get("monthlyMobileQcCnt"))
        total = (pc if isinstance(pc, (int, float)) else 0) + (mobile if isinstance(mobile, (int, float)) else 0)
        rows.append({
            "keyword": item.get("relKeyword", ""),
            "pc_search": pc,
            "mobile_search": mobile,
            "total_search": total,
            "competition": item.get("compIdx", "-"),
            "pc_ctr": item.get("monthlyAvePcCtr", "-"),
            "mobile_ctr": item.get("monthlyAveMobileCtr", "-"),
            "ad_depth": item.get("plAvgDepth", "-"),
        })
    return rows


def naver_headers(client_id, client_secret):
    """NAVER API HUB 인증 헤더."""
    return {
        "X-NCP-APIGW-API-KEY-ID": (client_id or "").strip(),
        "X-NCP-APIGW-API-KEY": (client_secret or "").strip(),
        "Accept": "application/json",
    }

def _secret_value(*paths):
    """Streamlit Secrets에서 여러 형태의 키 경로를 안전하게 읽습니다.
    지원 예: st.secrets['NAVER_CLIENT_ID'], st.secrets['naver']['client_id']
    """
    try:
        for path in paths:
            cur = st.secrets
            ok = True
            for part in path.split("."):
                if part not in cur:
                    ok = False
                    break
                cur = cur[part]
            if ok and cur is not None and str(cur).strip():
                return str(cur).strip()
    except Exception:
        pass
    return ""

def _initial_credential(env_name, *secret_paths):
    # 우선순위: Streamlit Secrets → 환경변수
    return _secret_value(*secret_paths) or os.getenv(env_name, "").strip()

def _raise_naver_error(r, api_name):
    if r.ok:
        return
    try:
        body = r.json()
    except Exception:
        body = r.text[:500]
    raise requests.HTTPError(
        f"{api_name} 실패: HTTP {r.status_code} · {body}",
        response=r,
    )

def naver_search(api, query, client_id, client_secret, display=10, sort=None):
    """NAVER API HUB 검색 API 호출.

    지원 검색 엔드포인트: blog, news, webkr, kin, image
    webkr는 sort 파라미터를 보내지 않습니다.
    """
    url = f"{NAVER_API_HUB_BASE}/search/v1/{api}"
    params = {
        "query": query,
        "display": min(max(int(display), 1), 100),
        "format": "json",
    }
    if sort and api in {"blog", "news", "image", "kin"}:
        params["sort"] = sort
    if api == "image":
        params["filter"] = "all"

    r = requests.get(
        url,
        headers=naver_headers(client_id, client_secret),
        params=params,
        timeout=20,
    )
    _raise_naver_error(r, f"네이버 API HUB {api} 검색 API")
    return r.json()

def naver_trend(keyword, client_id, client_secret, days=30):
    end = date.today()
    start = end - timedelta(days=days)
    url = f"{NAVER_API_HUB_BASE}/search-trend/v1/search"
    payload = {
        "startDate": start.isoformat(),
        "endDate": end.isoformat(),
        "timeUnit": "date",
        "keywordGroups": [{"groupName": keyword, "keywords": [keyword]}],
    }
    r = requests.post(
        url,
        headers={**naver_headers(client_id, client_secret), "Content-Type": "application/json"},
        json=payload,
        timeout=20,
    )
    _raise_naver_error(r, "네이버 API HUB 검색어 트렌드 API")
    return r.json()

def naver_shopping_trend(keyword, category_code, client_id, client_secret, days=30):
    if not category_code:
        return None
    end = date.today()
    start = end - timedelta(days=days)
    url = f"{NAVER_API_HUB_BASE}/shopping/v1/category/keywords"
    payload = {
        "startDate": start.isoformat(),
        "endDate": end.isoformat(),
        "timeUnit": "date",
        "category": category_code,
        "keyword": [{"name": keyword, "param": [keyword]}],
    }
    r = requests.post(
        url,
        headers={**naver_headers(client_id, client_secret), "Content-Type": "application/json"},
        json=payload,
        timeout=20,
    )
    _raise_naver_error(r, "네이버 API HUB 쇼핑인사이트 API")
    return r.json()

def extract_blog_id(url_or_id):
    value = (url_or_id or "").strip()
    if not value:
        return ""
    if "blog.naver.com" not in value:
        return value.strip("/")
    path = urlparse(value).path.strip("/")
    return path.split("/")[0] if path else ""

def trend_summary(trend_data):
    data = []
    for result in trend_data.get("results", []):
        data = result.get("data", [])
        break
    values = [float(x.get("ratio", 0)) for x in data]
    if not values:
        return {"direction": "데이터 없음", "recent_avg": None, "previous_avg": None, "peak": None}
    recent = values[-7:] if len(values) >= 7 else values
    previous = values[-14:-7] if len(values) >= 14 else values[:-len(recent)]
    recent_avg = sum(recent) / len(recent)
    previous_avg = sum(previous) / len(previous) if previous else None
    if previous_avg is None or previous_avg == 0:
        direction = "상승" if recent_avg > 50 else "보통"
    else:
        change = (recent_avg - previous_avg) / previous_avg
        if change >= 0.15:
            direction = "상승"
        elif change <= -0.15:
            direction = "하락"
        else:
            direction = "보합"
    return {
        "direction": direction,
        "recent_avg": round(recent_avg, 2),
        "previous_avg": round(previous_avg, 2) if previous_avg is not None else None,
        "peak": round(max(values), 2),
        "points": data,
    }

def compact_results(items, fields):
    out = []
    for i, item in enumerate(items or [], 1):
        row = {"rank": i}
        for f in fields:
            row[f] = clean_html(item.get(f, ""))
        out.append(row)
    return out

def dedupe_search_items(items):
    """검색 결과를 링크 기준으로 중복 제거합니다."""
    seen = set()
    out = []
    for item in items or []:
        link = (item.get("link") or item.get("originallink") or "").strip()
        key = link or (item.get("title") or "").strip()
        if not key or key in seen:
            continue
        seen.add(key)
        out.append(item)
    return out

def current_web_searches(keyword, client_id, client_secret, commercial=False, is_travel_content=False, time_sensitive=False):
    """주제 유형별 보강 검색. 검색어를 병렬로 호출합니다.

    - 여행: 여행자 선택용 검색어
    - 신청·일정형(지원금·정책·축제·행사·프로모션): 공식/신청/일정 검색어
    - 일반 정보형: 최소한의 보강 검색만 수행
    """
    kw = keyword.strip()
    if is_travel_content:
        queries = [
            kw,
            f"{kw} 추천",
            f"{kw} 일정",
            f"{kw} 숙소" if any(x in kw for x in ("숙소", "호텔", "리조트")) else f"{kw} 추천 장소",
            f"{kw} 비교",
            f"{kw} 예약",
            f"{kw} 공식",
        ]
        if commercial:
            queries.append(f"{kw} 가격")
    elif time_sensitive:
        queries = [
            f"{kw} 공식 홈페이지",
            f"{kw} 신청 방법",
            f"{kw} 신청 일정",
            f"{kw} 대상 조건",
            f"{kw} 1차 2차",
            f"{kw} 최신",
        ]
        if any(x in kw for x in ("축제", "행사", "페스티벌", "박람회")):
            queries.append(f"{kw} 축제 공식")
        if commercial:
            queries.append(f"{kw} 할인 쿠폰")
    else:
        queries = [kw, f"{kw} 공식", f"{kw} {date.today().year}"]
        if commercial:
            queries.append(f"{kw} 가격")

    def _one(q):
        try:
            return naver_search("webkr", q, client_id, client_secret, display=10, sort="sim").get("items", [])
        except Exception:
            return []

    merged = []
    with ThreadPoolExecutor(max_workers=min(4, len(queries))) as ex:
        for items in ex.map(_one, queries):
            merged.extend(items)
    return dedupe_search_items(merged)[:40]


# 신청·일정 확인이 중요한 주제를 키워드에서 자동 감지합니다(체크박스 기본값으로만 사용).
TIME_SENSITIVE_PATTERN = re.compile(
    r"지원금|지원사업|신청|접수|모집|축제|행사|페스티벌|박람회|환급|바우처|수당|장려금|보조금|"
    r"정책|공모|청약|쿠폰|할인코드|프로모션|이벤트|회차|\d차"
)


def is_time_sensitive_keyword(keyword):
    return bool(TIME_SENSITIVE_PATTERN.search(keyword or ""))


def official_candidate_results(items):
    """공식 출처 후보를 넓게 추립니다. 최종 공식 여부는 AI가 실제 페이지 내용과 도메인을 함께 검토합니다."""
    keywords = (
        'gov.kr', 'go.kr', 'korea.kr', 'mois.go.kr', 'mcst.go.kr', 'tour.go.kr',
        'visitkorea.or.kr', 'kto.visitkorea.or.kr', 'seoul.go.kr', 'busan.go.kr',
        'incheon.go.kr', 'daejeon.go.kr', 'daegu.go.kr', 'gwangju.go.kr',
        'ulsan.go.kr', 'jeju.go.kr', 'or.kr'
    )
    out = []
    for item in items or []:
        url = (item.get('link') or item.get('originallink') or '').lower()
        if any(domain in url for domain in keywords):
            out.append(item)
    return dedupe_search_items(out)[:15]


def _extract_page_text_and_links(raw, base_url=""):
    # HTML 본문 + 접근성 속성 + 신청/접수 등 행동 링크를 범용적으로 추출합니다.
    raw_no_script = re.sub(r"<script[\s\S]*?</script>", " ", raw or "", flags=re.I)
    raw_no_script = re.sub(r"<style[\s\S]*?</style>", " ", raw_no_script, flags=re.I)
    attrs = []
    for m in re.finditer(r"""(?:alt|title|aria-label)\s*=\s*["']([^"']+)["']""", raw_no_script, flags=re.I):
        v = re.sub(r"\s+", " ", clean_html(m.group(1))).strip()
        if v: attrs.append(v)
    text = clean_html(raw_no_script)
    text = re.sub(r"\s+", " ", text).strip()
    if attrs: text = (text + " " + " ".join(attrs)).strip()

    action_terms = ("신청", "접수", "모집", "지원", "예약", "참여", "등록", "신청하기", "접수하기",
                    "지원하기", "예약하기", "참여하기", "등록하기", "사전신청", "신청 페이지",
                    "온라인 신청", "온라인 접수", "모집요강", "접수페이지")
    links=[]
    for m in re.finditer(r"""<a\b[^>]*href\s*=\s*["']([^"']+)["'][^>]*>([\s\S]*?)</a>""", raw_no_script, flags=re.I):
        href=m.group(1).strip(); anchor=re.sub(r"\s+", " ", clean_html(m.group(2))).strip()
        if not href or href.lower().startswith(("javascript:", "mailto:", "tel:", "#")): continue
        full=urljoin(base_url, href) if base_url else href
        if not full.startswith(("http://", "https://")): continue
        if any(term.lower() in (anchor+" "+href).lower() for term in action_terms): links.append({"text":anchor,"url":full})
    seen=set(); dedup=[]
    for x in links:
        if x["url"] not in seen: seen.add(x["url"]); dedup.append(x)
    return text, dedup[:12]


PAGE_TIMEOUT = 8
PAGE_HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; ContentAnalyzer/2.6)"}


def _http_get_text(url, timeout=PAGE_TIMEOUT):
    r = requests.get(url, timeout=timeout, headers=PAGE_HEADERS)
    if not r.ok:
        return None
    # EUC-KR 등 인코딩 정보가 없는 한국 사이트의 글자 깨짐을 줄입니다.
    if not r.encoding or r.encoding.lower() == "iso-8859-1":
        r.encoding = r.apparent_encoding
    return r.text


def fetch_raw_pages(urls, cache, max_workers=10):
    """여러 URL을 동시에 가져옵니다. cache(dict)에 이미 있는 URL은 다시 받지 않습니다.
    반환: {url: raw_html 또는 None}
    """
    todo = [u for u in dict.fromkeys(urls) if u and u not in cache]

    def work(u):
        try:
            return u, _http_get_text(u)
        except Exception:
            return u, None

    if todo:
        with ThreadPoolExecutor(max_workers=min(max_workers, len(todo))) as ex:
            for u, raw in ex.map(work, todo):
                cache[u] = raw
    return {u: cache.get(u) for u in urls}


def _item_url(item):
    url = (item.get("link") or item.get("originallink") or "").strip()
    return url if url.startswith(("http://", "https://")) else ""


def build_source_pages(items, cache, limit=5, text_limit=9000):
    """이미 받아온(cache) HTML로 본문·행동 링크를 정리합니다."""
    pages = []
    for item in (items or [])[:limit]:
        url = _item_url(item)
        raw = cache.get(url) if url else None
        if not raw:
            continue
        text, links = _extract_page_text_and_links(raw, url)
        if text:
            pages.append({"title": clean_html(item.get("title", "")), "url": url, "text": text[:text_limit], "action_links": links})
    return pages


def fetch_related_action_pages(pages, cache, limit=8):
    """공식/참고 페이지의 신청·접수·모집·예약 등 행동 링크를 동시에 따라가 실제 상태를 확인합니다."""
    if limit <= 0:
        return []
    candidates = []; seen = set()
    for page in pages or []:
        for link in page.get("action_links", []) or []:
            u = (link.get("url") or "").strip()
            if u and u not in seen:
                seen.add(u); candidates.append((u, link.get("text", ""), page.get("url", "")))
    candidates = candidates[:limit]
    raws = fetch_raw_pages([c[0] for c in candidates], cache)
    out = []
    for url, anchor, parent in candidates:
        raw = raws.get(url)
        if not raw:
            continue
        text, links = _extract_page_text_and_links(raw, url)
        if text:
            out.append({"title": anchor or "공식 행동 페이지", "url": url, "parent_url": parent, "text": text[:9000], "action_links": links})
    return out


def _naver_blog_mobile_url(link):
    """블로그 검색 결과 링크를 본문이 바로 들어있는 모바일 주소로 바꿉니다."""
    link = (link or "").strip()
    m = re.search(r"blog\.naver\.com/([A-Za-z0-9_\-]+)/(\d+)", link)
    if m:
        return f"https://m.blog.naver.com/{m.group(1)}/{m.group(2)}"
    m = re.search(r"blogId=([^&]+).*?logNo=(\d+)", link)
    if m:
        return f"https://m.blog.naver.com/{m.group(1)}/{m.group(2)}"
    return link


def parse_naver_blog_structure(raw, url="", title=""):
    """상위 노출 블로그 글의 구조(소제목·분량·표·이미지·FAQ 여부)를 추출합니다.
    스마트에디터 ONE(se-component) 구조를 기준으로 하고, 구형 에디터는 본문 텍스트만 사용합니다.
    """
    raw = re.sub(r"<script[\s\S]*?</script>", " ", raw or "", flags=re.I)
    raw = re.sub(r"<style[\s\S]*?</style>", " ", raw, flags=re.I)

    def _txt(s):
        return re.sub(r"\s+", " ", html_unescape(clean_html(s))).strip()

    headings, quotes, texts = [], [], []
    image_count = table_count = 0
    chunks = re.split(r'<div class="se-component ', raw)
    if len(chunks) > 1:
        for chunk in chunks[1:]:
            head = chunk[:200]
            # split 결과는 class 속성 나머지('se-text ...">')로 시작하므로 첫 '>' 이후만 본문으로 씁니다.
            body = _txt(chunk[chunk.find(">") + 1:][:8000])
            if "se-sectionTitle" in head:
                if body: headings.append(body[:60])
            elif "se-quotation" in head:
                if body: quotes.append(body[:80])
            elif "se-image" in head:
                image_count += max(1, chunk.count("<img"))
            elif "se-table" in head:
                table_count += 1
                if body: texts.append(body)
            elif "se-text" in head:
                if body: texts.append(body)
        full_text = " ".join(texts)
    else:
        full_text = _txt(raw)
        image_count = raw.count("<img")
        table_count = raw.lower().count("<table")
    return {
        "title": title,
        "url": url,
        "char_count": len(re.sub(r"\s", "", full_text)),
        "headings": headings[:15],
        "quotes_used_as_headings": quotes[:6],
        "image_count": image_count,
        "table_count": table_count,
        "has_faq": bool(re.search(r"FAQ|자주\s*묻는|Q\.", full_text)),
        "intro": full_text[:300],
        "text_excerpt": full_text[:1500],
    }


def build_top_blog_structures(blog_items, cache, limit=5):
    out = []
    for item in (blog_items or [])[:limit]:
        m_url = _naver_blog_mobile_url(item.get("link", ""))
        raw = cache.get(m_url)
        if not raw:
            continue
        info = parse_naver_blog_structure(raw, url=item.get("link", ""), title=clean_html(item.get("title", "")))
        if info["char_count"] >= 200:
            out.append(info)
    return out


def compact_analysis_input(payload):
    """AI 분석 프롬프트에 넣을 입력을 줄입니다.
    프롬프트에 따로 넣는 항목은 제외하고, 페이지 원문은 페이지당 3,000자로 자릅니다.
    """
    skip = {"extra_search_terms", "additional_search_data", "benchmark", "searchad_keyword_data"}
    out = {}
    for k, v in (payload or {}).items():
        if k in skip:
            continue
        if k in ("source_pages", "official_source_pages", "action_pages"):
            out[k] = [
                {**p, "text": _compact_text(p.get("text"), 6000 if p.get("user_provided") else 3000), "action_links": (p.get("action_links") or [])[:6]}
                for p in (v or [])
            ]
        elif k == "specific_existing_post" and isinstance(v, dict) and v.get("text"):
            out[k] = {**v, "text": _compact_text(v.get("text"), 6000)}
        else:
            out[k] = v
    return out


def compact_benchmark_for_prompt(benchmark):
    if not isinstance(benchmark, dict) or not benchmark.get("text"):
        return benchmark or {}
    if benchmark.get("source_type") == "OFFICIAL_REFERENCE":
        # 공식으로 지정된 URL의 본문은 official_source_pages에 이미 들어가므로 여기서는 요약만 보냅니다.
        return {**benchmark, "text": _compact_text(benchmark.get("text"), 1500),
                "text_note": "본문 전체는 official_source_pages의 '[입력 URL]' 항목을 참고하세요.",
                "action_links": (benchmark.get("action_links") or [])[:6], "info_links": (benchmark.get("info_links") or [])[:8]}
    return {**benchmark, "text": _compact_text(benchmark.get("text"), 4000), "action_links": (benchmark.get("action_links") or [])[:6]}


# 내 기존글 URL이 없을 때는 AI가 작성해도 코드가 덮어쓰는 필드들입니다. 요청하지 않아 시간과 토큰을 아낍니다.
EXISTING_CONTENT_FIELDS = (
    "existing_content_asset_summary", "existing_content_relevance", "existing_content_strengths",
    "existing_content_missing_or_extendable", "cannibalization_note", "recommended_source_post",
)


def analysis_schema_for(has_specific_post):
    if has_specific_post:
        return ANALYSIS_SCHEMA
    props = {k: v for k, v in ANALYSIS_SCHEMA["properties"].items() if k not in EXISTING_CONTENT_FIELDS}
    required = [k for k in ANALYSIS_SCHEMA["required"] if k not in EXISTING_CONTENT_FIELDS]
    return {"type": "OBJECT", "properties": props, "required": required}


ANALYSIS_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "search_intent": {"type": "STRING"},
        "main_keyword": {"type": "STRING"},
        "main_keyword_source": {"type": "STRING"},
        "main_keyword_evidence": {"type": "STRING"},
        "competition": {"type": "STRING"},
        "opportunity": {"type": "STRING"},
        "trend_interpretation": {"type": "STRING"},
        "related_keywords": {"type": "ARRAY", "items": {"type": "STRING"}},
        "long_tail_keywords": {"type": "ARRAY", "items": {"type": "STRING"}},
        "title_patterns": {"type": "ARRAY", "items": {"type": "STRING"}},
        "reader_questions": {"type": "ARRAY", "items": {"type": "STRING"}},
        "current_source_facts": {
            "type": "ARRAY",
            "items": {
                "type": "OBJECT",
                "properties": {
                    "fact": {"type": "STRING"},
                    "source_title": {"type": "STRING"},
                    "source_url": {"type": "STRING"},
                    "source_type": {"type": "STRING"},
                    "verified_date": {"type": "STRING"},
                    "use": {"type": "STRING"}
                },
                "required": ["fact", "source_title", "source_url", "source_type", "verified_date", "use"]
            }
        },
        "freshness_warning": {"type": "STRING"},
        "content_gaps": {"type": "ARRAY", "items": {"type": "STRING"}},
        "missing_info": {
            "type": "ARRAY",
            "items": {
                "type": "OBJECT",
                "properties": {
                    "item": {"type": "STRING"},
                    "why_needed": {"type": "STRING"},
                    "where_to_find": {"type": "STRING"}
                },
                "required": ["item", "why_needed", "where_to_find"]
            }
        },
        "home_feed_angle": {"type": "STRING"},
        "search_fit_score": {"type": "INTEGER"},
        "home_feed_fit_score": {"type": "INTEGER"},
        "recommended_content_mode": {"type": "STRING"},
        "content_mode_reason": {"type": "STRING"},
        "official_sources": {
            "type": "ARRAY",
            "items": {
                "type": "OBJECT",
                "properties": {
                    "name": {"type": "STRING"},
                    "url": {"type": "STRING"},
                    "purpose": {"type": "STRING"},
                    "verified_fact": {"type": "STRING"},
                    "source_type": {"type": "STRING"},
                    "actions": {
                        "type": "ARRAY",
                        "items": {
                            "type": "OBJECT",
                            "properties": {
                                "label": {"type": "STRING"},
                                "url": {"type": "STRING"}
                            },
                            "required": ["label", "url"]
                        }
                    }
                },
                "required": ["name", "url", "purpose", "verified_fact", "source_type", "actions"]
            }
        },
        "recommended_strategy": {"type": "STRING"},
        "strategy_reason": {"type": "STRING"},
        "recommended_outline": {"type": "ARRAY", "items": {"type": "STRING"}},
        "existing_content_asset_summary": {"type": "STRING"},
        "existing_content_relevance": {"type": "STRING"},
        "existing_content_strengths": {"type": "ARRAY", "items": {"type": "STRING"}},
        "existing_content_missing_or_extendable": {"type": "ARRAY", "items": {"type": "STRING"}},
        "current_time_extension_points": {"type": "ARRAY", "items": {"type": "STRING"}},
        "new_content_opportunities": {
            "type": "ARRAY",
            "items": {
                "type": "OBJECT",
                "properties": {
                    "topic": {"type": "STRING"},
                    "search_intent": {"type": "STRING"},
                    "keyword": {"type": "STRING"},
                    "reason": {"type": "STRING"},
                    "source_post": {"type": "STRING"}
                },
                "required": ["topic", "search_intent", "keyword", "reason", "source_post"]
            }
        },
        "recommended_new_keywords": {"type": "ARRAY", "items": {"type": "STRING"}},
        "cannibalization_note": {"type": "STRING"},
        "recommended_source_post": {"type": "STRING"},
        "travel_checkpoints": {"type": "ARRAY", "items": {"type": "STRING"}}
    },
    "required": [
        "search_intent", "main_keyword", "main_keyword_source", "main_keyword_evidence",
        "competition", "opportunity", "trend_interpretation",
        "related_keywords", "long_tail_keywords", "title_patterns", "reader_questions",
        "current_source_facts", "freshness_warning",
        "content_gaps", "missing_info", "home_feed_angle", "search_fit_score", "home_feed_fit_score",
        "recommended_content_mode", "content_mode_reason", "official_sources", "recommended_strategy",
        "strategy_reason", "recommended_outline", "existing_content_asset_summary",
        "existing_content_relevance", "existing_content_strengths",
        "existing_content_missing_or_extendable", "current_time_extension_points",
        "new_content_opportunities", "recommended_new_keywords",
        "cannibalization_note", "recommended_source_post", "travel_checkpoints"
    ],
}

ARTICLE_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "seo_title": {"type": "STRING"},
        "home_title": {"type": "STRING"},
        "content_mode": {"type": "STRING"},
        "content_mode_reason": {"type": "STRING"},
        "target_length_rule": {"type": "STRING"},
        "character_count": {"type": "INTEGER"},
        "thumbnail_text": {"type": "STRING"},
        "meta_description": {"type": "STRING"},
        "main_keyword": {"type": "STRING"},
        "secondary_keywords": {"type": "ARRAY", "items": {"type": "STRING"}},
        "long_tail_keywords": {"type": "ARRAY", "items": {"type": "STRING"}},
        "outline": {"type": "ARRAY", "items": {"type": "STRING"}},
        "toc_included": {"type": "BOOLEAN"},
        "toc_reason": {"type": "STRING"},
        "gap_coverage": {
            "type": "ARRAY",
            "items": {
                "type": "OBJECT",
                "properties": {
                    "gap": {"type": "STRING"},
                    "status": {"type": "STRING"},
                    "evidence": {"type": "STRING"}
                },
                "required": ["gap", "status", "evidence"]
            }
        },
        "body_markdown": {"type": "STRING"},
        "faq": {
            "type": "ARRAY",
            "items": {
                "type": "OBJECT",
                "properties": {
                    "question": {"type": "STRING"},
                    "answer": {"type": "STRING"},
                },
                "required": ["question", "answer"],
            },
        },
        "inline_official_links": {
            "type": "ARRAY",
            "items": {
                "type": "OBJECT",
                "properties": {
                    "link_id": {"type": "STRING"},
                    "insert_after": {"type": "STRING"},
                    "label": {"type": "STRING"},
                    "url": {"type": "STRING"},
                    "purpose": {"type": "STRING"}
                },
                "required": ["link_id", "insert_after", "label", "url", "purpose"]
            }
        },
        "official_sources": {
            "type": "ARRAY",
            "items": {
                "type": "OBJECT",
                "properties": {
                    "name": {"type": "STRING"},
                    "url": {"type": "STRING"},
                    "purpose": {"type": "STRING"},
                    "actions": {
                        "type": "ARRAY",
                        "items": {
                            "type": "OBJECT",
                            "properties": {
                                "label": {"type": "STRING"},
                                "url": {"type": "STRING"}
                            },
                            "required": ["label", "url"]
                        }
                    }
                },
                "required": ["name", "url", "purpose", "actions"]
            }
        },
        "coupang_link_needed": {"type": "BOOLEAN"},
        "coupang_link_reason": {"type": "STRING"},
        "tags": {"type": "ARRAY", "items": {"type": "STRING"}},
    },
    "required": [
        "seo_title", "home_title", "content_mode", "content_mode_reason", "target_length_rule", "character_count",
        "thumbnail_text", "meta_description",
        "main_keyword", "secondary_keywords", "long_tail_keywords",
        "outline", "toc_included", "toc_reason", "gap_coverage", "body_markdown", "faq",
        "inline_official_links", "official_sources", "coupang_link_needed", "coupang_link_reason", "tags",
    ],
}

class AIOutputError(RuntimeError):
    """출력 잘림/JSON 불완전처럼 같은 설정으로 재시도해도 해결되지 않는 오류."""


def _openai_schema(schema):
    """Gemini 스키마를 OpenAI strict JSON Schema 형식으로 변환합니다.

    OpenAI Structured Outputs의 strict 모드에서는 루트뿐 아니라 배열 items를
    포함한 모든 object에 ``additionalProperties: false``가 필요합니다.
    """
    if isinstance(schema, dict):
        out = {}
        for key, value in schema.items():
            if key == "type" and isinstance(value, str):
                out[key] = value.lower()
            else:
                out[key] = _openai_schema(value)
        if out.get("type") == "object":
            # Gemini 스키마에는 이 제약이 없지만 OpenAI strict Structured
            # Outputs에서는 중첩 object까지 명시해야 합니다.
            out["additionalProperties"] = False
            # 이 앱의 출력 객체는 모든 필드를 소비하므로, strict 모드가 요구하는
            # required 배열이 누락된 경우에도 스키마의 모든 속성을 요구합니다.
            if "properties" in out and "required" not in out:
                out["required"] = list(out["properties"])
        return out
    if isinstance(schema, list):
        return [_openai_schema(x) for x in schema]
    return schema

def openai_json(api_key, model, prompt, schema, max_tokens=8000, retries=2, thinking="low"):
    """OpenAI Responses API를 이용한 구조화 JSON 생성."""
    if not api_key:
        raise RuntimeError("OpenAI API Key가 설정되지 않았습니다.")
    payload = {
        "model": model,
        "input": prompt,
        "max_output_tokens": max_tokens,
        "text": {
            "format": {
                "type": "json_schema",
                "name": "naver_blog_content",
                "strict": True,
                "schema": _openai_schema(schema),
            }
        },
    }
    # 추론 모델은 추론 토큰도 max_output_tokens에 포함되므로 추론 강도를 제한합니다.
    if thinking and str(model or "").lower().startswith(("gpt-5", "o1", "o3", "o4")):
        payload["reasoning"] = {"effort": thinking}
    last_error = None
    for attempt in range(retries):
        try:
            response = requests.post(
                "https://api.openai.com/v1/responses",
                headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                json=payload,
                timeout=180,
            )
            if not response.ok:
                detail = response.text[:1200]
                if response.status_code in (429, 500, 502, 503, 504) and attempt < retries - 1:
                    time.sleep(2 + attempt * 2)
                    continue
                raise RuntimeError(f"OpenAI API 오류 HTTP {response.status_code}: {detail}")
            data = response.json()
            if data.get("status") == "incomplete":
                reason = (data.get("incomplete_details") or {}).get("reason", "unknown")
                raise AIOutputError(
                    f"OpenAI 응답이 완성되지 않았습니다(사유: {reason}). "
                    f"출력 토큰 한도({max_tokens:,})를 늘려야 할 수 있습니다."
                )
            texts = []
            for item in data.get("output", []) or []:
                for content in item.get("content", []) or []:
                    if content.get("type") == "output_text" and content.get("text"):
                        texts.append(content["text"])
            text = "".join(texts).strip()
            if not text:
                raise RuntimeError("OpenAI 응답에서 JSON 텍스트를 찾지 못했습니다.")
            return json.loads(text)
        except AIOutputError:
            raise
        except Exception as e:
            last_error = e
            if attempt < retries - 1 and ("429" in str(e) or "500" in str(e) or "503" in str(e)):
                time.sleep(2 + attempt * 2)
                continue
            raise
    raise RuntimeError(f"OpenAI 응답 처리 실패: {last_error}")

def ai_json(client, prompt, schema, max_tokens=8000, thinking="low"):
    """
    AI 공급자 선택부입니다. 사이드바에서 선택한 AI가 분석·제목·본문을 모두 작성합니다.
    - GEMINI: 전부 Gemini로 작성합니다. OpenAI 키가 함께 입력돼 있으면
      Gemini 일일 quota 초과가 실제로 발생한 경우에만 OpenAI로 대체합니다.
    - OPENAI: 처음부터 전부 OpenAI로 작성합니다.
    """
    mode = client.get("provider_mode", "GEMINI")

    if mode == "OPENAI":
        return openai_json(
            client.get("openai_key", ""),
            client.get("openai_model", OPENAI_MODEL),
            prompt,
            schema,
            max_tokens=max_tokens,
            thinking=thinking,
        )

    # GEMINI: 분석·제목·본문 모두 Gemini로 작성합니다.
    gemini_client = genai.Client(api_key=client.get("gemini_key", ""))
    try:
        return gemini_json(
            gemini_client,
            prompt,
            schema,
            max_tokens=max_tokens,
            model=client.get("gemini_model", MODEL),
            thinking=thinking,
        )
    except RuntimeError as e:
        # Gemini 일일 quota가 실제로 초과된 경우에만 OpenAI를 대체 호출합니다.
        # 일반 오류/JSON 오류/일시적 서버 오류에는 OpenAI를 사용하지 않습니다.
        message = str(e).upper()
        quota_exceeded = (
            "GEMINI API 일일 요청 한도를 초과했습니다" in str(e)
            or "GENERATE_CONTENT_FREETIER_REQUESTS" in message
            or "GENERATEREQUESTSPERDAYPERPROJECTPERMODEL" in message
            or "GENERATE_REQUESTS_PER_DAY" in message
            or "PERDAYPERPROJECTPERMODEL" in message
        )
        if quota_exceeded and client.get("openai_key", ""):
            return openai_json(
                client.get("openai_key", ""),
                client.get("openai_model", OPENAI_MODEL),
                prompt,
                schema,
                max_tokens=max_tokens,
                thinking=thinking,
            )
        raise

def _gemini_thinking_config(model, level):
    """모델 계열에 맞는 thinking 설정을 만듭니다.

    thinking 토큰은 max_output_tokens에 포함되므로, 제한하지 않으면
    실제 JSON 출력 공간이 부족해 응답이 중간에 잘립니다.
    SDK가 구버전이라 해당 필드를 지원하지 않으면 None을 반환합니다.
    """
    if not level:
        return None
    m = (model or "").lower()
    try:
        if "gemini-3" in m:
            return types.ThinkingConfig(thinking_level=level)
        if "2.5" in m:
            budget = {"minimal": 512, "low": 1024, "medium": 4096, "high": 8192}.get(level, 1024)
            return types.ThinkingConfig(thinking_budget=budget)
    except Exception:
        return None
    return None


def gemini_json(client, prompt, schema, max_tokens=8000, retries=3, model=MODEL, thinking="low"):
    """Gemini 호출.

    - thinking 토큰을 제한해 출력 공간을 확보합니다.
    - finish_reason이 MAX_TOKENS면 재시도하지 않고 바로 원인을 알려줍니다
      (같은 설정으로 재시도하면 quota만 소모됩니다).
    - 일시적 429/503만 제한적으로 재시도하고, 일일 quota 초과는 즉시 중단합니다.
    """
    import random

    thinking_cfg = _gemini_thinking_config(model, thinking)
    last_error = None
    for attempt in range(retries):
        try:
            cfg_kwargs = dict(
                max_output_tokens=max_tokens,
                response_mime_type="application/json",
                response_schema=schema,
            )
            if thinking_cfg is not None:
                cfg_kwargs["thinking_config"] = thinking_cfg
            resp = client.models.generate_content(
                model=model,
                contents=prompt,
                config=types.GenerateContentConfig(**cfg_kwargs),
            )

            cand = resp.candidates[0] if getattr(resp, "candidates", None) else None
            finish = str(getattr(cand, "finish_reason", "") or "").upper()
            um = getattr(resp, "usage_metadata", None)
            thoughts = getattr(um, "thoughts_token_count", 0) or 0
            outputs = getattr(um, "candidates_token_count", 0) or 0
            if "MAX_TOKENS" in finish:
                raise AIOutputError(
                    "Gemini 출력이 토큰 한도에 걸려 중간에 잘렸습니다. "
                    f"(생각 토큰 {thoughts:,} / 출력 토큰 {outputs:,} / 한도 {max_tokens:,}) "
                    "같은 설정으로 다시 시도해도 결과가 같으므로 재시도하지 않았습니다."
                )

            text = (resp.text or "").strip()
            if not text:
                raise ValueError(f"Gemini 응답이 비어 있습니다. (finish_reason: {finish or '알 수 없음'})")
            if text.startswith("```json") and text.endswith("```"):
                text = text[7:-3].strip()
            elif text.startswith("```") and text.endswith("```"):
                text = text[3:-3].strip()
            try:
                return json.loads(text)
            except json.JSONDecodeError as json_error:
                # 잘림(MAX_TOKENS)이 아닌데 JSON이 깨진 경우만 재시도합니다.
                last_error = json_error
                if attempt < retries - 1:
                    time.sleep(1 + attempt)
                    continue
                raise AIOutputError(
                    "Gemini 응답 JSON 형식이 올바르지 않습니다. "
                    f"(finish_reason: {finish or '알 수 없음'}, 생각 토큰 {thoughts:,} / 출력 토큰 {outputs:,}) "
                    f"원본 오류: {json_error}"
                ) from json_error
        except AIOutputError:
            raise
        except Exception as e:
            last_error = e
            message = str(e)
            upper = message.upper()

            # 모델/SDK가 thinking 설정을 거부하면 thinking 설정 없이 한 번 더 시도합니다.
            if thinking_cfg is not None and "THINKING" in upper and ("400" in upper or "INVALID" in upper):
                thinking_cfg = None
                continue

            daily_quota = (
                "GENERATE_CONTENT_FREETIER_REQUESTS" in upper
                or "GENERATEREQUESTSPERDAYPERPROJECTPERMODEL" in upper
                or "GENERATE_REQUESTS_PER_DAY" in upper
                or "PERDAYPERPROJECTPERMODEL" in upper
            )
            if daily_quota:
                raise RuntimeError(
                    "Gemini API 일일 요청 한도를 초과했습니다. "
                    "현재 프로젝트의 일일 quota가 초기화되거나 유료 Tier/할당량이 적용되기 전에는 "
                    "같은 요청을 반복해도 해결되지 않습니다. Google AI Studio의 사용량/Rate limits를 확인해 주세요. "
                    f"원본 오류: {message}"
                )

            transient_429 = "429" in upper or "RESOURCE_EXHAUSTED" in upper
            transient_503 = "503" in upper or "UNAVAILABLE" in upper or "HIGH DEMAND" in upper
            if not (transient_429 or transient_503):
                raise

            if attempt < retries - 1:
                delay = None
                m = re.search(r"retry(?:delay| in)\D{0,20}(\d+(?:\.\d+)?)\s*s", message, re.I)
                if m:
                    try:
                        delay = float(m.group(1))
                    except Exception:
                        delay = None
                if delay is None:
                    delay = min(30, 2 ** attempt * 2)
                delay += random.uniform(0, 1.0)
                time.sleep(delay)

    raise RuntimeError(
        f"Gemini가 일시적으로 응답하지 않습니다. 잠시 후 다시 시도해 주세요. 원본 오류: {last_error}"
    )


def analyze_with_ai(client, payload):
    prompt = f"""
당신은 네이버 블로그 콘텐츠 전략가이자 '최신 정보 검증' 편집자입니다.
현재 키워드의 검색 의도뿐 아니라, 글에 넣을 수치·기간·할인율·프로모션·코드·일정처럼 시간이 지나면 바뀌는 정보를 반드시 검증해야 합니다.

핵심 원칙:
1. 사용자가 입력한 키워드는 Creator Advisor에서 이미 선별한 '원본 키워드'입니다. 이 키워드를 분석의 출발점이자 주제의 중심으로 취급하세요.
2. 원본 키워드 자체의 네이버 검색 데이터(블로그·검색트렌드·뉴스·웹문서·지식iN)를 먼저 분석하세요.
2-1. 사용자가 입력한 '추가 검색 표현'이 있다면 각각의 실제 네이버 검색 결과를 별도로 확인하고, 원본 키워드와 같은 주제를 가리키는 표현인지 판단하세요. 이 표현들은 오타·유사명칭·띄어쓰기·통용명칭일 수 있으므로 버리지 말고 검색 유입을 고려한 보조 표현으로 관리하세요.
2-2. 추가 검색 표현이 실제로 같은 대상을 가리킨다는 근거가 있으면 본문에서 자연스럽게 설명하거나 관련 표현으로 사용할 수 있습니다. 단, 의미가 다른 표현은 억지로 포함하지 마세요.
2-3. 추가 검색 표현은 원칙적으로 SEO 메인키워드를 바꾸는 용도가 아닙니다. 사용자가 입력한 원본 키워드를 메인키워드로 유지하고, 실제 검색 맥락을 넓히는 보조 표현으로 활용하세요.
2. 그 결과에서 실제로 확인되는 연관 검색어·검색 표현을 찾아 메인키워드를 결정하세요.
3. 메인키워드는 원칙적으로 사용자가 입력한 원본 키워드를 그대로 사용하세요. 단, 네이버 검색 데이터에서 띄어쓰기/표현 차이가 명확하게 확인되고 그 표현이 실제 검색에 더 적합하다고 판단되는 경우에만 연관 검색어를 메인키워드로 선택하세요. 근거 없이 새로운 키워드를 만들거나 다른 주제로 바꾸지 마세요.
4. 반환하는 main_keyword는 이후 제목과 본문 전체에서 사용할 '고정 SEO 메인키워드'입니다. 제목 생성 단계에서 다시 임의로 바꾸지 마세요.
5. 시의성이 있는 키워드는 '현재 웹검색 보강 결과'와 실제 페이지 확인 결과를 최우선으로 참고하세요.
6. 공식 사이트/공식 브랜드 페이지가 확인되면 제3자 블로그보다 우선하세요.
7. 현재 확인되지 않는 할인율, 프로모션 기간, 할인코드, 카드사 제휴, 가격, 이벤트명은 절대 추측해서 쓰지 마세요.
8. 검색 결과 제목만 보고 사실을 확정하지 말고, 제공된 source_pages/current_source_facts에서 근거가 있는 내용만 현재 사실로 취급하세요.
9. 근거가 부족하면 '현재 확인 필요'로 표시하고 글에 단정적으로 넣지 마세요.
10. '2026년 9월'처럼 날짜가 중요한 제목은 현재 기준일과 실제 확인된 기간이 맞는 경우에만 사용하세요.
8. 검색형과 홈판형의 적합도를 각각 0~100으로 평가하세요. 이 점수는 사용자에게 선택권을 주기 위한 참고값이며, AI가 작성 유형을 자동 선택해서는 안 됩니다.
9. 검색형은 정보 정확성과 검색 의도 충족을 최우선으로 하고, 홈판형은 클릭을 유도하는 제목·첫 문장 흐름을 최우선으로 하세요.
10. 추천 작성 유형을 계산하더라도 UI에서 자동 선택하거나 글 작성 유형으로 확정하지 마세요.
11. 지원금·정부정책·공공정보·축제·국내여행 등 공식 확인이 중요한 키워드는 공식 홈페이지/공공기관 페이지 후보를 우선 검토하고, 실제 확인 가능한 URL과 그 페이지에서 가져온 핵심 사실을 official_sources에 남기세요.
12. official_sources의 actions에는 실제 공식 페이지에서 확인된 경우에만 '신청하기', '자격 조회하기', '예약하기', '일정 확인하기', '내용 확인하기' 등의 짧은 버튼명을 붙이고 해당 공식 URL을 넣으세요. 별도의 신청/조회/예약 URL을 확인하지 못했다면 임의로 만들지 말고 actions를 빈 배열로 두세요.
13. 공식 페이지의 대표 URL과 신청/조회/예약용 URL이 다르면 각각 구분하세요. URL은 반드시 제공된 검색 결과·실제 확인 페이지에서 확인된 주소만 사용하세요.
13-1. 공식 페이지에서 현재 상태가 바뀐 사실(예: 1차 마감, 2차 일정, 추가 접수, 변경된 조건, 현재 운영기간 등)을 발견했다면 current_source_facts와 current_time_extension_points에 명시하세요. 단순히 '공식 페이지가 있다'로 끝내지 말고, 현재 검색자가 기존 콘텐츠에서 놓치기 쉬운 최신 정보를 찾아내세요.
13-2. 공식 페이지에서 발견한 정보가 기존 검색 결과의 일반적인 설명보다 더 최신이거나 구체적이라면 그 차이를 opportunity/content_gaps/new_content_opportunities에 반영하세요.
13-3. 공식 페이지의 사실은 글에 사용할 수 있지만, 참고/벤치마크 URL의 사실은 공식 또는 다른 신뢰 가능한 근거로 교차 확인되지 않았다면 현재 사실로 확정하지 마세요.
12. 쿠팡파트너스 링크는 제품 추천/구매 의도가 실제로 있는 경우에만 필요 여부를 판단하고, 최대 1개 선택사항으로만 표시하세요. 애드센스 유도용 외부 링크는 제안하지 마세요.

홈판 제목 공식:
- 반전, 숫자, 의외성, 상황, 경험, 궁금증을 조합해 클릭 이유를 만드세요.
- '직접 해보니', '알고 보니', '의외로', '결국', '생각보다', '다시 한다면' 같은 표현은 주제에 자연스럽게 맞을 때 활용하세요.
- 과장, 허위, 확인되지 않은 수치·기간·효과를 만들지 마세요.
- 홈판형 제목 3개를 만들 때 가능하면 서로 다른 클릭 장치를 사용하세요: 반전형 / 숫자형 / 의외성·경험형.

참고/벤치마크 URL 분석 원칙:
- benchmark가 입력되면 URL의 종류가 블로그인지 공식 페이지인지 뉴스인지 일반 웹페이지인지 먼저 구분하세요.
- 블로그/뉴스/일반 웹페이지는 제목 구성, 목차, 정보 배열, 독자 질문, 빠진 내용, 차별화 포인트를 분석하는 참고자료로 사용하세요. 그대로 베끼거나 문장을 재현하지 마세요.
- 공식 홈페이지/공공기관 페이지라면 단순 벤치마크가 아니라 최신 사실의 보강 후보로 보고, 제공된 official_source_pages와 함께 대조하세요. 공식 페이지에서 확인된 핵심 사실은 current_source_facts와 official_sources에 반영하세요.
- 참고 URL 하나가 제공됐다고 해서 그 페이지의 모든 주장을 사실로 확정하지 마세요. 공식 근거가 필요한 주제는 공식 페이지를 우선합니다.
- 단, benchmark.source_type이 OFFICIAL_REFERENCE이면 사용자가 공식 홈페이지로 지정했거나 공공 도메인입니다. 이 페이지와 official_source_pages 중 user_provided가 true인 하위 페이지(일정·프로그램·교통·주차 등)의 내용은 공식 사실로 사용하고, 확인된 세부 정보(프로그램·시간·장소·요금·주차·셔틀 등)를 current_source_facts에 빠짐없이 기록하세요. 여기에 없는 정보만 '확인 필요'로 표시하세요.
- 참고 URL에서 현재 글에 추가할 가치가 있는 정보가 발견되면 content_gaps, current_time_extension_points, new_content_opportunities에 구체적으로 기록하세요.

기존 콘텐츠 자산 원칙:
- 내 블로그 주소/ID는 이 앱에서 내 블로그 전체를 자동 검색하기 위한 값으로 사용하지 않습니다.
- '특정 기존글 URL(선택)'이 입력된 경우에만 그 글을 내 콘텐츠 자산으로 직접 읽고 비교하세요.
- 특정 기존글 URL이 입력되지 않았다면 기존 글 자산을 추정하거나 자동 검색했다고 가정하지 마세요. 이 경우 추천 전략은 반드시 NEW_KEYWORD입니다.
- 벤치마크 블로그 URL은 내 기존글 URL과 완전히 별개의 입력입니다. 경쟁/참고 콘텐츠 분석용입니다.

전략 판단 규칙:
- 기존글 URL이 없으면: NEW_KEYWORD
- 기존글 URL이 있으면 UPDATE_EXISTING / NEW_DERIVED / NEW_UNRELATED / NO_OPPORTUNITY 중 판단
- 시간 경과에 따른 새 가치(실제 사용 후 평가, 재구매, 현재 추천 기준 등)를 검토하되 입력에 없는 경험은 만들지 마세요.

키워드 산출 규칙:
- main_keyword: 사용자가 입력한 keyword 또는 실제 네이버 검색 데이터에서 확인된 그 키워드의 연관 검색어 중 하나만 선택하세요.
- main_keyword_source: 반드시 'INPUT_KEYWORD' 또는 'RELATED_SEARCH' 중 하나로 반환하세요.
- INPUT_KEYWORD를 선택했다면 main_keyword는 입력 keyword와 동일해야 합니다.
- RELATED_SEARCH를 선택했다면 related_keywords 또는 실제 검색 결과에서 확인 가능한 표현과 동일해야 하며, 왜 선택했는지 main_keyword_evidence에 근거를 적으세요.
- related_keywords에는 원본 키워드에서 실제로 파생된 검색 표현을 우선 넣으세요. 단순히 주제가 비슷한 일반 명사를 임의로 넣지 마세요.
- 메인키워드를 결정한 뒤에는 제목과 본문에서 이 값을 변경하지 않는 것을 전제로 분석하세요.

제목 생성 규칙:
- 추천 제목은 정확히 3개를 제시하세요.
- 세 제목은 같은 내용을 말하더라도 검색의도/클릭각도를 조금씩 달리할 수 있습니다.
- 그러나 제목에서 약속한 핵심 내용이 본문에서 반드시 실제로 다뤄질 수 있어야 합니다.
- 제목에 '총정리', '최신', '2026년 9월', '할인코드', '최대 XX%', '특정 카드사' 등의 최신성/수치 표현을 넣을 경우 반드시 제공된 최신 근거가 있어야 합니다.
- 근거가 없으면 그런 표현을 제목에서 빼세요.

여행 콘텐츠 모드:
- 입력값 is_travel_content가 true이면 '실제 방문 후기'가 아닌 여행 정보·추천 콘텐츠로 분석하세요. 별도의 실제경험후기 기능은 이 모드에 적용하지 않습니다.
- 핵심 목적은 검색자가 여행 계획을 세우거나 숙소·맛집·관광지·코스 등을 실제로 선택할 수 있도록 돕는 것입니다. 검색 결과를 단순 요약하지 말고 '무엇을 고르면 되는가'까지 연결하세요.
- 먼저 검색의도를 한 문장으로 정의하세요. 예: '나트랑 3박5일 숙소를 어디에 몇 박씩 배분할지 결정하려는 검색'처럼 구체적으로 작성하세요.
- 숙소/장소/맛집 등 추천형 주제라면 검색 결과에서 실제 후보를 찾아 후보군을 만들고, 후보별 근거를 확인한 뒤 핵심 후보 3~5개를 선정하세요. 존재하지 않거나 검색 근거가 부족한 후보를 만들지 마세요.
- 후보를 선정할 때 여행자의 선택 기준 3가지를 먼저 정하세요. 이 기준은 단순 장식용 체크리스트가 아니라 후보 선정·비교·최종 추천에 실제로 적용해야 합니다. travel_checkpoints에 반환하세요.
- 추천 후보는 '왜 이 후보가 들어갔는지'가 설명되어야 합니다. 후보마다 최소한 위치/접근성, 핵심 시설·특징, 가격 또는 가격대(확인된 경우), 장점, 주의점, 추천 대상 중 주제에 맞는 항목을 조사하세요. 확인되지 않은 항목은 비워두거나 확인 필요로 표시하세요.
- 여러 후보를 다루는 경우 전체 후보 비교표를 특정 후보의 상세 섹션 안에 끼워 넣지 말고, 별도의 '한눈에 비교' 구간에서 먼저 제시하세요. 이후 각 후보 섹션에서는 해당 후보의 정보만 상세히 설명하세요.
- 추천형 여행글의 기본 논리는 '검색자의 고민 → 선택 기준 → 후보 선정 → 한눈에 비교 → 후보별 상세 → 상황별 추천 → 실제 일정/선택 방법' 순서를 우선합니다. 키워드 성격상 일부 단계가 불필요하면 생략하되, 후보를 추천하는 글이라면 후보 선정 근거와 최종 선택 기준은 반드시 남기세요.
- 여행지/숙소/맛집/쇼핑/일정 등 주제에 따라 조사 항목과 구조를 달리하세요. 숙소 글을 지역 일반론으로 바꾸거나, 맛집 글을 숙소 비교글처럼 작성하지 마세요.
- 'TOP 3'를 제목이나 본문에 사용할 경우 실제로 3개 후보를 선정하고 각각의 역할과 차이를 설명하세요. 'TOP 3'를 쓰지 않았다면 후보 수를 억지로 3개로 맞추지 마세요.
- 실제 방문 경험이 입력되지 않았다면 방문한 것처럼 추정하거나 1인칭 체험을 만들지 마세요. 여행 정보·추천형에서는 '제가 가보니', '직접 묵어보니' 같은 표현을 사용하지 마세요.

검색 노출 가능성 극대화용 콘텐츠 설계:
- 단순히 메인키워드를 반복하는 글을 만들지 말고, 검색자가 이 키워드를 입력했을 때 해결하려는 '핵심 질문'을 3~7개로 추출하세요.
- 핵심 질문은 실제 검색 결과 제목·설명·연관 검색어·뉴스·웹문서·공식자료에서 근거를 찾아 작성하세요. 임의로 일반적인 질문을 만들지 마세요.
- recommended_outline은 키워드 나열 순서가 아니라 '검색자가 정보를 찾는 순서'가 되도록 설계하세요. 가장 중요한 답을 앞쪽에 두고, 신청/예약/구매/선택처럼 행동이 필요한 주제는 행동 방법을 지나치게 뒤로 미루지 마세요.
- content_gaps는 '경쟁문서에 없는 정보'뿐 아니라 '현재 검색자가 기존 글에서 놓치기 쉬운 최신 정보', '제목은 약속하지만 본문에서 부족하기 쉬운 정보'도 포함하세요.
- 단, content_gaps에는 **수집된 자료(current_source_facts·source_pages·official_source_pages·action_pages)로 실제로 채울 수 있는 차별 정보만** 넣으세요. 각 항목은 "무엇이 부족하다"가 아니라 "우리 글이 무엇을 담을 수 있다"로 쓰세요. 예: "탈춤공원·원도심·하회마을별로 볼 수 있는 프로그램을 나눠 정리(경쟁 글은 한 곳으로만 안내)".
- 독자에게 꼭 필요하지만 수집된 자료에서 확인되지 않은 정보(예: 공연별 시간표, 주차장 위치, 셔틀 시간, 입장료)는 content_gaps에 넣지 말고 missing_info에 넣으세요. where_to_find에는 사용자가 직접 확인할 수 있는 위치를 구체적으로 적으세요(예: "공식 홈페이지 > 행사안내 > 행사일정표(이미지로 게시되는 경우가 많음)"). 추측으로 채우지 마세요.
- 검색형/혼합형에서 반드시 갖춰야 할 정보와 있으면 차별화되는 정보를 구분해서 판단하세요.
- 키워드의 검색의도와 직접 관련 없는 일반론으로 글자 수를 늘리는 전략은 사용하지 마세요.
- 제목에서 약속한 정보가 실제 본문에서 충분히 답변될 수 있도록 title promise를 분석 단계에서 검토하세요.

홈판 콘텐츠 설계:
- 홈판은 검색형 글을 짧게 줄인 형태가 아닙니다. '스크롤을 멈출 이유 → 궁금증 → 핵심 반전/정보 → 읽고 나서 얻는 이득 → 저장/공유할 이유'의 흐름으로 설계하세요.
- home_feed_angle에는 단순한 클릭 문구가 아니라 '왜 지금 이 글을 눌러야 하는지'와 '읽고 나서 무엇을 알게 되는지'를 함께 적으세요.
- 홈판형은 제목과 첫 문장의 정보가 정확히 연결되어야 하며, 낚시성 과장·근거 없는 숫자·가짜 경험을 사용하지 마세요.
- 홈판 본문은 한 문단에 한 메시지를 원칙으로 하고, 초반 20~30% 안에 핵심 궁금증 또는 예상 밖의 정보를 한 번 해소해 이탈을 줄이세요.
- 중반에는 독자가 계속 읽을 이유가 되는 추가 정보/비교/체크포인트를 배치하고, 마지막에는 '그래서 어떻게 하면 되는지' 또는 '무엇을 기억하면 되는지'를 짧게 정리하세요.

현재 회차/신청상태 우선 규칙:
- 아래 current_status는 실제 페이지에서 추출한 신청기간과 오늘 날짜를 비교한 편집 기준입니다.
- current_status.status가 ACTIVE이면 **현재 신청 중인 회차를 제목·도입부·본문 구조의 기준 회차로 우선 사용하세요.** 목차를 사용하는 글이라면 해당 회차를 목차에도 반영할 수 있습니다. 이미 끝난 과거 회차를 현재 진행 중인 것처럼 제목에 쓰지 마세요.
- current_status.status가 UPCOMING이면 **다음 신청 회차를 중심으로 작성하세요.** 직전 회차는 '지난 회차' 또는 비교 설명이 필요할 때만 짧게 언급하세요.
- current_status.status가 ENDED이면 확인된 다음 회차가 없으므로 종료 사실을 명확히 하고, 추측으로 다음 회차를 만들지 마세요.
- 제목에 '1차/2차/3차' 같은 회차를 넣을 때는 반드시 current_status와 current_source_facts/source_pages의 근거를 확인하세요.
- current_status.type이 EVENT이면 신청 회차가 아니라 축제·행사의 개최 기간입니다. UPCOMING은 '개최 예정', ACTIVE는 '진행 중', ENDED는 '종료'로 해석하고, event_period의 기간은 확인된 사실로 사용하세요.
- 사용자가 공식 홈페이지 URL을 입력한 경우에도 URL 자체를 제목의 주제로 삼지 말고, **그 페이지에서 현재 시점에 실제로 적용되는 신청 상태·일정·조건을 추출해 반영**하세요.

현재 정보 검증:
- current_source_facts는 '현재 확인된 사실' 후보입니다.
- source_pages는 실제 웹페이지에서 추출한 참고 내용입니다.
- action_pages는 공식/참고 페이지에서 신청·접수·모집·지원·예약 등의 행동 링크를 자동으로 따라가 확인한 실제 페이지입니다. 명시적인 마감/종료/신청불가 상태는 검색 결과의 오래된 정보보다 우선합니다.
- current_source_facts에는 최소한 글에 실제로 사용할 가치가 높은 사실만 넣고, source_url을 반드시 남기세요.
- 최신 정보가 부족하면 freshness_warning에 명확히 적으세요.

[실제 독자 질문과 상위 글 구조 활용]
- kin_questions(지식iN)는 실제 사람들이 이 키워드로 묻는 질문입니다. 반복되거나 중요한 질문을 골라 reader_questions에 5~8개의 자연스러운 구어체 질문 문장으로 정리하세요. 데이터에 근거가 없는 질문을 지어내지 말고, kin_questions가 비어 있으면 블로그·웹문서·연관 키워드에서 확인되는 질문으로 대신하세요. 오타·띄어쓰기·명칭 차이를 묻는 질문(예: 'A와 B는 같은 축제인가요?')은 reader_questions에서 제외하세요. 질문은 독자가 가장 많이 궁금해할 순서로 정렬하세요.
- top_blog_structures는 현재 상위 노출 블로그 글의 실제 소제목·분량·표·이미지·FAQ 여부입니다. 상위 글들이 공통으로 다루는 주제(기본으로 갖춰야 할 정보)와 아무도 제대로 다루지 않은 주제를 구분해 content_gaps와 recommended_outline에 반영하세요.
- 상위 글의 분량과 구성(표·FAQ 유무)을 근거로 competition과 opportunity를 구체적으로 적으세요.
- is_time_sensitive_topic이 false이면 회차·신청기간 검증은 필요한 경우에만 하고, 검색의도 충족과 정보 차별화에 집중하세요.

[사용자 추가 검색 표현]
{json.dumps(payload.get("extra_search_terms", []), ensure_ascii=False)}

[추가 검색 표현별 네이버 검색 데이터]
{json.dumps(payload.get("additional_search_data", []), ensure_ascii=False)}

[참고/벤치마크 URL]
{json.dumps(compact_benchmark_for_prompt(payload.get("benchmark", {})), ensure_ascii=False)}

[네이버 검색광고 키워드 도구 데이터]
{json.dumps(payload.get("searchad_keyword_data", []), ensure_ascii=False)}
- 이 데이터는 검색광고 키워드 도구의 월간 PC/모바일 검색수와 경쟁도입니다. 유기적 네이버 검색 노출량과 동일하다고 단정하지 마세요.

[입력]
{json.dumps(compact_analysis_input(payload), ensure_ascii=False)}

여행 콘텐츠 추가 산출 규칙:
- is_travel_content가 true이면 recommended_outline은 실제 작성할 H2 순서가 되도록 작성하세요. 각 항목은 서로 다른 역할을 가져야 하며 같은 후보/지역을 중복 설명하지 마세요.
- 추천형이면 outline 안에 '후보 비교'와 '후보별 상세'가 구분되어야 합니다.
- 후보별 상세 H2에는 한 후보만 대응시키고, 전체 비교표는 후보별 H2 밖의 독립 섹션으로 배치하세요.
- 추천 후보를 3개 선정했다면 outline과 current_source_facts/source_pages에 그 후보를 뒷받침할 근거가 있는지 확인하세요.
- 검색 자료만으로 특정 후보의 핵심 사실을 확인할 수 없다면 해당 후보를 확정 추천하지 말고 '추가 확인 필요'로 표시하세요.

JSON으로만 답하세요.
"""
    specific = payload.get("specific_existing_post") or {}
    has_specific = specific.get("status") not in (None, "not_provided")
    return ai_json(client, prompt, analysis_schema_for(has_specific), 20000, thinking="medium")


TITLE_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "titles": {
            "type": "ARRAY",
            "items": {
                "type": "OBJECT",
                "properties": {
                    "title": {"type": "STRING"},
                    "angle": {"type": "STRING"},
                    "why": {"type": "STRING"},
                },
                "required": ["title", "angle", "why"],
            },
        }
    },
    "required": ["titles"],
}

def generate_titles_for_mode(client, analysis_payload, mode, direct_experience_enabled=False, direct_experience_text=""):
    mode_label = {"SEARCH": "검색형", "HOME_FEED": "홈판형", "HYBRID": "혼합형"}.get(mode, mode)
    experience_status = "사용함" if direct_experience_enabled else "사용 안 함"
    experience_text = direct_experience_text.strip() if direct_experience_enabled else "제공되지 않음"
    prompt = f"""
당신은 네이버 블로그 제목 편집자입니다.
아래 분석 데이터를 바탕으로 사용자가 직접 선택한 작성 유형 '{mode_label}'에 맞는 제목 3개만 만드세요.

[작성 유형]
{mode_label}

[규칙]
- 검색형: 검색 의도와 핵심 키워드가 명확해야 하며, 공백 포함 약 28~38자를 목표로 하세요. 너무 짧아 정보 가치가 사라지지 않도록 메인키워드 + 검색 의도 + 클릭 보조 요소 1개 정도를 조합하세요.
- 홈판형: 검색형 제목을 짧게 줄이지 마세요. '상황/공감 → 의외의 결과 → 궁금증 → 얻는 정보' 중 최소 2개의 장치를 결합해 클릭 이유를 만드세요. 공백 포함 약 25~38자를 목표로 하되, 후킹을 억지로 삭제해 짧게 만들지 마세요. **메인키워드는 가능한 한 제목 앞쪽에 자연스럽게 포함**하고, 키워드 뒤에는 클릭 이유가 바로 이어지게 하세요. 제목만 보고도 무슨 주제인지 알 수 있어야 하며, 본문에서 실제로 해결할 궁금증을 제목에 심으세요.
- 혼합형: 검색 의도와 클릭성을 균형 있게 잡고 공백 포함 약 30~42자를 목표로 하세요. **메인키워드를 반드시 그대로 포함**한 뒤 가장 중요한 정보 1개 + 클릭 보조 요소 1개 정도까지만 사용하세요.
- **절대 규칙: 추천 제목 3개 모두에 [메인키워드]를 정확히 그대로 포함하세요.** 입력 키워드/메인키워드를 다른 표현으로 바꾸거나 삭제하지 마세요.
- 제목 생성 전에 메인키워드를 먼저 확정된 문자열로 인식하고, 제목 생성 과정에서 키워드 자체를 새로 선택하지 마세요.
- 추가 검색 표현은 제목에 반드시 모두 넣지 마세요. 제목의 핵심은 SEO 메인키워드이며, 추가 검색 표현은 제목 약속과 자연스럽게 맞을 때 최대 1개만 보조적으로 사용할 수 있습니다.
- 모든 유형에서 제목은 한 번에 읽히되, 지나치게 짧게 압축하지 마세요. 한 제목에 검색의도·조회·신청·지급·주의사항 등 여러 정보를 모두 나열하지 말고, 독자가 클릭할 핵심 이유 하나를 남기세요.
- 제목에 콜론(:), 슬래시(/), 쉼표를 이용해 정보를 여러 개 나열하는 방식을 피하세요. 특히 'A 및 B: C부터 D까지' 같은 긴 나열형 제목을 만들지 마세요.
- 제목에 '방법', '조회', '대상', '지급일'처럼 검색어를 넣더라도 핵심 의도에 필요한 것만 1~2개 선택하세요.
- 같은 단어와 핵심 키워드의 불필요한 반복을 피하세요.
- 확인되지 않은 최신 날짜, 할인율, 코드, 가격, 이벤트는 제목에 넣지 마세요.
- 제목에서 약속한 내용은 실제 본문으로 작성할 수 있어야 합니다.
- 회차/신청 일정이 있는 키워드는 현재 회차 상태를 최우선으로 반영하세요. 과거 회차가 검색 결과에 더 많이 보여도 현재 상태와 맞지 않으면 제목에서 선택하지 마세요.
- 정확히 3개를 반환하세요.

[독자 질문과 제목의 관계]
- 제목은 독자 질문을 모두 담는 곳이 아닙니다. 제목에는 메인키워드 + 가장 큰 궁금증 1~2개만 담고, 나머지 독자 질문은 본문과 FAQ에서 모두 답합니다. 어떤 제목을 고르더라도 본문은 reader_questions 전체를 다룬다는 전제로 제목을 만드세요.
- 제목의 궁금증은 reader_questions 앞쪽(가장 많이 묻는 질문)에서 고르세요. 질문 문장을 그대로 제목으로 쓰지 말고 "언제·어디서·얼마" 같은 핵심만 살리세요.
- 축제·행사·여행지·정책처럼 독자 궁금증이 여러 갈래인 주제라면, 3개 중 1개는 전체를 아우르는 총정리형으로 만드세요. 이때 가운뎃점(·)으로 최대 3개 항목까지 쓸 수 있어요(예: '{{메인키워드}} 2026 일정·주차·먹거리 한 번에 정리'). 나머지 2개는 서로 다른 핵심 궁금증을 앞세운 제목으로 만드세요.
- 'angle'에는 이 제목이 앞세운 궁금증을, 'why'에는 이 제목을 고르면 본문 도입부에서 무엇을 먼저 답하게 되는지 적으세요.

- 여행 콘텐츠 모드가 true이면 제목은 검색자가 해결하려는 구체적인 여행 선택 문제를 반영하세요. 핵심 키워드를 앞쪽에 두고 '추천/비교/선택/일정/숙소 조합' 중 실제 본문에서 다루는 한 가지 핵심 약속을 결합하세요.
- 추천 후보를 실제로 조사한 경우에만 'TOP 3', '추천 숙소', 특정 숙소명 등을 제목에 사용할 수 있습니다. 본문에서 실제로 다룰 수 없는 후보나 수치를 제목에 넣지 마세요.
- 여행 제목은 '필수 체크 포인트 3가지' 자체를 반복하기보다, 독자가 최종적으로 무엇을 선택할 수 있는지를 보여주는 방향을 우선하세요.
- 여행 콘텐츠의 3개 제목은 가능하면 ① 후보 추천형 ② 타겟 상황형 ③ 일정·선택 기준형으로 서로 다른 각도를 제시하세요.

[직접 경험]
- 사용 여부: {experience_status}
- 경험 내용: {experience_text}
- 직접 경험을 사용함으로 선택한 경우, 입력된 사실을 제목의 차별화 포인트로 자연스럽게 활용할 수 있습니다. 단, 모든 제목에 '직접 해보니' 같은 문구를 기계적으로 넣지 말고 제목의 약속과 잘 맞을 때만 활용하세요.
- 입력되지 않은 경험·결과·감정·수치·날짜를 지어내지 마세요.
- 직접 경험을 사용 안 함으로 선택한 경우, 개인의 방문·구매·사용 경험이 있는 것처럼 제목을 만들지 마세요.

원본 입력 키워드: {analysis_payload.get("keyword", "")}
SEO 메인키워드: {analysis_payload.get("main_keyword") or analysis_payload.get("keyword", "")}
메인키워드 선정 근거: {analysis_payload.get("main_keyword_evidence", "")}
검색 의도: {analysis_payload.get("search_intent", "")}
실제 독자 질문(많이 묻는 순): {json.dumps(analysis_payload.get("reader_questions", []), ensure_ascii=False)}
현재 회차/신청 상태: {json.dumps(analysis_payload.get("current_status", {}), ensure_ascii=False)}
콘텐츠 GAP: {json.dumps(analysis_payload.get("content_gaps", []), ensure_ascii=False)}
추가 검색 표현: {json.dumps(analysis_payload.get("extra_search_terms", []), ensure_ascii=False)}
연관 키워드: {json.dumps(analysis_payload.get("related_keywords", []), ensure_ascii=False)}
롱테일 키워드: {json.dumps(analysis_payload.get("long_tail_keywords", []), ensure_ascii=False)}
검색광고 키워드 데이터: {json.dumps(analysis_payload.get("searchad_keyword_data", []), ensure_ascii=False)}
현재 근거: {json.dumps(analysis_payload.get("current_source_facts", []), ensure_ascii=False)}

JSON으로만 답하세요.
"""
    result = ai_json(client, prompt, TITLE_SCHEMA, 4000, thinking="low")
    main_keyword = (analysis_payload.get("main_keyword") or analysis_payload.get("keyword") or "").strip()
    titles = (result.get("titles", []) or [])[:3]
    # 모델이 규칙을 어겨 메인키워드를 누락시키더라도 제목 단계에서 키워드가 사라지지 않도록 최종 방어선을 둡니다.
    if main_keyword:
        for item in titles:
            title = str(item.get("title", "")).strip()
            if title and main_keyword not in title:
                item["title"] = f"{main_keyword} {title}"
                item["why"] = (str(item.get("why", "")).strip() + " 메인키워드를 제목에 고정했습니다.").strip()
    return titles

def _compact_text(value, limit=1200):
    """작성 단계 Gemini 입력용 텍스트를 길이 제한합니다."""
    text = str(value or "").strip()
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + "…"

def build_writing_context(analysis_payload):
    """분석 단계에서 수집한 원본 데이터를 글 작성용 핵심 근거로 압축합니다.

    분석 단계에서는 충분한 원문을 수집할 수 있지만, 글 작성 단계에서
    분석 전체 JSON/원문 HTML을 다시 보내면 Gemini TPM을 불필요하게 소모합니다.
    글 작성에는 '글을 쓰는 데 필요한 사실'만 전달합니다.
    """
    a = analysis_payload or {}

    def compact_list(values, item_limit=500, max_items=12):
        out = []
        for value in (values or [])[:max_items]:
            if isinstance(value, dict):
                row = {}
                for k, v in value.items():
                    if isinstance(v, list):
                        row[k] = v[:8]
                    else:
                        row[k] = _compact_text(v, item_limit)
                out.append(row)
            else:
                out.append(_compact_text(value, item_limit))
        return out

    source_pages = []
    for page in (a.get("source_pages", []) or [])[:6]:
        source_pages.append({
            "title": _compact_text(page.get("title"), 180),
            "url": _compact_text(page.get("url"), 500),
            "text": _compact_text(page.get("text"), 2200),
            "action_links": [
                {
                    "text": _compact_text(x.get("text"), 120),
                    "url": _compact_text(x.get("url"), 500),
                }
                for x in (page.get("action_links", []) or [])[:5]
            ],
        })

    official_sources = []
    for src in (a.get("official_sources", []) or [])[:10]:
        official_sources.append({
            "name": _compact_text(src.get("name"), 180),
            "url": _compact_text(src.get("url"), 500),
            "purpose": _compact_text(src.get("purpose"), 500),
            "verified_fact": _compact_text(src.get("verified_fact"), 900),
            "source_type": _compact_text(src.get("source_type"), 100),
            "actions": [
                {
                    "label": _compact_text(x.get("label"), 120),
                    "url": _compact_text(x.get("url"), 500),
                }
                for x in (src.get("actions", []) or [])[:5]
            ],
        })

    user_official_pages = [
        {"title": _compact_text(p.get("title"), 120), "url": p.get("url", ""), "text": _compact_text(p.get("text"), 2500)}
        for p in (a.get("official_source_pages", []) or []) if p.get("user_provided")
    ][:6]

    return {
        "current_date": a.get("current_date") or date.today().isoformat(),
        "user_official_pages": user_official_pages,
        "keyword": a.get("keyword", ""),
        "main_keyword": a.get("main_keyword") or a.get("keyword", ""),
        "main_keyword_evidence": _compact_text(a.get("main_keyword_evidence"), 1000),
        "search_intent": _compact_text(a.get("search_intent"), 1200),
        "recommended_strategy": a.get("recommended_strategy", ""),
        "strategy_reason": _compact_text(a.get("strategy_reason"), 1200),
        "recommended_outline": compact_list(a.get("recommended_outline"), 300, 12),
        "content_gaps": compact_list(a.get("content_gaps"), 500, 12),
        "current_time_extension_points": compact_list(a.get("current_time_extension_points"), 500, 10),
        "new_content_opportunities": compact_list(a.get("new_content_opportunities"), 700, 8),
        "related_keywords": compact_list(a.get("related_keywords"), 120, 20),
        "long_tail_keywords": compact_list(a.get("long_tail_keywords"), 160, 20),
        "extra_search_terms": compact_list(a.get("extra_search_terms"), 160, 12),
        "title_patterns": compact_list(a.get("title_patterns"), 220, 8),
        "reader_questions": compact_list(a.get("reader_questions"), 200, 10),
        "missing_info": compact_list(a.get("missing_info"), 300, 10),
        "current_status": a.get("current_status", {}) or {},
        "freshness_warning": _compact_text(a.get("freshness_warning"), 1000),
        "current_source_facts": compact_list(a.get("current_source_facts"), 1000, 15),
        "official_sources": official_sources,
        "source_pages": source_pages,
        "benchmark": a.get("benchmark", {}) or {},
        "existing_content_asset_summary": _compact_text(a.get("existing_content_asset_summary"), 1200),
        "existing_content_relevance": _compact_text(a.get("existing_content_relevance"), 1000),
        "existing_content_strengths": compact_list(a.get("existing_content_strengths"), 400, 8),
        "existing_content_missing_or_extendable": compact_list(a.get("existing_content_missing_or_extendable"), 500, 10),
        "cannibalization_note": _compact_text(a.get("cannibalization_note"), 800),
        "recommended_source_post": _compact_text(a.get("recommended_source_post"), 500),
        "home_feed_angle": _compact_text(a.get("home_feed_angle"), 900),
        "search_fit_score": a.get("search_fit_score", 0),
        "home_feed_fit_score": a.get("home_feed_fit_score", 0),
        "recommended_content_mode": a.get("recommended_content_mode", ""),
        "travel_checkpoints": compact_list(a.get("travel_checkpoints"), 300, 10),
        "searchad_keyword_data": compact_list(a.get("searchad_keyword_data"), 300, 20),
    }


def _korean_date(iso_value):
    """'2026-09-23' → '2026년 9월 23일'"""
    try:
        d = date.fromisoformat(str(iso_value)[:10])
        return f"{d.year}년 {d.month}월 {d.day}일"
    except Exception:
        d = date.today()
        return f"{d.year}년 {d.month}월 {d.day}일"


def write_with_ai(client, analysis_payload, writing_options):
    selected_title = writing_options.get("selected_title", "").strip()
    main_keyword = analysis_payload.get("main_keyword") or analysis_payload["keyword"]
    current_date_kr = _korean_date(analysis_payload.get("current_date") or date.today().isoformat())
    mode = writing_options.get("content_mode", "AUTO")
    prompt = f"""
당신은 이 주제를 잘 아는 네이버 블로거입니다. 이웃에게 "이건 알고 가면 좋아요" 하고 알려주듯 쓰는 정보형 블로그 글을 작성하세요.
아래 분석 결과는 당신이 미리 조사해 둔 취재 노트이고, 독자에게 보여주는 글은 그 노트를 바탕으로 쓴 '블로그 글'입니다.
이 글의 최우선 목표는 ① 네이버 검색 상위 노출 ② 네이버 홈판 노출 ③ 네이버 AI 브리핑·생성형 검색(GEO)에서의 인용입니다.

[기본 정보]
- 오늘 날짜(작성 기준일): {current_date_kr}
- 작성 유형: {mode}
- 목표 분량: {writing_options.get("length", "공백 제외 1500자 이상")}
- 선택된 제목: {selected_title}
- 제목 선택 이유/각도: {writing_options.get("selected_title_reason", "")}
- SEO 메인 키워드(고정): {main_keyword}
- 원본 입력 키워드: {analysis_payload["keyword"]}
- 카테고리: {writing_options["category"]}

[글의 성격 — 보고서가 아니라 블로그 글]
- 독자는 조사 과정이 아니라 결과가 궁금합니다. "무엇이 확인됐고 무엇이 확인 안 됐는지"를 설명하지 말고, "그래서 언제·어디서·어떻게 하면 좋은지"를 알려주세요.
- 사실 확인은 글을 쓰기 전에 끝내는 작업입니다. 근거가 확인된 정보만 자신 있게 쓰고, 확인되지 않은 정보는 언급하지 말고 빼세요. 빠진 정보를 "아직 확인되지 않았어요", "확정된 것은 아니에요"라고 하나하나 설명하지 마세요.
- 변동 가능성 안내는 글 전체에서 딱 한 번, 마무리 부분에 "방문 전 공식 홈페이지 공지를 한 번 더 확인해 보세요"처럼 짧게만 쓰세요.
- 출처 표기도 최소화하세요. "공식 홈페이지 기준으로 정리했어요"는 기준일 줄 근처에 한 번이면 충분해요. 모든 문장에 "공식 페이지에 안내돼 있어요", "~로 표시돼 있어요"를 붙이지 마세요.
- 소제목은 조사 항목이 아니라 독자가 궁금한 것으로 쓰세요.
  - 좋은 예: '언제 가면 좋을까', '꼭 봐야 할 공연', '주차와 교통', '아이와 간다면'
  - 나쁜 예: '공식 명칭과 일정 확인', '프로그램 확인', '공간 구분'
- 정보를 나열만 하지 말고 블로거의 시선을 더하세요. "처음 간다면 ~부터 보는 걸 추천해요", "~할 땐 ~가 편해요", "~는 놓치기 쉬운데요"처럼 독자의 상황에 맞춘 팁과 추천을 섞으세요. 다만 직접 경험이 제공되지 않았다면 '가봤더니', '먹어보니' 같은 체험 표현은 쓰지 마세요.
- 행사·장소·제품이라면 조사 노트에 있는 사실을 바탕으로 분위기와 장면이 떠오르게 묘사해도 좋아요(예: 어떤 공연인지, 어떤 사람들이 즐기는지). 노트에 없는 사실은 지어내지 마세요.

[독자에게 절대 보이면 안 되는 표현]
- 작업 과정·내부 데이터를 드러내는 말: '시스템', '데이터', '분석 결과', '검증', '교차 확인', '확정 검증', '근거', '자료에 따르면', '검색 과정', '회차 운영 상태', 'current_status', '조사해 보니'
- 반복되는 유보 표현: '확인되지 않았어요', '단정하면 안 돼요', '확정된 것은 아니에요', '~로 표시돼 있지만', '~라는 사실과 ~는 다르거든요'
- 과거 연도 자료를 언급하며 "그대로 믿으면 안 된다"고 설명하는 문장. 지난 자료는 쓰지 않으면 그만이에요.
- 검색 표현·오타·명칭 차이를 설명하는 문장(예: "OO은 검색할 때 쓰는 표현이고 공식 명칭은 △△예요").

[말투 — 반드시 지킬 문체]
- 본문, 핵심 요약, FAQ 답변 모두 친근한 해요체로 씁니다.
- 문장 끝은 '~해요', '~이에요/~예요', '~거든요', '~랍니다', '~죠', '~세요'를 자연스럽게 섞습니다.
- 기본은 '~해요/~예요'입니다. '~랍니다'는 부드럽게 강조할 때 한 섹션에 1~2번 정도만 쓰고, 같은 어미를 3문장 연속 반복하지 마세요.
- '~합니다/~입니다/~습니다' 같은 합쇼체, '~다/~한다' 같은 평서체는 쓰지 마세요. 표 안의 짧은 명사형 항목과 공식 명칭 인용만 예외입니다.
- '~에요'가 아니라 받침 없는 말 뒤에는 '~예요', 받침 있는 말 뒤에는 '~이에요'로 맞춤법을 지키세요.
- 좋은 예: "신청은 복지로에서 할 수 있어요. 올해부터 서류가 하나 늘었거든요." / "생각보다 조건이 까다롭지 않답니다."
- 나쁜 예: "신청은 복지로에서 가능합니다." / "조건은 다음과 같다."
- 이 지시문은 합쇼체로 쓰여 있지만, 출력하는 글의 말투는 반드시 위 해요체를 따르세요.

[분량]
- 공백 제외 약 1500자 이상을 기본 하한으로 합니다. 의미 없는 반복으로 분량을 채우지 마세요.
- HOME_FEED: 1500~2000자 / SEARCH: 3000자 이상 / HYBRID: 2500~3500자 (모두 공백 제외)

[AI 브리핑·생성형 검색 인용 구조 — 모든 유형 공통]
AI 요약은 문단 전체가 아니라 '그 자체로 완결된 한두 문장'을 가져갑니다. 아래 규칙으로 인용되기 쉬운 문장을 만드세요.
1. 서론(2~4문단)이 끝난 바로 다음 줄에 `업데이트 기준일: {current_date_kr} (공식 홈페이지 기준)` 한 줄을 넣으세요. 공식 출처가 없는 주제라면 괄호 부분은 빼세요.
2. SEARCH/HYBRID는 기준일 줄 다음에 아래 형식의 핵심 요약을 넣고, 그 뒤에 목차를 넣으세요.
   **📌 핵심 요약**
   - (완결 문장 1: 주어 + 핵심 사실 + 숫자/조건)
   - (완결 문장 2)
   - (완결 문장 3)
   HOME_FEED는 별도 핵심 요약 블록을 만들지 말고, 서론 마지막 문장에 핵심 결과를 한 문장으로 먼저 알려주세요.
3. 번호가 붙은 각 H2 바로 아래 첫 문장은 그 소제목의 질문에 바로 답하는 문장입니다. 40~100자, 주어(메인키워드 또는 행사·장소·제도·제품명)를 넣어 그 문장만 따로 읽어도 뜻이 통하게 쓰되, 말하듯 자연스럽게 쓰세요. 예: "안동국제탈춤페스티벌은 9월 24일부터 10월 4일까지 11일 동안 열려요."
4. 문장을 '이것은', '이는', '이렇게', '위에서', '앞서', '그래서'처럼 앞 문장에 기대는 지시어로 시작하지 마세요. 특히 H2 첫 문장에서는 금지입니다.
5. 숫자·금액·기간·대상·조건은 근거가 확인된 경우에만 쓰고, 쓸 때는 유보 없이 분명하게 쓰세요. 기관 발표처럼 출처가 신뢰를 더해주는 핵심 수치에만 괄호로 출처를 한 번 붙이세요(예: "(고용노동부 발표)"). 모든 문장에 출처나 기준을 붙이지 마세요.
6. 핵심 섹션마다 정의형("OO은 ~예요"), 조건형("~라면 ~하는 게 좋아요"), 비교형("A는 ~, B는 ~예요") 문장 중 하나 이상을 자연스럽게 넣으세요.
7. 제도·기관·장소·제품은 처음 나올 때 정식 명칭을 쓰고, 이후에는 약칭을 써도 됩니다.
8. FAQ 질문은 실제로 검색창에 입력할 법한 구어체 질문으로 쓰고, 답변 첫 문장에 결론을 넣은 뒤 2~3문장으로 끝내세요.
9. reader_questions(실제 독자 질문)는 아래 [독자 질문 커버 규칙]대로 빠짐없이 답하세요.

[독자 질문 커버 규칙 — 제목과 상관없이 적용]
- reader_questions는 실제 검색자가 묻는 질문입니다. 어떤 제목을 골랐든 이 질문들은 본문 또는 FAQ에서 전부 답해야 합니다.
- 제목의 핵심 약속과 직접 관련된 질문 → 앞쪽 H2에서 가장 자세히 답하세요.
- 제목과 덜 관련된 질문 → 뒤쪽의 짧은 H2(예: '가기 전에 알아두면 좋은 것')에서 2~4문장으로 답하거나 FAQ로 답하세요. 제목 밖이라는 이유로 빼지 마세요.
- 같은 질문을 H2와 FAQ에 중복해서 답하지 마세요.
- 올해 세부 정보가 조사 노트와 '사용자가 직접 확인한 정보'에 모두 없는 질문(missing_info 항목)은 확인된 범위(무엇이 있는지)만 쓰고, 세부는 "어디서 보면 되는지"를 한 문장으로 안내하세요. 이런 안내 문장은 글 전체에서 2번을 넘기지 말고, 가능하면 FAQ 한 곳에 모으세요. 예: "부스 위치는 공식 홈페이지 공지사항에서 축제 직전에 올라와요."
- 명칭 차이·오타를 묻는 질문은 답하지 마세요.

[검색형(SEARCH) 작성 규칙]
- 검색 노출의 핵심은 키워드 반복량이 아니라 '검색의도 충족 + 정보 충실성 + 주제 집중도 + 최신성 + 차별 정보'입니다.
- 첫 300~500자 안에 검색자가 가장 궁금한 답의 방향을 먼저 제시하세요. 서론을 길게 끌지 마세요.
- 핵심 질문별 답변은 서로 다른 H2로 분리하고, H2만 읽어도 글의 답변 구조가 보이게 하세요.
- 행동이 필요한 주제라면 일정/대상/조건/방법/금액/주의사항 중 필요한 항목을 검색의도 순서로 배치하고, '신청 방법' 같은 행동 정보를 글 후반으로 미루지 마세요.
- 경쟁문서와 겹치는 기본 정보만 나열하지 말고, content_gaps·최신 변경점·실제 행동에 도움이 되는 세부사항을 반드시 반영하세요.
- 표는 비교/조건/일정처럼 표가 이해를 빠르게 만드는 경우에만 쓰세요. 체크리스트도 같은 기준으로 쓰세요.

[홈판형(HOME_FEED) 작성 규칙]
- 홈판은 검색형 글을 짧게 줄인 것이 아니라, 홈 피드에서 스크롤을 멈추게 하고 끝까지 읽을 이유를 만드는 별도 구조입니다.
- 첫 문장을 '안녕하세요', '오늘은 ~ 알아볼게요' 같은 인사로 시작하지 마세요.
- 첫 3~5문장: 상황/공감 → 의외의 사실 또는 문제 → 궁금증 확대 → 이 글에서 얻을 핵심.
- 제목 → 첫 문장 → 초반 정보가 하나의 약속처럼 이어져야 합니다. 초반 20~30% 안에 작은 답 또는 반전을 주고, 제목의 후킹 장치는 본문에서 실제 근거로 회수하세요.
- 한 문단 1~2문장, 짧은 문장과 조금 긴 설명을 섞어 모바일 리듬을 만드세요.
- 중반에는 비교·실수하기 쉬운 부분·의외의 포인트·체크리스트처럼 저장 가치가 있는 정보를 두세요.
- 후반에는 독자가 기억할 핵심 2~4개를 정리하고, 필요하면 '그래서 이렇게 하면 돼요' 식의 짧은 행동 가이드를 주세요.
- 하나의 핵심 스토리/각도를 끝까지 유지하되, 독자 질문 중 본문에 담지 못한 것은 FAQ로 답하세요. 인용구는 실제로 강한 한 문장이 있을 때만 쓰세요.

[혼합형(HYBRID) 작성 규칙]
- 서론은 홈판형처럼 공감과 궁금증으로 시작하고, 본문 구조는 검색형처럼 H2별 질문-답변으로 구성하세요.

[본문 구조·서식]
- body_markdown에는 글 제목을 반복하지 마세요. 시작은 2~4개의 서론 문단입니다.
- SEARCH/HYBRID는 핵심 요약 다음에 "## 목차"를 넣고 1., 2., 3. 형식으로 항목을 쓰세요. 목차 항목은 실제 H2 제목과 동일해야 합니다.
- HOME_FEED는 목차를 기본적으로 넣지 마세요. 글이 길거나 정보 구조가 복잡해 실제로 도움이 될 때만 3~5개의 짧은 목차를 넣을 수 있습니다.
- 주요 H2는 "## 1. ...", H3는 필요할 때만 "### 1-1. ..." 형식으로 번호를 붙이세요.
- H2 제목은 12~20자를 목표로, 최대 24자입니다. 한 H2에 한 핵심만 담고, 콜론(:) 나열·연도·과한 수식어는 본문으로 보내세요. 예: '환급금 대상 확인', '환급액과 지급일', '신청 방법'.
- 마지막 H2 제목에는 '정리' 또는 '마무리'를 넣으세요. 예: '## 5. 한 번에 정리'. 앱이 FAQ를 이 H2 바로 앞에 자동으로 넣습니다.
- body_markdown 안에 FAQ 섹션을 따로 쓰지 마세요. FAQ는 faq 필드에만 3~8개 작성합니다. 본문에서 답하지 못한 독자 질문이 많으면 그만큼 늘리세요.
- 모바일 화면 기준으로 1~3문장마다 문단을 나누세요.

[여행 정보·추천 콘텐츠 규칙 — is_travel_content가 true일 때]
- 실제 방문 후기가 아니라 여행 콘텐츠 편집자 관점의 '정보·추천형 여행글'로 작성하세요. 말투는 위 해요체를 그대로 유지합니다.
- '이 지역은 좋다' 같은 일반론보다 '그래서 어디를 선택하면 되는지'가 드러나야 합니다.
- 추천형이면 ① 검색자의 핵심 질문 ② 선택 기준 3개 ③ 실제 후보 선정 ④ 한눈에 비교 ⑤ 후보별 상세 ⑥ 상황별 추천 ⑦ 일정/선택 가이드 흐름을 따르세요.
- 선택 기준 3개는 후보 평가에 실제로 사용하세요. 비교표는 후보별 상세 섹션 밖의 독립 구간에 두세요.
- 후보별 H2는 그 후보의 정보만 다루고, 상세는 '한 줄 결론 → 위치/접근성 → 핵심 특징 → 가격(확인된 경우) → 장점 → 주의점 → 추천 대상'을 활용하세요.
- 'TOP 3'라고 하면 정확히 3개 후보를 각각 설명하고, 제목에 나온 숙소·장소는 본문에서 충분히 다루세요.
- 동선, 이동시간, 주차, 운영시간, 가격, 예약조건은 검색 근거가 있을 때만 쓰세요.
- 직접 방문 경험이 제공되지 않았다면 '제가 묵어보니' 같은 1인칭 체험 표현을 절대 쓰지 마세요.
- 인포크링크/제휴 CTA는 URL을 만들지 말고 '[인포크링크 삽입 위치]' 슬롯만 쓰세요.

[공식 링크]
- official_sources에는 분석에서 실제 확인된 공식 URL만 넣으세요. 신청·자격조회·예약 URL을 확인하지 못했다면 만들지 마세요.
- 공식 링크를 한곳에 몰아넣지 마세요. 자격·조건 설명 직후에는 '자격 조회하기', 신청 설명 직후에는 '신청하기', 예약 설명 직후에는 '예약하기'처럼 해당 정보 바로 아래에 배치하세요.
- 이를 위해 inline_official_links를 작성하고, insert_after에는 body_markdown 안에 실제로 있는 고유한 소제목 또는 문장 일부(20~80자)를 그대로 넣으세요.
- 넣을 링크가 없으면 inline_official_links는 빈 배열로 두고, 같은 링크를 여러 곳에 반복하지 마세요.

[팩트 규칙]
1) 선택된 제목의 약속이 글의 중심입니다. 제목의 약속은 가장 먼저, 가장 자세히 답하세요. 독자 질문(reader_questions)은 제목 밖이어도 다뤄야 하며, 검색의도·독자 질문과 무관한 일반 팁으로 분량을 채우지 마세요.
2) 할인율, 프로모션 기간, 할인코드, 가격, 일정, 신청기간 등 바뀌는 정보는 current_source_facts 또는 source_pages에서 근거가 확인된 것만 쓰세요. 추측은 금지입니다. 확인되지 않은 정보는 "확인 필요"라고 적지 말고 글에서 빼세요.
2-1) freshness_warning, current_status, gap 분석 같은 조사 노트의 메모는 글쓴이 참고용입니다. 그 내용이나 표현을 본문에 옮기지 마세요.
3) 공식 페이지의 최신 상태가 널리 알려진 내용과 다르면 최신 확인 내용을 우선하세요.
4) current_status가 ACTIVE면 현재 회차, UPCOMING이면 다음 회차를 기준으로 쓰고, 끝난 회차를 현재처럼 쓰지 마세요.
5) 참고/벤치마크 URL은 구조·정보 보강용입니다. 문장을 복사하지 말고, 사실은 공식 근거와 교차 확인된 것만 확정하세요. 단, benchmark.source_type이 OFFICIAL_REFERENCE이거나 user_official_pages에 있는 내용은 공식 사실로 사용하세요.
7) current_status.type이 EVENT면 행사 기간 기준으로 '개최 예정/진행 중/종료'를 정확히 표현하세요.
6) 애드센스 유도용 외부 링크는 쓰지 마세요. 쿠팡파트너스는 제품 구매 의도가 있을 때만 1개 슬롯을 제안하고, URL은 만들지 마세요.

[사용자가 직접 확인한 정보 — 공식 사실로 사용]
{writing_options.get("user_supplied_facts", "").strip() or "제공되지 않음"}
- 제공된 경우, 사용자가 공식 홈페이지·안내문에서 직접 옮겨 적은 확인된 정보입니다. 조사 노트보다 우선하는 공식 사실로 보고 빠짐없이 활용하세요.
- 시간표·요금·주차·셔틀처럼 항목이 여러 개인 정보는 표나 짧은 목록으로 정리해 독자가 한눈에 보게 하세요.
- 이 정보를 어디서 받았는지 설명하지 말고, 블로그 글의 정보로 자연스럽게 녹여 쓰세요.
- 이 정보로 채워진 missing_info 항목은 "공식 홈페이지에서 확인하세요"로 넘기지 말고 본문에서 직접 답하세요.

[직접 경험]
- 사용 여부: {"사용" if writing_options.get("direct_experience_enabled") else "사용 안 함"}
- 내용: {writing_options.get("direct_experience_text", "").strip() or "제공되지 않음. 개인 경험을 지어내 1인칭으로 쓰지 마세요."}
- 경험이 제공된 경우에만 1인칭 경험담으로 쓰고, 경험에 없는 날짜·금액·처리기간·감정·결과를 추가하지 마세요.
- 경험 문단은 구체적인 행동과 결과 중심으로 쓰고, 제도 자체의 조건·금액은 공식 근거로 따로 설명해 구분하세요. 네이버는 실제 경험 정보를 별도로 평가합니다.

[메인키워드 SEO 규칙]
- SEO 메인 키워드는 반드시 `{main_keyword}`입니다. 다른 키워드로 바꾸지 마세요.
- 메인키워드는 도입부 첫 2문장 안, 핵심 요약, 핵심 H2 섹션, 마지막 정리에서 자연스럽게 등장하게 하세요. 정해진 횟수를 채우듯 반복하지 마세요.
- secondary_keywords·long_tail_keywords는 의미가 맞을 때만 쓰고, 키워드 나열이나 동의어 폭탄은 금지입니다.
- 메인키워드나 추가 검색 표현이 공식 명칭과 글자가 다른 표현(오타·줄임말·띄어쓰기 차이)이라면, 본문에서는 공식 명칭을 쓰고 그 표현은 제목·태그에만 쓰세요. 본문에 꼭 넣어야 한다면 도입부에서 괄호 병기로 한 번만 쓰고(예: "안동국제탈춤페스티벌(안동탈춤축제)"), 명칭 차이를 설명하는 문장은 쓰지 마세요.
- 검색 자료에서 확인된 사람·장소·서비스·제도·상품명 같은 고유명사는 필요한 곳에 정확히 쓰세요. 근거 없는 엔티티는 추가하지 마세요.
- 제목에서 숫자/날짜/금액/조건을 약속했다면 그 근거가 본문에 명확히 있어야 합니다.

[기타 출력 필드]
- tags: '#' 없이 10~15개. 메인키워드 1개, 롱테일·연관 표현, 기관·장소·제품 고유명사 위주로 쓰고 같은 뜻의 태그를 반복하지 마세요.
- meta_description: 검색 요약문(참고용). 80~120자의 해요체 완결 문장으로 쓰세요. 네이버 에디터 필수 입력 항목이라고 가정하지 마세요.
- thumbnail_text: 썸네일에 얹을 짧은 문구(2~3줄, 줄마다 12자 이내). 본문에 없는 과장·숫자는 넣지 마세요.
- gap_coverage: content_gaps 항목마다 status는 '반영', '부분 반영', '미반영' 중 하나로 쓰고, evidence에는 그 GAP을 반영한 body_markdown 속 문장 일부(10~40자)를 한 글자도 바꾸지 말고 그대로 넣으세요. 미반영이면 evidence는 빈 문자열입니다.
- toc_included / toc_reason: 목차를 넣었는지와 짧은 이유. 홈판에서 생략했다면 "홈판 몰입을 위해 목차 생략"처럼 쓰세요.
- character_count: body_markdown의 공백 제외 글자 수 추정치.

[전략별 규칙]
- 추천 전략: {analysis_payload.get("recommended_strategy", "NEW_KEYWORD")}
- NEW_KEYWORD: 기존글을 전제로 하지 않고 현재 검색의도·경쟁·최신 근거·GAP 중심으로 새 글을 쓰세요.
- UPDATE_EXISTING: 기존 글의 핵심 정보를 유지하되 현재 시점에 필요한 내용을 근거와 함께 보강하세요.
- NEW_DERIVED: 기존 글을 요약하지 말고 새로운 검색의도와 현재 가치를 중심으로 쓰세요.
- NEW_UNRELATED: 기존 자산을 억지로 연결하지 말고 새 주제로 쓰세요.

[작성용 핵심 분석 데이터]
검색 적합도: {analysis_payload.get("search_fit_score", 0)} / 홈판 적합도: {analysis_payload.get("home_feed_fit_score", 0)}
{json.dumps(build_writing_context(analysis_payload), ensure_ascii=False, indent=2)}

[작성 기준 — 처음부터 지키며 한 번에 쓰세요]
초안을 여러 번 다시 쓰지 말고, 아래 기준을 지키며 한 번에 완성본을 쓰세요.
A. 제목의 핵심 약속이 본문 첫 30% 안에서 해결되기 시작한다.
B. 분석된 핵심 질문과 content_gaps가 구체적인 정보로 반영된다.
C. 숫자·날짜·가격·조건이 근거 데이터와 일치하고, 끝난 회차를 현재처럼 쓰지 않는다.
D. reader_questions가 본문 또는 FAQ에서 모두 답해졌고, 제목·독자 질문과 무관한 문단이나 키워드 반복·AI식 반복 문장이 없다.
E. 기준일 줄, (SEARCH/HYBRID) 핵심 요약, H2별 직답 문장이 있다.
F. 처음부터 끝까지 해요체를 유지한다.
G. 조사 보고서가 아니라 블로그 글로 읽힌다. 유보·검증 표현이 반복되지 않고, 소제목이 독자의 궁금증으로 쓰여 있다.

JSON으로만 답하세요.
"""
    return ai_json(client, prompt, ARTICLE_SCHEMA, 32000, thinking="low")


def seo_check(article, analysis):
    """키워드 개수보다 제목 약속·검색의도·구조·AI 인용 구조·말투를 우선 검사합니다.
    이 점수는 노출 보장이 아니라 발행 전 품질 게이트용입니다.
    """
    text = article.get("body_markdown", "") or ""
    keyword = (article.get("main_keyword") or analysis.get("main_keyword") or analysis.get("keyword", "")).strip()
    selected_title = (article.get("seo_title") or article.get("home_title") or "").strip()
    title_keyword_ok = bool(keyword and keyword in selected_title)
    title_words = [w for w in re.findall(r"[가-힣A-Za-z0-9]{2,}", selected_title) if len(w) >= 2]
    title_overlap = sum(1 for w in title_words if w in text)

    # GAP: 모델의 자기 신고가 아니라, evidence 문장이 본문에 실제로 있는지 대조합니다.
    gaps = article.get("gap_coverage", []) or []
    if not analysis.get("content_gaps"):
        gap_label, gap_ok = "콘텐츠 GAP 없음", True
    elif gaps:
        def _norm(s):
            return re.sub(r"\s+", "", str(s or ""))
        norm_text = _norm(text)
        done = 0
        for g in gaps:
            status = str(g.get("status", "")).strip()
            evidence = _norm(g.get("evidence", ""))
            if status == "반영" and len(evidence) >= 6 and evidence in norm_text:
                done += 1
        gap_ok = done == len(gaps)
        gap_label = f"콘텐츠 GAP 본문 반영 확인 ({done}/{len(gaps)})"
    else:
        gap_label, gap_ok = "콘텐츠 GAP 보완 필요", False

    char_count = len(re.sub(r"\s", "", text))
    mode = article.get("content_mode") or analysis.get("recommended_content_mode") or "SEARCH"
    if mode == "HOME_FEED":
        length_ok = 1500 <= char_count <= 2200
    elif mode == "SEARCH":
        length_ok = char_count >= 3000
    elif mode == "HYBRID":
        length_ok = 2500 <= char_count <= 4000
    else:
        length_ok = char_count >= 1500

    h2_titles = [x.strip() for x in re.findall(r"^##\s+([^#].*)$", text, flags=re.M)]
    content_h2 = [x for x in h2_titles if "목차" not in x]
    toc_present = bool(re.search(r"^##\s+목차", text, flags=re.M))
    toc_ok = (mode not in {"SEARCH", "HYBRID"}) or (toc_present and len(content_h2) >= 3)
    intro = text[:min(len(text), 900)]
    intro_keyword_ok = bool(keyword and keyword in intro)

    # AI 브리핑 인용 구조: 기준일, 핵심 요약, H2 직답 문장
    head = text[:2000]
    date_ok = bool(re.search(r"업데이트\s*기준일|\d{4}년\s*\d{1,2}월\s*\d{1,2}일\s*기준", head))
    summary_ok = (mode == "HOME_FEED") or ("핵심 요약" in head)
    lines = text.split("\n")
    direct_total, direct_good = 0, 0
    bad_starts = ("이것", "이는", "이렇게", "위에서", "앞서", "그래서", "그런데", "이처럼", "이 ")
    for i, line in enumerate(lines):
        m = re.match(r"^##\s+(?!#)(.*)$", line)
        if not m or "목차" in m.group(1):
            continue
        first = ""
        for nxt in lines[i + 1:]:
            s = nxt.strip()
            if not s:
                continue
            if s.startswith("#"):
                break
            first = s
            break
        if not first or first.startswith(("|", "-", "*", ">")) or re.match(r"^\d+\.", first):
            direct_total += 1
            continue
        direct_total += 1
        first_sentence = re.split(r"(?<=[.!?])\s", first)[0]
        if 20 <= len(first_sentence) <= 140 and not first_sentence.startswith(bad_starts):
            direct_good += 1
    direct_ok = direct_total == 0 or direct_good / direct_total >= 0.7

    # 말투: 해요체 어미 비율 검사 (합쇼체 '~니다'가 섞이면 경고)
    prose = "\n".join(
        ln for ln in text.split("\n")
        if ln.strip() and not ln.lstrip().startswith(("#", "|", "업데이트 기준일"))
    )
    # 문장 끝(마침표·느낌표·물음표 또는 줄 끝)만 셉니다. '필요', '생각보다' 같은 문장 중간 단어는 제외됩니다.
    end = r"(?:[.!?~]+|$)"
    endings_haeyo = len(re.findall(r"(?:요|죠)" + end, prose, flags=re.M))
    # '~랍니다/~답니다'는 의도한 친근체이므로 해요체 쪽으로 셉니다.
    endings_haeyo += len(re.findall(r"[랍답]니다" + end, prose, flags=re.M))
    endings_formal = len(re.findall(r"(?<![랍답])니다" + end, prose, flags=re.M))
    endings_plain = len(re.findall(r"(?<![요죠니])다" + end, prose, flags=re.M))
    total_endings = endings_haeyo + endings_formal + endings_plain
    tone_ratio = (endings_haeyo / total_endings) if total_endings else 1.0
    tone_ok = tone_ratio >= 0.85

    # 보고서체: 조사 과정·유보 표현이 본문에 새어 나왔는지 검사합니다.
    report_phrases = [
        "시스템", "검증", "교차 확인", "확인되지 않", "확정되지 않", "확정된 것은 아니", "단정하면 안",
        "표시돼 있", "표시되어 있", "안내돼 있", "자료에 따르면", "검색 과정", "근거", "데이터",
        "다시 확인해야", "확인해야 해요", "확인이 필요",
    ]
    report_hits = {ph: text.count(ph) for ph in report_phrases if ph in text}
    report_total = sum(report_hits.values())
    report_ok = report_total <= 3

    # 문장 반복/과도한 동일 표현을 간단히 탐지합니다.
    sentences = [re.sub(r"\s+", " ", x).strip() for x in re.split(r"(?<=[.!?다요죠])\s+", text) if len(x.strip()) >= 18]
    normalized = [re.sub(r"[^가-힣A-Za-z0-9]", "", x) for x in sentences]
    duplicates = len(normalized) - len(set(normalized))
    duplicate_ok = duplicates <= max(1, len(normalized) // 40)

    related = [str(x).strip() for x in (analysis.get("related_keywords", []) or []) if str(x).strip()]
    related_hits = sum(1 for x in related[:10] if x in text)
    related_ok = related_hits >= min(2, len(related)) if related else True

    # 독자 질문 커버: 질문의 핵심 단어가 본문+FAQ에 절반 이상 등장하면 답한 것으로 봅니다.
    faq_text = " ".join(f"{x.get('question','')} {x.get('answer','')}" for x in (article.get("faq", []) or []))
    cover_text = re.sub(r"\s+", "", text + faq_text)
    stop = {"어디", "언제", "어떻게", "무엇", "있나요", "하나요", "인가요", "되나요", "나요", "이용", "확인", "방법", "가능", "해야", "하는", "있는", "어디서", "몇", "시에"}
    kw_norm = re.sub(r"\s+", "", keyword)
    rq_total, rq_covered, rq_missing = 0, 0, []
    for q in (analysis.get("reader_questions", []) or []):
        words = []
        for w in re.findall(r"[가-힣A-Za-z0-9]{2,}", str(q)):
            w = re.sub(r"(은|는|이|가|을|를|과|와|에서|에|의|도|로|으로|이나|나|부터|까지)$", "", w)
            # 동사·형용사 활용형(열리고, 시작하나요 등)은 본문에서 형태가 바뀌므로 명사만 비교합니다.
            if re.search(r"(나요|까요|가요|어요|아요|해요|하고|리고|이고|하나|해야|하는|되는|있는|없는|는지|을까|려면|하면|면)$", w):
                continue
            if len(w) < 2 or w in stop or w == kw_norm or kw_norm in w:
                continue
            words.append(w)
        if not words:
            continue
        rq_total += 1
        hit = sum(1 for w in words if w in cover_text)
        if hit / len(words) >= 0.5:
            rq_covered += 1
        else:
            rq_missing.append(str(q))
    rq_ok = rq_total == 0 or rq_covered == rq_total

    faq_count = len(article.get("faq", []) or [])
    faq_ok = faq_count >= (2 if mode == "HOME_FEED" else 3)

    tags = [str(t).strip().lstrip("#") for t in (article.get("tags", []) or []) if str(t).strip()]
    tags_ok = 5 <= len(tags) <= 30 and bool(keyword) and any(keyword.replace(" ", "") == t.replace(" ", "") for t in tags)

    checks = {
        "제목 메인키워드 포함": title_keyword_ok,
        "제목 약속 본문 반영": title_overlap >= max(1, min(3, len(title_words))),
        "도입부 메인키워드 반영": intro_keyword_ok,
        "업데이트 기준일 명시": date_ok,
        ("핵심 요약 블록" if mode != "HOME_FEED" else "핵심 요약 블록(홈판 생략)"): summary_ok,
        f"H2 직답 문장 ({direct_good}/{direct_total})": direct_ok,
        gap_label: gap_ok,
        "분량 규칙 충족": length_ok,
        ("목차·H2 구조" if mode in {"SEARCH", "HYBRID"} else "홈판 목차 선택 적절성·H2 구조"): toc_ok,
        "연관 검색어 자연스러운 반영": related_ok,
        "문장 중복 과다 없음": duplicate_ok,
        f"해요체 말투 유지 ({round(tone_ratio * 100)}%)": tone_ok,
        (f"블로그 문체(보고서·유보 표현 {report_total}회" + (": " + ", ".join(list(report_hits)[:4]) if report_hits else "") + ")"): report_ok,
        "FAQ 구성": faq_ok,
        (f"독자 질문 반영 ({rq_covered}/{rq_total})" + (" · 누락: " + " / ".join(rq_missing[:3]) if rq_missing else "")): rq_ok,
        "태그 구성(메인키워드 포함)": tags_ok,
        "홈판 제목 별도 생성": bool(article.get("home_title")),
        "썸네일 문구 생성": bool(article.get("thumbnail_text")),
    }
    score = round(sum(checks.values()) / len(checks) * 100)
    return score, checks, text.count(keyword) if keyword else 0, gaps, gap_label



def _extract_status_evidence(text):
    """특정 사이트의 메뉴명을 하드코딩하지 않고 신청/접수/모집 등의 상태 표현을 의미 기준으로 추출합니다."""
    t=re.sub(r"\s+"," ",text or "").strip(); found=[]
    patterns=[
        ("CLOSED",r"(?:신청|접수|모집|지원|예약|등록|참여)[^.!?]{0,35}(?:마감|종료|완료|불가)|(?:마감|종료|완료|불가)[^.!?]{0,35}(?:신청|접수|모집|지원|예약|등록|참여)|현재[^.!?]{0,25}(?:신청|접수|모집|지원|예약)[^.!?]{0,20}(?:할 수 없습니다|불가능합니다|불가합니다)|접수기간이 아닙니다|신청기간이 아닙니다|모집이 완료되었습니다|모집 종료되었습니다"),
        ("UPCOMING",r"(?:신청|접수|모집|지원|예약|등록|참여)[^.!?]{0,30}(?:예정|오픈 예정|시작 예정)|(?:예정|오픈 예정)[^.!?]{0,30}(?:신청|접수|모집|지원|예약)|(?:신청|접수|모집|지원|예약)[^.!?]{0,20}(?:부터|부터 시작)"),
        ("OPEN",r"(?:신청|접수|모집|지원|예약|등록|참여)[^.!?]{0,25}(?:가능|진행 중|진행중|모집 중|모집중)|(?:신청하기|접수하기|지원하기|예약하기|참여하기|등록하기|온라인 신청|온라인 접수)")]
    for state,pat in patterns:
        for m in re.finditer(pat,t,flags=re.I):
            found.append({"state":state,"snippet":t[max(0,m.start()-45):min(len(t),m.end()+55)]})
            if len(found)>=30:return found
    return found

def _round_evidence(text, round_no):
    t=re.sub(r"\s+"," ",text or ""); evidence=[]
    for m in re.finditer(rf"{round_no}\s*차",t,flags=re.I):
        evidence.extend(_extract_status_evidence(t[max(0,m.start()-140):min(len(t),m.end()+220)]))
    return evidence

def derive_current_status(keyword,current_date,source_pages=None,official_source_pages=None,action_pages=None,benchmark=None):
    """공식/참고 페이지의 기간 + 실제 행동 페이지 상태를 종합합니다.
    특정 사이트의 '사전신청하기' 같은 메뉴명이나 단일 '마감' 문구에 의존하지 않습니다."""
    texts=[]
    for page in (official_source_pages or []):
        if page.get('text'): texts.append((page.get('title',''),page.get('url',''),page.get('text',''),True))
    for page in (action_pages or []):
        if page.get('text'): texts.append((page.get('title',''),page.get('url',''),page.get('text',''),True))
    for page in (source_pages or []):
        if page.get('text'): texts.append((page.get('title',''),page.get('url',''),page.get('text',''),False))
    if benchmark and benchmark.get('status')=='ok' and benchmark.get('text'):
        texts.append(('사용자 지정 참고 URL',benchmark.get('url',''),benchmark.get('text',''),True))

    pat=re.compile(r'(\d+)\s*차[^.\n]{0,100}?(?:신청|접수|모집|지원)[^0-9]{0,35}(\d{1,2})\s*(?:월|[./-])\s*(\d{1,2})\s*(?:일)?\s*(?:~|∼|\-|–|부터|까지)\s*(?:(\d{1,2})\s*(?:월|[./-])\s*)?(\d{1,2})\s*(?:일)?',re.I)
    found=[]; seen=set(); all_status=[]
    for title,url,text,is_official in texts:
        clean=re.sub(r"\s+"," ",text)
        ev=_extract_status_evidence(clean)
        if ev: all_status.append({'url':url,'title':title,'official':is_official,'evidence':ev[:12]})
        for m in pat.finditer(clean):
            try:
                rno=int(m.group(1)); sm=int(m.group(2)); sd=int(m.group(3)); em=int(m.group(4) or sm); ed=int(m.group(5))
                st=date(current_date.year,sm,sd); en=date(current_date.year,em,ed)
                if en<st: en=date(current_date.year+1,em,ed)
                key=(rno,st,en,url)
                if key in seen: continue
                seen.add(key); found.append({'round':rno,'start':st,'end':en,'title':title,'url':url,'official':is_official,'evidence':_round_evidence(clean,rno)})
            except Exception: continue

    if not found:
        event = detect_event_period(texts, current_date)
        if event:
            s, e = event['start'], event['end']
            if current_date < s:
                state, label = 'UPCOMING', '개최 예정'
            elif current_date <= e:
                state, label = 'ACTIVE', '진행 중'
            else:
                state, label = 'ENDED', '종료'
            period = f"{s.month}/{s.day}~{e.month}/{e.day}"
            return {
                'type': 'EVENT', 'status': state, 'current_round': '', 'current_round_state': label, 'next_round': '',
                'event_period': {'start': s.isoformat(), 'end': e.isoformat(), 'source_url': event['url'], 'source_title': event['title'], 'evidence': event['snippet']},
                'current_status_reason': f"현재 기준일 {current_date.isoformat()} 기준, 공식/지정 페이지에서 확인한 행사 기간({period})은 '{label}' 상태입니다.",
                'rounds': [], 'status_evidence': all_status[:15],
            }
        closed=sum(1 for x in all_status for e in x['evidence'] if e['state']=='CLOSED')
        opened=sum(1 for x in all_status for e in x['evidence'] if e['state']=='OPEN')
        state='ENDED' if closed and not opened else ('ACTIVE' if opened and not closed else 'UNKNOWN')
        return {'status':state,'current_round':'','current_round_state':'신청 마감' if state=='ENDED' else ('신청 가능' if state=='ACTIVE' else '확인 필요'),'next_round':'','current_status_reason':'회차 기간은 구조화하지 못했지만 공식/행동 페이지의 상태 표현을 확인했습니다.' if all_status else '공식/참고 페이지에서 회차별 기간과 상태를 확인하지 못했습니다.','rounds':[],'status_evidence':all_status[:15]}

    found.sort(key=lambda x:(x['start'],x['round']))
    for x in found:
        states=[e.get('state') for e in x.get('evidence',[])]
        x['explicit_closed']='CLOSED' in states
        x['explicit_open']='OPEN' in states and not x['explicit_closed']

    active=[x for x in found if x['start']<=current_date<=x['end'] and not x['explicit_closed']]
    upcoming=[x for x in found if x['start']>current_date and not x['explicit_closed']]
    if active:
        chosen=active[0]; state='ACTIVE'; reason=f"현재 기준일 {current_date.isoformat()}은 {chosen['round']}차 신청기간({chosen['start'].month}/{chosen['start'].day}~{chosen['end'].month}/{chosen['end'].day})입니다. 공식 상태 문구상 마감으로 확인되지 않았습니다."
    elif upcoming:
        chosen=upcoming[0]; state='UPCOMING'; reason=f"현재 기준일 {current_date.isoformat()}에는 이전 회차가 종료 또는 공식 마감되었고, 다음 신청은 {chosen['round']}차({chosen['start'].month}/{chosen['start'].day}~{chosen['end'].month}/{chosen['end'].day})입니다."
    else:
        chosen=found[-1]; state='ENDED'; reason=f"현재 기준일 {current_date.isoformat()}에는 확인된 신청기간이 모두 종료되었거나 공식 페이지에서 마감 상태로 확인되었습니다."
    return {'status':state,'current_round':f"{chosen['round']}차",'current_round_state':'현재 신청 중' if state=='ACTIVE' else ('다음 신청' if state=='UPCOMING' else '종료'),'next_round':f"{upcoming[0]['round']}차" if upcoming and upcoming[0]['round']!=chosen['round'] else '','current_status_reason':reason,'rounds':[{'round':f"{x['round']}차",'application_start':x['start'].isoformat(),'application_end':x['end'].isoformat(),'source_url':x['url'],'source_title':x['title'],'explicit_closed':x.get('explicit_closed',False),'explicit_open':x.get('explicit_open',False),'status_evidence':x.get('evidence',[])[:6]} for x in found],'status_evidence':all_status[:15]}

def build_analysis_payload(keyword, category, trend, blog, news, web, shopping, benchmark, specific_post=None, blog_id="", current_web=None, source_pages=None, official_source_pages=None, action_pages=None, is_travel_content=False, extra_search_terms=None, additional_search_data=None):
    has_specific = bool(specific_post and specific_post.get("status") not in (None, "not_provided"))
    return {
        "keyword": keyword,
        "category": category,
        "extra_search_terms": extra_search_terms or [],
        "additional_search_data": additional_search_data or [],
        "is_travel_content": bool(is_travel_content),
        "trend": trend_summary(trend),
        "blog_results": compact_results(blog.get("items", []), ["title", "description", "bloggername", "bloggerlink", "postdate"]),
        "news_results": compact_results(news.get("items", []), ["title", "description", "originallink", "pubDate"]),
        "web_results": compact_results(web.get("items", []), ["title", "description", "link"]),
        "current_web_results": compact_results(current_web or [], ["title", "description", "link"]),
        "source_pages": source_pages or [],
        "current_date": date.today().isoformat(),
        "current_status": derive_current_status(keyword, date.today(), source_pages=source_pages, official_source_pages=[], benchmark=benchmark),
        "own_blog_id": blog_id,
        "own_existing_posts": [],
        "specific_existing_post": specific_post or {"status": "not_provided"},
        "own_search_note": (
            "특정 기존글 URL이 입력되어 해당 글만 내 콘텐츠 자산으로 비교합니다." if has_specific
            else "특정 기존글 URL이 입력되지 않아 내 블로그 기존글 비교는 수행하지 않습니다. 현재 키워드 자체를 기준으로 새 콘텐츠를 분석합니다."
        ),
        "shopping_trend": shopping,
        "benchmark": benchmark,
        "recommended_strategy": "PENDING" if has_specific else "NEW_KEYWORD",
    }

INFO_LINK_TERMS = (
    "일정", "프로그램", "공연", "행사", "개요", "소개", "안내", "오시는", "찾아오", "교통", "주차",
    "셔틀", "입장", "요금", "관람", "티켓", "예매", "공지", "부스", "먹거리", "체험", "이용", "운영시간", "FAQ",
)


def _extract_info_links(raw, base_url, limit=12):
    """사용자가 지정한 홈페이지에서 같은 사이트의 안내성 하위 페이지 링크(일정·프로그램·교통·주차 등)를 찾습니다."""
    base_host = re.sub(r"^www\.", "", urlparse(base_url).netloc.lower())
    out, seen = [], {base_url.rstrip("/")}
    for m in re.finditer(r"""<a\b[^>]*href\s*=\s*["']([^"']+)["'][^>]*>([\s\S]*?)</a>""", raw or "", flags=re.I):
        href = m.group(1).strip()
        anchor = re.sub(r"\s+", " ", clean_html(m.group(2))).strip()
        if not href or not anchor or len(anchor) > 30:
            continue
        if href.lower().startswith(("javascript:", "mailto:", "tel:", "#")):
            continue
        full = urljoin(base_url, href)
        host = re.sub(r"^www\.", "", urlparse(full).netloc.lower())
        if host != base_host or re.search(r"\.(pdf|hwp|jpg|jpeg|png|zip)$", full, re.I):
            continue
        if not any(t.lower() in anchor.lower() for t in INFO_LINK_TERMS):
            continue
        key = full.split("#")[0].rstrip("/")
        if key in seen:
            continue
        seen.add(key)
        out.append({"text": anchor, "url": full})
        if len(out) >= limit:
            break
    return out


def fetch_benchmark(url, force_official=False):
    """사용자가 지정한 공개 URL을 읽습니다.
    - force_official=True(사용자가 '공식 홈페이지'로 표시)면 공식 근거로 취급합니다.
    - 공공 도메인(go.kr 등)은 자동으로 공식 근거로 취급합니다.
    - 같은 사이트의 일정·프로그램·교통·주차 등 안내 하위 페이지 링크도 함께 찾아 둡니다.
    """
    url = (url or "").strip()
    if not url:
        return {"status": "not_provided"}
    try:
        raw = _http_get_text(url, timeout=15)
        if not raw:
            return {"status": "failed", "url": url, "error": "페이지를 불러오지 못했습니다."}
        raw = re.sub(r"<script[\s\S]*?</script>", " ", raw, flags=re.I)
        raw = re.sub(r"<style[\s\S]*?</style>", " ", raw, flags=re.I)
        text, links = _extract_page_text_and_links(raw, url)
        info_links = _extract_info_links(raw, url)
        host = re.sub(r"^www\.", "", urlparse(url).netloc.lower())
        if force_official or host.endswith(("go.kr", "gov.kr", "korea.kr", "or.kr")):
            source_type = "OFFICIAL_REFERENCE"
        elif "blog.naver.com" in host:
            source_type = "BLOG_REFERENCE"
        elif "news" in host or "press" in host:
            source_type = "NEWS_REFERENCE"
        else:
            source_type = "WEB_REFERENCE"
        return {
            "status": "ok", "url": url, "source_type": source_type, "host": host,
            "user_marked_official": bool(force_official),
            "text": text[:9000], "action_links": links, "info_links": info_links,
            "note": "" if len(text) >= 300 else "본문 텍스트가 거의 없습니다. 내용이 이미지·자바스크립트로 표시되는 페이지일 수 있습니다.",
        }
    except Exception as e:
        return {"status": "failed", "url": url, "error": str(e)}


def fetch_benchmark_subpages(benchmark, cache, limit=8):
    """사용자가 지정한 URL의 안내 하위 페이지(일정·프로그램·교통·주차 등)를 동시에 읽습니다."""
    links = (benchmark or {}).get("info_links", [])[:limit]
    if not links:
        return []
    raws = fetch_raw_pages([x["url"] for x in links], cache)
    pages = []
    for link in links:
        raw = raws.get(link["url"])
        if not raw:
            continue
        text, action_links = _extract_page_text_and_links(raw, link["url"])
        if text and len(text) >= 100:
            pages.append({
                "title": f"[입력 URL 하위] {link['text']}", "url": link["url"],
                "text": text[:9000], "action_links": action_links, "user_provided": True,
            })
    return pages


EVENT_RANGE_PATTERN = re.compile(
    r"(?:(20\d{2})\s*(?:년|[./-])\s*)?(\d{1,2})\s*(?:월|[./])\s*(\d{1,2})\s*(?:일|\.)?(?:\s*\([^)]{1,4}\))?"
    r"\s*(?:~|∼|～|–|—|-|부터)\s*"
    r"(?:(20\d{2})\s*(?:년|[./-])\s*)?(?:(\d{1,2})\s*(?:월|[./])\s*)?(\d{1,2})\s*일?"
)
EVENT_CONTEXT = re.compile(r"기간|일정|일시|개최|축제|행사|운영|개막|페스티벌|박람회|전시")


def detect_event_period(texts, current_date):
    """축제·행사의 개최 기간(예: 9월 24일~10월 4일)을 찾아 오늘 날짜와 비교합니다.
    texts: [(title, url, text, is_official)]
    """
    from collections import Counter
    votes, official_votes, info = Counter(), Counter(), {}
    for title, url, text, is_official in texts:
        clean = re.sub(r"\s+", " ", text or "")
        for m in EVENT_RANGE_PATTERN.finditer(clean):
            before = clean[max(0, m.start() - 40):m.start()]
            if not EVENT_CONTEXT.search(before):
                continue
            try:
                y1 = int(m.group(1) or current_date.year)
                sm, sd = int(m.group(2)), int(m.group(3))
                em = int(m.group(5) or sm); ed = int(m.group(6))
                y2 = int(m.group(4) or y1)
                start = date(y1, sm, sd); end = date(y2, em, ed)
                if end < start and not m.group(4):
                    end = date(y1 + 1, em, ed)
                if not (0 <= (end - start).days <= 120):
                    continue
                if abs((start - current_date).days) > 400:
                    continue
            except Exception:
                continue
            key = (start, end)
            votes[key] += 1
            if is_official:
                official_votes[key] += 1
            info.setdefault(key, {"url": url, "title": title, "snippet": clean[max(0, m.start() - 40):m.end() + 40]})
    if not votes:
        return None
    # 공식 페이지에서 찾은 기간이 있으면 뉴스·블로그의 기간(작년 일정 등)보다 항상 우선합니다.
    (start, end), _ = (official_votes or votes).most_common(1)[0]
    return {"start": start, "end": end, **info[(start, end)]}


def normalize_extra_search_terms(raw):
    """쉼표/줄바꿈으로 입력한 추가 검색 표현을 중복 제거해 최대 8개까지 반환합니다."""
    parts = re.split(r"[,\n;]+", raw or "")
    out = []
    seen = set()
    for part in parts:
        term = re.sub(r"\s+", " ", part).strip()
        if not term:
            continue
        key = term.casefold()
        if key in seen:
            continue
        seen.add(key)
        out.append(term)
    return out[:8]

def collect_additional_search_data(terms, client_id, client_secret):
    """추가 검색 표현별로 네이버 검색 데이터를 수집합니다. 원본 키워드와 다른 표현의 실제 검색 맥락을 분석하기 위한 데이터입니다."""
    result = []
    for term in terms:
        item = {"term": term, "blog": [], "web": [], "news": []}
        try:
            item["blog"] = compact_results(naver_search("blog", term, client_id, client_secret, display=10, sort="sim").get("items", []), ["title", "description", "bloggername", "postdate"])
        except Exception:
            pass
        try:
            item["web"] = compact_results(naver_search("webkr", term, client_id, client_secret, display=10, sort="sim").get("items", []), ["title", "description", "link"])
        except Exception:
            pass
        try:
            item["news"] = compact_results(naver_search("news", term, client_id, client_secret, display=5, sort="date").get("items", []), ["title", "description", "originallink", "pubDate"])
        except Exception:
            pass
        result.append(item)
    return result


def fetch_naver_post(url):
    """사용자가 지정한 기존 네이버 글을 콘텐츠 자산으로 읽습니다."""
    url = (url or "").strip()
    if not url:
        return {"status": "not_provided"}
    try:
        r = requests.get(url, timeout=15, headers={"User-Agent": "Mozilla/5.0 (compatible; ContentAnalyzer/2.1)"})
        r.raise_for_status()
        raw = r.text
        # 네이버 블로그의 본문이 별도 iframe/JSON에 들어가는 경우가 있어
        # 우선 전체 HTML에서 사람이 읽을 수 있는 텍스트를 최대한 추출합니다.
        text = re.sub(r"<script[\s\S]*?</script>", " ", raw, flags=re.I)
        text = re.sub(r"<style[\s\S]*?</style>", " ", text, flags=re.I)
        text = clean_html(text)
        text = re.sub(r"\s+", " ", text).strip()
        title_match = re.search(r"<title[^>]*>(.*?)</title>", raw, flags=re.I|re.S)
        title = clean_html(title_match.group(1)) if title_match else ""
        return {"status": "ok", "url": url, "title": title, "text": text[:12000]}
    except Exception as e:
        return {"status": "failed", "url": url, "error": str(e)}

# 세션에 API 설정을 보관합니다.
# .env 값은 최초 기본값으로만 사용하고, 사용자가 저장한 값이 우선합니다.
for _key, _env, _secret_paths in [
    ("gemini_key", "GEMINI_API_KEY", ("GEMINI_API_KEY", "gemini.api_key")),
    ("openai_key", "OPENAI_API_KEY", ("OPENAI_API_KEY", "openai.api_key")),
    ("naver_id", "NAVER_CLIENT_ID", ("NAVER_CLIENT_ID", "naver.client_id")),
    ("naver_secret", "NAVER_CLIENT_SECRET", ("NAVER_CLIENT_SECRET", "naver.client_secret")),
    ("searchad_access", "NAVER_SEARCHAD_ACCESS_LICENSE", ("NAVER_SEARCHAD_ACCESS_LICENSE", "searchad.access_license")),
    ("searchad_secret", "NAVER_SEARCHAD_SECRET_KEY", ("NAVER_SEARCHAD_SECRET_KEY", "searchad.secret_key")),
    ("searchad_customer_id", "NAVER_SEARCHAD_CUSTOMER_ID", ("NAVER_SEARCHAD_CUSTOMER_ID", "searchad.customer_id")),
    ("own_blog", "NAVER_BLOG_ID", ("NAVER_BLOG_ID", "naver.blog_id")),
]:
    if _key not in st.session_state or not st.session_state[_key]:
        st.session_state[_key] = _initial_credential(_env, *_secret_paths)

if "credentials_saved" not in st.session_state:
    st.session_state.credentials_saved = False
if "connection_test" not in st.session_state:
    st.session_state.connection_test = None

# API 설정은 사이드바의 연결 테스트와 본문 작업 흐름 모두에서 사용합니다.
# 먼저 계산해 두어, 사이드바가 provider_ready를 참조할 때도 항상 정의돼 있게 합니다.
gemini_key = st.session_state.gemini_key
openai_key = st.session_state.openai_key
naver_id = st.session_state.naver_id
naver_secret = st.session_state.naver_secret
own_blog = st.session_state.own_blog

ai_provider = st.session_state.get("ai_provider", "GEMINI")
# 이전 버전에서 저장된 혼합(HYBRID) 설정은 Gemini로 정리합니다.
if ai_provider not in AI_PROVIDER_OPTIONS:
    ai_provider = "GEMINI"
    st.session_state["ai_provider"] = "GEMINI"
gemini_model = st.session_state.get("gemini_model", MODEL).strip() or MODEL
openai_model = st.session_state.get("openai_model", OPENAI_MODEL).strip() or OPENAI_MODEL
provider_ready = (
    (ai_provider == "GEMINI" and bool(gemini_key))
    or (ai_provider == "OPENAI" and bool(openai_key))
)

def build_ai_config():
    return {
        "provider_mode": ai_provider,
        "gemini_key": gemini_key,
        "gemini_model": gemini_model,
        "openai_key": openai_key,
        "openai_model": openai_model,
    }

with st.sidebar:
    st.header("⚙️ 설정")
    st.caption("AI API와 NAVER API를 이 브라우저 세션에서 설정해 사용할 수 있습니다. API 키 자체는 GitHub 코드에 저장하지 않습니다.")

    with st.form("api_settings_form", clear_on_submit=False):
        st.selectbox(
            "AI 사용 방식",
            AI_PROVIDER_OPTIONS,
            format_func=lambda x: AI_PROVIDER_LABELS[x],
            key="ai_provider",
        )
        st.text_input("Gemini API Key", type="password", key="gemini_key")
        st.text_input("OpenAI API Key", type="password", key="openai_key")
        st.text_input("Gemini 모델명", key="gemini_model")
        st.text_input("OpenAI 모델명", key="openai_model")
        st.text_input(
            "Naver Client ID",
            key="naver_id",
        )
        st.text_input(
            "Naver Client Secret",
            type="password",
            key="naver_secret",
        )
        st.markdown("**네이버 검색광고 API**")
        st.text_input("검색광고 Access License Key", type="password", key="searchad_access")
        st.text_input("검색광고 Secret Key", type="password", key="searchad_secret")
        st.text_input("검색광고 Customer ID", key="searchad_customer_id")
        st.caption("키워드 도구 조회 전용입니다. PC/모바일 검색량·경쟁도 확인에 사용합니다.")
        save_settings = st.form_submit_button(
            "💾 설정 저장",
            type="primary",
            use_container_width=True,
        )

    if save_settings:
        # 저장 버튼을 누른 순간 현재 입력값을 그대로 세션에 확정합니다.
        st.session_state.credentials_saved = True
        st.session_state.connection_test = None
        st.success(f"설정이 현재 세션에 저장됐어요. · {AI_PROVIDER_LABELS.get(st.session_state.ai_provider, st.session_state.ai_provider)}")

    if st.session_state.credentials_saved:
        st.caption("🟢 저장된 API 설정을 사용 중입니다.")

    if st.session_state.naver_id and st.session_state.naver_secret:
        naver_source = "Streamlit Secrets/환경변수에서 불러온 기본값" if not st.session_state.credentials_saved else "설정 화면에서 저장한 현재 세션값"
        st.caption(f"네이버 인증값 출처: {naver_source}")

    if provider_ready and st.session_state.naver_id and st.session_state.naver_secret:
        if st.button("🔌 네이버 API 연결 테스트", use_container_width=True):
            results = {}
            try:
                test_blog = naver_search(
                    "blog", "비짓재팬", st.session_state.naver_id, st.session_state.naver_secret,
                    display=1, sort="sim"
                )
                results["블로그 검색 API"] = "정상" if "items" in test_blog else "응답 확인 필요"
            except Exception as e:
                results["블로그 검색 API"] = f"실패: {e}"

            try:
                test_trend = naver_trend(
                    "비짓재팬", st.session_state.naver_id, st.session_state.naver_secret, days=7
                )
                results["검색어 트렌드 API"] = "정상" if "results" in test_trend else "응답 확인 필요"
            except Exception as e:
                results["검색어 트렌드 API"] = f"실패: {e}"

            if st.session_state.searchad_access and st.session_state.searchad_secret and st.session_state.searchad_customer_id:
                try:
                    test_ad = naver_searchad_keyword_tool(
                        "비짓재팬", st.session_state.searchad_access, st.session_state.searchad_secret, st.session_state.searchad_customer_id
                    )
                    results["검색광고 키워드 도구 API"] = "정상" if "keywordList" in test_ad else "응답 확인 필요"
                except Exception as e:
                    results["검색광고 키워드 도구 API"] = f"실패: {e}"
            st.session_state.connection_test = results

    if st.session_state.connection_test:
        st.markdown("**연결 테스트 결과**")
        for name, result in st.session_state.connection_test.items():
            if result == "정상":
                st.success(f"{name}: 정상", icon="✅")
            else:
                st.error(f"{name}: {result}", icon="❌")

    st.divider()
    st.subheader("내 블로그")
    own_blog = st.text_input(
        "네이버 블로그 주소 또는 ID",
        key="own_blog",
        placeholder="예: https://blog.naver.com/ladisu",
    )
    st.caption("내 블로그 주소/ID는 보관용 기준 정보입니다. 기존글 자동 검색에는 사용하지 않습니다. 정확히 비교할 글은 아래 '특정 기존글 URL(선택)'로 지정합니다.")
    specific_existing_url = st.text_input(
        "특정 기존글 URL(선택)",
        placeholder="예: https://blog.naver.com/ladisu/223000000000",
        help="정확히 분석하고 싶은 과거 글이 있다면 입력하세요. 입력하면 이 글을 최우선 원본 자산으로 분석합니다.",
    )

# 실제 작성 유형은 분석 결과를 본 뒤 사용자가 직접 선택합니다.
# 톤은 별도 선택 UI 없이 글쓰기 기본값으로 사용합니다.
tone = "자연스럽고 친근한 존댓말(~해요, ~랍니다)"
content_mode_request = st.session_state.get("selected_content_mode", "")
length = {
    "HOME_FEED": "공백 제외 1500~2000자",
    "SEARCH": "공백 제외 3000자 이상",
    "HYBRID": "공백 제외 2500~3500자",
}.get(content_mode_request, "")

if not provider_ready or not naver_id or not naver_secret:
    st.title("🔎 네이버 콘텐츠 기회 분석기 V3.1 SEO/GEO")
    st.info("왼쪽 사이드바에서 사용할 AI 방식과 API Key, Naver Client ID / Secret을 입력하면 시작할 수 있어요.")
    st.markdown("""
### 이 버전에서 하는 일
1. Creator Advisor에서 직접 선별한 키워드를 입력
2. 네이버 검색어 트렌드 분석
3. 블로그·뉴스·웹·지식iN 검색 + 상위 블로그 구조 분석
4. 필요하면 쇼핑인사이트 분석
5. 선택한 벤치마크 블로그가 있으면 참고 콘텐츠를 분석
6. 특정 기존글 URL을 입력한 경우에만 내 콘텐츠 자산과 비교
7. 검색용 제목 + 홈판용 제목 + 본문 + FAQ + 태그까지 작성 (이미지는 별도 도구에서 생성)
""")
    st.stop()

client = build_ai_config()

st.title("🔎 네이버 콘텐츠 기회 분석기")
st.caption(f"Creator Advisor에서 직접 선별한 키워드를 넣으면, 네이버 데이터 → 콘텐츠 GAP → (선택) 벤치마크/기존글 비교 → 검색·홈판 전략 → 글 작성까지 연결합니다. · AI: {AI_PROVIDER_LABELS.get(ai_provider, ai_provider)}")

if "analysis" not in st.session_state:
    st.session_state.analysis = None
if "article" not in st.session_state:
    st.session_state.article = None
if "selected_title" not in st.session_state:
    st.session_state.selected_title = ""
if "selected_title_reason" not in st.session_state:
    st.session_state.selected_title_reason = ""
if "selected_content_mode" not in st.session_state:
    st.session_state.selected_content_mode = ""
if "title_options" not in st.session_state:
    st.session_state.title_options = []
if "article_history" not in st.session_state:
    st.session_state.article_history = []
if "selected_history_id" not in st.session_state:
    st.session_state.selected_history_id = None

st.subheader("1. 키워드 입력")
c1, c2 = st.columns([3, 1])
with c1:
    keyword = st.text_input(
        "Creator Advisor에서 선별한 키워드",
        placeholder="예: 비짓재팬",
        label_visibility="collapsed",
    )
with c2:
    category = st.selectbox("카테고리", CATEGORIES, index=2)

_auto_time_sensitive = is_time_sensitive_keyword(keyword)
time_sensitive_topic = st.checkbox(
    "📅 신청·일정이 있는 주제 (지원금·정책·축제·행사·프로모션)",
    value=_auto_time_sensitive,
    key=f"time_sensitive_topic_{_auto_time_sensitive}",
    help="체크하면 공식 페이지와 신청·예약 링크까지 깊게 확인합니다. 일반 정보 주제는 체크를 해제하면 분석이 훨씬 빨라져요.",
)
if _auto_time_sensitive:
    st.caption("키워드에서 신청·일정형 주제로 자동 감지했어요. 아니라면 체크를 해제해 주세요.")

travel_content = st.checkbox(
    "✈️ 여행글로 작성",
    value=False,
    help="여행 키워드라면 여행자 선택 기준·필수 체크포인트·비교표·동선·예약 전 체크사항 중심의 여행 콘텐츠 로직을 추가합니다."
)

with st.expander("선택 옵션", expanded=False):
    commercial = st.checkbox("상품/구매 의도가 있는 키워드", value=False)
    shopping_category = st.text_input(
        "네이버 쇼핑 카테고리 코드(선택)",
        placeholder="예: 50000000",
        disabled=not commercial,
    )
    extra_search_terms_raw = st.text_area(
        "추가 검색 표현(선택)",
        placeholder="같은 주제를 사람들이 다르게 검색하는 표현이 있다면 입력하세요.\n예: 온돌기차, 온돌마루 열차, 서해금빛열차 온돌방",
        help="오타·비슷한 명칭·띄어쓰기 차이·실제 검색 표현 등을 입력할 수 있습니다. 비워두어도 됩니다.",
        height=90,
    )
    benchmark_url = st.text_input(
        "참고/벤치마크 URL(선택)",
        placeholder="참고할 블로그·공식 홈페이지·뉴스·안내 페이지 URL",
        help="블로그뿐 아니라 공식 홈페이지, 공공기관, 뉴스, 안내 페이지도 입력할 수 있습니다. 같은 사이트의 일정·프로그램·교통·주차 안내 페이지까지 함께 확인합니다.",
    )
    benchmark_is_official = st.checkbox(
        "이 URL은 공식 홈페이지예요 (내용을 공식 사실로 사용)",
        value=False,
        disabled=not benchmark_url.strip(),
        help="축제·행사 공식 사이트처럼 .kr/.com 주소라 자동으로 공식 판별이 안 되는 경우 체크하세요. go.kr·or.kr 등 공공 도메인은 자동으로 공식 처리됩니다.",
    )

analyze_clicked = st.button(
    "🔍 키워드 분석 시작",
    type="primary",
    disabled=not keyword.strip(),
    use_container_width=True,
)

if analyze_clicked:
    with st.status("네이버 데이터를 수집하고 콘텐츠 기회를 분석하는 중...", expanded=True) as status:
        try:
            analysis_started = time.time()
            is_time_sensitive = bool(time_sensitive_topic)

            st.write("① 네이버 기본 데이터 (트렌드·블로그·뉴스·웹문서·지식iN) 동시 수집")

            def _safe_kin():
                try:
                    return naver_search("kin", keyword, naver_id, naver_secret, display=20, sort="sim")
                except Exception as kin_error:
                    return {"items": [], "error": str(kin_error)}

            with ThreadPoolExecutor(max_workers=5) as ex:
                f_trend = ex.submit(naver_trend, keyword, naver_id, naver_secret)
                f_blog = ex.submit(naver_search, "blog", keyword, naver_id, naver_secret, 30, "sim")
                f_news = ex.submit(naver_search, "news", keyword, naver_id, naver_secret, 10, "date")
                f_web = ex.submit(naver_search, "webkr", keyword, naver_id, naver_secret, 10, "sim")
                f_kin = ex.submit(_safe_kin)
                trend = f_trend.result()
                blog = f_blog.result()
                news = f_news.result()
                web = f_web.result()
                kin = f_kin.result()
            if kin.get("error"):
                st.caption("지식iN 질문은 가져오지 못했어요(현재 API 설정에서 미지원일 수 있어요). 나머지 분석은 계속합니다.")

            extra_search_terms = normalize_extra_search_terms(extra_search_terms_raw)
            additional_search_data = []
            if extra_search_terms:
                st.write("② 추가 검색 표현 분석")
                additional_search_data = collect_additional_search_data(extra_search_terms, naver_id, naver_secret)

            topic_label = "여행" if travel_content else ("신청·일정형" if is_time_sensitive else "일반 정보형")
            st.write(f"③ 보강 검색 ({topic_label})")
            current_web = current_web_searches(
                keyword, naver_id, naver_secret,
                commercial=commercial, is_travel_content=travel_content, time_sensitive=is_time_sensitive,
            )
            official_candidates = official_candidate_results(current_web)

            # 주제 유형별 페이지 확인 범위
            if is_time_sensitive:
                source_limit, official_limit, action_limit = 10, 8, 8
            elif travel_content:
                source_limit, official_limit, action_limit = 8, 5, 4
            else:
                source_limit, official_limit, action_limit = 5, 3, 0

            st.write("④ 페이지 본문·상위 블로그 구조 동시 확인")
            page_cache = {}
            source_items = current_web[:source_limit]
            official_items = official_candidates[:official_limit]
            blog_items = (blog.get("items", []) or [])[:5]
            urls = [_item_url(x) for x in source_items + official_items]
            urls += [_naver_blog_mobile_url(x.get("link", "")) for x in blog_items]
            fetch_raw_pages([u for u in urls if u], page_cache)

            official_source_pages = build_source_pages(official_items, page_cache, limit=official_limit)
            official_urls = {p["url"] for p in official_source_pages}
            # 공식 페이지와 겹치는 페이지는 source_pages에서 빼서 AI에 같은 원문이 두 번 들어가지 않게 합니다.
            source_pages = [
                p for p in build_source_pages(source_items, page_cache, limit=source_limit)
                if p["url"] not in official_urls
            ]
            top_blog_structures = build_top_blog_structures(blog_items, page_cache, limit=5)

            benchmark = fetch_benchmark(benchmark_url, force_official=benchmark_is_official)
            if benchmark.get("status") == "failed":
                st.warning(f"입력한 URL을 읽지 못했어요: {benchmark.get('error','')}")
            elif benchmark.get("note"):
                st.warning(f"입력한 URL: {benchmark['note']}")
            if benchmark.get("status") == "ok":
                subpages = []
                if benchmark.get("info_links"):
                    st.write(f"⑤ 입력한 URL의 안내 하위 페이지 확인 ({len(benchmark['info_links'][:8])}개: 일정·프로그램·교통·주차 등)")
                    subpages = fetch_benchmark_subpages(benchmark, page_cache, limit=8)
                if benchmark.get("source_type") == "OFFICIAL_REFERENCE":
                    main_page = {
                        "title": "[입력 URL] 공식 홈페이지", "url": benchmark["url"], "text": benchmark.get("text", ""),
                        "action_links": benchmark.get("action_links", []), "user_provided": True,
                    }
                    merged, seen_urls = [], set()
                    for p in [main_page] + subpages + official_source_pages:
                        if p["url"] in seen_urls:
                            continue
                        seen_urls.add(p["url"]); merged.append(p)
                    official_source_pages = merged
                else:
                    source_pages = subpages + source_pages
            action_pages = []
            if action_limit > 0:
                st.write("⑥ 신청·예약 링크 상태 확인")
                action_seed_pages = list(official_source_pages) + list(source_pages[:6])
                if benchmark.get("status") == "ok":
                    action_seed_pages.append(benchmark)
                action_pages = fetch_related_action_pages(action_seed_pages, page_cache, limit=action_limit)

            blog_id = extract_blog_id(own_blog)
            specific_post = fetch_naver_post(specific_existing_url) if specific_existing_url.strip() else {"status": "not_provided"}

            shopping = None
            if commercial and shopping_category.strip():
                st.write("⑦ 쇼핑인사이트")
                shopping = naver_shopping_trend(
                    keyword, shopping_category.strip(), naver_id, naver_secret
                )

            searchad_data = {"keywordList": []}
            if st.session_state.get("searchad_access") and st.session_state.get("searchad_secret") and st.session_state.get("searchad_customer_id"):
                st.write("⑧ 네이버 검색광고 키워드 도구")
                try:
                    searchad_data = naver_searchad_keyword_tool(
                        keyword,
                        st.session_state.searchad_access,
                        st.session_state.searchad_secret,
                        st.session_state.searchad_customer_id,
                    )
                except Exception as e:
                    searchad_data = {"keywordList": []}
                    st.warning(f"검색광고 키워드 데이터는 가져오지 못했지만 나머지 분석은 계속합니다: {e}")

            payload = build_analysis_payload(
                keyword, category, trend, blog, news, web,
                shopping, benchmark, specific_post=specific_post, blog_id=blog_id,
                current_web=current_web, source_pages=source_pages, official_source_pages=official_source_pages, action_pages=action_pages, is_travel_content=travel_content,
                extra_search_terms=extra_search_terms, additional_search_data=additional_search_data
            )
            payload["is_time_sensitive_topic"] = is_time_sensitive
            payload["searchad_keyword_data"] = compact_searchad_keywords(searchad_data, limit=50)
            payload["official_candidate_results"] = compact_results(official_candidates, ["title", "description", "link"])
            payload["official_source_pages"] = official_source_pages
            payload["action_pages"] = action_pages
            payload["kin_questions"] = compact_results((kin.get("items", []) or [])[:20], ["title", "description"])
            payload["top_blog_structures"] = top_blog_structures
            payload["current_status"] = derive_current_status(
                keyword, date.today(), source_pages=source_pages,
                official_source_pages=official_source_pages, action_pages=action_pages, benchmark=benchmark
            )

            st.write(f"⑨ AI 콘텐츠 전략 분석 (자료 수집 {time.time() - analysis_started:.0f}초)")
            client["active_task"] = "analysis"
            ai = analyze_with_ai(client, payload)
            payload.update(ai)

            # 명칭·오타 차이를 묻는 질문은 프롬프트로 한 번 거르고, 남아 있으면 코드에서 한 번 더 제외합니다.
            naming_q = re.compile(r"같은\s*(?:축제|행사|곳|것|제품|제도|사업|대회)\s*(?:인가요|인지|이에요|예요|맞나요)|다른\s*(?:이름|명칭)|명칭|오타|철자|띄어쓰기")
            payload["reader_questions"] = [
                q for q in (payload.get("reader_questions") or []) if not naming_q.search(str(q))
            ]

            # Creator Advisor 입력 키워드는 항상 분석의 기준점으로 보존합니다.
            # AI가 메인키워드를 비워두거나 임의의 새 표현을 만들면 원본 키워드로 복귀합니다.
            input_keyword = keyword.strip()
            ai_main = str(payload.get("main_keyword") or "").strip()
            source = str(payload.get("main_keyword_source") or "").strip()
            related = [str(x).strip() for x in (payload.get("related_keywords") or []) if str(x).strip()]
            if not ai_main:
                payload["main_keyword"] = input_keyword
                payload["main_keyword_source"] = "INPUT_KEYWORD"
                payload["main_keyword_evidence"] = "Creator Advisor에서 사용자가 입력한 원본 키워드를 기본 메인키워드로 사용했습니다."
            elif source == "RELATED_SEARCH" and ai_main not in related:
                payload["main_keyword"] = input_keyword
                payload["main_keyword_source"] = "INPUT_KEYWORD"
                payload["main_keyword_evidence"] = "AI가 선택한 연관 검색어가 분석 결과의 연관 키워드 목록에서 확인되지 않아 원본 입력 키워드로 복귀했습니다."

            # 특정 기존글 URL이 없으면 내 블로그 자산 비교를 수행하지 않으므로
            # AI가 임의로 기존글 기반 전략을 선택하지 못하도록 전략을 고정합니다.
            if not specific_existing_url.strip():
                payload["recommended_strategy"] = "NEW_KEYWORD"
                payload["recommended_source_post"] = "없음"
                payload["existing_content_asset_summary"] = "특정 기존글 URL이 입력되지 않아 분석 대상인 내 기존 콘텐츠 자산이 없습니다."
                payload["existing_content_relevance"] = "기존글 비교를 수행하지 않고 현재 키워드 자체의 검색 의도와 콘텐츠 GAP을 기준으로 분석했습니다."
                payload["existing_content_strengths"] = []
                payload["existing_content_missing_or_extendable"] = []
                # 기존글 비교 결과만 초기화하고, 최신성/현재회차 분석 결과는 보존합니다.
                payload["new_content_opportunities"] = payload.get("new_content_opportunities", [])
                payload["cannibalization_note"] = "기존글 URL을 지정하지 않았으므로 특정 기존글과의 자기잠식 비교는 수행하지 않았습니다."

            st.session_state.analysis = payload
            st.session_state.selected_content_mode = ""
            st.session_state.selected_title = ""
            st.session_state.selected_title_reason = ""
            st.session_state.title_options = []
            st.session_state.pop("content_mode_radio", None)
            st.session_state.pop("selected_title_radio", None)
            st.session_state.article = None
            st.session_state.user_supplied_facts = ""
            status.update(label="분석 완료", state="complete")
        except Exception as e:
            status.update(label="분석 실패", state="error")
            st.error(f"분석 중 오류가 발생했습니다: {e}")

analysis = st.session_state.analysis

if analysis:
    st.divider()
    st.subheader("2. 콘텐츠 기회 분석")

    direction = analysis.get("trend", {}).get("direction", "데이터 없음")
    has_specific_asset = (analysis.get("specific_existing_post", {}) or {}).get("status") == "ok"
    own_count = 1 if has_specific_asset else 0
    opportunity = analysis.get("opportunity", "확인 필요")
    strategy = analysis.get("recommended_strategy", "NEW")

    # 결과 요약은 가로 카드가 아니라 세로형으로 표시해 긴 문장이 잘리지 않도록 합니다.
    st.markdown("""
    <style>
    .summary-list{display:flex;flex-direction:column;gap:8px;margin:8px 0 14px;}
    .summary-row{display:grid;grid-template-columns:130px minmax(0,1fr);gap:14px;align-items:start;border:1px solid #e6e6e6;border-radius:10px;padding:12px 14px;background:#fff;}
    .summary-label{font-size:13px;color:#666;font-weight:700;line-height:1.5;}
    .summary-value{font-size:15px;line-height:1.55;font-weight:600;word-break:keep-all;overflow-wrap:anywhere;}
    @media(max-width:520px){.summary-row{grid-template-columns:1fr;gap:3px;padding:11px 12px;}.summary-label{font-size:12px;}.summary-value{font-size:14px;}}
    </style>
    """, unsafe_allow_html=True)
    def _row(label, value):
        value = str(value or "-")
        return f'<div class="summary-row"><div class="summary-label">{label}</div><div class="summary-value">{value}</div></div>'
    st.markdown(
        '<div class="summary-list">' +
        _row("관심도 추이", direction) +
        _row("콘텐츠 기회", opportunity) +
        _row("검색 의도", analysis.get("search_intent", "-")) +
        _row("검색 적합도", f"{analysis.get('search_fit_score', 0)}/100") +
        _row("홈판 적합도", f"{analysis.get('home_feed_fit_score', 0)}/100") +
        _row("내 기존 관련글", f"{own_count}개") +
        '</div>',
        unsafe_allow_html=True,
    )

    st.caption(analysis.get("own_search_note", ""))

    strategy_labels = {
        "UPDATE_EXISTING": "🔄 기존 글 업데이트",
        "NEW_DERIVED": "🆕 기존 글 기반 신규 글",
        "NEW_UNRELATED": "🆕 기존글과 무관한 신규 글",
        "NEW_KEYWORD": "🆕 신규 키워드 글",
        "NO_OPPORTUNITY": "⏸ 현재 작성 보류",
    }
    st.info(f"추천 콘텐츠 전략: **{strategy_labels.get(strategy, strategy)}")
    st.write(analysis.get("strategy_reason", ""))

    if analysis.get("is_travel_content"):
        checkpoints = analysis.get("travel_checkpoints", []) or []
        if checkpoints:
            st.markdown("### ✈️ 이 여행글의 필수 체크 포인트 3가지")
            for i, item in enumerate(checkpoints[:3], 1):
                st.markdown(f"**{i}. {item}**")

    st.markdown("### ✍️ 글 작성 유형 선택")
    st.caption("적합도는 참고값입니다. 실제 작성 유형은 AI가 자동으로 정하지 않고, 여기에서 직접 선택합니다.")
    mode_labels = {
        "SEARCH": "🔎 검색형 (공백 제외 3000자 이상)",
        "HOME_FEED": "🏠 홈판형 (공백 제외 1500~2000자)",
        "HYBRID": "🔄 혼합형 (공백 제외 2500~3500자)",
    }
    mode_values = ["SEARCH", "HOME_FEED", "HYBRID"]
    current_mode = st.session_state.get("selected_content_mode", "")
    selected_mode = st.radio(
        "작성할 글 유형",
        mode_values,
        index=(mode_values.index(current_mode) if current_mode in mode_values else None),
        format_func=lambda x: mode_labels[x],
        horizontal=True,
        key="content_mode_radio",
    )
    if selected_mode != st.session_state.get("selected_content_mode"):
        st.session_state.selected_content_mode = selected_mode
        st.session_state.selected_title = ""
        st.session_state.selected_title_reason = ""
        st.session_state.title_options = []
        st.session_state.pop("selected_title_radio", None)

    st.markdown("#### 직접경험")
    st.caption("글작성유형과는 별도 설정입니다. 사용함을 선택하면 입력한 경험을 추천 제목과 본문에 함께 반영합니다.")
    def clear_title_selection():
        st.session_state.selected_title = ""
        st.session_state.selected_title_reason = ""
        st.session_state.title_options = []
        st.session_state.pop("selected_title_radio", None)

    experience_values = ["OFF", "ON"]
    current_experience_mode = "ON" if st.session_state.get("direct_experience_enabled", False) else "OFF"
    selected_experience_mode = st.radio(
        "직접경험 사용 여부",
        experience_values,
        index=experience_values.index(current_experience_mode),
        format_func=lambda value: "사용함" if value == "ON" else "사용 안 함",
        horizontal=True,
        key="direct_experience_mode",
    )
    direct_experience_enabled = selected_experience_mode == "ON"
    if direct_experience_enabled != st.session_state.get("direct_experience_enabled", False):
        st.session_state.direct_experience_enabled = direct_experience_enabled
        clear_title_selection()

    direct_experience_text = ""
    if direct_experience_enabled:
        direct_experience_text = st.text_area(
            "직접 경험 내용",
            key="direct_experience_text",
            height=180,
            placeholder=(
                "예: 오사카에서 실제로 쇼핑해봤는데 생각보다 별로였던 제품이 있었어요. "
                "다시 간다면 안 살 것과 꼭 다시 살 것을 정리하고 싶어요.\n\n"
                "※ 실제로 겪은 내용만 입력하세요. 구체적으로 적을수록 제목과 본문에 더 자연스럽게 반영됩니다."
            ),
            on_change=clear_title_selection,
        ).strip()
        st.caption("입력한 경험만 사실로 사용합니다. 날짜·금액·처리기간 등을 입력하지 않았다면 AI가 임의로 만들지 않습니다.")

    if st.button("🎯 선택한 유형의 추천 제목 보기", use_container_width=True):
        if direct_experience_enabled and not direct_experience_text:
            st.warning("직접경험을 사용하려면 경험 내용을 입력해 주세요.")
        else:
            with st.spinner("선택한 작성 유형에 맞는 제목 3개를 만드는 중..."):
                try:
                    client["active_task"] = "title"
                    st.session_state.title_options = generate_titles_for_mode(
                        client,
                        analysis,
                        selected_mode,
                        direct_experience_enabled=direct_experience_enabled,
                        direct_experience_text=direct_experience_text,
                    )
                    if st.session_state.title_options:
                        st.session_state.selected_title = st.session_state.title_options[0].get("title", "")
                        st.session_state.selected_title_reason = st.session_state.title_options[0].get("why", "")
                    else:
                        st.warning("추천 제목을 생성하지 못했습니다.")
                except Exception as e:
                    st.error(f"추천 제목 생성 중 오류가 발생했습니다: {e}")

    title_options = st.session_state.get("title_options", []) or []
    if title_options:
        labels = [x.get("title", "").strip() for x in title_options if x.get("title", "").strip()]
        if labels:
            current = st.session_state.get("selected_title", labels[0])
            if current not in labels:
                current = labels[0]
            selected = st.radio("작성할 제목 선택", labels, index=labels.index(current), key="selected_title_radio")
            st.session_state.selected_title = selected
            selected_obj = next((x for x in title_options if x.get("title", "").strip() == selected), {})
            st.session_state.selected_title_reason = selected_obj.get("why", "")
            if selected_obj.get("angle"):
                st.caption(f"선택 제목의 작성 각도: {selected_obj.get('angle')}")

    if analysis.get("freshness_warning"):
        st.warning("⚠️ 최신 정보 확인: " + analysis.get("freshness_warning"))
    if analysis.get("current_source_facts"):
        with st.expander("🔎 현재 시점 근거 정보", expanded=False):
            for fact in analysis.get("current_source_facts", []):
                st.markdown(f"**{fact.get('fact','')}**")
                st.caption(f"{fact.get('source_type','')} · {fact.get('source_title','')} · 확인일 {fact.get('verified_date','')}")
                if fact.get("source_url"):
                    st.write(fact.get("source_url"))

    if analysis.get("official_sources"):
        st.markdown("### 🔗 확인된 공식 출처")
        st.caption("지원금·정부정책·축제·공공정보 등에서 확인된 공식 페이지입니다. 최종 발행 전 직접 한 번 더 확인해 주세요.")
        for idx, src in enumerate(analysis.get("official_sources", []), 1):
            name = src.get("name", "공식 페이지")
            url = src.get("url", "")
            purpose = src.get("purpose", "")
            fact = src.get("verified_fact", "")
            st.markdown(f"**{name}**")
            if purpose:
                st.caption(purpose)
            if fact:
                st.write(f"확인 내용: {fact}")
            if url:
                try:
                    st.link_button("공식 페이지 확인", url, key=f"analysis_official_{idx}")
                except Exception:
                    st.markdown(f"[공식 페이지 확인]({url})")
            actions = src.get("actions", []) or []
            if actions:
                cols = st.columns(min(len(actions), 3))
                for j, action in enumerate(actions[:3]):
                    label = str(action.get("label", "확인하기")).strip() or "확인하기"
                    action_url = str(action.get("url", "")).strip()
                    if not action_url:
                        continue
                    with cols[j]:
                        try:
                            st.link_button(label, action_url, key=f"analysis_action_{idx}_{j}")
                        except Exception:
                            st.markdown(f"[{label}]({action_url})")

    # 분석 결과는 2열 x 3행 카드 그리드로 표시합니다. 각 카드는 내부 스크롤을 가집니다.
    from html import escape as html_escape
    st.markdown("""
    <style>
    .analysis-card{border:1px solid #e5e7eb;border-radius:12px;background:#fff;padding:14px 16px;height:340px;overflow-y:auto;box-sizing:border-box;margin-bottom:14px;}
    .analysis-card h4{font-size:1rem;margin:0 0 10px 0;}
    .analysis-card h5{font-size:.82rem;margin:11px 0 4px;color:#555;}
    .analysis-card p,.analysis-card li{font-size:.86rem;line-height:1.55;}
    .analysis-card .small{font-size:.78rem;color:#666;line-height:1.5;}
    .keyword-compact{font-size:.78rem;line-height:1.6;color:#444;}
    .kw-table{width:100%;border-collapse:collapse;font-size:.75rem;}
    .kw-table th,.kw-table td{border-bottom:1px solid #eee;padding:6px 4px;text-align:left;vertical-align:top;}
    .kw-table th{position:sticky;top:0;background:#fafafa;z-index:1;}
    </style>
    """, unsafe_allow_html=True)

    def html_card(title, body):
        return f'<div class="analysis-card"><h4>{title}</h4>{body}</div>'

    related = ", ".join(html_escape(str(x)) for x in analysis.get("related_keywords", [])) or "-"
    longtails = ", ".join(html_escape(str(x)) for x in analysis.get("long_tail_keywords", [])) or "-"
    patterns = " · ".join(html_escape(str(x)) for x in (analysis.get("title_patterns", []) or [])) or "-"
    card1 = html_card("🔑 1. 키워드 분석", f"<h5>연관 키워드</h5><div class=\"keyword-compact\">{related}</div><h5>롱테일 키워드</h5><div class=\"keyword-compact\">{longtails}</div><h5>제목 패턴</h5><div class=\"keyword-compact\">{patterns}</div>")

    outline = "".join(f"<li>{html_escape(str(x))}</li>" for x in (analysis.get("recommended_outline", []) or [])) or "<li>-</li>"
    card2 = html_card("📊 2. 경쟁 콘텐츠", f"<h5>검색 결과에서 반복되는 주제</h5><ul>{outline}</ul><h5>경쟁 수준</h5><p>{html_escape(str(analysis.get('competition', '-')))}</p><div class=\"small\">네이버 블로그 검색 API의 제목·설명 등을 기반으로 한 요약입니다.</div>")

    gaps = "".join(f"<li>🧩 {html_escape(str(x))}</li>" for x in (analysis.get("content_gaps", []) or [])) or "<li>-</li>"
    card3 = html_card("🧩 3. 콘텐츠 GAP", f"<ul>{gaps}</ul>")

    home_angle = html_escape(str(analysis.get("home_feed_angle", "-")))
    fit_reason = html_escape(str(analysis.get("content_mode_reason", analysis.get("strategy_reason", "-"))))
    card4 = html_card("🏠 4. 홈판 전략", f"<h5>홈판 콘텐츠 각도</h5><p>{home_angle}</p><h5>적합도</h5><p>홈판 {analysis.get('home_feed_fit_score',0)}/100 · 검색 {analysis.get('search_fit_score',0)}/100</p><h5>판단 근거</h5><p>{fit_reason}</p><div class=\"small\">적합도는 참고값이며 작성 유형은 사용자가 직접 선택합니다.</div>")

    ad_rows = analysis.get("searchad_keyword_data", []) or []
    if ad_rows:
        trs=[]
        for r in ad_rows:
            trs.append(f"<tr><td>{html_escape(str(r.get('keyword','-')))}</td><td>{html_escape(str(r.get('pc_search','-')))}</td><td>{html_escape(str(r.get('mobile_search','-')))}</td><td>{html_escape(str(r.get('total_search','-')))}</td><td>{html_escape(str(r.get('competition','-')))}</td></tr>")
        ad_table = '<table class="kw-table"><thead><tr><th>키워드</th><th>PC</th><th>모바일</th><th>합계</th><th>경쟁도</th></tr></thead><tbody>' + ''.join(trs) + '</tbody></table>'
    else:
        ad_table = '<p>검색광고 API 키를 설정하면 입력 키워드 기준 추천 키워드와 PC/모바일 검색량·경쟁도가 표시됩니다.</p>'
    card5 = html_card("🔎 5. 추천 키워드 · 검색광고 데이터", '<div class="small">네이버 검색광고 키워드 도구의 연관 키워드입니다. 월간 PC/모바일 검색수와 경쟁도를 참고하세요.</div>' + ad_table)

    specific = analysis.get("specific_existing_post", {}) or {}
    if specific.get("status") == "ok":
        asset_head = "🎯 지정한 기존글을 콘텐츠 자산으로 분석했습니다."
        asset_detail = f"<p><b>{html_escape(str(specific.get('title') or specific.get('url','')))}</b></p><div class='small'>{html_escape(str(specific.get('url','')))}</div>"
    elif specific.get("status") == "failed":
        asset_head = "⚠️ 입력한 기존글 URL을 직접 읽지 못했습니다."
        asset_detail = "<p>기존글 비교 없이 현재 키워드 기준으로 작성할 수 있습니다.</p>"
    else:
        asset_head = "ℹ️ 특정 기존글 URL이 입력되지 않았습니다."
        asset_detail = "<p>기존글 비교는 선택사항입니다.</p>"
    strengths = "".join(f"<li>{html_escape(str(x))}</li>" for x in (analysis.get("existing_content_strengths", []) or [])) or "<li>-</li>"
    extensions = "".join(f"<li>🆕 {html_escape(str(x))}</li>" for x in (analysis.get("current_time_extension_points", []) or [])) or "<li>-</li>"
    opportunities = "".join(f"<li><b>{html_escape(str(x.get('topic','')))}</b> · {html_escape(str(x.get('keyword','')))}</li>" for x in (analysis.get("new_content_opportunities", []) or [])) or "<li>-</li>"
    card6 = html_card("📚 6. 내 기존글", asset_detail + f"<div class='small'>{asset_head}</div><h5>기존 콘텐츠 자산</h5><p>{html_escape(str(analysis.get('existing_content_asset_summary','-')))}</p><h5>현재 키워드와의 연결성</h5><p>{html_escape(str(analysis.get('existing_content_relevance','-')))}</p><h5>기존 글의 강점</h5><ul>{strengths}</ul><h5>확장 포인트</h5><ul>{extensions}</ul><h5>추천 신규 콘텐츠 기회</h5><ul>{opportunities}</ul><h5>자기잠식 주의</h5><p>{html_escape(str(analysis.get('cannibalization_note','-')))}</p><div class='small'>내 블로그 전체 자동 검색은 하지 않습니다. 비교하려면 사이드바의 특정 기존글 URL을 지정하세요.</div>")

    # 정확히 2 x 3: 1/2 → 3/4 → 5/6. 내 기존글은 마지막 칸입니다.
    c1, c2 = st.columns(2)
    with c1: st.markdown(card1, unsafe_allow_html=True)
    with c2: st.markdown(card2, unsafe_allow_html=True)
    c1, c2 = st.columns(2)
    with c1: st.markdown(card3, unsafe_allow_html=True)
    with c2: st.markdown(card4, unsafe_allow_html=True)
    c1, c2 = st.columns(2)
    with c1: st.markdown(card5, unsafe_allow_html=True)
    with c2: st.markdown(card6, unsafe_allow_html=True)

    rq = analysis.get("reader_questions", []) or []
    tops = analysis.get("top_blog_structures", []) or []
    if rq or tops:
        q1, q2 = st.columns(2)
        with q1:
            with st.expander(f"🙋 실제 독자 질문 ({len(rq)}개)", expanded=False):
                for q in rq:
                    st.write(f"• {q}")
                if not rq:
                    st.caption("이번 분석에서는 독자 질문을 정리하지 못했어요.")
        with q2:
            with st.expander(f"📑 상위 블로그 구조 ({len(tops)}개)", expanded=False):
                for t in tops:
                    st.markdown(f"**{t.get('title','')}**")
                    st.caption(
                        f"공백 제외 {t.get('char_count',0):,}자 · 이미지 {t.get('image_count',0)} · 표 {t.get('table_count',0)} · FAQ {'있음' if t.get('has_faq') else '없음'}"
                    )
                    if t.get("headings"):
                        st.caption(" / ".join(t.get("headings", [])))
                if not tops:
                    st.caption("상위 블로그 본문을 읽지 못했어요.")

    # 실제로 읽은 공식/참고 페이지 목록: 자료 부족이 '못 읽어서'인지 '원래 없어서'인지 판단하는 용도
    official_pages_read = analysis.get("official_source_pages", []) or []
    bench = analysis.get("benchmark", {}) or {}
    with st.expander(f"🔎 실제로 읽은 공식·참고 페이지 ({len(official_pages_read)}개)", expanded=False):
        if bench.get("status") == "ok":
            st.caption(
                f"입력 URL: {bench.get('url','')} · 판별: {bench.get('source_type','')} · "
                f"본문 {len(bench.get('text','')):,}자 · 발견한 안내 하위 링크 {len(bench.get('info_links', []) or [])}개"
            )
            if bench.get("note"):
                st.warning(bench["note"])
        elif bench.get("status") == "failed":
            st.warning(f"입력 URL을 읽지 못했어요: {bench.get('error','')}")
        for pg in official_pages_read:
            n = len(pg.get("text", "") or "")
            flag = " ⚠️ 본문 거의 없음(이미지·스크립트 페이지일 수 있음)" if n < 300 else ""
            st.caption(f"• {pg.get('title','')} — 본문 {n:,}자{flag}")
            st.caption(f"  {pg.get('url','')}")
        if not official_pages_read:
            st.caption("공식 페이지를 읽지 못했어요. 참고 URL에 공식 홈페이지를 넣고 '공식 홈페이지예요'를 체크해 보세요.")

    missing = analysis.get("missing_info", []) or []
    if missing:
        st.markdown("### 📝 글에 넣으면 좋은데 자료가 부족한 정보")
        st.caption("수집한 페이지에서 확인되지 않은 정보예요. 공식 홈페이지의 이미지(행사일정표·포스터)나 공지사항에서 직접 확인해 아래에 적어주면, 글에 그대로 반영돼요.")
        for m in missing:
            st.markdown(f"**• {m.get('item','')}**")
            st.caption(f"필요한 이유: {m.get('why_needed','')} · 확인 위치: {m.get('where_to_find','')}")
    st.text_area(
        "직접 확인한 정보 입력 (선택)",
        key="user_supplied_facts",
        height=140,
        placeholder="예)\n개막식: 9월 OO일(요일) OO:OO, OO공연장\n주차: OO주차장, 임시주차장 OO / 셔틀 OO분 간격\n입장료: OO공연장 유료(성인 OOOO원), 그 외 무료",
        help="공식 홈페이지나 안내문에서 직접 확인한 내용만 적어주세요. 공식 사실로 사용됩니다. 형식은 자유예요.",
    )

    st.divider()
    st.subheader("3. 글 작성")

    if st.session_state.get("direct_experience_enabled", False):
        st.caption("직접경험이 추천 제목과 본문에 반영됩니다. 경험 내용을 바꾸려면 위의 ‘글 작성 유형 선택’ 영역에서 수정해 주세요.")

    if not st.session_state.get("selected_title"):
        st.warning("먼저 글 작성에 사용할 추천 제목을 하나 선택해 주세요.")
    if st.button("✍️ 선택한 제목으로 글 작성", type="primary", use_container_width=True):
        if not st.session_state.get("selected_title"):
            st.error("글 작성 전에 추천 제목을 하나 선택해 주세요.")
            st.stop()
        if not st.session_state.get("selected_content_mode"):
            st.error("먼저 검색형·홈판형·혼합형 중 하나를 직접 선택해 주세요.")
            st.stop()
        with st.spinner("선택한 유형과 제목에 맞춰 글을 작성하고 있어요..."):
            try:
                selected_title = st.session_state.get("selected_title", "").strip()
                selected_mode = st.session_state.get("selected_content_mode")
                selected_length = {
                    "HOME_FEED": "공백 제외 1500~2000자",
                    "SEARCH": "공백 제외 3000자 이상",
                    "HYBRID": "공백 제외 2500~3500자",
                }[selected_mode]
                client["active_task"] = "writing"
                article = write_with_ai(
                    client,
                    analysis,
                    {"category": category, "tone": tone, "length": selected_length,
                     "content_mode": selected_mode,
                     "selected_title": selected_title,
                     "selected_title_reason": st.session_state.get("selected_title_reason", ""),
                     "direct_experience_enabled": bool(st.session_state.get("direct_experience_enabled", False)),
                     "direct_experience_text": st.session_state.get("direct_experience_text", "").strip() if st.session_state.get("direct_experience_enabled", False) else "",
                     "user_supplied_facts": st.session_state.get("user_supplied_facts", "").strip()},
                )
                # 사용자가 선택한 제목과 분석에서 확정한 메인키워드를 실제 발행 데이터에 고정합니다.
                if selected_title:
                    article["seo_title"] = selected_title
                    article["home_title"] = selected_title
                article["main_keyword"] = analysis.get("main_keyword") or analysis.get("keyword")
                article["content_mode"] = st.session_state.get("selected_content_mode")

                article["character_count"] = len(re.sub(r"\s", "", article.get("body_markdown", "")))
                article["target_length_rule"] = {
                    "HOME_FEED": "공백 제외 1500~2000자",
                    "SEARCH": "공백 제외 3000자 이상",
                    "HYBRID": "공백 제외 2500~3500자",
                }.get(article["content_mode"], "공백 제외 1500자 이상")
                if not article.get("official_sources"):
                    article["official_sources"] = analysis.get("official_sources", []) or []

                # 작성 결과를 이번 앱 세션의 '작성한 글' 보관함에 추가합니다.
                history_item = dict(article)
                history_item["history_id"] = datetime.now().strftime("%Y%m%d%H%M%S%f")
                history_item["created_at"] = datetime.now().strftime("%Y-%m-%d %H:%M")
                history_item["source_keyword"] = analysis.get("keyword", "")
                history_item["category"] = category
                history_item["content_mode"] = selected_mode
                st.session_state.article_history = [
                    history_item, *st.session_state.article_history
                ][:50]
                st.session_state.selected_history_id = history_item["history_id"]
                st.session_state.article = article
            except Exception as e:
                st.error(f"글 작성 중 오류가 발생했습니다: {e}")

def render_body_with_inline_official_links(body_markdown, inline_links):
    """본문의 관련 문단 바로 뒤에 공식 행동 링크를 배치합니다."""
    body = body_markdown or ""
    links = inline_links or []
    used = set()
    # 긴 anchor부터 처리해 부분 일치 충돌을 줄입니다.
    for link in sorted(links, key=lambda x: len(str(x.get("insert_after", ""))), reverse=True):
        anchor = str(link.get("insert_after", "")).strip()
        url = str(link.get("url", "")).strip()
        label = str(link.get("label", "확인하기")).strip() or "확인하기"
        purpose = str(link.get("purpose", "")).strip()
        if not anchor or not url or anchor in used:
            continue
        idx = body.find(anchor)
        if idx < 0:
            continue
        end = idx + len(anchor)
        # 링크가 문장 중간을 가리키면 해당 줄 끝까지를 본문 단위로 사용합니다.
        line_end = body.find("\n", end)
        if line_end < 0:
            line_end = len(body)
        insert_at = line_end
        html = f'<div style="margin:8px 0 14px 0;">'
        html += f'<a href="{html_escape(url)}" target="_blank" style="text-decoration:none;">'
        html += f'<span style="display:inline-block;padding:7px 13px;border:1px solid #d1d5db;border-radius:8px;background:#f8fafc;font-weight:600;">🔗 {html_escape(label)}</span></a>'
        if purpose:
            html += f'<div style="font-size:0.82rem;color:#6b7280;margin-top:4px;">{html_escape(purpose)}</div>'
        html += '</div>'
        # Markdown과 HTML을 섞을 수 있으므로 조각 단위로 렌더링합니다.
        before = body[:insert_at]
        after = body[insert_at:]
        st.markdown(before, unsafe_allow_html=True)
        st.markdown(html, unsafe_allow_html=True)
        body = after
        used.add(anchor)
    if body.strip():
        st.markdown(body, unsafe_allow_html=True)
    return used

article = st.session_state.article

# 작성한 글 목록: 제목을 클릭하면 해당 글을 다시 열 수 있습니다.
if st.session_state.get("article_history"):
    st.divider()
    st.subheader("📚 작성한 글")
    st.caption("이번 앱 세션에서 작성한 글입니다. 제목을 클릭하면 내용을 다시 볼 수 있어요.")

    for idx, item in enumerate(st.session_state.article_history):
        hid = item.get("history_id", str(idx))
        title = (item.get("seo_title") or item.get("home_title") or "제목 없음").strip()
        keyword_text = item.get("source_keyword", "")
        created = item.get("created_at", "")
        mode_text = item.get("content_mode", "")
        prefix = "▶ " if hid == st.session_state.get("selected_history_id") else ""
        label = f"{prefix}{title}"
        meta = " · ".join(x for x in [created, keyword_text, mode_text] if x)
        c1, c2 = st.columns([8, 2])
        with c1:
            if st.button(label, key=f"history_title_{hid}", use_container_width=True):
                st.session_state.article = item
                st.session_state.selected_history_id = hid
                st.rerun()
        with c2:
            st.caption(meta)

if article:
    st.divider()
    st.subheader("4. 최종 콘텐츠")

    st.markdown("### 🧭 작성 유형")
    st.info(f"{article.get('content_mode', analysis.get('recommended_content_mode', 'SEARCH'))} · {article.get('target_length_rule', length)} · 공백 제외 {article.get('character_count', len(re.sub(r'\s', '', article.get('body_markdown', '')))):,}자")

    a, b = st.columns(2)
    with a:
        st.markdown("### 🔍 검색용 제목")
        st.code(article.get("seo_title", ""), language=None)
        st.caption("분석 단계에서 선택한 제목을 기준으로 작성된 결과입니다.")
    with b:
        st.markdown("### 🏠 홈판용 제목")
        st.code(article.get("home_title", ""), language=None)

    c, d = st.columns(2)
    with c:
        st.markdown("### 🖼 썸네일 문구")
        st.code(article.get("thumbnail_text", ""), language=None)
    with d:
        st.markdown("### 🔎 검색 요약문 (참고용)")
        st.write(article.get("meta_description", ""))
        st.caption("네이버 스마트에디터에 별도로 입력하는 필수 항목이 아닙니다. 본문의 핵심 내용을 확인하기 위한 참고용 요약문입니다.")

    st.markdown("### 핵심 키워드")
    st.write(article.get("main_keyword", ""))
    st.markdown("### 서브 키워드")
    st.write(", ".join(article.get("secondary_keywords", [])))
    st.markdown("### 롱테일 키워드")
    st.write(", ".join(article.get("long_tail_keywords", [])))

    st.markdown("### 본문")
    inline_links = article.get("inline_official_links", []) or []
    placed_inline_links = render_body_with_inline_official_links(article.get("body_markdown", ""), inline_links)
    unplaced_inline_links = [x for x in inline_links if str(x.get("insert_after", "")).strip() not in placed_inline_links]
    if unplaced_inline_links:
        st.caption("※ 일부 공식 링크는 본문 위치를 정확히 찾지 못해 아래 공식 출처 영역에서 확인할 수 있습니다.")

    st.markdown("### 네이버 스마트에디터용 본문")
    st.caption("네이버 모바일 기준으로 문단·문장 호흡을 짧게 정리한 본문입니다. 복사 아이콘으로 복사한 뒤 네이버 스마트에디터에 Ctrl+V로 붙여넣을 수 있습니다.")
    smart_text = re.sub(r"^#{1,6}\s*", "", article.get("body_markdown", ""), flags=re.MULTILINE)
    smart_text = re.sub(r"\*\*(.*?)\*\*", r"\1", smart_text)
    smart_text = re.sub(r"\[(.*?)\]\([^)]*\)", r"\1", smart_text)

    # 본문 내 공식 행동 링크도 같은 위치에 텍스트/URL 형태로 넣습니다.
    # 네이버 스마트에디터 복사에서는 Streamlit 버튼 자체를 전달할 수 없으므로,
    # 해당 정보 바로 아래에 [라벨] URL 형태로 넣어 링크 위치를 유지합니다.
    for link in sorted(article.get("inline_official_links", []) or [], key=lambda x: len(str(x.get("insert_after", ""))), reverse=True):
        anchor = str(link.get("insert_after", "")).strip()
        label = str(link.get("label", "확인하기")).strip() or "확인하기"
        url = str(link.get("url", "")).strip()
        if not anchor or not url:
            continue
        idx = smart_text.find(anchor)
        if idx < 0:
            continue
        line_end = smart_text.find("\n", idx + len(anchor))
        if line_end < 0:
            line_end = len(smart_text)
        block = f"\n\n[{label}] {url}\n"
        smart_text = smart_text[:line_end] + block + smart_text[line_end:]

    # FAQ도 스마트에디터 복사 영역에 함께 포함합니다.
    faq_items = article.get("faq", []) or []
    if faq_items:
        faq_lines = ["", "FAQ", ""]
        for item in faq_items:
            q = str(item.get("question", "")).strip()
            a = str(item.get("answer", "")).strip()
            if q:
                faq_lines.append(q)
            if a:
                faq_lines.append(a)
            faq_lines.append("")
        faq_block = "\n" + "\n".join(faq_lines).rstrip() + "\n"
        # FAQ는 결론 뒤에 붙이지 않고, 결론 직전에 배치합니다.
        # 본문에 번호가 붙은 '결론' H2가 있으면 해당 H2 앞에 삽입하고,
        # 결론을 찾지 못하면 본문 마지막에 안전하게 추가합니다.
        # smart_text는 이미 '#'이 제거된 상태이므로 번호형 소제목 줄을 기준으로 찾습니다.
        conclusion_pattern = re.compile(r"(?m)^(?:\d+\.\s*)?(?:결론|마무리|[^\n]{0,15}정리)[^\n]{0,20}$")
        # 목차에도 같은 문구가 있으므로 마지막 일치(실제 본문 소제목)를 사용합니다.
        matches = list(conclusion_pattern.finditer(smart_text))
        match = matches[-1] if matches else None
        if match:
            smart_text = smart_text[:match.start()].rstrip() + faq_block + "\n" + smart_text[match.start():].lstrip()
        else:
            smart_text = smart_text.rstrip() + faq_block

    # 모바일 가독성용 최소 정리: 과도한 연속 빈 줄만 정리합니다.
    smart_text = re.sub(r"\n{3,}", "\n\n", smart_text).strip()

    # 네이버 스마트에디터에 바로 붙여넣을 수 있도록 태그를 # 포함 형태로 함께 넣습니다.
    # 쉼표만 있는 기존 출력은 사용하지 않고, 각 태그 앞에 #을 붙여 공백으로 구분합니다.
    tags = []
    for raw_tag in (article.get("tags", []) or []):
        tag = str(raw_tag).strip().lstrip("#").replace(",", "")
        if tag and tag not in tags:
            tags.append(tag)
    if tags:
        smart_text = smart_text.rstrip() + "\n\n" + " ".join(f"#{tag}" for tag in tags)

    smart_text = smart_text.strip() + "\n"

    st.code(smart_text, language=None)

    if article.get("toc_included"):
        st.info(f"목차 포함: {article.get('toc_reason', '')}")
    else:
        st.caption(f"목차 생략: {article.get('toc_reason', '')}")

    if article.get("gap_coverage"):
        st.markdown("### 콘텐츠 GAP 반영 현황")
        for g in article.get("gap_coverage", []):
            status = g.get("status", "")
            icon = "✅" if status == "반영" else ("🟡" if status == "부분 반영" else "🔴")
            st.write(f"{icon} {g.get('gap','')} — {status}")
            if g.get("evidence"):
                st.caption(f"본문 근거: {g.get('evidence')}")

    if article.get("faq"):
        st.markdown("### FAQ")
        for item in article["faq"]:
            with st.expander(item.get("question", "")):
                st.write(item.get("answer", ""))

    st.markdown("### 🔗 공식 출처 / 신청·조회·예약 링크")
    official_sources = article.get("official_sources", []) or analysis.get("official_sources", [])
    if official_sources:
        st.caption("본문에서는 자격·신청·예약 등 해당 내용을 설명한 위치 바로 아래에 관련 링크를 배치하고, 이 영역에서는 전체 공식 출처를 모아 확인할 수 있습니다.")
        for idx, src in enumerate(official_sources, 1):
            name = src.get("name", "공식 페이지")
            purpose = src.get("purpose", "")
            url = str(src.get("url", "")).strip()
            st.markdown(f"**{name}**")
            if purpose:
                st.caption(purpose)
            if url:
                try:
                    st.link_button("공식 페이지 확인", url, key=f"article_official_{idx}")
                except Exception:
                    st.markdown(f"[공식 페이지 확인]({url})")
            actions = src.get("actions", []) or []
            if actions:
                cols = st.columns(min(len(actions), 3))
                for j, action in enumerate(actions[:3]):
                    label = str(action.get("label", "확인하기")).strip() or "확인하기"
                    action_url = str(action.get("url", "")).strip()
                    if not action_url:
                        continue
                    with cols[j]:
                        try:
                            st.link_button(label, action_url, key=f"article_action_{idx}_{j}")
                        except Exception:
                            st.markdown(f"[{label}]({action_url})")
    else:
        st.caption("확인된 공식 출처가 없습니다.")
    if article.get("coupang_link_needed"):
        st.info("제품 추천 글이라 쿠팡파트너스 링크 1개 슬롯을 선택적으로 사용할 수 있습니다. 실제 파트너스 URL은 사용자가 확인 후 직접 입력하세요.")
        st.caption(article.get("coupang_link_reason", "제품 구매 의도 때문에 선택적으로 제안된 슬롯입니다."))
    else:
        st.caption("이 글에는 쿠팡파트너스 링크가 필수가 아닙니다.")

    st.markdown("### 태그")
    display_tags = []
    for raw_tag in (article.get("tags", []) or []):
        tag = str(raw_tag).strip().lstrip("#").replace(",", "")
        if tag and tag not in display_tags:
            display_tags.append(tag)
    st.code(" ".join(f"#{tag}" for tag in display_tags), language=None)
    st.caption("스마트에디터용 본문 복사 영역에도 위 태그가 # 포함 형태로 함께 들어갑니다.")

    score, checks, count, gaps, gap_label = seo_check(article, analysis)
    st.divider()
    st.subheader("5. SEO 최종 체크")
    st.metric("내부 품질 점수", f"{score}/100")
    st.caption(f"※ 네이버가 제공하는 실제 점수가 아니라, 이 도구의 내부 체크리스트 점수입니다. 메인 키워드 본문 등장 횟수: {count}회")
    for name, ok in checks.items():
        st.write(("✅ " if ok else "⚠️ ") + name)

    output = {
        "analysis": analysis,
        "article": article,
        "seo_score": score,
        "seo_checks": checks,
    }
    st.download_button(
        "💾 분석 결과 JSON 저장",
        data=json.dumps(output, ensure_ascii=False, indent=2),
        file_name=f"{analysis.get('keyword','keyword')}_content_analysis.json",
        mime="application/json",
    ) 
