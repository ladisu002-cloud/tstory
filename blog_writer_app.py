import os
import time
import urllib.parse
import requests
import streamlit as st
from google import genai
from google.genai import types
from dotenv import load_dotenv

load_dotenv()

st.set_page_config(page_title="블로그 원고 자동생성기", page_icon="✍️", layout="wide")

# gemini-2.5-flash는 2026-08 기준 안정 버전입니다.
# 더 최신 모델(예: gemini-3-flash)에 접근 권한이 있으면 이 값만 바꿔주세요.
MODEL = "gemini-2.5-flash"


def ask_gemini(client, prompt: str, max_output_tokens: int = 2000) -> str:
    resp = client.models.generate_content(
        model=MODEL,
        contents=prompt,
        config=types.GenerateContentConfig(max_output_tokens=max_output_tokens),
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
        "Gemini API 키",
        value=os.getenv("GEMINI_API_KEY", ""),
        type="password",
        help="aistudio.google.com/apikey 에서 발급받은 키를 입력하세요.",
    )
    pixabay_key = st.text_input(
        "Pixabay API 키 (선택)",
        value=os.getenv("PIXABAY_API_KEY", ""),
        type="password",
        help="실사 스톡사진을 검색하려면 입력하세요. pixabay.com/api/docs 에서 무료 발급.",
    )
    st.divider()
    channel = st.radio("발행 예정 채널", ["네이버 블로그", "티스토리"], help="채널에 따라 문체/구성 톤을 살짝 다르게 씁니다.")
    post_type = st.radio(
        "글쓰기 유형",
        ["홈판형 (경험·공감 위주)", "검색형 (정보·해결 위주)"],
    )

if not api_key:
    st.info("왼쪽 사이드바에 Gemini API 키를 입력하면 시작할 수 있어요.")
    st.stop()

client = genai.Client(api_key=api_key)

st.title("✍️ 블로그 원고 자동생성기")
st.caption("제목 → 본문 → 이미지 키워드까지 만들고, 완성되면 복사해서 네이버/티스토리에 직접 붙여넣으세요.")

for key in ["titles", "chosen_title", "body", "image_keywords"]:
    if key not in st.session_state:
        st.session_state[key] = None

# ---------- 1단계: 주제 입력 & 제목 생성 ----------
st.subheader("1. 주제 입력")
topic = st.text_area("어떤 글을 쓸까요? (키워드, 상품명, 최근 겪은 일 등을 자유롭게 적어주세요)", height=100)
extra_context = st.text_area(
    "참고할 내용 (선택)",
    placeholder="예: 이 상품의 특징, 가격대, 본인 경험담, 강조하고 싶은 포인트 등",
    height=80,
)

if st.button("제목 후보 만들기", type="primary", disabled=not topic.strip()):
    with st.spinner("제목을 만들고 있어요..."):
        type_guide = (
            "네이버 홈판·추천 영역에 걸리도록 경험과 공감 위주로"
            if "홈판형" in post_type
            else "검색해서 들어온 사람이 필요한 답을 빠르게 찾도록 정보 중심으로"
        )
        prompt = f"""당신은 {channel}에 올릴 블로그 글의 제목을 짓는 카피라이터입니다.
주제: {topic}
참고 내용: {extra_context or '없음'}
글쓰기 방향: {type_guide} 제목을 지어주세요.

검색 노출에 유리하면서도 클릭하고 싶게 만드는 제목 5개를 만들어주세요.
각 제목은 한 줄씩, 번호 없이 줄바꿈으로만 구분해서 출력하세요. 다른 설명은 붙이지 마세요."""

        text = ask_gemini(client, prompt, max_output_tokens=500)
        st.session_state.titles = [t.strip() for t in text.strip().split("\n") if t.strip()]
        st.session_state.chosen_title = None
        st.session_state.body = None

