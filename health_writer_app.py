import os
import time
import urllib.parse
import requests
import streamlit as st
from google import genai
from google.genai import types
from dotenv import load_dotenv

load_dotenv()

st.set_page_config(page_title="티스토리 건강 블로그 원고생성기", page_icon="💪", layout="wide")

# gemini-2.5-flash는 2026-08 기준 안정 버전입니다.
# 더 최신 모델(예: gemini-3-flash)에 접근 권한이 있으면 이 값만 바꿔주세요.
MODEL = "gemini-2.5-flash"

PROGRAMS = ["보이스 오브 피트니스 (tvN)", "수퍼푸드 (tvN)", "직접 입력"]


def ask_gemini(client, prompt: str, max_output_tokens: int = 2000, use_search: bool = False) -> str:
    config_kwargs = {"max_output_tokens": max_output_tokens}
    if use_search:
        config_kwargs["tools"] = [types.Tool(google_search=types.GoogleSearch())]
    resp = client.models.generate_content(
        model=MODEL,
        contents=prompt,
        config=types.GenerateContentConfig(**config_kwargs),
    )
    return resp.text or ""


def generate_pollinations_image(prompt: str, width: int = 768, height: int = 768):
    encoded = urllib.parse.quote(prompt)
    url = f"https://image.pollinations.ai/prompt/{encoded}?width={width}&height={height}&nologo=true&model=flux"
    try:
        resp = requests.get(url, timeout=30)
        if resp.status_code == 200 and resp.headers.get("content-type", "").startswith("image"):
            return resp.content
    except requests.RequestException:
        pass
    return None


def search_pixabay_images(query: str, api_key: str, per_page: int = 6):
    url = "https://pixabay.com/api/"
    params = {"key": api_key, "q": query, "image_type": "photo", "per_page": per_page, "safesearch": "true"}
    try:
        resp = requests.get(url, params=params, timeout=15)
        if resp.status_code == 200:
            return resp.json().get("hits", [])
    except requests.RequestException:
        pass
    return []


with st.sidebar:
    st.header("설정")
    api_key = st.text_input(
        "Gemini API 키", value=os.getenv("GEMINI_API_KEY", ""), type="password",
        help="aistudio.google.com/apikey 에서 발급받은 키를 입력하세요.",
    )
    pixabay_key = st.text_input(
        "Pixabay API 키 (선택)", value=os.getenv("PIXABAY_API_KEY", ""), type="password",
        help="실사 스톡사진을 검색하려면 입력하세요. pixabay.com/api/docs 에서 무료 발급.",
    )
    st.divider()
    post_type = st.radio("글쓰기 유형", ["홈판형 (경험·공감 위주)", "검색형 (정보·해결 위주)"], index=1)

if not api_key:
    st.info("왼쪽 사이드바에 Gemini API 키를 입력하면 시작할 수 있어요.")
    st.stop()

client = genai.Client(api_key=api_key)

st.title("💪 티스토리 건강 블로그 원고생성기")
st.caption("방송 예고/하이라이트를 소재로, 웹검색으로 배경지식을 보강해서 건강 정보 글을 씁니다. 발행은 완성된 원고를 복사해서 직접 붙여넣는 방식입니다.")

for key in ["research", "titles", "chosen_title", "body", "image_keywords"]:
    if key not in st.session_state:
        st.session_state[key] = None

# ---------- 1단계: 소재 입력 ----------
st.subheader("1. 오늘의 소재")
program = st.selectbox("어떤 프로그램에서 가져온 소재인가요?", PROGRAMS)
clip_text = st.text_area(
    "방송 페이지에서 예고/하이라이트 제목이나 설명을 복사해서 붙여넣으세요",
    placeholder="예: [예고] 굽은 등 펴주는 5분 스트레칭, 오늘 밤 공개! / 예: 오늘 소개된 슈퍼푸드 - 여름철 지친 몸에 좋은 토마토",
    height=100,
)

st.caption("⚠️ 방송사 사이트는 자동 스크래핑이 막혀있어 이 칸은 직접 복사·붙여넣기 해주셔야 해요.")

# ---------- 2단계: 웹검색으로 배경지식 보강 ----------
st.subheader("2. 관련 배경지식 검색")
if st.button("관련 정보 검색", disabled=not clip_text.strip()):
    with st.spinner("웹에서 관련 건강 정보를 찾고 있어요..."):
        prompt = f"""다음은 건강 관련 방송 프로그램({program})의 예고/하이라이트 내용입니다.

"{clip_text}"

이 소재에서 다루는 건강 주제(운동법, 식재료, 증상 관리 등)에 대해 웹 검색으로
신뢰할 수 있는 일반적인 배경지식을 찾아서 정리해주세요.
- 방송 내용을 그대로 옮기지 말고, 그 주제 자체에 대한 공신력 있는 정보를 찾아주세요.
- 효능을 과장하거나 특정 질병 치료를 단정하는 표현은 쓰지 마세요.
- 5~8줄 정도로 핵심만 정리해주세요."""
        st.session_state.research = ask_gemini(client, prompt, max_output_tokens=1500, use_search=True)

if st.session_state.research:
    st.markdown("**검색으로 찾은 배경지식**")
    st.info(st.session_state.research)

