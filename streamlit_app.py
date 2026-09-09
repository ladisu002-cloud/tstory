
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
    page_title="네이버 콘텐츠 기회 분석기 V1.3",
    page_icon="🔎",
    layout="wide",
)

CATEGORIES = ["건강", "생활정보", "여행", "육아", "제품리뷰", "정부지원", "기타"]
MODEL = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")

def clean_html(text):
    return re.sub(r"<[^>]+>", "", text or "").replace("&quot;", '"').replace("&amp;", "&").strip()

def naver_headers(client_id, client_secret):
    # Naver Open API는 Client ID/Secret을 반드시 HTTP 헤더로 전달합니다.
    return {
        "X-Naver-Client-Id": (client_id or "").strip(),
        "X-Naver-Client-Secret": (client_secret or "").strip(),
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

def naver_search(api, query, client_id, client_secret, display=10, sort="sim"):
    url = f"https://openapi.naver.com/v1/search/{api}.json"
    r = requests.get(
        url,
        headers=naver_headers(client_id, client_secret),
        params={"query": query, "display": display, "sort": sort},
        timeout=15,
    )
    _raise_naver_error(r, f"네이버 {api} 검색 API")
    return r.json()

def naver_trend(keyword, client_id, client_secret, days=30):
    end = date.today()
    start = end - timedelta(days=days)
    url = "https://openapi.naver.com/v1/datalab/search"
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
        timeout=15,
    )
    _raise_naver_error(r, "네이버 검색어 트렌드 API")
    return r.json()

def naver_shopping_trend(keyword, category_code, client_id, client_secret, days=30):
    if not category_code:
        return None
    end = date.today()
    start = end - timedelta(days=days)
    url = "https://openapi.naver.com/v1/datalab/shopping/category/keyword"
    payload = {
        "startDate": start.isoformat(),
        "endDate": end.isoformat(),
        "timeUnit": "date",
        "category": category_code,
        "keyword": keyword,
    }
    r = requests.post(
        url,
        headers={**naver_headers(client_id, client_secret), "Content-Type": "application/json"},
        json=payload,
        timeout=15,
    )
    _raise_naver_error(r, "네이버 쇼핑인사이트 API")
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
    if not blog_id:
        return []
    key = blog_id.lower()
    matches = []
    for item in blog_items:
        bloggerlink = (item.get("bloggerlink") or "").lower()
        link = (item.get("link") or "").lower()
        if key in bloggerlink or f"blog.naver.com/{key}" in link:
            matches.append(item)
    return matches

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
    },
    "required": [
        "search_intent", "competition", "opportunity", "trend_interpretation",
        "related_keywords", "long_tail_keywords", "title_patterns",
        "content_gaps", "home_feed_angle", "recommended_strategy",
        "strategy_reason", "recommended_outline",
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
        "outline", "body_markdown", "faq", "image_plan", "tags",
    ],
}

def gemini_json(client, prompt, schema, max_tokens=8000):
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

def analyze_with_ai(client, payload):
    prompt = f"""
당신은 네이버 블로그 콘텐츠 전략가입니다.
목표는 네이버 검색 노출과 네이버 홈 피드 클릭 가능성을 함께 고려해
'이 키워드로 지금 어떤 글을 만들어야 하는가'를 판단하는 것입니다.

중요한 원칙:
- 네이버 크리에이터 어드바이저의 순위나 검색량을 추정해서 만들지 마세요.
- 제공된 데이터만 근거로 판단하세요.
- 검색어 트렌드는 절대 검색량이 아니라 상대적인 관심도 추이입니다.
- 네이버 검색 API의 블로그 결과만으로 경쟁 글의 전체 본문 구조를 안다고 주장하지 마세요.
- 관련 키워드는 검색 결과의 제목/설명과 일반적인 검색 의도를 종합해 제안하세요.
- 기존 글이 없으면 신규 작성 전략을 추천하세요.
- 기존 글이 있으면 UPDATE 또는 NEW_SEPARATE 중 하나를 판단하세요.
- 기존 글과 검색 의도가 거의 같고 최신성 보강이 핵심이면 UPDATE.
- 검색 의도가 다르거나 별도의 문제 해결형 주제가 더 적합하면 NEW_SEPARATE.
- 기존 글 정보가 없으면 무리해서 업데이트를 추천하지 마세요.

[입력]
{json.dumps(payload, ensure_ascii=False, indent=2)}

JSON으로만 답하세요.
"""
    return gemini_json(client, prompt, ANALYSIS_SCHEMA, 6000)

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
7) 모바일에서 읽기 쉽게 짧은 문단과 명확한 소제목 사용
8) 한국어 문체는 친근한 존댓말(~해요, ~랍니다)을 기본으로 하기

