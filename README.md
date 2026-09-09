# 네이버 콘텐츠 기회 분석기 V1.1

Creator Advisor에서 직접 선별한 키워드를 입력하면 Naver Open API와 Gemini를 이용해 콘텐츠 기회를 분석하고, 신규 작성/기존 글 업데이트/별도 신규 글 전략을 판단한 뒤 네이버 검색용·홈판용 제목과 글 초안을 생성하는 Streamlit 앱입니다.

## V1.1 변경점
- 기존 티스토리 전용 문구 제거
- 키워드 분석 → 콘텐츠 전략 → 글 작성 흐름 정리
- 네이버 트렌드/블로그/뉴스/웹/이미지 API 사용
- 상품·구매형 키워드에만 쇼핑인사이트 선택 적용
- 내 블로그 관련 글은 검색 결과에서 조건부 비교
- NEW / UPDATE / NEW_SEPARATE 전략 분기
- 검색용 제목과 홈판용 제목 분리
- 콘텐츠 GAP 및 이미지 계획 출력
- 내부 SEO 체크 점수 제공
- API 키는 환경변수 또는 Streamlit Secrets로 관리

## 실행
```bash
pip install -r requirements.txt
cp .env.example .env
streamlit run streamlit_app.py
```

## .env
```env
GEMINI_API_KEY=your_gemini_key
GEMINI_MODEL=gemini-2.5-flash
NAVER_CLIENT_ID=your_naver_client_id
NAVER_CLIENT_SECRET=your_naver_client_secret
NAVER_BLOG_ID=ladisu
```

공개 GitHub 저장소에는 실제 API 키를 절대 커밋하지 마세요.

## 사용 순서
1. Naver Creator Advisor에서 내 블로그와 관련 있는 키워드를 직접 선별합니다.
2. 키워드를 입력하고 카테고리를 선택합니다.
3. `키워드 분석 시작`을 누릅니다.
4. 트렌드, 블로그, 뉴스, 웹, 이미지 결과와 기존 글을 바탕으로 콘텐츠 기회를 확인합니다.
5. `이 분석으로 글 작성`을 누릅니다.
6. 검색용 제목/홈판용 제목/썸네일/본문/FAQ/이미지 계획/태그를 확인합니다.
7. JSON으로 분석 결과를 저장할 수 있습니다.

## 주의
- Naver Search Trend API는 절대 검색량이 아니라 상대적인 관심도 추이입니다.
- Search API 결과만으로 경쟁 블로그의 전체 본문을 분석했다고 주장하지 않습니다.
- 경쟁 블로그 URL 입력은 참고 데이터 수집용이며, 접근이 차단되는 URL은 자동 분석되지 않을 수 있습니다.
- 내부 SEO 점수는 Naver의 실제 점수가 아닙니다.
