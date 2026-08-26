import os
import re
import json
import urllib.parse
import requests
from bs4 import BeautifulSoup
import streamlit as st
from google import genai
from google.genai import types
from dotenv import load_dotenv

load_dotenv()

st.set_page_config(page_title="티스토리 SEO 블로그 원고생성기", page_icon="📝", layout="wide")

# gemini-3.6-flash는 2026-08 기준 최신 안정 버전입니다.
MODEL = "gemini-3.6-flash"
MAX_BATCH_TOPICS = 30

TONE_OPTIONS = {
    "친근한 존댓말": "친근하고 다정한 존댓말, 어려운 용어를 풀어서 설명",
    "담백한 존댓말": "정보 전달에 집중하는 담백하고 정중한 존댓말",
    "전문적 문어체": "전문적이고 신뢰감 있는 문어체",
}

QUALITY_RULES = (
    "모바일 가독성을 위해 한 문장은 평균 40~50자 이내로 짧게 끊고, 2~4문장마다 문단을 나누세요. "
    "핵심 키워드는 제목/도입부/소제목 2곳 이상/마무리에 걸쳐 자연스럽게 5~7회 반복하되 "
    "억지로 욱여넣지 말고 문맥에 맞는 동의어·변형 표현으로 분산하세요. "
    "친근하고 신뢰감 있는 어조로, 독자가 흔히 궁금해하거나 헷갈리는 지점을 콕 짚어 먼저 풀어주세요. "
    '"이것은 중요한 요소입니다", "다음과 같은 방법이 있습니다" 같은 딱딱하고 상투적인 AI 문체는 피하세요. '
    "단, 실제로 확인되지 않은 1인칭 경험(예: 특정 제품을 직접 써봤다는 구체적 후기)을 사실처럼 지어내지는 마세요 — "
    "독자를 오도할 수 있으므로, 대신 흔히 겪는 상황에 공감하는 화법으로 신뢰를 쌓으세요."
)

STYLE_GUIDE = (
    "다음은 이 블로그 필자의 고유한 말투이니 반드시 반영하세요: "
    "도입부는 '안녕하세요!' 같은 짧은 인사, 또는 독자의 경험에 공감을 구하는 질문"
    "(예: '혹시 ~해보신 적 있으신가요?', '~ 때문에 고민이신가요?')으로 시작하고, "
    "이 글을 쓰게 된 개인적 계기나 상황을 1~2문장으로 먼저 밝히세요. "
    "전체 서술은 1인칭 경험담 화법을 기본으로 하되('저도 ~했는데', '제가 알아보니'), "
    "실제로 확인되지 않은 구체적 개인 체험을 지어내지는 마세요 — "
    "체험이 확실하지 않을 때는 '~라고 해요', '~더라고요(전해 들은 정보)'처럼 톤만 유지하고 사실을 지어내지 않습니다. "
    "문장 끝맺음은 '~했어요', '~하더라구요', '~랍니다', '~한답니다', '~네요'처럼 "
    "부드럽고 구어체적인 존댓말로 통일하고, 단정적인 서술('~입니다', '~합니다')보다는 "
    "완곡한 어미('~인 것 같아요', '~인 듯해요')를 섞어 친근한 톤을 유지하세요. "
    "한 문단은 1~3문장으로 짧게 끊어 모바일에서 술술 읽히게 구성하고, "
    "'정말', '진짜', '너무', '완전' 같은 감탄 부사를 과하지 않게 섞어 감정을 자연스럽게 드러내세요."
)

HEALTH_SYSTEM_RULES = (
    "특정 의약품명이나 복용량, 개별 진단·치료를 지시하는 문장은 절대 쓰지 마세요. "
    "운동·식습관·수면 같은 일반적인 생활습관 정보만 다루고, '~에 도움이 된다고 알려져 있습니다', "
    "'전문가들은 ~을 권장합니다'처럼 출처를 특정하지 않는 일반론으로 서술하세요. "
    "실존 여부가 불확실한 특정 연구·논문·저널명을 지어내 인용하지 마세요."
)

