
import os
import re
import json
from datetime import date, timedelta
from urllib.parse import urlparse
import requests
import streamlit as st
from google import genai
from google.genai import types
from dotenv import load_dotenv

load_dotenv()

st.set_page_config(
    page_title="네이버 콘텐츠 기회 분석기 V2.3",
    page_icon="🔎",
    layout="wide",
)

CATEGORIES = ["리뷰", "맛집", "일상", "쇼핑정보", "여행정보", "핫이슈", "기타정보"]
MODEL = os.getenv("GEMINI_MODEL", "gemini-3.6-flash")

def clean_html(text):
    return re.sub(r"<[^>]+>", "", text or "").replace("&quot;", '"').replace("&amp;", "&").strip()

# NAVER API HUB 공통 엔드포인트
NAVER_API_HUB_BASE = "https://naverapihub.apigw.ntruss.com"

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

    지원 검색 엔드포인트: blog, news, webkr, image
    webkr는 sort 파라미터를 보내지 않습니다.
    """
    url = f"{NAVER_API_HUB_BASE}/search/v1/{api}"
    params = {
        "query": query,
        "display": min(max(int(display), 1), 100),
        "format": "json",
    }
    if sort and api in {"blog", "news", "image"}:
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

def current_web_searches(keyword, client_id, client_secret, commercial=False):
    """시의성이 강한 키워드를 위해 일반 웹검색 외에 공식/최신 검색을 보강합니다."""
    queries = [f"{keyword} 공식", f"{keyword} 최신"]
    if commercial:
        queries.append(f"{keyword} 할인 쿠폰")
    merged = []
    for q in queries:
        try:
            result = naver_search("webkr", q, client_id, client_secret, display=10, sort="sim")
            merged.extend(result.get("items", []))
        except Exception:
            continue
    return dedupe_search_items(merged)[:30]

def fetch_source_pages(items, limit=5):
    """웹검색 결과 중 상위 후보의 실제 페이지 내용을 짧게 확인합니다.
    검색 결과만 보고 최신 할인율/코드/기간을 만들어내지 않도록 하기 위한 보강 단계입니다.
    """
    pages = []
    for item in (items or [])[:limit]:
        url = (item.get("link") or item.get("originallink") or "").strip()
        if not url or not url.startswith(("http://", "https://")):
            continue
        try:
            r = requests.get(
                url, timeout=10,
                headers={"User-Agent": "Mozilla/5.0 (compatible; ContentAnalyzer/2.3)"},
            )
            if not r.ok:
                continue
            raw = r.text
            raw = re.sub(r"<script[\s\S]*?</script>", " ", raw, flags=re.I)
            raw = re.sub(r"<style[\s\S]*?</style>", " ", raw, flags=re.I)
            text = clean_html(raw)
            text = re.sub(r"\s+", " ", text).strip()
            if text:
                pages.append({
                    "title": clean_html(item.get("title", "")),
                    "url": url,
                    "text": text[:7000],
                })
        except Exception:
            continue
    return pages

ANALYSIS_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "search_intent": {"type": "STRING"},
        "competition": {"type": "STRING"},
        "opportunity": {"type": "STRING"},
        "trend_interpretation": {"type": "STRING"},
        "related_keywords": {"type": "ARRAY", "items": {"type": "STRING"}},
        "long_tail_keywords": {"type": "ARRAY", "items": {"type": "STRING"}},
        "title_patterns": {"type": "ARRAY", "items": {"type": "STRING"}},
        "recommended_titles": {
            "type": "ARRAY",
            "items": {
                "type": "OBJECT",
                "properties": {
                    "title": {"type": "STRING"},
                    "angle": {"type": "STRING"},
                    "why": {"type": "STRING"}
                },
                "required": ["title", "angle", "why"]
            }
        },
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
        "home_feed_angle": {"type": "STRING"},
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
        "recommended_source_post": {"type": "STRING"}
    },
    "required": [
        "search_intent", "competition", "opportunity", "trend_interpretation",
        "related_keywords", "long_tail_keywords", "title_patterns", "recommended_titles",
        "current_source_facts", "freshness_warning",
        "content_gaps", "home_feed_angle", "recommended_strategy",
        "strategy_reason", "recommended_outline", "existing_content_asset_summary",
        "existing_content_relevance", "existing_content_strengths",
        "existing_content_missing_or_extendable", "current_time_extension_points",
        "new_content_opportunities", "recommended_new_keywords",
        "cannibalization_note", "recommended_source_post"
    ],
}

ARTICLE_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "seo_title": {"type": "STRING"},
        "home_title": {"type": "STRING"},
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
        "image_plan": {
            "type": "ARRAY",
            "items": {
                "type": "OBJECT",
                "properties": {
                    "position": {"type": "STRING"},
                    "purpose": {"type": "STRING"},
                    "prompt": {"type": "STRING"},
                },
                "required": ["position", "purpose", "prompt"],
            },
        },
        "tags": {"type": "ARRAY", "items": {"type": "STRING"}},
    },
    "required": [
        "seo_title", "home_title", "thumbnail_text", "meta_description",
        "main_keyword", "secondary_keywords", "long_tail_keywords",
        "outline", "toc_included", "toc_reason", "gap_coverage", "body_markdown", "faq", "image_plan", "tags",
    ],
}

def gemini_json(client, prompt, schema, max_tokens=8000, retries=3):
    """Gemini 호출. 일시적 429/503만 제한적으로 재시도하고 일일 quota 초과는 즉시 중단합니다."""
    import time
    import random

    last_error = None
    for attempt in range(retries):
        try:
            resp = client.models.generate_content(
                model=MODEL,
                contents=prompt,
                config=types.GenerateContentConfig(
                    max_output_tokens=max_tokens,
                    response_mime_type="application/json",
                    response_schema=schema,
                ),
            )
            text = (resp.text or "").strip()
            return json.loads(text)
        except Exception as e:
            last_error = e
            message = str(e)
            upper = message.upper()

            # 일일/프로젝트 quota 초과는 기다리거나 재시도해도 해결되지 않으므로 즉시 중단합니다.
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
                # 서버가 제시한 RetryInfo가 있으면 우선 사용하고, 없으면 지수 백오프를 사용합니다.
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
1. 현재 키워드와 네이버 검색 데이터(블로그·검색트렌드·뉴스·웹문서·이미지)를 종합해 콘텐츠 전략을 세우세요.
2. 시의성이 있는 키워드는 '현재 웹검색 보강 결과'와 실제 페이지 확인 결과를 최우선으로 참고하세요.
3. 공식 사이트/공식 브랜드 페이지가 확인되면 제3자 블로그보다 우선하세요.
4. 현재 확인되지 않는 할인율, 프로모션 기간, 할인코드, 카드사 제휴, 가격, 이벤트명은 절대 추측해서 쓰지 마세요.
5. 검색 결과 제목만 보고 사실을 확정하지 말고, 제공된 source_pages/current_source_facts에서 근거가 있는 내용만 현재 사실로 취급하세요.
6. 근거가 부족하면 '현재 확인 필요'로 표시하고 글에 단정적으로 넣지 마세요.
7. '2026년 9월'처럼 날짜가 중요한 제목은 현재 기준일과 실제 확인된 기간이 맞는 경우에만 사용하세요.
8. 추천 제목은 실제로 작성할 글의 방향을 결정하는 단계입니다. 제목과 본문이 서로 다른 주제로 흘러가지 않도록 검색의도와 최신 근거를 반영해 3개를 만드세요.

기존 콘텐츠 자산 원칙:
- 내 블로그 주소/ID는 이 앱에서 내 블로그 전체를 자동 검색하기 위한 값으로 사용하지 않습니다.
- '특정 기존글 URL(선택)'이 입력된 경우에만 그 글을 내 콘텐츠 자산으로 직접 읽고 비교하세요.
- 특정 기존글 URL이 입력되지 않았다면 기존 글 자산을 추정하거나 자동 검색했다고 가정하지 마세요. 이 경우 추천 전략은 반드시 NEW_KEYWORD입니다.
- 벤치마크 블로그 URL은 내 기존글 URL과 완전히 별개의 입력입니다. 경쟁/참고 콘텐츠 분석용입니다.

전략 판단 규칙:
- 기존글 URL이 없으면: NEW_KEYWORD
- 기존글 URL이 있으면 UPDATE_EXISTING / NEW_DERIVED / NEW_UNRELATED / NO_OPPORTUNITY 중 판단
- 시간 경과에 따른 새 가치(실제 사용 후 평가, 재구매, 현재 추천 기준 등)를 검토하되 입력에 없는 경험은 만들지 마세요.

제목 생성 규칙:
- 추천 제목은 정확히 3개를 제시하세요.
- 세 제목은 같은 내용을 말하더라도 검색의도/클릭각도를 조금씩 달리할 수 있습니다.
- 그러나 제목에서 약속한 핵심 내용이 본문에서 반드시 실제로 다뤄질 수 있어야 합니다.
- 제목에 '총정리', '최신', '2026년 9월', '할인코드', '최대 XX%', '특정 카드사' 등의 최신성/수치 표현을 넣을 경우 반드시 제공된 최신 근거가 있어야 합니다.
- 근거가 없으면 그런 표현을 제목에서 빼세요.

현재 정보 검증:
- current_source_facts는 '현재 확인된 사실' 후보입니다.
- source_pages는 실제 웹페이지에서 추출한 참고 내용입니다.
- current_source_facts에는 최소한 글에 실제로 사용할 가치가 높은 사실만 넣고, source_url을 반드시 남기세요.
- 최신 정보가 부족하면 freshness_warning에 명확히 적으세요.

[입력]
{json.dumps(payload, ensure_ascii=False, indent=2)}

JSON으로만 답하세요.
"""
    return gemini_json(client, prompt, ANALYSIS_SCHEMA, 8500)


