
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
    page_title="네이버 콘텐츠 기회 분석기 V2.1",
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

def own_blog_matches(blog_items, blog_id):
    """일반 키워드 검색 결과에서 내 블로그 글만 추립니다."""
    if not blog_id:
        return []
    key = blog_id.lower().strip()
    matches = []
    seen = set()
    for item in blog_items or []:
        bloggerlink = (item.get("bloggerlink") or "").lower()
        link = (item.get("link") or "").lower()
        if key in bloggerlink or f"blog.naver.com/{key}" in link:
            unique = item.get("link") or item.get("title")
            if unique not in seen:
                seen.add(unique)
                matches.append(item)
    return matches

def search_own_blog_posts(keyword, blog_id, client_id, client_secret):
    """내 블로그 관련글을 일반 검색 결과에만 의존하지 않고 여러 검색식으로 보완합니다."""
    if not blog_id:
        return [], "내 블로그 주소/ID가 없어 기존 글 검색을 건너뛰었습니다."

    queries = [
        keyword,
        f"{keyword} {blog_id}",
        f"site:blog.naver.com/{blog_id} {keyword}",
    ]
    all_items = []
    seen = set()
    for q in queries:
        try:
            result = naver_search("blog", q, client_id, client_secret, display=100, sort="sim")
            for item in result.get("items", []):
                link = item.get("link") or ""
                title = item.get("title") or ""
                key = link or title
                if key not in seen:
                    seen.add(key)
                    all_items.append(item)
        except Exception:
            continue

    matches = own_blog_matches(all_items, blog_id)
    if matches:
        note = f"내 블로그 관련글 {len(matches)}개를 여러 검색식으로 확인했습니다."
    else:
        note = "현재 네이버 검색 API에서 내 블로그 관련글 후보를 찾지 못했습니다. 검색 결과에 노출되지 않는 오래된 글까지 존재하지 않는다고 단정하지 않습니다."
    return matches, note

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
        "related_keywords", "long_tail_keywords", "title_patterns",
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
    """Gemini 일시적 503/UNAVAILABLE에 대비해 자동 재시도합니다."""
    import time
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
            if "503" not in message and "UNAVAILABLE" not in message and "high demand" not in message:
                raise
            if attempt < retries - 1:
                time.sleep(2 ** attempt * 2)
    raise RuntimeError(f"Gemini가 일시적으로 응답하지 않습니다. 잠시 후 다시 시도해 주세요. 원본 오류: {last_error}")