HEALTH_DISCLAIMER = (
    "이 글은 일반적인 건강 정보 제공을 목적으로 하며, 개인의 의학적 진단이나 "
    "치료를 대체하지 않습니다. 증상이 있다면 반드시 전문의와 상담하세요."
)

# 모든 이미지 프롬프트 끝에 코드가 직접 붙이는 스타일 고정 문구.
# AI가 지시를 깜빡해서 일러스트/카툰 스타일이 섞여 나오는 걸 막기 위해,
# 프롬프트 안 지시가 아니라 여기서 강제로 통일합니다.
IMAGE_STYLE_SUFFIX = (
    ", realistic photograph, natural lighting, high detail, no illustration, "
    "no cartoon, no drawing, no vector art, no 3D render"
)


def _strip_tags(text: str) -> str:
    return re.sub(r"<[^>]+>", "", text or "")


def naver_search(query: str, client_id: str, client_secret: str, api: str = "webkr", display: int = 5):
    """네이버 검색 오픈API (무료, 결제 불필요). api: webkr(웹문서) 또는 encyc(백과사전)."""
    url = f"https://openapi.naver.com/v1/search/{api}.json"
    headers = {"X-Naver-Client-Id": client_id, "X-Naver-Client-Secret": client_secret}
    params = {"query": query, "display": display}
    resp = requests.get(url, headers=headers, params=params, timeout=10)
    resp.raise_for_status()
    items = resp.json().get("items", [])
    return [{"title": _strip_tags(i.get("title", "")), "description": _strip_tags(i.get("description", ""))} for i in items]


def research_topic_naver(topic: str, client_id: str, client_secret: str) -> str:
    """네이버 검색 오픈API로 배경지식을 모읍니다 (무료, 결제 불필요)."""
    results = []
    try:
        results += naver_search(topic, client_id, client_secret, api="encyc", display=3)
    except requests.RequestException:
        pass
    try:
        results += naver_search(topic, client_id, client_secret, api="webkr", display=5)
    except requests.RequestException:
        pass
    if not results:
        return ""
    lines = [f"- {r['title']}: {r['description']}" for r in results if r["description"]]
    return "\n".join(lines)


def research_topic_wikipedia(topic: str) -> str:
    """한국어 위키백과 검색 (완전 무료, 가입/키/카드 불필요)."""
    search_url = "https://ko.wikipedia.org/w/api.php"
    params = {"action": "query", "list": "search", "srsearch": topic, "format": "json", "srlimit": 3}
    resp = requests.get(search_url, params=params, timeout=10)
    resp.raise_for_status()
    titles = [item["title"] for item in resp.json().get("query", {}).get("search", [])]
    if not titles:
        return ""
    lines = []
    for title in titles:
        try:
            summary_url = f"https://ko.wikipedia.org/api/rest_v1/page/summary/{requests.utils.quote(title)}"
            s = requests.get(summary_url, timeout=10)
            if s.status_code == 200:
                extract = s.json().get("extract", "")
                if extract:
                    lines.append(f"- {title}: {extract}")
        except requests.RequestException:
            continue
    return "\n".join(lines)


def fetch_trusted_site_text(url: str, max_chars: int = 3000) -> str:
    """지정한 URL 페이지의 본문 텍스트를 가져옵니다 (완전 무료, 키 불필요).
    사이트가 자동 접근을 막아둔 경우(로봇 차단, 403 등) 예외가 날 수 있어 호출부에서 try/except로 감싸주세요."""
    resp = requests.get(url, timeout=10, headers={"User-Agent": "Mozilla/5.0 (compatible; ArticleResearchBot/1.0)"})
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")
    for tag in soup(["script", "style", "nav", "header", "footer", "aside"]):
        tag.decompose()
    text = soup.get_text(separator="\n")
    lines = [line.strip() for line in text.split("\n") if line.strip()]
    return "\n".join(lines)[:max_chars]