def write_with_ai(client, analysis_payload, writing_options):
    selected_title = writing_options.get("selected_title", "").strip()
    prompt = f"""
당신은 네이버 블로그용 SEO 콘텐츠 작가이자 팩트체크 편집자입니다.
아래 분석 결과와 '선택된 제목'을 기준으로 실제 발행 가능한 한국어 글을 작성하세요.

가장 중요한 원칙:
1) 선택된 제목이 글의 계약(약속)입니다. 본문 전체가 그 제목의 검색의도와 약속을 정확히 충족해야 합니다.
2) 제목에 없는 새로운 주제로 본문이 옆길로 새지 않도록 하세요.
3) 현재 시점의 할인율, 프로모션 기간, 할인코드, 카드 제휴, 가격, 이벤트명 등은 analysis의 current_source_facts 또는 source_pages에서 근거가 확인된 것만 작성하세요.
4) 근거가 없는 최신 정보는 절대로 추측하거나 예전 정보로 채우지 마세요. '현재 확인 필요'라고 표시하거나 해당 내용을 제외하세요.
5) 특히 할인코드/쿠폰 글에서는 오래된 코드나 과거 이벤트를 현재 진행 중인 것처럼 쓰지 마세요.
6) 공식 사이트가 확인된 경우 공식 출처를 우선합니다. 제3자 블로그의 숫자·코드만으로 현재 사실을 확정하지 마세요.
7) 글 작성 시 분석 단계에서 확인되지 않은 카드사 제휴, 할인율, 세일 기간, 가격, 코드 등을 추가하지 마세요.
8) 사용자가 직접 경험했다고 주어지지 않은 내용을 1인칭 체험처럼 쓰지 마세요.
9) 네이버 모바일 화면을 최우선으로 고려해 짧은 문단과 명확한 소제목을 사용하세요.
10) 한 문단은 기본 1~2문장, 최대 3문장을 넘기지 마세요.
11) 문단 사이에는 빈 줄 1줄을 두세요.
12) 한 문장은 40~60자 안팎을 우선하고 긴 문장은 자연스럽게 나누세요.
13) 친근한 존댓말(~해요, ~랍니다)을 기본으로 하세요.
14) 본문은 반드시 자연스러운 서론으로 시작하고 첫 H2/H3보다 서론이 먼저 와야 합니다.
15) 목차는 모든 글에 강제로 넣지 말고 검색의도와 분량에 따라 판단하세요.
16) 분석의 콘텐츠 GAP은 실제 본문에 반영하고 gap_coverage에 기록하세요.

카테고리: {writing_options["category"]}
톤: {writing_options["tone"]}
목표 분량: {writing_options["length"]}
메인 키워드: {analysis_payload["keyword"]}
추천 전략: {analysis_payload["recommended_strategy"]}
선택된 제목: {selected_title}
제목 선택 이유/각도: {writing_options.get("selected_title_reason", "")}

[최신 근거 데이터]
{json.dumps(analysis_payload.get("current_source_facts", []), ensure_ascii=False, indent=2)}

[실제 웹페이지 확인 데이터]
{json.dumps(analysis_payload.get("source_pages", []), ensure_ascii=False, indent=2)}

[전체 분석 데이터]
{json.dumps(analysis_payload, ensure_ascii=False, indent=2)}

전략별 작성 규칙:
- NEW_KEYWORD: 기존글을 전제로 하지 않고 현재 키워드의 검색의도·경쟁·최신 근거·콘텐츠 GAP을 중심으로 새 글을 작성하세요.
- UPDATE_EXISTING: 기존 글의 핵심 정보를 유지하되 현재 시점에 필요한 내용을 근거와 함께 보강하세요.
- NEW_DERIVED: 기존 글을 복붙/요약하지 말고 새로운 검색의도와 현재 가치를 중심으로 작성하세요.
- NEW_UNRELATED: 기존 자산을 억지로 연결하지 말고 새 주제로 작성하세요.

제목 충실도 규칙:
- 선택된 제목의 핵심 키워드와 약속을 본문 첫 부분부터 일관되게 유지하세요.
- 선택된 제목과 무관한 과거 프로모션이나 일반적인 여행 팁을 분량 채우기용으로 추가하지 마세요.
- 예를 들어 '2026년 9월 트립닷컴 할인코드'라면 현재 9월에 실제 확인된 코드/혜택/기간/적용조건을 중심으로 작성하고, 근거 없는 카드 제휴나 과거 세일을 넣지 마세요.
- 특정 세일 기간을 제목이나 목차에 넣었다면 본문에 실제 기간과 혜택을 명확히 설명하세요. 근거가 없다면 해당 표현을 사용하지 마세요.

이미지 계획 규칙:
- 기본은 '실사 사진'입니다.
- 이미지에 텍스트를 넣는 인포그래픽을 기본으로 만들지 마세요.
- 기본 프롬프트에는 'no text, no typography, no infographic, no watermark'를 포함하세요.
- 제품/여행/음식/생활 장면을 실제 촬영한 것처럼 표현하세요.
- 인포그래픽이 꼭 필요한 데이터 비교 글에서만 선택적으로 사용하고, 그 경우에도 이미지 안에 정확한 한글 문구를 AI가 임의 생성하도록 요구하지 마세요.

검색 요약문 규칙:
- meta_description은 '검색 요약문(참고용)'으로 작성하세요.
- 네이버 스마트에디터에 별도 입력하는 필수 필드라고 가정하지 마세요.
- 본문을 복사한다고 메타디스크립션이 자동 입력된다고 설명하지 마세요.

썸네일 문구:
- 실제 썸네일 이미지 제작에 사용할 짧은 문구를 작성하세요.

body_markdown에는 글 제목을 반복하지 말고 H2/H3 마크다운을 사용하세요.
body_markdown 시작은 2~4개의 서론 문단이어야 합니다.
FAQ는 3~5개.
image_plan은 실제 제작 가능한 이미지 계획을 작성하세요.
이미지 프롬프트는 영어로 작성하되 이미지 자체에 글자를 생성하도록 요구하지 마세요.

JSON으로만 답하세요.
"""
    return gemini_json(client, prompt, ARTICLE_SCHEMA, 10000)


