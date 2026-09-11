# Codex·Claude에서 Apple 미리 알림 사용하기

메모를 할 일로 바꾸고, 오늘 할 일을 확인하고, 날짜를 옮기거나 완료 처리할 수 있습니다.
**macOS 14 이상**의 Mac과 Apple 미리 알림 앱이 필요합니다.
Apple silicon과 Intel Mac용 실행 환경이 모두 들어 있습니다.

이 문서는 **v0.8.0** 기준입니다. 설치 전에
[해당 릴리스](https://github.com/Oscar-V4/apple-reminders/releases/tag/v0.8.0)와
[검증 기록](release-evidence/claude-support-0.8.0.md)을 확인하세요.

## Codex에 설치

Codex에 다음 명령을 실행해 달라고 하거나, `codex` 명령을 쓸 수 있는 터미널에서 실행하세요.

```bash
codex plugin marketplace add Oscar-V4/apple-reminders --ref v0.8.0
codex plugin add apple-reminders@oscar-v4-reminders
```

설치 후 **새 Codex 작업**을 여세요.

## Claude Code에 설치

Claude Code 대화창에서 다음 명령을 차례로 실행하세요.

```text
/plugin marketplace add https://github.com/Oscar-V4/apple-reminders.git#v0.8.0
/plugin install apple-reminders@oscar-v4-reminders
```

터미널에서는 `/plugin` 대신 `claude plugin`을 쓰면 됩니다.
설치 후 Claude Code 세션을 다시 시작하세요. 필요한 경우
`/apple-reminders:apple-reminders`로 기본 스킬을 직접 부를 수 있습니다.

## Claude Desktop에 설치

1. [apple-reminders-0.8.0.mcpb 다운로드](https://github.com/Oscar-V4/apple-reminders/releases/download/v0.8.0/apple-reminders-0.8.0.mcpb)
2. Claude Desktop의 **Settings → Extensions → Advanced settings → Install Extension…** 선택
3. 내려받은 파일을 선택하고 설치 안내에 따라 확장을 활성화
4. 새 대화 시작

JSON 설정 파일을 직접 편집하지 않아도 됩니다. Desktop 확장은 MCP 도구와
서버 안내를 제공하며, Claude Code용 스킬을 자동으로 불러오지는 않습니다.
조직 계정은 관리자의 로컬 확장 설치 정책을 따릅니다.

## 처음 실행할 때

다음처럼 요청하세요.

> 기한이 지난 할 일과 오늘까지 해야 할 일을 보여줘.

macOS가 플러그인의 서명된 헬퍼에 미리 알림 접근을 허용할지 물으면 허용하세요.
Claude의 도구 사용 승인과 macOS의 데이터 접근 권한은 별개입니다.
권한을 거절했거나 나중에 해제했다면 **시스템 설정 → 개인정보 보호 및 보안 → 미리 알림**에서
헬퍼의 접근 권한을 다시 켜세요.

이후에는 자연스럽게 요청하면 됩니다.

- “이 회의록의 후속 작업을 업무 목록에 넣어줘.”
- “영수증 제출을 금요일 오후 3시로 등록해줘.”
- “그 할 일의 날짜를 월요일로 옮기고 메모와 알림은 유지해줘.”
- “이번 주에 해야 할 일을 정리해줘.”

## npm은 필요 없나요?

필요 없습니다. 이 플러그인은 Python 실행 환경과 서명·공증된 macOS 헬퍼를
포함합니다. Node.js, npm, 별도 Python, Homebrew, Xcode를 설치할 필요가 없습니다.
Codex와 Claude Code는 자체 플러그인 설치 기능을 쓰고, Desktop은 `.mcpb` 파일로
설치하는 편이 간단합니다. 이 버전용 npm 패키지는 배포하지 않습니다.

## 업데이트와 삭제

Codex와 Claude Code 설치는 특정 버전에 고정됩니다. 새 버전으로 바꾸려면
[영문 안내의 업데이트 명령](../README.md#upgrade)을 실행하고 새 세션을 시작하세요.
Desktop은 새 릴리스의 `.mcpb` 파일을 같은 확장 설정 화면에서 다시 설치하면 됩니다.
직접 배포한 확장 파일은 수동으로 업데이트합니다.

[삭제 안내](../README.md#uninstall)를 따라 플러그인이나 확장을 제거해도
미리 알림에 저장된 할 일은 남습니다. Mac에서 Codex와 Claude를 함께 쓰면
실행 캐시와 작업 기록도 공유합니다. 로컬 지원 파일까지 지우려면 먼저 모든 클라이언트를
종료하고 [전체 삭제 안내](installation.md#full-removal)를 확인하세요.

## 알아둘 범위

기본 도구는 15개입니다. 날짜·메모·완료 등 일반 항목은 Apple의 EventKit을 사용합니다.
섹션·태그·네이티브 첨부·최근 삭제 항목 복구는 macOS와 미리 알림 버전에 따른
별도 지원 검사를 통과해야 합니다. 이번 버전에는 “한 달 전”처럼 달력 단위로
설정하는 미리 알림 기능도 포함되며, 기록된 macOS·앱 버전에서만 허용됩니다.
도구 목록에 보인다는 이유만으로 지원되는 것은 아닙니다.

이 플러그인은 Mac에서 실행됩니다. 웹 브라우저의 Claude나 원격 Linux 환경에서는
이 로컬 서버를 통해 Mac의 미리 알림에 접근할 수 없습니다.
Mac에서 변경이 확인되어도 다른 기기에 표시되었음을 보장하지 않습니다.
변경 결과가 불확실하다고 나오면 같은 명령을 반복하기 전에 해당 항목을 다시 읽도록 요청하세요.

미리 알림 작업은 로컬에서 실행되지만, 선택된 도구 결과는 사용 중인 AI 서비스에
전달됩니다. [개인정보 안내](../PRIVACY.md)와 서비스별 설정을 확인하세요.
이 프로젝트는 Apple·OpenAI·Anthropic과 제휴하거나 이들의 인증을 받은 제품이 아닙니다.

문제가 있으면 [문제 해결 안내](installation.md)와 [지원 안내](../SUPPORT.md)를 확인하세요.
실제 할 일 내용, 계정 정보, 스크린샷, 데이터베이스, 비공개 로그는 이슈에 첨부하지 마세요.