# =========================================================
# Gemini 호출
# =========================================================
def call_gemini_json(client, prompt: str) -> dict:
    resp = client.models.generate_content(
        model=MODEL,
        contents=prompt,
        config=types.GenerateContentConfig(
            max_output_tokens=4000,
            response_mime_type="application/json",
        ),
    )
    text = (resp.text or "").strip()
    # 혹시 코드펜스가 붙어오면 제거
    if text.startswith("```"):
        text = text.split("```")[1]
        if text.startswith("json"):
            text = text[4:]
    return json.loads(text)


def research_topic_gemini(client, topic: str) -> str:
    """건강 주제에 대해 Gemini 웹검색(Grounding, 유료)으로 배경지식을 찾습니다.
    결제 미설정 시 예외가 날 수 있어, 호출부에서 반드시 try/except로 감싸주세요."""
    prompt = f"""'{topic}'에 대해 웹 검색으로 의학적으로 신뢰할 수 있는 최신 정보를 찾아서 정리해주세요.
- 공신력 있는 출처(의학 기관, 논문, 병원 등) 기반 정보만 반영하세요.
- 효능을 과장하거나 특정 질병의 치료·완치를 단정하는 표현은 배제하세요.
- 8~12줄 정도로, 사실 위주로 핵심만 정리하세요."""
    resp = client.models.generate_content(
        model=MODEL,
        contents=prompt,
        config=types.GenerateContentConfig(
            max_output_tokens=1500,
            tools=[types.Tool(google_search=types.GoogleSearch())],
        ),
    )
    return (resp.text or "").strip()


def research_seo_rules_gemini(client, platform_hint: str = "티스토리") -> str:
    """플랫폼의 최신 상위노출 규칙을 웹검색으로 확인합니다 (Gemini 웹검색, 유료)."""
    query = (
        f"{platform_hint} 블로그 상위노출 SEO 규칙 최신 기준을 검색해서 알려줘. "
        "제목 글자수, 본문 분량, 키워드 배치, 이미지 개수, 태그, 저품질/금지 패턴 등 핵심만 정리해줘."
    )
    resp = client.models.generate_content(
        model=MODEL,
        contents=query,
        config=types.GenerateContentConfig(
            max_output_tokens=1000,
            tools=[types.Tool(google_search=types.GoogleSearch())],
        ),
    )
    return (resp.text or "").strip()


def research_seo_rules_naver(client_id: str, client_secret: str, platform_hint: str = "티스토리") -> str:
    """플랫폼의 최신 상위노출 규칙을 네이버 검색(무료)으로 확인합니다."""
    query = f"{platform_hint} 블로그 상위노출 SEO 규칙"
    try:
        results = naver_search(query, client_id, client_secret, api="webkr", display=5)
    except requests.RequestException:
        return ""
    return "\n".join(f"- {r['title']}: {r['description']}" for r in results if r["description"])