def seo_check(article, analysis):
    text = article.get("body_markdown", "")
    keyword = analysis.get("keyword", "")
    count = text.count(keyword) if keyword else 0
    gaps = article.get("gap_coverage", []) or []
    valid_status = {"반영", "완료", "충분히 반영", "해당 없음", "없음"}
    gap_statuses = [str(x.get("status", "")).strip() for x in gaps]
    if not analysis.get("content_gaps"):
        gap_label = "콘텐츠 GAP 없음"
        gap_ok = True
    elif gaps and all(any(v in st for v in valid_status) for st in gap_statuses):
        gap_label = f"콘텐츠 GAP 반영 완료 ({len(gaps)}/{len(gaps)})"
        gap_ok = True
    elif gaps:
        done = sum(any(v in st for v in valid_status) for st in gap_statuses)
        gap_label = f"콘텐츠 GAP 일부 반영 ({done}/{len(gaps)})"
        gap_ok = False
    else:
        gap_label = "콘텐츠 GAP 보완 필요"
        gap_ok = False
    checks = {
        "메인 키워드 반영": count >= 2,
        "검색의도 반영": bool(analysis.get("search_intent")),
        gap_label: gap_ok,
        "서론 포함": bool(re.search(r"(^|\n)\s*(서론|들어가며|먼저|요즘|최근)", text, re.I)) or len(text.strip().split("\n\n")) >= 2,
        "FAQ 포함": len(article.get("faq", [])) >= 3,
        "이미지 계획 포함": len(article.get("image_plan", [])) >= 3,
        "홈판 제목 별도 생성": bool(article.get("home_title")),
        "썸네일 문구 생성": bool(article.get("thumbnail_text")),
    }
    score = round(sum(checks.values()) / len(checks) * 100)
    return score, checks, count, gaps, gap_label

