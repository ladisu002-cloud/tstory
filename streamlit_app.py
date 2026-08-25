import os
import json
import urllib.parse
import streamlit as st
from google import genai
from google.genai import types
from dotenv import load_dotenv

load_dotenv()

st.set_page_config(page_title="티스토리 SEO 블로그 원고생성기", page_icon="📝", layout="wide")

# gemini-2.5-flash는 2026-08 기준 안정 버전입니다.
# 더 최신 모델(예: gemini-3-flash)에 접근 권한이 있으면 이 값만 바꿔주세요.
MODEL = "gemini-2.5-flash"
MAX_BATCH_TOPICS = 30


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


def build_article_prompt(topic: str, tone: str, length_label: str) -> str:
    length_guide = {
        "짧게 (5문단 내외)": "각 섹션 본문은 2~3문장, 전체 3~4개 섹션",
        "보통 (기본)": "각 섹션 본문은 3~5문장, 전체 4~5개 섹션",
        "길게 (상세)": "각 섹션 본문은 5~7문장, 전체 5~7개 섹션",
    }.get(length_label, "각 섹션 본문은 3~5문장, 전체 4~5개 섹션")

    return f"""당신은 티스토리에 올릴 SEO 최적화 블로그 글을 쓰는 전문 작가입니다.
주제: {topic}
말투: {tone}
분량: {length_guide}

아래 JSON 형식으로만 응답하세요. 다른 설명, 코드펜스, 다른 텍스트는 절대 붙이지 마세요.

{{
  "meta_description": "검색결과 요약에 쓰일 1~2문장 (60자 내외, 핵심 키워드 포함)",
  "title": "SEO에 유리하면서 클릭하고 싶은 제목 (30자 내외)",
  "intro": "글 도입부 2~3문장, 주제에 대한 공감과 궁금증 유발",
  "cta_text": "본문 상단에 넣을 짧은 행동유도 문구 (예: 🔍 OOO 확인해보세요!)",
  "sections": [
    {{
      "heading": "소제목",
      "content_html": "본문 내용. <p>, <b>, <ul><li> 태그만 사용한 HTML 조각. 의료/건강 주제면 단정적 치료 효과 주장은 피하고 개인차가 있을 수 있다는 취지를 자연스럽게 포함.",
      "image_prompt": "이 섹션과 어울리는 이미지를 위한 영어 프롬프트 (사진 스타일, 구체적으로)"
    }}
  ],
  "conclusion": "마무리 문단 2~3문장, 핵심 요약과 다음 행동 제안"
}}

sections는 {length_guide.split(',')[1].strip() if ',' in length_guide else '4~5개'} 정도로 구성하세요.
과장된 효능 단정 표현(완치, 100% 등)은 쓰지 마세요."""


def pollinations_url(prompt: str, width: int = 800, height: int = 500) -> str:
    encoded = urllib.parse.quote(prompt)
    return f"https://image.pollinations.ai/prompt/{encoded}?width={width}&height={height}&nologo=true&model=flux"


# =========================================================
# 고정 HTML 템플릿 렌더링
# =========================================================
def render_article_html(article: dict, adsense_client: str, adsense_slot: str, use_ads: bool) -> str:
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

    section_html_parts = []
    for i, s in enumerate(sections):
        img_prompt = s.get("image_prompt", "")
        img_tag = ""
        if img_prompt:
            img_url = pollinations_url(img_prompt)
            img_tag = f"""<!-- 이미지 프롬프트: {img_prompt} -->
<img src="{img_url}" alt="{s['heading']}" style="width: 100%; max-width: 700px; border-radius: 8px; display: block; margin: 15px auto;" />"""

        # 두 번째 섹션 다음에 본문 중간 광고 삽입 (클릭률 높은 위치)
        mid_ad = ad_block() if (use_ads and i == 1) else ""

        section_html_parts.append(f"""
<div id="section{i+1}">
<h2 style="background: #3498db; color: white; padding: 10px 15px; border-radius: 5px; font-size: 22px; margin: 30px 0 20px;">{s['heading']}</h2>
{mid_ad}
{img_tag}
{s.get('content_html', '')}
</div>""")

    sections_html = "\n".join(section_html_parts)
    top_ad = ad_block()
    bottom_ad = ad_block()

    return f"""<div class="blog-post">
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

{bottom_ad}

<div style="background-color: #e8f4fc; padding: 20px; border-radius: 8px; margin: 30px 0;">
<h2 style="color: #2c3e50; font-size: 22px; margin: 0 0 15px 0;">결론</h2>
<p style="font-size: 16px; line-height: 1.8; color: #333;">{article.get('conclusion', '')}</p>
</div>
</div>"""


def generate_article(client, topic: str, tone: str, length_label: str) -> dict:
    prompt = build_article_prompt(topic, tone, length_label)
    return call_gemini_json(client, prompt)


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
    tone = st.selectbox("말투", ["친근한 존댓말", "정보 전달형 담백한 문체", "전문가 톤"])
    length_label = st.selectbox("분량", ["짧게 (5문단 내외)", "보통 (기본)", "길게 (상세)"], index=1)
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
if "batch_results" not in st.session_state:
    st.session_state.batch_results = None

tab_single, tab_batch = st.tabs(["📝 하나씩 만들기", "📋 여러 주제 한 번에 생성 (배치)"])

# =========================================================
# 탭 1: 하나씩
# =========================================================
with tab_single:
    topic = st.text_input("주제를 입력하세요", placeholder="예: 알부민 효능과 부족 증상")

    if st.button("글 생성하기", type="primary", disabled=not topic.strip()):
        with st.spinner("SEO 글을 작성하고 있어요..."):
            try:
                st.session_state.single_article = generate_article(client, topic, tone, length_label)
            except Exception as e:
                st.error(f"생성 중 오류가 발생했어요: {e}")
                st.session_state.single_article = None

    if st.session_state.single_article:
        html = render_article_html(st.session_state.single_article, adsense_client, adsense_slot, use_ads)
        st.subheader("미리보기")
        st.components.v1.html(html, height=1200, scrolling=True)
        st.subheader("HTML 코드 (티스토리 HTML 모드에 붙여넣기)")
        st.code(html, language="html")
        st.download_button("HTML 파일 다운로드", html, file_name="article.html", mime="text/html")
        st.caption("이미지는 Pollinations.ai 링크로 자동 삽입돼요. 영구 보존하려면 이미지를 다운로드해서 티스토리에 직접 업로드 후 src를 교체해주세요.")

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
                article = generate_article(client, t, tone, length_label)
                st.session_state.batch_results.append({"topic": t, "article": article})
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
            html = render_article_html(article, adsense_client, adsense_slot, use_ads)
            with st.expander(f"✅ {article.get('title', result['topic'])}"):
                st.code(html, language="html")
                st.download_button(
                    "HTML 파일 다운로드", html, file_name=f"article_{i+1}.html", mime="text/html",
                    key=f"dl_batch_{i}",
                )