def analyze_with_ai(client, payload):
    prompt = f"""
당신은 네이버 블로그 콘텐츠 전략가입니다.
핵심 목표는 '현재 키워드'와 '사용자가 과거에 만든 콘텐츠 자산'을 연결해
지금 시점에 어떤 콘텐츠 기회를 만들 수 있는지 판단하는 것입니다.

가장 중요한 관점:
- 내 블로그 주소/ID는 단순히 검색 결과에서 내 글을 찾기 위한 값이 아닙니다.
- 기존 글은 '콘텐츠 자산'입니다. 기존 글의 주제, 경험, 목록, 관찰, 정보, 당시의 한계를 분석하고
  현재 키워드와 연결해 새로운 글을 만들 수 있는지 판단하세요.
- 특정 기존글 URL이 제공되면 그 글을 최우선 원본 자산으로 분석하세요.
- 자동 검색으로 기존 글 후보를 찾았다면 후보도 자산으로 활용하세요.
- 자동 검색에서 찾지 못했다고 해서 기존 글이 없다고 단정하지 마세요.

전략 판단은 반드시 다음 중 하나로 분류하세요:
1. UPDATE_EXISTING: 기존 글과 검색 의도가 거의 같고 최신 정보 보강이 핵심일 때
2. NEW_DERIVED: 기존 글의 경험/주제를 활용하되 현재 검색 의도와 새로운 정보 가치가 달라 별도 신규 글로 만들 때
3. NEW_UNRELATED: 기존 콘텐츠 자산과 연결할 만한 실질적 가치가 없어 완전히 새 글로 만들 때
4. NO_OPPORTUNITY: 제공된 데이터만으로 현재 키워드의 콘텐츠 기회가 낮다고 판단될 때

특히 다음과 같은 '시간이 지나서 새로 생긴 가치'를 적극적으로 검토하세요:
- 실제 사용/섭취 후 평가
- 다시 구매할 것 vs 사지 않을 것
- 재방문/재구매 의향
- 몇 달 사용 후 장단점
- 처음 작성할 당시 알 수 없었던 문제점
- 현재 시점의 추천 기준
- 다음에 다시 간다면 무엇을 고를지
- 당시 목록형 정보에서 현재의 비교/후기/의사결정형 정보로 확장할 수 있는지
단, 입력 데이터에 없는 실제 경험을 사실처럼 만들어내지 마세요. '추가로 확인하면 좋은 경험 포인트'와 '이미 제공된 경험'을 구분하세요.

추가 원칙:
- 네이버 크리에이터 어드바이저의 순위나 절대 검색량을 추정하지 마세요.
- 검색어 트렌드는 상대 관심도 추이입니다.
- 검색 API 블로그 결과만으로 경쟁 글 전체 본문을 안다고 주장하지 마세요.
- 콘텐츠 GAP은 검색 결과와 기존 자산을 비교해 도출하세요.
- 새 글을 추천할 때는 기존 글과의 자기잠식(검색의도 중복) 가능성도 설명하세요.
- 추천 신규 주제는 최소 3개 이상 제시하되, 기존 자산에서 실제로 확장 가능한 것과 완전 신규 주제를 구분하세요.

[입력]
{json.dumps(payload, ensure_ascii=False, indent=2)}

JSON으로만 답하세요.
"""
    return gemini_json(client, prompt, ANALYSIS_SCHEMA, 7500)