def build_analysis_payload(keyword, category, trend, blog, news, web, images, shopping, benchmark, specific_post=None, blog_id="", current_web=None, source_pages=None):
    has_specific = bool(specific_post and specific_post.get("status") not in (None, "not_provided"))
    return {
        "keyword": keyword,
        "category": category,
        "trend": trend_summary(trend),
        "blog_results": compact_results(blog.get("items", []), ["title", "description", "bloggername", "bloggerlink", "postdate"]),
        "news_results": compact_results(news.get("items", []), ["title", "description", "originallink", "pubDate"]),
        "web_results": compact_results(web.get("items", []), ["title", "description", "link"]),
        "current_web_results": compact_results(current_web or [], ["title", "description", "link"]),
        "source_pages": source_pages or [],
        "current_date": date.today().isoformat(),
        "image_results": compact_results(images.get("items", []), ["title", "link", "thumbnail"]),
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

def fetch_benchmark(url):
    if not url.strip():
        return {"status": "not_provided"}
    try:
        r = requests.get(
            url.strip(),
            timeout=15,
            headers={"User-Agent": "Mozilla/5.0 (compatible; ContentAnalyzer/1.0)"},
        )
        r.raise_for_status()
        text = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", r.text)).strip()
        return {"status": "ok", "url": url.strip(), "text": text[:7000]}
    except Exception as e:
        return {"status": "failed", "url": url.strip(), "error": str(e)}


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
    ("naver_id", "NAVER_CLIENT_ID", ("NAVER_CLIENT_ID", "naver.client_id")),
    ("naver_secret", "NAVER_CLIENT_SECRET", ("NAVER_CLIENT_SECRET", "naver.client_secret")),
    ("own_blog", "NAVER_BLOG_ID", ("NAVER_BLOG_ID", "naver.blog_id")),
]:
    if _key not in st.session_state or not st.session_state[_key]:
        st.session_state[_key] = _initial_credential(_env, *_secret_paths)

