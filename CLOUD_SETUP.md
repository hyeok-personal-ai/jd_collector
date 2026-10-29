# Private cloud collection

## 구성

공개 소스 저장소와 별도의 비공개 실행 저장소를 사용합니다. 비공개 저장소의 기본 브랜치에는 검토한 코드와 예약 워크플로만, `state` 브랜치에는 `snapshot.json.gz`만 보관합니다. 공개 저장소나 공개 Actions 아티팩트에 수집 데이터를 올리지 않습니다.

`python -m app.cloud run --repository OWNER/PRIVATE_REPO --budget 600`은 비공개 여부를 확인한 뒤 기존 상태를 읽고 직행 개발 후보·공개 기업 첫 목록 및 인디스워크 모집 분류를 확인합니다. 전송할 공고는 개발·IT로 분류한 것만 포함합니다. 작업 큐·즐겨찾기·로컬 설정·인증정보·전체 과거 주소 목록은 보내지 않습니다.

일반적인 비공개 예약 워크플로는 6시간 간격으로 실행하고 `contents: write` 권한의 해당 저장소 GITHUB_TOKEN을 사용합니다. 체크아웃 인증정보는 보존하지 않으며 수집 산출물을 로그·캐시·Actions 아티팩트로 업로드하지 않습니다. 실제 공고가 있는 상태 브랜치에는 워크플로 파일을 두지 않습니다. 실행 환경은 작업마다 초기화하고 상태 파일로 이어받습니다.

GitHub 예약은 지연되거나 실행 한도에 영향을 받을 수 있습니다. 실시간 서버나 정확한 시각 실행을 보장하지 않습니다. 사이트의 robots·보안 검증·접근 제한도 그대로 적용합니다. 클라우드가 차단 해제 수단은 아닙니다.

## 로컬 연결

기존 GitHub CLI 로그인으로 비공개 저장소를 읽습니다. 앱에 토큰을 저장하지 않습니다.
`data/cloud-config.json`을 다음 형태로 만들며 이 파일은 Git에서 제외됩니다.

```json
{"repository":"OWNER/PRIVATE_REPO","enabled":true}
```

앱을 다시 켜면 즉시 확인하고 이후 5분마다 새 상태를 확인합니다. 화면의 **클라우드 공고 가져오기**로 수동 실행할 수 있습니다. CLI는 `python -m app.cloud sync`입니다.

출처/공고 ID로 합치며 로컬 ID와 즐겨찾기를 유지합니다. 더 최근에 원문을 확인한 로컬 값은 덮어쓰지 않습니다. 확정 마감일은 가져온 뒤에도 로컬에서 만료 처리합니다. 가져오기는 로컬 DB의 기존 쓰기 잠금을 따릅니다. 다운로드·검증 실패 시 기존 공고와 마지막 성공 위치를 보존하고 다음에 재시도합니다.

클라우드 수집 상태는 로컬 수집 기록과 구분됩니다. 한 출처의 실패를 전체 성공으로 해석하지 않습니다. 공개 기업 첫 목록은 기업 전체 페이지나 전체 개발 공고의 완전 수집을 의미하지 않습니다.

## 최초 구성과 유지

비공개 실행 저장소에 검토한 소스를 배치하고, 별도 `state` 브랜치를 준비한 뒤 운영 PC에서 한 번 `python -m app.cloud bootstrap --repository OWNER/PRIVATE_REPO`로 개발 공고와 필요한 수집 위치만 전송합니다. 최초 상태가 공개 저장소에 전송되는 것은 코드에서 차단됩니다. 이후 상태 게시에는 기존 파일 SHA를 사용하므로 경합을 강제로 덮어쓰지 않습니다.

비공개 수집 워크플로를 기본 브랜치에 등록하고 한 번 수동 실행하여 데이터 저장과 로컬 반영을 검증해야 합니다. 별도 장기 개인 토큰이나 로컬 포트 공개가 필요하지 않습니다. 공개 소스 변경이 비공개 실행 저장소에 자동 배포되지는 않으므로 수정 사항을 검토해 반영하세요.


## Recent public discovery

Development mode also samples the latest three official recruitment sitemaps within seven days. It inspects ten public detail pages per batch, retains development candidates and related public company links, and persists the inspection cursor in private state. Non-development detail bodies are not retained by this discovery stage. This expands beyond company first lists; neither the URL backlog nor a completed batch proves full active-development coverage. Local automatic collection continues batches, while Actions resumes within its configured time budget.


## Source access diagnostics

Run `python -m app.source_diagnostics` to compare robots and the first public WordPress list using the normal collector user agent and policy checks. It prints response status, content hashes, safe headers, pagination and runtime versions; never response bodies, cookies, tokens or arbitrary query values. Cloud collection records the failing response metadata in private state. A successful probe cannot prove every later page is accessible.

WordPress pages are saved incrementally. Completed pages survive a later failure or time limit, restored pages are read in numerical order, and absence checks run only after the current list has been fully reconciled. Access denials still stop collection; no challenge-solving or alternate identities are used.