def write_with_ai(client, analysis_payload, writing_options):
    prompt = f"""
당신은 네이버 블로그용 SEO 콘텐츠 작가입니다.
아래 분석 결과를 바탕으로 실제 발행 가능한 한국어 정보형 글을 작성하세요.

목표:
1) 검색자가 입력한 키워드의 의도를 정확히 충족
2) 연관/롱테일 검색어를 자연스럽게 반영
3) 네이버 홈 피드에서도 클릭할 이유가 있는 제목과 도입부
4) 과도한 키워드 반복 금지
5) 확인되지 않은 숫자, 효능, 경험, 통계를 지어내지 않기
6) 사용자가 직접 경험했다고 주어지지 않은 내용을 1인칭 체험처럼 쓰지 않기
7) 네이버 모바일 화면을 최우선으로 고려해 짧은 문단과 명확한 소제목을 사용하기
8) 모바일 가독성을 위해 한 문단은 기본 1~2문장으로 구성하고, 3문장을 넘기지 않기
9) 문장과 문장 사이에는 필요하면 빈 줄을 넣어 호흡을 만들고, 핵심 문장은 한 문장만 단독 문단으로 배치하기
10) 문단과 문단 사이에는 반드시 빈 줄 1줄을 두고, 소제목 위·아래에도 빈 줄을 두기
11) 한 문장이 지나치게 길어지지 않도록 40~60자 안팎을 우선하고, 긴 문장은 자연스럽게 2문장으로 나누기
12) 모바일에서 스크롤하며 읽어도 핵심이 바로 보이도록 결론·수치·주의사항·체크포인트는 별도 짧은 문단으로 강조하기
13) 한국어 문체는 친근한 존댓말(~해요, ~랍니다)을 기본으로 하기
14) 본문은 반드시 자연스러운 서론으로 시작하세요. 첫 번째 번호형 소제목이나 H2/H3보다 서론이 먼저 와야 합니다.
15) 홈판용 글이라도 서론을 생략하지 마세요. 홈판에서는 첫 2~4개 문단의 공감·문제제기·궁금증 유발이 중요합니다.
16) 목차는 모든 글에 강제로 넣지 마세요. 카테고리, 검색의도, 글의 예상 길이를 보고 판단하세요. 정보형/여행정보/긴 핫이슈·쇼핑정보 글은 목차를 권장하고, 맛집/일상/짧은 리뷰는 생략할 수 있습니다.
17) 목차를 넣는다면 반드시 '서론 → 목차 → 본론' 순서로 배치하세요.
18) 분석에서 제시된 콘텐츠 GAP이 있다면 본문에 실제로 반영하고, 각 GAP의 반영 여부를 gap_coverage에 기록하세요.

작성 조건:
- 카테고리: {writing_options["category"]} (사용자 블로그 실제 카테고리)
- 톤: {writing_options["tone"]}
- 목표 분량: {writing_options["length"]}
- 메인 키워드: {analysis_payload["keyword"]}
- 추천 전략: {analysis_payload["recommended_strategy"]}
- 기존 콘텐츠 자산: {analysis_payload.get("recommended_source_post", "없음")}
- 새로운 글의 핵심 각도: {analysis_payload.get("strategy_reason", "")}

전략별 작성 규칙:
- UPDATE_EXISTING이면 기존 글의 핵심 정보를 유지하면서 현재 시점에 필요한 내용을 보강하세요.
- NEW_DERIVED이면 기존 글을 단순 복붙/요약/재작성하지 말고, 기존 글에서 파생된 새로운 검색 의도와 현재 시점의 가치에 집중하세요.
  예: 과거의 '무엇을 샀는지' 글이라면 현재는 '직접 써보니 다시 살 것/안 살 것', '몇 달 후 평가', '다음에 다시 사올 것'처럼 의사결정형 콘텐츠로 확장할 수 있습니다.
- NEW_UNRELATED이면 기존 자산을 억지로 연결하지 말고 완전히 새 주제로 작성하세요.
- 입력에 없는 개인 경험은 만들어내지 말고, 필요한 경우 일반 정보형 표현으로 전환하세요.

분석 데이터:
{json.dumps(analysis_payload, ensure_ascii=False, indent=2)}

body_markdown에는 글 제목을 반복하지 말고 H2/H3 마크다운을 사용하세요.
body_markdown의 시작은 번호형 H2/H3가 아니라 2~4개의 서론 문단이어야 합니다.
[네이버 모바일 가독성 필수 규칙]
- 문단 사이에는 빈 줄 1줄을 넣으세요.
- 한 문단은 1~2문장, 최대 3문장을 넘기지 마세요.
- 긴 문장은 짧게 나누고, 한 문단에 하나의 핵심만 담으세요.
- 중요한 한 문장은 단독 문단으로 배치할 수 있습니다.
- H2/H3 소제목 앞뒤에는 빈 줄을 넣으세요.
- 모바일에서 한눈에 읽히도록 문단을 촘촘하게 붙이지 마세요.
- 쉼표를 과도하게 사용해 한 문장을 길게 이어 쓰지 마세요.
목차를 넣는 경우 서론 다음에 '### 목차'를 두고, 그 다음부터 본론 소제목을 시작하세요.
toc_included에는 목차를 실제로 넣었는지 true/false를 기록하고, toc_reason에는 넣거나 생략한 이유를 짧게 적으세요.
content_gaps가 있다면 gap_coverage에 각 GAP을 그대로 적고 status는 '반영' 또는 '부분 반영' 또는 '미반영' 중 하나로만 기록하세요. evidence에는 본문에서 어떻게 반영했는지 적으세요.
FAQ는 3~5개.
이미지 계획은 실제 촬영/스크린샷/인포그래픽 등 현실적으로 제작 가능한 형태로 작성하세요.
이미지 프롬프트는 영어로 작성하세요.

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

def build_analysis_payload(keyword, category, trend, blog, news, web, images, own_posts, shopping, benchmark, own_search_note="", specific_post=None, blog_id=""):
    return {
        "keyword": keyword,
        "category": category,
        "trend": trend_summary(trend),
        "blog_results": compact_results(blog.get("items", []), ["title", "description", "bloggername", "bloggerlink", "postdate"]),
        "news_results": compact_results(news.get("items", []), ["title", "description", "originallink", "pubDate"]),
        "web_results": compact_results(web.get("items", []), ["title", "description", "link"]),
        "image_results": compact_results(images.get("items", []), ["title", "link", "thumbnail"]),
        "own_blog_id": blog_id,
        "own_existing_posts": compact_results(own_posts, ["title", "description", "link", "postdate"]),
        "specific_existing_post": specific_post or {"status": "not_provided"},
        "own_search_note": own_search_note,
        "shopping_trend": shopping,
        "benchmark": benchmark,
        "recommended_strategy": "PENDING",
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
    st.caption("내 블로그는 기존 글을 '콘텐츠 자산'으로 분석해 현재 키워드와 연결할 기회를 찾는 데 사용합니다.")
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
    st.title("🔎 네이버 콘텐츠 기회 분석기 V2.1")
    st.info("왼쪽 사이드바에 Gemini API Key와 Naver Client ID / Secret을 입력하면 시작할 수 있어요.")
    st.markdown("""
