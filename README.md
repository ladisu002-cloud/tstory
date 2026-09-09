# 네이버 콘텐츠 기회 분석기 v1

기존 티스토리 건강 글 생성기를 네이버 블로그 콘텐츠 기회 분석기로 전환한 Streamlit MVP입니다.

## 핵심 흐름

Creator Advisor에서 사용자가 직접 선별한 키워드
→ 네이버 검색어 트렌드
→ 블로그/뉴스/웹/이미지 검색
→ (선택) 쇼핑인사이트
→ 내 블로그 관련 글 확인
→ AI 검색의도/콘텐츠 GAP/홈판 전략 분석
→ 신규/업데이트/별도 신규 전략 결정
→ 네이버용 글 작성
→ 내부 SEO 체크

> Creator Advisor 자체 데이터를 자동 수집하지 않습니다. 사용자가 키워드를 직접 입력합니다.

## 현재 버전의 한계

- 네이버 검색 API의 블로그 결과를 이용해 내 기존 글을 찾습니다. 오래된 글이나 검색 결과 1000위 밖의 글은 놓칠 수 있습니다.
- 경쟁 콘텐츠 분석은 Search API의 제목/설명 등으로 요약합니다. 네이버 블로그 전체 본문을 자동 크롤링하는 기능은 넣지 않았습니다.
- 쇼핑인사이트 키워드 API는 카테고리 코드가 필요하므로 상품 키워드일 때 사용자가 카테고리 코드를 입력합니다.
- SEO 점수는 네이버의 실제 점수가 아니라 내부 체크리스트입니다.

## 실행

```bash
pip install -r requirements.txt
cp env.example .env
streamlit run streamlit_app.py
```

Streamlit Cloud에서는 Secrets에 다음을 넣으면 됩니다.

```toml
GEMINI_API_KEY = "..."
GEMINI_MODEL = "gemini-2.5-flash"
NAVER_CLIENT_ID = "..."
NAVER_CLIENT_SECRET = "..."
NAVER_BLOG_ID = "..."
```

## 기존 저장소에서 교체

현재 저장소의 기존 `streamlit_app.py`, `requirements.txt`, `env.example`, `README.md`를 이 버전으로 교체하면 됩니다.

기존 티스토리 전용 광고/쿠팡/HTML 렌더링 로직은 제거했습니다.