if "credentials_saved" not in st.session_state:
    st.session_state.credentials_saved = False
if "connection_test" not in st.session_state:
    st.session_state.connection_test = None

with st.sidebar:
    st.header("⚙️ 설정")
    st.caption("NAVER API HUB 방식으로 연결합니다. API 키는 이 브라우저 세션에서만 사용하며 GitHub에는 저장하지 않습니다.")

    with st.form("api_settings_form", clear_on_submit=False):
        st.text_input(
            "Gemini API Key",
            type="password",
            key="gemini_key",
        )
        st.text_input(
            "Naver Client ID",
            key="naver_id",
        )
        st.text_input(
            "Naver Client Secret",
            type="password",
            key="naver_secret",
        )
        save_settings = st.form_submit_button(
            "💾 설정 저장",
            type="primary",
            use_container_width=True,
        )

    if save_settings:
        # 저장 버튼을 누른 순간 현재 입력값을 그대로 세션에 확정합니다.
        st.session_state.credentials_saved = True
        st.session_state.connection_test = None
        st.success("설정이 현재 세션에 저장됐어요.")

    if st.session_state.credentials_saved:
        st.caption("🟢 저장된 API 설정을 사용 중입니다.")

    if st.session_state.naver_id and st.session_state.naver_secret:
        naver_source = "Streamlit Secrets/환경변수에서 불러온 기본값" if not st.session_state.credentials_saved else "설정 화면에서 저장한 현재 세션값"
        st.caption(f"네이버 인증값 출처: {naver_source}")

    if st.session_state.gemini_key and st.session_state.naver_id and st.session_state.naver_secret:
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
    st.divider()
    st.subheader("작성 기본값")
    tone = st.selectbox("말투", ["친근한 정보형", "담백한 정보형", "전문적인 정보형"])
    length = st.selectbox("목표 분량", ["약 2500자", "약 4000자", "약 6000자"], index=1)

# 실제 API 호출에는 세션에 저장된 값을 사용합니다.
gemini_key = st.session_state.gemini_key
naver_id = st.session_state.naver_id
naver_secret = st.session_state.naver_secret
own_blog = st.session_state.own_blog

