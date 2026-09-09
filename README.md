# 네이버 콘텐츠 기회 분석기 V1.3

V1.3에서는 Streamlit Secrets를 실제로 읽도록 수정했습니다.

## Streamlit Secrets

다음 중 하나의 형태로 설정할 수 있습니다.

### 단일 키 형태
```toml
GEMINI_API_KEY = "..."
NAVER_CLIENT_ID = "..."
NAVER_CLIENT_SECRET = "..."
NAVER_BLOG_ID = "..."
```

### 그룹 형태
```toml
[naver]
client_id = "..."
client_secret = "..."
blog_id = "..."

[gemini]
api_key = "..."
```

Secrets를 수정한 뒤에는 Streamlit 앱을 재시작/재배포하세요.

## 중요

Client ID/Secret은 GitHub에 올리지 마세요.