def build_article_prompt(topic: str, tone: str, length_label: str, research: str, seo_rules: str = "") -> str:
    length_guide = {
        "짧게 (5문단 내외)": "각 섹션 본문은 2~3문장, 전체 3~4개 섹션",
        "보통 (기본)": "각 섹션 본문은 3~5문장, 전체 4~5개 섹션",
        "길게 (상세)": "각 섹션 본문은 5~7문장, 전체 5~7개 섹션",
    }.get(length_label, "각 섹션 본문은 3~5문장, 전체 4~5개 섹션")

    tone_desc = TONE_OPTIONS.get(tone, tone)

    research_block = (
        f"아래는 웹검색으로 확인한 신뢰할 수 있는 배경지식입니다. 이 내용에 근거해서만 사실 정보를 작성하고,\n"
        f"여기 없는 통계나 수치, 연구 결과를 지어내지 마세요.\n\n[검색된 배경지식]\n{research}\n"
        if research
        else "웹검색 결과가 없으니, 논란의 여지가 있거나 확인이 필요한 구체적 수치·연구 인용은 넣지 말고 "
        "일반적으로 널리 알려진 수준의 정보만 다루세요.\n"
    )

    seo_rules_block = f"\n[최신 SEO 규칙 참고]\n{seo_rules}\n" if seo_rules else ""

    return f"""당신은 20년차 SEO 전문가이자 건강 정보 콘텐츠 작가입니다. 티스토리에 올릴 글을 씁니다.
정확성이 최우선입니다 — 확인되지 않은 정보나 지어낸 수치를 절대 포함하지 마세요.
주제: {topic}
말투: {tone_desc}
분량: {length_guide}

{HEALTH_SYSTEM_RULES}

{QUALITY_RULES}

{STYLE_GUIDE}

{research_block}
{seo_rules_block}

아래 JSON 형식으로만 응답하세요. 다른 설명, 코드펜스, 다른 텍스트는 절대 붙이지 마세요.
모든 이미지 프롬프트(thumbnail_prompt, 각 section의 image_prompt)는 반드시 사실적인 사진 스타일로만 작성하세요.
일러스트, 카툰, 손그림, 벡터 아트, 애니메이션 스타일은 절대 쓰지 마세요 — 실제 카메라로 찍은 듯한 사진 묘사만 쓰세요.

{{
  "meta_description": "검색결과 요약에 쓰일 1~2문장 (60자 내외, 핵심 키워드 포함)",
  "title": "SEO에 유리하면서 클릭하고 싶은 제목 (30자 내외)",
  "intro": "도입부. 짧은 인사나 공감 질문으로 시작하고, 이 글을 쓰게 된 계기를 1인칭으로 2~3문장 밝히기",
  "cta_text": "본문 상단에 넣을 짧은 행동유도 문구 (예: 🔍 OOO 확인해보세요!)",
  "thumbnail_prompt": "블로그 썸네일용 대표 이미지의 영어 프롬프트. 사진 스타일, 주제를 한눈에 보여주는 구도, 텍스트나 로고 없이",
  "sections": [
    {{
      "heading": "소제목",
      "content_html": "본문 내용. <p>, <b>, <ul><li> 태그만 사용한 HTML 조각. 1인칭 공감형 구어체 존댓말 유지, 단정적 치료 효과 주장 금지.",
      "image_prompt": "이 섹션과 어울리는 이미지의 영어 프롬프트. 사진 스타일, 구체적인 피사체·구도·조명 묘사"
    }}
  ],
  "qa": [
    {{"question": "독자가 흔히 궁금해할 질문", "answer": "짧고 명확한 답변, 1~2문장"}}
  ],
  "conclusion": "마무리 문단. 요약이나 다짐 한두 문장 + 개인차가 있을 수 있다는 담백한 단서",
  "tags": ["검색 노출에 도움될 태그 5~10개, 짧은 키워드 형태, # 없이"]
}}

sections는 {length_guide.split(',')[1].strip() if ',' in length_guide else '4~5개'} 정도로, qa는 3~4개로 구성하세요.
과장된 효능 단정 표현(완치, 100% 등)은 쓰지 마세요."""




