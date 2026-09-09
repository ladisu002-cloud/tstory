# 네이버 콘텐츠 기회 분석기 V1.6

NAVER API HUB + Gemini 기반의 네이버 콘텐츠 기회 분석기입니다.

## V1.6 주요 변경
- 기존 `openapi.naver.com` 방식 제거
- NAVER API HUB 검색 API 사용
- NAVER API HUB 검색어 트렌드 사용
- NAVER API HUB 쇼핑인사이트 사용
- API HUB 인증 헤더 사용
  - `X-NCP-APIGW-API-KEY-ID`
  - `X-NCP-APIGW-API-KEY`
- 블로그/뉴스/웹문서/이미지 검색 지원
- Streamlit Secrets와 환경변수 지원

## Streamlit Secrets
```toml
NAVER_CLIENT_ID = "..."
NAVER_CLIENT_SECRET = "..."
NAVER_BLOG_ID = "..."
GEMINI_API_KEY = "..."
```

또는 `[naver]`, `[gemini]` 그룹 형식도 지원합니다.

## 실행
```bash
pip install -r requirements.txt
streamlit run streamlit_app.py
```

API 키는 코드나 GitHub에 직접 넣지 마세요.


## V1.6 변경사항
- 콘텐츠 GAP 체크를 단순 경고가 아닌 없음/완료/일부/보완 필요 상태로 표시합니다.
- 글 작성 시 서론 → 본문 → 마무리 구조를 강제합니다.
- 네이버 스마트에디터용 복사 영역을 추가했습니다. 브라우저에서 HTML/텍스트 클립보드로 복사를 시도합니다.


### V2.0 변경사항
- 블로그 카테고리: 리뷰 / 맛집 / 일상 / 쇼핑정보 / 여행정보 / 핫이슈 / 기타정보
- 카테고리와 검색의도에 따라 목차를 선택적으로 생성
- 모든 글에 자연스러운 서론을 먼저 작성하도록 강화
- 콘텐츠 GAP을 반영/부분 반영/미반영으로 실제 체크
- 네이버 스마트에디터용 순수 본문 복사 영역 추가