if not gemini_key or not naver_id or not naver_secret:
    st.title("🔎 네이버 콘텐츠 기회 분석기 V2.3")
    st.info("왼쪽 사이드바에 Gemini API Key와 Naver Client ID / Secret을 입력하면 시작할 수 있어요.")
    st.markdown("""
### 이 버전에서 하는 일
1. Creator Advisor에서 직접 선별한 키워드를 입력
2. 네이버 검색어 트렌드 분석
3. 블로그·뉴스·웹·이미지 검색 분석
4. 필요하면 쇼핑인사이트 분석
5. 선택한 벤치마크 블로그가 있으면 참고 콘텐츠를 분석
6. 특정 기존글 URL을 입력한 경우에만 내 콘텐츠 자산과 비교
7. 검색용 제목 + 홈판용 제목 + 본문 + 이미지 계획까지 작성
""")
    st.stop()

client = genai.Client(api_key=gemini_key)

st.title("🔎 네이버 콘텐츠 기회 분석기")
st.caption("Creator Advisor에서 직접 선별한 키워드를 넣으면, 네이버 데이터 → 콘텐츠 GAP → (선택) 벤치마크/기존글 비교 → 검색·홈판 전략 → 글 작성까지 연결합니다.")

if "analysis" not in st.session_state:
    st.session_state.analysis = None
if "article" not in st.session_state:
    st.session_state.article = None
if "selected_title" not in st.session_state:
    st.session_state.selected_title = ""
if "selected_title_reason" not in st.session_state:
    st.session_state.selected_title_reason = ""

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