# =========================================================
# 고정 HTML 템플릿 렌더링
# =========================================================
def render_article_html(article: dict, adsense_client: str, adsense_slot: str, use_ads: bool):
    """HTML 문자열과, 순서대로 정리된 이미지 프롬프트 목록을 함께 반환합니다."""
    def ad_block():
        if not use_ads or not adsense_client.strip():
            return ""
        return f"""
<script src="https://pagead2.googlesyndication.com/pagead/js/adsbygoogle.js?client={adsense_client}"></script>
<ins class="adsbygoogle" style="display: block;" data-ad-client="{adsense_client}" data-ad-slot="{adsense_slot}" data-ad-format="auto" data-full-width-responsive="true"></ins>
<script>(adsbygoogle = window.adsbygoogle || []).push({{}});</script>
"""

    sections = article.get("sections", [])
    toc_items = "\n".join(
        f'<li><a style="color: #3498db; text-decoration: none;" href="#section{i+1}">{s["heading"]}</a></li>'
        for i, s in enumerate(sections)
    )

    thumbnail_prompt = article.get("thumbnail_prompt", "")
    thumbnail_full_prompt = (thumbnail_prompt + IMAGE_STYLE_SUFFIX) if thumbnail_prompt else ""

    section_html_parts = []
    image_prompts = []  # 본문 [이미지N] 표시와 번호가 같은, 섹션별 프롬프트 목록 (썸네일은 별도)
    for i, s in enumerate(sections):
        img_prompt = s.get("image_prompt", "")
        img_placeholder = ""
        if img_prompt:
            full_prompt = img_prompt + IMAGE_STYLE_SUFFIX
            image_prompts.append({"index": len(image_prompts) + 1, "heading": s["heading"], "prompt": full_prompt})
            placeholder_num = len(image_prompts)
            # 단순한 한 줄 문단만 사용 — 중첩 div/절대위치 스타일은 티스토리 에디터가
            # 드래그 삽입 커서 위치를 못 잡고 이미지가 맨 뒤로 밀리는 원인이 됩니다.
            img_placeholder = (
                f'<p style="text-align:center; color:#999; border:2px dashed #ccc; '
                f'padding:20px; margin:15px 0; border-radius:8px;">'
                f'[이미지{placeholder_num}] — 여기에 이 줄을 지우고 이미지를 끼워넣으세요</p>'
            )

        # 두 번째 섹션 다음에 본문 중간 광고 삽입 (클릭률 높은 위치)
        mid_ad = ad_block() if (use_ads and i == 1) else ""

        section_html_parts.append(f"""
<div id="section{i+1}">
<h2 style="background: #3498db; color: white; padding: 10px 15px; border-radius: 5px; font-size: 22px; margin: 30px 0 20px;">{s['heading']}</h2>
{mid_ad}
{img_placeholder}
{s.get('content_html', '')}
</div>""")

    sections_html = "\n".join(section_html_parts)
    top_ad = ad_block()

    # Q&A 섹션 렌더링 (세 번째 광고는 여기 바로 앞에 위치 — "Q&A 시작 직전" 자리)
    qa_items = article.get("qa", [])
    qa_html = ""
    if qa_items:
        qa_rows = "\n".join(
            f'''<div style="margin: 12px 0; padding: 12px 15px; background-color: #f8f9fa; border-radius: 6px;">
<p style="font-weight: bold; color: #2c3e50; margin: 0 0 6px 0;">Q. {q.get('question', '')}</p>
<p style="margin: 0; color: #333;">A. {q.get('answer', '')}</p>
</div>'''
            for q in qa_items
        )
        qa_html = f'''
<div style="background-color: #fff; border: 1px solid #e0e0e0; border-radius: 8px; padding: 20px; margin: 20px 0;">
<h2 style="color: #2c3e50; font-size: 20px; margin: 0 0 15px 0;">자주 묻는 질문</h2>
{qa_rows}
</div>'''

    qa_ad = ad_block() if (use_ads and qa_html) else ""
    # Q&A가 없는 경우를 대비해, 이전까지 쓰던 "결론 직전" 위치를 폴백으로 둡니다.
    fallback_bottom_ad = ad_block() if (use_ads and not qa_html) else ""

    html = f"""<div class="blog-post">
<p style="font-size: 16px; line-height: 1.8; color: #333; font-weight: bold; margin-bottom: 20px;">{article.get('meta_description', '')}</p>

<h1 style="background: linear-gradient(to right, #3498db, #2980b9); color: white; padding: 15px; border-radius: 8px; text-align: center; font-size: 24px; margin-bottom: 30px; box-shadow: 0 4px 6px rgba(0,0,0,0.1);">{article.get('title', '')}</h1>

<p style="font-size: 16px; line-height: 1.8; color: #333;">{article.get('intro', '')}</p>

<div style="background: linear-gradient(to right, #3498db, #2980b9); color: white; padding: 15px; border-radius: 8px; text-align: center; margin: 20px 0 30px; box-shadow: 0 4px 6px rgba(0,0,0,0.1);">
<a style="color: white; text-decoration: none; font-weight: bold; font-size: 18px;" href="#">{article.get('cta_text', '')}</a>
</div>

{top_ad}

<div style="background-color: #f8f9fa; padding: 15px; border-radius: 8px; margin: 20px 0; border-left: 5px solid #3498db;">
<h2 style="color: #2c3e50; font-size: 20px; margin: 0 0 10px 0;">목차</h2>
<ol style="padding-left: 20px; margin: 0;">
{toc_items}
</ol>
</div>

{sections_html}

{fallback_bottom_ad}

<div style="background-color: #e8f4fc; padding: 20px; border-radius: 8px; margin: 30px 0;">
<h2 style="color: #2c3e50; font-size: 22px; margin: 0 0 15px 0;">결론</h2>
<p style="font-size: 16px; line-height: 1.8; color: #333;">{article.get('conclusion', '')}</p>
</div>

{qa_ad}
{qa_html}

<div style="background-color: #fdf6e3; border: 1px solid #eee0b8; border-radius: 8px; padding: 15px 20px; margin: 20px 0; font-size: 14px; color: #7a6a3a; line-height: 1.6;">
⚠️ {HEALTH_DISCLAIMER}
</div>
</div>"""

    return html, image_prompts, thumbnail_full_prompt


