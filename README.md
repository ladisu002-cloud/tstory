# 네이버 콘텐츠 기회 분석기 V1.2

Creator Advisor에서 직접 선별한 키워드를 입력하면 네이버 API와 AI를 이용해 콘텐츠 기회를 분석하고 글 작성까지 연결하는 Streamlit 앱입니다.

## V1.2 변경사항
- 사이드바 API 설정에 **💾 설정 저장** 버튼 추가
- 저장한 API 키는 현재 Streamlit 브라우저 세션에서 계속 사용
- **🔌 네이버 API 연결 테스트** 추가
- 블로그 검색 API와 검색어 트렌드 API를 분리해서 테스트
- Naver API 401 인증 오류에 대한 안내 강화
- 실제 분석에서도 저장된 세션 설정을 사용

> API 키는 GitHub에 올리지 마세요. 이 버전의 저장 버튼은 영구 파일 저장이 아니라 현재 세션에만 저장합니다.

## 실행
```bash
pip install -r requirements.txt
streamlit run streamlit_app.py
```

## 설정
1. 왼쪽 사이드바에 Gemini API Key 입력
2. Naver Client ID 입력
3. Naver Client Secret 입력
4. **💾 설정 저장** 클릭
5. **🔌 네이버 API 연결 테스트** 클릭
6. 두 API가 정상인지 확인한 뒤 키워드 분석 시작

## 401 오류가 계속될 경우
- Client ID와 Client Secret이 같은 Naver 애플리케이션의 조합인지 확인
- 네이버 개발자센터에서 해당 애플리케이션에 검색어트렌드 API 권한이 활성화되어 있는지 확인
- Client Secret에 앞뒤 공백이 없는지 확인
- API 연결 테스트에서 블로그 검색과 검색어 트렌드 결과를 각각 확인