# ---------- 3단계: 제목 생성 ----------
if st.session_state.research:
    st.subheader("3. 제목 후보")
    if st.button("제목 후보 만들기", type="primary"):
        with st.spinner("제목을 만들고 있어요..."):
            type_guide = (
                "경험과 공감 위주로" if "홈판형" in post_type else "검색해서 들어온 사람이 필요한 답을 빠르게 찾도록"
            )
            prompt = f"""티스토리 건강 블로그에 올릴 글의 제목을 짓는 카피라이터입니다.
소재: {clip_text}
배경지식: {st.session_state.research}
방향: {type_guide} 제목을 지어주세요.

검색 노출에 유리하면서 클릭하고 싶은 제목 5개를, 번호 없이 한 줄씩 출력하세요.
과장된 효능 단정 표현(완치, 100% 등)은 쓰지 마세요."""
            text = ask_gemini(client, prompt, max_output_tokens=500)
            st.session_state.titles = [t.strip() for t in text.strip().split("\n") if t.strip()]
            st.session_state.body = None

if st.session_state.titles:
    st.subheader("4. 제목 선택 & 본문 생성")
    choice = st.radio("제목을 고르거나 직접 수정하세요", st.session_state.titles, key="title_radio")
    custom_title = st.text_input("직접 쓰기", value="")
    st.session_state.chosen_title = custom_title.strip() if custom_title.strip() else choice

    if st.button("본문 쓰기", type="primary"):
        with st.spinner("AI가 본문을 작성하고 있어요..."):
            type_guide = (
                "실제로 겪은 것처럼 자연스러운 구어체로"
                if "홈판형" in post_type
                else "핵심 정보를 앞에 배치하고 소제목으로 구조화해서"
            )
            prompt = f"""티스토리 건강 블로그 글을 쓰는 작가입니다.
제목: {st.session_state.chosen_title}
소재: {clip_text}
배경지식: {st.session_state.research}

{type_guide} 900~1300자 분량의 본문을 작성하세요.
- 문단은 2~4줄 단위로 짧게 끊어주세요.
- 의료 정보이므로 단정적 치료 효과 주장은 피하고, "개인차가 있을 수 있다", "증상이 지속되면 전문의와 상담하라"는 취지를 자연스럽게 포함하세요.
- 광고처럼 딱딱하지 않게 자연스러운 말투로 써주세요.
- 사진이 들어갈 지점에 [사진: 어떤 장면인지 설명] 형태로 표시하세요.
- 본문만 출력하세요."""
            st.session_state.body = ask_gemini(client, prompt, max_output_tokens=2000)

if st.session_state.body:
    st.subheader("5. 완성된 원고")
    st.markdown("**제목**")
    st.code(st.session_state.chosen_title, language=None)
    st.markdown("**본문**")
    st.code(st.session_state.body, language=None)

    st.subheader("6. 이미지 만들기")
    img_tab1, img_tab2 = st.tabs(["🎨 AI 이미지 생성 (무료, 키 불필요)", "📷 실사 스톡사진 검색 (Pixabay)"])

    if st.button("본문에서 이미지 프롬프트 뽑기"):
        with st.spinner("이미지 프롬프트를 뽑고 있어요..."):
            prompt = f"""아래 본문의 [사진: ...] 지점들을 영어 이미지 생성 프롬프트로 바꿔주세요.
한 줄에 하나씩만 출력하세요.

본문:
{st.session_state.body}"""
            text = ask_gemini(client, prompt, max_output_tokens=500)
            st.session_state.image_keywords = [t.strip() for t in text.strip().split("\n") if t.strip()]

    if st.session_state.image_keywords:
        with img_tab1:
            if st.button("이 프롬프트로 이미지 생성", key="gen_poll"):
                for i, kw in enumerate(st.session_state.image_keywords):
                    with st.spinner(f"생성 중... ({i+1}/{len(st.session_state.image_keywords)})"):
                        img_bytes = generate_pollinations_image(kw)
                        if img_bytes:
                            st.image(img_bytes, caption=kw, width=400)
                            st.download_button("다운로드", img_bytes, file_name=f"image_{i+1}.jpg", mime="image/jpeg", key=f"dl_{i}")
                        else:
                            st.warning(f"생성 실패: {kw}")
                        time.sleep(15)
        with img_tab2:
            if not pixabay_key:
                st.info("사이드바에 Pixabay API 키를 입력하면 실사 사진을 검색할 수 있어요.")
            elif st.button("이 프롬프트로 사진 검색", key="search_pixabay"):
                for kw in st.session_state.image_keywords:
                    st.write(f"**{kw}**")
                    hits = search_pixabay_images(kw, pixabay_key)
                    if hits:
                        cols = st.columns(min(len(hits), 4))
                        for col, hit in zip(cols, hits):
                            with col:
                                st.image(hit["webformatURL"], use_container_width=True)
                                st.caption(f"[원본 보기]({hit['pageURL']})")
                    else:
                        st.warning("검색 결과 없음")

    st.divider()
    st.success("완성됐어요! 제목/본문을 복사해서 티스토리 편집기에 붙여넣고, 이미지는 다운로드해서 직접 첨부해주세요.")