def generate_article(client, topic: str, tone: str, length_label: str,
                      naver_id: str = "", naver_secret: str = "", use_seo_rules: bool = False,
                      trusted_url: str = ""):
    """(article dict, research_source: str, research_error: str|None)를 반환합니다.
    검색 방식은 사용자가 고르지 않고 자동으로 결정합니다:
    지정 사이트 URL이 있으면 그 페이지를 먼저 쓰고, 없거나 실패하면 네이버(키 있을 때) → 위키백과 순으로 대체합니다."""
    research = ""
    research_error = None
    research_source = ""

    if trusted_url.strip():
        url = trusted_url.strip()
        if "{topic}" in url:
            url = url.replace("{topic}", urllib.parse.quote(topic))
        try:
            research = fetch_trusted_site_text(url)
            if research:
                research_source = "지정한 사이트"
            else:
                research_error = "지정한 사이트에서 내용을 가져오지 못했어요."
        except Exception as e:
            research_error = f"지정한 사이트 접근 실패: {e}"

    if not research and naver_id and naver_secret:
        try:
            research = research_topic_naver(topic, naver_id, naver_secret)
            if research:
                research_source = "네이버 검색"
        except Exception as e:
            if not research_error:
                research_error = str(e)

    if not research:  # 위 방법이 다 없거나 실패 → 위키백과로 최종 대체
        try:
            research = research_topic_wikipedia(topic)
            if research:
                research_source = "위키백과"
            elif not research_error:
                research_error = "지정 사이트·네이버·위키백과 모두 관련 정보를 찾지 못했어요."
        except Exception as e:
            if not research_error:
                research_error = str(e)

    seo_rules = ""
    if use_seo_rules:
        try:
            if naver_id and naver_secret:
                seo_rules = research_seo_rules_naver(naver_id, naver_secret)
        except Exception:
            pass  # SEO 규칙 검색은 실패해도 글쓰기 자체는 계속 진행

    prompt = build_article_prompt(topic, tone, length_label, research, seo_rules)
    article = call_gemini_json(client, prompt)
    return article, research_source, research_error


