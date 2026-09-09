# 네이버 콘텐츠 기회 분석기 V1.4

NAVER API HUB + Gemini 기반의 네이버 콘텐츠 기회 분석기입니다.

## V1.4 주요 변경
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