if st.session_state.titles:
    st.subheader("2. 제목 선택")
    choice = st.radio("마음에 드는 제목을 고르거나, 직접 수정하세요", st.session_state.titles, key="title_radio")
    custom_title = st.text_input("직접 쓰기 (입력하면 이 제목을 사용합니다)", value="")
    st.session_state.chosen_title = custom_title.strip() if custom_title.strip() else choice

    st.subheader("3. 본문 생성")
    if st.button("본문 쓰기", type="primary"):
        with st.spinner("AI가 본문을 작성하고 있어요..."):
            type_guide = (
                "경험과 공감 위주로, 실제로 겪은 것처럼 자연스러운 구어체로"
                if "홈판형" in post_type
                else "검색 의도에 맞게 핵심 정보를 앞부분에 배치하고, 소제목으로 구조화해서"
            )
            prompt = f"""당신은 {channel}에 올릴 블로그 글을 쓰는 작가입니다.
제목: {st.session_state.chosen_title}
주제: {topic}
참고 내용: {extra_context or '없음'}

{type_guide} 800~1200자 분량의 본문을 작성하세요.
- 문단은 2~4줄 단위로 짧게 끊어주세요 (모바일 가독성).
- 광고처럼 딱딱하지 않게, 사람이 직접 쓴 것처럼 자연스러운 말투로 써주세요.
- 본문 중간중간 사진이 들어갈 만한 지점에 [사진: 어떤 장면인지 간단 설명] 형태로 표시해주세요.
- 본문만 출력하고, 제목이나 다른 설명은 붙이지 마세요."""

            st.session_state.body = ask_gemini(client, prompt, max_output_tokens=2000)

if st.session_state.body:
    st.subheader("4. 완성된 원고")
    st.markdown("**제목**")
    st.code(st.session_state.chosen_title, language=None)
    st.markdown("**본문**")
    st.code(st.session_state.body, language=None)

    st.subheader("5. 이미지 만들기")
    img_tab1, img_tab2 = st.tabs(["🎨 AI 이미지 생성 (완전 무료, 키 불필요)", "📷 실사 스톡사진 검색 (Pixabay, 무료 키 필요)"])

    if st.button("본문에서 이미지 프롬프트 뽑기"):
        with st.spinner("이미지 프롬프트를 뽑고 있어요..."):
            prompt = f"""아래 블로그 본문에서 [사진: ...] 표시가 된 지점들을 찾아,
각각을 이미지 생성 도구에 바로 넣을 수 있는 영어 프롬프트로 변환해주세요.
사진 지점 순서대로, 한 줄에 하나씩만 출력하세요. 다른 설명은 붙이지 마세요.

본문:
{st.session_state.body}"""
            text = ask_gemini(client, prompt, max_output_tokens=500)
            st.session_state.image_keywords = [t.strip() for t in text.strip().split("\n") if t.strip()]

    if st.session_state.image_keywords:
        with img_tab1:
            st.caption("Pollinations.ai (Flux 모델) — 회원가입도 API 키도 필요 없습니다.")
            if st.button("이 프롬프트로 이미지 생성", key="gen_pollinations"):
                for i, kw in enumerate(st.session_state.image_keywords):
                    with st.spinner(f"이미지 생성 중... ({i+1}/{len(st.session_state.image_keywords)})"):
                        img_bytes = generate_pollinations_image(kw)
                        if img_bytes:
                            st.image(img_bytes, caption=kw, width=400)
                            st.download_button(
                                "이 이미지 다운로드", img_bytes, file_name=f"image_{i+1}.jpg",
                                mime="image/jpeg", key=f"dl_poll_{i}",
                            )
                        else:
                            st.warning(f"생성 실패: {kw} (잠시 후 다시 시도해주세요)")
                        time.sleep(15)  # 익명 요청 속도 제한(약 15초당 1회) 대응

        with img_tab2:
            if not pixabay_key:
                st.info("왼쪽 사이드바에 Pixabay API 키를 입력하면 실사 스톡사진을 검색할 수 있어요.")
            else:
                if st.button("이 프롬프트로 사진 검색", key="search_pixabay"):
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
                            st.warning("검색 결과가 없어요.")
    else:
        st.caption("먼저 위 버튼으로 본문에서 이미지 프롬프트를 뽑아주세요.")

    st.divider()
    st.success("완성됐어요! 제목/본문을 복사해서 네이버 블로그나 티스토리 편집기에 붙여넣고, 이미지는 다운로드한 파일을 직접 첨부해주세요.")