작성 조건:
- 카테고리: {writing_options["category"]}
- 톤: {writing_options["tone"]}
- 목표 분량: {writing_options["length"]}
- 메인 키워드: {analysis_payload["keyword"]}
- 추천 전략: {analysis_payload["recommended_strategy"]}

분석 데이터:
{json.dumps(analysis_payload, ensure_ascii=False, indent=2)}

body_markdown에는 제목을 반복하지 말고 H2/H3 마크다운을 사용하세요.
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
    checks = {
        "메인 키워드 반영": count >= 2,
        "검색의도 반영": bool(analysis.get("search_intent")),
        "콘텐츠 GAP 반영": any(g.lower() in text.lower() for g in analysis.get("content_gaps", [])[:3]) if analysis.get("content_gaps") else True,
        "FAQ 포함": len(article.get("faq", [])) >= 3,
        "이미지 계획 포함": len(article.get("image_plan", [])) >= 3,
        "홈판 제목 별도 생성": bool(article.get("home_title")),
        "썸네일 문구 생성": bool(article.get("thumbnail_text")),
    }
    score = round(sum(checks.values()) / len(checks) * 100)
    return score, checks, count

def build_analysis_payload(keyword, category, trend, blog, news, web, images, own_posts, shopping, benchmark):
    return {
        "keyword": keyword,
        "category": category,
        "trend": trend_summary(trend),
        "blog_results": compact_results(blog.get("items", []), ["title", "description", "bloggername", "bloggerlink", "postdate"]),
        "news_results": compact_results(news.get("items", []), ["title", "description", "originallink", "pubDate"]),
        "web_results": compact_results(web.get("items", []), ["title", "description", "link"]),
        "image_results": compact_results(images.get("items", []), ["title", "link", "thumbnail"]),
        "own_existing_posts": compact_results(own_posts, ["title", "description", "link", "postdate"]),
        "shopping_trend": shopping,
        "benchmark": benchmark,
        "recommended_strategy": "NEW" if not own_posts else "PENDING",
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
    st.caption("API 키는 이 브라우저 세션에서만 사용합니다. GitHub에는 저장하지 않습니다.")

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
    st.caption("기존 글이 있을 때만 관련 글 비교에 사용합니다.")
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
    st.title("🔎 네이버 콘텐츠 기회 분석기 V1.3")
    st.info("왼쪽 사이드바에 Gemini API Key와 Naver Client ID / Secret을 입력하면 시작할 수 있어요.")
    st.markdown("""
### 이 버전에서 하는 일
1. Creator Advisor에서 직접 선별한 키워드를 입력
2. 네이버 검색어 트렌드 분석
3. 블로그·뉴스·웹·이미지 검색 분석
4. 필요하면 쇼핑인사이트 분석
5. 내 블로그에 관련 글이 있는지 확인
6. 신규 작성 / 기존 글 업데이트 / 별도 신규 글을 AI가 판단
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

            st.write("⑥ 내 기존 글 확인")
            blog_id = extract_blog_id(own_blog)
            own_posts = own_blog_matches(blog.get("items", []), blog_id)

            shopping = None
            if commercial and shopping_category.strip():
                st.write("⑦ 쇼핑인사이트")
                shopping = naver_shopping_trend(
                    keyword, shopping_category.strip(), naver_id, naver_secret
                )

            benchmark = fetch_benchmark(benchmark_url)
            payload = build_analysis_payload(
                keyword, category, trend, blog, news, web, images,
                own_posts, shopping, benchmark
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

    m1, m2, m3, m4, m5 = st.columns(5)
    m1.metric("관심도 추이", direction)
    m2.metric("콘텐츠 기회", opportunity)
    m3.metric("검색의도", analysis.get("search_intent", "-"))
    m4.metric("기존 관련글", f"{own_count}개")
    m5.metric("추천 전략", strategy)

    if strategy == "UPDATE":
        st.success("🔄 기존 글 업데이트를 우선 추천합니다.")
    elif strategy == "NEW_SEPARATE":
        st.info("🆕 기존 글과 검색 의도가 달라 별도 신규 글을 추천합니다.")
    else:
        st.info("🆕 관련 기존 글이 없거나 신규 콘텐츠 가치가 높아 신규 작성을 추천합니다.")

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
        if not analysis.get("own_existing_posts"):
            st.info("현재 검색 결과에서 내 블로그의 관련 글을 찾지 못했습니다.")
        else:
            for post in analysis["own_existing_posts"]:
                st.markdown(f"**{post.get('title','')}**")
                st.caption(f"{post.get('postdate','')} · {post.get('link','')}")
                st.write(post.get("description", ""))

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

    score, checks, count = seo_check(article, analysis)
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