### 이 버전에서 하는 일
1. Creator Advisor에서 직접 선별한 키워드를 입력
2. 네이버 검색어 트렌드 분석
3. 블로그·뉴스·웹·이미지 검색 분석
4. 필요하면 쇼핑인사이트 분석
5. 내 블로그에 관련 글이 있는지 확인
6. 내 기존 콘텐츠를 '자산'으로 분석해 업데이트 / 파생 신규 / 완전 신규 전략을 AI가 판단
7. 검색용 제목 + 홈판용 제목 + 본문 + 이미지 계획까지 작성
""")
    st.stop()

client = genai.Client(api_key=gemini_key)

st.title("🔎 네이버 콘텐츠 기회 분석기")
st.caption("Creator Advisor에서 직접 선별한 키워드를 넣으면, 네이버 데이터 → 콘텐츠 GAP → 기존 글 → 검색·홈판 전략 → 글 작성까지 연결합니다.")

if "analysis" not in st.session_state:
    st.session_state.analysis = None
if "article" not in st.session_state:
    st.session_state.article = None

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

            st.write("⑤ 이미지 검색")
            images = naver_search("image", keyword, naver_id, naver_secret, display=10, sort="sim")

            st.write("⑥ 내 기존 콘텐츠 자산 수집")
            blog_id = extract_blog_id(own_blog)
            own_posts, own_search_note = search_own_blog_posts(
                keyword, blog_id, naver_id, naver_secret
            )
            # 키워드 검색 결과에 안 잡혀도 자산으로 직접 지정할 수 있도록 지원
            specific_post = fetch_naver_post(specific_existing_url) if specific_existing_url.strip() else {"status": "not_provided"}

            shopping = None
            if commercial and shopping_category.strip():
                st.write("⑦ 쇼핑인사이트")
                shopping = naver_shopping_trend(
                    keyword, shopping_category.strip(), naver_id, naver_secret
                )

            benchmark = fetch_benchmark(benchmark_url)
            payload = build_analysis_payload(
                keyword, category, trend, blog, news, web, images,
                own_posts, shopping, benchmark, own_search_note,
                specific_post=specific_post, blog_id=blog_id
            )

            st.write("⑧ AI 콘텐츠 전략 분석")
            ai = analyze_with_ai(client, payload)
            payload.update(ai)

            st.session_state.analysis = payload
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
    own_count = len(analysis.get("own_existing_posts", []))
    opportunity = analysis.get("opportunity", "확인 필요")
    strategy = analysis.get("recommended_strategy", "NEW")

    # 모바일/좁은 화면에서도 한눈에 읽히도록 큰 st.metric 대신 컴팩트 카드 사용
    st.markdown("""
    <style>
    .summary-grid{display:grid;grid-template-columns:repeat(5,minmax(0,1fr));gap:8px;margin:8px 0 14px;}
    .summary-card{border:1px solid #e6e6e6;border-radius:10px;padding:10px 11px;background:#fff;min-height:72px;}
    .summary-label{font-size:11px;color:#777;margin-bottom:5px;font-weight:600;}
    .summary-value{font-size:15px;line-height:1.35;font-weight:700;word-break:keep-all;}
    @media(max-width:900px){.summary-grid{grid-template-columns:repeat(2,minmax(0,1fr));}}
    @media(max-width:520px){.summary-grid{grid-template-columns:1fr;}}
    </style>
    """, unsafe_allow_html=True)
    def _card(label, value):
        value = str(value or "-")
        if len(value) > 34:
            value = value[:34] + "…"
        return f'<div class="summary-card"><div class="summary-label">{label}</div><div class="summary-value">{value}</div></div>'
    st.markdown(
        '<div class="summary-grid">' +
        _card("관심도 추이", direction) +
        _card("콘텐츠 기회", opportunity) +
        _card("검색 의도", analysis.get("search_intent", "-")) +
        _card("내 기존 관련글", f"{own_count}개") +
        _card("추천 전략", strategy) +
        '</div>',
        unsafe_allow_html=True,
    )

    strategy_labels = {
        "UPDATE_EXISTING": "🔄 기존 글 업데이트",
        "NEW_DERIVED": "🆕 기존 글 기반 신규 글",
        "NEW_UNRELATED": "🆕 완전 신규 글",
        "NO_OPPORTUNITY": "⏸ 현재 작성 보류",
    }
    st.info(f"추천 콘텐츠 전략: **{strategy_labels.get(strategy, strategy)}**")
    st.write(analysis.get("strategy_reason", ""))

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
        if analysis.get("own_search_note"):
            st.caption("🔎 " + analysis.get("own_search_note"))
        specific = analysis.get("specific_existing_post", {}) or {}
        if specific.get("status") == "ok":
            st.success("🎯 지정한 기존글을 최우선 콘텐츠 자산으로 분석했습니다.")
            st.markdown(f"**{specific.get('title') or specific.get('url','')}**")
            st.caption(specific.get("url", ""))
        elif specific.get("status") == "failed":
            st.warning("지정한 기존글을 직접 읽지 못했습니다. 자동 발견 후보와 입력된 정보만으로 분석했습니다.")
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
        if analysis.get("own_existing_posts"):
            st.markdown("### 자동 발견된 관련 후보")
            for post in analysis["own_existing_posts"]:
                st.markdown(f"**{post.get('title','')}**")
                st.caption(f"{post.get('postdate','')} · {post.get('link','')}")
                st.write(post.get("description", ""))
        else:
            st.caption("자동 검색에서 후보가 없더라도 기존 글이 없다는 뜻은 아닙니다. 정확한 글을 분석하려면 '특정 기존글 URL'을 입력할 수 있습니다.")

    with tabs[4]:
        st.markdown("### 홈판 콘텐츠 각도")
        st.write(analysis.get("home_feed_angle", "-"))
        st.markdown("### 추천 이유")
        st.write(analysis.get("strategy_reason", "-"))

    st.divider()
    st.subheader("3. 글 작성")
    if st.button("✍️ 이 분석으로 글 작성", type="primary", use_container_width=True):
        with st.spinner("검색 의도와 콘텐츠 GAP을 반영해 글을 작성하고 있어요..."):
            try:
                article = write_with_ai(
                    client,
                    analysis,
                    {"category": category, "tone": tone, "length": length},
                )
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
    with b:
        st.markdown("### 🏠 홈판용 제목")
        st.code(article.get("home_title", ""), language=None)

    c, d = st.columns(2)
    with c:
        st.markdown("### 🖼 썸네일 문구")
        st.code(article.get("thumbnail_text", ""), language=None)
    with d:
        st.markdown("### Meta description")
        st.write(article.get("meta_description", ""))

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