# =========================================================
# 사이드바
# =========================================================
with st.sidebar:
    st.header("설정")
    api_key = st.text_input(
        "Gemini API 키", value=os.getenv("GEMINI_API_KEY", ""), type="password",
        help="aistudio.google.com/apikey 에서 발급받은 키를 입력하세요.",
    )
    st.divider()
    tone = st.selectbox("말투", list(TONE_OPTIONS.keys()))
    length_label = st.selectbox("분량", ["짧게 (5문단 내외)", "보통 (기본)", "길게 (상세)"], index=1)
    trusted_url = st.text_input(
        "신뢰하는 건강정보 사이트 URL (선택)",
        placeholder="예: https://www.amc.seoul.kr/asan/healthinfo/disease/diseaseDetail.do?contentId={topic}",
        help="고정 URL을 넣으면 매번 그 페이지만 참고합니다. 사이트 안에서 직접 검색해보고 나온 결과 URL에서 검색어 부분을 {topic}으로 바꿔 넣으면, 주제마다 자동으로 그 검색어로 바꿔서 요청해요.",
    )
    with st.expander("네이버 검색 키 (선택 — 없어도 위키백과로 자동 검색됨)"):
        naver_id = st.text_input("네이버 Client ID", value=os.getenv("NAVER_CLIENT_ID", ""))
        naver_secret = st.text_input("네이버 Client Secret", value=os.getenv("NAVER_CLIENT_SECRET", ""), type="password")
        st.caption("⚠️ 네이버가 검색 API를 네이버클라우드플랫폼(NCP)으로 이전해서, 신규 발급 시 카드 등록이 필요할 수 있어요. 안 넣으셔도 위키백과로 자동 검색되니 필수는 아닙니다.")
    use_seo_rules = st.checkbox(
        "최신 SEO 규칙도 검색해서 반영", value=False,
        help="네이버 검색 키가 있을 때만 동작합니다 (티스토리 상위노출 기준을 검색해서 참고).",
    )
    st.divider()
    use_ads = st.checkbox("애드센스 광고 위치 자동 삽입", value=True)
    adsense_client = st.text_input("애드센스 클라이언트 ID", value=os.getenv("ADSENSE_CLIENT_ID", "ca-pub-여기에본인ID"))
    adsense_slot = st.text_input("애드센스 슬롯 ID", value=os.getenv("ADSENSE_SLOT_ID", "여기에본인슬롯ID"))

if not api_key:
    st.info("왼쪽 사이드바에 Gemini API 키를 입력하면 시작할 수 있어요.")
    st.stop()

client = genai.Client(api_key=api_key)

st.title("📝 티스토리 SEO 블로그 원고생성기")
st.caption("주제만 입력하면 목차·소제목이 갖춰진 SEO 글을 고정 템플릿으로 만들어줍니다. 광고 위치와 이미지까지 자동으로 채워집니다.")

if "single_article" not in st.session_state:
    st.session_state.single_article = None
if "single_research_source" not in st.session_state:
    st.session_state.single_research_source = ""
if "single_research_error" not in st.session_state:
    st.session_state.single_research_error = None
if "batch_results" not in st.session_state:
    st.session_state.batch_results = None

tab_single, tab_batch = st.tabs(["📝 하나씩 만들기", "📋 여러 주제 한 번에 생성 (배치)"])

# =========================================================
# 탭 1: 하나씩
# =========================================================
with tab_single:
    topic = st.text_input("주제를 입력하세요", placeholder="예: 알부민 효능과 부족 증상")

    if st.button("글 생성하기", type="primary", disabled=not topic.strip()):
        with st.spinner("배경지식을 확인하고, SEO 글을 작성하고 있어요..."):
            try:
                article, research_source, research_error = generate_article(
                    client, topic, tone, length_label, naver_id, naver_secret, use_seo_rules, trusted_url
                )
                st.session_state.single_article = article
                st.session_state.single_research_source = research_source
                st.session_state.single_research_error = research_error
            except Exception as e:
                st.error(f"생성 중 오류가 발생했어요: {e}")
                st.session_state.single_article = None

    if st.session_state.single_article:
        if st.session_state.single_research_source:
            st.success(f"✅ {st.session_state.single_research_source}으로 확인한 정보를 기반으로 작성됐어요.")
        elif st.session_state.single_research_error:
            st.warning(
                f"⚠️ 검색이 실패해서 일반 지식만으로 작성됐어요. 이 글은 발행 전에 직접 사실관계를 확인해주세요.\n\n"
                f"오류 내용: {st.session_state.single_research_error}"
            )

        html, image_prompts, thumbnail_prompt = render_article_html(st.session_state.single_article, adsense_client, adsense_slot, use_ads)
        st.subheader("미리보기")
        st.components.v1.html(html, height=1200, scrolling=True)
        st.subheader("HTML 코드 (티스토리 HTML 모드에 붙여넣기)")
        st.code(html, language="html")
        st.download_button("HTML 파일 다운로드", html, file_name="article.html", mime="text/html")

        if thumbnail_prompt:
            st.subheader("썸네일 프롬프트")
            st.code(thumbnail_prompt, language=None)

        tags = st.session_state.single_article.get("tags", [])
        if tags:
            st.subheader("태그 (티스토리 발행 시 태그 입력창에 붙여넣기)")
            st.code(", ".join(tags), language=None)

        if image_prompts:
            st.subheader("이미지 프롬프트 (순서대로)")
            st.caption("본문 안의 '[이미지N]' 표시와 번호가 같은 순서예요. 이 프롬프트로 이미지를 만든 뒤, 해당 위치 문단을 지우고 그 자리에 이미지를 끼워넣으세요.")
            for ip in image_prompts:
                st.markdown(f"**{ip['index']}. {ip['heading']}**")
                st.code(ip["prompt"], language=None)

