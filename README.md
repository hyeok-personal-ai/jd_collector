# Recruit Radar / jd_collector

공개 채용 경로를 확인하고 공고를 수집·검색하는 로컬 Python 앱의 소스 기록입니다.
운영 데이터, 실제 공고 본문, 개인 설정 및 인증정보는 포함하지 않습니다.

## 실행

Python 3.11 이상이 필요합니다.

```sh
python -m venv .venv
# Windows: .venv\Scripts\activate
# Linux/macOS: source .venv/bin/activate
python -m pip install -e ".[dev]"
python -X utf8 -m app.launcher
```

브라우저에서 http://127.0.0.1:8000/ 을 엽니다. Windows에서는 `./start.ps1`도 사용할 수 있습니다.
새 실행은 빈 DB로 시작합니다. 서버의 수집 설정에서 출처와 자동 수집을 확인하세요.
`.env.example`은 설정 참고용이며 앱이 자동으로 읽지는 않습니다. 필요한 값은 환경변수로 설정합니다.

## 검증

```sh
python scripts/check_publication.py
python -X utf8 -m pytest -q
python -X utf8 -m app.offline_replay
```

테스트 데이터는 합성 샘플입니다. 실제 사이트 응답을 요구하는 테스트는 해당 자료가 없어 건너뜁니다.
GitHub Actions는 Linux/Windows, Python 3.11/3.12에서 공개 파일 점검·테스트·오프라인 재현을 실행합니다.
실제 사이트 수집, 로컬 DB 동기화, 운영 데이터 업로드는 실행하지 않습니다.

## 현재 구현 범위

- 직행: 공개 기업 첫 목록·관련 공고에서 개발 후보 발견, 공개 본문 및 상태 반영, 이어받기.
- 인디스워크: 공개 모집 분류 동기화. WordPress/CDN 보안 검증이 발생하면 중단하고 재확인 대기.
- 확정 마감일의 로컬 만료 처리, 상시·조기마감·미확인 기한의 원문 재확인.
- 검색·필터·즐겨찾기·CSV·로컬 백업과 수집 현황.
- 공개 ATS 및 기업별 경로의 일부 어댑터. 전체 기업 및 전체 개발 공고 수집 완료는 아닙니다.

사이트의 접근 정책과 이용 조건을 확인하고 사용해야 합니다. robots 확인 실패·로그인·보안 검증은 우회하지 않습니다.
공개 URL이 있다는 이유만으로 본문 재배포 권한이 생기는 것은 아닙니다.
수집 결과는 원문과 대조하세요. 전체 공고 확보와 실시간 정확성을 보장하지 않습니다.

공개 범위와 제외 기준은 [PUBLICATION.md](PUBLICATION.md), 구현 기록은 [CHANGELOG.md](CHANGELOG.md)를 확인하세요.