with st.expander("선택 옵션", expanded=False):
    commercial = st.checkbox("상품/구매 의도가 있는 키워드", value=False)
    shopping_category = st.text_input(
        "네이버 쇼핑 카테고리 코드(선택)",
        placeholder="예: 50000000",
        disabled=not commercial,
    )
    benchmark_url = st.text_input(
        "벤치마크 블로그 URL(선택)",
        placeholder="분석하고 싶은 공개 URL이 있다면 입력",
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
            st.write("① 검색어 트렌드")
            trend = naver_trend(keyword, naver_id, naver_secret)

            st.write("② 블로그 검색")
            blog = naver_search("blog", keyword, naver_id, naver_secret, display=30, sort="sim")

            st.write("③ 뉴스 검색")
            news = naver_search("news", keyword, naver_id, naver_secret, display=10, sort="date")

            st.write("④ 웹문서 검색")
            web = naver_search("webkr", keyword, naver_id, naver_secret, display=10, sort="sim")

            st.write("⑤ 최신·공식 웹문서 보강 검색")
            current_web = current_web_searches(keyword, naver_id, naver_secret, commercial=commercial)
            source_pages = fetch_source_pages(current_web, limit=5)

            st.write("⑥ 이미지 검색")
            images = naver_search("image", keyword, naver_id, naver_secret, display=10, sort="sim")

            st.write("⑦ 지정 기존글 확인(선택)")
            blog_id = extract_blog_id(own_blog)
            specific_post = fetch_naver_post(specific_existing_url) if specific_existing_url.strip() else {"status": "not_provided"}

            shopping = None
            if commercial and shopping_category.strip():
                st.write("⑧ 쇼핑인사이트")
                shopping = naver_shopping_trend(
                    keyword, shopping_category.strip(), naver_id, naver_secret
                )

            benchmark = fetch_benchmark(benchmark_url)
            payload = build_analysis_payload(
                keyword, category, trend, blog, news, web, images,
                shopping, benchmark, specific_post=specific_post, blog_id=blog_id,
                current_web=current_web, source_pages=source_pages
            )

            st.write("⑨ AI 콘텐츠 전략 분석")
            ai = analyze_with_ai(client, payload)
            payload.update(ai)

            # 특정 기존글 URL이 없으면 내 블로그 자산 비교를 수행하지 않으므로
            # AI가 임의로 기존글 기반 전략을 선택하지 못하도록 전략을 고정합니다.
            if not specific_existing_url.strip():
                payload["recommended_strategy"] = "NEW_KEYWORD"
                payload["recommended_source_post"] = "없음"
                payload["existing_content_asset_summary"] = "특정 기존글 URL이 입력되지 않아 분석 대상인 내 기존 콘텐츠 자산이 없습니다."
                payload["existing_content_relevance"] = "기존글 비교를 수행하지 않고 현재 키워드 자체의 검색 의도와 콘텐츠 GAP을 기준으로 분석했습니다."
                payload["existing_content_strengths"] = []
                payload["existing_content_missing_or_extendable"] = []
                payload["current_time_extension_points"] = []
                payload["new_content_opportunities"] = []
                payload["cannibalization_note"] = "기존글 URL을 지정하지 않았으므로 특정 기존글과의 자기잠식 비교는 수행하지 않았습니다."

            st.session_state.analysis = payload
            titles = payload.get("recommended_titles", []) or []
            # 화면에서는 항상 최대 3개 후보만 제시합니다.
            payload["recommended_titles"] = titles[:3]
            titles = payload["recommended_titles"]
            if titles:
                st.session_state.selected_title = titles[0].get("title", "")
                st.session_state.selected_title_reason = titles[0].get("why", "")
            else:
                st.session_state.selected_title = ""
                st.session_state.selected_title_reason = ""
            st.session_state.article = None
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

    st.markdown("### 🎯 글 작성용 추천 제목 3가지")
    st.caption("제목 패턴만 보고 글을 쓰지 않고, 아래에서 실제 작성할 제목을 하나 선택합니다. 선택한 제목이 글의 핵심 방향이 됩니다.")
    title_options = analysis.get("recommended_titles", []) or []
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
        else:
            st.warning("추천 제목을 생성하지 못했습니다. 제목 패턴을 참고해 직접 제목을 선택해 주세요.")
    else:
        st.warning("추천 제목이 없습니다. 제목 패턴을 참고해 직접 제목을 선택해 주세요.")

    if analysis.get("freshness_warning"):
        st.warning("⚠️ 최신 정보 확인: " + analysis.get("freshness_warning"))
    if analysis.get("current_source_facts"):
        with st.expander("🔎 현재 시점 근거 정보", expanded=False):
            for fact in analysis.get("current_source_facts", []):
                st.markdown(f"**{fact.get('fact','')}**")
                st.caption(f"{fact.get('source_type','')} · {fact.get('source_title','')} · 확인일 {fact.get('verified_date','')}")
                if fact.get("source_url"):
                    st.write(fact.get("source_url"))

    tabs = st.tabs(["🔑 키워드", "📊 경쟁 콘텐츠", "🧩 콘텐츠 GAP", "📚 내 기존글", "🏠 홈판 전략"])

    with tabs[0]:
        st.markdown("### 연관 키워드")
        st.write(", ".join(analysis.get("related_keywords", [])) or "-")
        st.markdown("### 롱테일 키워드")
        st.write(", ".join(analysis.get("long_tail_keywords", [])) or "-")
        st.markdown("### 제목 패턴")
        for x in analysis.get("title_patterns", []):
            st.write(f"• {x}")

    with tabs[1]:
        st.markdown("### 검색 결과에서 반복되는 주제")
        for x in analysis.get("recommended_outline", []):
            st.write(f"• {x}")
        st.markdown("### 경쟁 수준")
        st.write(analysis.get("competition", "-"))
        st.caption("이 분석은 네이버 블로그 검색 API 결과의 제목·설명 등을 기반으로 한 요약이며, 경쟁 블로그 전체 본문을 직접 분석한 결과가 아닙니다.")

    with tabs[2]:
        for x in analysis.get("content_gaps", []):
            st.write(f"🧩 {x}")

    with tabs[3]:
        st.markdown("### 기존 콘텐츠 자산 분석")
        st.caption("🔎 " + analysis.get("own_search_note", ""))
        specific = analysis.get("specific_existing_post", {}) or {}
        if specific.get("status") == "ok":
            st.success("🎯 지정한 기존글을 콘텐츠 자산으로 분석했습니다.")
            st.markdown(f"**{specific.get('title') or specific.get('url','')}**")
            st.caption(specific.get("url", ""))
        elif specific.get("status") == "failed":
            st.warning("입력한 특정 기존글 URL을 직접 읽지 못했습니다. 기존글 비교 없이 현재 키워드 기준으로 작성할 수 있습니다.")
        else:
            st.info("특정 기존글 URL이 입력되지 않았습니다. 이 탭의 기존글 비교 기능은 선택사항이며, 현재 키워드 분석과 글 작성은 그대로 진행됩니다.")
        st.markdown("### 기존 글에서 이미 가진 자산")
        st.write(analysis.get("existing_content_asset_summary", "-"))
        st.markdown("### 현재 키워드와의 연결성")
        st.write(analysis.get("existing_content_relevance", "-"))
        st.markdown("### 기존 글의 강점")
        for x in analysis.get("existing_content_strengths", []):
            st.write(f"• {x}")
        st.markdown("### 지금 새로 확장할 수 있는 포인트")
        for x in analysis.get("current_time_extension_points", []):
            st.write(f"🆕 {x}")
        st.markdown("### 추천 신규 콘텐츠 기회")
        for x in analysis.get("new_content_opportunities", []):
            st.write(f"**{x.get('topic','')}** · {x.get('keyword','')}")
            st.caption(f"검색의도: {x.get('search_intent','')} · {x.get('reason','')}")
        st.markdown("### 자기잠식 주의")
        st.write(analysis.get("cannibalization_note", "-"))
        if not has_specific_asset:
            st.caption("내 블로그 전체 자동 검색은 V2.3에서 제거했습니다. 기존글과 비교하려면 사이드바의 '특정 기존글 URL(선택)'에 원하는 글만 입력하세요.")

    with tabs[4]:
        st.markdown("### 홈판 콘텐츠 각도")
        st.write(analysis.get("home_feed_angle", "-"))
        st.markdown("### 추천 이유")
        st.write(analysis.get("strategy_reason", "-"))

    st.divider()
    st.subheader("3. 글 작성")
    if not st.session_state.get("selected_title"):
        st.warning("먼저 글 작성에 사용할 추천 제목을 하나 선택해 주세요.")
    if st.button("✍️ 선택한 제목으로 글 작성", type="primary", use_container_width=True):
        if not st.session_state.get("selected_title"):
            st.error("글 작성 전에 추천 제목을 하나 선택해 주세요.")
            st.stop()
        with st.spinner("검색 의도와 콘텐츠 GAP을 반영해 글을 작성하고 있어요..."):
            try:
                selected_title = st.session_state.get("selected_title", "").strip()
                article = write_with_ai(
                    client,
                    analysis,
                    {"category": category, "tone": tone, "length": length,
                     "selected_title": selected_title,
                     "selected_title_reason": st.session_state.get("selected_title_reason", "")},
                )
                # 선택한 제목을 최종 검색용 제목으로 고정해 제목과 본문의 방향이 어긋나는 것을 방지합니다.
                if selected_title:
                    article["seo_title"] = selected_title
                st.session_state.article = article
            except Exception as e:
                st.error(f"글 작성 중 오류가 발생했습니다: {e}")

article = st.session_state.article

if article:
    st.divider()
    st.subheader("4. 최종 콘텐츠")

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
    st.markdown(article.get("body_markdown", ""))

    st.markdown("### 네이버 스마트에디터용 본문")
    st.caption("네이버 모바일 기준으로 문단·문장 호흡을 짧게 정리한 본문입니다. 복사 아이콘으로 복사한 뒤 네이버 스마트에디터에 Ctrl+V로 붙여넣을 수 있습니다.")
    smart_text = re.sub(r"^#{1,6}\s*", "", article.get("body_markdown", ""), flags=re.MULTILINE)
    smart_text = re.sub(r"\*\*(.*?)\*\*", r"\1", smart_text)
    smart_text = re.sub(r"\[(.*?)\]\([^)]*\)", r"\1", smart_text)

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
        smart_text = smart_text.rstrip() + "\n" + "\n".join(faq_lines).rstrip() + "\n"

    # 모바일 가독성용 최소 정리: 과도한 연속 빈 줄만 정리합니다.
    smart_text = re.sub(r"\n{3,}", "\n\n", smart_text).strip() + "\n"

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

    if article.get("faq"):
        st.markdown("### FAQ")
        for item in article["faq"]:
            with st.expander(item.get("question", "")):
                st.write(item.get("answer", ""))

    st.markdown("### 이미지 삽입 계획")
    st.caption("기본값은 실사 사진 중심입니다. 이미지 안의 글자·인포그래픽은 사실 전달에 꼭 필요한 경우에만 선택적으로 사용합니다.")
    for i, item in enumerate(article.get("image_plan", []), 1):
        st.markdown(f"**{i}. {item.get('position','')} — {item.get('purpose','')}**")
        st.code(item.get("prompt", ""), language=None)

    st.markdown("### 태그")
    st.code(", ".join(article.get("tags", [])), language=None)

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