# =========================================================
# 탭 2: 배치
# =========================================================
with tab_batch:
    st.caption("왼쪽에서 선택한 말투·분량·광고 설정을 그대로 사용해 순차 생성합니다.")
    st.subheader(f"주제 목록 (한 줄에 하나씩, 최대 {MAX_BATCH_TOPICS}개)")
    batch_input = st.text_area(
        "주제 목록", placeholder="예)\n혈압 낮추는 법\n겨울철 난방비 절약 방법\n무선 청소기 추천\n...",
        height=200, label_visibility="collapsed",
    )
    topics = [t.strip() for t in batch_input.strip().split("\n") if t.strip()][:MAX_BATCH_TOPICS]
    if batch_input.strip():
        st.caption(f"{len(topics)}개 주제가 입력됐어요.")

    if st.button("🚀 전체 순차 생성", type="primary", disabled=not topics):
        st.session_state.batch_results = []
        progress = st.progress(0, text="시작합니다...")
        for i, t in enumerate(topics):
            progress.progress(i / len(topics), text=f"({i+1}/{len(topics)}) '{t}' 작성 중...")
            try:
                article, research_source, research_error = generate_article(
                    client, t, tone, length_label, naver_id, naver_secret, use_seo_rules, trusted_url
                )
                st.session_state.batch_results.append(
                    {"topic": t, "article": article, "research_source": research_source, "research_error": research_error}
                )
            except Exception as e:
                st.session_state.batch_results.append({"topic": t, "error": str(e)})
        progress.progress(1.0, text="완료!")

    if st.session_state.batch_results:
        st.divider()
        st.subheader(f"생성 결과 ({len(st.session_state.batch_results)}개)")
        for i, result in enumerate(st.session_state.batch_results):
            if "error" in result:
                with st.expander(f"❌ {result['topic']} — 생성 실패"):
                    st.error(result["error"])
                continue
            article = result["article"]
            html, image_prompts, thumbnail_prompt = render_article_html(article, adsense_client, adsense_slot, use_ads)
            status_icon = "✅🔍" if result.get("research_source") else "✅⚠️"
            with st.expander(f"{status_icon} {article.get('title', result['topic'])}"):
                if result.get("research_source"):
                    st.caption(f"{result['research_source']} 기반으로 작성됨")
                else:
                    st.caption("검색 없이 작성됨 — 발행 전 사실관계 확인 권장")
                st.code(html, language="html")
                st.download_button(
                    "HTML 파일 다운로드", html, file_name=f"article_{i+1}.html", mime="text/html",
                    key=f"dl_batch_{i}",
                )
                if thumbnail_prompt:
                    st.markdown("**썸네일 프롬프트**")
                    st.code(thumbnail_prompt, language=None)
                tags = article.get("tags", [])
                if tags:
                    st.markdown("**태그**")
                    st.code(", ".join(tags), language=None)
                if image_prompts:
                    st.markdown("**이미지 프롬프트 (순서대로)**")
                    for ip in image_prompts:
                        st.markdown(f"{ip['index']}. {ip['heading']}")
                        st.code(ip["prompt"], language=None)
